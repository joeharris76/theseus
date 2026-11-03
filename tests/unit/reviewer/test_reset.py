from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import ALPHA_POLICY, BETA_POLICY

from theseus_reviewer import cli, github, reset

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ATTESTERS = frozenset({"joeharris76"})
REPO = "BenchBox-dev/BenchBox"
HEAD = "a" * 40
OTHER = "b" * 40
START = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
OWNER = {"login": "joeharris76", "type": "User"}
BETA_OWNER = {"login": "beta-owner", "type": "User"}


def _comment(body: str, minutes: int, *, user: dict[str, str] = OWNER, edited: bool = False) -> dict[str, Any]:
    created = (START + timedelta(minutes=minutes)).isoformat()
    updated = (START + timedelta(minutes=minutes + 1)).isoformat() if edited else created
    return {"user": user, "body": body, "created_at": created, "updated_at": updated}


def _instruction(head: str = HEAD, minutes: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _comment(f"Owner instruction: reset the backoff for {head}", minutes, **kwargs)


def _reset(head: str = HEAD, minutes: int = 5, reason: str = "the gateway was down", **kwargs: Any) -> dict[str, Any]:
    return _comment(reset.reset_body(head, reason), minutes, **kwargs)


def test_a_reset_after_an_owner_instruction_is_accepted() -> None:
    assert reset.reset_moment([_instruction(), _reset()], HEAD, ATTESTERS) == START + timedelta(minutes=5)


def test_the_latest_valid_reset_wins() -> None:
    comments = [_instruction(), _reset(minutes=5), _reset(minutes=9)]
    assert reset.reset_moment(comments, HEAD, ATTESTERS) == START + timedelta(minutes=9)


@pytest.mark.parametrize(
    "comments",
    [
        [_reset()],
        [_instruction(OTHER), _reset()],
        [_instruction(minutes=6), _reset()],
        [_instruction(), _reset(OTHER)],
        [_instruction(), _reset(edited=True)],
        [_instruction(edited=True), _reset()],
        [_instruction(), _reset(user={"login": "someone", "type": "User"})],
        [_instruction(), _reset(user={"login": "joeharris76", "type": "Bot"})],
        [_instruction(user={"login": "someone", "type": "User"}), _reset()],
        [_instruction(), _comment(f"Oracle backoff reset: {HEAD}", 5)],
        [_instruction(), _comment(f"Oracle backoff reset: {HEAD}\nReason: why\nmore", 5)],
        [_instruction(), _comment(f"> Oracle backoff reset: {HEAD}\nReason: why", 5)],
    ],
)
def test_anything_short_of_an_owner_reset_with_a_prior_instruction_is_ignored(comments: list[dict[str, Any]]) -> None:
    assert reset.reset_moment(comments, HEAD, ATTESTERS) is None


@pytest.mark.parametrize(
    ("reason", "valid"),
    [("the gateway was down", True), ("", False), (" padded ", False), ("two\nlines", False), ("x" * 201, False)],
)
def test_a_reason_is_one_trimmed_line(reason: str, valid: bool) -> None:
    assert reset.valid_reason(reason) is valid


def test_the_attesters_are_a_parameter_not_a_default() -> None:
    comments = [_instruction(user=BETA_OWNER), _reset(user=BETA_OWNER)]
    assert reset.reset_moment(comments, HEAD, ATTESTERS) is None
    assert reset.reset_moment(comments, HEAD, frozenset({"beta-owner"})) == START + timedelta(minutes=5)
    assert reset.has_owner_instruction(comments, HEAD, frozenset({"beta-owner"}))
    assert not reset.has_owner_instruction(comments, HEAD, ATTESTERS)
    assert not reset.has_owner_instruction(comments, HEAD, frozenset())


class Console:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, comments: list[dict[str, Any]], *, login: str = "joeharris76"):
        self.posted: list[tuple[int, str]] = []
        self.pull = {"state": "open", "head": {"sha": HEAD}}
        monkeypatch.setattr(github, "current_login", lambda: login)
        monkeypatch.setattr(github, "get_json", lambda path: self.pull)
        monkeypatch.setattr(github, "get_paginated", lambda path: comments)
        monkeypatch.setattr(github, "post_comment", lambda repo, pr, body: self.posted.append((pr, body)))

    def run(self, *, head: str = HEAD, reason: str = "the gateway was down", policy: Path = ALPHA_POLICY) -> int:
        argv = ["reset-backoff", "--pr", "7", "--head", head, "--reason", reason, "--repo", REPO]
        return cli.main([*argv, "--policy", str(policy)])


def test_the_owner_resets_a_backoff_with_the_reason_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(monkeypatch, [_instruction()])
    assert console.run() == 0
    assert console.posted == [(7, f"Oracle backoff reset: {HEAD}\nReason: the gateway was down")]
    posted = _comment(console.posted[0][1], 5)
    assert reset.reset_moment([_instruction(), posted], HEAD, ATTESTERS) == START + timedelta(minutes=5)


def test_a_reset_without_an_owner_instruction_naming_the_head_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(monkeypatch, [_instruction(OTHER)])
    with pytest.raises(SystemExit, match="Owner instruction"):
        console.run()
    assert console.posted == []


def test_an_account_that_is_not_an_owner_cannot_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(monkeypatch, [_instruction()], login="someone")
    with pytest.raises(SystemExit, match="not an owner"):
        console.run()
    assert console.posted == []


@pytest.mark.parametrize("state", ["closed", "head-moved"])
def test_a_reset_for_a_closed_pull_request_or_a_moved_head_is_refused(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    console = Console(monkeypatch, [_instruction()])
    console.pull = (
        {"state": "closed", "head": {"sha": HEAD}} if state == "closed" else {"state": "open", "head": {"sha": OTHER}}
    )
    with pytest.raises(SystemExit, match="is not open at head"):
        console.run()
    assert console.posted == []


@pytest.mark.parametrize(("head", "reason"), [("abc", "why"), (HEAD, ""), (HEAD, "a\nb")])
def test_a_malformed_head_or_reason_is_refused(monkeypatch: pytest.MonkeyPatch, head: str, reason: str) -> None:
    console = Console(monkeypatch, [_instruction()])
    with pytest.raises(SystemExit):
        console.run(head=head, reason=reason)
    assert console.posted == []


def test_the_consumer_policy_names_who_may_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(monkeypatch, [_instruction(user=BETA_OWNER)], login="beta-deputy")
    assert console.run(policy=BETA_POLICY) == 0
    assert console.posted == [(7, f"Oracle backoff reset: {HEAD}\nReason: the gateway was down")]


def test_an_alpha_attester_cannot_reset_under_the_beta_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(monkeypatch, [_instruction()])
    with pytest.raises(SystemExit, match="not an owner"):
        console.run(policy=BETA_POLICY)
    assert console.posted == []
