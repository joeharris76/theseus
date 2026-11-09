from __future__ import annotations

import pytest

from .harness import CHECKER, SLUG, Harness, ago

pytestmark = [pytest.mark.integration, pytest.mark.medium]

CHANGED = "def checker(a, b):\n    return bool(a == b)\n"
UNAVAILABLE = {
    "kind": "error",
    "status": 500,
    "provider": "anthropic",
    "message": "api_error: 500 Internal Server Error",
}
VERDICT = "review-verdict.yml"


def _failed_run(harness: Harness, pr: int, head: str, event: str, minutes: int, step: str) -> dict:
    run = harness.github.add_run(VERDICT, event=event, pr=pr, head_sha=head, title_head=head, created_at=ago(minutes))
    harness.github.complete_run(run, "failure", (step,))
    return run


def test_a_failed_run_is_redispatched_once_then_a_second_failure_is_surfaced_as_pending(harness: Harness) -> None:
    head = harness.branch("feat/sweep", {CHECKER: CHANGED}, "wrap in bool")
    harness.github.open_pull(61, "feat/sweep")
    _failed_run(harness, 61, head, "pull_request_target", 40, "Run the attempt loop")

    first = harness.sweep()

    assert first.code == 0, first.stderr
    assert harness.github.dispatches == [
        {"workflow": VERDICT, "ref": "develop", "inputs": {"pr": "61", "head": head, "reason": "stateless-retry"}}
    ]
    assert harness.github.review_statuses(head) == []

    retry = _failed_run(harness, 61, head, "workflow_dispatch", 20, "Verify Linux process isolation")
    second = harness.sweep()
    third = harness.sweep()

    assert (second.code, third.code) == (0, 0)
    assert len(harness.github.dispatches) == 1
    surfaced = harness.github.review_statuses(head)
    assert [(item["state"], item["creator"]["login"]) for item in surfaced] == [("pending", "benchbox-review[bot]")]
    assert "Verify Linux process isolation" in surfaced[0]["description"]
    assert surfaced[0]["target_url"].endswith(f"/{SLUG}/actions/runs/{retry['id']}")


def _pending_review(harness: Harness, pr: int, branch: str) -> tuple[str, dict]:
    head = harness.branch(branch, {CHECKER: CHANGED}, "wrap in bool")
    harness.github.open_pull(pr, branch)
    harness.script_omp([UNAVAILABLE])
    run = harness.review(pr)
    assert run.steps["finalize"].outputs["state"] == "pending"
    assert harness.github.artifact_json(f"review-verdict-state-{pr}", "state.json")["pending_cause"] == "all-absent"
    return head, run.run


def test_a_same_head_pending_state_past_its_backoff_is_dispatched(harness: Harness) -> None:
    head, run = _pending_review(harness, 62, "feat/pending")
    harness.github.age(run, 180)

    sweep = harness.sweep()

    assert sweep.code == 0, sweep.stderr
    assert harness.github.dispatches[-1:] == [
        {"workflow": VERDICT, "ref": "develop", "inputs": {"pr": "62", "head": head}}
    ]


def test_a_due_same_head_state_is_not_dispatched_over_a_run_in_flight(harness: Harness) -> None:
    head, run = _pending_review(harness, 63, "feat/busy")
    harness.github.age(run, 180)
    harness.github.add_run(
        VERDICT, event="workflow_dispatch", pr=63, head_sha=head, title_head=head, created_at=ago(10)
    )
    before = list(harness.github.dispatches)

    sweep = harness.sweep()

    assert sweep.code == 0, sweep.stderr
    assert harness.github.dispatches == before
    assert "retry skipped for #63: a run is still in flight" in sweep.outputs["summary"]


def test_a_pending_state_for_an_older_head_does_not_dispatch_the_new_head(harness: Harness) -> None:
    _, run = _pending_review(harness, 64, "feat/moved")
    harness.github.age(run, 180)
    harness.push("feat/moved", {CHECKER: "def checker(a, b):\n    return a == b or a is b\n"}, "short-circuit")
    before = list(harness.github.dispatches)

    sweep = harness.sweep()

    assert sweep.code == 0, sweep.stderr
    assert harness.github.dispatches == before


def test_a_manual_retry_whose_guard_fails_still_consumes_the_retry(harness: Harness) -> None:
    head, run = _pending_review(harness, 90, "feat/consumed")
    harness.github.age(run, 180)
    posted = len(harness.github.review_statuses(head))
    harness.script_omp([UNAVAILABLE])

    first = harness.review(
        90,
        event_name="workflow_dispatch",
        before_guard=lambda: harness.github.fail("GET", r"/pulls/90/reviews", 502, 100),
    )
    harness.github.failures.clear()

    assert first.steps["prepare"].outputs["decision"] == "review"
    assert first.steps["publish"].code == 2
    assert len(harness.github.review_statuses(head)) == posted
    harness.script_omp([UNAVAILABLE])
    second = harness.review(90, event_name="workflow_dispatch")
    assert second.steps["prepare"].outputs["decision"] == "skip"


def test_owner_reset_survives_skip_only_runs_and_admits_exactly_one_review(harness: Harness) -> None:
    head, prior = _pending_review(harness, 91, "feat/reset-skips")
    harness.github.age(prior, 30)
    harness.github.comment(91, f"Owner instruction: reset the backoff on {head}", created_at=ago(20))
    harness.github.comment(
        91, f"Review backoff reset: {head}\nReason: retry the unavailable reviewer", created_at=ago(19)
    )
    for event, minutes in (("issue_comment", 10), ("workflow_dispatch", 5)):
        skipped = harness.github.add_run(
            VERDICT,
            event=event,
            pr=91,
            head_sha=harness.github.git("rev-parse", "develop").strip(),
            created_at=ago(minutes),
        )
        harness.github.complete_run(skipped, "success")
        steps = [
            {"name": name, "status": "completed", "conclusion": "skipped"}
            for name in ["Run the attempt loop", *(f"Resume the attempt loop after attempt {n}" for n in range(1, 5))]
        ]
        harness.github.jobs[skipped["id"]] = {
            "total_count": 1,
            "jobs": [
                {
                    "name": "review",
                    "status": "completed",
                    "conclusion": "skipped" if event == "issue_comment" else "success",
                    "steps": [] if event == "issue_comment" else steps,
                }
            ],
        }
    harness.script_omp([UNAVAILABLE])
    first = harness.review(91, event_name="workflow_dispatch")
    assert first.steps["prepare"].outputs["decision"] == "review", first.steps["prepare"].stdout
    assert harness.omp_calls() > 0
    harness.script_omp([UNAVAILABLE])
    second = harness.review(91, event_name="workflow_dispatch")
    assert second.steps["prepare"].outputs["decision"] == "skip", second.steps["prepare"].stdout
    assert harness.omp_calls() == 0
