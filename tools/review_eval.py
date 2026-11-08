#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = "evals/cases"
RESULTS_DIR = "evals/results"
SCHEMA = 1
EPSILON = 1e-9
DIGITS = 6
WEIGHTS = {"recall": 0.6, "precision": 0.2, "decision": 0.2}
DECISIONS = ("SHIP", "SHIP_WITH_FIXES", "DO_NOT_SHIP")
REVIEWED = "reviewed"
NOT_REVIEWED = "not-reviewed"
UNAVAILABLE = "unavailable"
STATUSES = (REVIEWED, NOT_REVIEWED, UNAVAILABLE)
MAX_FILE_BYTES = 65536
MAX_CASE_BYTES = 262144
MAX_FINDINGS = 40
TEXT_CAP = 1200
DEFAULT_CASE_TIMEOUT = 5400
POLICY_PATH = ".github/review.yml"
CONFIG_FILES = (
    "src/theseus_reviewer/render.py",
    "src/theseus_reviewer/classify.py",
    ".github/review.yml",
    ".github/review-omp-config.yml",
)
CONFIG_PREFIXES = ("src/theseus_reviewer/templates/",)
GUIDANCE_GLOB = re.compile(r"\.github/review-guidance[^/]*")
GUIDANCE_KEY = re.compile(r"^\s*review_guidance:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
PATH_VALUE = re.compile(r"^\s+[a-z_]+:\s*[\"']?([A-Za-z0-9_./-]+\.(?:txt|yml|yaml|md))[\"']?\s*$", re.MULTILINE)
SECRET_SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
)
CASE_KEYS = {"id", "incident", "message", "expected", "fake"}
EXPECTED_KEYS = {"decisions", "findings", "acceptable"}
FINDING_KEYS = {"id", "path", "terms"}
ACCEPTABLE_KEYS = {"path", "terms"}
REPLY_KEYS = {"decision", "summary", "files_examined", "defects"}
ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
SKELETON_EXCLUDED = ("workflows", "dependabot.yml", "theseus-lock.json")
GIT_IDENTITY = ("-c", "user.name=review-eval", "-c", "user.email=review-eval@example.invalid")


class EvalError(Exception):
    pass


def git(root: Path, *arguments: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False)
    if check and result.returncode != 0:
        raise EvalError(
            f"git {' '.join(arguments[:2])} failed: {result.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    return result.stdout if result.returncode == 0 else b""


def normalise_path(value: Any) -> str:
    return str(value).strip().removeprefix("./")


@dataclass(frozen=True)
class Matcher:
    path: str
    terms: tuple[tuple[str, ...], ...]

    def matches(self, finding: Mapping[str, Any]) -> bool:
        if normalise_path(finding.get("file", "")) != self.path:
            return False
        text = f"{finding.get('title', '')} {finding.get('detail', '')}".lower()
        return all(any(word in text for word in group) for group in self.terms)


@dataclass(frozen=True)
class Expected:
    id: str
    matcher: Matcher


@dataclass(frozen=True)
class Case:
    id: str
    incident: str
    message: str
    decisions: tuple[str, ...]
    findings: tuple[Expected, ...]
    acceptable: tuple[Matcher, ...]
    base: Mapping[str, bytes]
    head: Mapping[str, bytes]
    fake: Mapping[str, Sequence[Mapping[str, Any]]]


def _terms(value: Any, where: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or not value:
        raise EvalError(f"{where}: terms must be a non-empty list of alternative lists")
    groups = []
    for group in value:
        if not isinstance(group, list) or not group or not all(isinstance(word, str) and word for word in group):
            raise EvalError(f"{where}: every term group must be a non-empty list of non-empty strings")
        if any(word != word.lower() for word in group):
            raise EvalError(f"{where}: terms are matched case-insensitively and must be written in lower case")
        groups.append(tuple(group))
    return tuple(groups)


def _matcher(entry: Any, where: str, keys: set[str]) -> Matcher:
    if not isinstance(entry, dict) or set(entry) != keys:
        raise EvalError(f"{where}: expected exactly the keys {sorted(keys)}")
    path = entry["path"]
    if not isinstance(path, str) or normalise_path(path) != path or not path:
        raise EvalError(f"{where}: path must be a repository-relative string")
    return Matcher(path, _terms(entry["terms"], where))


def _tree(directory: Path, where: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise EvalError(f"{where}: {path.relative_to(directory)} is a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise EvalError(f"{where}: {relative} exceeds {MAX_FILE_BYTES} bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvalError(f"{where}: {relative} is not UTF-8 text") from exc
        if SECRET_SHAPES.search(text):
            raise EvalError(f"{where}: {relative} contains something shaped like a secret")
        files[relative] = data
    return files


def _reply(entry: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(entry, dict) or not {"decision", "summary", "files_examined"} <= set(entry) <= REPLY_KEYS:
        raise EvalError(f"{where}: a fake reply carries decision, summary, files_examined and optional defects")
    if entry["decision"] not in DECISIONS:
        raise EvalError(f"{where}: unknown decision {entry['decision']!r}")
    return entry


def load_case(directory: Path) -> Case:
    where = f"case {directory.name}"
    try:
        data = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"{where}: case.json is unreadable: {exc}") from exc
    if not isinstance(data, dict) or set(data) - CASE_KEYS or not CASE_KEYS - {"fake"} <= set(data):
        raise EvalError(f"{where}: case.json keys must be {sorted(CASE_KEYS)} (fake is optional)")
    if data["id"] != directory.name or not ID.fullmatch(data["id"]):
        raise EvalError(f"{where}: id must equal the directory name and match {ID.pattern}")
    for key in ("incident", "message"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise EvalError(f"{where}: {key} must be a non-empty string")
    expected = data["expected"]
    if not isinstance(expected, dict) or set(expected) != EXPECTED_KEYS:
        raise EvalError(f"{where}: expected must carry {sorted(EXPECTED_KEYS)}")
    decisions = expected["decisions"]
    if not isinstance(decisions, list) or not decisions or not set(decisions) <= set(DECISIONS):
        raise EvalError(f"{where}: decisions must be a non-empty subset of {DECISIONS}")
    findings = []
    for entry in expected["findings"]:
        matcher = _matcher(entry, f"{where} finding", FINDING_KEYS)
        findings.append(Expected(str(entry["id"]), matcher))
    if len({item.id for item in findings}) != len(findings) or not all(ID.fullmatch(item.id) for item in findings):
        raise EvalError(f"{where}: expected finding ids must be unique slugs")
    if "SHIP" in decisions and findings:
        raise EvalError(f"{where}: a case that expects findings cannot accept SHIP")
    acceptable = tuple(_matcher(entry, f"{where} acceptable", ACCEPTABLE_KEYS) for entry in expected["acceptable"])
    base = _tree(directory / "base", where) if (directory / "base").is_dir() else {}
    head = _tree(directory / "head", where) if (directory / "head").is_dir() else {}
    if not head:
        raise EvalError(f"{where}: head/ must hold at least one changed file")
    if sum(len(value) for value in (*base.values(), *head.values())) > MAX_CASE_BYTES:
        raise EvalError(f"{where}: the trees exceed {MAX_CASE_BYTES} bytes")
    if all(base.get(path) == value for path, value in head.items()):
        raise EvalError(f"{where}: head/ changes nothing against base/")
    fake = {}
    for name, replies in (data.get("fake") or {}).items():
        if not isinstance(replies, list) or not replies:
            raise EvalError(f"{where}: fake {name} must be a non-empty list of replies")
        fake[name] = tuple(_reply(entry, f"{where} fake {name}") for entry in replies)
    return Case(
        data["id"], data["incident"], data["message"], tuple(decisions), tuple(findings), acceptable, base, head, fake
    )


def load_cases(root: Path = ROOT, only: Iterable[str] | None = None) -> dict[str, Case]:
    directory = root / CASES_DIR
    if not directory.is_dir():
        raise EvalError(f"{CASES_DIR} does not exist")
    wanted = set(only or ())
    cases = {path.name: load_case(path) for path in sorted(directory.iterdir()) if path.is_dir()}
    unknown = wanted - set(cases)
    if unknown:
        raise EvalError(f"unknown case ids: {', '.join(sorted(unknown))}")
    if not cases:
        raise EvalError(f"{CASES_DIR} holds no cases")
    return {name: case for name, case in cases.items() if not wanted or name in wanted}


def normalised_decision(label: str) -> str:
    return label.strip().upper().replace(" ", "_")


def _assign(expected: Sequence[Expected], found: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    owner: dict[int, int] = {}

    def place(index: int, seen: set[int]) -> bool:
        for slot, finding in enumerate(found):
            if slot in seen or not expected[index].matcher.matches(finding):
                continue
            seen.add(slot)
            if slot not in owner or place(owner[slot], seen):
                owner[slot] = index
                return True
        return False

    for index in range(len(expected)):
        place(index, set())
    return owner


def score_observation(case: Case, observation: Mapping[str, Any]) -> dict[str, Any]:
    if observation.get("status") != REVIEWED:
        return {
            "recall": 0.0,
            "precision": 0.0,
            "decision": 0.0,
            "score": 0.0,
            "matched": [],
            "missed": [item.id for item in case.findings],
            "noise": 0,
        }
    found = list(observation.get("findings") or ())
    owner = _assign(case.findings, found)
    matched_ids = sorted(case.findings[index].id for index in owner.values())
    missed = [item.id for item in case.findings if item.id not in matched_ids]
    noise = sum(
        1
        for slot, finding in enumerate(found)
        if slot not in owner and not any(matcher.matches(finding) for matcher in case.acceptable)
    )
    recall = len(matched_ids) / len(case.findings) if case.findings else 1.0
    judged = len(matched_ids) + noise
    precision = len(matched_ids) / judged if judged else 1.0
    decision = 1.0 if normalised_decision(str(observation.get("decision", ""))) in case.decisions else 0.0
    parts = {"recall": recall, "precision": precision, "decision": decision}
    score = sum(WEIGHTS[name] * value for name, value in parts.items())
    return {
        **{name: round(value, DIGITS) for name, value in parts.items()},
        "score": round(score, DIGITS),
        "matched": matched_ids,
        "missed": missed,
        "noise": noise,
    }


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), DIGITS) if values else 0.0


def case_score(runs: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {name: _mean([run["score"][name] for run in runs]) for name in (*WEIGHTS, "score")}


def aggregate(scores: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    return {name: _mean([item[name] for item in scores.values()]) for name in (*WEIGHTS, "score")}


def tree_digest(entries: Mapping[str, bytes]) -> str:
    lines = "".join(f"{path}\t{hashlib.sha256(data).hexdigest()}\n" for path, data in sorted(entries.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


Reader = Callable[[str], bytes | None]


@dataclass(frozen=True)
class Tree:
    paths: tuple[str, ...]
    read: Reader


def committed_tree(root: Path, ref: str) -> Tree:
    listing = git(root, "ls-tree", "-r", "--name-only", "-z", ref).decode("utf-8").split("\0")
    paths = tuple(sorted(path for path in listing if path))

    def read(path: str) -> bytes | None:
        result = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

    return Tree(paths, read)


def working_tree(root: Path) -> Tree:
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode("utf-8").split("\0")
    paths = tuple(sorted({path for path in listing if path and (root / path).is_file()}))

    def read(path: str) -> bytes | None:
        target = root / path
        return target.read_bytes() if target.is_file() else None

    return Tree(paths, read)


def guidance_paths(tree: Tree) -> set[str]:
    named = {path for path in tree.paths if GUIDANCE_GLOB.fullmatch(path)}
    policy = tree.read(POLICY_PATH)
    if policy is not None:
        named.update(GUIDANCE_KEY.findall(policy.decode("utf-8", "replace")))
    return named


def is_reviewer_config(path: str, guidance: set[str]) -> bool:
    return path in CONFIG_FILES or path.startswith(CONFIG_PREFIXES) or path in guidance


def config_entries(tree: Tree) -> dict[str, bytes]:
    guidance = guidance_paths(tree)
    entries = {}
    for path in tree.paths:
        if is_reviewer_config(path, guidance):
            data = tree.read(path)
            if data is not None:
                entries[path] = data
    return entries


def corpus_entries(tree: Tree) -> dict[str, bytes]:
    return {
        path: data for path in tree.paths if path.startswith(f"{CASES_DIR}/") and (data := tree.read(path)) is not None
    }


def skeleton_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    github = root / ".github"
    if github.is_dir():
        for path in sorted(github.rglob("*")):
            relative = path.relative_to(root).as_posix()
            parts = path.relative_to(github).parts
            if path.is_file() and parts[0] not in SKELETON_EXCLUDED:
                files[relative] = path.read_bytes()
    policy = files.get(POLICY_PATH, b"").decode("utf-8", "replace")
    for name in {*GUIDANCE_KEY.findall(policy), *PATH_VALUE.findall(policy)}:
        target = root / name
        if name not in files and target.is_file() and ".." not in Path(name).parts:
            files[name] = target.read_bytes()
    if POLICY_PATH not in files:
        raise EvalError(f"{POLICY_PATH} is missing from {root}")
    return files


def _write_tree(destination: Path, files: Mapping[str, bytes]) -> None:
    for relative, data in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _commit(repository: Path, message: str) -> str:
    git(repository, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repository), *GIT_IDENTITY, "commit", "--quiet", "--allow-empty", "-m", message],
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"},
    )
    return git(repository, "rev-parse", "HEAD").decode("utf-8").strip()


def gated_base(policy: bytes) -> str:
    match = re.search(r"^\s+gated_base:\s*[\"']?([A-Za-z0-9._/-]+)", policy.decode("utf-8", "replace"), re.MULTILINE)
    if match is None:
        raise EvalError("the consumer policy names no gated_base")
    return match.group(1)


def build_repository(case: Case, skeleton: Mapping[str, bytes], destination: Path) -> tuple[Path, str, str]:
    branch = gated_base(skeleton[POLICY_PATH])
    destination.mkdir(parents=True)
    git(destination, "init", "--quiet", f"--initial-branch={branch}")
    _write_tree(destination, {**skeleton, **case.base})
    base = _commit(destination, "base")
    git(destination, "switch", "--quiet", "-c", f"eval/{case.id}")
    _write_tree(destination, case.head)
    head = _commit(destination, case.message)
    return destination, base, head


def parse_outcome(state: Path) -> dict[str, Any] | None:
    for path in sorted((state / "cache").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return data
    return None


def _clip(value: Any) -> str:
    return " ".join(str(value).split())[:TEXT_CAP]


def observation_from_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    findings = [
        {
            "severity": str(item.get("severity", "")),
            "file": normalise_path(item.get("file", "")),
            "line": item.get("line") if isinstance(item.get("line"), int) else 0,
            "title": _clip(item.get("title", "")),
            "detail": _clip(item.get("detail", "")),
        }
        for item in outcome["findings"][:MAX_FINDINGS]
        if isinstance(item, dict)
    ]
    return {
        "status": REVIEWED,
        "decision": normalised_decision(str(outcome.get("label", ""))),
        "reviewer": str(outcome.get("reviewer", "")),
        "summary": _clip(outcome.get("summary", "")),
        "findings": findings,
    }


def unreviewed(status: str, detail: str) -> dict[str, Any]:
    return {"status": status, "decision": "", "reviewer": "", "summary": "", "findings": [], "detail": _clip(detail)}


def review_command(base: str, head: str, result_file: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "theseus_reviewer",
        "local-review",
        "--policy",
        POLICY_PATH,
        "--repo",
        "eval/eval",
        "--member",
        "--result-json",
        str(result_file),
        "--base-ref",
        base,
        "--trusted-ref",
        base,
        "--head",
        head,
    ]


def run_live_case(
    case: Case,
    skeleton: Mapping[str, bytes],
    source: Path,
    timeout: int,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="review-eval-") as scratch:
        work = Path(scratch)
        repository, base, head = build_repository(case, skeleton, work / "repository")
        state = work / "state"
        environment = {
            **(os.environ if base_environment is None else base_environment),
            "THESEUS_LOCAL_STATE": str(state),
            "PYTHONPATH": str(source / "src"),
        }
        try:
            result = subprocess.run(
                review_command(base, head, work / "member.json"),
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return unreviewed(UNAVAILABLE, f"the review did not finish within {timeout} seconds")
        if result.returncode == 2:
            return unreviewed(
                UNAVAILABLE, result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "exit 2"
            )
        outcome = parse_outcome(state)
        if outcome is None:
            return unreviewed(NOT_REVIEWED, f"exit {result.returncode} and no recorded outcome")
        return observation_from_outcome(outcome)


def build_result(
    cases: Mapping[str, Case],
    runs: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    mode: str,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    recorded: dict[str, Any] = {}
    for case_id, case in cases.items():
        scored = [
            {"observation": dict(observation), "score": score_observation(case, observation)}
            for observation in runs[case_id]
        ]
        recorded[case_id] = {"runs": scored, "score": case_score(scored)}
    complete = all(run["observation"]["status"] == REVIEWED for item in recorded.values() for run in item["runs"])
    return {
        "schema": SCHEMA,
        "mode": mode,
        **meta,
        "complete": complete,
        "cases": recorded,
        "aggregate": aggregate({case_id: item["score"] for case_id, item in recorded.items()}),
    }


def rescored(result: Mapping[str, Any], cases: Mapping[str, Case]) -> dict[str, float]:
    scores = {}
    for case_id, case in cases.items():
        entry = (result.get("cases") or {}).get(case_id)
        if not isinstance(entry, dict) or not entry.get("runs"):
            raise EvalError(f"the result has no runs for case {case_id}")
        scores[case_id] = case_score([{"score": score_observation(case, run["observation"])} for run in entry["runs"]])
    return aggregate(scores)


def usable_result(result: Any, where: str) -> list[str]:
    problems = []
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        return [f"{where}: not a schema {SCHEMA} result"]
    if result.get("mode") != "live":
        problems.append(f"{where}: mode is {result.get('mode')!r}; only a live run counts")
    if result.get("complete") is not True:
        problems.append(f"{where}: the run is incomplete; every case must have been reviewed")
    if result.get("dirty") is not False:
        problems.append(f"{where}: the run was made on uncommitted reviewer configuration")
    return problems


def read_results(tree: Tree) -> dict[str, Any]:
    results = {}
    for path in tree.paths:
        if path.startswith(f"{RESULTS_DIR}/") and path.endswith(".json"):
            data = tree.read(path)
            try:
                results[path] = json.loads(data.decode("utf-8")) if data is not None else None
            except ValueError:
                results[path] = None
    return results


def latest_baseline(results: Mapping[str, Any]) -> tuple[str, Any] | None:
    usable = [(path, data) for path, data in sorted(results.items()) if not usable_result(data, path)]
    return usable[-1] if usable else None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    lines: tuple[str, ...]


def check_change(root: Path, base: str, head: str) -> Verdict:
    base_tree, head_tree = committed_tree(root, base), committed_tree(root, head)
    base_guidance, head_guidance = guidance_paths(base_tree), guidance_paths(head_tree)
    changed = [
        path
        for path in git(root, "diff", "--name-only", "-z", base, head).decode("utf-8").split("\0")
        if path and (is_reviewer_config(path, base_guidance) or is_reviewer_config(path, head_guidance))
    ]
    if not changed:
        return Verdict(True, ("no template, policy or guidance change; nothing to score",))
    listing = [f"reviewer configuration changed: {', '.join(changed)}"]
    baseline = latest_baseline(read_results(base_tree))
    if baseline is None:
        return Verdict(
            True,
            (*listing, f"no usable live baseline is recorded under {RESULTS_DIR} on the base; the rule is inactive"),
        )
    baseline_path, baseline_result = baseline
    cases = load_cases_from_tree(base_tree)
    attached = [
        (path, data)
        for path, data in sorted(read_results(head_tree).items())
        if path not in read_results(base_tree) and not usable_result(data, path)
    ]
    digest = tree_digest(config_entries(head_tree))
    corpus = tree_digest(corpus_entries(head_tree))
    matching = [
        (path, data)
        for path, data in attached
        if data.get("config_digest") == digest and data.get("corpus_digest") == corpus
    ]
    if not matching:
        return Verdict(
            False,
            (
                *listing,
                f"attach a live eval run for this exact configuration under {RESULTS_DIR}/ "
                f"(config digest {digest[:12]}, corpus digest {corpus[:12]}): "
                "python tools/review_eval.py run --mode live",
                *(f"ignored {path}: it describes a different configuration or corpus" for path, _ in attached),
            ),
        )
    path, result = matching[-1]
    try:
        before, after = rescored(baseline_result, cases), rescored(result, cases)
    except EvalError as exc:
        return Verdict(False, (*listing, str(exc)))
    lines = (*listing, f"baseline {baseline_path}: score {before['score']}", f"attached {path}: score {after['score']}")
    if after["score"] + EPSILON < before["score"]:
        return Verdict(False, (*lines, "the reviewer configuration change lowers the eval score"))
    return Verdict(True, (*lines, "the eval score is not lower than the baseline"))


def load_cases_from_tree(tree: Tree) -> dict[str, Case]:
    with tempfile.TemporaryDirectory(prefix="review-eval-cases-") as scratch:
        destination = Path(scratch)
        _write_tree(destination, corpus_entries(tree))
        return load_cases(destination)


def git_state(root: Path) -> dict[str, Any]:
    head = git(root, "rev-parse", "HEAD").decode("utf-8").strip()
    tracked = working_tree(root)
    committed = committed_tree(root, "HEAD")
    changed = [
        path
        for path, data in {**config_entries(tracked), **corpus_entries(tracked)}.items()
        if committed.read(path) != data
    ]
    gone = [path for path in {**config_entries(committed), **corpus_entries(committed)} if tracked.read(path) is None]
    return {
        "git_sha": head,
        "dirty": bool(changed or gone),
        "config_digest": tree_digest(config_entries(tracked)),
        "corpus_digest": tree_digest(corpus_entries(tracked)),
    }


def summary_text(result: Mapping[str, Any], baseline: tuple[str, float] | None) -> str:
    rows = ["| case | decision | recall | precision | decision ok | score |", "|---|---|---|---|---|---|"]
    for case_id, entry in result["cases"].items():
        decisions = ", ".join(
            sorted({run["observation"].get("decision") or run["observation"]["status"] for run in entry["runs"]})
        )
        score = entry["score"]
        rows.append(
            f"| {case_id} | {decisions} | {score['recall']} | {score['precision']} | {score['decision']} | {score['score']} |"
        )
    total = result["aggregate"]
    lines = [
        f"## Review eval {result['date']} ({result['git_sha'][:12]})",
        "",
        f"Aggregate score **{total['score']}** (recall {total['recall']}, precision {total['precision']}, decision {total['decision']}); "
        f"complete: {result['complete']}.",
    ]
    if baseline is not None:
        lines.append(f"Baseline {baseline[0]}: {baseline[1]}.")
    return "\n".join([*lines, "", *rows, ""])


def command_validate(arguments: argparse.Namespace) -> int:
    cases = load_cases(Path(arguments.root))
    print(f"{len(cases)} cases valid: {', '.join(cases)}")
    return 0


def command_run(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root).resolve()
    cases = load_cases(root, arguments.case)
    consumer = Path(arguments.consumer).resolve()
    skeleton = skeleton_files(consumer)
    state = git_state(root)
    if state["dirty"] and not arguments.allow_dirty:
        print(
            "refusing: reviewer configuration or the corpus has uncommitted changes (--allow-dirty records the run as dirty)",
            file=sys.stderr,
        )
        return 2
    runs: dict[str, list[dict[str, Any]]] = {}
    for case_id, case in cases.items():
        runs[case_id] = []
        for number in range(arguments.repeat):
            print(f"running {case_id} ({number + 1}/{arguments.repeat})", flush=True)
            runs[case_id].append(run_live_case(case, skeleton, root, arguments.case_timeout))
    meta = {"date": datetime.now(UTC).strftime("%Y-%m-%d"), "policy": POLICY_PATH, "repeat": arguments.repeat, **state}
    result = build_result(cases, runs, mode="live", meta=meta)
    output = (
        Path(arguments.out) if arguments.out else root / RESULTS_DIR / f"{meta['date']}-{state['git_sha'][:12]}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    baseline = None
    if arguments.compare:
        found = latest_baseline(
            {path: data for path, data in read_results(working_tree(root)).items() if root / path != output}
        )
        if found is not None:
            baseline = (found[0], rescored(found[1], cases)["score"])
    text = summary_text(result, baseline)
    print(text)
    if arguments.summary:
        with open(arguments.summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(f"wrote {output}")
    if not result["complete"]:
        print("the run is incomplete: at least one case was not reviewed", file=sys.stderr)
        return 1
    if baseline is not None and result["aggregate"]["score"] + EPSILON < baseline[1]:
        print(
            f"the aggregate score {result['aggregate']['score']} is below the baseline {baseline[1]}", file=sys.stderr
        )
        return 1
    return 0


def command_score(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root)
    cases = load_cases(root)
    result = json.loads(Path(arguments.result).read_text(encoding="utf-8"))
    print(json.dumps(rescored(result, cases), indent=2, sort_keys=True))
    return 0


def command_check_change(arguments: argparse.Namespace) -> int:
    verdict = check_change(Path(arguments.root).resolve(), arguments.base, arguments.head)
    for line in verdict.lines:
        print(line)
    return 0 if verdict.ok else 1


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description="Score the reviewer against the incident corpus")
    main.add_argument("--root", default=str(ROOT))
    commands = main.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.set_defaults(handler=command_validate)
    run = commands.add_parser("run")
    run.add_argument("--mode", choices=["live"], default="live")
    run.add_argument("--case", action="append", default=[])
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--out", default="")
    run.add_argument("--consumer", default=str(ROOT))
    run.add_argument("--case-timeout", type=int, default=DEFAULT_CASE_TIMEOUT)
    run.add_argument("--summary", default="")
    run.add_argument("--compare", action="store_true")
    run.add_argument("--allow-dirty", action="store_true")
    run.set_defaults(handler=command_run)
    score = commands.add_parser("score")
    score.add_argument("result")
    score.set_defaults(handler=command_score)
    check = commands.add_parser("check-change")
    check.add_argument("--base", required=True)
    check.add_argument("--head", default="HEAD")
    check.set_defaults(handler=command_check_change)
    return main


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if getattr(arguments, "repeat", 1) < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    try:
        return int(arguments.handler(arguments))
    except EvalError as exc:
        print(f"review-eval: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
