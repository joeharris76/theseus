from __future__ import annotations

from pathlib import Path

import pytest

from theseus_reviewer import legacy

from .test_cli_plan import _acked_thread, _plan
from .test_same_head_rerun import _decided, _fake

pytestmark = [pytest.mark.unit, pytest.mark.fast, pytest.mark.usefixtures("consumer_cwd")]


def _event(body: str) -> dict:
    return {"action": "created", "issue": {"number": 7, "pull_request": {}}, "comment": {"body": body}}


def _decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> str:
    fake = _fake(_decided(), [_acked_thread()])
    _, values, _ = _plan(monkeypatch, tmp_path, fake, event_name="issue_comment", event=_event(body))
    return values["decision"]


@pytest.mark.parametrize(
    "body",
    [
        "/cc please re-review",
        "/reviewers",
        "/review-me",
        "/Reviewers",
        "/Oracle-Reviewer",
        "please /review this",
        "> /review",
        "Owner instruction: the earlier comment said\nReview adjudication: WITHDRAW D1 - nope",
        "> Review adjudication: WITHDRAW D1 - quoted",
        f"> {legacy.ADJUDICATION_PREFIX}WITHDRAW D1 - quoted",
        f"see {legacy.OWNER_COMMAND}",
        "",
    ],
)
def test_a_comment_that_is_not_a_review_command_creates_no_manual_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    assert _decision(monkeypatch, tmp_path, body) == "skip"


@pytest.mark.parametrize(
    "body",
    [
        "/review",
        "  /review\n",
        "/review again please",
        legacy.OWNER_COMMAND,
        "/Review",
        "/REVIEW",
        "/Oracle-Review",
        "/ORACLE-REVIEW now",
    ],
)
def test_the_exact_rerun_commands_still_start_a_manual_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    assert _decision(monkeypatch, tmp_path, body) == "repost"
