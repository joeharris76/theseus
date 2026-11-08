from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from theseus_reviewer import isolation

pytestmark = [pytest.mark.unit, pytest.mark.fast]


LINUX_USER = {"environ": {}, "platform": "linux", "uid": 1001}


def _proc(tmp_path: Path, scope: str = "1") -> Path:
    (tmp_path / "sys/kernel/yama").mkdir(parents=True)
    (tmp_path / "sys/kernel/yama/ptrace_scope").write_text(scope)
    (tmp_path / "self").mkdir()
    (tmp_path / "self/status").write_text("Uid:\t1001 1001 1001 1001\nCapEff:\t0\nCapPrm:\t0\nCapInh:\t0\nCapAmb:\t0\n")
    return tmp_path


@pytest.mark.parametrize("scope", ["1", "2", "3"])
def test_isolation_accepts_restricted_nonroot_configuration(tmp_path, scope) -> None:
    isolation.require_isolation(_proc(tmp_path, scope), **LINUX_USER)


@pytest.mark.parametrize("scope", ["0", "4", "garbage"])
def test_isolation_rejects_unsafe_or_unknown_scope(tmp_path, scope) -> None:
    with pytest.raises(ValueError):
        isolation.require_isolation(_proc(tmp_path, scope), **LINUX_USER)


def test_isolation_rejects_missing_controls(tmp_path) -> None:
    root = _proc(tmp_path)
    (root / "sys/kernel/yama/ptrace_scope").unlink()
    with pytest.raises(OSError):
        isolation.require_isolation(root, **LINUX_USER)


@pytest.mark.parametrize("field", ["CapEff", "CapPrm", "CapInh", "CapAmb"])
def test_isolation_rejects_ptrace_capability(tmp_path, field) -> None:
    root = _proc(tmp_path)
    status = root / "self/status"
    status.write_text(status.read_text(encoding="utf-8").replace(f"{field}:\t0", f"{field}:\t80000"))
    with pytest.raises(ValueError, match="CAP_SYS_PTRACE"):
        isolation.require_isolation(root, **LINUX_USER)


def test_isolation_rejects_a_present_id_token_request_token(tmp_path) -> None:
    root = _proc(tmp_path)
    with pytest.raises(ValueError, match="ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        isolation.require_isolation(root, **{**LINUX_USER, "environ": {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x"}})


def test_isolation_main_fails_when_the_process_holds_an_id_token_request_token() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer.isolation"],
        env={**os.environ, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in proc.stderr


def test_isolation_accepts_an_absent_id_token_request_token(tmp_path) -> None:
    root = _proc(tmp_path)
    isolation.require_isolation(root, **LINUX_USER)


def test_isolation_rejects_root(tmp_path) -> None:
    root = _proc(tmp_path)
    status = root / "self/status"
    status.write_text(status.read_text(encoding="utf-8").replace("1001", "0"))
    with pytest.raises(ValueError, match="non-root"):
        isolation.require_isolation(root, **LINUX_USER)


@pytest.mark.skipif(sys.platform != "linux", reason="the permanent runner prerequisite is Linux-only")
def test_live_linux_prerequisite_and_sibling_memory_denial() -> None:
    if not Path("/proc/sys/kernel/yama/ptrace_scope").exists() and not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("this kernel has no Yama LSM (no /proc/sys/kernel/yama/ptrace_scope); CI runners provide it")
    isolation.require_isolation()
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as sibling:
        try:
            script = (
                "import sys\n"
                "try:\n"
                "    open('/proc/' + sys.argv[1] + '/mem', 'rb')\n"
                "except PermissionError:\n"
                "    sys.exit(0)\n"
                "sys.exit(1)\n"
            )
            result = subprocess.run([sys.executable, "-c", script, str(sibling.pid)], check=False)
            assert result.returncode == 0
            assert os.getuid() != 0
        finally:
            sibling.terminate()
            sibling.wait(timeout=10)
