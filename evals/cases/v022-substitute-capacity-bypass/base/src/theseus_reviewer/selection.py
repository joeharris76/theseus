from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import capacity


@dataclass(frozen=True)
class Reviewer:
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class SelectionInput:
    load_tokens: int
    capacities: Mapping[str, int]
    capacity_error: str = ""


def skip_reason(reviewer: Reviewer, selection: SelectionInput) -> str | None:
    if not reviewer.enabled:
        return "disabled"
    limit = selection.capacities.get(reviewer.name)
    if selection.load_tokens:
        if limit is None:
            return capacity.unknown_text(selection.capacity_error)
        if selection.load_tokens > limit:
            return capacity.skip_text(selection.load_tokens, limit)
    return None


def next_reviewer(chain: Sequence[Reviewer], selection: SelectionInput) -> Reviewer | None:
    for reviewer in chain:
        if skip_reason(reviewer, selection) is None:
            return reviewer
    return None
