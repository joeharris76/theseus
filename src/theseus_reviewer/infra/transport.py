"""The seams every outside call goes through: HTTP requests and commands (gh, security, openssl, the host script).

Secret values travel only in HTTP bodies, authorization headers and command stdin, never in argv or the child's
environment. Tests fake both seams and assert that.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .. import credentials

HTTP_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 60.0


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError as exc:
            raise TransportError(f"response is not JSON (HTTP {self.status})") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse: ...


class UrllibTransport:
    """The real HTTP transport. A non-2xx answer is returned, not raised; only a failure to get any answer raises."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        if not url.startswith("https://"):
            raise TransportError(f"refusing a non-https url: {url.split('?', 1)[0]}")
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only, checked above
                return HttpResponse(response.status, _lower(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, _lower(exc.headers.items()), exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method} {url.split('?', 1)[0]} failed: {type(exc).__name__}") from exc


def _lower(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in items}


class Runner(Protocol):
    """Runs one command. ``stdin`` is how a secret reaches a child; ``argv`` must never carry one."""

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]: ...


GhRunner = Runner


def run_command(
    argv: Sequence[str], *, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired as exc:
        raise TransportError(f"{argv[0]} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise TransportError(f"cannot run {argv[0]}: {exc.strerror or type(exc).__name__}") from exc


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


class Redactor:
    """Every secret value seen in this run. All text shown to the owner passes through ``scrub``."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: list[str] = []
        self.add(*values)

    def add(self, *values: str) -> None:
        for value in values:
            if len(value) >= credentials.MIN_LENGTH and value not in self._values:
                self._values.append(value)

    def scrub(self, text: str) -> str:
        return credentials.redact(text, self._values)

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self._values)
