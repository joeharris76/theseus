from __future__ import annotations

import pytest

from .fake_github import OWNER_TOKEN
from .harness import CHECKER, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIRST = "def checker(a, b):\n    return a is b\n"
DEFECT = {
    "severity": "High",
    "file": CHECKER,
    "line": 99,
    "title": "Unreachable branch lacks validation",
    "detail": "Line 99 is outside the hunk and lacks input bounds validation.",
}
DISPUTE_ONE = f"Dispute: D1 - line 99 is unreachable from the public interface, see {CHECKER}:2."
DISPUTE_TWO = f"Dispute: D1 - input bounds are enforced at the gateway boundary, see {CHECKER}:2."
ADJUDICATION = "Review adjudication: WITHDRAW D1 - verified as dead code by owner"


def test_threadless_finding_dispute_escalation_adjudication_gate_flow(harness: Harness) -> None:
    pr = 80
    harness.branch("feat/body-only", {CHECKER: FIRST}, "add checker")
    harness.github.open_pull(pr, "feat/body-only")
    harness.script_omp(
        [
            {
                "decision": "SHIP_WITH_FIXES",
                "summary": "One defect outside diff.",
                "files_examined": [CHECKER],
                "defects": [DEFECT],
            }
        ]
    )

    run = harness.review(pr)
    head = run.plan["head_sha"]

    assert harness.github.threads[pr] == []
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["failure"]
    latest_review = harness.github.reviews[pr][-1]["body"]
    assert "D1 · " in latest_review and "(thread below)" not in latest_review
    assert "Dispute: D<n> - <reason>" in latest_review

    gate_step = harness.gate(pr, head)
    assert gate_step.code == 3, gate_step.stderr
    assert "open findings" in harness.github.gate_statuses(head)[-1]["description"]

    harness.github.comment(pr, DISPUTE_ONE, token=OWNER_TOKEN)
    rerun_cmd_1 = harness.github.comment(pr, "/review", token=OWNER_TOKEN)

    harness.script_omp(
        [
            {
                "decision": "SHIP_WITH_FIXES",
                "summary": "Maintained on first dispute round.",
                "files_examined": [CHECKER],
                "priors": {
                    "D1": {
                        "status": "not_fixed",
                        "evidence": f"{CHECKER}:99 still lacks bounds validation.",
                        "claim": DISPUTE_ONE,
                        "concedes": False,
                    }
                },
            }
        ]
    )

    run_disp_1 = harness.review(pr, event_name="issue_comment", action="created", comment=rerun_cmd_1)
    assert run_disp_1.plan["protocol"]["forced"] is True
    assert run_disp_1.plan["protocol"]["round"] == 2
    assert [item["id"] for item in run_disp_1.plan["disputes"]] == ["D1"]
    assert harness.github.threads[pr] == []
    assert [item["state"] for item in harness.github.review_statuses(head)] == ["failure", "failure"]
    assert harness.gate(pr, head).code == 3
    assert "escalated" not in harness.github.review_statuses(head)[-1]["description"]

    harness.github.comment(pr, DISPUTE_TWO, token=OWNER_TOKEN)
    rerun_cmd_2 = harness.github.comment(pr, "/review", token=OWNER_TOKEN)

    harness.script_omp(
        [
            {
                "decision": "SHIP_WITH_FIXES",
                "summary": "Maintained on second dispute round.",
                "files_examined": [CHECKER],
                "priors": {
                    "D1": {
                        "status": "not_fixed",
                        "evidence": f"{CHECKER}:99 gateway enforcement not demonstrated.",
                        "claim": DISPUTE_TWO,
                        "concedes": False,
                    }
                },
            }
        ]
    )

    run_disp_2 = harness.review(pr, event_name="issue_comment", action="created", comment=rerun_cmd_2)
    assert run_disp_2.plan["protocol"]["forced"] is True
    assert run_disp_2.plan["protocol"]["round"] == 3
    assert run_disp_2.plan["protocol"]["maintained"] == {"D1": 1}

    latest_status = harness.github.review_statuses(head)[-1]
    assert latest_status["state"] == "failure"
    assert latest_status["description"].endswith("; escalated D1")
    assert "Escalated to the owner: D1" in harness.github.reviews[pr][-1]["body"]
    assert "Review adjudication: WITHDRAW|MAINTAIN D<n> - <reason>" in harness.github.reviews[pr][-1]["body"]

    gate_step_2 = harness.gate(pr, head)
    assert gate_step_2.code == 3
    assert "need the owner's ruling" in harness.github.gate_statuses(head)[-1]["description"]

    rerun_cmd_3 = harness.github.comment(pr, "/review", token=OWNER_TOKEN)
    run_refused = harness.review(pr, event_name="issue_comment", action="created", comment=rerun_cmd_3)
    assert run_refused.plan["decision"] == "skip"
    assert "already had 2 dispute re-reviews; D1 escalated to the owner" in run_refused.plan["decision_reason"]

    adjudication_comment = harness.github.comment(pr, ADJUDICATION, token=OWNER_TOKEN)
    run_adj = harness.review(pr, event_name="issue_comment", action="created", comment=adjudication_comment)
    assert run_adj.plan["decision"] == "carry"
    assert run_adj.plan["adjudication"] == {
        "action": "WITHDRAW",
        "id": "D1",
        "reason": "verified as dead code by owner",
    }

    latest_review = harness.github.reviews[pr][-1]
    assert latest_review["body"].startswith("### ✅ SHIP · owner ruling: WITHDRAW D1")
    assert "**Owner ruling:** WITHDRAW D1 - verified as dead code by owner" in latest_review["body"]
    latest_verdict_status = harness.github.review_statuses(head)[-1]
    assert latest_verdict_status["state"] == "success"
    assert "owner adjudication: WITHDRAW D1; SHIP" in latest_verdict_status["description"]

    gate_final = harness.gate(pr, head)
    assert gate_final.code == 0, gate_final.stderr
    assert harness.github.gate_statuses(head)[-1]["state"] == "success"
    assert f"review: pass (review verdict for {head})" in harness.github.gate_statuses(head)[-1]["description"]
