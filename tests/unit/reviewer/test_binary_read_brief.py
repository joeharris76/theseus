from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import cli, coverage
from theseus_reviewer.diff import STAGED_DIFF_NAME, binary_files
from theseus_reviewer.evidence import Gap, Trace, check
from theseus_reviewer.policy import Policy
from theseus_reviewer.render import (
    BINARY_MARK,
    BINARY_READS_NOTE,
    RETRY_NONE,
    UNREAD_BINARY_MARK,
    UNREAD_MARK,
    retry_note,
)
from theseus_reviewer.verdict import Verdict

from .test_attempts import _classification, _plan

pytestmark = [pytest.mark.unit, pytest.mark.medium]

PROTECTED = "bundle/_binaries/"
EXECUTABLES = (
    f"{PROTECTED}tpc-ds/darwin-arm64/dsdgen",
    f"{PROTECTED}tpc-ds/darwin-arm64/dsqgen",
    f"{PROTECTED}tpc-ds/linux-x86_64/dsdgen",
)
CHECKSUMS = f"{PROTECTED}tpc-ds/checksums.sha256"
REQUIRED = sorted((*EXECUTABLES, CHECKSUMS))
FEEDBACK = "Earlier attempt feedback: "


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _executable(name: str, build: str) -> bytes:
    return b"\x7fELF\x02\x01\x01\x00" + bytes(56) + f"{name}-{build}".encode()


def _commit(root: Path, build: str) -> None:
    for path in EXECUTABLES:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_executable(path, build))
    sums = "".join(
        f"{build}{index:062d}  {path.rsplit('/', 2)[-2]}/{path.rsplit('/', 1)[-1]}\n"
        for index, path in enumerate(EXECUTABLES)
    )
    (root / CHECKSUMS).write_text(sums, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", build)


def _rebuild(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "workspace"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, "aa")
    base = _git(root, "rev-parse", "HEAD").strip()
    _commit(root, "bb")
    return root, _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", base, "HEAD")


def _rows(required: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "previous_path": None,
            "additions": 3 if path == CHECKSUMS else 0,
            "deletions": 3 if path == CHECKSUMS else 0,
            "status": "modified",
        }
        for path in required
    ]


def _brief(
    policy: Policy,
    tmp_path: Path,
    workspace: Path,
    diff: str,
    gap: Gap | None = None,
    required: list[str] = REQUIRED,
    **extra: Any,
) -> str:
    plan = {
        **_plan(policy, ("large", "huge")),
        "brief_mode": "file-list",
        "diff_inline": None,
        "evidence_files": list(required),
        "reviewed_files": _rows(required),
        **extra,
    }
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    (plan_dir / cli.DIFF_FILE).write_text(diff, encoding="utf-8")
    (workspace / STAGED_DIFF_NAME).unlink(missing_ok=True)
    unread = gap.files if gap else ()
    unseen = gap.unseen if gap else ()
    return cli._assemble_prompt(plan, plan_dir, _classification("large"), workspace, unread, unseen)


def _row(brief: str, path: str) -> str:
    section = brief.split("Required reads:", 1)[1].split("-END CHANGE DATA", 1)[0]
    return next(line for line in section.splitlines() if line.startswith(f"{path} (+"))


def _gap(workspace: Path, diff: str, run: Trace) -> Gap | None:
    return check(
        Verdict(
            status="complete",
            incomplete_reason="",
            decision="SHIP",
            summary="summary",
            files_examined=tuple(REQUIRED),
            defects=(),
        ),
        clears=True,
        harness="omp",
        brief_mode="file-list",
        workspace=workspace,
        required=REQUIRED,
        run=run,
        targets=coverage.targets({"evidence_files": REQUIRED}, workspace),
        binary_files=binary_files(diff),
        exempt_paths=("docs/*.md",),
        is_protected=lambda path: path.startswith(PROTECTED),
    )


def _diff_read(workspace: Path) -> Trace:
    lines = (workspace / STAGED_DIFF_NAME).read_bytes().count(b"\n")
    return Trace(reads=(STAGED_DIFF_NAME,), spans=((STAGED_DIFF_NAME, ((1, lines),)),))


def test_the_first_brief_states_the_one_read_each_changed_executable_needs(policy: Policy, tmp_path: Path) -> None:
    workspace, diff = _rebuild(tmp_path)
    assert binary_files(diff) == frozenset(EXECUTABLES)
    brief = _brief(policy, tmp_path, workspace, diff)
    assert BINARY_READS_NOTE.strip() in brief
    assert "one successful read of that exact path on its own" in brief
    assert "shows only the file's metadata, that one read is enough" in brief and "every line must be shown" in brief
    for path in EXECUTABLES:
        assert _row(brief, path).endswith(BINARY_MARK)
    assert "binary" not in _row(brief, CHECKSUMS)
    assert UNREAD_MARK not in brief and UNREAD_BINARY_MARK not in brief
    assert f"{FEEDBACK}{RETRY_NONE}" in brief


def test_a_reviewer_that_follows_the_brief_covers_executables_and_their_checksums(
    policy: Policy, tmp_path: Path
) -> None:
    workspace, diff = _rebuild(tmp_path)
    _brief(policy, tmp_path, workspace, diff)
    diff_only = _diff_read(workspace)
    followed = Trace(reads=(*diff_only.reads, *EXECUTABLES), spans=diff_only.spans)
    assert _gap(workspace, diff, followed) is None


def test_reading_the_whole_diff_without_a_read_of_each_executable_is_still_rejected(
    policy: Policy, tmp_path: Path
) -> None:
    workspace, diff = _rebuild(tmp_path)
    _brief(policy, tmp_path, workspace, diff)
    gap = _gap(workspace, diff, _diff_read(workspace))
    assert gap is not None and sorted(gap.files) == sorted(EXECUTABLES)
    together = Trace(reads=(*_diff_read(workspace).reads, ";".join(EXECUTABLES)), spans=_diff_read(workspace).spans)
    assert _gap(workspace, diff, together) is not None


def test_the_retry_names_the_missing_executable_reads_not_unseen_hunks(policy: Policy, tmp_path: Path) -> None:
    workspace, diff = _rebuild(tmp_path)
    _brief(policy, tmp_path, workspace, diff)
    gap = _gap(workspace, diff, _diff_read(workspace))
    assert gap is not None
    retry = _brief(policy, tmp_path, workspace, diff, gap)
    for path in EXECUTABLES:
        assert _row(retry, path).endswith(UNREAD_BINARY_MARK)
    assert UNREAD_MARK not in _row(retry, CHECKSUMS) and "binary" not in _row(retry, CHECKSUMS)
    feedback = retry.split(FEEDBACK, 1)[1].split("\n", 1)[0]
    assert feedback == retry_note(len(EXECUTABLES), len(EXECUTABLES))
    assert (
        "changed hunk" not in feedback and f"did not read {len(EXECUTABLES)} of the required binary files" in feedback
    )


def test_a_retry_that_missed_an_executable_and_a_hunk_counts_each_apart(policy: Policy, tmp_path: Path) -> None:
    workspace, diff = _rebuild(tmp_path)
    _brief(policy, tmp_path, workspace, diff)
    gap = _gap(workspace, diff, Trace(reads=EXECUTABLES[1:]))
    assert gap is not None and sorted(gap.files) == sorted((EXECUTABLES[0], CHECKSUMS))
    retry = _brief(policy, tmp_path, workspace, diff, gap)
    assert _row(retry, EXECUTABLES[0]).endswith(UNREAD_BINARY_MARK)
    assert UNREAD_MARK in _row(retry, CHECKSUMS)
    for path in EXECUTABLES[1:]:
        assert _row(retry, path).endswith(BINARY_MARK)
    feedback = retry.split(FEEDBACK, 1)[1].split("\n", 1)[0]
    assert feedback == retry_note(2, 1)
    assert "did not read 1 of the required binary files nor every changed hunk of 1 other required files" in feedback


def test_only_binaries_the_check_requires_carry_the_binary_mark(policy: Policy, tmp_path: Path) -> None:
    font, exempt, guarded = "web/icon.woff2", "assets/blob.bin", f"{PROTECTED}fonts/icon.woff2"
    paths = sorted((font, exempt, guarded, EXECUTABLES[0]))
    diff = "".join(
        f"diff --git a/{p} b/{p}\nindex 1111111..2222222 100644\nBinary files a/{p} and b/{p} differ\n" for p in paths
    )
    workspace = tmp_path / "exempt-workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    brief = _brief(
        policy,
        tmp_path,
        workspace,
        diff,
        required=paths,
        evidence_exempt_paths=["assets/*.bin"],
        evidence_protected_files=[guarded, EXECUTABLES[0]],
    )
    for path in (font, exempt):
        assert "binary" not in _row(brief, path)
    for path in (guarded, EXECUTABLES[0]):
        assert _row(brief, path).endswith(BINARY_MARK)
