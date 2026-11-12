from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import known, protocol
from theseus_reviewer.policy import Policy, PolicyError, parse_policy
from theseus_reviewer.verdict import REDACTED, validate

from .test_attempts import CHECKER, _finding, _v
from .test_pre_existing_and_intent import _judged, _old, _plan_with
from .test_rounds import _final

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def _trigger_plan(policy: Policy, tmp_path: Path, trigger: str, addition: str) -> dict[str, Any]:
    (tmp_path / "operation.py").write_text("values = []\ndef ratio(value):\n    return 1 / value\n", encoding="utf-8")
    (tmp_path / trigger).write_text(addition, encoding="utf-8")
    return _plan_with(
        policy,
        added_lines={trigger: [[1, len(addition.splitlines())]]},
        new_files=[trigger],
        changed_paths=[trigger],
        workspace=str(tmp_path),
    )


TRIGGERS = [
    ("caller.py", "from operation import ratio\nratio(0)\n"),
    ("init.py", "from operation import values\nvalues.append(0)\n"),
    ("input.json", '{"denominator": 0}\n'),
]


@pytest.mark.parametrize("severity", ["Critical", "High", "Medium", "Low"])
def test_a_genuine_base_defect_is_known_unless_critical(policy: Policy, severity: str) -> None:
    judgement = _judged(_plan_with(policy), _old(severity))
    if severity == "Critical":
        assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1
        assert judgement.advisory == ()
    else:
        assert judgement.decision == protocol.SHIP and judgement.defects == ()
        assert [item.severity for item in judgement.advisory] == [severity]


@pytest.mark.parametrize(("trigger", "addition"), TRIGGERS)
def test_a_disguised_new_defect_that_cites_its_trigger_blocks(
    policy: Policy, tmp_path: Path, trigger: str, addition: str
) -> None:
    plan = _trigger_plan(policy, tmp_path, trigger, addition)
    cited = _old("High", 3, file="operation.py", detail=f"ratio(0) in {trigger}:1 now raises ZeroDivisionError")
    judgement = _judged(plan, cited)
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1
    assert judgement.advisory == ()


@pytest.mark.parametrize(("trigger", "addition"), TRIGGERS)
def test_a_trigger_the_finding_does_not_cite_is_the_accepted_risk_and_logs_as_known(
    policy: Policy, tmp_path: Path, trigger: str, addition: str
) -> None:
    plan = _trigger_plan(policy, tmp_path, trigger, addition)
    judgement = _judged(plan, _old("High", 3, file="operation.py", detail="a zero denominator raises"))
    assert judgement.decision == protocol.SHIP and len(judgement.advisory) == 1


@pytest.mark.parametrize(
    "detail",
    [
        "The same pattern recurs at checker.py:11.",
        "Also see line 11.",
        "see lines 10-12",
        "permalink checker.py#L11",
        "also at L11",
        "also at call site (11)",
        "see foo: 11",
    ],
)
def test_a_cited_location_on_a_changed_line_blocks(policy: Policy, detail: str) -> None:
    judgement = _judged(_plan_with(policy), _old("Medium", 2, detail=detail))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


@pytest.mark.parametrize(
    ("title", "detail"),
    [
        ("Old issue at line 11", "plain detail"),
        ("Old issue (11)", "plain detail"),
        ("Old issue", "see lines 11-12"),
    ],
)
def test_a_changed_line_in_the_title_blocks_too(policy: Policy, title: str, detail: str) -> None:
    judgement = _judged(_plan_with(policy), {**_old("Medium", 2), "title": title, "detail": detail})
    assert judgement.decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize(
    "detail", ["other.py:3 repeats it", "mirrors util/helpers.py", "see line 4", "benchbox/core/equivalence/checker.py"]
)
def test_a_cited_location_that_is_unchanged_stays_known(policy: Policy, detail: str) -> None:
    judgement = _judged(_plan_with(policy), _old("Medium", 2, detail=detail))
    assert judgement.decision == protocol.SHIP and len(judgement.advisory) == 1


def test_a_cited_file_the_change_modifies_or_adds_blocks_even_without_a_line(policy: Policy) -> None:
    plan = _plan_with(policy, added_lines={"caller.py": [[1, 2]]}, changed_paths=[CHECKER, "caller.py"])
    assert _judged(plan, _old("Medium", 2, detail="called from caller.py")).decision == protocol.SHIP_WITH_FIXES
    added = _plan_with(policy, new_files=["fresh.py"], added_lines={"fresh.py": []}, changed_paths=["fresh.py"])
    assert _judged(added, _old("Medium", 2, detail="used by fresh.py:3")).decision == protocol.SHIP_WITH_FIXES


def test_a_link_or_redaction_in_the_finding_blocks(policy: Policy) -> None:
    for text in ("see https://example.com/o/n/blob/abc/src/other.py#L40", f"token {REDACTED} shows"):
        verdict = validate(_v("s", [{**_old("Medium", 2), "detail": text}]))
        assert protocol.judge_planned(verdict, _plan_with(policy)).decision == protocol.SHIP_WITH_FIXES


def test_at_most_five_known_defects_per_review(policy: Policy) -> None:
    five = [_old("Low", 2, title=f"issue {n}") for n in range(5)]
    assert len(_judged(_plan_with(policy), *five).advisory) == 5
    six = _judged(_plan_with(policy), *[*five, _old("Low", 2, title="issue 5")])
    assert six.advisory == () and six.decision == protocol.SHIP_WITH_FIXES and len(six.defects) == 6


def test_known_defects_do_not_count_toward_the_defect_limit(policy: Policy) -> None:
    plan = {**_plan_with(policy), "max_defects": 1}
    judgement = _judged(plan, _old("Medium"), _old("Low", title="other"), _finding("Low", 11))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1
    assert len(judgement.advisory) == 2


def test_an_introduced_finding_alongside_known_ones_still_blocks(policy: Policy) -> None:
    judgement = _judged(_plan_with(policy), _old("Medium"), _finding("High", 11))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1
    assert len(judgement.advisory) == 1


def test_block_mode_never_demotes(policy: Policy) -> None:
    judgement = _judged(_plan_with(policy, "block"), _old("Low"))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


def test_a_do_not_ship_decision_is_never_softened(policy: Policy) -> None:
    from .test_attempts import _v as verdict_data

    data = {**verdict_data("s", [_old("Low")]), "decision": "DO_NOT_SHIP", "defects": []}
    judgement = protocol.judge_planned(validate(data), _plan_with(policy))
    assert judgement.decision == protocol.DO_NOT_SHIP and judgement.advisory == ()


def test_a_known_defect_publishes_in_the_not_blocking_section_and_the_review_ships(
    policy: Policy, tmp_path: Path
) -> None:
    final = _final(policy, tmp_path, _plan_with(policy), _v("s", [_old("Medium"), _old("High", 3)]))
    assert final.state == "success"
    body = final.review["body"]
    assert "**Pre-existing, not blocking**" in body
    record = protocol.decode_marker(body)
    assert [item["id"] for item in record["advisory"]] == ["D1", "D2"]
    assert record["open_defects"] == [] and record["decision"] == "SHIP"


def test_a_critical_pre_existing_defect_blocks_in_the_review(policy: Policy, tmp_path: Path) -> None:
    final = _final(policy, tmp_path, _plan_with(policy), _v("s", [_old("Critical")]))
    assert final.state == "failure"
    assert "advisory" not in protocol.decode_marker(final.review["body"])


def test_the_known_defects_are_recorded_through_the_sink_with_stable_ids(policy: Policy, tmp_path: Path) -> None:
    plan = {**_plan_with(policy), "repo": "o/n", "pr": 7, "head_sha": "a" * 40}
    final = _final(policy, tmp_path, plan, _v("s", [_old("Medium", title="Unchecked ratio"), _old("Low", 3)]))

    class Recorder:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def record(self, entries: Any) -> None:
            self.seen.extend(entries)

    sink = Recorder()
    held = known.publish(plan, final, sink)
    assert held == sink.seen and len(held) == 2
    assert held[0]["id"] == known.defect_id(final.judgement.advisory[0]) and held[0]["id"].startswith("kd-")
    assert held[0]["repo"] == "o/n" and held[0]["pr"] == 7 and held[0]["file"] == CHECKER
    again = known.publish(plan, final, known.NoSink())
    assert [item["id"] for item in again] == [item["id"] for item in held]


def test_the_file_sink_writes_json_only_when_there_is_something_to_record(tmp_path: Path) -> None:
    path = tmp_path / known.KNOWN_FILE
    known.FileSink(path).record([])
    assert not path.exists()
    known.FileSink(path).record([{"id": "kd-1"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": "kd-1"}]


def test_a_promotion_review_or_a_missing_workspace_never_demotes(policy: Policy) -> None:
    assert _judged(_plan_with(policy, promotion={"schema": 1}), _old("Low")).decision == protocol.SHIP_WITH_FIXES
    plan = _plan_with(policy)
    del plan["workspace"]
    assert _judged(plan, _old("Low")).decision == protocol.SHIP_WITH_FIXES


def test_a_finding_in_a_file_that_is_not_in_the_head_tree_never_demotes(policy: Policy, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x\n", encoding="utf-8")
    plan = _plan_with(policy, workspace=str(tmp_path))
    wrong = validate(_v("s", [{**_old("Medium", 2), "file": "x.py"}]))
    assert protocol.judge_planned(wrong, plan, workspace=tmp_path).decision == protocol.SHIP_WITH_FIXES
    right = validate(_v("s", [{**_old("Medium", 2), "file": "src/x.py"}]))
    assert protocol.judge_planned(right, plan, workspace=tmp_path).decision == protocol.SHIP


def test_an_untouched_file_with_the_changed_files_elsewhere_is_known(policy: Policy) -> None:
    plan = _plan_with(policy, added_lines={CHECKER: [[10, 12]]}, changed_paths=[CHECKER])
    finding = {**_old("Medium", 2), "file": "elsewhere/untouched.py"}
    assert _judged(plan, finding).decision == protocol.SHIP


def test_the_policy_accepts_block_and_known_and_names_the_replacement_for_advisory() -> None:
    from .test_policy import _raw

    for mode in ("block", "known"):
        raw = _raw()
        raw["protocol"]["pre_existing"] = mode
        assert parse_policy(raw).protocol.pre_existing == mode
    raw = _raw()
    raw["protocol"]["pre_existing"] = "advisory"
    with pytest.raises(PolicyError, match="replaced by known"):
        parse_policy(raw)


def _citing(policy: Policy, detail: str, **plan_over: Any) -> protocol.Judgement:
    base = {
        "added_lines": {
            ".github/workflows/ci.yml": [[5, 5]],
            "Makefile": [[12, 12]],
            "src/pkg/b.py": [[40, 41]],
            "app/[id]/page.tsx": [[3, 3]],
            "conf/app.properties": [[5, 5]],
            CHECKER: [[10, 12]],
        },
        "changed_paths": [".github/workflows/ci.yml", "Makefile", "src/pkg/b.py", "app/[id]/page.tsx"],
    }
    plan = _plan_with(policy, **{**base, **plan_over})
    return _judged(plan, _old("Medium", 2, detail=detail))


@pytest.mark.parametrize(
    "detail",
    [
        "Triggered by .github/workflows/ci.yml:5",
        "see ./src/pkg/b.py:40",
        "see Makefile:12",
        "see pkg/b.py:40",
        "see b.py line 40",
        "see app/[id]/page.tsx:3",
        "see conf/app.properties:5",
        "see src/pkg/b.py:12, 40",
        "see src/pkg/b.py lines 12 and 41",
        "see SRC/PKG/B.PY#L40",
        "see src\\pkg\\b.py:40",
        "see src/pkg/b.py",
        "see Makefile",
    ],
)
def test_every_spelling_of_a_changed_file_the_finding_cites_blocks(policy: Policy, detail: str) -> None:
    conf = {
        "changed_paths": [
            ".github/workflows/ci.yml",
            "Makefile",
            "src/pkg/b.py",
            "app/[id]/page.tsx",
            "conf/app.properties",
        ]
    }
    judgement = _citing(policy, detail, **conf)
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


@pytest.mark.parametrize(
    "detail",
    ["see src/pkg/b.py:12", "see Makefile:3", "see src/pkg/b.py:20-30", "see util/other.py:40", "see other.py"],
)
def test_a_cited_line_the_change_does_not_add_stays_known(policy: Policy, detail: str) -> None:
    judgement = _citing(policy, detail)
    assert judgement.decision == protocol.SHIP and len(judgement.advisory) == 1


@pytest.mark.parametrize(
    "detail",
    [
        "caller in [new code](https://github.com/o/n/blob/abc/src/pkg/b.py#L40)",
        "see [b](src/pkg/b.py#L40)",
    ],
)
def test_a_markdown_link_citation_blocks(policy: Policy, detail: str) -> None:
    verdict = validate(_v("s", [_old("Medium", 2, detail=detail)]))
    assert "[link removed]" in verdict.defects[0].detail
    assert protocol.judge_planned(verdict, _plan_with(policy)).decision == protocol.SHIP_WITH_FIXES


def test_a_known_defect_of_an_unrecorded_review_is_not_logged(policy: Policy, tmp_path: Path) -> None:
    from dataclasses import replace

    plan = {**_plan_with(policy), "repo": "o/n", "pr": 7, "head_sha": "a" * 40}
    final = _final(policy, tmp_path, plan, _v("s", [_old("Medium")]))
    assert len(known.publish(plan, final, known.NoSink())) == 1
    assert known.publish(plan, replace(final, state="pending"), known.NoSink()) == []


def test_the_id_does_not_change_with_the_spelling_of_the_path() -> None:
    from theseus_reviewer.verdict import Finding

    plain = Finding("Low", "src/é.py", 1, "Title", "d")
    spelled = Finding("Low", "./src/e\u0301.py", 9, "Title", "other")
    assert known.defect_id(plain) == known.defect_id(spelled)
