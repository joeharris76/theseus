from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import attempts as attempt_files
from theseus_reviewer import cli, providers, report, retry
from theseus_reviewer._vendor import yaml
from theseus_reviewer.absence import AUTH, ERROR, Absence, classify
from theseus_reviewer.classify import SIZE_BUCKETS, fallback_classification
from theseus_reviewer.jev import JevError
from theseus_reviewer.policy import PolicyError, RetryRules, parse_policy
from theseus_reviewer.selection import ABSENT, PENDING, REVIEW, Attempt, SelectionInput, next_step

from .conftest import POLICY_PATH
from .test_selection import NOW, _reviewer

pytestmark = [pytest.mark.unit, pytest.mark.fast]

OPUS = ["anthropic/claude-opus-5-5:medium", "openai-codex/gpt-6.1-sol:high", "google-antigravity/gemini-4-argon:medium"]
SONNET = ["anthropic/claude-sonnet-5-5:high", "openai-codex/gpt-6.1-sol:medium"]
WINDOWS = {
    "anthropic/claude-opus-5-5": 872_000,
    "anthropic/claude-sonnet-5-5": 872_000,
    "openai-codex/gpt-6.1-sol": 372_000,
    "google-antigravity/gemini-4-argon": 983_040,
}
CHAIN = [_reviewer("large", model="@review"), _reviewer("huge", model="@plan")]
CANDIDATES = {"large": OPUS, "huge": OPUS}


def _served(*models: str) -> providers.Served:
    return providers.Served(frozenset(models))


def _input(**overrides: Any) -> SelectionInput:
    base = SelectionInput(
        chain=CHAIN,
        brief_mode="inline",
        max_attempts=5,
        now=NOW,
        load_tokens=100_000,
        capacities={"large": 872_000, "huge": 872_000},
        candidates=CANDIDATES,
        served=_served(*(providers.strip_effort(item) for item in OPUS)),
        windows=WINDOWS,
        pool_providers={"pool": "anthropic"},
    )
    return SelectionInput(**{**base.__dict__, **overrides})


def test_a_fully_served_chain_runs_the_first_reviewer_on_its_primary() -> None:
    step = next_step(_input(), [])
    assert step.kind == REVIEW and step.reviewer is not None and step.reviewer.name == "large"
    assert step.resolution is not None and step.resolution.target == OPUS[0] and not step.resolution.substituted


def test_the_reviewer_runs_its_first_served_substitute_when_the_primary_is_not_served() -> None:
    served = _served("openai-codex/gpt-6.1-sol", "google-antigravity/gemini-4-argon")
    step = next_step(_input(served=served), [])
    assert step.reviewer is not None and step.reviewer.name == "large"
    assert step.resolution is not None and step.resolution.target == OPUS[1] and step.resolution.substituted


def test_the_capacity_of_the_substitute_not_the_primary_decides() -> None:
    served = _served("openai-codex/gpt-6.1-sol", "google-antigravity/gemini-4-argon")
    step = next_step(_input(served=served, load_tokens=500_000), [])
    assert step.resolution is not None and step.resolution.target == OPUS[2]


def test_a_load_no_served_candidate_holds_is_capacity_degraded_unavailable_and_not_oversize() -> None:
    served = _served("openai-codex/gpt-6.1-sol")
    step = next_step(_input(served=served, load_tokens=500_000), [])
    assert step.kind == PENDING and step.unavailable and not step.oversize
    assert all("capacity-degraded" in reason for reason in step.reasons)


def test_a_load_above_every_nominal_window_stays_oversize() -> None:
    served = _served("openai-codex/gpt-6.1-sol")
    step = next_step(_input(served=served, load_tokens=900_000), [])
    assert step.kind == PENDING and step.oversize and not step.unavailable


def test_nothing_served_is_pending_unavailable_naming_each_reviewer() -> None:
    step = next_step(_input(served=_served()), [])
    assert step.kind == PENDING and step.unavailable
    assert [reason.split(":")[0] for reason in step.reasons] == ["large", "huge"]


def test_an_unknown_served_set_is_pending_unavailable_with_the_probe_error() -> None:
    step = next_step(_input(served=None, served_error="gateway unreachable"), [])
    assert step.kind == PENDING and step.unavailable
    assert all("gateway unreachable" in reason for reason in step.reasons)


def test_an_attempt_that_ran_makes_a_later_pending_all_absent_not_unavailable() -> None:
    first = Attempt(1, "large", ABSENT, "timeout", target=OPUS[0], nominal=OPUS[0])
    step = next_step(_input(served=_served()), [first])
    assert step.kind == PENDING and not step.unavailable


def test_a_request_time_provider_failure_excludes_the_provider_and_does_not_spend_the_retry() -> None:
    failed = Attempt(1, "large", ABSENT, ERROR, "503", provider="anthropic", target=OPUS[0], nominal=OPUS[0])
    step = next_step(_input(), [failed])
    assert step.reviewer is not None and step.reviewer.name == "large"
    assert step.resolution is not None and step.resolution.target == OPUS[1]
    second = Attempt(2, "large", ABSENT, ERROR, "503", provider="openai-codex", target=OPUS[1], nominal=OPUS[0])
    third = next_step(_input(), [failed, second])
    assert third.reviewer is not None and third.resolution is not None and third.resolution.target == OPUS[2]


@pytest.mark.parametrize("kind", [AUTH, ERROR])
def test_an_attributed_failure_on_a_provider_the_attempt_did_not_choose_keeps_the_usual_limit(kind: str) -> None:
    walked = Attempt(1, "large", ABSENT, kind, "x", provider="google-antigravity", target=OPUS[0], nominal=OPUS[0])
    step = next_step(_input(), [walked])
    assert step.reviewer is not None and step.resolution is not None and step.resolution.target == OPUS[0]
    twice = Attempt(2, "large", ABSENT, kind, "x", provider="google-antigravity", target=OPUS[0], nominal=OPUS[0])
    assert next_step(_input(), [walked, twice]).reviewer.name == "huge"


def test_a_failure_without_an_attributed_provider_is_not_free() -> None:
    plain = Attempt(1, "large", ABSENT, AUTH, "401")
    assert next_step(_input(), [plain]).reviewer.name == "huge"


def test_every_candidate_excluded_ends_the_reviewer_without_a_further_attempt() -> None:
    history = [
        Attempt(1, "large", ABSENT, ERROR, "x", provider="anthropic", target=OPUS[0], nominal=OPUS[0]),
        Attempt(2, "large", ABSENT, ERROR, "x", provider="openai-codex", target=OPUS[1], nominal=OPUS[0]),
        Attempt(3, "large", ABSENT, ERROR, "x", provider="google-antigravity", target=OPUS[2], nominal=OPUS[0]),
    ]
    step = next_step(_input(), history)
    assert step.kind == PENDING and not step.unavailable


def test_a_replayed_attempt_that_ran_another_model_than_the_policy_selects_is_refused() -> None:
    served = _served("openai-codex/gpt-6.1-sol")
    selection_input = _input(served=served)
    forged = Attempt(1, "large", ABSENT, "timeout", target=OPUS[0], nominal=OPUS[0])
    assert any("ran anthropic/claude-opus-5-5" in item for item in attempt_files.replay(selection_input, [forged]))
    honest = Attempt(1, "large", ABSENT, "timeout", target=OPUS[1], nominal=OPUS[0])
    assert attempt_files.replay(selection_input, [honest]) == []


def test_the_pool_block_is_primary_only_for_the_provider_the_pool_leads_with() -> None:
    from theseus_reviewer.selection import pool_resets

    chain = [_reviewer("a", pool="pool")]
    quota = Attempt(1, "a", ABSENT, "quota", reset_at=NOW + timedelta(minutes=20), provider="anthropic")
    held = pool_resets(_input(chain=chain), [quota])
    assert held["pool"].primary
    other = pool_resets(_input(chain=chain, pool_providers={"pool": "openai-codex"}), [quota])
    assert not other["pool"].primary


def _terminal(status: int, provider: str = "anthropic") -> str:
    message = {
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": "boom",
        "errorStatus": status,
        "provider": provider,
    }
    return json.dumps({"type": "message_end", "message": message})


@pytest.mark.parametrize(("status", "kind"), [(401, AUTH), (404, ERROR), (500, ERROR), (503, ERROR), (529, ERROR)])
def test_the_provider_comes_from_the_structured_terminal_error_for_auth_not_found_and_server_failures(
    status: int, kind: str
) -> None:
    missing = classify("omp", 1, _terminal(status), "", timed_out=False, now=NOW)
    assert (missing.kind, missing.provider) == (kind, "anthropic")


@pytest.mark.parametrize("status", [400, 422, 413])
def test_other_client_errors_do_not_name_a_provider_to_exclude(status: int) -> None:
    assert classify("omp", 1, _terminal(status), "", timed_out=False, now=NOW).provider == ""


def test_a_provider_named_only_in_model_text_is_never_attributed() -> None:
    text = json.dumps(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "anthropic 503"}]},
        }
    )
    assert classify("omp", 1, text, "anthropic 503 Service Unavailable", timed_out=False, now=NOW) == Absence(
        ERROR, "exit code 1"
    )


RULES = RetryRules(daily_budget=6, backoff_start_minutes=60, sweep_max_dispatches=5)
HEAD = "a" * 40


def _unavailable_state(retries: int = 0, at: datetime = NOW) -> retry.State:
    return retry.State(
        7, HEAD, retry.PENDING, at, tuple(at - timedelta(hours=i + 1) for i in range(retries)), {}, retry.UNAVAILABLE
    )


def test_an_unavailable_result_spends_no_retry_even_when_the_run_was_a_manual_retry() -> None:
    state = retry.next_state(
        previous=None,
        pr=7,
        head_sha=HEAD,
        outcome=retry.PENDING,
        manual=True,
        now=NOW,
        pool_blocked_until={},
        pending_cause=retry.UNAVAILABLE,
    )
    assert state.retries == () and state.pending_cause == retry.UNAVAILABLE
    absent = retry.next_state(
        previous=None,
        pr=7,
        head_sha=HEAD,
        outcome=retry.PENDING,
        manual=True,
        now=NOW,
        pool_blocked_until={},
        pending_cause=retry.ALL_ABSENT,
    )
    assert absent.retries == (NOW,)


@pytest.mark.parametrize("spent", [0, 1, 4])
def test_an_unavailable_head_is_retried_after_a_flat_backoff_however_many_retries_were_spent(spent: int) -> None:
    state = _unavailable_state(retries=spent)
    early = NOW + timedelta(minutes=59)
    late = NOW + timedelta(minutes=61)
    assert not retry.due_for_retry(state, HEAD, early, RULES)
    assert retry.due_for_retry(state, HEAD, late, RULES)


def test_an_unavailable_pending_state_is_swept_while_other_pending_causes_are_not() -> None:
    state = _unavailable_state()
    assert retry.due_for_retry(state, HEAD, NOW + timedelta(hours=1), RULES)
    stopped = retry.State(7, HEAD, retry.PENDING, NOW, (), {}, retry.STOPPED)
    assert not retry.due_for_retry(stopped, HEAD, NOW + timedelta(hours=9), RULES)


def _minimal_plan(bucket: str | None) -> dict[str, Any]:
    return {
        "repo": "owner/repo",
        "pr": 7,
        "head_sha": HEAD,
        "base_sha": "b" * 40,
        "run_id": "1",
        "chain": [item.to_json() for item in CHAIN],
        "buckets": {name: "large" for name in SIZE_BUCKETS},
        "brief_mode": "inline",
        "max_attempts": 5,
        "reviewed_files": [{"path": "a.py", "additions": 1, "deletions": 0, "status": "modified"}],
        "gate_path_list": ["gate/*"],
        "classifier_unavailable_bucket": bucket,
    }


def _select(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plan: dict[str, Any], error: Exception) -> dict[str, Any]:
    def broken(_: Any) -> Any:
        raise error

    monkeypatch.setattr(cli, "_classify_plan", broken)
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    args = argparse.Namespace(
        plan=str(tmp_path / "plan.json"),
        attempts_dir=str(tmp_path / "attempts"),
        slot=1,
        classification=str(tmp_path / "classification.json"),
        workspace=None,
        classify_only=True,
    )
    assert cli.command_select(args) == 0
    path = tmp_path / "classification.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@pytest.mark.parametrize("bucket", ["trivial", "medium", "max"])
def test_a_classifier_outage_falls_back_to_the_policy_bucket_with_the_deep_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bucket: str
) -> None:
    held = _select(monkeypatch, tmp_path, _minimal_plan(bucket), JevError(503, "down"))
    assert held["size_bucket"] == bucket and held["template"] == "deep" and held["capability"] == "deep"
    assert held["source"] == "fallback" and held["files"] == []


@pytest.mark.parametrize(("bucket", "expected"), [("small", "large"), ("max", "max")])
def test_the_fallback_never_goes_below_the_gate_floor_and_uses_the_gate_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bucket: str, expected: str
) -> None:
    plan = _minimal_plan(bucket)
    plan["reviewed_files"] = [{"path": "gate/x.py", "additions": 1, "deletions": 0, "status": "modified"}]
    held = _select(monkeypatch, tmp_path, plan, JevError(None, "unreachable"))
    assert held["size_bucket"] == expected and held["template"] == "gate" and held["gate_flags"] == ["gate/x.py"]


def test_a_missing_classifier_key_is_configuration_and_still_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held = _select(monkeypatch, tmp_path, _minimal_plan("max"), JevError(None, "no key", missing_key=True))
    assert held == {}


def test_a_policy_without_the_key_keeps_the_pending_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _select(monkeypatch, tmp_path, _minimal_plan(None), JevError(503, "down")) == {}


def test_a_malformed_classifier_answer_is_not_a_loss_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _select(monkeypatch, tmp_path, _minimal_plan("max"), KeyError("size")) == {}


def test_the_fallback_classification_round_trips_with_its_source() -> None:
    held = fallback_classification("large", [])
    from theseus_reviewer.classify import Classification

    assert Classification.from_json(held.to_json()).fallback
    assert not Classification.from_json({**held.to_json(), "source": "classifier"}).fallback


def _policy(**classifier: Any) -> Any:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    data["classifier"] = {**data["classifier"], **classifier}
    return parse_policy(data)


def test_the_policy_key_is_optional_and_accepts_every_size_bucket() -> None:
    assert _policy().classifier.unavailable_bucket is None
    assert [_policy(unavailable_bucket=bucket).classifier.unavailable_bucket for bucket in SIZE_BUCKETS] == list(
        SIZE_BUCKETS
    )


@pytest.mark.parametrize("value", ["gigantic", 3, ""])
def test_the_policy_key_refuses_anything_but_a_size_bucket(value: Any) -> None:
    with pytest.raises(PolicyError, match="unavailable_bucket"):
        _policy(unavailable_bucket=value)


def _final(description: str, degraded: str) -> report.Final:
    return report.Final("success", description + degraded, "", None, "large", degraded=degraded)


def test_the_run_suffix_never_cuts_the_degraded_suffix_of_a_success_status() -> None:
    degraded = " \u00b7 degraded: google-antigravity"
    base = "large: " + "x" * 200
    final = _final(report._clip(base, report.DESCRIPTION_LIMIT - len(degraded)), degraded)
    payload = report.status_payload({"status_context": "review-verdict", "run_uuid": "r" * 36}, final, "https://x")
    assert len(payload["description"]) <= report.DESCRIPTION_LIMIT
    assert degraded in payload["description"] and payload["description"].endswith(f"[run {'r' * 36}]")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_from_the_classifier_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    assert _select(monkeypatch, tmp_path, _minimal_plan("max"), JevError(status, "rejected")) == {}


@pytest.mark.parametrize("status", [None, 429, 500, 503])
def test_an_outage_status_from_the_classifier_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int | None
) -> None:
    held = _select(monkeypatch, tmp_path, _minimal_plan("large"), JevError(status, "down"))
    assert held["size_bucket"] == "large" and held["source"] == "fallback"


class _AnswersSizeThenFails:
    def __init__(self, size: str) -> None:
        self.size = size

    def __enter__(self) -> _AnswersSizeThenFails:
        return self

    def __exit__(self, *ignored: object) -> None:
        return None

    def ask(self, state: object, questions: dict[str, Any]) -> dict[str, Any]:
        if "size" not in questions:
            raise JevError(503, "down")
        return {
            "size": {"choice": self.size, "probabilities": {self.size: 1.0}, "confidence": 1.0},
            "capability": {"choice": "routine", "probabilities": {"routine": 1.0}, "confidence": 1.0},
            "template": {"choice": "standard", "probabilities": {"standard": 1.0}, "confidence": 1.0},
        }


@pytest.mark.parametrize(
    ("answered", "bucket", "expected"),
    [("huge", "large", "huge"), ("max", "medium", "max"), ("small", "large", "large")],
)
def test_a_per_file_failure_after_the_size_answer_keeps_the_larger_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answered: str, bucket: str, expected: str
) -> None:
    monkeypatch.setattr(cli, "JevSession", lambda: _AnswersSizeThenFails(answered))
    plan = _minimal_plan(bucket)
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    args = argparse.Namespace(
        plan=str(tmp_path / "plan.json"),
        attempts_dir=str(tmp_path / "attempts"),
        slot=1,
        classification=str(tmp_path / "classification.json"),
        workspace=None,
        classify_only=True,
    )
    assert cli.command_select(args) == 0
    held = json.loads((tmp_path / "classification.json").read_text(encoding="utf-8"))
    assert held["size_bucket"] == expected and held["source"] == "fallback"


def _next(previous: retry.State | None, now: datetime, cause: str, head: str = HEAD, **extra: Any) -> retry.State:
    return retry.next_state(
        previous=previous,
        pr=7,
        head_sha=head,
        outcome=retry.PENDING,
        manual=True,
        now=now,
        pool_blocked_until={},
        pending_cause=cause,
        **extra,
    )


def test_unavailable_runs_are_recorded_for_a_dispatch_and_kept_for_a_later_state_on_the_same_head() -> None:
    first = _next(None, NOW, retry.UNAVAILABLE, dispatched=True)
    later = NOW + timedelta(hours=2)
    second = _next(first, later, retry.ALL_ABSENT)
    assert first.unavailable_runs == (NOW,) and second.unavailable_runs == (NOW,) and second.retries == (later,)
    assert retry.State.from_json(second.to_json()).unavailable_runs == (NOW,)
    assert retry.State.from_json({**second.to_json(), "unavailable_runs": []}).unavailable_runs == ()
    assert retry.recent_unavailable(second, NOW + timedelta(hours=25)) == ()


def test_a_review_comment_during_an_outage_adds_no_entry_because_it_is_not_a_dispatch() -> None:
    assert _next(None, NOW, retry.UNAVAILABLE).unavailable_runs == ()
    assert _next(None, NOW, retry.UNAVAILABLE, dispatched=False).unavailable_runs == ()


@pytest.mark.parametrize("outcome", [retry.SUCCESS, retry.FAILURE, retry.PENDING])
def test_entries_of_another_head_are_never_inherited(outcome: str) -> None:
    other = retry.State(7, "c" * 40, outcome, NOW, (), {}, None, 0, None, (NOW,))
    assert _next(other, NOW + timedelta(hours=1), retry.ALL_ABSENT).unavailable_runs == ()
    assert _next(other, NOW + timedelta(hours=1), retry.UNAVAILABLE, dispatched=True).unavailable_runs == (
        NOW + timedelta(hours=1),
    )


def test_more_recorded_entries_than_dispatches_never_make_the_spent_count_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = _dispatches(2, HEAD, NOW)
    state = retry.State(
        7,
        HEAD,
        retry.PENDING,
        NOW,
        (),
        {},
        retry.ALL_ABSENT,
        0,
        None,
        tuple(NOW - timedelta(hours=i) for i in range(9)),
    )
    assert _bounded(monkeypatch, state, runs) == "its retry is due"


def _dispatches(count: int, head: str, now: datetime) -> tuple[Any, ...]:
    from theseus_reviewer import sweep

    return tuple(
        sweep.Run(
            id=index,
            pr=7,
            event=sweep.DISPATCH_EVENT,
            status=sweep.COMPLETED,
            conclusion="success",
            created_at=now - timedelta(hours=index + 1),
            head=head,
        )
        for index in range(count)
    )


def _bounded(monkeypatch: pytest.MonkeyPatch, state: retry.State, runs: tuple[Any, ...]) -> str | None:
    monkeypatch.setattr(cli, "_already_surfaced", lambda *args: False)
    return cli._bounded_retry(
        "o/r", 7, HEAD, runs, state.updated_at, RULES, NOW, "review-verdict", "bot", "t", None, state
    )


def test_an_outage_longer_than_the_daily_dispatch_cap_keeps_being_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = _dispatches(9, HEAD, NOW)
    state = retry.State(
        7, HEAD, retry.PENDING, NOW, (), {}, retry.UNAVAILABLE, 0, None, tuple(r.created_at for r in runs)
    )
    assert _bounded(monkeypatch, state, runs) == "its retry is due"


def test_the_dispatches_of_an_outage_do_not_use_up_the_all_absent_budget_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = _dispatches(7, HEAD, NOW)
    recorded = tuple(r.created_at for r in runs)
    recovered = retry.State(7, HEAD, retry.PENDING, NOW, (), {}, retry.ALL_ABSENT, 0, None, recorded)
    assert _bounded(monkeypatch, recovered, runs) == "its retry is due"
    unrecorded = retry.State(7, HEAD, retry.PENDING, NOW, (), {}, retry.ALL_ABSENT)
    assert _bounded(monkeypatch, unrecorded, runs) is None


SMOL = [
    "openai-codex/gpt-6-luna:xhigh",
    "anthropic/claude-sonnet-5-5:medium",
    "google-antigravity/gemini-3.8-flash:medium",
]
WINDOWS.update({"openai-codex/gpt-6-luna": 144_000, "google-antigravity/gemini-3.8-flash": 936_000})
ALL_SERVED = tuple(providers.strip_effort(item) for item in (*OPUS, *SMOL))


@pytest.mark.parametrize(
    "served",
    [
        ALL_SERVED,
        tuple(item for item in ALL_SERVED if not item.startswith("anthropic/")),
        tuple(item for item in ALL_SERVED if item.startswith("google-antigravity/")),
        tuple(item for item in ALL_SERVED if item.startswith("openai-codex/")),
        (),
    ],
)
@pytest.mark.parametrize("load", [900_000, 950_000])
def test_a_load_above_every_nominal_window_is_oversize_whatever_is_served(served: tuple[str, ...], load: int) -> None:
    step = next_step(_input(served=_served(*served), load_tokens=load), [])
    assert step.kind == PENDING and step.oversize and not step.unavailable and step.reviewer is None


def test_a_950k_load_with_the_primary_unserved_never_runs_on_the_larger_substitute() -> None:
    served = _served("openai-codex/gpt-6.1-sol", "google-antigravity/gemini-4-argon")
    step = next_step(_input(served=served, load_tokens=950_000), [])
    assert step.kind == PENDING and step.oversize and step.resolution is None


def test_a_load_above_the_primary_window_escalates_a_bucket_instead_of_substituting() -> None:
    chain = [_reviewer("small", model="@smol"), _reviewer("medium", model="@default")]
    held = _input(
        chain=chain,
        candidates={"small": SMOL, "medium": SONNET},
        capacities={"small": 144_000, "medium": 872_000},
        load_tokens=200_000,
        served=_served(*(providers.strip_effort(item) for item in (*SONNET, *SMOL[1:]))),
    )
    step = next_step(held, [])
    assert step.reviewer is not None and step.reviewer.name == "medium"
    assert step.resolution is not None and step.resolution.target == SONNET[0] and not step.resolution.substituted
    alone = next_step(replace(held, chain=chain[:1]), [])
    assert alone.kind == PENDING and alone.oversize


def test_an_unknown_primary_capacity_fails_closed_even_when_a_substitute_would_fit() -> None:
    served = _served("openai-codex/gpt-6.1-sol")
    step = next_step(_input(served=served, capacities={}, capacity_error="catalog unreadable"), [])
    assert step.kind == PENDING and not step.unavailable
    assert all("capacity cannot be determined" in reason for reason in step.reasons)


def test_a_substitute_with_a_smaller_window_than_the_load_is_skipped_for_the_next_one() -> None:
    served = _served("openai-codex/gpt-6.1-sol", "google-antigravity/gemini-4-argon")
    step = next_step(_input(served=served, load_tokens=400_000), [])
    assert step.resolution is not None and step.resolution.target == OPUS[2]


def test_a_replayed_attempt_on_a_substitute_for_an_oversize_load_is_refused() -> None:
    served = _served("openai-codex/gpt-6.1-sol", "google-antigravity/gemini-4-argon")
    forged = Attempt(1, "large", ABSENT, "timeout", target=OPUS[2], nominal=OPUS[0])
    errors = attempt_files.replay(_input(served=served, load_tokens=950_000), [forged])
    assert errors and "policy selects pending" in errors[0]


def _small_then_medium(**overrides: Any) -> SelectionInput:
    chain = [_reviewer("small", model="@smol"), _reviewer("medium", model="@default")]
    return _input(
        chain=chain,
        candidates={"small": SMOL, "medium": SONNET},
        capacities={"small": 144_000, "medium": 872_000},
        load_tokens=200_000,
        **overrides,
    )


def test_an_outage_with_a_too_small_slot_and_an_unavailable_higher_tier_is_unavailable() -> None:
    step = next_step(_small_then_medium(served=None, served_error="gateway unreachable"), [])
    assert step.kind == PENDING and step.unavailable and not step.oversize
    assert any("small: skipped: the change needs about 200000" in reason for reason in step.reasons)
    assert any("medium: unavailable: gateway unreachable" in reason for reason in step.reasons)


def test_a_partial_outage_with_a_too_small_slot_and_an_unavailable_higher_tier_is_unavailable() -> None:
    served = _served("openai-codex/gpt-6-luna", "google-antigravity/gemini-3.8-flash")
    step = next_step(_small_then_medium(served=served), [])
    assert step.kind == PENDING and step.unavailable and not step.oversize
