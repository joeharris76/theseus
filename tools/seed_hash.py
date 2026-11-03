#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "tools" / "seed_map.json"


def load_map() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _git_show(repo: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"], check=True, capture_output=True
    ).stdout


def digest(entries: list[dict], read: Callable[[dict], bytes], kinds: frozenset[str]) -> str:
    lines = sorted(
        f"{entry['theseus']}\t{hashlib.sha256(read(entry)).hexdigest()}\n" for entry in entries if entry["kind"] in kinds
    )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalized seed hash: sha256 over sorted '<theseus path>\\t<sha256 of file bytes>\\n' lines "
        "for the mapped entries of the selected kinds."
    )
    origin = parser.add_mutually_exclusive_group(required=True)
    origin.add_argument("--benchbox-repo", type=Path, help="BenchBox checkout; reads the baseline commit blobs.")
    origin.add_argument("--tree", type=Path, help="Directory holding files at their Theseus paths.")
    origin.add_argument("--git-rev", help="Revision of this repository holding files at their Theseus paths.")
    parser.add_argument("--kind", action="append", choices=("source", "test", "fixture"), default=None)
    parser.add_argument("--verify", metavar="HEX", help="Exit non-zero unless the digest equals HEX.")
    args = parser.parse_args(argv)

    seed = load_map()
    entries = seed["entries"]
    kinds = frozenset(args.kind or ["source"])
    if args.benchbox_repo:
        baseline = seed["baseline_commit"]
        result = digest(entries, lambda e: _git_show(args.benchbox_repo, baseline, e["benchbox"]), kinds)
    elif args.tree:
        result = digest(entries, lambda e: (args.tree / e["theseus"]).read_bytes(), kinds)
    else:
        result = digest(entries, lambda e: _git_show(ROOT, args.git_rev, e["theseus"]), kinds)
    print(result)
    if args.verify and args.verify != result:
        print(f"seed hash mismatch: expected {args.verify}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
