from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_token_travels_only_in_the_environment(monkeypatch: pytest.MonkeyPatch, remote: tuple[Path, str, str]) -> None:
    repo, base, head = remote
    seen: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append((argv, dict(kwargs.get("env") or {})))  # type: ignore[call-overload]
        return real(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(gitdiff.subprocess, "run", spy)
    monkeypatch.setenv("GH_TOKEN", "never-in-argv")
    gitdiff.build("owner/name", base, head, remote=repo.as_uri())
    assert seen and all("never-in-argv" not in " ".join(argv) for argv, _ in seen)
    header = [value for _, env in seen for key, value in env.items() if key.startswith("GIT_CONFIG_VALUE_")]
    assert header and all(value.startswith("AUTHORIZATION: basic ") for value in header)
    assert all("GH_TOKEN" not in env for _, env in seen)


def test_name_status_paths_include_both_sides_of_a_rename() -> None:
    listing = "M\0a.py\0R090\0old.py\0new.py\0D\0gone.py\0A\0added.py\0"
    assert gitdiff.name_status_paths(listing) == {"a.py", "old.py", "new.py", "gone.py", "added.py"}
