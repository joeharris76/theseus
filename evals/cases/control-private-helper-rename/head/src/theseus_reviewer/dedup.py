from __future__ import annotations

import hashlib
import re

_SPACES = re.compile(r"\s+")


def _normalised_title(title: str) -> str:
    return _SPACES.sub(" ", title.strip().lower())


def fingerprint(path: str, title: str) -> str:
    digest = hashlib.sha256(f"{path}\n{_normalised_title(title)}".encode()).hexdigest()
    return digest[:16]
