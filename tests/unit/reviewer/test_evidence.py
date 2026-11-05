from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import coverage, promotion
from theseus_reviewer.coverage import Target
from theseus_reviewer.diff import STAGED_DIFF_NAME, hunk_index
from theseus_reviewer.diff import binary_files as diff_binary_files
from theseus_reviewer.evidence import (
    CITATION_PREFIX,
    INLINE,
    UNVERIFIED_WITHDRAWAL,
    Gap,
    Trace,
    compacted,
    describe,
    glob_match,
    mark_citations,
    relative,
    returned,
    trace,
)
from theseus_reviewer.evidence import (
    check as _check,
)
from theseus_reviewer.matchers import load_matchers
from theseus_reviewer.policy import load_policy
from theseus_reviewer.verdict import Finding, PriorDefect, Verdict

from .conftest import POLICY_PATH

pytestmark = [pytest.mark.unit, pytest.mark.fast]

CONSUMER = load_policy(POLICY_PATH).consumer
MATCHERS = load_matchers(CONSUMER, POLICY_PATH.parents[1])
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "oracle_reviewers"
SHIP_WITH_FIXES = "SHIP_WITH_FIXES"


def check(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("is_protected", MATCHERS.is_protected)
    kwargs.setdefault("clears", True)
    kwargs.setdefault("targets", {})
    return _check(*args, **kwargs)


OMP_STREAM = (FIXTURES / "omp_multi_turn_review.jsonl").read_text(encoding="utf-8")


def _read_details(
    first: int = 1,
    last: int = 5,
    *,
    total: int | None = None,
    truncated: bool = False,
    stray: tuple[int | None, ...] = (),
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "totalLines": total or last,
        "rowPrefixes": False,
        "displayContent": {"text": "x", "startLine": first, "lineNumbers": [*range(first, last + 1), *stray]},
    }
    if truncated:
        details["truncation"] = {
            "truncated": True,
            "truncatedBy": "lines",
            "totalLines": total or last,
            "outputLines": last - first + 1,
            "lastLinePartial": False,
            "firstLineExceedsLimit": False,
        }
        details["meta"] = {"truncation": {"shownRange": {"start": first, "end": last}}}
    return details


def _events(name: str, path: str, details: dict[str, Any], *, is_error: bool = False, output: str = "match") -> str:
    call = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": name, "arguments": {"path": path}}],
        },
    }
    result = {
        "type": "message_end",
        "message": {
            "role": "toolResult",
            "toolCallId": "c1",
            "toolName": name,
            "isError": is_error,
            "content": [{"type": "text", "text": output}],
            "details": details,
        },
    }
    return f"{json.dumps(call)}\n{json.dumps(result)}\n"


def _tool_events(
    name: str,
    path: str,
    *,
    is_error: bool,
    output: str = "match",
    call_id: str = "c1",
    lines: tuple[int, int] = (1, 5),
    total: int | None = None,
    truncated: bool = False,
) -> str:
    details = (
        _read_details(*lines, total=total, truncated=truncated)
        if name == "read" and not is_error
        else {"files": ["pkg/unrelated.py"]}
    )
    text = _events(name, path, details, is_error=is_error, output=output)
    return text.replace('"c1"', json.dumps(call_id))


def _fixture_details(name: str) -> dict[str, Any]:
    for line in (FIXTURES / "omp_candidate" / name).read_text(encoding="utf-8").splitlines():
        message = json.loads(line).get("message", {})
        details = message.get("details")
        if message.get("toolName") == "read" and isinstance(details, dict) and details.get("truncation"):
            return details
    raise AssertionError(name)


def _spans(*items: tuple[str, tuple[int, int]]) -> Trace:
    return Trace(spans=tuple((path, (span,)) for path, span in items))


def _returned_lines(*paths: str) -> dict[str, tuple[tuple[int, int], ...]]:
    return {path: ((1, 9),) for path in paths}


def _put(workspace: Path, name: str, text: str = "x = 1\n") -> Path:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _listed(path: str, *hunks: Any, diff: str | None = None, removed: bool = False) -> Target:
    return Target(path, diff, tuple(hunks), True, removed)


def _report(workspace: Path, required: list[str], run: Trace, plan: dict[str, Any] | None = None) -> coverage.Report:
    found = coverage.targets({"evidence_files": required, **(plan or {})}, workspace)
    return coverage.evaluate(found, required, returned(run, workspace), workspace)


def _verdict(examined: tuple[str, ...] = (), defects: tuple[Finding, ...] = ()) -> Verdict:
    return Verdict(
        status="complete",
        incomplete_reason="",
        decision="SHIP",
        summary="summary",
        files_examined=examined,
        defects=defects,
    )


def _finding(file: str = "pkg/a.py", line: int = 2, end_line: int | None = None) -> Finding:
    return Finding(
        severity="High",
        file=file,
        line=line,
        title="title",
        detail="detail",
        end_line=end_line,
    )


MODIFIED_DIFF = (
    "diff --git a/pkg/a.py b/pkg/a.py\n"
    "index 111..222 100644\n"
    "--- a/pkg/a.py\n"
    "+++ b/pkg/a.py\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    " three\n"
)
ADDITIVE_DIFF = (
    "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -2,2 +2,4 @@\n two\n+new1\n+new2\n three\n"
)
MODIFIED_BLOCK = (5, 9)
ADDITIVE_BLOCK = (4, 8)


def _added_diff(path: str, count: int) -> str:
    body = "".join(f"+line {number}\n" for number in range(1, count + 1))
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{count} @@\n{body}"


def _staged(workspace: Path, diff: str) -> None:
    _put(workspace, STAGED_DIFF_NAME, diff)


def test_omp_trace_reads_the_recorded_review_stream() -> None:
    checker = "/workspace/benchbox/core/equivalence/checker.py"
    run = trace("omp", OMP_STREAM)
    assert run.reads == (checker,)
    assert run.greps == ("/workspace",)
    assert run.spans == ((checker, ((1, 2),)),)


@pytest.mark.parametrize(
    ("name", "is_error", "output", "reads", "greps"),
    [
        ("read", False, "line", ("pkg/a.py:10-40",), ()),
        ("grep", False, "pkg/a.py\n*12|def f", (), ("pkg/a.py:10-40",)),
        ("grep", False, "No matches found", (), ()),
        ("read", True, "Path not found", (), ()),
        ("glob", False, "pkg/a.py", (), ()),
    ],
)
def test_omp_trace_counts_only_reads_that_returned_content(
    name: str, is_error: bool, output: str, reads: tuple[str, ...], greps: tuple[str, ...]
) -> None:
    events = _tool_events(name, "pkg/a.py:10-40", is_error=is_error, output=output)
    run = trace("omp", events)
    assert (run.reads, run.greps) == (reads, greps)


def test_a_read_records_its_returned_lines_in_the_ledger() -> None:
    run = trace("omp", _tool_events("read", "pkg/a.py:10-40", is_error=False, lines=(10, 40), total=90, truncated=True))
    assert run.spans == (("pkg/a.py:10-40", ((10, 40),)),)
    assert run.ledger == (("pkg/a.py:10-40", ((10, 40),), True, False),)


def test_bare_read_covers_only_the_returned_300_lines(tmp_path: Path) -> None:
    long_module = "".join(f"line {number}\n" for number in range(1, 361))
    _put(tmp_path, "long_module.py", long_module)
    details = _fixture_details("chain_review.jsonl")
    assert details["displayContent"]["lineNumbers"][-2:] == [None, 305]
    run = trace("omp", _events("read", "long_module.py", details))
    assert returned(run, tmp_path) == {"long_module.py": ((1, 300),)}
    _staged(tmp_path, _added_diff("long_module.py", 360))
    assert _report(tmp_path, ["long_module.py"], run).uncovered != ()
    assert coverage.contains(returned(run, tmp_path)["long_module.py"], 300, 300)
    assert not coverage.contains(returned(run, tmp_path)["long_module.py"], 305, 305)
    assert not coverage.contains(returned(run, tmp_path)["long_module.py"], 301, 301)


def test_a_ranged_read_covers_the_lines_omp_reports_not_the_requested_ones(tmp_path: Path) -> None:
    _put(tmp_path, "long_module.py", "".join(f"line {number}\n" for number in range(1, 361)))
    run = trace("omp", _events("read", "long_module.py:5-9", _read_details(4, 12, total=360, truncated=True)))
    assert returned(run, tmp_path) == {"long_module.py": ((4, 12),)}
    raw = trace("omp", _events("read", "long_module.py:5-9:raw", _read_details(5, 9, total=360)))
    assert returned(raw, tmp_path) == {"long_module.py": ((5, 9),)}


def test_last_line_partial_read_drops_the_partial_line(tmp_path: Path) -> None:
    _put(tmp_path, "wide.py", "x\n" * 20)
    details = _read_details(1, 10, total=20, truncated=True)
    details["truncation"]["lastLinePartial"] = True
    run = trace("omp", _events("read", "wide.py", details))
    assert returned(run, tmp_path) == {"wide.py": ((1, 9),)}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda details: details["truncation"].update(firstLineExceedsLimit=True),
        lambda details: details["meta"]["truncation"].update(shownRange={"start": 1}),
        lambda details: details.pop("displayContent"),
        lambda details: details["displayContent"].update(lineNumbers="1-10"),
    ],
)
def test_a_read_whose_returned_lines_cannot_be_determined_credits_nothing(tmp_path: Path, mutate: Any) -> None:
    _put(tmp_path, "wide.py", "x\n" * 20)
    details = _read_details(1, 10, total=20, truncated=True)
    mutate(details)
    assert returned(trace("omp", _events("read", "wide.py", details)), tmp_path) == {}


def test_returned_lines_are_clamped_to_the_file_length(tmp_path: Path) -> None:
    _put(tmp_path, "short.py", "x\n" * 3)
    run = trace("omp", _events("read", "short.py", _read_details(1, 5, total=3)))
    assert returned(run, tmp_path) == {"short.py": ((1, 3),)}


def test_multi_path_read_credits_no_lines(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py")
    _put(tmp_path, "pkg/b.py")
    notes = {"notes": ["Note: interpreted as 2 paths"], "displayReadTargets": ["pkg/a.py", "pkg/b.py:1-3"]}
    without_lines = trace("omp", _events("read", "pkg/a.py;pkg/b.py:1-3", notes))
    assert without_lines.reads == ("pkg/a.py;pkg/b.py:1-3",)
    assert without_lines.spans == ()
    with_lines = trace("omp", _events("read", "pkg/a.py;pkg/b.py", _read_details(1, 1)))
    assert returned(without_lines, tmp_path) == returned(with_lines, tmp_path) == {}


@pytest.mark.parametrize("path", ["pkg/a.py;pkg/b.py", "pkg/a.py", "pkg", "{root}/pkg/a.py", "pkg/a.py:1-20"])
def test_grep_never_credits_a_line(tmp_path: Path, path: str) -> None:
    _put(tmp_path, "pkg/a.py", "x = 1\n")
    _put(tmp_path, "pkg/b.py", "x = 1\n")
    run = trace("omp", _tool_events("grep", path.format(root=tmp_path), is_error=False, output="pkg/a.py\n*1|x"))
    assert run.spans == ()
    assert returned(run, tmp_path) == {}


def test_grep_never_covers_a_hunk(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\ntwo\nnew1\nnew2\nthree\n")
    _staged(tmp_path, ADDITIVE_DIFF)
    run = trace("omp", _tool_events("grep", "pkg/a.py", is_error=False, output="pkg/a.py\n*3|new1"))
    report = _report(tmp_path, ["pkg/a.py"], run)
    assert not report.complete
    assert report.covered == 0 and report.total == 1
    problem = check(
        _verdict(examined=("pkg/a.py",)),
        harness="omp",
        brief_mode="file-list",
        workspace=tmp_path,
        required=["pkg/a.py"],
        run=run,
        targets=coverage.targets({"evidence_files": ["pkg/a.py"]}, tmp_path),
    )
    assert problem is not None
    assert problem.files == ("pkg/a.py",)
    assert "1 of 1 changed hunks" in problem.reason


def test_a_whole_file_grep_over_an_unlisted_file_is_still_an_unread_file(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py")
    run = trace("omp", _tool_events("grep", "pkg/a.py", is_error=False, output="pkg/a.py\n*1|x"))
    report = _report(tmp_path, ["pkg/a.py"], run)
    assert report.untouched == ("pkg/a.py",)


def test_single_file_selectors_credit_the_exact_file_and_path_lists_credit_nothing(tmp_path: Path) -> None:
    for name in ("pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py", "pkg/f.py"):
        _put(tmp_path, name)
    run = Trace(
        spans=(
            ("pkg/a.py:10-40", ((10, 40),)),
            ("pkg/b.py:raw", ((1, 5),)),
            ("pkg/c.py:5+20:raw", ((5, 24),)),
            (f"{tmp_path}/pkg/e.py; pkg/f.py:1-9", ((1, 9),)),
        )
    )
    assert returned(run, tmp_path) == {
        "pkg/a.py": ((10, 40),),
        "pkg/b.py": ((1, 5),),
        "pkg/c.py": ((5, 24),),
    }


def test_returned_lines_of_repeated_reads_are_joined(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "x\n" * 50)
    run = Trace(
        spans=(("pkg/a.py:1-10", ((1, 10),)), ("pkg/a.py:11-20", ((11, 20),)), ("./pkg/a.py:30-40", ((30, 40),)))
    )
    assert returned(run, tmp_path) == {"pkg/a.py": ((1, 20), (30, 40))}


def test_untraced_harnesses_have_no_trace() -> None:
    assert trace("text", OMP_STREAM) == Trace()
    assert not compacted("text", OMP_STREAM)


def test_untraced_harness_skips_coverage(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\ntwo\nTWO\nthree\n")
    _staged(tmp_path, MODIFIED_DIFF)
    found = coverage.targets({"evidence_files": ["pkg/a.py"]}, tmp_path)
    assert (
        check(
            _verdict(examined=("pkg/a.py",)),
            harness="text",
            brief_mode="file-list",
            workspace=tmp_path,
            required=["pkg/a.py"],
            run=Trace(),
            targets=found,
        )
        is None
    )
    unexamined = check(
        _verdict(),
        harness="text",
        brief_mode="file-list",
        workspace=tmp_path,
        required=["pkg/a.py"],
        run=Trace(),
        targets=found,
    )
    assert unexamined == Gap("the verdict would ship but did not examine pkg/a.py", ("pkg/a.py",))


def test_relative_maps_workspace_paths_and_leaves_outside_paths_absolute(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    assert relative("pkg/a.py", workspace) == "pkg/a.py"
    assert relative("./pkg/a.py", workspace) == "pkg/a.py"
    assert relative(str(workspace.resolve() / "pkg" / "a.py"), workspace) == "pkg/a.py"
    assert relative("/etc/passwd", workspace) == "/etc/passwd"
    assert relative("pkg/a.py:12", workspace) == "pkg/a.py"
    assert relative("pkg/a.py:12-40", workspace) == "pkg/a.py"
    assert relative(str(workspace.resolve() / "pkg" / "a.py") + ":3", workspace) == "pkg/a.py"
    assert relative("../ws2/a.py", workspace).endswith("ws2/a.py")
    assert not relative("../ws2/a.py", workspace).startswith("ws2")


def test_mark_citations_leaves_valid_citation_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    marked = mark_citations(_verdict(defects=(_finding(),)), workspace)
    assert marked.defects[0].detail == "detail"


def test_mark_citations_flags_line_beyond_end_of_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\n")
    marked = mark_citations(_verdict(defects=(_finding(line=9),)), workspace)
    assert marked.defects[0].detail.startswith(f"{CITATION_PREFIX}: pkg/a.py has 2 lines")
    assert marked.defects[0].detail.endswith("detail")


def test_mark_citations_flags_end_line_beyond_end_of_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\n")
    marked = mark_citations(_verdict(defects=(_finding(line=1, end_line=7),)), workspace)
    assert "line 7 does not exist" in marked.defects[0].detail


def test_mark_citations_flags_path_missing_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marked = mark_citations(_verdict(defects=(_finding(file="pkg/missing.py"),)), workspace)
    assert marked.defects[0].detail.startswith(f"{CITATION_PREFIX}: pkg/missing.py is not a file")


def test_check_passes_when_examined_covers_required(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    verdict = _verdict(examined=("pkg/a.py",))
    assert (
        check(
            verdict,
            harness="omp",
            brief_mode="summary",
            workspace=workspace,
            required=["pkg/a.py"],
            run=Trace(),
            targets={"pkg/a.py": _listed("pkg/a.py")},
        )
        is None
    )


def test_check_reports_examined_gap_without_trace_gate(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    verdict = _verdict(examined=("pkg/other.py",))
    problem = check(
        verdict,
        harness="text",
        brief_mode="summary",
        workspace=workspace,
        required=["pkg/a.py"],
        run=Trace(),
    )
    assert problem == Gap("the verdict would ship but did not examine pkg/a.py", ("pkg/a.py",))


@pytest.mark.parametrize(
    ("seen", "named"),
    [
        ((), ("pkg/a.py", "pkg/b.py")),
        (("pkg/a.py",), ("pkg/b.py",)),
        (("pkg/a.py", "pkg/b.py"), ()),
    ],
)
def test_check_requires_returned_lines_for_every_required_file_outside_the_diff(
    tmp_path: Path, seen: tuple[str, ...], named: tuple[str, ...]
) -> None:
    for name in ("pkg/a.py", "pkg/b.py"):
        _put(tmp_path, name)
    found = check(
        _verdict(examined=("pkg/a.py", "pkg/b.py", "pkg/anchor.py")),
        harness="omp",
        brief_mode="summary",
        workspace=tmp_path,
        required=["pkg/a.py", "pkg/b.py", "pkg/anchor.py"],
        run=Trace(spans=tuple((name, ((1, 1),)) for name in seen)),
        targets={"pkg/anchor.py": _listed("pkg/anchor.py")},
    )
    if not named:
        assert found is None
        return
    assert found is not None
    assert found.files == named
    assert f"no read of {', '.join(named)}" in found.reason


def test_modified_hunk_needs_its_diff_block(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\nTWO\nthree\n")
    _staged(tmp_path, MODIFIED_DIFF)
    whole_file = _spans(("pkg/a.py", (1, 3)))
    report = _report(tmp_path, ["pkg/a.py"], whole_file)
    assert (report.total, report.covered) == (1, 0)
    assert [(item.source, item.start, item.end) for item in report.uncovered] == [(STAGED_DIFF_NAME, *MODIFIED_BLOCK)]
    partial_block = _spans((STAGED_DIFF_NAME, (MODIFIED_BLOCK[0], MODIFIED_BLOCK[1] - 1)))
    assert not _report(tmp_path, ["pkg/a.py"], partial_block).complete
    whole_block = _spans((STAGED_DIFF_NAME, MODIFIED_BLOCK))
    report = _report(tmp_path, ["pkg/a.py"], whole_block)
    assert report.complete and report.covered == 1
    joined = Trace(spans=((STAGED_DIFF_NAME, ((1, 6),)), (STAGED_DIFF_NAME, ((7, 9),))))
    assert _report(tmp_path, ["pkg/a.py"], joined).complete


def test_added_hunk_is_covered_by_reading_its_lines_in_the_file(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\ntwo\nnew1\nnew2\nthree\n")
    _staged(tmp_path, ADDITIVE_DIFF)
    hunk = hunk_index(ADDITIVE_DIFF)["pkg/a.py"][0]
    assert (hunk.additive, hunk.new_start, hunk.new_end) == (True, 2, 5)
    assert not _report(tmp_path, ["pkg/a.py"], _spans(("pkg/a.py", (2, 4)))).complete
    assert _report(tmp_path, ["pkg/a.py"], _spans(("pkg/a.py", (2, 5)))).complete
    assert _report(tmp_path, ["pkg/a.py"], _spans(("pkg/a.py", (1, 5)))).complete
    assert _report(tmp_path, ["pkg/a.py"], _spans((STAGED_DIFF_NAME, ADDITIVE_BLOCK))).complete


def test_added_file_is_covered_by_full_ranged_reads(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/big.py", "".join(f"line {number}\n" for number in range(1, 401)))
    _staged(tmp_path, _added_diff("pkg/big.py", 400))
    first = trace("omp", _tool_events("read", "pkg/big.py", is_error=False, lines=(1, 300), total=400, truncated=True))
    second = trace(
        "omp", _tool_events("read", "pkg/big.py:301-400", is_error=False, lines=(301, 400), total=400, truncated=False)
    )
    assert not _report(tmp_path, ["pkg/big.py"], first).complete
    both = Trace(spans=first.spans + second.spans)
    report = _report(tmp_path, ["pkg/big.py"], both)
    assert report.complete and (report.total, report.covered) == (1, 1)


def test_rename_without_hunks_needs_no_read(tmp_path: Path) -> None:
    rename = (
        "diff --git a/pkg/old.py b/pkg/new.py\nsimilarity index 100%\nrename from pkg/old.py\nrename to pkg/new.py\n"
    )
    _staged(tmp_path, rename)
    _put(tmp_path, "pkg/new.py")
    report = _report(tmp_path, ["pkg/new.py"], Trace())
    assert report.complete and report.total == 0
    assert (
        check(
            _verdict(examined=("pkg/new.py",)),
            harness="omp",
            brief_mode="file-list",
            workspace=tmp_path,
            required=["pkg/new.py"],
            run=Trace(),
            targets=coverage.targets({"evidence_files": ["pkg/new.py"]}, tmp_path),
        )
        is None
    )


def test_removed_promotion_file_is_covered_by_whole_old_read_or_its_diff_block(tmp_path: Path) -> None:
    directory = promotion.STAGED_PROMOTION_DIR
    upstream = f"{directory}/{promotion.UPSTREAM_DIFF_NAME}"
    old = f"{directory}/old/pkg/gone.py"
    _put(tmp_path, old, "a\nb\nc\n")
    diff = "diff --git a/pkg/gone.py b/pkg/gone.py\ndeleted file mode 100644\n--- a/pkg/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-a\n-b\n-c\n"
    _put(tmp_path, upstream, diff)
    assert not _report(tmp_path, [old], Trace()).complete
    assert not _report(tmp_path, [old], _spans((old, (1, 2)))).complete
    assert _report(tmp_path, [old], _spans((old, (1, 3)))).complete
    assert _report(tmp_path, [old], _spans((upstream, (5, 8)))).complete
    assert not _report(tmp_path, [old], _spans((upstream, (5, 7)))).complete


def test_check_requires_coverage_in_inline_mode_only_for_listing(tmp_path: Path) -> None:
    listed = {"pkg/a.py": _listed("pkg/a.py", *hunk_index(MODIFIED_DIFF)["pkg/a.py"])}
    assert (
        check(
            _verdict(),
            harness="omp",
            brief_mode=INLINE,
            workspace=tmp_path,
            required=["pkg/a.py"],
            run=Trace(),
            targets=listed,
        )
        is None
    )
    omitted = check(
        _verdict(),
        harness="omp",
        brief_mode=INLINE,
        workspace=tmp_path,
        required=["pkg/a.py", "pkg/b.py"],
        run=Trace(),
        targets=listed,
    )
    assert omitted == Gap("the verdict would ship but the brief omits the diff of pkg/b.py", ("pkg/b.py",))
    unlisted = check(
        _verdict(),
        harness="omp",
        brief_mode=INLINE,
        workspace=tmp_path,
        required=["pkg/a.py"],
        run=Trace(),
        targets={"pkg/a.py": Target("pkg/a.py", None, (), False)},
    )
    assert unlisted is not None and unlisted.files == ("pkg/a.py",)


def test_check_non_clearing_verdict_has_no_requirement(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for mode in ("summary", INLINE):
        assert (
            check(
                _verdict(examined=()),
                clears=False,
                harness="omp",
                brief_mode=mode,
                workspace=workspace,
                required=["pkg/a.py"],
                run=Trace(),
            )
            is None
        )


def test_swf_without_coverage_is_incomplete(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\nTWO\nthree\n")
    _staged(tmp_path, MODIFIED_DIFF)
    arguments: dict[str, Any] = {
        "harness": "omp",
        "brief_mode": "file-list",
        "workspace": tmp_path,
        "required": ["pkg/a.py"],
        "targets": coverage.targets({"evidence_files": ["pkg/a.py"]}, tmp_path),
    }
    verdict = replace(_verdict(examined=("pkg/a.py",)), decision=SHIP_WITH_FIXES)
    shallow = check(verdict, run=_spans(("pkg/a.py", (1, 3))), **arguments)
    assert shallow is not None
    assert shallow.files == ("pkg/a.py",)
    assert shallow.reason.startswith("the verdict would ship but 1 of 1 changed hunks")
    assert check(verdict, run=_spans((STAGED_DIFF_NAME, MODIFIED_BLOCK)), **arguments) is None
    assert check(verdict, clears=False, run=Trace(), **arguments) is None


def test_check_matches_examined_after_relative_normalization(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    verdict = _verdict(examined=("pkg/a.py:12-40",))
    assert (
        check(
            verdict,
            harness="omp",
            brief_mode="summary",
            workspace=workspace,
            required=["pkg/a.py"],
            run=Trace(),
            targets={"pkg/a.py": _listed("pkg/a.py")},
        )
        is None
    )


def test_check_truncates_long_missing_lists(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    verdict = _verdict(examined=())
    required = [f"pkg/{name}.py" for name in ("a", "b", "c", "d", "e", "f", "g")]
    problem = check(
        verdict,
        harness="omp",
        brief_mode="summary",
        workspace=workspace,
        required=required,
        run=Trace(),
        targets={path: _listed(path) for path in required},
    )
    assert problem is not None
    assert problem.reason.endswith("and 2 more")
    assert "pkg/g.py" not in problem.reason
    assert problem.files == tuple(required)


def test_a_clearing_verdict_with_no_staged_diff_listing_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py")
    found = coverage.targets({"evidence_files": ["pkg/a.py"]}, tmp_path)
    arguments: dict[str, Any] = {
        "harness": "omp",
        "brief_mode": "file-list",
        "workspace": tmp_path,
        "required": ["pkg/a.py"],
        "run": _spans(("pkg/a.py", (1, 1))),
        "targets": found,
    }
    problem = check(_verdict(examined=("pkg/a.py",)), **arguments)
    assert problem == Gap(
        "the verdict would ship but the staged diff lists none of the required files: pkg/a.py", ("pkg/a.py",)
    )
    assert check(_verdict(), clears=False, **arguments) is None
    _staged(tmp_path, _added_diff("pkg/a.py", 1))
    arguments["targets"] = coverage.targets({"evidence_files": ["pkg/a.py"]}, tmp_path)
    assert check(_verdict(examined=("pkg/a.py",)), **arguments) is None


def test_check_truncates_long_unread_lists(tmp_path: Path) -> None:
    required = [f"pkg/{name}.py" for name in ("a", "b", "c", "d", "e", "f", "g")]
    problem = check(
        _verdict(examined=(*required, "pkg/anchor.py")),
        harness="omp",
        brief_mode="summary",
        workspace=tmp_path,
        required=[*required, "pkg/anchor.py"],
        run=Trace(),
        targets={"pkg/anchor.py": _listed("pkg/anchor.py")},
    )
    assert problem is not None
    assert problem.files == tuple(required)
    assert problem.reason.endswith("and 3 more")
    assert "pkg/g.py" not in problem.reason


def _withdrawn_verdict(evidence: str, examined: tuple[str, ...] = ("pkg/a.py",)) -> Verdict:
    return Verdict(
        status="complete",
        incomplete_reason="",
        decision="SHIP",
        summary="summary",
        files_examined=examined,
        defects=(),
        prior_defects=(PriorDefect(id="D1", status="withdrawn", evidence=evidence),),
    )


def test_mark_citations_keeps_a_withdrawal_with_a_head_commit_citation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    marked = mark_citations(
        _withdrawn_verdict("never a defect: pkg/a.py:2 keeps the row"), workspace, read=_returned_lines("pkg/a.py")
    )
    assert marked.prior_defects[0].status == "withdrawn"


def test_mark_citations_reopens_a_withdrawal_without_a_verifiable_citation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    marked = mark_citations(_withdrawn_verdict("never a defect"), workspace)
    assert marked.prior_defects[0].status == "not_fixed"
    assert UNVERIFIED_WITHDRAWAL in marked.prior_defects[0].evidence


def test_mark_citations_reopens_a_withdrawal_citing_a_missing_line(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\n")
    marked = mark_citations(
        _withdrawn_verdict("never a defect: pkg/a.py:9"), workspace, read=_returned_lines("pkg/a.py")
    )
    assert marked.prior_defects[0].status == "not_fixed"


def test_mark_citations_reopens_a_withdrawal_citing_a_file_examined_but_never_read(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    marked = mark_citations(_withdrawn_verdict("never a defect: pkg/a.py:2"), workspace)
    assert marked.prior_defects[0].status == "not_fixed"
    assert UNVERIFIED_WITHDRAWAL in marked.prior_defects[0].evidence


def test_mark_citations_reopens_a_withdrawal_citing_an_unexamined_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    marked = mark_citations(_withdrawn_verdict("never a defect: pkg/a.py:2", ()), workspace, read={})
    assert marked.prior_defects[0].status == "not_fixed"


def test_withdrawal_citing_unreturned_line_counts_as_not_fixed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "a.py").write_text("one\ntwo\nthree\nfour\n")
    verdict = _withdrawn_verdict("never a defect: pkg/a.py:4 keeps the row")
    outside = mark_citations(verdict, workspace, read={"pkg/a.py": ((1, 3),)})
    assert outside.prior_defects[0].status == "not_fixed"
    assert outside.prior_defects[0].demoted == "withdrawal"
    assert UNVERIFIED_WITHDRAWAL in outside.prior_defects[0].evidence
    inside = mark_citations(verdict, workspace, read={"pkg/a.py": ((1, 3), (4, 4))})
    assert inside.prior_defects[0].status == "withdrawn"
    unrelated = mark_citations(verdict, workspace, read={"pkg/b.py": ((1, 9),)})
    assert unrelated.prior_defects[0].status == "not_fixed"


def test_repro_citation_needs_a_returned_line_of_that_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "repro.py").write_text("one\n")
    prior = PriorDefect("D1", "fixed", "repro: repro.py fails before", "", True)
    verdict = replace(_verdict(), prior_defects=(prior,))
    for read in ({}, {"repro.py": ()}, {"other.py": ((1, 1),)}):
        marked = mark_citations(verdict, workspace, disputed=("D1",), read=read)
        assert marked.prior_defects[0].status == "not_fixed"
        assert marked.prior_defects[0].demoted == "fixed"
    marked = mark_citations(verdict, workspace, disputed=("D1",), read={"repro.py": ((1, 1),)})
    assert marked.prior_defects[0].status == "fixed"


def test_compacted_detects_the_event() -> None:
    event = json.dumps({"type": "auto_compaction_start", "reason": "threshold"})
    quiet = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": []}})
    assert compacted("omp", f"{quiet}\n{event}\n")
    assert compacted("omp", f"{event}\n")
    assert not compacted("omp", f"{quiet}\n")
    assert not compacted("omp", "")
    assert not compacted("text", f"{event}\n")
    assert not compacted("omp", "auto_compaction_start\n{not json auto_compaction_start}\n")
    mention = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": "auto_compaction_start"}})
    assert not compacted("omp", f"{mention}\n")
    assert not compacted("omp", json.dumps({"type": "auto_compaction_end"}))


def _sibling_files(workspace: Path) -> None:
    _put(workspace, "pkg/a.py")
    _put(workspace, "pkg/ab.py")


@pytest.mark.parametrize(
    "selector",
    [
        "",
        ":raw",
        ":5",
        ":L5",
        ":5-",
        ":5..",
        ":5-10",
        ":5..10",
        ":L5-L10",
        ":5+20",
        ":L5+L20",
        ":-30",
        ":1-12,-15",
        ":1-30,:-30",
        ":5-16,960-973",
        ":5-10:raw",
        ":raw:5-10",
        ":17:raw",
        ":5-:raw",
        ":5+20:raw",
    ],
)
def test_every_documented_line_and_raw_selector_credits_the_exact_path(tmp_path: Path, selector: str) -> None:
    _sibling_files(tmp_path)
    run = _spans((f"pkg/a.py{selector}", (3, 8)))
    assert returned(run, tmp_path) == {"pkg/a.py": ((3, 8),)}


@pytest.mark.parametrize(
    "path",
    [
        "pkg/a.py?q=.version",
        "pkg/a.py:conflicts",
        "pkg/a.py:img",
        "pkg/a.py:5:7",
        "pkg/a.py:0x",
        "pkg/a.pyx",
        "pkg/a.py.bak:5",
        "../outside/pkg/a.py",
        "pkg/a.py:../../../pkg/ab.py",
        "pkg",
        "pkg/*.py",
    ],
)
def test_unrecognized_selectors_siblings_and_traversal_credit_nothing(tmp_path: Path, path: str) -> None:
    _sibling_files(tmp_path)
    assert "pkg/a.py" not in returned(_spans((path, (1, 5))), tmp_path)


def test_a_computed_query_read_stays_uncredited_while_a_plain_read_credits(tmp_path: Path) -> None:
    _sibling_files(tmp_path)
    queried = trace("omp", _tool_events("read", "pkg/a.py?q=.version", is_error=False, output="1"))
    plain = trace("omp", _tool_events("read", "pkg/a.py", is_error=False, output="def f(): ..."))
    assert returned(queried, tmp_path) == {}
    assert returned(plain, tmp_path) == {"pkg/a.py": ((1, 5),)}


@pytest.mark.parametrize(
    "path",
    [
        "dist/x.whl:",
        "dist/x.whl:pkg/mod.py",
        "dist/x.whl:pkg/mod.py:5-9",
        "dist/x.tar.gz:a/b.txt",
        "dist/x.whl:pkg/mod.py?q=.x",
        "dist/x.whl:../../pkg/mod.py",
        "dist/x.txt:pkg/mod.py",
        "pkg/a.py;dist/x.whl:pkg/mod.py",
        "dist/x.whl:pkg/mod.py;pkg/a.py",
    ],
)
def test_archive_reads_credit_no_lines(tmp_path: Path, path: str) -> None:
    for name in ("dist/x.whl", "dist/x.tar.gz", "dist/x.txt", "pkg/a.py"):
        _put(tmp_path, name)
    run = trace("omp", _tool_events("read", path, is_error=False, output="pkg/\nmod.py (12)"))
    assert returned(run, tmp_path) == {}


@pytest.mark.parametrize(
    ("path", "is_error", "output"),
    [
        ("dist/x.whl:pkg/mod.py", True, "Archive entry not found"),
        ("dist/x.whl:pkg/mod.py", False, "[Cannot read binary archive entry 'pkg/mod.so' (12 bytes)]"),
    ],
)
def test_failed_archive_reads_are_counted_as_failures(tmp_path: Path, path: str, is_error: bool, output: str) -> None:
    run = trace("omp", _tool_events("read", path, is_error=is_error, output=output))
    assert returned(run, tmp_path) == {}
    assert run.failed == 1


def _summarized_read(path: str, spans: int, lines: int) -> str:
    details = {"summary": {"lines": 64, "elidedSpans": spans, "elidedLines": lines}}
    return _events("read", path, details, output="x")


def test_elided_read_credits_no_lines(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "x\n" * 64)
    run = trace("omp", _summarized_read("pkg/a.py", 7, 107))
    assert run.reads == ("pkg/a.py",)
    assert run.elisions == (("pkg/a.py", 7, 107),)
    assert run.ledger == (("pkg/a.py", (), False, True),)
    assert returned(run, tmp_path) == {}
    assert trace("omp", _summarized_read("pkg/a.py", 0, 0)).elisions == ()
    details = _read_details(1, 64)
    details["summary"] = {"lines": 64, "elidedSpans": 1, "elidedLines": 3}
    assert returned(trace("omp", _events("read", "pkg/a.py", details)), tmp_path) == {}


def test_describe_reports_bounded_counts_not_raw_streams(tmp_path: Path) -> None:
    failed = _tool_events("read", "pkg/gone.py", is_error=True, call_id="c2")
    events = _summarized_read("pkg/a.py", 7, 107) + failed
    required = [f"pkg/f{index}.py" for index in range(12)] + ["pkg/a.py"]
    text = describe(trace("omp", events), required, tmp_path)
    assert "reads=1" in text and "failed=1" in text and "elided_spans=7" in text and "elided_lines=107" in text
    assert "required=13" in text and "and 8 more" in text
    assert "hunks_covered" not in text
    assert len(text) <= 700


def test_describe_reports_hunk_coverage_when_a_report_is_given(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/a.py", "one\nTWO\nthree\n")
    _staged(tmp_path, MODIFIED_DIFF)
    run = _spans(("pkg/a.py", (1, 3)))
    report = _report(tmp_path, ["pkg/a.py"], run)
    text = describe(run, ["pkg/a.py"], tmp_path, report)
    assert "hunks_covered=0/1 files_open=1" in text
    assert len(text) <= 700


def test_describe_redacts_credentials_in_requested_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "fakecred-Aa1Bb2Cc3Dd4"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    run = trace("omp", _tool_events("read", f"pkg/{secret}.py", is_error=False, output="x"))
    assert secret not in describe(run, [], tmp_path)


def test_a_symlink_leaving_the_checkout_credits_nothing_for_read_archive_or_grep(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x\n", encoding="utf-8")
    (outside / "x.whl").write_text("z", encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "inside.py").write_text("x\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    (workspace / "link.whl").symlink_to(outside / "x.whl")
    for name, path in (("read", "link.txt"), ("read", "link.whl:pkg/m.py"), ("grep", "link.txt")):
        run = trace("omp", _tool_events(name, path, is_error=False, output="x"))
        assert returned(run, workspace) == {}


def test_a_symlink_staying_inside_the_checkout_is_still_credited(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "inside.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "pkg" / "alias.py").symlink_to(tmp_path / "pkg" / "inside.py")
    run = trace("omp", _tool_events("read", "pkg/alias.py", is_error=False, output="x"))
    seen = returned(run, tmp_path)
    assert [coverage.canonical(path, tmp_path) for path in seen] == ["pkg/inside.py"]
    assert list(seen.values()) == [((1, 5),)]


@pytest.mark.parametrize(
    "literal",
    ["a.py:raw", "a.py:1-2", "a.py:5", "a.py:conflicts", "a.py:img", "a.py?q=.x", "a.py:table", "a.py:pkg/m.py"],
)
def test_a_literal_filename_that_collides_with_a_selector_credits_nothing(tmp_path: Path, literal: str) -> None:
    (tmp_path / "a.py").write_text("base\n", encoding="utf-8")
    (tmp_path / literal).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / literal).write_text("literal\n", encoding="utf-8")
    run = trace("omp", _tool_events("read", literal, is_error=False, output="literal"))
    assert run.spans != ()
    assert returned(run, tmp_path) == {}


@pytest.mark.parametrize("literal", ["a.py:raw", "a.py:1-2"])
def test_a_literal_filename_without_a_base_file_is_credited_exactly(tmp_path: Path, literal: str) -> None:
    (tmp_path / literal).write_text("literal\n", encoding="utf-8")
    run = trace("omp", _tool_events("read", literal, is_error=False, output="literal"))
    assert returned(run, tmp_path) == {literal: ((1, 5),)}


def test_a_selector_credits_the_base_file_when_no_such_literal_file_exists(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("base\n", encoding="utf-8")
    run = trace("omp", _tool_events("read", "a.py:raw", is_error=False, output="base"))
    assert returned(run, tmp_path) == {"a.py": ((1, 5),)}


def test_required_symlink_inside_checkout_is_credited_by_real_path(tmp_path):
    (tmp_path / "real.md").write_text("x\n")
    (tmp_path / "link.md").symlink_to("real.md")
    found = {"link.md": Target("link.md", None, (), False)}
    assert coverage.evaluate(found, ["link.md"], returned(_spans(("real.md", (1, 1))), tmp_path), tmp_path).complete


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _head_repo(tmp_path: Path, files: dict[str, bytes], attributes: str = "") -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    base = _git(root, "rev-parse", "HEAD").strip()
    if attributes:
        (root / ".gitattributes").write_text(attributes, encoding="utf-8")
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return root, _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, "HEAD")


ANCHOR = "anchor.txt"


def _exposure(
    workspace: Path, required: list[str], run: Trace | None = None, anchor: bool = False, **kwargs: object
) -> str | None:
    diff = kwargs.pop("diff", "")
    examined: tuple[str, ...] = ()
    run = Trace() if run is None else run
    if anchor:
        _put(workspace, ANCHOR, "x\n")
        _staged(workspace, str(diff) + _added_diff(ANCHOR, 1))
        required = [*required, ANCHOR]
        examined = (ANCHOR,)
        run = replace(run, reads=(*run.reads, ANCHOR), spans=(*run.spans, (ANCHOR, ((1, 1),))))
    if diff:
        hinted = kwargs.get("binary_files", ())
        assert isinstance(hinted, (list, tuple))
        kwargs["binary_files"] = [*hinted, *diff_binary_files(str(diff))]
        _staged(workspace, str(diff))
    gap = check(
        _verdict(examined=examined),
        harness="omp",
        brief_mode="summary",
        workspace=workspace,
        required=required,
        run=run,
        targets=coverage.targets({"evidence_files": required}, workspace),
        **kwargs,
    )
    return gap.reason if gap else None


def _policy_exemptions() -> tuple[str, ...]:
    return load_policy(POLICY_PATH).evidence.exempt_paths


def _unread(path: str) -> str:
    return f"no read of {path}"


def _unexamined(path: str) -> str:
    return f"the verdict would ship but did not examine {path}"


def test_binary_file_and_readme_no_longer_make_attempt_absent(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"vendor/package.woff2": b"PK\x03\x04\x00\x00data", "pkg/README.md": b"prose\n"})
    assert (
        _exposure(
            root,
            ["vendor/package.woff2", "pkg/README.md"],
            diff=diff,
            exempt_paths=["README*.md", "**/README*.md"],
        )
        is None
    )


def test_source_file_still_requires_read(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _put(workspace, "other.py")
    required = ["pkg/README.md", "benchbox/core/checker.py"]
    for run in (None, _spans(("other.py", (1, 1)))):
        problem = _exposure(workspace, required, run, True, exempt_paths=["README*.md", "**/README*.md"])
        assert problem is not None and _unread("benchbox/core/checker.py") in problem
        assert "README" not in problem


def test_soundness_path_prose_file_still_requires_read(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    soundness_prose = ".github/PULL_REQUEST_TEMPLATE.md"
    _put(workspace, "other.py")
    for run in (None, _spans(("other.py", (1, 1)))):
        problem = _exposure(workspace, [soundness_prose], run, True, exempt_paths=["*.md"])
        assert problem is not None and _unread(soundness_prose) in problem


def test_an_archive_stays_required_even_after_a_binary_entry_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "dist").mkdir(parents=True)
    (workspace / "dist" / "x.whl").write_bytes(b"PK\x03\x04\x00\x00")
    events = _tool_events(
        "read",
        "dist/x.whl:pkg/mod.so",
        is_error=False,
        output="[Cannot read binary archive entry 'pkg/mod.so' (12 bytes)]",
    )
    run = trace("omp", events)
    assert run.failed == 1
    problem = _exposure(workspace, ["dist/x.whl"], run, True, binary_files=["dist/x.whl"])
    assert problem is not None and _unread("dist/x.whl") in problem


def test_diff_detected_binary_files_are_exempt(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"bin.woff2": b"head\x00tail"})
    assert "Binary files /dev/null and b/bin.woff2 differ" in diff
    assert _exposure(root, ["bin.woff2"], diff=diff) is None


def test_binary_notice_without_a_nul_byte_does_not_exempt(tmp_path: Path) -> None:
    root, _ = _head_repo(tmp_path, {"pkg/mod.woff2": b"value = 1\n"})
    notice = "diff --git a/pkg/mod.woff2 b/pkg/mod.woff2\nBinary files a/pkg/mod.woff2 and b/pkg/mod.woff2 differ\n"
    kwargs: dict[str, Any] = {"diff": notice, "binary_files": ["pkg/mod.woff2"]}
    problem = _exposure(root, ["pkg/mod.woff2"], **kwargs)
    assert problem is not None and "the file itself lines 1" in problem
    assert _exposure(root, ["pkg/mod.woff2"], _spans(("pkg/mod.woff2", (1, 1))), **kwargs) == _unexamined(
        "pkg/mod.woff2"
    )


def test_a_nul_head_binary_named_in_the_diff_needs_only_a_plain_read(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"data/blob.dat": b"head\x00tail"})
    assert "Binary files /dev/null and b/data/blob.dat differ" in diff
    problem = _exposure(root, ["data/blob.dat"], diff=diff)
    assert problem is not None and _unread("data/blob.dat") in problem
    plain = trace("omp", _events("read", "data/blob.dat", {"notes": ["binary"]}))
    assert plain.spans == () and plain.reads == ("data/blob.dat",)
    assert _exposure(root, ["data/blob.dat"], plain, diff=diff) == _unexamined("data/blob.dat")


def test_nul_byte_after_the_probe_window_does_not_confirm_binary(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"late.woff2": b"a" * 8000 + b"\x00"})
    problem = _exposure(root, ["late.woff2"], diff=diff, binary_files=["late.woff2"])
    assert problem is not None
    assert "changed hunks" in problem and "late.woff2" in problem


def test_a_text_file_the_diff_calls_binary_needs_its_whole_file_read(tmp_path: Path) -> None:
    root, diff = _head_repo(
        tmp_path,
        {"benchbox/core/checker.py": b"def verify():\n    return True\n"},
        attributes="*.py -diff\n",
    )
    path = "benchbox/core/checker.py"
    assert f"Binary files /dev/null and b/{path} differ" in diff
    unread = _exposure(root, [path], diff=diff)
    assert unread is not None and "the file itself lines 1-2" in unread
    plain = _exposure(root, [path], Trace(reads=(path,)), diff=diff)
    assert plain is not None and "the file itself lines 1-2" in plain
    partial = _exposure(root, [path], _spans((path, (1, 1))), diff=diff)
    assert partial is not None and "the file itself lines 1-2" in partial
    assert _exposure(root, [path], _spans((path, (1, 2))), diff=diff) == _unexamined(path)


def test_a_symlinked_binary_outside_the_checkout_is_not_confirmed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"\x00\x01")
    (workspace / "link.woff2").symlink_to(outside)
    problem = _exposure(workspace, ["link.woff2"], anchor=True, binary_files=["link.woff2"])
    assert problem is not None and _unread("link.woff2") in problem


def test_a_binary_absent_from_the_head_tree_is_not_confirmed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    problem = _exposure(workspace, ["gone.woff2"], anchor=True, binary_files=["gone.woff2"])
    assert problem is not None and _unread("gone.woff2") in problem


@pytest.mark.parametrize("path", ["package.whl", "picture.woff2", "data.zip"])
def test_binary_extension_alone_does_not_exempt_a_file(tmp_path: Path, path: str) -> None:
    (tmp_path / path).write_bytes(b"\x00\x01")
    problem = _exposure(tmp_path, [path], anchor=True)
    assert problem is not None and _unread(path) in problem


@pytest.mark.parametrize("path", [".github/PULL_REQUEST_TEMPLATE.md", ".gitattributes", "docs/.gitattributes"])
def test_binary_soundness_file_still_requires_exposure(tmp_path: Path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01")
    notice = f"diff --git a/{path} b/{path}\nBinary files a/{path} and b/{path} differ\n"
    problem = _exposure(tmp_path, [path], diff=notice, binary_files=[path], exempt_paths=["*.md", "*"])
    assert problem is not None and _unread(path) in problem


@pytest.mark.parametrize(
    "path",
    [
        "data.sql",
        "golden.csv",
        "fixture.json",
        "mod.py",
        "notes.txt",
        "blob.dat",
        "noext",
        "_native.so",
        "mod.pyc",
        "tool.exe",
        "data.parquet",
    ],
)
def test_a_nul_byte_in_a_text_type_or_unknown_file_never_exempts(tmp_path: Path, path: str) -> None:
    root, diff = _head_repo(tmp_path, {path: b"payload\x00hidden"})
    assert "differ" in diff
    problem = _exposure(root, [path], diff=diff, binary_files=[path])
    assert problem is not None and _unread(path) in problem
    read = _exposure(root, [path], Trace(reads=(path,)), diff=diff, binary_files=[path])
    assert read == _unexamined(path)


def test_a_machine_read_doc_stays_required_despite_a_recursive_docs_glob(tmp_path: Path) -> None:
    path = "docs/agent/overview.md"
    problem = _exposure(tmp_path, [path], anchor=True, exempt_paths=["docs/**/*.md"])
    assert problem is not None and _unread(path) in problem
    assert _exposure(tmp_path, ["docs/guide/page.md"], exempt_paths=["docs/**/*.md"]) is None


def test_a_protected_file_whose_name_ends_in_a_line_suffix_stays_required(tmp_path: Path) -> None:
    path = "pkg/README.md:1"
    protected = _exposure(
        tmp_path, [path], anchor=True, exempt_paths=["**/README*.md"], is_protected=lambda p: p == path
    )
    assert protected is not None and _unread(path) in protected


def test_the_docs_exemption_covers_only_direct_children_of_docs(tmp_path: Path) -> None:
    exempt = list(_policy_exemptions())
    assert _exposure(tmp_path, ["docs/index.md"], exempt_paths=exempt) is None
    for path in ("docs/benchmarks/tpc-h.md", "docs/development/result-validation.md"):
        problem = _exposure(tmp_path, [path], anchor=True, exempt_paths=exempt)
        assert problem is not None and _unread(path) in problem


def test_readme_and_changelog_exemptions_apply_at_any_depth(tmp_path: Path) -> None:
    exempt = list(_policy_exemptions())
    for path in ("pkg/README.md", "pkg/sub/README.md", "pkg/CHANGELOG.md", "a/b/c/CHANGELOG-1.md"):
        assert _exposure(tmp_path, [path], exempt_paths=exempt) is None


def test_a_protected_binary_stays_required(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"vendor/pack.woff2": b"PK\x00\x00"})
    assert _exposure(root, ["vendor/pack.woff2"], diff=diff) is None
    protected = _exposure(root, ["vendor/pack.woff2"], diff=diff, is_protected=lambda path: path == "vendor/pack.woff2")
    assert protected is not None and _unread("vendor/pack.woff2") in protected
    plain = Trace(reads=("vendor/pack.woff2",))
    read = _exposure(
        root, ["vendor/pack.woff2"], plain, diff=diff, is_protected=lambda path: path == "vendor/pack.woff2"
    )
    assert read == _unexamined("vendor/pack.woff2")


def test_an_all_exempt_change_needs_no_read(tmp_path: Path) -> None:
    root, diff = _head_repo(tmp_path, {"pic.woff2": b"wOF2\x00"})
    assert _exposure(root, [], diff=diff) is None
    assert _exposure(root, ["pic.woff2"], diff=diff) is None


def test_a_gitattributes_change_is_a_soundness_path_change() -> None:
    assert MATCHERS.is_soundness_path(".gitattributes")
    assert MATCHERS.is_soundness_path("docs/.gitattributes")
    assert not MATCHERS.is_soundness_path("docs/gitattributes.md")


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "pkg/README.md",
        "README-dev.md",
        "CHANGELOG.md",
        "pkg/CHANGELOG-1.md",
        "docs/guide.md",
    ],
)
def test_prose_globs_cover_prose_files(path: str) -> None:
    assert any(glob_match(path, pattern) for pattern in _policy_exemptions())


@pytest.mark.parametrize(
    "path",
    [
        "benchbox/README_helper.py",
        "README/loader.py",
        "pkg/README/loader.py",
        "CHANGELOG/gen.py",
        "benchbox/core/CHANGELOG_parser.py",
        "docs/gen.py",
        "docs/a/conf.yaml",
        "docs/a/b/guide.md",
        "CONTRIBUTING.md",
        "notes.md",
    ],
)
def test_prose_globs_do_not_cover_other_files(path: str) -> None:
    assert not any(glob_match(path, pattern) for pattern in _policy_exemptions())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Binary files a/blob and b/blob differ\n", frozenset({"blob"})),
        ("Binary files /dev/null and b/blob differ\n", frozenset({"blob"})),
        ("Binary files a/blob and /dev/null differ\n", frozenset({"blob"})),
        ("GIT binary patch\nliteral 3\nabc\n", frozenset({"blob"})),
        ("@@ -1 +1 @@\n+GIT binary patch\n", frozenset()),
        ("@@ -1 +1 @@\n Binary files a/blob and b/blob differ\n", frozenset()),
        ("Binary files a/other and b/other differ\n", frozenset()),
    ],
)
def test_binary_detection_uses_diff_metadata(body: str, expected: frozenset[str]) -> None:
    from theseus_reviewer.diff import binary_files

    assert binary_files("diff --git a/blob b/blob\n" + body) == expected


def test_exposure_exemption_does_not_bypass_withdrawal_citations(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("prose\n", encoding="utf-8")
    verdict = _withdrawn_verdict("never a defect: docs/guide.md:1", ())
    assert (
        check(
            verdict,
            harness="omp",
            brief_mode="file-list",
            workspace=tmp_path,
            required=["docs/guide.md"],
            run=Trace(),
            exempt_paths=["docs/*.md"],
        )
        is None
    )
    marked = mark_citations(verdict, tmp_path)
    assert marked.prior_defects[0].status == "not_fixed"
    assert UNVERIFIED_WITHDRAWAL in marked.prior_defects[0].evidence


@pytest.mark.parametrize("name", ["a+b.py", "with space.py", "naïve.py", "-hidden.py", "pkg/x y/z.py"])
def test_withdrawal_citation_accepts_every_schema_valid_path(tmp_path: Path, name: str) -> None:
    workspace = tmp_path / "ws"
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("one\ntwo\n")
    prior = PriorDefect("D1", "withdrawn", f"see {name}:2 for the refutation", "", True)
    marked = mark_citations(replace(_verdict(), prior_defects=(prior,)), workspace, read=_returned_lines(name))
    assert marked.prior_defects[0].status == "withdrawn"
    assert marked.prior_defects[0].demoted is None


@pytest.mark.parametrize("cited", ["../b.py:1", "/abs/b.py:1", "x+b.py:1", "b.py:9"])
def test_withdrawal_citation_still_rejects_escapes_unread_paths_and_missing_lines(tmp_path: Path, cited: str) -> None:
    workspace = tmp_path / "ws"
    (workspace / "abs").mkdir(parents=True)
    (workspace / "b.py").write_text("one\n")
    prior = PriorDefect("D1", "withdrawn", f"{cited} refutes it", "", True)
    marked = mark_citations(replace(_verdict(), prior_defects=(prior,)), workspace, read=_returned_lines("b.py"))
    assert marked.prior_defects[0].status == "not_fixed"


def test_repro_citation_accepts_a_path_with_spaces(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "with space.py").write_text("one\n")
    prior = PriorDefect("D1", "fixed", "repro: with space.py fails before", "", True)
    marked = mark_citations(
        replace(_verdict(), prior_defects=(prior,)), workspace, disputed=("D1",), read=_returned_lines("with space.py")
    )
    assert marked.prior_defects[0].status == "fixed"
