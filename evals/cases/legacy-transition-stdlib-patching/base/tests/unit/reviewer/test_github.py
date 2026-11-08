from __future__ import annotations

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
