from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Resolution:
    target: str | None
    substituted: bool
    reason: str


def resolve(
    candidates: Sequence[str], served: frozenset[str], windows: Mapping[str, int], load_tokens: int
) -> Resolution:
    for index, model in enumerate(candidates):
        if model not in served:
            continue
        if load_tokens and windows.get(model, 0) < load_tokens:
            continue
        return Resolution(model, index > 0, "")
    return Resolution(None, False, "unavailable: no candidate is served and large enough")
