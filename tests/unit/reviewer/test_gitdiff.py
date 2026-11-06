from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import gitdiff

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    body = "".join(f"line {number}\n" for number in range(40))
    (repo / "keep.py").write_text(body, encoding="utf-8")
    (repo / "moved.py").write_text(body.replace("line 1\n", "other 1\n"), encoding="utf-8")
    (repo / "gone.py").write_text("gone\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "keep.py").write_text(body.replace("line 20\n", "changed 20\n"), encoding="utf-8")
    _git(repo, "mv", "moved.py", "renamed.py")
    (repo / "gone.py").unlink()
    (repo / "added.py").write_text("new\n", encoding="utf-8")
    head = _commit(repo, "feature")
    return repo, base, head


def test_hosted_and_local_build_identical_diffs(remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    built = gitdiff.build("owner/name", base, head, remote=repo.as_uri(), token="secret")
    assert built.text == gitdiff.local_diff(repo, base, head)
    assert "rename from moved.py" in built.text and "changed 20" in built.text
    assert built.paths == {"keep.py", "moved.py", "renamed.py", "gone.py", "added.py"}
    gitdiff.verify_paths(built, ["keep.py", "moved.py", "renamed.py", "gone.py", "added.py"])


def test_build_reports_an_unreachable_commit(remote: tuple[Path, str, str]) -> None:
    repo, base, _ = remote
    with pytest.raises(gitdiff.GitDiffError, match="fetch failed"):
        gitdiff.build("owner/name", base, "1" * 40, remote=repo.as_uri(), token="")


def test_build_reports_a_missing_remote(tmp_path: Path) -> None:
    with pytest.raises(gitdiff.GitDiffError, match="failed"):
        gitdiff.build("owner/name", "a" * 40, "b" * 40, remote=(tmp_path / "absent").as_uri(), token="")


class Recorder:
    def __init__(self, banner: str | None = None) -> None:
        self.banner = banner
        self.calls: list[tuple[list[str], Path | None, dict[str, str]]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), cwd, dict(env or {})))
        if self.banner is not None:
            return subprocess.CompletedProcess(list(argv), 0, self.banner.encode(), b"")
        return gitdiff.run_process(argv, cwd, env)


def test_token_travels_only_in_the_environment(monkeypatch: pytest.MonkeyPatch, remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    recorder = Recorder()
    monkeypatch.setenv("GH_TOKEN", "never-in-argv")
    gitdiff.build("owner/name", base, head, remote=repo.as_uri(), runner=recorder)
    seen = [(argv, env) for argv, _, env in recorder.calls]
    assert seen and all("never-in-argv" not in " ".join(argv) for argv, _ in seen)
    header = [value for _, env in seen for key, value in env.items() if key.startswith("GIT_CONFIG_VALUE_")]
    assert header and all(value.startswith("AUTHORIZATION: basic ") for value in header)
    assert all("GH_TOKEN" not in env for _, env in seen)


def test_local_diff_runs_git_through_the_injected_runner(remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    recorder = Recorder()
    text = gitdiff.local_diff(repo, base, head, runner=recorder)
    assert text == gitdiff.local_diff(repo, base, head)
    assert [argv for argv, _, _ in recorder.calls] == [
        ["git", "--version"],
        ["git", f"--attr-source={gitdiff.EMPTY_TREE}", "diff", *gitdiff.DIFF_FLAGS, base, head],
    ]
    assert recorder.calls[1][1] == repo


def test_build_runs_every_git_command_through_the_injected_runner(remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    recorder = Recorder()
    built = gitdiff.build("owner/name", base, head, remote=repo.as_uri(), token="", runner=recorder)
    commands = [argv[1] for argv, _, _ in recorder.calls[1:]]
    assert recorder.calls[0][0] == ["git", "--version"]
    assert commands == [
        "init",
        "remote",
        "fetch",
        f"--attr-source={gitdiff.EMPTY_TREE}",
        f"--attr-source={gitdiff.EMPTY_TREE}",
    ]
    assert built.paths == {"keep.py", "moved.py", "renamed.py", "gone.py", "added.py"}


def test_a_runner_that_cannot_start_git_is_reported() -> None:
    def broken(argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> Any:
        raise OSError("no git")

    with pytest.raises(gitdiff.GitVersionError, match="no git"):
        gitdiff.local_diff(Path("."), "a", "b", runner=broken)


def test_name_status_paths_include_both_sides_of_a_rename() -> None:
    listing = "M\0a.py\0R090\0old.py\0new.py\0D\0gone.py\0A\0added.py\0"
    assert gitdiff.name_status_paths(listing) == {"a.py", "old.py", "new.py", "gone.py", "added.py"}


HOSTILE_ATTRIBUTES = [
    "* -diff\n",
    "*.py diff=evil\n[diff]\n",
    "* binary\n",
    "* diff=nonexistent-driver\n",
    "*.py -text\n*.py eol=crlf\n",
]


def _attribute_repo(tmp_path: Path, attributes: str | None) -> tuple[Path, str, str]:
    repo = tmp_path / "attrs"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "mod.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "mod.py").write_text("a = 1\nb = 3\n", encoding="utf-8")
    if attributes is not None:
        (repo / ".gitattributes").write_text(attributes, encoding="utf-8")
    return repo, base, _commit(repo, "head")


def _section(diff: str, path: str) -> str:
    return next(part for part in diff.split("diff --git ") if part.startswith(f"a/{path} "))


@pytest.mark.parametrize("attributes", HOSTILE_ATTRIBUTES)
def test_head_tree_attributes_never_change_the_built_diff(tmp_path: Path, attributes: str) -> None:
    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    plain, plain_base, plain_head = _attribute_repo(plain_root, None)
    hostile_root = tmp_path / "hostile"
    hostile_root.mkdir()
    hostile, base, head = _attribute_repo(hostile_root, attributes)
    expected = _section(gitdiff.local_diff(plain, plain_base, plain_head), "mod.py")
    assert "+b = 3" in expected
    local_text = gitdiff.local_diff(hostile, base, head)
    hosted = gitdiff.build("owner/name", base, head, remote=hostile.as_uri(), token="")
    assert _section(local_text, "mod.py") == expected
    assert _section(hosted.text, "mod.py") == expected


@pytest.mark.parametrize(
    ("banner", "accepted"),
    [
        ("git version 2.39.3 (Apple Git-146)", False),
        ("git version 2.39.9", False),
        ("git version 2.40.0", True),
        ("git version 2.54.0 (Apple Git-157)", True),
        ("git version 3.0.1", True),
        ("git version 1.9.5", False),
        ("not git output", False),
        ("", False),
    ],
)
def test_diff_arguments_require_attr_source_support_and_never_drop_the_flag(banner: str, accepted: bool) -> None:
    recorder = Recorder(banner)
    if accepted:
        for arguments in (
            gitdiff.diff_arguments("a", "b", recorder),
            gitdiff.name_status_arguments("a", "b", recorder),
        ):
            assert arguments[0] == f"--attr-source={gitdiff.EMPTY_TREE}"
        return
    for call in (gitdiff.diff_arguments, gitdiff.name_status_arguments):
        with pytest.raises(gitdiff.GitVersionError, match="2.40"):
            call("a", "b", recorder)


def test_an_old_git_stops_both_builders_before_any_command_runs(remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    recorder = Recorder("git version 2.39.3 (Apple Git-146)")
    with pytest.raises(gitdiff.GitVersionError):
        gitdiff.local_diff(repo, base, head, runner=recorder)
    with pytest.raises(gitdiff.GitVersionError):
        gitdiff.build("owner/name", base, head, remote=repo.as_uri(), token="", runner=recorder)
    assert {tuple(argv) for argv, _, _ in recorder.calls} == {("git", "--version")}


def test_the_git_version_is_probed_once_per_runner() -> None:
    recorder = Recorder("git version 2.54.0")
    for _ in range(3):
        gitdiff.diff_arguments("a", "b", recorder)
        gitdiff.name_status_arguments("a", "b", recorder)
    assert [argv for argv, _, _ in recorder.calls] == [["git", "--version"]]
