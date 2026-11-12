from __future__ import annotations

from pathlib import Path

MAX_GUIDANCE_BYTES = 16384


class GuidanceError(ValueError):
    pass


def check(text: str, label: str) -> str:
    size = len(text.encode("utf-8"))
    if size > MAX_GUIDANCE_BYTES:
        raise GuidanceError(f"{label} is {size} bytes; the limit is {MAX_GUIDANCE_BYTES}")
    if not text.strip():
        raise GuidanceError(f"{label} is empty")
    return text.strip()


def read(root: Path, relative: str) -> str:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise GuidanceError(f"{relative} must be a regular file under the trusted checkout")
    with path.open("rb") as handle:
        data = handle.read(MAX_GUIDANCE_BYTES + 1)
    try:
        return check(data.decode("utf-8"), relative)
    except UnicodeDecodeError as exc:
        raise GuidanceError(f"{relative} is not valid UTF-8: {exc}") from exc
