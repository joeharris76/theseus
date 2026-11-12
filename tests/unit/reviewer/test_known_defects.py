from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import known, protocol
from theseus_reviewer.policy import Policy, PolicyError, parse_policy
from theseus_reviewer.verdict import Finding, validate

from .test_attempts import CHECKER, _finding, _v
from .test_pre_existing_and_intent import _judged, _plan_with
from .test_rounds import _final

pytestmark = [pytest.mark.unit, pytest.mark.fast]

OPERATION = "operation.py"
CITE = "the zero denominator in operation.py:3 raises instead of returning a result"


def _tree(tmp_path: Path, **files: str) -> Path:
    (tmp_path / OPERATION).write_text("values = []\ndef ratio(value):\n    return 1 / value\n", encoding="utf-8")
    (tmp_path / CHECKER).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / CHECKER).write_text("x\n", encoding="utf-8")
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


@pytest.mark.parametrize("severity", ["Medium", "Low"])
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


def test_a_changed_file_whose_full_content_references_the_cited_module_is_a_trigger(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path, caller="import operation\nnothing\n")
    (tmp_path / "caller.py").write_text("import operation as op\nop.ratio(0)\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"caller.py": [[2, 2]]}, changed_paths=["caller.py"])
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES


def test_a_changed_file_that_calls_a_function_the_cited_file_defines_is_a_trigger(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "alias.py").write_text("from somewhere import f as ratio\nratio(0)\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"alias.py": [[2, 2]]}, changed_paths=["alias.py"])
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize(
    "path", ["input.json", "pyproject.toml", "settings.yaml", ".github/workflows/ci.yml", "Makefile"]
)
def test_a_root_level_data_or_config_change_is_a_possible_trigger(policy: Policy, tmp_path: Path, path: str) -> None:
    _tree(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("denominator: 0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={path: [[1, 1]]}, changed_paths=[path])
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES


def test_a_data_file_in_the_cited_files_package_is_a_possible_trigger(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "pkg" / "data").mkdir(parents=True)
    (tmp_path / "pkg" / "op.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "pkg" / "data" / "cases.csv").write_text("0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"pkg/data/cases.csv": [[1, 1]]}, changed_paths=["pkg/data/cases.csv"])
    finding = _old(detail="pkg/op.py:2 divides by zero", file="pkg/op.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
    docs = _plan(policy, tmp_path, added_lines={"docs/notes.md": [[1, 1]]}, changed_paths=["docs/notes.md"])
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("words\n", encoding="utf-8")
    assert _judged(docs, finding).decision == protocol.SHIP


def test_a_data_file_anywhere_in_the_repository_is_a_possible_trigger(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "op.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "cases.csv").write_text("0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"other/cases.csv": [[1, 1]]}, changed_paths=["other/cases.csv"])
    finding = _old(detail="pkg/op.py:2 divides by zero", file="pkg/op.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


def test_a_changed_caller_that_reaches_the_cited_code_through_an_unchanged_module_is_a_trigger(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "c.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("import c\ndef run(v):\n    return c.divide(v)\n", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("import b\nb.run(0)\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"src/a.py": [[1, 2]]}, changed_paths=["src/a.py"])
    finding = _old(detail="src/c.py:2 divides by zero", file="src/c.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
    (tmp_path / "src" / "d.py").write_text("print('hi')\n", encoding="utf-8")
    unrelated = _plan(policy, tmp_path, added_lines={"src/d.py": [[1, 1]]}, changed_paths=["src/d.py"])
    assert _judged(unrelated, finding).decision == protocol.SHIP


def test_a_symlink_to_a_changed_file_is_the_same_cited_file(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to("real.py")
    plan = _plan(policy, tmp_path, added_lines={"real.py": [[1, 1]]}, changed_paths=["real.py"])
    cited = _old(detail="operation.py:3 relies on link.py:1")
    assert _judged(plan, cited).decision == protocol.SHIP_WITH_FIXES
    changed_link = _plan(policy, tmp_path, added_lines={"link.py": [[1, 1]]}, changed_paths=["link.py"])
    assert _judged(changed_link, _old(detail="operation.py:3 relies on real.py:1")).decision == protocol.SHIP_WITH_FIXES


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
    final = _final(policy, tmp_path, _plan(policy, tmp_path), _v("s", [_old("Medium"), _old("Low", title="Other")]))
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


def test_a_changed_path_that_is_gone_from_the_head_tree_is_a_trigger(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    plan = _plan(policy, tmp_path, added_lines={"guard.py": [[1, 1]]}, changed_paths=["guard.py"])
    assert _judged(plan, _old()).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize("path", ["answers.duckdb", "expected.npy", "requirements.in", "Pipfile", "go.sum", "cases.gz"])
def test_any_non_source_extension_in_the_cited_package_is_a_possible_trigger(
    policy: Policy, tmp_path: Path, path: str
) -> None:
    _tree(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "op.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    target = tmp_path / "pkg" / path
    target.write_bytes(b"\x00\x01")
    plan = _plan(policy, tmp_path, added_lines={f"pkg/{path}": [[1, 1]]}, changed_paths=[f"pkg/{path}"])
    finding = _old(detail="pkg/op.py:2 divides by zero", file="pkg/op.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


def test_every_defined_name_is_a_needle_and_an_oversized_cited_file_blocks(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    body = "".join(f"def f{n}():\n    pass\n" for n in range(80))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    (tmp_path / "user.py").write_text("f79()\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"user.py": [[1, 1]]}, changed_paths=["user.py"])
    finding = _old(detail="big.py:1 is wrong", file="big.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
    (tmp_path / "big.py").write_text("x = 1\n" + "#" * 300000, encoding="utf-8")
    (tmp_path / "user.py").write_text("unrelated\n", encoding="utf-8")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


def test_a_changed_file_that_the_cited_file_imports_is_a_possible_trigger(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ratio.py").write_text(
        "from src.helpers import denominator\ndef ratio(v):\n    return v / denominator()\n", encoding="utf-8"
    )
    (tmp_path / "src" / "helpers.py").write_text("def denominator():\n    return 0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"src/helpers.py": [[2, 2]]}, changed_paths=["src/helpers.py"])
    finding = _old(detail="src/ratio.py:3 divides by zero", file="src/ratio.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
    (tmp_path / "src" / "other.py").write_text("x = 1\n", encoding="utf-8")
    unrelated = _plan(policy, tmp_path, added_lines={"src/other.py": [[1, 1]]}, changed_paths=["src/other.py"])
    assert _judged(unrelated, finding).decision == protocol.SHIP


def test_the_dependency_walk_skips_symlinks_that_leave_the_tree_and_special_files(
    policy: Policy, tmp_path: Path
) -> None:
    import os

    _tree(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("operation\n", encoding="utf-8")
    (tmp_path / "evil.py").symlink_to(outside)
    os.mkfifo(tmp_path / "pipe.py")
    (tmp_path / "notes.py").write_text("unrelated = 1\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"notes.py": [[1, 1]]}, changed_paths=["notes.py"])
    assert _judged(plan, _old()).decision == protocol.SHIP


def test_a_changed_transitive_dependency_of_the_cited_code_is_a_possible_trigger(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("import a\ndef check(v):\n    return 1 / a.value()\n", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("import b\ndef value():\n    return b.number()\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def number():\n    return 0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"src/b.py": [[2, 2]]}, changed_paths=["src/b.py"])
    assert _judged(plan, _old(detail="src/x.py:3 divides by zero", file="src/x.py")).decision == (
        protocol.SHIP_WITH_FIXES
    )


def test_a_changed_producer_whose_output_an_unchanged_orchestrator_passes_to_the_cited_code_is_a_trigger(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("def validate(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "src" / "z.py").write_text("import x\nimport y\nx.validate(y.produce())\n", encoding="utf-8")
    (tmp_path / "src" / "y.py").write_text("def produce():\n    return 0\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"src/y.py": [[2, 2]]}, changed_paths=["src/y.py"])
    assert _judged(plan, _old(detail="src/x.py:2 divides by zero", file="src/x.py")).decision == (
        protocol.SHIP_WITH_FIXES
    )


def test_the_related_set_stays_bounded_when_every_module_imports_the_package(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for number in range(60):
        (package / f"m{number}.py").write_text(f"import pkg\nfrom pkg import m{(number + 1) % 60}\n", encoding="utf-8")
    (package / "op.py").write_text("import pkg\ndef divide(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "standalone.py").write_text("import pkg\nprint('x')\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"tools/standalone.py": [[2, 2]]}, changed_paths=["tools/standalone.py"])
    finding = _old(detail="pkg/op.py:3 divides by zero", file="pkg/op.py")
    assert _judged(plan, finding).decision == protocol.SHIP


def test_a_very_common_module_name_is_followed_through_imports_and_unrelated_changes_still_pass(
    policy: Policy, tmp_path: Path
) -> None:
    _tree(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    for number in range(25):
        (tmp_path / "pkg" / f"user{number}.py").write_text("from pkg import utils\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "standalone.py").write_text("print('x')\n", encoding="utf-8")
    finding = _old(detail="pkg/utils.py:2 divides by zero", file="pkg/utils.py")
    unrelated = _plan(
        policy, tmp_path, added_lines={"tools/standalone.py": [[1, 1]]}, changed_paths=["tools/standalone.py"]
    )
    assert _judged(unrelated, finding).decision == protocol.SHIP
    related = _plan(policy, tmp_path, added_lines={"pkg/user3.py": [[1, 1]]}, changed_paths=["pkg/user3.py"])
    assert _judged(related, finding).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize(
    "middle",
    [
        "from lib import helpers, \\\n    parser\nparser.parse(0)\n",
        "import importlib\nP = importlib.import_module('lib.parser')\n",
        "P = __import__('lib.parser')\n",
    ],
)
def test_continuation_and_dynamic_imports_are_followed_to_the_cited_module(
    policy: Policy, tmp_path: Path, middle: str
) -> None:
    _tree(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "parser.py").write_text("def parse(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "lib" / "helpers.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "lib" / "mid.py").write_text(middle + "def go(v):\n    return v\n", encoding="utf-8")
    (tmp_path / "app" / "new.py").write_text("from lib import mid\nmid.go(0)\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"app/new.py": [[1, 2]]}, changed_paths=["app/new.py"])
    finding = _old(detail="lib/parser.py:2 divides by zero", file="lib/parser.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


def test_a_function_defined_in_a_package_init_is_followed_to_the_cited_module(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "parser.py").write_text("def parse(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "lib" / "__init__.py").write_text(
        "from lib import parser\ndef go(v):\n    return parser.parse(v)\n", encoding="utf-8"
    )
    (tmp_path / "app" / "new.py").write_text("import lib\nlib.go(0)\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"app/new.py": [[1, 2]]}, changed_paths=["app/new.py"])
    finding = _old(detail="lib/parser.py:2 divides by zero", file="lib/parser.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize("severity", ["Critical", "High"])
def test_critical_and_high_pre_existing_defects_always_block(policy: Policy, tmp_path: Path, severity: str) -> None:
    _tree(tmp_path)
    judgement = _judged(_plan(policy, tmp_path), _old(severity))
    assert judgement.decision == protocol.SHIP_WITH_FIXES and len(judgement.defects) == 1 and judgement.advisory == ()


def test_a_changed_package_init_runs_before_the_cited_module_and_is_a_trigger(policy: Policy, tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "op.py").write_text("def divide(v):\n    return 1 / v\n", encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text("DEBUG = True\n", encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"pkg/__init__.py": [[1, 1]]}, changed_paths=["pkg/__init__.py"])
    finding = _old(detail="pkg/op.py:2 divides by zero", file="pkg/op.py")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES


@pytest.mark.parametrize(
    ("name", "source", "caller"),
    [
        ("ratio.go", "package stats\nfunc Compute(v int) int {\n\treturn 1 / v\n}\n", "stats.Compute(0)\n"),
        ("ratio.rs", "pub fn compute(v: i32) -> i32 {\n    1 / v\n}\n", "compute(0);\n"),
        ("Ratio.java", "public class Ratio {\n}\n", "new Ratio();\n"),
    ],
)
def test_definitions_in_other_languages_are_needles(
    policy: Policy, tmp_path: Path, name: str, source: str, caller: str
) -> None:
    _tree(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / name).write_text(source, encoding="utf-8")
    (tmp_path / "main.go").write_text(caller, encoding="utf-8")
    plan = _plan(policy, tmp_path, added_lines={"main.go": [[1, 1]]}, changed_paths=["main.go"])
    finding = _old(detail=f"lib/{name}:2 is wrong", file=f"lib/{name}")
    assert _judged(plan, finding).decision == protocol.SHIP_WITH_FIXES
