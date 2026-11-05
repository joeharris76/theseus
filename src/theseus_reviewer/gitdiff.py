from __future__ import annotations

import base64
import functools
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GLOBAL_FLAGS = (f"--attr-source={EMPTY_TREE}",)
FETCH_FLAGS = ("--no-tags", "--depth=1", "--filter=blob:none")
DEFAULT_SERVER = "https://github.com"
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
UNBUILT_REASON = "the pull request diff could not be built"
MIN_GIT_VERSION = (2, 40)
MIN_GIT_VERSION_TEXT = "2.40"


class GitDiffError(RuntimeError):
    pass


class GitVersionError(GitDiffError):
    pass


@dataclass(frozen=True)
class BuiltDiff:
    text: str
    paths: frozenset[str]


def _version_text() -> str:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, check=False)
    except OSError as exc:
        raise GitVersionError(f"git could not run: {exc}") from exc
    return result.stdout.decode("utf-8", errors="replace").strip()


@functools.cache
def _probed_version() -> str:
    return _version_text()


def require_attr_source() -> None:
    match = re.match(r"git version (\d+)\.(\d+)", _probed_version())
    if match is None:
        raise GitVersionError(
            f"the git version could not be determined; git {MIN_GIT_VERSION_TEXT} or newer is required"
        )
    if (int(match.group(1)), int(match.group(2))) < MIN_GIT_VERSION:
        raise GitVersionError(f"git {MIN_GIT_VERSION_TEXT} or newer is required ({_probed_version()})")


def diff_arguments(base: str, head: str) -> list[str]:
    require_attr_source()
    return [*GLOBAL_FLAGS, "diff", *DIFF_FLAGS, base, head]


def name_status_arguments(base: str, head: str) -> list[str]:
    require_attr_source()
    return [*GLOBAL_FLAGS, "diff", "--name-status", "-z", "-M", base, head]


def name_status_paths(output: str) -> frozenset[str]:
    tokens = output.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens) and tokens[index]:
        width = 3 if tokens[index][0] in "RC" else 2
        paths.update(token for token in tokens[index + 1 : index + width] if token)
        index += width
    return frozenset(paths)


def _run(args: list[str], cwd: Path, env: Mapping[str, str] | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False, env=None if env is None else dict(env)
        )
    except OSError as exc:
        raise GitDiffError(f"git could not run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        command = next(arg for arg in args if not arg.startswith("-"))
        raise GitDiffError(f"git {command} failed: {detail}")
    return result.stdout


def local_diff(root: Path, base: str, head: str) -> str:
    return _run(diff_arguments(base, head), root).decode("utf-8", errors="replace")


def _git_environment(server: str, token: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in ("GIT_DIR", "GIT_WORK_TREE", *TOKEN_ENV)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        env[f"GIT_CONFIG_KEY_{count}"] = f"http.{server.rstrip('/')}/.extraheader"
        env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {credential}"
    return env


def build(repo: str, merge_base: str, head: str, *, remote: str | None = None, token: str | None = None) -> BuiltDiff:
    require_attr_source()
    server = os.environ.get("GITHUB_SERVER_URL", DEFAULT_SERVER)
    url = remote if remote is not None else f"{server.rstrip('/')}/{repo}.git"
    secret = token if token is not None else next((os.environ[name] for name in TOKEN_ENV if os.environ.get(name)), "")
    env = _git_environment(server, secret)
    parent = os.environ.get("RUNNER_TEMP") or None
    workdir = Path(tempfile.mkdtemp(prefix="oracle-git-diff-", dir=parent))
    try:
        _run(["init", "--bare", "--quiet"], workdir, env)
        _run(["remote", "add", "origin", url], workdir, env)
        _run(["fetch", *FETCH_FLAGS, "origin", merge_base, head], workdir, env)
        text = _run(diff_arguments(merge_base, head), workdir, env).decode("utf-8", errors="replace")
        listing = _run(name_status_arguments(merge_base, head), workdir, env).decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return BuiltDiff(text, name_status_paths(listing))


def verify_paths(built: BuiltDiff, api_paths: Iterable[str]) -> None:
    expected = frozenset(api_paths)
    if built.paths != expected:
        raise GitDiffError("the git diff lists different paths than the pull request files")
