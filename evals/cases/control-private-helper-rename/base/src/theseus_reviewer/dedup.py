from __future__ import annotations

import hashlib
import re

_SPACES = re.compile(r"\s+")


def _normalise(title: str) -> str:
    return _SPACES.sub(" ", title.strip().lower())


def fingerprint(path: str, title: str) -> str:
    digest = hashlib.sha256(f"{path}\n{_normalise(title)}".encode()).hexdigest()
    return digest[:16]
