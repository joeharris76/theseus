from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ROOT = Path(__file__).resolve().parents[3]
REVIEW_TEST = "tests/unit/reviewer/test_local_review.py"
SMART_QUOTE = "\u201d"


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "commit.gpgsign", "false")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture", "--quiet")
    return path


LEGACY_LOCALE_PROBE = """
import locale, sys
from pathlib import Path
assert locale.getpreferredencoding(False).lower() not in ("utf-8", "utf8"), locale.getpreferredencoding(False)
from theseus_reviewer import local
repo = Path(sys.argv[1])
"""


def _legacy_locale(body: str, repo: Path) -> subprocess.CompletedProcess[str]:
    """Run `body` in a child whose locale encoding is not UTF-8 (the Windows cp1252 failure mode)."""
    env = {
        **os.environ,
        "MUTANT_UNDER_TEST": "",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "ascii:backslashreplace",
    }
    return subprocess.run(
        [sys.executable, "-c", LEGACY_LOCALE_PROBE + body, str(repo)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_local_imports_without_fcntl() -> None:
    probe = "import sys; sys.modules['fcntl'] = None; import theseus_reviewer.local"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "MUTANT_UNDER_TEST": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_explicit_collection_skips_without_fcntl() -> None:
    probe = (
        "import sys; sys.modules['fcntl'] = None; import pytest; "
        "raise SystemExit(pytest.main(['"
        + REVIEW_TEST
        + "', '-q', '--no-header', '-p', 'no:cacheprovider', '-o', 'addopts=']))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "MUTANT_UNDER_TEST": ""},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    assert "skipped" in proc.stdout.lower()
    assert "error" not in proc.stdout.lower()


def test_git_show_decodes_smart_quotes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"note.md": f"quoted {SMART_QUOTE}text{SMART_QUOTE}\n"})
    proc = _legacy_locale("print(ascii(local._git(repo, 'show', 'HEAD:note.md')))", repo)
    assert proc.returncode == 0, proc.stderr
    assert "\\u201d" in proc.stdout


def test_repository_remote_decodes_smart_quotes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"note.md": "plain\n"})
    _git(repo, "remote", "add", "origin", f"https://github.com/acme/r{SMART_QUOTE}epo")
    body = "try:\n    local._repository(repo)\nexcept local.Refused as exc:\n    print('refused:', exc)\n"
    proc = _legacy_locale(body, repo)
    assert proc.returncode == 0, proc.stderr
    assert "cannot tell which GitHub repository" in proc.stdout
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/repo.git")
    proc = _legacy_locale("print(local._repository(repo))", repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "acme/repo"
