from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import cli, coverage, evidence, local, promotion, runner
from .policy import PolicyError

SCHEMA = 1
EXIT_COVERED = 0
EXIT_NOT_COVERED = 3
EXIT_UNDETERMINED = 2
DEFAULT_READ_LINES = 300
_SELECTOR_CHUNK = re.compile(r"(?:L?(?P<a>\d+)(?:(?P<sep>-|\.\.)(?:L?(?P<b>\d+))?|\+L?(?P<n>\d+))?|-(?P<tail>\d+))")


class Undetermined(Exception):
    pass


def _chunk_ranges(chunk: str, total: int) -> coverage.Ranges:
    match = _SELECTOR_CHUNK.fullmatch(chunk.strip())
    if match is None or total < 1:
        return ()
    if match.group("tail"):
        return ((max(total - int(match.group("tail")) + 1, 1), total),)
    start = int(match.group("a"))
    if match.group("n"):
        end = start + int(match.group("n")) - 1
    elif match.group("b"):
        end = int(match.group("b"))
    else:
        end = start + DEFAULT_READ_LINES - 1
    end = min(end, total)
    return ((start, end),) if start <= end else ()


def selector_ranges(path: str, total: int) -> coverage.Ranges:
    selector = evidence.OMP_SELECTOR.search(path)
    if selector is None:
        return ((1, min(total, DEFAULT_READ_LINES)),) if total else ()
    parts = [part for part in selector.group(0)[1:].split(":") if part != "raw"]
    if not parts:
        return ((1, min(total, DEFAULT_READ_LINES)),) if total else ()
    spans = [span for chunk in parts[0].split(",") for span in _chunk_ranges(chunk.lstrip(":"), total)]
    return coverage.join(tuple(spans))


def _stdout(events: list[Any]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _known(workspace: Path) -> set[str]:
    known: set[str] = set()
    for root, directories, files in os.walk(workspace):
        directories[:] = [name for name in directories if name != ".git"]
        base = Path(root).relative_to(workspace)
        known.update((base / name).as_posix() for name in files)
    return known


def _resolve(path: str, known: set[str]) -> str | None:
    plain = evidence.OMP_SELECTOR.sub("", path.strip())
    parts = [part for part in plain.split("/") if part and part != "."]
    for index in range(len(parts)):
        candidate = "/".join(parts[index:])
        if candidate in known:
            return candidate
    return None


def _recorded(capture: Mapping[str, Any]) -> list[tuple[str, coverage.Ranges | None]]:
    if capture.get("schema") == 2 and isinstance(capture.get("reads"), list):
        found = []
        for item in capture["reads"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            lines = item.get("lines")
            ranges = tuple((int(a), int(b)) for a, b in lines) if isinstance(lines, list) else ()
            found.append((item["path"], ranges))
        return found
    run = evidence.trace("omp", _stdout(capture.get("events") or []))
    return [(path, None) for path, _, _, elided in run.ledger if not elided]


def returned_from_capture(capture: Mapping[str, Any], workspace: Path) -> tuple[dict[str, coverage.Ranges], int]:
    known = _known(workspace)
    gathered: dict[str, list[coverage.Ranges]] = {}
    unresolved = 0
    for path, ranges in _recorded(capture):
        if ";" in path:
            continue
        target = _resolve(path, known)
        if target is None:
            unresolved += 1
            continue
        if ranges is None:
            data = (workspace / target).read_bytes()
            total = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
            ranges = selector_ranges(path, total)
        if ranges:
            gathered.setdefault(target, []).append(ranges)
    return {target: coverage.join(*groups) for target, groups in gathered.items()}, unresolved


def compacted_in(capture: Mapping[str, Any]) -> bool:
    return any(
        isinstance(event, dict) and event.get("type") == evidence.COMPACTION_EVENT
        for event in capture.get("events") or []
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Undetermined(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _workspace(root: Path, head: str, plan_dir: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--shared", str(root), str(destination)],
        capture_output=True,
        check=True,
    )
    _git(destination, "checkout", "--quiet", "--detach", head)
    diff_file = plan_dir / cli.DIFF_FILE
    if diff_file.is_file() and diff_file.read_text(encoding="utf-8").strip():
        if runner.stage_pull_request_diff(diff_file, destination) is None:
            raise Undetermined("the pull request diff could not be staged into the audit workspace")
    if (plan_dir / promotion.PROMOTION_PLAN_DIR).is_dir():
        if promotion.stage_promotion_workspace(plan_dir, destination) is None:
            raise Undetermined("the promotion tree could not be staged into the audit workspace")
    return destination


def _plan(args: argparse.Namespace, root: Path, work: Path) -> tuple[dict[str, Any], Path, str]:
    try:
        local._default_base_ref(args, root)
        head = local._git(root, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
        trusted = local.Trusted.read(root, args.trusted_ref or args.base_ref, args.policy)
    except (local.Unavailable, local.Refused, PolicyError) as exc:
        raise Undetermined(str(exc)) from exc
    policy = trusted.write(work / "trusted")
    run = local.Run(
        root,
        work,
        policy,
        work / "trusted",
        local._repository(root, args.repo),
        head,
        args.base_ref,
        None,
        uuid.uuid4().hex,
        local.Reporter(),
        "",
    )
    code, _ = run.phase("plan")
    if code != 0 or not run.layout.plan_file.is_file():
        raise Undetermined("the review plan cannot be built")
    plan = json.loads(run.layout.plan_file.read_text(encoding="utf-8"))
    if plan["decision"] != cli.REVIEW:
        raise Undetermined(f"the plan does not call for a review: {plan['decision_reason']}")
    return plan, run.layout.plan, head


def report(plan: Mapping[str, Any], workspace: Path, capture: Mapping[str, Any]) -> dict[str, Any]:
    found = coverage.targets(plan, workspace)
    protected = set(plan.get("evidence_protected_files") or ())
    needed = evidence.required_files(
        plan.get("evidence_files") or (),
        workspace,
        plan.get("binary_files") or (),
        plan.get("evidence_exempt_paths") or (),
        lambda path: path in protected,
    )
    reads, unresolved = returned_from_capture(capture, workspace)
    inline = plan.get("brief_mode") == evidence.INLINE
    result = coverage.evaluate(found, needed, {} if inline else reads, workspace)
    open_files = [] if inline else list(result.files)
    omitted = [path for path in needed if path not in found or not found[path].listed] if inline else []
    squashed = compacted_in(capture)
    return {
        "schema": SCHEMA,
        "capture_schema": capture.get("schema"),
        "reads": "recorded" if capture.get("schema") == 2 else "reconstructed",
        "brief_mode": plan.get("brief_mode"),
        "required": len(needed),
        "hunks": {"total": result.total, "covered": result.covered},
        "uncovered": [
            {"file": item.path, "source": item.source, "start": item.start, "end": item.end}
            for item in ([] if inline else result.uncovered)
        ],
        "untouched": [] if inline else list(result.untouched),
        "omitted_from_brief": omitted,
        "unresolved_reads": unresolved,
        "compacted": squashed,
        "covered": not open_files and not omitted and not squashed,
    }


def command_audit(args: argparse.Namespace) -> int:
    try:
        capture = json.loads(Path(args.capture).read_text(encoding="utf-8"))
        if not isinstance(capture, dict):
            raise Undetermined("the capture is not a JSON object")
        root = Path(args.repo_root).resolve()
        work = Path(tempfile.mkdtemp(prefix="review-audit-"))
        try:
            plan, plan_dir, head = _plan(args, root, work)
            workspace = _workspace(root, head, plan_dir, work / "workspace")
            result = report(plan, workspace, capture)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except (Undetermined, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"audit-coverage: {exc}")
        return EXIT_UNDETERMINED
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"hunks covered {result['hunks']['covered']}/{result['hunks']['total']}"
            f" ({result['reads']} reads, {result['required']} required files)"
        )
        for item in result["uncovered"][:20]:
            print(f"  not seen: {item['file']} ({item['source']} lines {item['start']}-{item['end']})")
        for path in result["untouched"][:20]:
            print(f"  never read: {path}")
        for path in result["omitted_from_brief"][:20]:
            print(f"  not in the brief: {path}")
        if result["compacted"]:
            print("  the reviewer's context was compacted")
        print("covered" if result["covered"] else "not covered")
    return EXIT_COVERED if result["covered"] else EXIT_NOT_COVERED


def register(commands: Any) -> None:
    audit = commands.add_parser("audit-coverage")
    audit.add_argument("--capture", required=True)
    audit.add_argument("--policy", required=True)
    audit.add_argument("--repo", default="")
    audit.add_argument("--repo-root", default=".")
    audit.add_argument("--base-ref", default="")
    audit.add_argument("--trusted-ref", default="")
    audit.add_argument("--head", default="HEAD")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(handler=command_audit)
