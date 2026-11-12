from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import known, protocol
from theseus_reviewer.policy import Policy, PolicyError, parse_policy
from theseus_reviewer.verdict import Finding, validate

from .test_attempts import _finding, _v
from .test_pre_existing_and_intent import _judged, _plan_with
from .test_rounds import _final

pytestmark = [pytest.mark.unit, pytest.mark.fast]

OPERATION = "operation.py"
CITE = "the zero denominator in operation.py:3 raises instead of returning a result"


def _tree(tmp_path: Path, **files: str) -> Path:
    (tmp_path / OPERATION).write_text("values = []\ndef ratio(value):\n    return 1 / value\n", encoding="utf-8")
    for name, text in files.items():
        (tmp_path / name.replace("__", "/")).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name.replace("__", "/")).write_text(text, encoding="utf-8")
    return tmp_path


def _plan(policy: Policy, tmp_path: Path, mode: str = "known", **over: Any) -> dict[str, Any]:
    return {**_plan_with(policy, mode, workspace=str(tmp_path)), **over}


def _old(severity: str = "Medium", detail: str = CITE, **over: Any) -> dict[str, Any]:
    return {
        **_finding(severity, 3),
        "file": OPERATION,
        "detail": detail,
        "origin": "pre-existing",
        "title": "Unchecked ratio",
        **over,
    }


def _diff(policy: Policy, tmp_path: Path, name: str, text: str, **over: Any) -> dict[str, Any]:
    _tree(tmp_path, **{name: text})
    lines = len(text.splitlines())
    path = name.replace("__", "/")
    return _plan(policy, tmp_path, added_lines={path: [[1, lines]]}, new_files=[path], changed_paths=[path], **over)


@pytest.mark.parametrize("severity", ["High", "Medium", "Low"])
def test_a_cited_unchanged_pre_existing_defect_is_known_and_does_not_block(
    policy: Policy, tmp_path: Path, severity: str
) -> None:
    _tree(tmp_path)
    judgement = _judged(_plan(policy, tmp_path), _old(severity))
    assert judgement.decision == protocol.SHIP and judgement.defects == ()
    assert [item.severity for item in judgement.advisory] == [severity]


def test_a_critical_pre_existing_defect_always_blocks(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    judgement = _judged(_plan(policy, tmp_path), _old("Critical"))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1 and judgement.advisory == ()


@pytest.mark.parametrize("detail", ["the zero denominator raises", "a result is wrong"])
def test_an_uncited_pre_existing_defect_blocks(policy: Policy, tmp_path: Path, detail: str) -> None:
    _tree(tmp_path)
    judgement = _judged(_plan(policy, tmp_path), _old(detail=detail))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("caller.py", "from operation import ratio\nratio(0)\n"),
        ("pkg__init.py", "import operation\n"),
        ("pkg__use.py", "from . import operation as op\n"),
        ("script.sh", "python operation.py 0\n"),
    ],
)
def test_a_changed_file_that_imports_or_references_the_cited_module_is_a_trigger(
    policy: Policy, tmp_path: Path, name: str, text: str
) -> None:
    judgement = _judged(_diff(policy, tmp_path, name, text), _old())
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


def test_a_changed_file_that_calls_a_cited_symbol_is_a_trigger(policy: Policy, tmp_path: Path) -> None:
    plan = _diff(policy, tmp_path, "driver.py", "total = ratio(0)\n")
    judgement = _judged(plan, _old(detail="`ratio` in operation.py:3 divides without a guard"))
    assert judgement.decision == protocol.SHIP_WITH_FIXES
    called = _old(detail="ratio() in operation.py:3 divides without a guard")
    assert _judged(plan, called).decision == protocol.SHIP_WITH_FIXES


def test_an_unrelated_change_leaves_the_defect_known(policy: Policy, tmp_path: Path) -> None:
    plan = _diff(policy, tmp_path, "notes.md", "unrelated words\n")
    judgement = _judged(plan, _old())
    assert judgement.decision == protocol.SHIP and len(judgement.advisory) == 1


def test_an_unchanged_line_of_a_trigger_file_does_not_count(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path, caller="import operation\nnothing\n")
    (tmp_path / "caller.py").write_text("import operation\nnothing new\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"caller.py": [[2, 2]]}, changed_paths=["caller.py"])
    assert _judged(plan, _old()).decision == protocol.SHIP


def test_a_change_to_a_cited_file_blocks(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = _plan(policy, tmp_path, added_lines={OPERATION: [[1, 1]]}, changed_paths=[OPERATION])
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES
    other = _tree(tmp_path, helper="x = 1\n")
    cited = _plan(policy, other, added_lines={"helper.py": [[1, 1]]}, changed_paths=["helper.py"])
    detail = "operation.py:3 relies on helper.py:1 being sorted"
    assert _judged(cited, _old(detail=detail)).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize("detail", ["operation.py", "see operation.py:3", "operation.py lines 2-3", "operation.py#L3"])
def test_any_citation_form_of_the_own_file_counts_as_a_citation(policy: Policy, tmp_path: Path, detail: str) -> None:
    _tree(tmp_path)
    assert _judged(_plan(policy, tmp_path), _old(detail=detail)).decision == protocol.SHIP


def test_a_url_or_redaction_in_the_finding_blocks(policy: Policy, tmp_path: Path) -> None:
    from theseus_reviewer.verdict import REDACTED

    _tree(tmp_path)
    for text in ("see https://example.com/o/n/blob/abc/operation.py#L3", f"operation.py:3 token {REDACTED}"):
        verdict = validate(_v("s", [_old(detail=text)]))
        assert protocol.judge_planned(verdict, _plan(policy, tmp_path)).decision == protocol.SHIP_WITH_FIXES


def test_without_a_workspace_nothing_is_known(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = _plan(policy, tmp_path)
    del plan["workspace"]
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES


def test_an_introduced_or_unmarked_finding_blocks(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    for over in ({"origin": "introduced"},):
        assert _judged(_plan(policy, tmp_path), _old(**over)).decision == protocol.SHIP_WITH_FIXES


def test_at_most_five_known_defects_per_review(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    five = [_old("Low", title=f"issue {n}") for n in range(5)]
    assert len(_judged(_plan(policy, tmp_path), *five).advisory) == 5
    six = _judged(_plan(policy, tmp_path), *[*five, _old("Low", title="issue 5")])
    assert six.advisory == () and six.decision == protocol.SHIP_WITH_FIXES and len(six.defects) == 6


def test_known_defects_do_not_count_toward_the_defect_limit(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = {**_plan(policy, tmp_path), "max_defects": 1}
    judgement = _judged(plan, _old(), _old("Low", title="other"), {**_finding("Low", 11), "origin": "introduced"})
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1
    assert len(judgement.advisory) == 2


def test_block_mode_never_demotes(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    judgement = _judged(_plan(policy, tmp_path, "block"), _old("Low"))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and judgement.advisory == ()


def test_a_do_not_ship_decision_is_never_softened(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    data = {**_v("s", [_old("Low")]), "decision": "DO_NOT_SHIP", "defects": []}
    judgement = protocol.judge_planned(validate(data), _plan(policy, tmp_path))
    assert judgement.decision == protocol.DO_NOT_SHIP and judgement.advisory == ()


def test_a_promotion_review_never_demotes(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = _plan(policy, tmp_path, promotion={"schema": 1})
    assert _judged(plan, _old("Low")).decision == protocol.SHIP_WITH_FIXES


def test_a_finding_whose_file_is_not_in_the_head_tree_never_demotes(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    verdict = validate(_v("s", [_old(file="missing.py", detail="missing.py:3 raises")]))
    assert protocol.judge_planned(verdict, _plan(policy, tmp_path)).decision == protocol.SHIP_WITH_FIXES


def test_a_known_defect_publishes_in_the_not_blocking_section_and_the_review_ships(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    final = _final(policy, tmp_path, _plan(policy, tmp_path), _v("s", [_old("Medium"), _old("High", title="Other")]))
    assert final.state == "success"
    assert "**Pre-existing, not blocking**" in final.review["body"]
    record = protocol.decode_marker(final.review["body"])
    assert [item["id"] for item in record["advisory"]] == ["D1", "D2"]
    assert record["open_defects"] == [] and record["decision"] == "SHIP"


def test_a_critical_pre_existing_defect_blocks_in_the_review(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    final = _final(policy, tmp_path, _plan(policy, tmp_path), _v("s", [_old("Critical")]))
    assert final.state == "failure"
    assert "advisory" not in protocol.decode_marker(final.review["body"])


def test_the_known_defects_are_recorded_through_the_sink_with_stable_ids(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = {**_plan(policy, tmp_path), "repo": "o/n", "pr": 7, "head_sha": "a" * 40}
    final = _final(policy, tmp_path, plan, _v("s", [_old("Medium"), _old("Low", title="Second")]))

    class Recorder:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def record(self, entries: Any) -> None:
            self.seen.extend(entries)

    sink = Recorder()
    held = known.publish(plan, final, sink)
    assert held == sink.seen and len(held) == 2
    assert held[0]["id"] == known.defect_id(final.judgement.advisory[0]) and held[0]["id"].startswith("kd-")
    assert held[0]["repo"] == "o/n" and held[0]["pr"] == 7 and held[0]["file"] == OPERATION
    assert [item["id"] for item in known.publish(plan, final, known.NoSink())] == [item["id"] for item in held]


def test_a_known_defect_of_an_unrecorded_review_is_not_logged(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = {**_plan(policy, tmp_path), "repo": "o/n", "pr": 7, "head_sha": "a" * 40}
    final = _final(policy, tmp_path, plan, _v("s", [_old("Medium")]))
    assert len(known.publish(plan, final, known.NoSink())) == 1
    assert known.publish(plan, replace(final, state="pending"), known.NoSink()) == []


def test_the_file_sink_writes_json_only_when_there_is_something_to_record(tmp_path: Path) -> None:
    path = tmp_path / known.KNOWN_FILE
    known.FileSink(path).record([])
    assert not path.exists()
    known.FileSink(path).record([{"id": "kd-1"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": "kd-1"}]


def test_the_id_does_not_change_with_the_spelling_of_the_path() -> None:
    plain = Finding("Low", "src/é.py", 1, "Title", "d")
    spelled = Finding("Low", "./src/e\u0301.py", 9, "Title", "other")
    assert known.defect_id(plain) == known.defect_id(spelled)


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


def test_a_cited_unchanged_file_other_than_the_own_file_is_searched_for_triggers(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "parser.py").write_text("def parse(x):\n    return 1 / x\n", encoding="utf-8")
    (tmp_path / "app" / "report.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "new.py").write_text("from lib import parser\nparser.parse(0)\n", encoding="utf-8")
    plan = _plan(
        policy, tmp_path, added_lines={"app/new.py": [[1, 2]]}, new_files=["app/new.py"], changed_paths=["app/new.py"]
    )
    detail = "report.py:1 uses lib/parser.py:2 which divides by zero"
    finding = _old(detail=detail, file="app/report.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
    quiet = _plan(policy, tmp_path)
    assert _judged(quiet, finding).decision == protocol.SHIP


@pytest.mark.parametrize("detail", ["HTTP 500: 3 retries are wasted", "wasted (3) retries", "ratio: 3"])
def test_incidental_numbers_are_not_a_citation(policy: Policy, tmp_path: Path, detail: str) -> None:
    _tree(tmp_path)
    assert _judged(_plan(policy, tmp_path), _old(detail=detail)).decision == protocol.SHIP_WITH_FIXES


def test_a_mixed_case_cited_module_is_searched_with_its_case(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "Parser.py").write_text("def parse(x):\n    return 1 / x\n", encoding="utf-8")
    (tmp_path / "app" / "report.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "new.py").write_text("from lib import Parser\nParser.parse(0)\n", encoding="utf-8")
    plan = _plan(
        policy, tmp_path, added_lines={"app/new.py": [[1, 2]]}, new_files=["app/new.py"], changed_paths=["app/new.py"]
    )
    finding = _old(detail="report.py:1 uses lib/Parser.py:2 which divides by zero", file="app/report.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
