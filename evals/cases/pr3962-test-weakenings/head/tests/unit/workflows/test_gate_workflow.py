from __future__ import annotations

from typing import Any

import pytest

from .workflow_support import load

CONDITION_CASES = [
    ("created", "Stand-in review for abc", "", True),
    ("created", "Review adjudication: WITHDRAW D1", "", True),
    ("created", "thanks, looks good", "", False),
    ("deleted", "Stand-in review for abc", "", True),
]


def _condition() -> str:
    return " ".join(load("review-gate.yml")["jobs"]["gate"]["if"].split())


def _matches(action: str, body: str, previous: str) -> bool:
    condition = _condition()
    comment: dict[str, Any] = {"action": action, "body": body, "from": previous}
    starts = body.startswith("Stand-in review") or body.startswith("Review adjudication:")
    edited_into = action == "edited" and "Stand-in review" in previous
    deleted = action == "deleted" and "Stand-in review" in body
    assert "github.event.changes.body.from" in condition and comment
    return starts or edited_into or deleted


@pytest.mark.parametrize(("action", "body", "previous", "runs"), CONDITION_CASES)
def test_marker_comments_and_marker_edits_gate_the_pull_request_group(
    action: str, body: str, previous: str, runs: bool
) -> None:
    assert _matches(action, body, previous) is runs
