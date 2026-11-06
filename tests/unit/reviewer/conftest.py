from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import ALPHA_POLICY as POLICY_PATH
from theseus_reviewer.policy import Policy, load_policy


@pytest.fixture(scope="session")
def policy() -> Policy:
    return load_policy(POLICY_PATH)


@pytest.fixture
def consumer_cwd(monkeypatch: pytest.MonkeyPatch, alpha_root: Path) -> Path:
    monkeypatch.chdir(alpha_root)
    return alpha_root


@pytest.fixture(scope="session")
def codex_enabled_policy() -> Policy:
    policy = load_policy(POLICY_PATH)
    reviewers = {
        name: replace(reviewer, enabled=True, disabled_reason="") if reviewer.family == "codex" else reviewer
        for name, reviewer in policy.reviewers.items()
    }
    return replace(policy, reviewers=reviewers)


collect_ignore = ["test_local_review.py"] if sys.platform == "win32" else []

WINDOWS_SKIP = pytest.mark.skip(
    reason="the reviewer tooling runs on Linux Actions runners: it uses POSIX paths, process groups and shebang CLIs"
)


def pytest_collection_modifyitems(config, items):
    if sys.platform != "win32":
        return
    for item in items:
        if Path(__file__).parent in Path(str(item.path)).parents:
            item.add_marker(WINDOWS_SKIP)


PR_GET_CHILD_SUBREAPER = 37


def _is_subreaper() -> bool:
    import ctypes

    flag = ctypes.c_int(0)
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) == 0
    return bool(flag.value)


@pytest.fixture(autouse=True)
def _worker_is_never_a_subreaper():
    linux = sys.platform.startswith("linux")
    assert not linux or not _is_subreaper(), "the pytest worker is already a child subreaper"
    yield
    assert not linux or not _is_subreaper(), "a test made the pytest worker a child subreaper"
