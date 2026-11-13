"""Optional mirror of threadless findings as annotations on a non-required check run.

The mirror is presentation only. It never carries a conclusion other than neutral, is never a required context, and
nothing reads it back: readers, the gate and the digest keep using the review, its record and its threads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import layout
from .verdict import Finding

FILE = "check-run.json"
NAME = "review-findings"
CONCLUSION = "neutral"
MAX_PER_REQUEST = 50
MAX_TITLE = 255
MAX_MESSAGE = 60_000
LEVELS = {"Critical": "failure", "High": "failure", "Medium": "warning", "Low": "notice"}
MIRROR_NOTE = "This is a mirror of a finding in the review. Reply to it or dispute it in the review, not here."
TRUNCATED = "…"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(TRUNCATED)].rstrip() + TRUNCATED


def annotation(finding: Finding, defect_id: str | None) -> dict[str, Any]:
    label = f"{defect_id} · " if defect_id else ""
    title = f"{finding.severity} · {label}{layout.neutralize_inline(finding.title)}"
    return {
        "path": layout.plain_path(finding.file),
        "start_line": finding.line,
        "end_line": max(finding.end_line or finding.line, finding.line),
        "annotation_level": LEVELS.get(finding.severity, "warning"),
        "title": _cut(title, MAX_TITLE),
        "message": _cut(f"{layout.neutralize(finding.detail).rstrip()}\n\n{MIRROR_NOTE}", MAX_MESSAGE),
    }


def payload(plan: Mapping[str, Any], threadless: Sequence[tuple[Finding, str | None]]) -> dict[str, Any] | None:
    if not threadless:
        return None
    count = len(threadless)
    summary = (
        f"{count} finding{'s' if count != 1 else ''} from the review of {plan['head_sha']} "
        f"{'has' if count == 1 else 'have'} no review thread, because the line is outside the diff. "
        f"{MIRROR_NOTE}"
    )
    return {
        "name": NAME,
        "head_sha": plan["head_sha"],
        "title": f"{count} finding{'s' if count != 1 else ''} without a review thread",
        "summary": summary,
        "annotations": [annotation(finding, defect_id) for finding, defect_id in threadless],
    }


def requests(mirror: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The check-run bodies to send in order: the first creates the run, each later one appends a batch."""
    annotations = list(mirror["annotations"])
    output = {"title": mirror["title"], "summary": mirror["summary"]}
    batches = [annotations[start : start + MAX_PER_REQUEST] for start in range(0, len(annotations), MAX_PER_REQUEST)]
    first, *rest = batches
    return [
        {
            "name": mirror["name"],
            "head_sha": mirror["head_sha"],
            "status": "completed",
            "conclusion": CONCLUSION,
            "output": {**output, "annotations": first},
        },
        *({"output": {**output, "annotations": batch}} for batch in rest),
    ]
