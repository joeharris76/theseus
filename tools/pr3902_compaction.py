#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from theseus_reviewer import evidence  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "replays" / "pr3902_compaction.json"
CAPTURE_RUN = "41696376860"
REPOSITORY = "BenchBox-dev/benchbox"
CAPTURE_ARTIFACT = "ora" + "cle-verdict-capture"
WORKSPACE_PREFIX = "/home/runner/work/benchbox/benchbox/workspace"
COMPACTION_START = "auto_compaction_start"
COMPACTION_END = "auto_compaction_end"
ANSWER_KEYS = ("status", "decision")


class CompactionError(Exception):
    pass


def trimmed(event: dict[str, Any]) -> dict[str, Any]:
    keep = ("type", "action", "reason", "aborted", "willRetry")
    return {key: event[key] for key in keep if key in event}


def build(capture: dict[str, Any]) -> dict[str, Any]:
    if capture.get("run_id") != CAPTURE_RUN or capture.get("harness") != "omp":
        raise CompactionError("the capture is not the verdict capture of the expected run")
    paths: list[str] = []
    positions: dict[str, int] = {}
    calls: list[list[Any]] = []
    answer: dict[str, Any] | None = None
    order: list[dict[str, Any]] = []
    for event in capture["events"]:
        kind = event.get("type")
        if kind in (COMPACTION_START, COMPACTION_END):
            order.append({"after_calls": len(calls), **trimmed(event)})
            continue
        if kind != "message_end" or event["message"].get("role") != "assistant":
            continue
        for block in event["message"]["content"]:
            if block.get("type") == "toolCall":
                path = block["arguments"]["path"]
                if path != WORKSPACE_PREFIX and not path.startswith(WORKSPACE_PREFIX + "/"):
                    raise CompactionError(f"tool call path {path!r} is outside the workspace")
                relative = "" if path == WORKSPACE_PREFIX else path.removeprefix(WORKSPACE_PREFIX + "/")
                selector = None
                match = evidence.OMP_SELECTOR.search(relative)
                if match is not None:
                    relative, selector = relative[: match.start()], match.group(0)[1:]
                position = positions.setdefault(relative, len(positions))
                if position == len(paths):
                    paths.append(relative)
                calls.append([block["name"], position, *([] if selector is None else [selector])])
            elif block.get("type") == "text":
                try:
                    parsed = json.loads(block["text"])
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("decision"):
                    answer = {key: parsed.get(key) for key in ANSWER_KEYS}
                    answer["files_examined"] = len(parsed.get("files_examined", []))
                    answer["defects"] = len(parsed.get("defects", []))
    starts = [item for item in order if item["type"] == COMPACTION_START]
    if not starts or answer is None:
        raise CompactionError("the capture holds no compaction event or no final answer")
    return {
        "schema": 1,
        "source": {"repository": REPOSITORY, "capture_run": CAPTURE_RUN, "slot": capture.get("slot")},
        "compaction": order,
        "answer": answer,
        "paths": paths,
        "calls": calls,
    }


def dump(fixture: dict[str, Any]) -> str:
    def row(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)

    parts = []
    for key, value in fixture.items():
        if isinstance(value, list):
            body = ",\n".join("  " + row(item) for item in value)
            parts.append(f" {json.dumps(key)}: [\n{body}\n ]")
        else:
            parts.append(f" {json.dumps(key)}: {row(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild tests/fixtures/replays/pr3902_compaction.json from the verdict capture of run "
            f"{CAPTURE_RUN} (gh run download {CAPTURE_RUN} -R {REPOSITORY} -n {CAPTURE_ARTIFACT} -D DIR): "
            "the compaction events, the tool calls without outputs and the shape of the accepted final answer."
        )
    )
    parser.add_argument("--capture", type=Path, required=True, help="capture-1.json of the verdict capture artifact.")
    parser.add_argument(
        "--output",
        type=Path,
        default=FIXTURE_PATH,
        help="fixture to write (default: tests/fixtures/replays/pr3902_compaction.json).",
    )
    parser.add_argument("--check", action="store_true", help="compare with the existing fixture instead of writing.")
    args = parser.parse_args(argv)
    try:
        text = dump(build(json.loads(args.capture.read_bytes())))
    except CompactionError as exc:
        print(f"compaction fixture generation failed: {exc}", file=sys.stderr)
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
