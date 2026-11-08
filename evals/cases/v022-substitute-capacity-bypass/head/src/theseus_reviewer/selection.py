from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import capacity, providers


@dataclass(frozen=True)
class Reviewer:
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class SelectionInput:
    load_tokens: int
    capacities: Mapping[str, int]
    capacity_error: str = ""
    availability: bool = False
    served: frozenset[str] = frozenset()
    windows: Mapping[str, int] = field(default_factory=dict)
    candidates: Mapping[str, Sequence[str]] = field(default_factory=dict)


def skip_reason(reviewer: Reviewer, selection: SelectionInput) -> tuple[str | None, providers.Resolution | None]:
    if not reviewer.enabled:
        return "disabled", None
    resolution: providers.Resolution | None = None
    limit = selection.capacities.get(reviewer.name)
    if selection.availability:
        resolution = providers.resolve(
            selection.candidates.get(reviewer.name, ()), selection.served, selection.windows, selection.load_tokens
        )
        if resolution.target is None:
            return resolution.reason, resolution
    if selection.load_tokens and not (resolution is not None and resolution.substituted):
        if limit is None:
            return capacity.unknown_text(selection.capacity_error), resolution
        if selection.load_tokens > limit:
            return capacity.skip_text(selection.load_tokens, limit), resolution
    return None, resolution


def next_reviewer(chain: Sequence[Reviewer], selection: SelectionInput) -> Reviewer | None:
    for reviewer in chain:
        reason, _ = skip_reason(reviewer, selection)
        if reason is None:
            return reviewer
    return None
