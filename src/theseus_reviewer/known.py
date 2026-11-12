from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import dedup
from .verdict import Finding

KNOWN_FILE = "known-defects.json"
DETAIL_LIMIT = 1000


class Sink(Protocol):
    def record(self, entries: Sequence[Mapping[str, Any]]) -> None: ...


class NoSink:
    def record(self, entries: Sequence[Mapping[str, Any]]) -> None:
        return None


class FileSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, entries: Sequence[Mapping[str, Any]]) -> None:
        if entries:
            self.path.write_text(json.dumps(list(entries), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def defect_id(finding: Finding) -> str:
    return f"kd-{dedup.fingerprint(finding.file, finding.title)}"


def entries(plan: Mapping[str, Any], findings: Sequence[Finding], reviewer: str | None) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = defect_id(finding)
        seen.setdefault(
            key,
            {
                "id": key,
                "repo": plan["repo"],
                "pr": plan["pr"],
                "head_sha": plan["head_sha"],
                "severity": finding.severity,
                "file": finding.file,
                "line": finding.line,
                "end_line": finding.end_line,
                "title": finding.title,
                "detail": finding.detail[:DETAIL_LIMIT],
                "reviewer": reviewer or "",
            },
        )
    return list(seen.values())


def publish(plan: Mapping[str, Any], final: Any, sink: Sink) -> list[dict[str, Any]]:
    judgement = final.judgement
    held = entries(plan, judgement.advisory if judgement is not None else (), final.reviewer)
    sink.record(held)
    return held
