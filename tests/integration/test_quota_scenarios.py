from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from theseus_reviewer import status_text

from .harness import CHECKER, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIRST = "def checker(a, b):\n    return bool(a == b)\n"
SECOND = "def checker(a, b):\n    return bool(a == b) or a is b\n"
CLAUDE_REVIEWERS = 3
SHIP = {"decision": "SHIP", "summary": "The change keeps checker semantics; no defects.", "files_examined": [CHECKER]}


def _quota(provider: str) -> dict:
    return {
        "kind": "error",
        "status": 429,
        "provider": provider,
        "message": "rate_limit_error: 429 Too Many Requests. Resets in 5d.",
    }


def _blocked_claude(harness: Harness, pr: int, provider: str) -> str:
    head = harness.branch("feat/quota", {CHECKER: FIRST}, "wrap in bool")
    harness.github.open_pull(pr, "feat/quota")
    harness.script_omp([_quota(provider)] * CLAUDE_REVIEWERS + [SHIP])
    run = harness.review(pr)
    assert run.steps["finalize"].outputs["state"] == "pending"
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["pending"]
    return head


def _recorded_block(harness: Harness, pr: int) -> dict:
    return harness.github.artifact_json(f"review-verdict-state-{pr}", "state.json")["pool_blocked_until"]["claude"]


def test_a_fallback_providers_quota_reset_does_not_block_the_next_head(harness: Harness) -> None:
    _blocked_claude(harness, 91, "openai-codex")
    block = _recorded_block(harness, 91)
    assert harness.omp_calls() == CLAUDE_REVIEWERS
    head = harness.push("feat/quota", {CHECKER: SECOND}, "short-circuit on identity")

    run = harness.review(91)

    assert harness.omp_calls() == CLAUDE_REVIEWERS + 1
    assert run.plan["pool_blocked_until"] == {}
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["success"]
    assert block["provider"] == "openai-codex"
    assert datetime.fromisoformat(block["until"]) - datetime.fromisoformat(block["recorded_at"]) <= timedelta(hours=1)


def test_a_primary_providers_quota_reset_blocks_the_pool_for_at_most_the_backoff(harness: Harness) -> None:
    _blocked_claude(harness, 92, "anthropic")
    block = _recorded_block(harness, 92)
    head = harness.push("feat/quota", {CHECKER: SECOND}, "short-circuit on identity")

    run = harness.review(92)

    assert harness.omp_calls() == CLAUDE_REVIEWERS
    assert list(run.plan["pool_blocked_until"]) == ["claude"]
    [status] = harness.github.review_statuses(head)
    assert status["state"] == "pending"
    assert status["description"].startswith(status_text.unavailable("provider quota"))
    assert block["provider"] == "anthropic"
    held = datetime.fromisoformat(block["until"]) - datetime.fromisoformat(block["recorded_at"])
    assert timedelta(minutes=59) < held <= timedelta(hours=1)


def test_a_primary_providers_block_is_cleared_once_the_backoff_passes_and_the_provider_answers(
    harness: Harness,
) -> None:
    _blocked_claude(harness, 93, "anthropic")
    harness.github.age(harness.github.runs[-1], 61)
    head = harness.push("feat/quota", {CHECKER: SECOND}, "short-circuit on identity")

    run = harness.review(93)

    assert harness.omp_calls() == CLAUDE_REVIEWERS + 1
    assert run.plan["pool_blocked_until"] == {}
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["success"]
    assert harness.github.artifact_json("review-verdict-state-93", "state.json")["pool_blocked_until"] == {}


def test_a_manual_dispatch_during_the_backoff_leaves_the_recorded_block_unchanged(harness: Harness) -> None:
    _blocked_claude(harness, 94, "anthropic")
    before = _recorded_block(harness, 94)

    run = harness.review(94, event_name="workflow_dispatch", inputs={"reason": "manual"})

    assert harness.omp_calls() == CLAUDE_REVIEWERS
    assert run.steps["prepare"].outputs["decision"] != "review"
    assert _recorded_block(harness, 94) == before


def test_a_quota_on_one_reviewers_models_does_not_skip_the_rest_of_its_pool(harness: Harness) -> None:
    head = harness.branch("feat/quota", {CHECKER: FIRST}, "wrap in bool")
    harness.github.open_pull(95, "feat/quota")
    harness.script_omp([_quota("anthropic"), SHIP])

    harness.review(95)

    assert harness.omp_calls() == 2
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["success"]
    block = _recorded_block(harness, 95)
    assert "anthropic/claude-sonnet-5-5" in block["models"]
    assert "anthropic/claude-opus-5-5" not in block["models"]
