from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import (
    absence,
    capacity,
    cli,
    driver,
    github,
    local,
    providers,
    report,
    retry,
    selection,
    unavailable,
)
from theseus_reviewer.policy import Policy
from theseus_reviewer.selection import Attempt, Step

from .test_attempts import HEAD, _v
from .test_driver import REPO, _environment, _final, _plan
from .test_report_outcomes import _comment_plan, _defect, _finalize

pytestmark = [pytest.mark.unit, pytest.mark.fast]

BOT = "theseus-review"
OTHER = "d" * 40
QUOTA_PAYLOAD = (
    '429 {"error":{"code":429,"message":"You have exhausted your capacity on this model. Your quota will reset '
    'after 3h12m5s.","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo",'
    '"reason":"RATE_LIMIT_EXCEEDED","metadata":{"uiMessage":"true","model":"gemini-3-pro-high"}}]}}'
)
GATEWAY_PAYLOAD = "gateway answered 503: connect ECONNREFUSED 100.64.1.9:8443 Bearer ya29.a0AfH6SMBx-secret-token"
INCOMPLETE_PAYLOAD = "read 3 of 7 changed hunks; sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 tskey-auth-kabc123"
LEAKS = re.compile(r"sk-|Bearer|tskey-|ya29|RESOURCE_EXHAUSTED|ECONNREFUSED|googleapis|uiMessage")


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "outputs.txt"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return target


def _absent(slot: int, reviewer: str, kind: str, detail: str, provider: str = "") -> Attempt:
    return Attempt(slot, reviewer, selection.ABSENT, absence=kind, detail=detail, provider=provider)


def _payload_attempts() -> list[Attempt]:
    return [
        _absent(1, "small", absence.QUOTA, QUOTA_PAYLOAD, "google-antigravity"),
        _absent(2, "medium", absence.ERROR, GATEWAY_PAYLOAD, "anthropic"),
        _absent(3, "large", absence.INCOMPLETE, INCOMPLETE_PAYLOAD),
    ]


def _pending(policy: Policy, attempts: list[Attempt], reasons: tuple[str, ...] = (), **plan: Any) -> report.Final:
    return _finalize(
        policy,
        {**_comment_plan(policy), "bot_login": BOT, "routed": ["anthropic", "google-antigravity"], **plan},
        None,
        kind=selection.PENDING,
        reviewer=None,
        attempts=attempts,
        reasons=reasons,
    )


def test_every_absence_kind_and_pending_cause_has_a_reason() -> None:
    assert set(unavailable.ABSENCE_CAUSES) | {absence.OK} == set(absence.KINDS)
    handled = {*unavailable.PENDING_CAUSES, retry.ALL_ABSENT, retry.UNAVAILABLE}
    own_note = {retry.MARKER_LIMIT, retry.OMP_INSTALL, retry.UNPUBLISHED}
    assert handled | own_note == set(retry.PENDING_CAUSES)
    keys = [cause.key for cause in unavailable.CAUSES]
    assert len(keys) == len(set(keys))
    for cause in unavailable.CAUSES:
        assert re.fullmatch(r"[a-z0-9-]+", cause.key) and cause.reason and cause.next_step


@pytest.mark.parametrize("kind", sorted(set(absence.KINDS) - {absence.OK}))
def test_an_attempt_of_every_absence_kind_renders_its_mapped_reason(policy: Policy, kind: str) -> None:
    final = _pending(policy, [_absent(1, "small", kind, "RAW provider text sk-abc123", "anthropic")])
    mapped = unavailable.attempt_cause(_absent(1, "small", kind, "", "anthropic"))
    assert f"- small (attempt 1): {mapped.reason} (anthropic)" in final.body.split("\n")
    assert "RAW" not in final.body and "RAW" not in final.description


def test_captured_provider_payloads_never_reach_any_body_or_status(policy: Policy) -> None:
    pending = _pending(policy, _payload_attempts(), served_error=GATEWAY_PAYLOAD)
    mixed = _finalize(
        policy,
        {**_comment_plan(policy), "routed": ["anthropic", "google-antigravity"], "served_error": GATEWAY_PAYLOAD},
        _v("s", [_defect("Problem")]),
        kind=selection.FAIL,
        attempts=[
            Attempt(1, "small", selection.FAIL),
            _absent(2, "max", absence.QUOTA, QUOTA_PAYLOAD, "google-antigravity"),
        ],
    )
    for final in (pending, mixed):
        status = report.status_payload(_comment_plan(policy), final, "https://example.test/run")
        for text in (final.body, final.description, status["description"], json.dumps(final.review)):
            assert not LEAKS.search(text), text
    quota = "the model provider's usage quota is exhausted (google-antigravity)"
    assert f"- max (attempt 2): {quota}" in mixed.body.split("\n")
    assert f"- small (attempt 1): {quota}" in pending.body.split("\n")
    assert f"- medium (attempt 2): {unavailable.PROVIDER_ERROR.reason} (anthropic)" in pending.body.split("\n")
    assert f"- large (attempt 3): {unavailable.INCOMPLETE.reason}" in pending.body.split("\n")


def test_an_unsafe_provider_name_is_left_out_of_the_reason(policy: Policy) -> None:
    final = _pending(policy, [_absent(1, "small", absence.QUOTA, "", "Bearer abc def")])
    assert f"- small (attempt 1): {unavailable.QUOTA.reason}" in final.body.split("\n")


def test_a_gateway_that_cannot_be_reached_is_one_reason_in_every_place_it_appears(policy: Policy) -> None:
    served = f"gateway unreachable: {GATEWAY_PAYLOAD}"
    reason = f"small: {providers.UNAVAILABLE_PREFIX} {served}"
    plan = {**_comment_plan(policy), "bot_login": BOT, "served_error": served}
    step = Step(selection.PENDING, None, (reason,), unavailable=True)
    final = report.finalize(plan, step, [], [], {}, {})
    assert final.pending_cause == retry.UNAVAILABLE
    assert final.description == "pending: no reviewer available (the model gateway could not be reached)"
    assert f"- small: {unavailable.GATEWAY.reason}" in final.body.split("\n")
    assert not LEAKS.search(final.body)
    assert final.body.rstrip().endswith(f"<!-- review-unavailable: v1 {HEAD} gateway-unreachable -->")


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (unavailable.QUOTA, unavailable.SWEEP_RETRIES),
        (unavailable.GATEWAY, unavailable.SWEEP_RETRIES),
        (unavailable.INCOMPLETE, unavailable.SWEEP_RETRIES),
        (unavailable.TOO_LARGE, unavailable.SPLIT),
        (unavailable.ARTIFACTS, unavailable.NEW_HEAD),
        (unavailable.BLOCKED, unavailable.MANUAL_RETRY),
    ],
)
def test_each_cause_has_its_specific_next_step(cause: unavailable.Cause, expected: str) -> None:
    assert unavailable.next_step([cause]) == expected


def test_the_next_step_texts_say_what_the_reader_can_do() -> None:
    assert "hourly sweep retries" in unavailable.SWEEP_RETRIES and "`/review`" in unavailable.SWEEP_RETRIES
    assert "smaller" in unavailable.SPLIT and "stand-in" in unavailable.SPLIT
    assert "new head" in unavailable.NEW_HEAD


def test_a_transient_absence_renders_the_sweep_next_step_and_a_marker_naming_every_cause(policy: Policy) -> None:
    final = _pending(policy, _payload_attempts())
    assert f"Next step: {unavailable.SWEEP_RETRIES}" in final.body.split("\n")
    key = "incomplete-read+provider-error+quota"
    assert final.body.rstrip().endswith(f"<!-- review-unavailable: v1 {HEAD} {key} -->")


def test_an_oversize_change_says_to_split_it_or_ask_for_a_stand_in(policy: Policy) -> None:
    plan = {**_comment_plan(policy), "brief_mode": "oversize", "bot_login": BOT}
    final = _finalize(policy, plan, None, kind=selection.PENDING, reviewer=None)
    assert report.OVERSIZE_NOTE in final.body and "stand-in" in report.OVERSIZE_NOTE
    assert final.body.rstrip().endswith(f"<!-- review-unavailable: v1 {HEAD} too-large -->")
    assert "Next step:" not in final.body


def test_withheld_artifacts_name_one_reason_and_leave_the_errors_to_the_log(policy: Policy) -> None:
    final = _finalize(policy, _comment_plan(policy), _v("fine"), errors=["attempt 1: wrong run sk-secret"])
    assert "sk-secret" not in final.body and "wrong run" not in final.body
    assert f"- {unavailable.ARTIFACTS.reason}" in final.body.split("\n")
    assert f"Next step: {unavailable.NEW_HEAD}" in final.body.split("\n")
    assert final.body.rstrip().endswith(f"<!-- review-unavailable: v1 {HEAD} artifacts-invalid -->")


def test_a_verdict_body_carries_neither_the_marker_nor_a_next_step(policy: Policy) -> None:
    final = _finalize(
        policy,
        _comment_plan(policy),
        _v("s", [_defect("Problem")]),
        kind=selection.FAIL,
        attempts=[Attempt(1, "small", selection.FAIL), _absent(2, "max", absence.QUOTA, QUOTA_PAYLOAD)],
    )
    assert unavailable.MARKER_OPEN not in final.body and "Next step:" not in final.body


def test_the_headline_the_collapsed_section_and_the_local_prefix_are_kept(policy: Policy) -> None:
    final = _pending(policy, _payload_attempts())
    lines = final.body.split("\n")
    assert lines[0] == "### ⏳ NOT REVIEWED YET · reviewers unavailable · shadow (advisory)"
    assert any(line.startswith("<details><summary>What happened") for line in lines)
    assert local.AVAILABILITY_PREFIX == "availability: "
    gone = _pending(policy, [], reasons=(f"small: {providers.UNAVAILABLE_PREFIX} {GATEWAY_PAYLOAD}",))
    assert not LEAKS.search(gone.description)


def _post(login: str, body: str, at: str, **extra: Any) -> dict[str, Any]:
    return {"login": f"{login}[bot]", "user_type": "Bot", "body": body, "at": at, **extra}


def _notice(key: str, head: str = HEAD) -> str:
    return f"### review-verdict: pending for `{head}`\n\nprose\n\n<!-- review-unavailable: v1 {head} {key} -->\n"


def _verdict_post(at: str) -> dict[str, Any]:
    return _post(BOT, f"### review-verdict: success for `{HEAD}`\n\nok", at)


EARLY, LATE = "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z"


@pytest.mark.parametrize(
    ("posts", "body", "expected"),
    [
        ([_post(BOT, _notice("quota"), EARLY)], _notice("quota"), True),
        ([_post(BOT, _notice("quota"), EARLY)], _notice("quota").replace("prose", "other"), True),
        ([_post(BOT, _notice("quota"), EARLY)], _notice("timeout"), False),
        ([], _notice("quota"), False),
        ([_post(BOT, f"### review-verdict: pending for `{HEAD}`\n\nno marker", EARLY)], _notice("quota"), False),
        ([_post(BOT, "<!-- review-unavailable: v1 nothex quota -->", EARLY, commit_id=HEAD)], _notice("quota"), False),
        ([_post(BOT, _notice("quota", OTHER), EARLY, commit_id=HEAD)], _notice("quota"), False),
        ([_post(BOT, _notice("quota"), EARLY), _post(BOT, _notice("timeout"), LATE)], _notice("quota"), False),
        ([_post(BOT, _notice("timeout"), EARLY), _post(BOT, _notice("quota"), LATE)], _notice("quota"), True),
        ([_post(BOT, _notice("quota"), EARLY), _verdict_post(LATE)], _notice("quota"), False),
        ([_post(BOT, _notice("quota"), "not a time")], _notice("quota"), False),
        ([_post("someone", _notice("quota"), EARLY)], _notice("quota"), False),
        ([{**_post(BOT, _notice("quota"), EARLY), "user_type": "User"}], _notice("quota"), False),
        ([_post(BOT, _notice("quota"), EARLY, state="DISMISSED")], _notice("quota"), False),
        ([_post(BOT, _notice("quota", OTHER), EARLY)], _notice("quota"), False),
    ],
)
def test_a_not_reviewed_post_repeats_only_when_the_latest_post_for_the_head_has_the_same_marker(
    posts: list[dict[str, Any]], body: str, expected: bool
) -> None:
    assert unavailable.repeats(posts, HEAD, body, BOT) is expected


def test_a_body_without_a_marker_is_never_a_repeat() -> None:
    assert not unavailable.repeats([_post(BOT, _notice("quota"), EARLY)], HEAD, "plain", BOT)


def test_raw_listings_normalize_into_comparable_posts() -> None:
    comments = [{"user": {"login": f"{BOT}[bot]", "type": "Bot"}, "body": _notice("quota"), "created_at": EARLY}]
    reviews = [
        {
            "login": f"{BOT}[bot]",
            "user_type": "Bot",
            "state": "COMMENTED",
            "body": _notice("timeout"),
            "commit_id": HEAD,
            "submitted_at": LATE,
        }
    ]
    posts = unavailable.posts_of(comments, reviews)
    assert unavailable.previous_key(posts, HEAD, BOT) == (HEAD, "timeout")
    assert unavailable.previous_key(posts[:1], HEAD, BOT) == (HEAD, "quota")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self, repo: str, head: str, payload: Path) -> None:
        self.calls.append("status")

    def comment(self, repo: str, pr: int, payload: Path) -> None:
        self.calls.append("comment")

    def review(self, repo: str, pr: int, payload: Path) -> None:
        self.calls.append("review")

    def dispatch_gate(self, repo: str, pr: int, head: str) -> None:
        self.calls.append("dispatch")

    def gate(self, repo: str, pr: int, head: str) -> None:
        self.calls.append("gate")


def _publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> tuple[Any, Recorder]:
    environment = _environment(tmp_path)
    _plan(environment.layout, bot_login=BOT)
    _final(environment.layout.final)
    (environment.layout.final / f"{kind}.json").write_text(json.dumps({"body": _notice("quota")}), encoding="utf-8")
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    sink = Recorder()
    monkeypatch.setattr(driver.CiEnvironment, "sink", lambda self: sink)
    monkeypatch.setattr(github, "reviews", lambda repo, pr: [])
    return environment, sink


@pytest.mark.parametrize("kind", ["comment", "review"])
@pytest.mark.parametrize(("previous", "posted"), [("quota", False), ("timeout", True), (None, True)])
def test_publish_posts_a_not_reviewed_notice_once_per_head_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, kind: str, previous: str | None, posted: bool
) -> None:
    environment, sink = _publish(tmp_path, monkeypatch, kind)
    earlier = [
        {
            "user": {"login": f"{BOT}[bot]", "type": "Bot"},
            "body": _notice(previous) if previous else "no marker here",
            "created_at": EARLY,
        }
    ]
    monkeypatch.setattr(github, "issue_comments", lambda repo, pr: earlier)

    assert driver.publish(environment, argparse.Namespace()) == 0

    assert (kind in sink.calls) is posted
    assert sink.calls.count("status") == 1 and "gate" in sink.calls
    assert environment.repo == REPO


def test_publish_posts_again_when_the_listing_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    environment, sink = _publish(tmp_path, monkeypatch, "review")

    def unreadable(repo: str, pr: int) -> list[Any]:
        raise github.GitHubError("listing failed")

    monkeypatch.setattr(github, "issue_comments", unreadable)

    assert driver.publish(environment, argparse.Namespace()) == 0
    assert "review" in sink.calls


def test_bookkeeping_reasons_are_dropped_from_the_cause_unless_nothing_else_remains() -> None:
    capacity_unknown = f"small: {capacity.UNKNOWN_PREFIX} x"
    step = Step(selection.PENDING, None, (capacity_unknown, "medium: skipped: no attempt slot left in this run"))
    assert unavailable.causes(step, [], {}) == (unavailable.CAPACITY_UNKNOWN,)
    only = Step(selection.PENDING, None, ("medium: skipped: no attempt slot left in this run",))
    assert unavailable.causes(only, [], {}) == (unavailable.ATTEMPT_LIMIT,)
    assert unavailable.causes(Step(selection.PENDING), [], {}) == (unavailable.GENERIC,)


HOSTILE_PROVIDERS = ["x --> @octocat", "`rm`", "anthropic\n- injected", "a" * 80, "Anthropic"]


def test_a_routed_provider_name_is_printed() -> None:
    plan = {"routed": ["anthropic", "google-antigravity"]}
    item = _absent(1, "small", absence.QUOTA, QUOTA_PAYLOAD, "anthropic")
    step = Step(selection.PENDING)
    assert "(anthropic)" in unavailable.lines([item], step, plan)[0]
    assert "excluded after anthropic failed" in unavailable.excluded_line(item, plan)


@pytest.mark.parametrize("name", [*HOSTILE_PROVIDERS, "openai"])
def test_an_unrouted_provider_name_is_never_printed(name: str) -> None:
    plan = {"routed": ["anthropic"]}
    item = _absent(1, "small", absence.QUOTA, QUOTA_PAYLOAD, name)
    out = unavailable.lines([item], Step(selection.PENDING), plan)[0]
    excluded = unavailable.excluded_line(item, plan)
    assert name not in out
    assert name not in excluded
    assert "excluded after a provider failed" in excluded
    assert "\n" not in out + excluded


def test_no_provider_name_is_printed_when_the_plan_routes_none() -> None:
    item = _absent(1, "small", absence.QUOTA, QUOTA_PAYLOAD, "anthropic")
    assert "anthropic" not in unavailable.lines([item], Step(selection.PENDING), {})[0]
    assert "anthropic" not in unavailable.excluded_line(item, {})
