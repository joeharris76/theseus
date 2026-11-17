"""Fakes for the infra seams: HTTP, commands and the ``security`` tool. No fake touches a network or a keychain."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from theseus_reviewer.infra.transport import HttpResponse

SENTINELS = {
    "admin": "SENTINEL-openai-admin-0001",
    "root_id": "SENTINEL-ts-root-id-0002",
    "root_secret": "SENTINEL-ts-root-secret-0003",
    "typesafe": "SENTINEL-typesafe-key-0004",
    "gateway": "SENTINEL-gateway-token-0005",
    "ts_secret": "SENTINEL-ts-client-secret-0006",
    "openai": "SENTINEL-openai-sa-key-0007",
    "pem_marker": "SENTINEL-pem-body-0008",
}


def json_response(status: int, data: Any, **headers: str) -> HttpResponse:
    return HttpResponse(
        status, {key.lower().replace("_", "-"): value for key, value in headers.items()}, json.dumps(data).encode()
    )


@dataclass
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None

    def json(self) -> Any:
        assert self.body is not None
        return json.loads(self.body)


Handler = Callable[[Call], HttpResponse]


@dataclass
class FakeHttp:
    """Routes by method and a regex on the url. An unrouted request fails the test."""

    routes: list[tuple[str, re.Pattern[str], Handler]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def on(self, method: str, pattern: str, answer: HttpResponse | Handler) -> None:
        handler = answer if callable(answer) else (lambda call, response=answer: response)
        self.routes.append((method, re.compile(pattern), handler))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 0,
    ) -> HttpResponse:
        call = Call(method, url, dict(headers or {}), body)
        self.calls.append(call)
        for route_method, pattern, handler in self.routes:
            if route_method == method and pattern.fullmatch(url):
                return handler(call)
        raise AssertionError(f"unrouted {method} {url}")

    def mutating(self) -> list[Call]:
        return [call for call in self.calls if call.method not in ("GET", "HEAD")]


@dataclass
class RunCall:
    argv: tuple[str, ...]
    stdin: str | None


Answer = subprocess.CompletedProcess[str]


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Answer:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@dataclass
class FakeRunner:
    """Routes by a predicate on argv. An unrouted command fails the test."""

    routes: list[tuple[Callable[[Sequence[str]], bool], Callable[[RunCall], Answer]]] = field(default_factory=list)
    calls: list[RunCall] = field(default_factory=list)

    def on(self, match: Callable[[Sequence[str]], bool], answer: Answer | Callable[[RunCall], Answer]) -> None:
        handler = answer if callable(answer) else (lambda call, response=answer: response)
        self.routes.append((match, handler))

    def __call__(self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 0) -> Answer:
        call = RunCall(tuple(argv), stdin)
        self.calls.append(call)
        for match, handler in self.routes:
            if match(argv):
                return handler(call)
        raise AssertionError(f"unrouted command {list(argv)}")


_PLAIN = re.compile(r"[A-Za-z0-9._~+/=@:-]*")


@dataclass
class FakeSecurity:
    """An in-memory ``security`` that behaves like the real one where the store depends on it.

    ``-w`` prints a value that is not plain printable text as hex, ``-i`` reports a failed command without a
    failing exit status, and an unparsable line stops at the first space or quote, so a value that is not encoded
    by the store comes back wrong and the contract test notices.
    """

    items: dict[tuple[str, str], tuple[str, datetime]] = field(default_factory=dict)
    clock: datetime = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    argv_seen: list[tuple[str, ...]] = field(default_factory=list)
    fail_writes_after: int | None = None

    def __call__(self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 0) -> Answer:
        self.argv_seen.append(tuple(argv))
        assert argv[0] == "security"
        command = argv[1]
        if command == "-i":
            return self._interactive(stdin or "")
        if command == "find-generic-password":
            return self._find(argv)
        if command == "delete-generic-password":
            key = (_flag(argv, "-s"), _flag(argv, "-a"))
            if key not in self.items:
                return completed(returncode=44, stderr="The specified item could not be found in the keychain.")
            del self.items[key]
            return completed()
        if command == "dump-keychain":
            blocks = [
                f'keychain: "/fake/login.keychain-db"\nattributes:\n    "acct"<blob>="{account}"\n    "svce"<blob>="{service}"\n'
                for (service, account) in sorted(self.items)
            ]
            blocks.append(
                'keychain: "/fake/login.keychain-db"\nattributes:\n    "acct"<blob>="other"\n    "svce"<blob>="elsewhere"\n'
            )
            return completed("".join(blocks))
        raise AssertionError(f"unexpected security command {command}")

    def _interactive(self, stdin: str) -> Answer:
        for line in stdin.splitlines():
            if len(line) > 4096:  # the real tool silently drops what it cannot read
                continue
            if self.fail_writes_after is not None:
                if self.fail_writes_after <= 0:
                    continue
                self.fail_writes_after -= 1
            tokens = shlex.split(line)
            assert tokens[0] == "add-generic-password"
            value = _flag(tokens, "-w")
            self.items[(_flag(tokens, "-s"), _flag(tokens, "-a"))] = (value, self.clock)
        return completed()

    def _find(self, argv: Sequence[str]) -> Answer:
        key = (_flag(argv, "-s"), _flag(argv, "-a"))
        if key not in self.items:
            return completed(
                returncode=44,
                stderr="security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.",
            )
        value, modified = self.items[key]
        if "-w" in argv:
            shown = value if _PLAIN.fullmatch(value) else value.encode().hex()
            return completed(shown + "\n")
        stamp = modified.strftime("%Y%m%d%H%M%SZ")
        return completed(
            f'keychain: "/fake"\nattributes:\n    "mdat"<timedate>=0x{stamp.encode().hex()}  "{stamp}\\000"\n'
        )


def _flag(argv: Sequence[str], flag: str) -> str:
    return argv[list(argv).index(flag) + 1]
