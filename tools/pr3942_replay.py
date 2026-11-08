#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from theseus_reviewer import evidence, promotion  # noqa: E402
from theseus_reviewer.diff import hunk_index  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "replays" / "pr3942_coverage.json"
CAPTURE_RUN = "41858800966"
REPOSITORY = "BenchBox-dev/benchbox"
MERGE_BASE = "37f78fbf"
HEAD = "4cbb8205"
LOCK_PATH = ".github/theseus-lock.json"
RETIRED_NAME = "ora" + "cle"
CAPTURE_ARTIFACT = f"{RETIRED_NAME}-verdict-capture"
PULL_REQUEST_DIFF = f".{RETIRED_NAME}-pull-request.diff"
PROMOTION_DIR = f".{RETIRED_NAME}-promotion"
PROMOTION_DIFF = f"{PROMOTION_DIR}/upstream.diff"
EXPECTED_REQUIRED = 304
EXPECTED_CALLS = 405
EXPECTED_PROMOTION_ENTRIES = 213
EXPECTED_PROMOTION_DIFF_LINES = 32947
REDACTED = "[redacted]"
WORKSPACE_PREFIX = "/home/runner/work/benchbox/benchbox/workspace"
LISTING_ROW = re.compile(r"(?P<path>.+) \(\+(?P<added>\d+)/-(?P<deleted>\d+), (?P<status>[a-z]+)(?:, [^)]*)?\)")


_WORD = re.escape(RETIRED_NAME)
RENAMES: tuple[tuple[str, str], ...] = (
    (f"test_{_WORD}_review_check", "test_review_decision"),
    (rf"{_WORD}-review\.yml", "review-request.yml"),
    (rf"{_WORD}-review-shadow", "review-shadow"),
    (rf"{_WORD}-reviewers\.yml", "review.yml"),
    (rf"{_WORD}-omp-config\.yml", "review-omp-config.yml"),
    (rf"{_WORD}_reviewers", "reviewers"),
    (rf"{_WORD}_review_check", "review_check"),
    (rf"{_WORD}_local_review", "local_review"),
    (rf"local-{_WORD}", "local-review"),
    (_WORD, "review"),
)


def renamed(value: str) -> str:
    for pattern, replacement in RENAMES:
        value = re.sub(pattern, replacement, value)
    return re.sub(r"review([_-])review(?![a-z])", "review", value)


def rename_fixture(value: Any) -> Any:
    if isinstance(value, str):
        return renamed(value)
    if isinstance(value, list):
        return [rename_fixture(item) for item in value]
    if isinstance(value, dict):
        return {key: rename_fixture(item) for key, item in value.items()}
    return value


class ReplayError(Exception):
    pass


def fail(message: str) -> None:
    raise ReplayError(message)


def git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, check=False)
    if result.returncode != 0:
        fail(f"git {' '.join(arguments[:3])} failed: {result.stderr.decode('utf-8', errors='replace').strip()[:300]}")
    return result.stdout


def archive_tree(repo: Path, commit: str, prefixes: tuple[str, ...] | None) -> dict[str, bytes]:
    return promotion.extract_tar_tree(
        git(repo, "archive", "--format=tar", commit), strip_single_root=False, prefixes=prefixes
    )


def line_total(text: str) -> int:
    lines = text.split("\n")
    return len(lines) - 1 if lines[-1] == "" else len(lines)


def file_lines(raw: bytes) -> int:
    lines = raw.split(b"\n")
    return len(lines) - 1 if lines[-1] == b"" else len(lines)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def text_lines(raw: bytes | None) -> list[str] | None:
    if raw is None:
        return []
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def legacy_upstream_diff(old_tree: dict[str, bytes], new_tree: dict[str, bytes]) -> tuple[list[dict[str, Any]], str]:
    entries: list[dict[str, Any]] = []
    chunks: list[str] = []
    for path in sorted(set(old_tree) | set(new_tree)):
        old_bytes, new_bytes = old_tree.get(path), new_tree.get(path)
        if old_bytes == new_bytes:
            continue
        status = "added" if old_bytes is None else ("removed" if new_bytes is None else "modified")
        staged = f"{PROMOTION_DIR}/{'old' if status == 'removed' else 'new'}/{path}"
        old_lines = text_lines(old_bytes)
        new_lines = text_lines(new_bytes)
        if old_lines is None or new_lines is None:
            chunks.append(f"diff --git a/{path} b/{path}\nBinary files a/{path} and b/{path} differ\n")
            entries.append({"path": path, "staged": staged, "status": status, "added": 0, "deleted": 0})
            continue
        body = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))
        added = sum(1 for line in body if line.startswith("+") and not line.startswith("+++"))
        deleted = sum(1 for line in body if line.startswith("-") and not line.startswith("---"))
        chunks.append(f"diff --git a/{path} b/{path}\n" + "".join(body))
        entries.append({"path": path, "staged": staged, "status": status, "added": added, "deleted": deleted})
    return entries, "".join(chunks)


def pull_request_files(benchbox: Path) -> tuple[list[dict[str, Any]], str]:
    flags = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")
    text = git(benchbox, "diff", *flags, MERGE_BASE, HEAD).decode("utf-8", errors="replace")
    statuses = git(benchbox, "diff", "--name-status", "-z", "-M", MERGE_BASE, HEAD).split(b"\0")
    numstat = git(benchbox, "diff", "--numstat", "-z", "-M", MERGE_BASE, HEAD).split(b"\0")
    counts: dict[str, tuple[int, int]] = {}
    position = 0
    while position < len(numstat) and numstat[position]:
        added, deleted, path = numstat[position].decode("utf-8").split("\t", 2)
        if path == "":
            path = numstat[position + 2].decode("utf-8")
            position += 3
        else:
            position += 1
        counts[path] = (int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
    names = {"A": "added", "D": "removed", "M": "modified", "T": "modified", "R": "renamed", "C": "added"}
    entries: list[dict[str, Any]] = []
    position = 0
    while position < len(statuses) and statuses[position]:
        code = statuses[position].decode("ascii")
        if code[0] in "RC":
            path = statuses[position + 2].decode("utf-8")
            position += 3
        else:
            path = statuses[position + 1].decode("utf-8")
            position += 2
        added, deleted = counts[path]
        entries.append({"path": path, "staged": path, "status": names[code[0]], "added": added, "deleted": deleted})
    return entries, text


def listing_rows(capture: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = next(
        event["message"]["content"][0]["text"]
        for event in capture["events"]
        if event.get("type") == "message_end" and event["message"].get("role") == "user"
    )
    lines = prompt.split("\nRequired reads:", 1)[1].split("\n")[1:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        match = LISTING_ROW.fullmatch(line)
        if match is None:
            break
        rows.append(
            {
                "path": match["path"].rsplit(" -> ", 1)[-1],
                "added": int(match["added"]),
                "deleted": int(match["deleted"]),
                "status": match["status"],
            }
        )
    return rows


def expand(pattern: str, candidates: list[str]) -> list[str]:
    expression = re.compile("".join(".*" if part == REDACTED else re.escape(part) for part in _pieces(pattern)))
    return [candidate for candidate in candidates if expression.fullmatch(candidate)]


def _pieces(pattern: str) -> list[str]:
    pieces: list[str] = []
    for index, part in enumerate(pattern.split(REDACTED)):
        if index:
            pieces.append(REDACTED)
        if part:
            pieces.append(part)
    return pieces


def same_change(entry: dict[str, Any], row: dict[str, Any]) -> bool:
    if entry["status"] != row["status"]:
        return False
    return entry["diff"] == "pull_request" or (entry["added"], entry["deleted"]) == (row["added"], row["deleted"])


def resolve_required(
    rows: list[dict[str, Any]], pull_request: list[dict[str, Any]], upstream: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    universe: dict[str, dict[str, Any]] = {}
    for entry in pull_request:
        if entry["status"] != "removed":
            universe[entry["staged"]] = {**entry, "diff": "pull_request"}
    for entry in upstream:
        universe[entry["staged"]] = {**entry, "diff": "promotion"}
    required: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for row in rows:
        if REDACTED in row["path"]:
            pending.append(row)
            continue
        entry = universe.get(row["path"])
        if entry is None:
            fail(f"listed path {row['path']} is not in the reconstructed change")
        if not same_change(entry, row):
            fail(f"listed counts of {row['path']} differ from the reconstructed change")
        required[row["path"]] = entry
    for row in pending:
        candidates = [
            path
            for path in expand(row["path"], sorted(universe))
            if path not in required and same_change(universe[path], row)
        ]
        if len(candidates) != 1:
            fail(f"redacted row {row['path']} matches {len(candidates)} candidates: {candidates[:5]}")
        required[candidates[0]] = universe[candidates[0]]
    return required


def split_call_path(path: str, known: set[str]) -> tuple[str, str | None]:
    relative = "" if path == WORKSPACE_PREFIX else path.removeprefix(WORKSPACE_PREFIX + "/")
    if relative in known:
        return relative, None
    match = evidence.OMP_SELECTOR.search(relative)
    if match is None:
        return relative, None
    return relative[: match.start()], match.group(0)[1:]


def resolve_calls(capture: dict[str, Any], required: dict[str, dict[str, Any]]) -> list[list[str | None]]:
    known = set(required)
    calls: list[list[str | None]] = []
    for event in capture["events"]:
        if event.get("type") != "message_end" or event["message"].get("role") != "assistant":
            continue
        for block in event["message"]["content"]:
            if block.get("type") != "toolCall":
                continue
            path = block["arguments"]["path"]
            if path != WORKSPACE_PREFIX and not path.startswith(WORKSPACE_PREFIX + "/"):
                fail(f"tool call path {path!r} is outside the workspace")
            relative, selector = split_call_path(path, known)
            if REDACTED in relative:
                candidates = expand(relative, sorted(known))
                if not candidates:
                    fail(f"redacted call path {relative} matches no required file")
                relative = candidates[0]
            calls.append([block["name"], relative, selector])
    return calls


def sections(text: str, index: dict[str, list[Any]], keep: dict[str, int]) -> list[Any]:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    starts = [position for position, line in enumerate(lines, start=1) if line.startswith("diff --git ")]
    result: list[Any] = []
    for ordinal, start in enumerate(starts):
        end = starts[ordinal + 1] - 1 if ordinal + 1 < len(starts) else len(lines)
        header = re.fullmatch(r"diff --git a/(?P<old>.+) b/(?P<new>.+)", lines[start - 1])
        if header is None:
            fail(f"unreadable diff header at line {start}")
        path = header["new"]
        if path in keep:
            hunks = [
                [h.block_start, h.block_end, h.new_start, h.new_count, int(h.additive)] for h in index.get(path, [])
            ]
            result.append([keep[path], end - start + 1, hunks])
        else:
            result.append(end - start + 1)
    if starts and starts[0] != 1:
        fail("diff does not begin with a file header")
    return result


def build(args: argparse.Namespace) -> dict[str, Any]:
    return rename_fixture(_build(args))


def _build(args: argparse.Namespace) -> dict[str, Any]:
    capture_bytes = args.capture.read_bytes()
    capture = json.loads(capture_bytes)
    if capture.get("run_id") != CAPTURE_RUN or not str(capture.get("head_sha", "")).startswith(HEAD):
        fail("the capture is not the verdict capture of the expected run and head")

    lock = promotion.parse_lock(git(args.benchbox_repo, "show", f"{HEAD}:{LOCK_PATH}").decode("utf-8"))
    bootstrap = lock.bootstrap
    if bootstrap is None:
        fail("the head lock has no bootstrap baseline")
    old_tree = promotion.apply_path_map(
        archive_tree(args.benchbox_repo, bootstrap.baseline_commit, tuple(bootstrap.path_map)), bootstrap.path_map
    )
    new_tree = archive_tree(args.theseus_repo, lock.source_commit, None)
    checks = {
        "baseline_source_sha256": (promotion.tree_sha256(old_tree), bootstrap.baseline_source_sha256),
        "source_sha256": (promotion.tree_sha256(new_tree), lock.source_sha256),
        "delta_sha256": (promotion.compute_delta_sha256(old_tree, new_tree), lock.delta_sha256),
    }
    for name, (actual, expected) in checks.items():
        if actual != expected:
            fail(f"{name} is {actual}, the head lock pins {expected}")

    upstream, upstream_text = legacy_upstream_diff(old_tree, new_tree)
    pull_request, pull_request_text = pull_request_files(args.benchbox_repo)
    if len(upstream) != EXPECTED_PROMOTION_ENTRIES or line_total(upstream_text) != EXPECTED_PROMOTION_DIFF_LINES:
        fail(f"the promotion delta has {len(upstream)} entries and {line_total(upstream_text)} lines")

    required = resolve_required(listing_rows(capture), pull_request, upstream)
    if len(required) != EXPECTED_REQUIRED:
        fail(f"{len(required)} required paths were resolved, expected {EXPECTED_REQUIRED}")
    calls = resolve_calls(capture, required)
    if len(calls) != EXPECTED_CALLS:
        fail(f"{len(calls)} tool calls were resolved, expected {EXPECTED_CALLS}")

    pull_request_index = hunk_index(pull_request_text)
    upstream_index = hunk_index(upstream_text)
    kept: dict[str, dict[str, int]] = {"pull_request": {}, "promotion": {}}
    ordinals = {staged: ordinal for ordinal, staged in enumerate(sorted(required))}
    required_rows: list[list[Any]] = []
    for staged, entry in sorted(required.items()):
        kept[entry["diff"]][entry["path"] if entry["diff"] == "promotion" else entry["staged"]] = ordinals[staged]
        if entry["diff"] == "promotion":
            raw = (old_tree if entry["status"] == "removed" else new_tree)[entry["path"]]
        else:
            raw = git(args.benchbox_repo, "show", f"{HEAD}:{staged}")
        if b"\0" in raw:
            fail(f"required file {staged} is binary")
        required_rows.append([staged, file_lines(raw)])

    return {
        "schema": 1,
        "source": {
            "repository": REPOSITORY,
            "merge_base": MERGE_BASE,
            "head": HEAD,
            "capture_run": CAPTURE_RUN,
            "theseus_commit": lock.source_commit,
            "baseline_commit": bootstrap.baseline_commit,
            "baseline_source_sha256": bootstrap.baseline_source_sha256,
            "source_sha256": lock.source_sha256,
            "delta_sha256": lock.delta_sha256,
            "promotion_entries": len(upstream),
            "statuses": dict(sorted(Counter(item["status"] for item in required.values()).items())),
        },
        "pull_request_diff": {
            "file": PULL_REQUEST_DIFF,
            "lines": line_total(pull_request_text),
            "sections": sections(pull_request_text, pull_request_index, kept["pull_request"]),
        },
        "promotion_diff": {
            "file": PROMOTION_DIFF,
            "lines": line_total(upstream_text),
            "sections": sections(upstream_text, upstream_index, kept["promotion"]),
        },
        "required": required_rows,
        "calls": [
            [name, ordinals.get(path, path), *([] if selector is None else [selector])]
            for name, path, selector in calls
        ],
    }


def dump(fixture: dict[str, Any]) -> str:
    def row(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)

    parts: list[str] = []
    for key, value in fixture.items():
        if isinstance(value, dict) and "sections" in value:
            head = {name: item for name, item in value.items() if name != "sections"}
            body = ",\n".join("  " + row(item) for item in value["sections"])
            parts.append(f' {json.dumps(key)}: {row(head)[:-1]},"sections":[\n{body}\n ]}}')
        elif isinstance(value, list):
            body = ",\n".join("  " + row(item) for item in value)
            parts.append(f" {json.dumps(key)}: [\n{body}\n ]")
        else:
            parts.append(f" {json.dumps(key)}: {row(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild tests/fixtures/replays/pr3942_coverage.json from public inputs: the BenchBox merge-base "
            f"{MERGE_BASE} -> head {HEAD} history, the head lock's bootstrap baseline and the Theseus source "
            f"commit it pins, and the verdict capture of run {CAPTURE_RUN} "
            f"(gh run download {CAPTURE_RUN} -R {REPOSITORY} -n {CAPTURE_ARTIFACT} -D DIR). The tree, "
            "baseline, source and delta digests are checked against the head lock before anything is written. "
            "The capture and the trees it names carry the names Theseus used before 0.2.0; RENAMES maps every string "
            "of the result to the current vocabulary, in a fixed order, and keeps the committed row order."
        )
    )
    parser.add_argument("--benchbox-repo", type=Path, required=True, help="BenchBox clone; only read with git.")
    parser.add_argument("--theseus-repo", type=Path, required=True, help="Theseus clone; only read with git.")
    parser.add_argument("--capture", type=Path, required=True, help="capture-1.json of the verdict capture artifact.")
    parser.add_argument(
        "--output",
        type=Path,
        default=FIXTURE_PATH,
        help="fixture to write (default: tests/fixtures/replays/pr3942_coverage.json).",
    )
    parser.add_argument("--check", action="store_true", help="compare with the existing fixture instead of writing.")
    args = parser.parse_args(argv)
    try:
        text = dump(build(args))
    except ReplayError as exc:
        print(f"replay generation failed: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} differs from the regenerated fixture", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(text.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
