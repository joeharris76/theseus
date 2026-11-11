"""Release-B output read back by the readers a consumer runs: the gate and the thread deduplication."""

from __future__ import annotations

import re

import pytest

from theseus_reviewer import dedup, protocol, review_check

from .harness import CHECKER, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"
FIRST = "def checker(a, b):\n    return a is b\n"
TITLE = "Identity comparison replaces equality"
DEFECT = {
    "severity": "High",
    "file": CHECKER,
    "line": 2,
    "title": TITLE,
    "detail": "checker returns a is b, so equal values held in distinct objects compare unequal.",
}
STILL_OPEN = {
    "status": "not_fixed",
    "evidence": f"{CHECKER}:2 still returns a is b.",
}
V0_2_8_THREAD = "**High**: {title}\n\n{detail}\n\n<!-- review-defect: c1-D1 -->\n\n<!-- review-finding: {print} -->"


def test_a_ship_in_the_new_layout_passes_the_gate_and_reads_as_success(harness: Harness) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(61, "feat/checker")
    harness.script_omp([{"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}])

    harness.review(61)

    body = harness.github.reviews[61][-1]["body"]
    assert body.startswith("### ✅ SHIP · no open findings")
    assert "### review-verdict" not in body and body.count(f"<!-- review-verdict: v2 success {head} -->") == 1
    assert body.rstrip().splitlines()[-1].startswith(protocol.MARKER_OPEN)
    assert review_check.verdict_state(body, head) == "success"
    assert protocol.decode_marker(body)["decision"] == protocol.SHIP

    gate = harness.gate(61, head)

    assert gate.code == 0, gate.stderr
    assert [item["state"] for item in harness.github.gate_statuses(head)] == ["success", "success"]
    assert harness.github.gate_statuses(head)[-1]["description"].startswith("Pass")


def test_a_blocking_review_in_the_new_layout_holds_the_gate_with_its_findings_named(harness: Harness) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIRST}, "compare by identity")
    harness.github.open_pull(62, "feat/checker")
    harness.script_omp(
        [{"decision": "SHIP_WITH_FIXES", "summary": "One defect.", "files_examined": [CHECKER], "defects": [DEFECT]}]
    )

    harness.review(62)

    review = harness.github.reviews[62][-1]
    assert review["body"].startswith("### 🔧 SHIP WITH FIXES · 1 finding blocks merge")
    assert review_check.verdict_state(review["body"], head) == "failure"
    [thread] = harness.github.threads[62]
    first = thread["comments"][0]["body"]
    assert first.splitlines()[0] == f"🟠 **High** · D1 · {TITLE}"
    assert protocol.thread_finding(first) == (1, "D1")
    assert [item["state"] for item in harness.github.gate_statuses(head)] == ["failure"]


@pytest.mark.parametrize("posted_by", ["this release", "v0.2.8"])
def test_a_thread_in_either_format_is_recognised_so_the_same_finding_is_not_posted_again(
    harness: Harness, posted_by: str
) -> None:
    harness.branch("feat/checker", {CHECKER: FIRST}, "compare by identity")
    harness.github.open_pull(63, "feat/checker")
    harness.script_omp(
        [{"decision": "SHIP_WITH_FIXES", "summary": "One defect.", "files_examined": [CHECKER], "defects": [DEFECT]}]
    )
    harness.review(63)
    [thread] = harness.github.threads[63]
    fingerprint = dedup.fingerprint(CHECKER, TITLE)
    if posted_by == "v0.2.8":
        thread["comments"][0]["body"] = V0_2_8_THREAD.format(title=TITLE, detail=DEFECT["detail"], print=fingerprint)
    assert dedup.thread_fingerprint(CHECKER, thread["comments"][0]["body"]) == fingerprint

    harness.push("feat/checker", {"README.md": "seed\n\nNotes.\n"}, "add notes")
    harness.script_omp(
        [
            {
                "decision": "SHIP_WITH_FIXES",
                "summary": "The defect stays open.",
                "files_examined": [CHECKER],
                "defects": [DEFECT],
                "priors": {"D1": STILL_OPEN},
            }
        ]
    )
    run = harness.review(63)

    assert run.plan["open_findings"] == {fingerprint: "High"}
    assert len(harness.github.threads[63]) == 1
    second = harness.github.reviews[63][-1]["body"]
    assert re.search(r"already open|Still open", second), second
