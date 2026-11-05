from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import audit, cli, evidence
from theseus_reviewer.diff import STAGED_DIFF_NAME, hunk_index

pytestmark = [pytest.mark.unit, pytest.mark.fast]

SOURCE = "".join(f"line {number}\n" for number in range(1, 21))
BIG = "".join(f"row {number}\n" for number in range(1, 401))
DIFF = (
    "diff --git a/a.py b/a.py\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,2 +1,3 @@\n"
    " line 1\n"
    "+line added\n"
    " line 2\n"
    "@@ -10,2 +11,2 @@\n"
    "-old line\n"
    "+line 11\n"
    " line 12\n"
)
MODE_ONLY = "diff --git a/mode.py b/mode.py\nold mode 100644\nnew mode 100755\n"
FIRST_BLOCK, SECOND_BLOCK = hunk_index(DIFF)["a.py"]


def _workspace(tmp_path: Path, diff: str | None = DIFF) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    (workspace / "big.py").write_text(BIG, encoding="utf-8")
    (workspace / "mode.py").write_text("x = 1\n", encoding="utf-8")
    if diff is not None:
        (workspace / STAGED_DIFF_NAME).write_text(diff, encoding="utf-8")
    return workspace


def _plan(*files: str, mode: str = "file-list", **over: Any) -> dict[str, Any]:
    return {
        "brief_mode": mode,
        "evidence_files": list(files),
        "binary_files": [],
        "evidence_exempt_paths": [],
        "evidence_protected_files": [],
        **over,
    }


def _ledger(path: str, *spans: tuple[int, int]) -> dict[str, Any]:
    return {"path": path, "lines": [list(span) for span in spans], "truncated": False, "elided": False}


def _capture(*reads: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"schema": 2, "reads": list(reads), "events": [], **over}


def _whole_diff() -> dict[str, Any]:
    return _ledger(STAGED_DIFF_NAME, (1, len(DIFF.splitlines())))


def _blocks() -> tuple[tuple[int, int], tuple[int, int]]:
    return (FIRST_BLOCK.block_start, FIRST_BLOCK.block_end), (SECOND_BLOCK.block_start, SECOND_BLOCK.block_end)


@pytest.mark.parametrize(
    ("path", "total", "expected"),
    [
        ("a.py", 1000, ((1, 300),)),
        ("a.py", 120, ((1, 120),)),
        ("a.py", 0, ()),
        ("a.py:5-9", 1000, ((5, 9),)),
        ("a.py:5+3", 1000, ((5, 7),)),
        ("a.py:5-", 1000, ((5, 304),)),
        ("a.py:5-", 100, ((5, 100),)),
        ("a.py:-3", 1000, ((998, 1000),)),
        ("a.py:-3000", 1000, ((1, 1000),)),
        ("a.py:raw", 1000, ((1, 300),)),
        ("a.py:raw", 40, ((1, 40),)),
        ("a.py:raw:5-9", 1000, ((5, 9),)),
        ("a.py:5-9:raw", 1000, ((5, 9),)),
        ("a.py:L5-L9", 1000, ((5, 9),)),
        ("a.py:5..9", 1000, ((5, 9),)),
        ("a.py:1-3,10-12", 1000, ((1, 3), (10, 12))),
        ("a.py:1-3,4-6", 1000, ((1, 6),)),
        ("a.py:10-12,1-3", 1000, ((1, 3), (10, 12))),
        ("a.py:995-1005", 1000, ((995, 1000),)),
        ("a.py:2000-2010", 1000, ()),
        ("a.py:9-5", 1000, ()),
        ("a.py:7", 1000, ((7, 306),)),
    ],
)
def test_a_path_selector_names_the_lines_it_returns(path: str, total: int, expected: tuple) -> None:
    assert audit.selector_ranges(path, total) == expected


def _call(identifier: str, path: str) -> dict[str, Any]:
    block = {"type": "toolCall", "id": identifier, "name": "read", "arguments": {"path": path}}
    return {"type": "message_end", "message": {"role": "assistant", "content": [block]}}


def _result(identifier: str, **details: Any) -> dict[str, Any]:
    message = {
        "role": "toolResult",
        "toolCallId": identifier,
        "toolName": "read",
        "content": [{"type": "text", "text": "text"}],
        "isError": False,
    }
    if details:
        message["details"] = details
    return {"type": "message_end", "message": message}


def _events(*reads: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for index, (path, details) in enumerate(reads):
        events += [_call(f"c{index}", path), _result(f"c{index}", **details)]
    return events


def test_a_schema_one_capture_with_withheld_details_is_reconstructed_from_the_selectors(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    capture = {
        "schema": 1,
        "events": _events(
            (f"{workspace}/big.py", {}),
            (f"{workspace}/a.py:5-9", {}),
            ("a.py:raw:12-14", {}),
            (f"{workspace}/{STAGED_DIFF_NAME}", {}),
        ),
    }
    reads, unresolved = audit.returned_from_capture(capture, workspace)
    assert unresolved == 0
    assert reads == {
        "big.py": ((1, 300),),
        "a.py": ((5, 9), (12, 14)),
        STAGED_DIFF_NAME: ((1, len(DIFF.splitlines())),),
    }


def test_a_schema_one_reconstruction_leaves_out_summarized_multi_path_and_failed_reads(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    failed = _result("c3")
    failed["message"]["isError"] = True
    events = _events(
        ("big.py", {"summary": {"elidedSpans": 2, "elidedLines": 40}}),
        (f"{workspace}/a.py;{workspace}/big.py:1-3", {}),
        ("a.py:1-2", {}),
    ) + [_call("c3", "mode.py"), failed]
    reads, unresolved = audit.returned_from_capture({"schema": 1, "events": events}, workspace)
    assert reads == {"a.py": ((1, 2),)} and unresolved == 0


def test_a_schema_one_capture_with_no_events_credits_nothing(tmp_path: Path) -> None:
    assert audit.returned_from_capture({"schema": 1}, _workspace(tmp_path)) == ({}, 0)


def test_a_schema_one_read_of_a_file_outside_the_workspace_is_counted_unresolved(tmp_path: Path) -> None:
    capture = {"schema": 1, "events": _events(("/elsewhere/nowhere.py", {}), ("a.py:1-2", {}))}
    reads, unresolved = audit.returned_from_capture(capture, _workspace(tmp_path))
    assert reads == {"a.py": ((1, 2),)} and unresolved == 1


def test_a_schema_two_ledger_is_taken_exactly_and_never_reconstructed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    capture = _capture(
        _ledger(f"{workspace}/big.py", (1, 120)),
        _ledger("big.py:1-300", (200, 210)),
        _ledger("a.py", (3, 4)),
        _ledger("a.py:raw", (4, 6), (9, 9)),
        _ledger("mode.py"),
        _ledger(f"{workspace}/a.py;{workspace}/big.py", (1, 5)),
        _ledger("/elsewhere/gone.py", (1, 5)),
    )
    reads, unresolved = audit.returned_from_capture(capture, workspace)
    assert reads == {"big.py": ((1, 120), (200, 210)), "a.py": ((3, 6), (9, 9))}
    assert unresolved == 1


def test_a_schema_two_capture_with_a_malformed_ledger_entry_skips_it(tmp_path: Path) -> None:
    capture = _capture({"lines": [[1, 2]]}, {"path": 3, "lines": []}, _ledger("a.py", (1, 2)))
    assert audit.returned_from_capture(capture, _workspace(tmp_path)) == ({"a.py": ((1, 2),)}, 0)


def test_a_capture_is_compacted_only_when_an_auto_compaction_event_exists() -> None:
    marker = {"type": evidence.COMPACTION_EVENT, "reason": "threshold"}
    assert audit.compacted_in({"events": [{"type": "message_end"}, marker]})
    assert not audit.compacted_in({"events": [{"type": "message_end"}, "auto_compaction_start"]})
    assert not audit.compacted_in({"events": []})
    assert not audit.compacted_in({})


def test_a_report_is_covered_when_the_whole_staged_diff_was_read(tmp_path: Path) -> None:
    result = audit.report(_plan("a.py"), _workspace(tmp_path), _capture(_whole_diff()))
    assert result["covered"] is True
    assert result["hunks"] == {"total": 2, "covered": 2} and result["uncovered"] == []
    assert result["untouched"] == [] and result["compacted"] is False
    assert (result["reads"], result["capture_schema"], result["required"]) == ("recorded", 2, 1)


def test_a_report_names_the_hunk_whose_block_was_not_read(tmp_path: Path) -> None:
    first, _ = _blocks()
    result = audit.report(_plan("a.py"), _workspace(tmp_path), _capture(_ledger(STAGED_DIFF_NAME, first)))
    assert result["covered"] is False and result["hunks"] == {"total": 2, "covered": 1}
    assert result["uncovered"] == [
        {
            "file": "a.py",
            "source": STAGED_DIFF_NAME,
            "start": SECOND_BLOCK.block_start,
            "end": SECOND_BLOCK.block_end,
        }
    ]


def test_a_partial_read_of_a_hunk_block_leaves_the_hunk_unseen(tmp_path: Path) -> None:
    start, end = _blocks()[1]
    result = audit.report(_plan("a.py"), _workspace(tmp_path), _capture(_ledger(STAGED_DIFF_NAME, (start, end - 1))))
    assert result["covered"] is False
    assert {(item["start"], item["end"]) for item in result["uncovered"]} == {
        (FIRST_BLOCK.block_start, FIRST_BLOCK.block_end),
        (start, end),
    }


def test_an_additive_hunk_is_also_seen_by_reading_its_lines_of_the_file(tmp_path: Path) -> None:
    lines = (FIRST_BLOCK.new_start, FIRST_BLOCK.new_end)
    second = (SECOND_BLOCK.block_start, SECOND_BLOCK.block_end)
    workspace = _workspace(tmp_path)
    result = audit.report(_plan("a.py"), workspace, _capture(_ledger("a.py", lines), _ledger(STAGED_DIFF_NAME, second)))
    assert result["covered"] is True
    result = audit.report(
        _plan("a.py"), workspace, _capture(_ledger("a.py", (SECOND_BLOCK.new_start, SECOND_BLOCK.new_end)))
    )
    assert result["covered"] is False and result["hunks"]["covered"] == 0


def test_a_file_absent_from_the_diff_and_never_read_is_untouched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = audit.report(_plan("a.py", "other.py"), workspace, _capture(_whole_diff()))
    assert result["untouched"] == ["other.py"] and result["covered"] is False
    result = audit.report(_plan("a.py", "other.py"), workspace, _capture(_whole_diff(), _ledger("a.py", (1, 1))))
    assert result["untouched"] == ["other.py"]
    result = audit.report(_plan("a.py", "other.py"), workspace, _capture(_whole_diff(), _ledger("big.py", (1, 1))))
    assert result["untouched"] == ["other.py"]


def test_a_listed_file_with_no_hunks_needs_no_read(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, DIFF + MODE_ONLY)
    result = audit.report(_plan("a.py", "mode.py"), workspace, _capture(_whole_diff()))
    assert result["untouched"] == [] and result["covered"] is True and result["hunks"]["total"] == 2


def test_a_report_skips_a_required_file_that_the_exempt_patterns_cover(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    plan = _plan("a.py", "other.py", evidence_exempt_paths=["other.py"])
    result = audit.report(plan, workspace, _capture(_whole_diff()))
    assert result["required"] == 1 and result["covered"] is True
    protected = {**plan, "evidence_protected_files": ["other.py"]}
    result = audit.report(protected, workspace, _capture(_whole_diff()))
    assert result["required"] == 2 and result["untouched"] == ["other.py"]


def test_a_report_on_an_inline_brief_needs_no_reads_when_every_file_is_listed(tmp_path: Path) -> None:
    plan = _plan("a.py", mode="inline", diff_inline=DIFF)
    result = audit.report(plan, _workspace(tmp_path, None), _capture())
    assert result["covered"] is True and result["brief_mode"] == "inline"
    assert result["uncovered"] == [] and result["untouched"] == [] and result["omitted_from_brief"] == []


def test_a_report_on_an_inline_brief_names_a_file_the_brief_left_out(tmp_path: Path) -> None:
    plan = _plan("a.py", "mode.py", mode="inline", diff_inline=DIFF)
    result = audit.report(plan, _workspace(tmp_path, None), _capture())
    assert result["covered"] is False and result["omitted_from_brief"] == ["mode.py"]
    assert result["uncovered"] == [] and result["untouched"] == []


def test_a_compacted_capture_is_never_covered_even_when_every_hunk_was_read(tmp_path: Path) -> None:
    events = [{"type": evidence.COMPACTION_EVENT}]
    result = audit.report(_plan("a.py"), _workspace(tmp_path), _capture(_whole_diff(), events=events))
    assert result["compacted"] is True and result["hunks"] == {"total": 2, "covered": 2}
    assert result["uncovered"] == [] and result["covered"] is False


def test_a_compacted_capture_on_an_inline_brief_is_not_covered(tmp_path: Path) -> None:
    plan = _plan("a.py", mode="inline", diff_inline=DIFF)
    capture = _capture(events=[{"type": evidence.COMPACTION_EVENT}])
    assert audit.report(plan, _workspace(tmp_path, None), capture)["covered"] is False


def test_a_schema_one_report_is_marked_reconstructed_and_credits_the_selector_lines(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    capture = {"schema": 1, "events": _events((f"{workspace}/{STAGED_DIFF_NAME}", {}))}
    result = audit.report(_plan("a.py"), workspace, capture)
    assert (result["reads"], result["capture_schema"], result["covered"]) == ("reconstructed", 1, True)


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env)
    return done.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / "a.py").write_text(SOURCE, encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "--quiet", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _prepared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, head: str, plan: dict[str, Any], diff: str = DIFF
) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / cli.DIFF_FILE).write_text(diff, encoding="utf-8")
    monkeypatch.setattr(audit, "_plan", lambda args, root, work: (plan, plan_dir, head))


def _args(tmp_path: Path, root: Path, capture: dict[str, Any] | None, *, as_json: bool = True) -> argparse.Namespace:
    path = tmp_path / "capture.json"
    if capture is not None:
        path.write_text(json.dumps(capture), encoding="utf-8")
    return argparse.Namespace(
        capture=str(path),
        policy="policy.yml",
        repo="owner/name",
        repo_root=str(root),
        base_ref="develop",
        trusted_ref="",
        head="HEAD",
        json=as_json,
    )


def test_the_audit_exits_zero_when_every_hunk_was_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py"))
    assert audit.command_audit(_args(tmp_path, root, _capture(_whole_diff()))) == audit.EXIT_COVERED == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["covered"] is True and printed["hunks"] == {"total": 2, "covered": 2}


def test_the_audit_exits_three_when_a_hunk_was_not_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py"))
    code = audit.command_audit(_args(tmp_path, root, _capture(_ledger(STAGED_DIFF_NAME, _blocks()[0]))))
    assert code == audit.EXIT_NOT_COVERED == 3
    assert json.loads(capsys.readouterr().out)["uncovered"][0]["start"] == SECOND_BLOCK.block_start


def test_the_audit_exits_three_for_a_compacted_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py"))
    capture = _capture(_whole_diff(), events=[{"type": evidence.COMPACTION_EVENT}])
    assert audit.command_audit(_args(tmp_path, root, capture, as_json=False)) == audit.EXIT_NOT_COVERED
    printed = capsys.readouterr().out
    assert "the reviewer's context was compacted" in printed and printed.rstrip().endswith("not covered")


def test_the_audit_prints_a_readable_summary_and_exits_zero_when_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py"))
    assert audit.command_audit(_args(tmp_path, root, _capture(_whole_diff()), as_json=False)) == audit.EXIT_COVERED
    printed = capsys.readouterr().out
    assert "hunks covered 2/2 (recorded reads, 1 required files)" in printed
    assert printed.rstrip().endswith("covered") and "not covered" not in printed


def test_the_audit_lists_the_unseen_hunks_and_untouched_files_in_text_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py", "other.py"), DIFF)
    assert audit.command_audit(_args(tmp_path, root, _capture(), as_json=False)) == audit.EXIT_NOT_COVERED
    printed = capsys.readouterr().out
    assert f"not seen: a.py ({STAGED_DIFF_NAME} lines {FIRST_BLOCK.block_start}-{FIRST_BLOCK.block_end})" in printed
    assert "never read: other.py" in printed


def test_the_audit_exits_two_when_the_plan_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = repository

    def refuse(args: argparse.Namespace, root: Path, work: Path) -> Any:
        raise audit.Undetermined("the review plan cannot be built")

    monkeypatch.setattr(audit, "_plan", refuse)
    assert audit.command_audit(_args(tmp_path, root, _capture())) == audit.EXIT_UNDETERMINED == 2
    assert "the review plan cannot be built" in capsys.readouterr().out


def test_the_audit_exits_two_when_the_head_cannot_be_checked_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str]
) -> None:
    root, _ = repository
    _prepared(monkeypatch, tmp_path, "f" * 40, _plan("a.py"))
    assert audit.command_audit(_args(tmp_path, root, _capture(_whole_diff()))) == audit.EXIT_UNDETERMINED


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"], ids=["missing", "garbled", "not-an-object"])
def test_the_audit_exits_two_for_a_capture_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repository: tuple[Path, str], content: str | None
) -> None:
    root, head = repository
    _prepared(monkeypatch, tmp_path, head, _plan("a.py"))
    args = _args(tmp_path, root, None)
    if content is not None:
        Path(args.capture).write_text(content, encoding="utf-8")
    assert audit.command_audit(args) == audit.EXIT_UNDETERMINED


def test_the_audit_command_is_registered_with_its_options() -> None:
    parser = argparse.ArgumentParser()
    audit.register(parser.add_subparsers(dest="command"))
    parsed = parser.parse_args(["audit-coverage", "--capture", "c.json", "--policy", "p.yml", "--json"])
    assert parsed.handler is audit.command_audit and parsed.json is True
    assert (parsed.head, parsed.repo_root, parsed.base_ref, parsed.trusted_ref) == ("HEAD", ".", "", "")
