from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

RETRY_DELAYS_SECONDS = (2.0, 5.0)
REQUEST_TIMEOUT_SECONDS = 30
TRANSIENT_GH_FAILURE = re.compile(
    r"HTTP 5[0-9]{2}|timed out|timeout|TLS handshake|connection reset|connection refused|unexpected EOF",
    re.IGNORECASE,
)
GH_WRITE_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input", "--method", "-X"})

sleep: Callable[[float], None] = time.sleep


def gh_call_is_read(args: Sequence[str]) -> bool:
    if args and args[0] == "graphql":
        return not any(arg.startswith("query=") and arg[6:].lstrip().startswith("mutation") for arg in args)
    return GH_WRITE_FLAGS.isdisjoint(args)


def gh_failure_is_transient(stderr: str) -> bool:
    return TRANSIENT_GH_FAILURE.search(stderr) is not None


def _read_once(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def open_read(request: urllib.request.Request, *, retry: bool = True) -> bytes:
    for delay in RETRY_DELAYS_SECONDS if retry else ():
        try:
            return _read_once(request)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        sleep(delay)
    return _read_once(request)
