from __future__ import annotations

import pytest

from theseus_reviewer import status_text

from .harness import CHECKER, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"
SHIP = {"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}


def _open(harness: Harness, pr: int) -> str:
    head = harness.branch(f"feat/pin-{pr}", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(pr, f"feat/pin-{pr}")
    harness.script_omp([SHIP])
    return head


def test_a_failed_install_posts_a_distinct_pending_status_runs_no_attempt_and_is_not_redispatched(
    harness: Harness,
) -> None:
    head = _open(harness, 71)

    run = harness.review(71, installable=False)

    assert run.steps["harness"].code == 0
    assert run.steps["harness"].outputs["state"] == "failed"
    assert "the omp download failed" in run.steps["harness"].stderr
    assert run.steps["harness"].outputs["env"] == ""
    assert "attempts" not in run.steps
    assert run.attempts() == [] and harness.omp_calls() == 0
    [status] = harness.github.review_statuses(head)
    assert status["state"] == "pending"
    assert status["description"] == status_text.OMP_INSTALL
    assert harness.github.artifact_json("review-verdict-state-71", "state.json")["pending_cause"] == "omp-install"

    harness.github.age(run.run, 600)
    before = list(harness.github.dispatches)
    sweep = harness.sweep()

    assert sweep.code == 0, sweep.stderr
    assert harness.github.dispatches == before
    assert harness.omp_calls() == 0


def test_a_binary_swapped_after_the_install_is_refused_before_any_review_runs(harness: Harness) -> None:
    head = _open(harness, 72)

    run = harness.review(72, swap_after_install=True)

    assert run.steps["harness"].outputs["state"] == "installed"
    attempts = run.attempts()
    assert attempts
    for attempt in attempts:
        assert attempt["outcome"] == "absent" and attempt["absence"]["kind"] == "error"
        assert "does not match the pinned sha256" in attempt["absence"]["detail"]
    assert harness.omp_calls() == 0
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["pending"]


def test_a_pin_with_a_path_character_build_key_stops_the_plan(harness: Harness) -> None:
    _open(harness, 73)
    text = harness.policy_file.read_text(encoding="utf-8")
    assert "    linux-x64:" in text
    harness.policy_file.write_text(text.replace("    linux-x64:", "    ../escape-x64:"), encoding="utf-8")

    run = harness.review(73)

    assert run.steps["prepare"].code != 0
    assert "not a platform name" in run.steps["prepare"].stderr
    assert harness.github.review_statuses(harness.github.pulls[73]["head"]["sha"]) == []
    assert harness.omp_calls() == 0
