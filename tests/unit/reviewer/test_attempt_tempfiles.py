from __future__ import annotations

import sys
from pathlib import Path

import pytest

from theseus_reviewer import omp_pin
from theseus_reviewer.policy import Policy

from .test_attempt_loop import Loop

pytestmark = [pytest.mark.unit, pytest.mark.fast]


@pytest.fixture
def loop(tmp_path: Path, policy: Policy, monkeypatch: pytest.MonkeyPatch) -> Loop:
    return Loop(tmp_path, policy, monkeypatch)


def test_real_execute_survives_deleted_cached_tempdir_across_slots(loop: Loop, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = loop.tmp_path / "deleted-cache"
    cached.mkdir()
    cached.rmdir()
    journal = loop.journal("homes")
    loop.fake("deleted_tempdir", path=str(cached))
    loop.fake("home_recording_build", journal=str(journal.path))
    monkeypatch.setenv(omp_pin.BINARY_ENV, sys.executable)
    assert loop.run_with_recovery() == 0
    homes = journal.items()
    assert len(homes) == 2 and homes[0] != homes[1]
    for slot in (1, 2):
        artifact = loop.artifact(slot)
        assert artifact["absence"]["kind"] == "invalid"
        assert "job did not finish" not in artifact["absence"]["detail"]
