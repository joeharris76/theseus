"""Where credentials live between runs: the macOS Keychain or a private directory.

``Secret`` wraps every value so an accidental print, format or log shows ``[redacted]``. ``reveal`` is the only
way out and is called where the value is handed to a child's stdin or an HTTP body.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
import tempfile
import urllib.parse
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .. import credentials
from .transport import Runner, run_command

KEYCHAIN_SERVICE = "theseus.infra"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
PENDING_SUFFIX = ".pending"
_ENCODED = "b64:"
_CHUNKS = "chunks:"
_HEADER = re.compile(r"chunks:([0-9a-f]{8}):([0-9]+)")
CHUNK = 2000
_PLAIN = re.compile(r"[A-Za-z0-9._~+/=@:-]+")
_MDAT = re.compile(r'"mdat"<timedate>=(?:0x[0-9A-Fa-f]+\s+)?"(\d{14})')
_STORE_DIR_MODE = 0o700
_STORE_FILE_MODE = 0o600


class StoreError(RuntimeError):
    pass


class Secret:
    """A credential value. ``str``, ``repr``, ``format`` and f-strings all give ``[redacted]``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Secret is immutable")

    def reveal(self) -> str:
        value: str = object.__getattribute__(self, "_value")
        return value

    def __repr__(self) -> str:
        return credentials.REDACTED

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return credentials.REDACTED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self.reveal() == other.reveal()

    def __hash__(self) -> int:
        return hash(("Secret", self.reveal()))

    def __bool__(self) -> bool:
        return bool(self.reveal())

    def __reduce__(self) -> tuple[type, tuple[str]]:
        raise TypeError("a Secret is never serialised")


class Store(Protocol):
    def get(self, name: str) -> Secret | None: ...

    def put(self, name: str, value: Secret) -> None: ...

    def delete(self, name: str) -> None: ...

    def modified_at(self, name: str) -> datetime | None: ...

    def names(self) -> list[str]: ...


SHARED_NAMES = frozenset({"TAILSCALE_ROOT_CLIENT_ID", "TAILSCALE_ROOT_CLIENT_SECRET", "OPENAI_ADMIN_KEY"})
_SCOPED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+:[A-Za-z0-9][A-Za-z0-9._-]*:" + NAME_PATTERN.pattern)
_LEFTOVER_SUFFIXES = (".next", ".pending", ".previous")


def check_name(name: str) -> str:
    if NAME_PATTERN.fullmatch(name) is None and _SCOPED.fullmatch(name) is None:
        raise StoreError(f"{name!r} is not a valid credential name")
    return name


class FileStore:
    """A 0700 directory with one 0600 file per name. A wider mode is refused, never repaired."""

    def __init__(self, directory: Path, replace: Callable[[str, str], None] = os.replace) -> None:
        self.directory = directory
        self._replace = replace

    def _checked_dir(self, create: bool) -> Path | None:
        directory = self.directory
        if not directory.exists():
            if not create:
                return None
            directory.mkdir(mode=_STORE_DIR_MODE, parents=True)
            directory.chmod(_STORE_DIR_MODE)
        mode = stat.S_IMODE(directory.stat().st_mode)
        if not directory.is_dir() or mode & 0o077:
            raise StoreError(f"{directory} must be a directory with mode 0700, not {mode:04o}")
        return directory

    def _path(self, name: str) -> Path:
        return self.directory / urllib.parse.quote(check_name(name), safe="")

    def get(self, name: str) -> Secret | None:
        if self._checked_dir(False) is None:
            return None
        path = self._path(name)
        if not path.exists():
            return None
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise StoreError(f"{path} must have mode 0600, not {mode:04o}")
        return Secret(path.read_text(encoding="utf-8"))

    def put(self, name: str, value: Secret) -> None:
        directory = self._checked_dir(True)
        assert directory is not None
        path = self._path(name)
        fd, staged = tempfile.mkstemp(prefix=".put-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), _STORE_FILE_MODE)
                handle.write(value.reveal())
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(staged, str(path))
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> None:
        if self._checked_dir(False) is not None:
            self._path(name).unlink(missing_ok=True)

    def modified_at(self, name: str) -> datetime | None:
        if self._checked_dir(False) is None:
            return None
        path = self._path(name)
        return datetime.fromtimestamp(path.stat().st_mtime, UTC) if path.exists() else None

    def names(self) -> list[str]:
        if self._checked_dir(False) is None:
            return []
        found = (urllib.parse.unquote(entry.name) for entry in self.directory.iterdir() if entry.is_file())
        return sorted(name for name in found if NAME_PATTERN.fullmatch(name) or _SCOPED.fullmatch(name))


def encode(value: str) -> str:
    """The Keychain form of a value: as is when it is plain, else base64 behind a prefix.

    ``security -w`` prints a value with control characters or non-ASCII bytes as hex, and ``security -i`` splits
    its command line on spaces and quotes, so only a plain value goes in unencoded.
    """
    if _PLAIN.fullmatch(value) is not None and not value.startswith((_ENCODED, _CHUNKS)):
        return value
    return _ENCODED + base64.b64encode(value.encode("utf-8")).decode("ascii")


def decode(stored: str) -> str:
    if not stored.startswith(_ENCODED):
        return stored
    try:
        return base64.b64decode(stored[len(_ENCODED) :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise StoreError("a Keychain item is damaged: its base64 value does not decode") from exc


class KeychainStore:
    """Generic passwords under service ``theseus.infra``, one account per name.

    A write feeds ``add-generic-password`` to ``security -i`` on stdin, so the value is never in argv. The write
    is read back, because ``security -i`` reports a failed command without a failing exit status.
    """

    def __init__(self, runner: Runner = run_command, keychain: str | None = None) -> None:
        self._run = runner
        self._keychain = keychain

    def _security(self, args: Sequence[str], *, stdin: str | None = None) -> tuple[int, str, str]:
        argv = ["security", *args]
        result = self._run(argv, stdin=stdin)
        return result.returncode, result.stdout, result.stderr

    def _tail(self) -> list[str]:
        return [self._keychain] if self._keychain else []

    def _read(self, account: str) -> str | None:
        code, out, err = self._security(
            ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", *self._tail()]
        )
        if code == 44 or "could not be found" in err:
            return None
        if code != 0:
            raise StoreError(f"security find-generic-password failed with exit {code}")
        return out.removesuffix("\n")

    def _write(self, account: str, text: str) -> None:
        """One item. ``security -i`` reads at most 4096 bytes of a line, so ``text`` is at most ``CHUNK`` long."""
        line = " ".join(
            ["add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", text, *self._tail()]
        )
        code, _, _ = self._security(["-i"], stdin=line + "\n")
        if code != 0 or self._read(account) != text:
            raise StoreError(f"the Keychain did not keep {account.split('#', 1)[0]}")

    def _remove(self, account: str) -> None:
        code, _, err = self._security(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, *self._tail()])
        if code not in (0, 44) and "could not be found" not in err:
            raise StoreError(f"security delete-generic-password failed with exit {code}")

    def _chunk_accounts(self, name: str, header: str | None) -> list[str]:
        match = _HEADER.fullmatch(header or "")
        if match is None:
            return []
        return [f"{name}#{match.group(1)}#{index}" for index in range(int(match.group(2)))]

    def get(self, name: str) -> Secret | None:
        check_name(name)
        stored = self._read(name)
        if stored is None:
            return None
        accounts = self._chunk_accounts(name, stored)
        if not accounts:
            return Secret(decode(stored))
        parts = [self._read(account) for account in accounts]
        if any(part is None for part in parts):
            raise StoreError(f"a Keychain item is damaged: part of {name} is missing")
        return Secret(decode("".join(part for part in parts if part is not None)))

    def put(self, name: str, value: Secret) -> None:
        """Write a value. A long one goes in numbered parts first and its header last, so an old value is replaced
        only by a complete one and a failed write leaves the old header and parts in place."""
        check_name(name)
        text = encode(value.reveal())
        old = self._chunk_accounts(name, self._read(name))
        if len(text) <= CHUNK:
            self._write(name, text)
        else:
            generation = uuid.uuid4().hex[:8]
            pieces = [text[start : start + CHUNK] for start in range(0, len(text), CHUNK)]
            accounts = [f"{name}#{generation}#{index}" for index in range(len(pieces))]
            try:
                for account, piece in zip(accounts, pieces, strict=True):
                    self._write(account, piece)
                self._write(name, f"{_CHUNKS}{generation}:{len(pieces)}")
            except StoreError:
                for account in accounts:
                    self._remove(account)
                raise
        for account in old:
            self._remove(account)

    def delete(self, name: str) -> None:
        check_name(name)
        for account in self._chunk_accounts(name, self._read(name)):
            self._remove(account)
        self._remove(name)

    def modified_at(self, name: str) -> datetime | None:
        check_name(name)
        code, out, err = self._security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, *self._tail()])
        if code == 44 or "could not be found" in err:
            return None
        match = _MDAT.search(out)
        if code != 0 or match is None:
            raise StoreError(f"cannot read the modification time of {name}")
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)

    def names(self) -> list[str]:
        code, out, _ = self._security(["dump-keychain", *self._tail()])
        if code != 0:
            raise StoreError(f"security dump-keychain failed with exit {code}")
        found: set[str] = set()
        for block in re.split(r"(?m)^keychain:", out):
            service = re.search(r'"svce"<blob>="([^"]*)"', block)
            account = re.search(r'"acct"<blob>="([^"]*)"', block)
            if service and account and service.group(1) == KEYCHAIN_SERVICE:
                if NAME_PATTERN.fullmatch(account.group(1)) or _SCOPED.fullmatch(
                    account.group(1)
                ):  # part items ("NAME#gen#n") are not names
                    found.add(account.group(1))
        return sorted(found)


class ScopedStore:
    """One repository's view of a store.

    Every name except the shared root credentials lives under ``<repository>:<environment>:<name>``, so two
    repositories that use the same secret names and the same Keychain or directory never see each other's values.
    An unprefixed item with a per-repository name is refused (``check_unscoped``), never read as this repository's.
    """

    def __init__(self, base: Store, repository: str, environment: str) -> None:
        self.base = base
        self._prefix = f"{repository}:{environment}:"

    def full_name(self, name: str) -> str:
        return name if name in SHARED_NAMES else self._prefix + check_name(name)

    def get(self, name: str) -> Secret | None:
        return self.base.get(self.full_name(name))

    def put(self, name: str, value: Secret) -> None:
        self.base.put(self.full_name(name), value)

    def delete(self, name: str) -> None:
        self.base.delete(self.full_name(name))

    def modified_at(self, name: str) -> datetime | None:
        return self.base.modified_at(self.full_name(name))

    def names(self) -> list[str]:
        found = self.base.names()
        mine = [name.removeprefix(self._prefix) for name in found if name.startswith(self._prefix)]
        return sorted([*mine, *(name for name in found if name in SHARED_NAMES)])

    def check_unscoped(self, configured: Sequence[str]) -> None:
        """Refuse when the base store holds an unprefixed item named like one of this configuration's secrets."""
        wanted = set(configured)
        for name in self.base.names():
            stem = name
            for suffix in _LEFTOVER_SUFFIXES:
                stem = stem.removesuffix(suffix)
            if name not in SHARED_NAMES and ":" not in name and stem in wanted:
                raise StoreError(
                    f"the store holds an unprefixed item {name}, which could belong to any repository. Stores now "
                    f"keep per-repository secrets as {self._prefix}<name>. Seed this repository's value with "
                    f"'theseus infra seed {stem}', then delete the old item ({_delete_hint(name)}) after checking "
                    "no other repository still needs it"
                )


def _delete_hint(name: str) -> str:
    return f"Keychain: security delete-generic-password -s {KEYCHAIN_SERVICE} -a {name}; file store: remove the file {name}"


def make_store(backend: str, directory: Path | None, runner: Runner = run_command) -> Store:
    if backend == "keychain":
        return KeychainStore(runner)
    if backend == "file":
        if directory is None:
            raise StoreError("the file store needs a directory")
        return FileStore(directory)
    raise StoreError(f"unknown store backend {backend!r}")


__all__ = [
    "KEYCHAIN_SERVICE",
    "PENDING_SUFFIX",
    "FileStore",
    "KeychainStore",
    "Secret",
    "SHARED_NAMES",
    "ScopedStore",
    "Store",
    "StoreError",
    "check_name",
    "decode",
    "encode",
    "make_store",
]
