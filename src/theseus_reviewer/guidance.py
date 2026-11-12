from __future__ import annotations

from pathlib import Path

MAX_GUIDANCE_BYTES = 16384


class GuidanceError(ValueError):
    pass


def check(data: bytes, label: str) -> str:
    if len(data) > MAX_GUIDANCE_BYTES:
        raise GuidanceError(f"{label} is over {MAX_GUIDANCE_BYTES} bytes, the limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuidanceError(f"{label} is not valid UTF-8: {exc}") from exc
    if not text.strip():
        raise GuidanceError(f"{label} is empty")
    return text.strip()


def read(root: Path, relative: str) -> str:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise GuidanceError(f"{relative} must be a regular file under the trusted checkout")
    with path.open("rb") as handle:
        return check(handle.read(MAX_GUIDANCE_BYTES + 1), relative)
