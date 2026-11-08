from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import github


def test_the_newest_unexpired_artifact_run_is_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = {
        "artifacts": [
            {"workflow_run": {"id": 4}, "expired": False},
            {"workflow_run": {"id": 9}, "expired": True},
            {"workflow_run": {"id": 7}, "expired": False},
        ]
    }
    monkeypatch.setattr(github, "get_json", lambda path: listing)

    assert github.latest_state_run("o/r", 3) == "7"


def test_no_unexpired_artifact_means_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "get_json", lambda path: {"artifacts": []})

    assert github.latest_state_run("o/r", 3) is None


def test_a_downloaded_state_artifact_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        directory = Path(argv[argv.index("-D") + 1])
        (directory / github.STATE_FILE).write_text(json.dumps({"pr": 3}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.download_state("o/r", "7", tmp_path) == {"pr": 3}


def test_a_failed_download_yields_no_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "no artifact")

    monkeypatch.setattr(github.subprocess, "run", failing_run)

    assert github.download_state("o/r", "7", tmp_path) is None
