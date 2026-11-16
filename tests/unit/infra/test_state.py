from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from theseus_reviewer.infra.state import State, StateError

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def test_a_new_state_round_trips_ids_and_versions(tmp_path: Path) -> None:
    state = State.load(tmp_path / "dir" / "state.json")
    assert state.version("A") == 0 and state.delivered("A", "o/r:env") == 0
    assert state.bump("A") == 1
    state.mark_delivered("A", "o/r:env")
    state.set_provider("tailscale_oauth", client_id="kabc123")
    state.save()
    again = State.load(state.path)
    assert again.version("A") == 1 and again.delivered("A", "o/r:env") == 1
    assert again.provider("tailscale_oauth") == {"client_id": "kabc123"}
    assert again.environments("A") == ["o/r:env"]
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.path.parent.stat().st_mode) == 0o700


def test_a_bump_makes_every_environment_stale_again(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    state.bump("A")
    state.mark_delivered("A", "e1")
    state.bump("A")
    assert state.delivered("A", "e1") < state.version("A")
    state.mark_delivered("A", "e1")
    assert state.delivered("A", "e1") == state.version("A") == 2


def test_the_state_file_has_only_the_documented_shape(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    state.bump("A")
    state.mark_delivered("A", "e1")
    state.save()
    assert json.loads(state.path.read_text()) == {
        "schema": 1,
        "secrets": {"A": {"version": 1, "environments": {"e1": 1}}},
        "providers": {},
    }


def test_the_state_refuses_to_write_a_known_secret_value(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    state.bump("A")
    state.secrets["A"]["note"] = "SENTINEL-value-9999"
    with pytest.raises(StateError, match="refusing"):
        state.save(["SENTINEL-value-9999"])
    assert not state.path.exists()


def test_a_damaged_or_foreign_state_file_is_an_error_not_a_reset(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        State.load(path)
    path.write_text(json.dumps({"schema": 9}), encoding="utf-8")
    with pytest.raises(StateError, match="schema 1"):
        State.load(path)


def test_a_provider_id_that_equals_a_stored_value_is_not_a_leak(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    state.set_provider("tailscale", client_id="kclient-abc123")
    state.save(["kclient-abc123"])
    assert json.loads(state.path.read_text())["providers"]["tailscale"] == {"client_id": "kclient-abc123"}
    state.bump("OTHER")
    state.secrets["OTHER"]["note"] = "SENTINEL-leaked-0001"
    with pytest.raises(StateError, match="refusing"):
        state.save(["kclient-abc123", "SENTINEL-leaked-0001"])
