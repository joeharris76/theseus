from __future__ import annotations

import re
from typing import Any

import pytest

from conftest import CONSUMER_ALPHA
from theseus_reviewer import adjudication, gate, protocol, report, review_check, selection, status_text
from theseus_reviewer.absence import CONTEXT_OVERFLOW, ERROR, INCOMPLETE, QUOTA
from theseus_reviewer.selection import Attempt
from theseus_reviewer.verdict import Finding

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = "42512404919"
RUN_UUID = "f" * 32
LIMIT = status_text.DESCRIPTION_LIMIT
CONFIG = gate.load_gate_config(".github/review.yml", CONSUMER_ALPHA)
QUOTA_PAYLOAD = (
    '429 Cloud Code Assist API error (429): { "error": { "code": 429, "message": "Individual quota reached. '
    'Please try again later.", "status": "RESOURCE_EXHAUSTED", "details": [ { "@type": "type.googleapis.com/'
    'google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED" } ] } }'
)
MANY = [f"D{number}" for number in range(10, 70)]


def _finding(severity: str) -> Finding:
    return Finding(severity, "src/a.py", 1, None, "title", "detail")


def _decisions(state: str) -> list[str]:
    cases: list[str] = []
    for decision in (protocol.SHIP, protocol.SHIP_WITH_FIXES, protocol.DO_NOT_SHIP):
        for escalated in ([], ["D1", "D2"], MANY):
            for tail in ("", status_text.carried_tail(SHA), status_text.ruling_tail("MAINTAIN", "D9999")):
                cases.append(
                    report.decision_description(
                        decision,
                        state,
                        open_entries=[{"severity": "High"}] * 99,
                        open_findings=[_finding("Medium")] * 3,
                        escalated=escalated,
                        fixed=MANY,
                        strikes=2,
                        strike_limit=3,
                        tail=tail,
                    )
                )
    return cases


def _attempt(slot: int, absence: str, detail: str = "") -> Attempt:
    return Attempt(slot, "large", selection.ABSENT, absence, detail)


def _pending_cases() -> list[str]:
    attempts = [_attempt(index, QUOTA, QUOTA_PAYLOAD) for index in range(1, 4)]
    incomplete = [_attempt(index, INCOMPLETE, "x" * 400) for index in range(1, 4)]
    cases = [
        report._pending_description(cause, attempts_, plan, degraded)
        for cause in ("oversize", "stopped", "unreported", "unavailable", "all-absent")
        for attempts_ in (attempts, incomplete, [_attempt(1, ERROR, "y" * 400)], [_attempt(1, CONTEXT_OVERFLOW)], [])
        for plan in ({}, {"served_error": "gateway down " * 20}, {"pool_blocked_until": {"large": {}}})
        for degraded in ([], ["provider-" + str(index) for index in range(40)])
    ]
    return cases + [
        status_text.INTEGRITY,
        status_text.MARKER_LIMIT,
        status_text.OMP_INSTALL,
        status_text.FORK,
        status_text.EDITED,
        status_text.GATEWAY_DOWN,
        status_text.OVERSIZE,
        status_text.not_reviewed_reason(f"the recorded SHIP WITH FIXES decision on head {SHA} " + "does not match " * 30),
        status_text.not_reviewed_reason(status_text.EDITED_REASON),
        status_text.not_reviewed_reason(""),
    ]


def _gate_cases() -> list[str]:
    return [
        status_text.gate_waiting(SHA),
        status_text.gate_open_findings(protocol.COMMENT_DISPUTE_GRAMMAR),
        status_text.gate_escalated(MANY, adjudication.GRAMMAR),
        status_text.gate_escalated(["D1", "D2"], adjudication.GRAMMAR),
        status_text.gate_state("error"),
        status_text.gate_pass_standin("x" * 39),
        status_text.gate_refusals("omp pin bump refused", ["reason " * 80, "second"]),
        status_text.gate_refusals("theseus promotion refused", [f"{SHA} " * 20]),
        status_text.gate_error("cannot load the base review policy: GitHub returned HTTP 404 for https://api.github.com/" + "x" * 300),
        status_text.gate_error(f"error {SHA} " * 40),
        status_text.GATE_NOT_REVIEWED,
        status_text.GATE_DO_NOT_SHIP,
        status_text.GATE_REFUSED,
        status_text.GATE_EXEMPT_STALE,
        status_text.GATE_UNMATCHED_SUCCESS,
        status_text.GATE_PASS_VERDICT,
        status_text.GATE_PASS_EXEMPT,
        status_text.GATE_EVALUATING,
        status_text.sweep_failure(["step " * 100, "two"]),
        status_text.sweep_failure([]),
        status_text.refused(10**6),
    ]


def test_every_decision_description_leaves_room_for_the_repost_mark_and_run_suffix() -> None:
    for description in _decisions("success"):
        assert len(description) <= status_text.SUCCESS_LIMIT
        posted = status_text.with_run_suffix(status_text.repost(description), RUN_UUID)
        assert len(posted) <= LIMIT and posted.endswith(f"{status_text.REPOSTED_MARK} [run {RUN_UUID}]")
    for description in _decisions("failure"):
        assert len(description) <= status_text.REPOST_LIMIT
        assert len(status_text.repost(description)) <= LIMIT


def test_pending_and_gate_descriptions_fit_with_worst_case_inputs() -> None:
    for description in [*_pending_cases(), *_gate_cases()]:
        assert len(description) <= LIMIT, description


def test_no_status_carries_a_full_commit_sha_or_the_review_prefix() -> None:
    for description in [*_decisions("success"), *_decisions("failure"), *_pending_cases(), *_gate_cases()]:
        if status_text.STANDIN_APPROVAL in description:
            continue
        assert not re.search(r"\b[0-9a-f]{40}\b", description), description
        assert not description.startswith(("review:", "review-gate:", "pending:")), description


def test_degraded_providers_are_shortened_by_whole_names_and_never_overflow() -> None:
    providers = [f"provider-{index}" for index in range(40)]
    for base in ("SHIP · no open findings", "x" * 120, "x" * 139):
        for limit in (status_text.SUCCESS_LIMIT, LIMIT):
            shown = status_text.with_degraded(base[:limit], providers, limit)
            assert len(shown) <= limit
            tail = shown.removeprefix(base[:limit]).removeprefix(status_text.DEGRADED_MARK)
            names = re.sub(r" \+\d+ more$", "", tail)
            assert all(name in providers for name in names.split(", ") if name)


def test_escalated_ids_survive_whole_and_the_label_is_never_cut() -> None:
    for tail in ("", status_text.carried_tail(SHA), status_text.ruling_tail("WITHDRAW", "D9999")):
        description = report.decision_description(
            protocol.SHIP_WITH_FIXES, "failure", open_entries=[{"severity": "Low"}] * 99, escalated=MANY, tail=tail
        )
        assert status_text.OPEN_FINDINGS_LABEL in description
        ids = status_text.ESCALATED.search(description).group("ids").split(",")  # type: ignore[union-attr]
        assert ids == MANY[: len(ids)] and len(ids) >= 1
        assert tail in description


def test_the_run_suffix_is_trailing_and_the_reposted_mark_precedes_it() -> None:
    posted = status_text.with_run_suffix(status_text.repost("SHIP · no open findings"), RUN_ID)
    assert posted == f"SHIP · no open findings (reposted) [run {RUN_ID}]"
    assert status_text.run_suffix(posted) == RUN_ID
    assert status_text.strip_run_suffix(posted).endswith(status_text.REPOSTED_MARK)
    assert status_text.run_suffix(status_text.strip_run_suffix(posted)) is None
    legacy = "large: SHIP WITH FIXES, 7 defect(s) to fix; escalated D1,D2 " + "x" * 100
    cut = status_text.repost(legacy)
    assert len(cut) <= LIMIT and cut.endswith(status_text.REPOSTED_MARK)


def test_the_expected_strings_for_the_documented_cases() -> None:
    severities = [("High", 5), ("Medium", 1), ("Low", 1)]
    limit = status_text.REPOST_LIMIT
    assert status_text.shipped([], "", limit) == "SHIP · no open findings"
    assert status_text.shipped(["D11", "D14"], "", limit) == "SHIP · D11, D14 fixed · no open findings"
    assert (
        status_text.with_findings("SHIP WITH FIXES", 7, severities, [], "", limit)
        == "SHIP WITH FIXES · 7 to fix (5 High, 1 Medium, 1 Low) · see review threads"
    )
    assert (
        status_text.with_findings("SHIP WITH FIXES", 2, [], ["D1", "D2"], "", limit)
        == "SHIP WITH FIXES · 2 to fix · escalated D1,D2 · owner ruling needed"
    )
    assert status_text.refused_decision("DO NOT SHIP", 2, 3, "", limit) == "DO NOT SHIP · rework needed · strike 2 of 3"
    assert status_text.refused(3) == "refused after 3 DO NOT SHIP decisions · open a new pull request"
    assert status_text.shipped([], status_text.carried_tail(SHA), limit) == "SHIP · carried from 0123456789ab (same patch)"
    assert status_text.shipped([], status_text.ruling_tail("WITHDRAW", "D2"), limit) == "SHIP · owner ruling: WITHDRAW D2"
    assert status_text.unavailable("provider quota") == (
        "Not reviewed yet · reviewers unavailable (provider quota) · retries hourly · /review retries now"
    )
    assert status_text.incomplete_read(3) == (
        "Not reviewed yet · reviewer did not read every changed hunk (3 attempts) · retries hourly"
    )
    assert status_text.EDITED == "a review was edited · verdict withheld · push a new head"
    assert status_text.gate_pass_standin("joeharris76") == "Pass · stand-in review by joeharris76"
    assert status_text.GATE_PASS_VERDICT == "Pass · review-verdict: SHIP"
    assert status_text.GATE_PASS_EXEMPT == "Pass · only exempt paths changed"
    assert status_text.gate_waiting(SHA) == (
        f"Waiting for review-verdict · owner bypass: 'Stand-in review: APPROVE {SHA}'"
    )
    assert status_text.gate_error(
        "cannot load the base review policy: GitHub returned HTTP 404 for https://api.github.com/repos/o/r"
    ) == "Gate error · cannot load the base review policy (HTTP 404) · see the run log"


def test_a_429_payload_never_reaches_a_status_description() -> None:
    for kind in (QUOTA, INCOMPLETE, ERROR):
        attempts = [_attempt(1, kind, QUOTA_PAYLOAD)]
        description = report._pending_description("all-absent", attempts, {}, [])
        for leaked in ("RESOURCE_EXHAUSTED", "Individual quota", "{", "Cloud Code", "429", "googleapis"):
            assert leaked not in description
    assert report._pending_description("all-absent", [_attempt(1, QUOTA, QUOTA_PAYLOAD)], {}, []) == (
        status_text.unavailable("provider quota")
    )
    assert report._detail(selection.Step(selection.PENDING, None, (f"large: absent (quota: {QUOTA_PAYLOAD})",)), "all-absent", "") != ""


def test_the_standin_grammar_in_the_waiting_status_round_trips_through_its_parser() -> None:
    quoted = re.search(r"'([^']+)'", status_text.gate_waiting(HEAD)).group(1)  # type: ignore[union-attr]
    assert quoted == f"{status_text.STANDIN_APPROVAL} {HEAD}"
    assert review_check._attested_shas({"body": quoted}) == [HEAD]


def test_the_adjudication_grammar_in_the_escalated_status_round_trips_through_its_parser() -> None:
    quoted = re.search(r"'([^']+)'", status_text.gate_escalated(["D1", "D2"], adjudication.GRAMMAR)).group(1)  # type: ignore[union-attr]
    assert quoted == adjudication.GRAMMAR
    filled = quoted.replace("WITHDRAW|MAINTAIN", "WITHDRAW").replace("<n>", "1").replace("<reason>", "counter-evidence")
    assert adjudication.parse(filled) == adjudication.Adjudication("WITHDRAW", "D1", "counter-evidence")


def test_the_dispute_grammar_in_the_open_findings_status_round_trips_through_its_parser() -> None:
    quoted = re.search(r"'([^']+)'", status_text.gate_open_findings(protocol.COMMENT_DISPUTE_GRAMMAR)).group(1)  # type: ignore[union-attr]
    assert quoted == protocol.COMMENT_DISPUTE_GRAMMAR
    filled = quoted.replace("<n>", "1").replace("<reason>", "a.py:3 shows the guard")
    assert protocol.parse_comment_dispute(filled) == ("D1", "a.py:3 shows the guard")
    assert protocol.is_dispute(filled)


def _readers(description: str, state: str) -> tuple[Any, ...]:
    status = {"state": state, "description": description}
    return (
        review_check._status_run_uuid(status),
        review_check._is_repost(status),
        review_check._open_findings([status]),
        (match.group("ids") if (match := status_text.ESCALATED.search(description)) else None),
        review_check._edit_hold(status),
        status_text.strip_run_suffix(description) == status_text.EXEMPT_SUCCESS_DESCRIPTION,
        description.startswith(status_text.REFUSED_DESCRIPTION_PREFIX),
        description.startswith(status_text.SWEEP_FAILURE_SURFACE_PREFIX),
    )


CLASSIFICATION_PAIRS = [
    ("large: SHIP [run 42512404919]", "SHIP · no open findings [run 42512404919]", "success"),
    ("large: SHIP (reposted) [run 42512404919]", "SHIP · no open findings (reposted) [run 42512404919]", "success"),
    ("large: SHIP WITH FIXES, 7 defect(s) to fix", "SHIP WITH FIXES · 7 to fix (5 High, 1 Medium, 1 Low) · see review threads", "failure"),
    ("large: SHIP WITH FIXES, 7 defect(s) to fix", "SHIP WITH FIXES · 7 to fix (5 High, 1 Medium, 1 Low) · see review threads", "success"),
    (
        "large: SHIP WITH FIXES, 2 defect(s) to fix; escalated D1,D2",
        "SHIP WITH FIXES · 2 to fix · escalated D1,D2 · owner ruling needed",
        "failure",
    ),
    (
        "large: SHIP WITH FIXES, 2 defect(s) to fix (reposted)",
        "SHIP WITH FIXES · 2 to fix (reposted)",
        "failure",
    ),
    ("large: DO NOT SHIP", "DO NOT SHIP · rework needed · strike 2 of 3", "failure"),
    (
        "refused after 3 DO NOT SHIP decisions: open a new pull request",
        "refused after 3 DO NOT SHIP decisions · open a new pull request",
        "failure",
    ),
    ("carried from 8f192e3e1e59: SHIP [run 42512404919]", "SHIP · carried from 8f192e3e1e59 (same patch) [run 42512404919]", "success"),
    ("carried from 8f192e3e1e59: SHIP WITH FIXES", "SHIP WITH FIXES · 2 to fix · carried from 8f192e3e1e59 (same patch)", "failure"),
    ("owner adjudication: WITHDRAW D2; SHIP [run 42512404919]", "SHIP · owner ruling: WITHDRAW D2 [run 42512404919]", "success"),
    (
        "owner adjudication: MAINTAIN D2; SHIP WITH FIXES, escalated D3",
        "SHIP WITH FIXES · 1 to fix · escalated D3 · owner ruling: MAINTAIN D2",
        "failure",
    ),
    ("only exempt paths changed", "only exempt paths changed", "success"),
    ("only exempt paths changed [run 42512404919]", "only exempt paths changed [run 42512404919]", "success"),
    ("a review was edited", "a review was edited · verdict withheld · push a new head", "pending"),
    ("fork: owner review", "Not reviewed · fork PR · needs an owner stand-in", "pending"),
    ("result withheld: artifacts failed validation", "Not reviewed · run artifacts failed validation · push a new head", "pending"),
    (
        f"pending: large: absent (quota: {QUOTA_PAYLOAD})"[:140],
        "Not reviewed yet · reviewers unavailable (provider quota) · retries hourly · /review retries now",
        "pending",
    ),
    ("pending: no reviewer available (gateway unreachable)", "Not reviewed yet · model gateway unreachable · retries hourly · /review retries now", "pending"),
    (
        "pending: too large for the review; split the pull request or ask the owner for a stand-in",
        "Not reviewed · too large to review · split the PR or ask an owner for a stand-in",
        "pending",
    ),
    (
        f"{status_text.SWEEP_FAILURE_SURFACE_PREFIX}Run the attempt loop. Rerun with /review or push a fix",
        status_text.sweep_failure(["Run the attempt loop"]),
        "pending",
    ),
]


@pytest.mark.parametrize(("old", "new", "state"), CLASSIFICATION_PAIRS)
def test_readers_classify_the_new_description_as_they_classified_the_old_one(old: str, new: str, state: str) -> None:
    assert _readers(new, state) == _readers(old, state)


def _statuses(description: str, state: str = "failure") -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "context": "review-verdict",
            "state": state,
            "description": description,
            "login": CONFIG.binding.bot_login,
            "user_type": "Bot",
            "created_at": "2026-10-01T13:00:00Z",
        }
    ]


def _decide(statuses: list[dict[str, Any]], **kwargs: Any) -> tuple[int, str]:
    classifier = CONFIG.policy.classifier
    return review_check.decide(
        HEAD,
        ["benchbox/core/equivalence/checker.py"],
        statuses,
        review_check.path_matcher(1, 1, CONFIG.matchers, classifier.exempt_paths, classifier.gate_paths),
        None,
        kwargs.pop("comments", []),
        binding=CONFIG.binding,
        reviews=[],
    )


@pytest.mark.parametrize(
    "description",
    [
        "large: SHIP WITH FIXES, 2 defect(s) to fix",
        "SHIP WITH FIXES · 2 to fix (1 High, 1 Low) · see review threads",
    ],
)
def test_open_findings_block_and_a_standin_cannot_waive_them_in_either_wording(description: str) -> None:
    standin = {
        "login": next(iter(CONFIG.binding.attesters)),
        "user_type": "User",
        "body": f"{status_text.STANDIN_APPROVAL} {HEAD}",
        "created_at": "2026-10-01T14:00:00Z",
        "updated_at": None,
    }
    status, message = _decide(_statuses(description), comments=[standin])
    assert status == review_check.WAITING and message == status_text.gate_open_findings(protocol.COMMENT_DISPUTE_GRAMMAR)


@pytest.mark.parametrize(
    "description",
    [
        "large: SHIP WITH FIXES, 2 defect(s) to fix; escalated D1,D2",
        "SHIP WITH FIXES · 2 to fix · escalated D1,D2 · owner ruling needed",
    ],
)
def test_an_escalation_reaches_the_owner_ruling_message_in_either_wording(description: str) -> None:
    status, message = _decide(_statuses(description))
    assert (status, message) == (review_check.WAITING, status_text.gate_escalated(["D1", "D2"], adjudication.GRAMMAR))


@pytest.mark.parametrize(
    ("description", "state", "expected"),
    [
        ("refused after 3 DO NOT SHIP decisions: open a new pull request", "failure", status_text.GATE_REFUSED),
        ("refused after 3 DO NOT SHIP decisions · open a new pull request", "failure", status_text.GATE_REFUSED),
        ("large: DO NOT SHIP", "failure", status_text.GATE_DO_NOT_SHIP),
        ("DO NOT SHIP · rework needed · strike 1 of 3", "failure", status_text.GATE_DO_NOT_SHIP),
        ("a review was edited", "pending", status_text.GATE_NOT_REVIEWED),
        (status_text.unavailable("provider quota"), "pending", status_text.GATE_NOT_REVIEWED),
    ],
)
def test_the_gate_reads_old_and_new_statuses_to_the_same_decision(description: str, state: str, expected: str) -> None:
    assert _decide(_statuses(description, state)) == (review_check.WAITING, expected)


@pytest.mark.parametrize(
    "description",
    ["something nobody can classify", "SHIP · no open findings", "SHIP · no open findings [run 42512404919]", ""],
)
def test_an_unclassifiable_or_unbacked_success_still_fails_closed(description: str) -> None:
    status, message = _decide(_statuses(description, "success"))
    assert status == review_check.WAITING
    assert message == status_text.GATE_UNMATCHED_SUCCESS


def test_the_exempt_status_with_a_stale_rule_set_waits_with_its_own_message() -> None:
    status, message = _decide(_statuses(status_text.EXEMPT_SUCCESS_DESCRIPTION, "success"))
    assert (status, message) == (review_check.WAITING, status_text.GATE_EXEMPT_STALE)


def test_no_decision_waits_with_the_prefix_the_consumers_match() -> None:
    status, message = _decide([])
    assert status == review_check.WAITING
    assert message.startswith(status_text.GATE_WAITING_PREFIX)
    assert f"{status_text.STANDIN_APPROVAL} {HEAD}" in message


def test_the_token_literals_live_only_in_the_shared_module() -> None:
    from pathlib import Path

    root = Path(report.__file__).parent
    literals = (
        "[run ",
        " (reposted)",
        "SHIP WITH FIXES",
        "escalated ",
        "only exempt paths changed",
        "refused after",
        "a review was edited",
        "review-verdict failed twice",
        "Stand-in review: APPROVE",
        "degraded: ",
    )
    for path in sorted(root.glob("*.py")):
        if path.name in ("status_text.py", "_vendor"):
            continue
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            assert literal not in text, f"{path.name} repeats {literal!r}"

