from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import CONSUMER_ALPHA
from theseus_reviewer import attempts as attempt_files
from theseus_reviewer import capacity, cli, comment_disputes, gate, protocol, report, review_check
from theseus_reviewer.diff import commentable_lines
from theseus_reviewer.policy import Policy
from theseus_reviewer.retry import State

from .test_attempts import CHECKER, HEAD, RUN_ID, _classification, _v, _write
from .test_cli_plan import (
    CAPTURE,
    TWO_FILE_DIFF,
    TWO_FILES,
    FakeGitHub,
    _plan,
    _pull,
    _record,
    _review,
)
from .test_dispute_escalation import _CommentFake
from .test_rounds import _final, _prior, _protocol_plan, _with_priors

pytestmark = [pytest.mark.unit, pytest.mark.fast, pytest.mark.usefixtures("consumer_cwd")]

ATTESTER = "joeharris76"
FIRST_AT = "2026-10-08T10:00:00Z"


def _comment(body: str, at: str = "2026-10-08T11:00:00Z", **over: Any) -> dict[str, Any]:
    return {
        "id": 70,
        "user": {"login": ATTESTER, "type": "User"},
        "created_at": at,
        "updated_at": at,
        "body": body,
        **over,
    }


# ---- the dispute comment grammar -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Dispute: D1 - pkg/kept.py:12 keeps the row",
        "dispute: D12 - lower-case prefix is accepted",
        "DISPUTE: D3 - upper-case prefix is accepted",
        "  Dispute: D1 - leading whitespace is trimmed\n",
        "Dispute: D1 - first line\nsecond line of the reason",
    ],
)
def test_a_dispute_comment_in_the_grammar_is_parsed(body: str) -> None:
    parsed = protocol.parse_comment_dispute(body)
    assert parsed is not None and parsed[0] == body.strip().split(" ")[1]


@pytest.mark.parametrize(
    "body",
    [
        "Dispute: D1",
        "Dispute: D1 -",
        "Dispute: D1 - ",
        "Dispute:D1 - no space",
        "Dispute: d1 - lower-case id",
        "Dispute: D0 - zero",
        "Dispute: D01 - leading zero",
        "Dispute: D12345 - too many digits",
        "Dispute: #D1 - hash",
        "Dispute: c1-D1 - cycle form",
        "Dispute: D1: colon",
        "Dispute: D1 and D2 - two",
        "Dispute: D1 -  \n",
        "I dispute: D1 - not the prefix",
        "> Dispute: D1 - quoted",
        "",
        None,
    ],
)
def test_anything_else_is_not_a_dispute_comment(body: str | None) -> None:
    assert protocol.parse_comment_dispute(body) is None


def test_the_trigger_prefix_is_case_insensitive_but_the_grammar_is_exact() -> None:
    assert protocol.is_comment_dispute_trigger("DISPUTE: whatever")
    assert not protocol.is_comment_dispute_trigger("Disputed: D1 - x")
    assert protocol.parse_comment_dispute("Dispute: D1 whatever") is None
    assert protocol.COMMENT_DISPUTE_GRAMMAR == "Dispute: D<n> - <reason>"


# ---- who may dispute, and what -----------------------------------------------------------------


def _history(*records: dict[str, Any], times: tuple[str, ...] = (FIRST_AT, "2026-10-08T12:00:00Z")) -> Any:
    held = protocol.History(list(records))
    held.submitted = [datetime.fromisoformat(item.replace("Z", "+00:00")) for item in times[: len(records)]]
    return held


def _open_record(**over: Any) -> dict[str, Any]:
    defect = {"id": "D1", "severity": "High", "file": CAPTURE, "line": 99, "end_line": None, "title": "Drops a row"}
    record = _record(TWO_FILE_DIFF, "SHIP_WITH_FIXES", head_sha=HEAD, open_defects=[defect], next_id=2)
    return {**record, **over}


def _collect(comments: list[dict[str, Any]], *records: dict[str, Any]) -> list[dict[str, Any]]:
    return comment_disputes.collect(comments, _history(*(records or (_open_record(),))), frozenset({ATTESTER}))


def test_an_attester_comment_disputes_an_open_finding_like_a_thread_reply() -> None:
    found = _collect([_comment("Dispute: D1 - pkg/kept.py:12 keeps the row")])
    assert len(found) == 1
    entry = found[0]
    assert (entry["id"], entry["cycle"], entry["path"], entry["title"]) == ("D1", 1, CAPTURE, "Drops a row")
    assert entry["replies"] == [{"author": ATTESTER, "body": "Dispute: D1 - pkg/kept.py:12 keeps the row"}]
    assert entry["claim_hashes"] == [protocol.claim_hash(protocol.canonical_claim(entry["replies"][0]["body"]))]
    assert protocol.live_disputes(found, _open_record())


@pytest.mark.parametrize(
    "comment",
    [
        _comment("Dispute: D1 - x", user={"login": "stranger", "type": "User"}),
        _comment("Dispute: D1 - x", user={"login": ATTESTER, "type": "Bot"}),
        _comment("Dispute: D1 - x", updated_at="2026-10-08T11:30:00Z"),
        _comment("Dispute: D1 - x", at="2026-10-08T09:00:00Z"),
        _comment("Dispute: D1 - x", at="2026-10-08T10:00:00Z"),
        _comment("Dispute: D2 - the finding is not open"),
        _comment("Dispute: D1"),
        _comment("Please look again at D1"),
        _comment("Dispute: D1 - x", created_at="not a time"),
    ],
    ids=[
        "not-an-attester",
        "a-bot",
        "edited",
        "before-the-review",
        "same-moment-as-the-review",
        "unknown-finding",
        "no-reason",
        "prose",
        "bad-time",
    ],
)
def test_a_comment_that_is_not_a_valid_unedited_attester_dispute_of_an_open_finding_counts_for_nothing(
    comment: dict[str, Any],
) -> None:
    assert _collect([comment]) == []


def test_a_comment_cannot_dispute_a_finding_before_the_review_that_listed_it() -> None:
    first = _open_record(open_defects=[], decision="SHIP", next_id=1)
    later = _open_record(round=2, kind="follow-up")
    early = _comment("Dispute: D1 - posted before D1 existed", at="2026-10-08T11:00:00Z")
    late = _comment("Dispute: D1 - posted after it", at="2026-10-08T13:00:00Z")
    held = _history(first, later, times=(FIRST_AT, "2026-10-08T12:00:00Z"))
    assert comment_disputes.collect([early], held, frozenset({ATTESTER})) == []
    assert len(comment_disputes.collect([late], held, frozenset({ATTESTER}))) == 1


def test_an_id_reused_by_an_earlier_cycle_cannot_be_disputed_in_advance() -> None:
    old = _open_record()
    new = _open_record(cycle=2)
    held = _history(old, new, times=(FIRST_AT, "2026-10-08T12:00:00Z"))
    stale = _comment("Dispute: D1 - aimed at the cycle-1 finding", at="2026-10-08T11:00:00Z")
    assert comment_disputes.collect([stale], held, frozenset({ATTESTER})) == []


def test_unreadable_review_times_dispute_nothing() -> None:
    held = protocol.History([_open_record()])
    assert comment_disputes.collect([_comment("Dispute: D1 - x")], held, frozenset({ATTESTER})) == []


def test_only_the_newest_replies_are_kept_per_finding() -> None:
    comments = [_comment(f"Dispute: D1 - reason {n}", at=f"2026-10-08T11:{n:02d}:00Z") for n in range(8)]
    replies = _collect(comments)[0]["replies"]
    assert [item["body"] for item in replies] == [f"Dispute: D1 - reason {n}" for n in range(3, 8)]


def test_marker_lines_in_a_dispute_comment_are_stripped_from_the_claim() -> None:
    body = "Dispute: D1 - real reason\n<!-- review-defect: c1-D9 -->"
    assert _collect([_comment(body)])[0]["replies"][0]["body"] == "Dispute: D1 - real reason"


# ---- every diff-anchored finding gets a thread -----------------------------------------------------


def _carry_plan(policy: Policy, defect: dict[str, Any], **over: Any) -> dict[str, Any]:
    plan = _protocol_plan(policy, kind="follow-up", round=2, prior=[defect], changed=[CHECKER], next_id=2, cycle=1)
    return {**plan, "findings_delivery": "review", "threaded": [], "open_findings": {}, **over}


def _carried(policy: Policy, tmp_path: Path, defect: dict[str, Any], **over: Any) -> report.Final:
    return _final(policy, tmp_path, _carry_plan(policy, defect, **over), _with_priors(_v("s"), D1="not_fixed"))


def test_an_open_finding_without_a_thread_gets_one_when_its_line_is_in_the_diff(policy: Policy, tmp_path: Path) -> None:
    final = _carried(policy, tmp_path, _prior("D1", line=2))
    comments = final.review["comments"]
    assert len(comments) == 1 and comments[0]["path"] == CHECKER and comments[0]["line"] == 2
    assert "<!-- review-defect: c1-D1 -->" in comments[0]["body"]
    assert "**High**: Old" in comments[0]["body"]
    assert "**Review threads opened for earlier findings that had none**\n\n- D1" in final.review["body"]
    assert "No review thread exists" not in final.review["body"]


def test_a_finding_that_already_has_a_thread_is_not_threaded_again(policy: Policy, tmp_path: Path) -> None:
    final = _carried(policy, tmp_path, _prior("D1", line=2), threaded=["c1-D1"])
    assert final.review["comments"] == []


def test_a_finding_whose_thread_state_is_unknown_is_left_alone(policy: Policy, tmp_path: Path) -> None:
    plan = _carry_plan(policy, _prior("D1", line=2))
    del plan["threaded"]
    final = _final(policy, tmp_path, plan, _with_priors(_v("s"), D1="not_fixed"))
    assert final.review["comments"] == []


def test_a_finding_matching_an_open_unmarked_thread_is_not_duplicated(policy: Policy, tmp_path: Path) -> None:
    from theseus_reviewer import dedup

    known = {dedup.fingerprint(CHECKER, "Old"): "High"}
    assert _carried(policy, tmp_path, _prior("D1", line=2), open_findings=known).review["comments"] == []


def test_a_finding_outside_the_diff_gets_no_thread_and_the_body_names_the_dispute_comment(
    policy: Policy, tmp_path: Path
) -> None:
    final = _carried(policy, tmp_path, _prior("D1", line=99))
    assert final.review["comments"] == []
    assert (
        "No review thread exists for D1. An attester can dispute one with a pull request comment:"
        in (final.review["body"])
    )
    assert "`Dispute: D<n> - <reason>`" in final.review["body"]


def test_comment_delivery_threads_nothing_but_still_names_the_dispute_comment(policy: Policy, tmp_path: Path) -> None:
    final = _carried(policy, tmp_path, _prior("D1", line=2), findings_delivery="comment")
    assert final.review is None
    assert "Dispute: D<n> - <reason>" in final.body


def test_a_fresh_defect_outside_the_diff_is_also_disputable_by_comment(policy: Policy, tmp_path: Path) -> None:
    plan = {**_protocol_plan(policy), "threaded": []}
    from .test_attempts import _finding

    final = _final(policy, tmp_path, plan, _v("s", [{**_finding("High", 400)}]))
    assert final.review["comments"] == []
    assert "No review thread exists for D1" in final.review["body"]


def test_the_plan_reads_which_findings_already_have_an_open_thread() -> None:
    marker = "\n<!-- review-defect: c1-D1 -->"
    bot = {"author": "benchbox-review[bot]", "author_type": "Bot", "resolved": False, "edited": False}
    threads = [
        {**bot, "body": "**High**: a" + marker},
        {**bot, "body": "**High**: b\n<!-- review-defect: c2-D4 -->"},
        {**bot, "body": "**High**: resolved\n<!-- review-defect: c1-D2 -->", "resolved": True},
        {**bot, "body": "**High**: edited\n<!-- review-defect: c1-D3 -->", "edited": True},
        {**bot, "body": "**High**: not ours\n<!-- review-defect: c1-D5 -->", "author": "someone[bot]"},
        {**bot, "body": "**High**: by a user\n<!-- review-defect: c1-D6 -->", "author_type": "User"},
        {**bot, "body": "**High**: unmarked"},
    ]
    assert cli._threaded_findings(threads, "benchbox-review") == ["c1-D1", "c2-D4"]


# ---- planning: a comment dispute forces the same re-judgement a thread reply does ----------------------


def _fake(*comments: dict[str, Any], reviews: list[dict[str, Any]] | None = None, threads: Any = ()) -> FakeGitHub:
    fake = FakeGitHub(_pull(), TWO_FILES, threads=threads, diff=TWO_FILE_DIFF)
    fake.reviews = reviews if reviews is not None else [_review(_open_record())]
    fake.comments = list(comments)
    return fake


REVIEW_EVENT = {"action": "created", "issue": {"number": 7, "pull_request": {}}, "comment": {"body": "/review"}}


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: FakeGitHub, event: dict[str, Any] = REVIEW_EVENT):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _, values, plan = _plan(monkeypatch, tmp_path, fake, event_name="issue_comment", event=event)
    return values, plan


def test_a_comment_dispute_plans_a_forced_re_judgement_of_a_threadless_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values, plan = _run(monkeypatch, tmp_path, _fake(_comment("Dispute: D1 - pkg/kept.py:12 keeps the row")))
    assert values["decision"] == "review" and plan["protocol"]["forced"] is True
    assert [item["id"] for item in plan["protocol"]["prior"]] == ["D1"]
    assert [(item["id"], item["replies"][0]["author"]) for item in plan["disputes"]] == [("D1", ATTESTER)]
    assert plan["threaded"] == []
    assert plan["protocol"]["maintained"] == {}
    assert protocol.disputed_claims(plan).keys() == {"D1"}


@pytest.mark.parametrize(
    "comment",
    [
        _comment("Dispute: D1 - x", user={"login": "stranger", "type": "User"}),
        _comment("Dispute: D1 - x", updated_at="2026-10-08T11:30:00Z"),
        _comment("Dispute: D1"),
        _comment("Dispute: D1 - x", at="2026-10-08T09:00:00Z"),
        _comment("/review please"),
    ],
    ids=["stranger", "edited", "malformed", "stale", "not-a-dispute"],
)
def test_an_invalid_dispute_comment_plans_no_reviewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, comment: dict[str, Any]
) -> None:
    values, plan = _run(monkeypatch, tmp_path, _fake(comment))
    assert values["decision"] in ("carry", "repost") and plan["chain"] == []
    assert plan["protocol"]["forced"] is False


def test_a_thread_dispute_and_a_comment_dispute_for_one_finding_are_both_shown_to_the_reviewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from .test_cli_plan import _disputed_thread

    thread = {**_disputed_thread(), "path": CAPTURE}
    values, plan = _run(monkeypatch, tmp_path, _fake(_comment("Dispute: D1 - from the comment"), threads=[thread]))
    assert values["decision"] == "review"
    assert len(protocol.disputed_claims(plan)["D1"]) == 2


def test_the_sweep_redispatches_a_head_with_an_unanswered_comment_dispute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from .test_cli_plan import _sweep

    state = State(7, HEAD, "failure", datetime.now(UTC) - timedelta(hours=3))
    moment = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    fake = FakeGitHub(_pull(), TWO_FILES, state, threads=[], diff=TWO_FILE_DIFF)
    fake.comments = [_comment("Dispute: D1 - x", at=moment)]
    assert _sweep(monkeypatch, tmp_path, fake) == 0
    assert fake.dispatched == [7]


def test_the_sweep_ignores_a_comment_that_is_not_an_attester_dispute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from .test_cli_plan import _sweep

    state = State(7, HEAD, "failure", datetime.now(UTC) - timedelta(hours=3))
    moment = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    fake = FakeGitHub(_pull(), TWO_FILES, state, threads=[], diff=TWO_FILE_DIFF)
    fake.comments = [
        _comment("Dispute: D1 - x", at=moment, user={"login": "stranger", "type": "User"}),
        _comment("Dispute: D1", at=moment),
    ]
    assert _sweep(monkeypatch, tmp_path, fake) == 0
    assert fake.dispatched == []


# ---- the full flow ----------------------------------------------------------------------------------------


GATE = gate.load_gate_config(".github/review.yml", CONSUMER_ALPHA)
BOT = "benchbox-review[bot]"


def _finalize(plan: dict[str, Any], directory: Path, verdict: dict[str, Any], run_id: str) -> report.Final:
    bound = {**{k: v for k, v in plan.items() if k != capacity.LOAD_KEY}, "run_id": run_id}
    _write(directory, 1, "medium", verdict=verdict)
    loaded = attempt_files.load(directory, {**bound, "run_id": RUN_ID}, RUN_ID)
    step, errors = attempt_files.decide(cli._selection(bound, _classification("medium")), loaded)
    assert errors == [], errors
    return report.finalize(bound, step, errors, loaded.attempts, loaded.verdicts, commentable_lines(TWO_FILE_DIFF))


def _maintain(plan: dict[str, Any]) -> dict[str, Any]:
    claim = plan["disputes"][0]["replies"][-1]["body"]
    entry = {
        "id": "D1",
        "status": "not_fixed",
        "evidence": "the row is still dropped at registry.py:99",
        "claim": claim,
        "concedes": False,
    }
    return {**_v("still open"), "prior_defects": [entry]}


def _status(final: report.Final, plan: dict[str, Any], at: str, number: int) -> dict[str, Any]:
    payload = report.status_payload(plan, final, "https://example.test/run")
    return {
        "id": number,
        "context": payload["context"],
        "state": payload["state"],
        "description": payload["description"],
        "login": BOT,
        "user_type": "Bot",
        "created_at": at,
    }


def _gate(statuses: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> tuple[int, str]:
    return review_check.decide(
        HEAD,
        [CHECKER, CAPTURE],
        statuses,
        review_check.path_matcher(
            2, 2, GATE.matchers, GATE.policy.classifier.exempt_paths, GATE.policy.classifier.gate_paths
        ),
        None,
        [],
        binding=GATE.binding,
        reviews=receipts,
    )


def _receipt(final: report.Final, at: str, number: int) -> dict[str, Any]:
    return {
        "id": number,
        "login": BOT,
        "user_type": "Bot",
        "commit_id": HEAD,
        "state": "COMMENTED",
        "submitted_at": at,
        "body": final.review["body"],
        "last_edited_at": None,
        "includes_created_edit": False,
    }


def _published(final: report.Final, number: int, at: str) -> dict[str, Any]:
    record = protocol.decode_marker(final.review["body"])
    assert record is not None
    return _review(record, id=number, submitted_at=at)


def test_threadless_finding_two_keyed_comment_disputes_escalation_owner_adjudication_then_the_gate_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 0. The earlier review listed D1 on a line outside the diff, so it has no thread and nothing to reply to.
    first = _review(_open_record(), id=1, submitted_at=FIRST_AT)
    statuses: list[dict[str, Any]] = []

    # 1. First keyed dispute: an attester comments, the next run re-judges D1 on the unchanged patch.
    dispute_one = _comment(
        "Dispute: D1 - registry.py:99 is guarded by the caller at kept.py:12", "2026-10-08T11:00:00Z"
    )
    values, plan_one = _run(monkeypatch, tmp_path / "one", _fake(dispute_one, reviews=[first]))
    assert values["decision"] == "review" and plan_one["protocol"]["forced"] and plan_one["protocol"]["round"] == 2
    final_one = _finalize(plan_one, tmp_path / "a1", _maintain(plan_one), "5101")
    record_one = protocol.decode_marker(final_one.review["body"])
    assert final_one.state == "failure" and record_one["disputed"] == ["D1"] and "escalated" not in record_one
    assert "Dispute dispositions" in final_one.review["body"] and "D1 (maintained)" in final_one.review["body"]
    review_one = _published(final_one, 2, "2026-10-08T12:00:00Z")
    statuses.append(_status(final_one, plan_one, "2026-10-08T12:00:30Z", 1))
    status, message = _gate(statuses, [_receipt(final_one, "2026-10-08T12:00:00Z", 2)])
    assert status == review_check.WAITING and "lists open findings" in message

    # 2. Second keyed dispute, posted after that review: the same finding stays open, so it escalates.
    dispute_two = _comment("Dispute: D1 - the same guard also covers the retry path", "2026-10-08T13:00:00Z", id=71)
    values, plan_two = _run(monkeypatch, tmp_path / "two", _fake(dispute_one, dispute_two, reviews=[first, review_one]))
    assert values["decision"] == "review" and plan_two["protocol"]["round"] == 3
    assert plan_two["protocol"]["maintained"] == {"D1": 1}
    final_two = _finalize(plan_two, tmp_path / "a2", _maintain(plan_two), "5102")
    record_two = protocol.decode_marker(final_two.review["body"])
    assert record_two["escalated"] == ["D1"] and final_two.description.endswith("; escalated D1")
    assert "Review adjudication: WITHDRAW|MAINTAIN D<n> - <reason>" in final_two.review["body"]
    review_two = _published(final_two, 3, "2026-10-08T14:00:00Z")
    statuses.append(_status(final_two, plan_two, "2026-10-08T14:00:30Z", 2))
    status, message = _gate(statuses, [_receipt(final_two, "2026-10-08T14:00:00Z", 3)])
    assert status == review_check.WAITING and "need the owner's ruling" in message and "D1" in message

    # A third dispute round on this patch is refused: only the owner can settle it now.
    values, plan_three = _run(
        monkeypatch,
        tmp_path / "three",
        _fake(dispute_one, dispute_two, reviews=[first, review_one, review_two]),
    )
    assert values["decision"] == "skip"
    assert "already had 2 dispute re-reviews; D1 escalated to the owner" in plan_three["decision_reason"]

    # 3. The owner's instruction and ruling clear the finding; the gate passes.
    ruling = _comment(
        "Review adjudication: WITHDRAW D1 - registry.py:99 is unreachable, see kept.py:12",
        "2026-10-08T15:00:00Z",
        id=55,
    )
    adjudicating = _CommentFake(_pull(), TWO_FILES, None, threads=[], diff=TWO_FILE_DIFF)
    adjudicating.reviews = [first, review_one, review_two]
    adjudicating.comments = [dispute_one, dispute_two]
    adjudicating.comment = ruling
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": {"id": 55, "body": ruling["body"]},
    }
    values, plan_four = _run(monkeypatch, tmp_path / "four", adjudicating, event)
    assert values["decision"] == "carry" and plan_four["adjudication"]["id"] == "D1"
    final_four = report.adjudicated({**plan_four, "findings_delivery": "review", "run_id": "5104"})
    record_four = json.loads(json.dumps(protocol.decode_marker(final_four.review["body"])))
    assert final_four.state == "success" and record_four["decision"] == "SHIP"
    assert record_four["open_defects"] == [] and record_four["adjudicated"] == [{"id": "D1", "action": "WITHDRAW"}]
    statuses.append(_status(final_four, {**plan_four, "run_id": "5104"}, "2026-10-08T15:00:30Z", 3))
    assert _gate(statuses, [_receipt(final_four, "2026-10-08T15:00:00Z", 4)]) == (
        review_check.PASS,
        f"review: pass (review verdict for {HEAD})",
    )
