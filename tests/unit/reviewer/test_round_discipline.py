from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import ALPHA_POLICY
from theseus_reviewer import cli, coverage, protocol, selection
from theseus_reviewer._vendor import yaml
from theseus_reviewer.policy import Policy, PolicyError, parse_policy
from theseus_reviewer.recovery import RETRY, Recovery

from .test_attempts import CHECKER, HEAD, _classification, _plan, _v
from .test_evidence_retry import _file_list_plan
from .test_rounds import _decided, _final, _prior, _protocol_plan, _with_priors

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PATHS = ["a.py", "b.py", "c.py"]
OLD = {path: "1" * 16 for path in PATHS}


def _patch_map(**changed: str) -> dict[str, str]:
    prints = {path: changed.get(path[0], "1" * 16) * 4 for path in PATHS}
    return protocol.patch_map(prints, PATHS)


def _record(round_number: int, *, defects: int = 1, kind: str = protocol.FOLLOW_UP, **over: Any) -> dict[str, Any]:
    record = {
        **_decided(HEAD),
        "round": round_number,
        "kind": kind if round_number > 1 else protocol.FIRST,
        "patch_digest": f"{round_number:032d}",
        "patch_map": protocol.patch_map({path: "1" * 64 for path in PATHS}, PATHS),
        "open_defects": [
            {"id": f"D{n}", "severity": "High", "file": "a.py", "line": 3, "end_line": None, "title": f"class {n}"}
            for n in range(1, defects + 1)
        ],
        "next_id": defects + 1,
        **over,
    }
    return record


def _next(records: list[dict[str, Any]], cap: int | None, **over: Any) -> protocol.Round:
    patches = protocol.patch_map({**{path: "1" * 64 for path in PATHS}, "b.py": "2" * 64}, PATHS)
    return protocol.plan_round(
        protocol.History(records),
        head_sha="e" * 40,
        base_ref="develop",
        digest="9" * 32,
        patches=patches,
        paths=PATHS,
        strike_count=0,
        max_do_not_ship=3,
        max_finding_rounds=cap,
        **over,
    )


def test_without_a_cap_a_follow_up_stays_scoped_to_the_changed_files() -> None:
    step = _next([_record(1), _record(2), _record(3), _record(4)], None)
    assert (step.kind, step.step_back) == (protocol.FOLLOW_UP, False)
    assert step.changed == frozenset({"b.py"})


def test_below_the_cap_a_follow_up_is_ordinary() -> None:
    step = _next([_record(1), _record(2)], 3)
    assert step.step_back is False and step.changed == frozenset({"b.py"})


def test_at_the_cap_the_next_round_is_one_full_scope_step_back_that_keeps_every_open_defect() -> None:
    records = [_record(1), _record(2, defects=2)]
    step = _next(records, 2)
    assert step.kind == protocol.FOLLOW_UP and step.step_back is True
    assert step.changed == frozenset(PATHS)
    assert step.cycle == 1 and step.number == 3
    assert [item["id"] for item in step.previous["open_defects"]] == ["D1", "D2"]
    assert "step-back" in step.reason and "2 rounds" in step.reason


def test_the_step_back_happens_once_and_later_rounds_are_ordinary_again() -> None:
    records = [_record(1), _record(2), _record(3, step_back=1)]
    step = _next(records, 2)
    assert step.step_back is False and step.changed == frozenset({"b.py"})


def test_a_round_with_no_open_defects_resets_the_count() -> None:
    clean = _record(3, defects=0, decision=protocol.SHIP)
    step = _next([_record(1), _record(2), clean, _record(4, cycle=1)], 3)
    assert step.step_back is False
    assert protocol.finding_streak([_record(1), _record(2), clean, _record(4)], 1) == [_record(4)]


def test_forced_dispute_rounds_count_toward_the_cap() -> None:
    records = [_record(1), _record(2, forced=True, disputed=["D1"])]
    assert protocol.finding_streak(records, 1) == records[::-1]
    assert _next(records, 2).step_back is True


def test_carries_do_not_count_as_rounds() -> None:
    carried = _record(2, kind=protocol.CARRY, carried=True)
    assert len(protocol.finding_streak([_record(1), carried], 1)) == 1


def test_another_cycle_starts_a_fresh_count() -> None:
    records = [_record(1, cycle=1), _record(2, cycle=1), _record(1, cycle=2)]
    assert len(protocol.finding_streak(records, 2)) == 1


def test_step_back_and_advisory_survive_the_marker_round_trip_and_bad_values_are_refused() -> None:
    entry = {"id": "D4", "severity": "Low", "file": "a.py", "line": 3, "end_line": None, "title": "old"}
    record = {k: v for k, v in _record(3).items() if k != "patch_map"}
    marker = protocol.encode_marker({**record, "step_back": 1, "advisory": [entry]})
    decoded = protocol.decode_marker(marker)
    assert decoded is not None and decoded["step_back"] == 1 and decoded["advisory"] == [entry]
    for bad in ({"step_back": 2}, {"step_back": True}, {"advisory": [{"id": "x"}]}, {"advisory": [entry] * 6}):
        assert protocol.checked_record({**record, **bad}) is None


def test_a_step_back_review_says_so_in_the_body_and_records_it(policy: Policy, tmp_path: Path) -> None:
    plan = _protocol_plan(
        policy,
        kind="follow-up",
        round=3,
        prior=[_prior("D1")],
        changed=[CHECKER],
        next_id=2,
        step_back=True,
        finding_rounds=2,
    )
    final = _final(policy, tmp_path, plan, _with_priors(_v("s"), D1="not_fixed"))
    assert final.review is not None
    assert "Step back: this pull request has had 2 review rounds with findings" in final.review["body"]
    assert protocol.decode_marker(final.review["body"])["step_back"] == 1
    ordinary = _final(
        policy,
        tmp_path / "ordinary",
        _protocol_plan(policy, kind="follow-up", round=3, prior=[_prior("D1")], changed=[CHECKER], next_id=2),
        _with_priors(_v("s"), D1="not_fixed"),
    )
    assert "Step back:" not in ordinary.review["body"]
    assert "step_back" not in protocol.decode_marker(ordinary.review["body"])


def _brief(policy: Policy, tmp_path: Path, **protocol_over: Any) -> tuple[str, Path]:
    base = _file_list_plan(_plan(policy, ("large", "huge")))
    plan = {
        **base,
        "brief_mode": "inline",
        "diff_inline": "diff --git a/x b/x\n+x\n",
        "protocol": {
            "prior": [_prior("D1"), _prior("D2")],
            "forced": False,
            "claimed": [],
            "next_id": 3,
            **protocol_over,
        },
    }
    plan_dir, workspace = tmp_path / "plan", tmp_path / "workspace"
    plan_dir.mkdir()
    workspace.mkdir()
    from .test_attempt_loop import _git

    _git(workspace, "init", "--quiet")
    return cli._assemble_prompt(plan, plan_dir, _classification("large"), workspace), workspace


def test_the_step_back_brief_asks_for_every_remaining_instance_of_each_class(policy: Policy, tmp_path: Path) -> None:
    brief, _ = _brief(policy, tmp_path, step_back=True, finding_rounds=4)
    assert "this is the one step-back review" in brief and "4 review rounds with findings" in brief
    assert "report all instances of a class in one defect entry" in brief
    assert "Earlier defects:" in brief and "never dropped by stepping back" in brief


def test_an_ordinary_follow_up_brief_has_no_step_back_instruction(policy: Policy, tmp_path: Path) -> None:
    brief, _ = _brief(policy, tmp_path)
    assert "step-back" not in brief


def test_the_class_level_rule_is_in_every_template() -> None:
    from theseus_reviewer.render import TEMPLATE_DIR, TEMPLATES

    for name in TEMPLATES:
        text = (TEMPLATE_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert "list it once: name the class in the title" in text and "(v3)" in text


def _since_inputs(text: str | None) -> Any:
    return type("Inputs", (), {"since_diff": staticmethod(lambda previous: text)})()


@pytest.mark.parametrize(
    "step",
    [
        protocol.Round(protocol.FOLLOW_UP, "r", previous={"head_sha": "c" * 40}),
        protocol.Round(protocol.FOLLOW_UP, "r", previous={"head_sha": "c" * 40}, forced=True),
        protocol.Round(protocol.FOLLOW_UP, "r", previous={"head_sha": "c" * 40}, step_back=True),
        protocol.Round(protocol.FIRST, "r"),
    ],
    ids=["follow-up", "forced", "step-back", "first"],
)
def test_only_an_ordinary_follow_up_stages_the_since_review_diff(step: protocol.Round, tmp_path: Path) -> None:
    plan: dict[str, Any] = {"head_sha": HEAD}
    cli._stage_since_review(plan, tmp_path, step, _since_inputs("diff --git a/a b/a\n+x\n"))
    staged = (tmp_path / cli.SINCE_DIFF_FILE).is_file()
    assert staged is (step.kind == protocol.FOLLOW_UP and not step.forced and not step.step_back)
    assert (plan.get("since_review") is not None) is staged


@pytest.mark.parametrize("text", [None, "", "  \n", "x" * 400_000])
def test_a_missing_empty_or_oversize_since_diff_is_simply_not_staged(text: str | None, tmp_path: Path) -> None:
    plan: dict[str, Any] = {"head_sha": HEAD}
    step = protocol.Round(protocol.FOLLOW_UP, "r", previous={"head_sha": "c" * 40})
    cli._stage_since_review(plan, tmp_path, step, _since_inputs(text))
    assert "since_review" not in plan and not (tmp_path / cli.SINCE_DIFF_FILE).exists()


def test_the_delta_first_brief_stages_the_since_diff_and_leaves_coverage_counting_every_hunk(
    policy: Policy, tmp_path: Path
) -> None:
    plan_dir, workspace = tmp_path / "plan", tmp_path / "workspace"
    plan_dir.mkdir()
    workspace.mkdir()
    from .test_attempt_loop import _git

    _git(workspace, "init", "--quiet")
    full = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n a\n+x\n"
    (plan_dir / cli.DIFF_FILE).write_text(full, encoding="utf-8")
    (plan_dir / cli.SINCE_DIFF_FILE).write_text("diff --git a/x b/x\n+delta\n", encoding="utf-8")
    base = _file_list_plan(_plan(policy, ("large", "huge")))
    plan = {
        **base,
        "evidence_files": ["x"],
        "reviewed_files": [{"path": "x", "previous_path": None, "additions": 1, "deletions": 0, "status": "modified"}],
        "protocol": {"prior": [_prior("D1")], "forced": False, "claimed": [], "next_id": 2},
    }
    brief = cli._assemble_prompt(plan, plan_dir, _classification("large"), workspace)
    without = coverage.targets(plan, workspace)
    assert "holds the changes since the last reviewed head" not in brief
    plan_with = {**plan, "since_review": {"head": "c" * 40, "file": cli.SINCE_DIFF_FILE}}
    workspace_two = tmp_path / "workspace2"
    workspace_two.mkdir()
    _git(workspace_two, "init", "--quiet")
    brief = cli._assemble_prompt(plan_with, plan_dir, _classification("large"), workspace_two)
    assert ".review-since-last-review.diff holds the changes since the last reviewed head" in brief
    assert "every changed hunk must still be seen" in brief
    assert (workspace_two / ".review-since-last-review.diff").read_text(encoding="utf-8").endswith("+delta\n")
    assert coverage.targets(plan_with, workspace_two) == without
    assert {path for path, target in without.items() if target.hunks} == {"x"}


def _forced_plan(policy: Policy, **over: Any) -> dict[str, Any]:
    plan = _protocol_plan(
        policy,
        kind="follow-up",
        round=2,
        forced=True,
        prior=[_prior("D1")],
        changed=[CHECKER],
        maintained={},
        next_id=2,
    )
    rebuttal = "Dispute: pkg/kept.py:12 keeps the row, so the defect is wrong."
    plan["disputes"] = [
        {
            "id": "D1",
            "cycle": 1,
            "path": "a.py",
            "title": "t",
            "replies": [{"author": "joe", "body": rebuttal}],
            "claim_hashes": [protocol.claim_hash(protocol.canonical_claim(rebuttal))],
        }
    ]
    return {**plan, **over}


def _fixed_verdict() -> Any:
    from theseus_reviewer.verdict import validate

    return validate(
        {**_v("s"), "prior_defects": [{"id": "D1", "status": "fixed", "evidence": "pkg/kept.py:12 keeps the row"}]}
    )


def test_a_forced_fixed_is_invalid_while_another_attempt_remains(policy: Policy) -> None:
    plan = _forced_plan(policy)
    assert protocol.demote_forced_fixed(_fixed_verdict(), plan) == _fixed_verdict()
    assert protocol.contradiction(_fixed_verdict(), plan) is not None


def test_a_forced_fixed_on_the_last_attempt_is_demoted_to_maintained_so_the_round_publishes(
    policy: Policy, tmp_path: Path
) -> None:
    plan = _forced_plan(policy, last_attempt=True)
    demoted = protocol.demote_forced_fixed(_fixed_verdict(), plan)
    entry = demoted.prior_defects[0]
    assert (entry.status, entry.demoted) == ("not_fixed", "disposition")
    assert "recorded as maintained" in entry.evidence
    assert protocol.contradiction(demoted, plan) is None
    judgement = protocol.judge_planned(demoted, plan)
    assert judgement.decision == protocol.SHIP_WITH_FIXES and [item["id"] for item in judgement.carried] == ["D1"]
    final = _final(policy, tmp_path, plan, demoted.to_json())
    record = protocol.decode_marker(final.review["body"])
    assert record["disputed"] == ["D1"] and [item["id"] for item in record["open_defects"]] == ["D1"]
    assert "D1 (maintained)" in final.review["body"]


def test_the_demoted_round_counts_toward_escalation(policy: Policy, tmp_path: Path) -> None:
    plan = _forced_plan(policy, last_attempt=True)
    plan["protocol"]["maintained"] = {"D1": 1}
    demoted = protocol.demote_forced_fixed(_fixed_verdict(), plan)
    final = _final(policy, tmp_path, plan, demoted.to_json())
    assert protocol.decode_marker(final.review["body"])["escalated"] == ["D1"]


def test_an_unforced_fixed_is_never_demoted(policy: Policy) -> None:
    plan = _forced_plan(policy, last_attempt=True)
    plan["protocol"]["forced"] = False
    assert protocol.demote_forced_fixed(_fixed_verdict(), plan) == _fixed_verdict()


def _attempt_marker(policy: Policy, tmp_path: Path, slot: int) -> bool:
    plan = _forced_plan(policy, chain=[policy.reviewers["small"].to_json()])
    attempts = []
    if slot == 2:
        attempts = [selection.Attempt(1, "small", selection.ABSENT, "incomplete")]
    return cli._with_last_attempt(plan, _classification("small"), attempts, policy.reviewers["small"], slot)[
        "last_attempt"
    ]


def test_the_last_attempt_is_derived_from_the_selection_not_from_a_counter(policy: Policy, tmp_path: Path) -> None:
    assert _attempt_marker(policy, tmp_path, 1) is False
    assert _attempt_marker(policy, tmp_path, 2) is True


def test_an_unforced_plan_gets_no_last_attempt_marker(policy: Policy) -> None:
    plan = {**_plan(policy, ("small",)), "protocol": {"forced": False}}
    assert cli._with_last_attempt(plan, _classification("small"), [], policy.reviewers["small"], 1) is plan
    assert Recovery(RETRY, "fallback").choice == RETRY


def _policy_text() -> dict[str, Any]:
    return yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))


def test_the_new_protocol_keys_default_to_today_and_are_strictly_validated() -> None:
    data = _policy_text()
    parsed = parse_policy(data)
    assert parsed.protocol.max_finding_rounds is None and parsed.protocol.pre_existing == "block"
    assert parsed.brief.include_intent is False
    data["protocol"].update(max_finding_rounds=4, pre_existing="advisory")
    data["brief"] = {"include_intent": True}
    parsed = parse_policy(data)
    assert parsed.protocol.max_finding_rounds == 4 and parsed.protocol.pre_existing == "advisory"
    assert parsed.brief.include_intent is True
    for key, value in (("max_finding_rounds", 0), ("max_finding_rounds", True), ("max_finding_rounds", "3")):
        bad = _policy_text()
        bad["protocol"][key] = value
        with pytest.raises(PolicyError, match="max_finding_rounds"):
            parse_policy(bad)
    bad = _policy_text()
    bad["protocol"]["pre_existing"] = "ignore"
    with pytest.raises(PolicyError, match="pre_existing"):
        parse_policy(bad)
    for brief in ({"include_intent": "yes"}, {"include_intent": True, "title": True}, ["include_intent"]):
        bad = _policy_text()
        bad["brief"] = brief
        with pytest.raises(PolicyError, match="brief"):
            parse_policy(bad)


def test_after_a_forced_fixed_invalid_every_later_attempt_demotes_even_if_attempts_remain(policy: Policy) -> None:
    plan = _forced_plan(policy, chain=[policy.reviewers["small"].to_json(), policy.reviewers["medium"].to_json()])
    detail = f"disputed finding D1 {protocol.FORCED_FIXED_MARK}, which cannot fix anything"
    stalled = [selection.Attempt(1, "small", selection.ABSENT, "invalid", detail)]
    marked = cli._with_last_attempt(plan, _classification("small"), stalled, policy.reviewers["small"], 2)
    assert marked["last_attempt"] is True
    other = [selection.Attempt(1, "small", selection.ABSENT, "invalid", "some other problem")]
    assert (
        cli._with_last_attempt(plan, _classification("small"), other, policy.reviewers["small"], 2)["last_attempt"]
        is False
    )
