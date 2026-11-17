from __future__ import annotations

import os
import pickle
import shutil
import stat
import subprocess
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


def test_a_failed_file_write_leaves_the_old_value_and_no_stray_file(tmp_path: Path) -> None:
    def failing(src: str, dst: str) -> None:
        raise OSError("disk full")

    directory = tmp_path / "store"
    FileStore(directory).put("A", Secret("old-value-aaaa"))
    store = FileStore(directory, replace=failing)
    with pytest.raises(OSError):
        store.put("A", Secret("new-value-bbbb"))
    assert store.get("A") == Secret("old-value-aaaa")
    assert store.names() == ["A"]
    assert sorted(entry.name for entry in directory.iterdir()) == ["A"]


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


def _full_size_pem() -> str:
    if shutil.which("openssl") is None:
        return PEM * 40
    return subprocess.run(["openssl", "genrsa", "4096"], capture_output=True, text=True, check=True).stdout


LIVE = pytest.mark.skipif(
    os.environ.get("THESEUS_INFRA_LIVE_KEYCHAIN") != "1" or not shutil.which("security"),
    reason="opt-in: set THESEUS_INFRA_LIVE_KEYCHAIN=1 on macOS to run against a temporary keychain",
)


@pytest.fixture
def live_keychain(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A temporary keychain. The user's search list is restored and the keychain deleted, even on failure."""
    listed = subprocess.run(["security", "list-keychains", "-d", "user"], capture_output=True, text=True, check=True)
    before = [line.strip().strip('"') for line in listed.stdout.splitlines() if line.strip()]
    keychain = str(tmp_path / "temp.keychain-db")
    try:
        subprocess.run(["security", "create-keychain", "-p", "temp-pass", keychain], check=True)
        subprocess.run(["security", "unlock-keychain", "-p", "temp-pass", keychain], check=True)
        yield keychain
    finally:
        subprocess.run(["security", "delete-keychain", keychain], check=False)
        subprocess.run(["security", "list-keychains", "-d", "user", "-s", *before], check=True)
        after = subprocess.run(["security", "list-keychains", "-d", "user"], capture_output=True, text=True, check=True)
        assert [line.strip().strip('"') for line in after.stdout.splitlines() if line.strip()] == before


@LIVE
@pytest.mark.parametrize("kind", ["awkward", "pem4096", "long-unicode", "very-long"])
def test_live_keychain_round_trips_through_a_temporary_keychain(live_keychain: str, kind: str) -> None:
    values = {
        "awkward": "\n".join(AWKWARD[:7]),
        "pem4096": _full_size_pem(),
        "long-unicode": "ünïcödé-✓-" * 600,
        "very-long": "x" * 20000,
    }
    value = values[kind]
    store = KeychainStore(keychain=live_keychain)
    store.put("LIVE_ITEM", Secret("old-value-that-must-survive"))
    store.put("LIVE_ITEM", Secret(value))
    got = store.get("LIVE_ITEM")
    assert got is not None and got.reveal() == value
    assert store.names() == ["LIVE_ITEM"] and store.modified_at("LIVE_ITEM") is not None
    store.delete("LIVE_ITEM")
    assert store.get("LIVE_ITEM") is None


@pytest.mark.parametrize("size", [1999, 2000, 2001, 4000, 4500, 20000, 100000])
def test_a_value_longer_than_a_security_line_is_kept_in_parts(size: int) -> None:
    fake = FakeSecurity()
    store = KeychainStore(fake)
    value = "ünï" * size
    store.put("A", Secret(value))
    assert store.get("A") == Secret(value)
    assert store.names() == ["A"]
    assert max(len(line) for call in fake.argv_seen for line in call) < 4096
    store.put("A", Secret("short-again"))
    assert store.get("A") == Secret("short-again")
    assert [account for _, account in fake.items] == ["A"]
    store.put("A", Secret(value))
    store.delete("A")
    assert fake.items == {} and store.get("A") is None


def test_a_failed_long_write_keeps_the_old_value_and_leaves_no_parts() -> None:
    fake = FakeSecurity()
    store = KeychainStore(fake)
    old = "old-" + "o" * 5000
    store.put("A", Secret(old))
    before = dict(fake.items)
    for allowed in range(0, 4):
        fake.fail_writes_after = allowed
        with pytest.raises(StoreError):
            store.put("A", Secret("n" * 9000))
        fake.fail_writes_after = None
        assert store.get("A") == Secret(old)
        assert fake.items == before


def test_a_value_that_looks_like_a_parts_header_is_stored_as_itself() -> None:
    store = KeychainStore(FakeSecurity())
    store.put("A", Secret("chunks:deadbeef:3"))
    assert store.get("A") == Secret("chunks:deadbeef:3")


def test_a_failure_at_every_part_write_and_at_the_header_keeps_the_old_value() -> None:
    fake = FakeSecurity()
    store = KeychainStore(fake)
    old = "old-" + "o" * 5000
    store.put("A", Secret(old))
    before = dict(fake.items)
    new = "n" * 11000
    parts = -(-len(new) // 2000)
    assert parts >= 5
    for allowed in range(0, parts + 1):  # 0..parts-1 fail a part write; parts fails the header write
        fake.fail_writes_after = allowed
        with pytest.raises(StoreError):
            store.put("A", Secret(new))
        fake.fail_writes_after = None
        assert store.get("A") == Secret(old)
        assert fake.items == before
    store.put("A", Secret(new))
    assert store.get("A") == Secret(new)
