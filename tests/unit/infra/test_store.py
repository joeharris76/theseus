from __future__ import annotations

import os
import pickle
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from theseus_reviewer.infra.store import FileStore, KeychainStore, Secret, Store, StoreError

from .fakes import FakeSecurity

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n  indented \"quoted\" 'line'\n-----END RSA PRIVATE KEY-----\n"
AWKWARD = [
    "plainvalue123",
    PEM,
    "with space and 'single' and \"double\" quotes",
    "back\\slash $dollar `tick` ; & | <>",
    "b64:looks-encoded",
    "ünïcödé-✓",
    "trailing newline\n",
    "x" * 4000,
]


@pytest.fixture(params=["file", "keychain"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "file":
        return FileStore(tmp_path / "store")
    return KeychainStore(FakeSecurity())


def test_a_missing_name_is_none_everywhere(store: Store) -> None:
    assert store.get("NOPE") is None
    assert store.modified_at("NOPE") is None
    assert store.names() == []
    store.delete("NOPE")


@pytest.mark.parametrize("value", AWKWARD)
def test_every_value_round_trips_exactly(store: Store, value: str) -> None:
    store.put("A_NAME", Secret(value))
    got = store.get("A_NAME")
    assert got is not None and got.reveal() == value


def test_put_replaces_and_delete_removes(store: Store) -> None:
    store.put("A", Secret("first-value"))
    store.put("A", Secret("second-value"))
    assert store.get("A") == Secret("second-value")
    store.delete("A")
    assert store.get("A") is None
    assert store.names() == []


def test_names_list_every_item_including_pending_ones(store: Store) -> None:
    for name in ("B_ONE", "A_ONE", "A_ONE.pending"):
        store.put(name, Secret("value-" + name))
    assert store.names() == ["A_ONE", "A_ONE.pending", "B_ONE"]


def test_modified_at_is_a_recent_utc_time(store: Store) -> None:
    store.put("A", Secret("value-aaaa"))
    modified = store.modified_at("A")
    assert modified is not None and modified.tzinfo is not None
    if isinstance(store, FileStore):
        assert abs((datetime.now(UTC) - modified).total_seconds()) < 60
    else:
        assert modified == datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("name", ["", "has space", "../escape", "a/b", "x;y", "-leading", "é"])
def test_a_bad_name_is_refused_before_anything_runs(store: Store, name: str) -> None:
    with pytest.raises(StoreError):
        store.put(name, Secret("value-aaaa"))
    with pytest.raises(StoreError):
        store.get(name)


def test_the_file_store_makes_a_private_directory_and_files(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "deep" / "store")
    store.put("A", Secret("value-aaaa"))
    assert stat.S_IMODE((tmp_path / "deep" / "store").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "deep" / "store" / "A").stat().st_mode) == 0o600
    assert [entry.name for entry in (tmp_path / "deep" / "store").iterdir()] == ["A"]


def test_the_file_store_refuses_a_wider_mode_instead_of_repairing_it(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    store = FileStore(directory)
    store.put("A", Secret("value-aaaa"))
    (directory / "A").chmod(0o644)
    with pytest.raises(StoreError, match="0600"):
        store.get("A")
    (directory / "A").chmod(0o600)
    directory.chmod(0o755)
    for call in (lambda: store.get("A"), lambda: store.put("B", Secret("value-bbbb")), store.names):
        with pytest.raises(StoreError, match="0700"):
            call()
    assert stat.S_IMODE((directory / "A").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_a_failed_file_write_leaves_the_old_value_and_no_stray_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileStore(tmp_path / "store")
    store.put("A", Secret("old-value-aaaa"))
    real_replace: Callable[..., None] = os.replace

    def failing(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        store.put("A", Secret("new-value-bbbb"))
    monkeypatch.setattr(os, "replace", real_replace)
    assert store.get("A") == Secret("old-value-aaaa")
    assert store.names() == ["A"]
    assert sorted(entry.name for entry in (tmp_path / "store").iterdir()) == ["A"]


def test_the_keychain_never_puts_a_value_in_argv() -> None:
    fake = FakeSecurity()
    store = KeychainStore(fake)
    for value in AWKWARD:
        store.put("A", Secret(value))
        store.get("A")
        store.modified_at("A")
        store.names()
    for argv in fake.argv_seen:
        assert not any(value in part for value in AWKWARD for part in argv if len(value) > 8)
    assert ("security", "-i") in fake.argv_seen


def test_the_keychain_reads_a_plain_value_an_owner_seeded_with_security_directly() -> None:
    fake = FakeSecurity()
    fake.items[("theseus.infra", "TAILSCALE_ROOT_CLIENT_ID")] = ("plain-id-123", fake.clock)
    assert KeychainStore(fake).get("TAILSCALE_ROOT_CLIENT_ID") == Secret("plain-id-123")


def test_the_keychain_ignores_other_services_when_listing() -> None:
    fake = FakeSecurity()
    fake.items[("other.service", "NOT_OURS")] = ("value-aaaa", fake.clock)
    store = KeychainStore(fake)
    store.put("OURS", Secret("value-bbbb"))
    assert store.names() == ["OURS"]


def test_a_keychain_write_that_security_reports_without_failing_is_still_an_error() -> None:
    class Silent(FakeSecurity):
        def _interactive(self, stdin: str):  # type: ignore[no-untyped-def]
            return super()._interactive("")

    with pytest.raises(StoreError, match="did not keep"):
        KeychainStore(Silent()).put("A", Secret("value-aaaa"))


def test_a_damaged_encoded_item_is_reported_without_its_text() -> None:
    fake = FakeSecurity()
    fake.items[("theseus.infra", "A")] = ("b64:notbase64a", fake.clock)
    with pytest.raises(StoreError) as caught:
        KeychainStore(fake).get("A")
    assert "notbase64a" not in str(caught.value)


def test_a_secret_shows_nothing_however_it_is_formatted() -> None:
    secret = Secret("SENTINEL-value-1234")
    shown = [
        repr(secret),
        str(secret),
        f"{secret}",
        f"{secret!r}",
        repr([secret]),
        repr({"k": secret}),
    ]  # noqa: UP032
    assert all("SENTINEL" not in text for text in shown)
    assert all("[redacted]" in text for text in shown)
    with pytest.raises(AttributeError):
        secret._value = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    assert secret.reveal() == "SENTINEL-value-1234"
