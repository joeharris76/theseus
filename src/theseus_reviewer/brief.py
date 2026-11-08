from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

FULL_DIFF_PLACEHOLDER = "<<full-pull-request-diff-path>>"
FULL_DIFF_LINE = (
    f"The whole pull request diff is at {FULL_DIFF_PLACEHOLDER}; read it for context outside the changed files below.\n"
)
FILE_LIST_DIFF_LINE = (
    f"The whole pull request diff is at {FULL_DIFF_PLACEHOLDER}; reading the hunk blocks of the required files there"
    " is the cheapest way to meet the required reads above. "
)


def with_full_diff(text: str, path: Path | None) -> str:
    if path is not None:
        return text.replace(FULL_DIFF_PLACEHOLDER, str(path), 1)
    first = text.find(FULL_DIFF_PLACEHOLDER)
    for line in (FULL_DIFF_LINE, FILE_LIST_DIFF_LINE):
        start = text.find(line)
        if start != -1 and start <= first < start + len(line):
            return text[:start] + text[start + len(line) :]
    return text


def _open_private(path: str, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)


def create_exclusive(path: Path, data: bytes, opener: Callable[[str, int], int] = _open_private) -> None:
    with open(path, "xb", opener=opener) as handle:
        handle.write(data)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive(path, text.encode("utf-8"))
