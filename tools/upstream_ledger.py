#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "tools" / "upstream_ledger.json"
SEED_MAP_PATH = ROOT / "tools" / "seed_map.json"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
DISPOSITIONS = ("port", "noop", "pending")
RECORD = "\x1e"
FIELD = "\x1f"


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    paths: tuple[str, ...]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise LedgerError(f"git {args[0]} failed in {repo}: {result.stderr.strip()[:300]}")
    return result.stdout


def load_ledger(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"cannot read the ledger {path}: {exc}") from exc
    if data.get("schema") != 1 or not FULL_SHA.fullmatch(str(data.get("baseline", ""))):
        raise LedgerError(f"{path} is not a schema 1 ledger with a full baseline commit")
    return data


def watched(ledger: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    mapped = {entry["benchbox"] for entry in json.loads(SEED_MAP_PATH.read_text(encoding="utf-8"))["entries"]}
    exact = tuple(sorted(mapped | set(ledger["watch"]["files"])))
    return exact, tuple(sorted(ledger["watch"]["prefixes"]))


def pathspecs(ledger: dict) -> list[str]:
    exact, prefixes = watched(ledger)
    return [*(f":(literal){path}" for path in exact), *(f":(top){prefix}" for prefix in prefixes)]


def touching(repo: Path, ledger: dict, ref: str) -> list[Commit]:
    baseline = ledger["baseline"]
    if subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", baseline, ref], capture_output=True, check=False
    ).returncode:
        raise LedgerError(f"{ref} does not descend from the baseline {baseline}")
    format_ = f"{RECORD}%H{FIELD}%s"
    raw = _git(
        repo,
        "log",
        "--reverse",
        "--no-renames",
        f"--format={format_}",
        "--name-only",
        f"{baseline}..{ref}",
        "--",
        *pathspecs(ledger),
    )
    commits: list[Commit] = []
    for block in raw.split(RECORD)[1:]:
        head, _, names = block.partition("\n")
        sha, _, subject = head.partition(FIELD)
        commits.append(Commit(sha, subject, tuple(name for name in names.splitlines() if name)))
    return commits


def range_size(repo: Path, ledger: dict, ref: str) -> int:
    return int(_git(repo, "rev-list", "--count", f"{ledger['baseline']}..{ref}").strip())


def validate_entry(entry: dict) -> str | None:
    sha = str(entry.get("commit", ""))
    if not FULL_SHA.fullmatch(sha):
        return f"entry {sha!r} is not a full commit"
    disposition = entry.get("disposition")
    if disposition not in DISPOSITIONS:
        return f"{sha[:12]}: disposition must be one of {', '.join(DISPOSITIONS)}"
    if disposition == "port" and not FULL_SHA.fullmatch(str(entry.get("port_commit", ""))):
        return f"{sha[:12]}: a port entry needs the full Theseus port_commit"
    if disposition == "noop" and not str(entry.get("rationale", "")).strip():
        return f"{sha[:12]}: a noop entry needs a rationale"
    if disposition == "port" and not str(entry.get("effects", "")).strip():
        return f"{sha[:12]}: a port entry needs the interface, policy and cache effects"
    return None


def port_commit_missing(entry: dict) -> bool:
    if entry.get("disposition") != "port":
        return False
    result = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{entry['port_commit']}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode != 0


@dataclass(frozen=True)
class Report:
    ref: str
    head: str
    examined: int
    commits: tuple[Commit, ...]
    pending: tuple[Commit, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.pending and not self.problems


def evaluate(repo: Path, ledger: dict, ref: str) -> Report:
    head = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    commits = touching(repo, ledger, head)
    by_commit: dict[str, dict] = {}
    problems: list[str] = []
    for entry in ledger["entries"]:
        problem = validate_entry(entry)
        if problem:
            problems.append(problem)
            continue
        if entry["commit"] in by_commit:
            problems.append(f"{entry['commit'][:12]} appears twice in the ledger")
        by_commit[entry["commit"]] = entry
        if port_commit_missing(entry):
            problems.append(
                f"{entry['commit'][:12]}: port commit {entry['port_commit'][:12]} is not in this repository"
            )
    touched = {commit.sha for commit in commits}
    problems.extend(
        f"{sha[:12]}: the ledger entry does not match any inventory-touching commit in {ref}; remove it"
        for sha in by_commit
        if sha not in touched
    )
    pending = tuple(
        commit for commit in commits if by_commit.get(commit.sha, {}).get("disposition", "pending") == "pending"
    )
    return Report(ref, head, range_size(repo, ledger, head), tuple(commits), pending, tuple(problems))


def render(report: Report, ledger: dict) -> str:
    lines = [
        f"baseline {ledger['baseline']}",
        f"ref {report.ref} = {report.head}",
        f"{report.examined} commits since baseline, {len(report.commits)} touch the extraction inventory, "
        f"{len(report.pending)} pending",
    ]
    for commit in report.commits:
        state = "pending" if commit in report.pending else "recorded"
        lines.append(f"  {commit.sha} {state} {commit.subject}")
        lines.extend(f"      {path}" for path in commit.paths)
    lines.extend(f"problem: {problem}" for problem in report.problems)
    lines.append(
        "ledger current: no un-ported upstream change" if report.ok else "ledger stale: un-ported upstream change"
    )
    return "\n".join(lines)


def refresh(ledger: dict, report: Report, path: Path) -> int:
    added = [commit for commit in report.commits if commit.sha not in {entry["commit"] for entry in ledger["entries"]}]
    ledger["entries"].extend(
        {"commit": commit.sha, "subject": commit.subject, "paths": list(commit.paths), "disposition": "pending"}
        for commit in added
    )
    path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return len(added)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List BenchBox commits since the baseline that touch the extraction inventory and fail while "
        "any lacks a port commit or a no-op rationale in the ledger."
    )
    parser.add_argument("command", choices=("check", "list", "refresh"))
    parser.add_argument("--benchbox-repo", type=Path, required=True, help="BenchBox checkout.")
    parser.add_argument(
        "--ref", required=True, help="BenchBox ref to compare with the baseline, for example origin/develop."
    )
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    args = parser.parse_args(argv)
    try:
        ledger = load_ledger(args.ledger)
        report = evaluate(args.benchbox_repo, ledger, args.ref)
        if args.command == "refresh":
            print(f"added {refresh(ledger, report, args.ledger)} pending entries to {args.ledger}")
            report = evaluate(args.benchbox_repo, load_ledger(args.ledger), args.ref)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render(report, ledger))
    if args.command == "list":
        return 0
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
