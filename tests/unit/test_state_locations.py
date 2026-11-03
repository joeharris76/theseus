from __future__ import annotations

from pathlib import Path

import pytest

from theseus_reviewer import local, omp_pin

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def test_defaults_are_neutral_and_never_name_a_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("THESEUS_LOCAL_STATE", raising=False)
    monkeypatch.delenv(omp_pin.CACHE_ENV, raising=False)
    assert local.state_dir() == tmp_path / ".cache" / "theseus" / "local-oracle"
    assert omp_pin.cache_root() == tmp_path / ".cache" / "theseus" / "omp"


def test_a_consumer_keeps_existing_findings_and_omp_cache_by_pointing_at_them(tmp_path, monkeypatch):
    existing = tmp_path / "existing-state"
    cache = tmp_path / "existing-omp"
    monkeypatch.setenv("THESEUS_LOCAL_STATE", str(existing))
    monkeypatch.setenv(omp_pin.CACHE_ENV, str(cache))
    assert local.state_dir() == existing
    assert omp_pin.cache_root() == Path(cache)


def test_persisted_formats_keep_their_identities():
    assert local.CACHE_SCHEMA == 2
    assert local.MEMBER_SCHEMA == "benchbox_member_review_v1"
