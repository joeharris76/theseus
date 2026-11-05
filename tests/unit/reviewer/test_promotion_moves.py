from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import cli, coverage, evidence, promotion, protocol, runner
from theseus_reviewer.classifier import ChangedFile
from theseus_reviewer.matchers import load_matchers
from theseus_reviewer.policy import load_policy

from .conftest import POLICY_PATH

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PACKAGE = "src/theseus_reviewer"
BODY = "".join(f"line {number}\n" for number in range(1, 21)).encode()
EDITED = BODY.replace(b"line 20", b"line twenty")
INIT = f"{PACKAGE}/__init__.py"
NEW_HELPER = f"{promotion.STAGED_PROMOTION_DIR}/new/{PACKAGE}/helper.py"
OLD_HELPER = f"{promotion.STAGED_PROMOTION_DIR}/old/{PACKAGE}/helper.py"
SURFACE_KEYS = {"package": "theseus-reviewer", "repository": "owner/theseus", "consumer_workflows": {}}


def _requirements(old: dict[str, bytes], new: dict[str, bytes]) -> tuple[Any, frozenset[str], frozenset[str]]:
    changed, diff_text, _ = promotion.compute_upstream_delta(old, new)
    required, whole = promotion.read_requirements(
        changed,
        old,
        new,
        promotion.executed_surface(new, **SURFACE_KEYS),
        promotion.executed_surface(old, strict=False, **SURFACE_KEYS),
    )
    return (changed, diff_text), required, whole


def _workspace(tmp_path: Path, old: dict[str, bytes], new: dict[str, bytes], diff_text: str) -> Path:
    root = tmp_path / promotion.STAGED_PROMOTION_DIR
    for name, tree in (("old", old), ("new", new)):
        for path, content in tree.items():
            target = root / name / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    (root / promotion.UPSTREAM_DIFF_NAME).write_text(diff_text, encoding="utf-8")
    return tmp_path


def _report(
    workspace: Path, required: frozenset[str], whole: frozenset[str], reads: dict[str, tuple[tuple[int, int], ...]]
) -> coverage.Report:
    plan = {"evidence_files": sorted(required), "evidence_whole_files": sorted(whole)}
    return coverage.evaluate(coverage.targets(plan, workspace), sorted(required), reads, workspace)


MOVE_IN = (
    {INIT: b"x\n", "tests/helper.py": BODY},
    {INIT: b"x\n", f"{PACKAGE}/helper.py": BODY},
    NEW_HELPER,
)
MOVE_IN_EDITED = (
    {INIT: b"x\n", "tests/helper.py": BODY},
    {INIT: b"x\n", f"{PACKAGE}/helper.py": EDITED},
    NEW_HELPER,
)
MOVE_OUT = (
    {INIT: b"x\n", f"{PACKAGE}/helper.py": BODY},
    {INIT: b"x\n", "tests/helper.py": BODY},
    OLD_HELPER,
)
MOVE_OUT_EDITED = (
    {INIT: b"x\n", f"{PACKAGE}/helper.py": BODY},
    {INIT: b"x\n", "tests/helper.py": EDITED},
    OLD_HELPER,
)


@pytest.mark.parametrize("case", [MOVE_IN, MOVE_IN_EDITED, MOVE_OUT, MOVE_OUT_EDITED], ids=lambda c: c[2])
def test_surface_moves_need_the_whole_staged_file_read(case: tuple[Any, ...], tmp_path: Path) -> None:
    old, new, staged = case
    (_, diff_text), required, whole = _requirements(old, new)
    assert required == {staged} == whole
    workspace = _workspace(tmp_path, old, new, diff_text)
    assert not _report(workspace, required, whole, {}).complete
    assert not _report(workspace, required, whole, {staged: ((1, 5),)}).complete
    assert not _report(workspace, required, whole, {coverage.UPSTREAM_DIFF: ((1, 10_000),)}).complete
    assert _report(workspace, required, whole, {staged: ((1, 20),)}).complete
    assert _report(workspace, required, whole, {staged: ((1, 9), (10, 20))}).complete


def test_move_within_the_surface_stays_unrequired() -> None:
    old = {INIT: b"x\n", f"{PACKAGE}/a.py": BODY}
    new = {INIT: b"x\n", f"{PACKAGE}/b.py": BODY}
    _, required, whole = _requirements(old, new)
    assert required == whole == frozenset()


def test_plan_lists_the_staged_old_file_of_a_move_out_and_marks_whole_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, staged = MOVE_OUT_EDITED
    (changed, diff_text), required, whole = _requirements(old, new)
    bundle = promotion.PromotionBundle(
        record=promotion.PromotionRecord(
            head_sha="a" * 40,
            old_source_sha256="1" * 64,
            new_source_sha256="2" * 64,
            old_artifact_sha256="3" * 64,
            new_artifact_sha256="4" * 64,
            delta_sha256="5" * 64,
            lock_sha256="6" * 64,
            bootstrap=False,
        ),
        changed_files=changed,
        diff_text=diff_text,
        old_tree=old,
        new_tree=new,
        required_paths=required,
        whole_paths=whole,
    )
    policy = load_policy(POLICY_PATH)
    matchers = load_matchers(policy.consumer, POLICY_PATH.parents[1])
    plan = cli._base_plan(policy, "owner/repo", 7, "1")
    plan.update({"base_sha": "b" * 40, "base_ref": "develop", "head_sha": "a" * 40})
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(cli.github, "review_threads", lambda *_args: [])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    inputs = cli.RoundInputs(
        files=[ChangedFile(".github/theseus-lock.json", None, 1, 1, status="modified")],
        in_scope={".github/theseus-lock.json"},
        full_diff="",
        states=None,
        found=protocol.History(records=[]),
        previous_strikes=0,
        pool_blocks={},
        manual=False,
        event_name="pull_request_target",
        base_changed_at=None,
        base_change_unreadable=False,
        promotion=bundle,
    )
    assert cli.plan_decision(plan, policy, tmp_path, inputs, matchers) == 0
    assert plan["decision"] == "review"
    assert staged in plan["evidence_files"]
    assert plan["evidence_whole_files"] == [staged]
    assert staged in plan["evidence_protected_files"]
    required = evidence.required_files(
        plan["evidence_files"], tmp_path, [], ["**/*.py", "**/helper.py"], runner._plan_protected(plan)
    )
    assert staged in required


def test_whole_file_hint_names_a_raw_read_when_lines_are_cut(tmp_path: Path) -> None:
    long_body = b"short\n" + b"x" * 900 + b"\nlast\n"
    old = {INIT: b"x\n", "tests/helper.py": long_body}
    new = {INIT: b"x\n", f"{PACKAGE}/helper.py": long_body}
    plain_old = {INIT: b"x\n", "tests/helper.py": BODY}
    plain_new = {INIT: b"x\n", f"{PACKAGE}/helper.py": BODY}
    for before, after, expected in ((old, new, True), (plain_old, plain_new, False)):
        root = tmp_path / str(expected)
        (_, diff_text), required, whole = _requirements(before, after)
        _workspace(root, before, after, diff_text)
        staged_dir = root / promotion.STAGED_PROMOTION_DIR
        hints = coverage.block_hints(sorted(required), "", diff_text, sorted(required), whole, staged_dir)
        if expected:
            assert hints[NEW_HELPER].endswith(f"read with :raw as {NEW_HELPER}:raw:1-3)")
        else:
            assert hints[NEW_HELPER] == "(read the whole file)"
