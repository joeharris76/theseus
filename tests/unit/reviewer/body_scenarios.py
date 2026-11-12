"""Review-body scenarios shaped like real hosted reviews, rendered by whatever renderer is importable.

The same module produced the "before" bodies in tests/fixtures/review_body_before with the previous renderer, so
the before and after sizes are measured on identical inputs.
"""

from __future__ import annotations

from typing import Any

from theseus_reviewer import report, selection
from theseus_reviewer.diff import commentable_lines
from theseus_reviewer.policy import Policy
from theseus_reviewer.selection import Attempt, Step
from theseus_reviewer.verdict import validate

from .test_attempts import CHECKER, DIFF, _plan, _v
from .test_rounds import OTHER, _prior, _rules

OLD_HEAD = "8f192e3e1e596e460c67c8103a6685ee6f504735"
COMMENTABLE = commentable_lines(DIFF)
BRIEF_NOTES = (
    "Read both changed files in full, then followed the new protocol logic into the report writer, the runner and "
    "the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the "
    "whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited "
    "evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed "
    "file's path is compared after normalisation. "
)
LONG_DETAIL = (
    "The collision check runs only when this branch's policy lists several bases, and the rival-base lookup "
    "additionally ignores bases absent from that local policy, so a pull request whose head is shared with another "
    "base is judged against the wrong policy. The consequence is that a gate can pass on a head that another "
    "protected branch would have refused, and the second branch never sees the first branch's findings. "
    "Detect the collision before consulting the local policy, and refuse when the shared head has no review on "
    "every base that contains it."
)
SIX = (
    ("High", "Detect shared-head collisions independently of the local base policy", 204),
    ("High", "Do not manufacture a maintained disposition from a fixed answer", 1107),
    ("High", "Unchanged finding lines do not verify pre-existing behavior", 858),
    ("High", "Bind accepted live results to trusted execution evidence", 677),
    ("High", "Include gate, scorer and corpus changes in evaluation enforcement", 640),
    ("Medium", "Do not report a passing evaluation gate without a baseline", 653),
)


def _step(policy: Policy, kind: str, reviewer: str = "small") -> Step:
    return Step(kind, policy.reviewers[reviewer])


def _plan_for(policy: Policy, **over: Any) -> dict[str, Any]:
    plan = {**_plan(policy, ("small",)), "mode": "enforce", "findings_delivery": "review"}
    plan["protocol"] = _rules(**over)
    return plan


def _render(policy: Policy, plan: dict[str, Any], verdict: dict[str, Any], attempts: list[Attempt] | None = None):
    kind = selection.FAIL if verdict["defects"] else selection.PASS
    done = Attempt(len(attempts or []) + 1, "small", kind)
    tried = [*(attempts or []), done]
    return report.finalize(plan, _step(policy, kind), [], tried, {done.slot: validate(verdict)}, COMMENTABLE)


def six_findings_without_threads(policy: Policy) -> report.Final:
    """Six findings in files outside the diff, so none has a thread (a SHIP WITH FIXES like BenchBox #4062)."""
    plan = _plan_for(policy, next_id=2)
    defects = [
        {"severity": s, "file": OTHER, "line": line, "title": title, "detail": LONG_DETAIL, "end_line": line + 2}
        for s, title, line in SIX
    ]
    return _render(policy, plan, _v(BRIEF_NOTES * 2, defects))


def one_finding_with_a_thread(policy: Policy) -> report.Final:
    """One finding with a thread after a fix and three unfinished attempts (a SHIP WITH FIXES like Theseus #45)."""
    plan = _plan_for(
        policy,
        kind="follow-up",
        round=2,
        prior=[_prior("D1")],
        next_id=2,
        changed=[CHECKER],
    )
    plan.update(scope="changed", reviewed_head=OLD_HEAD)
    defect = {
        "severity": "Medium",
        "file": CHECKER,
        "line": 2,
        "title": "Advisory demotion compares exact path strings, so another spelling of a changed file's path skips it",
        "detail": LONG_DETAIL,
    }
    verdict = _v("One fix is needed before merge. " + BRIEF_NOTES * 3, [defect])
    verdict["prior_defects"] = [{"id": "D1", "status": "fixed", "evidence": "the path is now normalised"}]
    absent = [
        Attempt(slot, "small", selection.ABSENT, absence="incomplete", detail="did not read 86 of 146 changed hunks")
        for slot in (1, 2, 3)
    ]
    return _render(policy, plan, verdict, absent)


def a_ship_after_fixes(policy: Policy) -> report.Final:
    """A SHIP whose reviewer summary is a long paragraph (like Theseus #45's final SHIP)."""
    plan = _plan_for(policy, kind="follow-up", round=3, prior=[_prior("D11"), _prior("D14")], next_id=15)
    plan.update(scope="changed", reviewed_head=OLD_HEAD)
    verdict = _v(BRIEF_NOTES * 4)
    verdict["prior_defects"] = [
        {"id": "D11", "status": "fixed", "evidence": "fixed"},
        {"id": "D14", "status": "fixed", "evidence": "fixed"},
    ]
    return _render(policy, plan, verdict)


SCENARIOS = {
    "swf_six_threadless": six_findings_without_threads,
    "swf_one_threaded": one_finding_with_a_thread,
    "ship_after_fixes": a_ship_after_fixes,
}
