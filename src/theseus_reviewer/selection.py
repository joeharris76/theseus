from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import capacity, credentials, providers
from .absence import AUTH, EMPTY, ERROR, INCOMPLETE, INVALID, published_detail
from .policy import Reviewer
from .recovery import RETRY, STOP, Recovery

PASS = "pass"
FAIL = "fail"
ABSENT = "absent"
REVIEW = "review"
PENDING = "pending"
BRIEF_MODES = ("inline", "file-list", "oversize")
QUOTA_BACKOFF = timedelta(hours=1)
JOB_FAILURE_DETAIL = "the reviewer job did not finish"
RETRYABLE_ABSENCES = frozenset({INCOMPLETE, EMPTY, ERROR})
MAX_ATTEMPTS_PER_REVIEWER = 2


@dataclass(frozen=True)
class Attempt:
    slot: int
    reviewer: str
    outcome: str
    absence: str = ""
    detail: str = ""
    reset_at: datetime | None = None
    provider: str = ""
    unread: tuple[str, ...] = ()
    recovery: Recovery | None = None
    target: str = ""
    nominal: str = ""


@dataclass(frozen=True)
class PoolBlock:
    until: datetime
    provider: str
    recorded_at: datetime
    primary: bool = False

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "until": self.until.isoformat(),
            "provider": self.provider,
            "recorded_at": self.recorded_at.isoformat(),
        }
        if self.primary:
            data["primary"] = True
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PoolBlock:
        return cls(
            datetime.fromisoformat(data["until"]),
            str(data.get("provider") or ""),
            datetime.fromisoformat(data["recorded_at"]),
            bool(data.get("primary")),
        )


@dataclass(frozen=True)
class Step:
    kind: str
    reviewer: Reviewer | None = None
    reasons: tuple[str, ...] = ()
    stopped: bool = False
    oversize: bool = False
    unavailable: bool = False
    resolution: providers.Resolution | None = None


@dataclass(frozen=True)
class SelectionInput:
    chain: Sequence[Reviewer]
    brief_mode: str
    max_attempts: int
    now: datetime
    pool_blocked_until: Mapping[str, PoolBlock] = field(default_factory=dict)
    load_tokens: int = 0
    capacities: Mapping[str, int] = field(default_factory=dict)
    capacity_error: str = ""
    candidates: Mapping[str, Sequence[str]] = field(default_factory=dict)
    served: providers.Served | None = None
    served_error: str = ""
    windows: Mapping[str, int] = field(default_factory=dict)
    pool_providers: Mapping[str, str] = field(default_factory=dict)

    @property
    def availability(self) -> bool:
        return bool(self.candidates) and (self.served is not None or bool(self.served_error))

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any], now: datetime) -> SelectionInput:
        return cls(
            chain=[Reviewer.from_json(item) for item in plan["chain"]],
            brief_mode=plan["brief_mode"],
            max_attempts=int(plan["max_attempts"]),
            now=now,
            pool_blocked_until={
                pool: PoolBlock.from_json(value) for pool, value in plan.get("pool_blocked_until", {}).items()
            },
            load_tokens=int(plan.get(capacity.LOAD_KEY) or 0),
            capacities={str(name): int(value) for name, value in (plan.get(capacity.CAPACITY_KEY) or {}).items()},
            capacity_error=str(plan.get(capacity.ERROR_KEY) or ""),
            candidates={str(name): tuple(items) for name, items in (plan.get("candidates") or {}).items()},
            served=providers.Served.from_json(plan["served"]) if plan.get("served") else None,
            served_error=str(plan.get("served_error") or ""),
            windows={str(name): int(value) for name, value in (plan.get("model_windows") or {}).items()},
            pool_providers={str(pool): str(name) for pool, name in (plan.get("pool_providers") or {}).items()},
        )


def _source(provider: str) -> str:
    return provider or "an unknown provider"


def effective_until(block: PoolBlock) -> datetime:
    return min(block.until, block.recorded_at + QUOTA_BACKOFF)


def active_blocks(blocks: Mapping[str, PoolBlock], now: datetime, *, same_head: bool = True) -> dict[str, PoolBlock]:
    return {
        pool: block for pool, block in blocks.items() if effective_until(block) > now and (same_head or block.primary)
    }


def needs_recovery(attempt: Attempt) -> bool:
    return attempt.outcome == ABSENT and attempt.absence == INVALID and attempt.detail != JOB_FAILURE_DETAIL


def stop_reason(attempt: Attempt) -> str:
    detail = published_detail(attempt.detail, credentials.known())
    return f"stopped: the process is blocked ({detail})"


def provider_failed(attempt: Attempt) -> bool:
    return attempt.outcome == ABSENT and attempt.absence in (AUTH, ERROR) and bool(attempt.provider)


def excluded_providers(attempts: Sequence[Attempt]) -> frozenset[str]:
    return frozenset(attempt.provider for attempt in attempts if provider_failed(attempt))


def _substituted_away(attempt: Attempt) -> bool:
    return (
        provider_failed(attempt) and bool(attempt.target) and providers.provider_of(attempt.target) == attempt.provider
    )


def _may_retry(history: Sequence[Attempt]) -> bool:
    last = history[-1]
    if last.outcome != ABSENT or last.detail == JOB_FAILURE_DETAIL:
        return False
    if len([item for item in history if not _substituted_away(item)]) >= MAX_ATTEMPTS_PER_REVIEWER:
        return False
    if _substituted_away(last):
        return True
    if needs_recovery(last):
        return last.recovery is not None and last.recovery.choice == RETRY
    return last.absence in RETRYABLE_ABSENCES


def retry_unread(attempts: Sequence[Attempt], reviewer: str, slot: int) -> tuple[str, ...]:
    history = sorted(
        (item for item in attempts if item.reviewer == reviewer and item.slot < slot), key=lambda a: a.slot
    )
    if not history or not _may_retry(history):
        return ()
    return history[-1].unread if history[-1].absence == INCOMPLETE else ()


def _skip_reason(
    reviewer: Reviewer,
    selection: SelectionInput,
    attempted: Mapping[str, Sequence[Attempt]],
    quota_pools: Mapping[str, str],
    excluded: frozenset[str],
) -> tuple[str | None, providers.Resolution | None]:
    history = attempted.get(reviewer.name, ())
    if history and not _may_retry(history):
        attempt = history[-1]
        return f"absent ({attempt.absence}{': ' + attempt.detail if attempt.detail else ''})", None
    if not reviewer.enabled:
        return f"disabled ({reviewer.disabled_reason})", None
    if reviewer.pool in quota_pools:
        return (
            f"skipped: {reviewer.pool} pool blocked in this run, {_source(quota_pools[reviewer.pool])} returned a quota error",
            None,
        )
    block = selection.pool_blocked_until.get(reviewer.pool)
    if block is not None and effective_until(block) > selection.now:
        return (
            f"skipped: {reviewer.pool} pool blocked until {effective_until(block).isoformat()}, "
            f"{_source(block.provider)} returned a quota error at {block.recorded_at.isoformat()}",
            None,
        )
    resolution: providers.Resolution | None = None
    limit = selection.capacities.get(reviewer.name)
    if selection.availability:
        resolution = providers.resolve(
            selection.candidates.get(reviewer.name, ()),
            selection.served,
            selection.served_error,
            excluded,
            selection.windows,
            selection.load_tokens,
        )
        if resolution.target is None:
            if resolution.degraded_capacity and limit is not None and selection.load_tokens > limit:
                return capacity.skip_text(selection.load_tokens, limit), resolution
            return resolution.reason, resolution
    if selection.load_tokens and not (resolution is not None and resolution.substituted):
        if limit is None:
            return capacity.unknown_text(selection.capacity_error), resolution
        if selection.load_tokens > limit:
            return capacity.skip_text(selection.load_tokens, limit), resolution
    if selection.brief_mode == "oversize":
        return "skipped: the brief exceeds the size cap even without the diff", resolution
    if selection.brief_mode == "file-list" and not (reviewer.read_only == "hard" and reviewer.reads_files):
        return (
            "skipped: the diff exceeds the brief cap and only hard read-only reviewers that read files may review it",
            resolution,
        )
    return None, resolution


def next_step(selection: SelectionInput, attempts: Sequence[Attempt]) -> Step:
    ordered = sorted(attempts, key=lambda attempt: attempt.slot)
    by_name = {reviewer.name: reviewer for reviewer in selection.chain}
    for attempt in ordered:
        if attempt.outcome == FAIL:
            return Step(FAIL, by_name.get(attempt.reviewer), (f"{attempt.reviewer}: did not clear the change",))
        if attempt.outcome == PASS:
            return Step(PASS, by_name.get(attempt.reviewer), (f"{attempt.reviewer}: cleared the change",))
    if ordered and needs_recovery(ordered[-1]):
        last = ordered[-1]
        if last.recovery is not None and last.recovery.choice == STOP:
            return Step(PENDING, None, (stop_reason(last),), stopped=True)
        if last.recovery is None and len(ordered) < selection.max_attempts:
            return Step(PENDING, None, (f"{last.reviewer}: absent (invalid): the recovery is not recorded",))
    attempted: dict[str, list[Attempt]] = {}
    for attempt in ordered:
        attempted.setdefault(attempt.reviewer, []).append(attempt)
    quota_pools = {
        by_name[attempt.reviewer].pool: attempt.provider
        for attempt in ordered
        if attempt.absence == "quota" and attempt.reviewer in by_name
    }
    excluded = excluded_providers(ordered)
    reasons: list[str] = []
    too_large = False
    blocked_otherwise = False
    only_availability = True
    for reviewer in selection.chain:
        reason, resolution = _skip_reason(reviewer, selection, attempted, quota_pools, excluded)
        if reason is None and len(ordered) >= selection.max_attempts:
            reason = "skipped: no attempt slot left in this run"
        if reason is not None:
            reasons.append(f"{reviewer.name}: {reason}")
            if reason.startswith(capacity.SKIP_PREFIX):
                too_large = True
                only_availability = False
            elif not reason.startswith("disabled"):
                blocked_otherwise = True
                if not reason.startswith((providers.UNAVAILABLE_PREFIX, providers.DEGRADED_CAPACITY_PREFIX)):
                    only_availability = False
            continue
        return Step(REVIEW, reviewer, resolution=resolution)
    if too_large and not blocked_otherwise:
        return Step(PENDING, None, (*reasons, capacity.SPLIT_INSTRUCTION), oversize=True)
    return Step(PENDING, None, tuple(reasons), unavailable=not ordered and blocked_otherwise and only_availability)


def pool_resets(selection: SelectionInput, attempts: Sequence[Attempt]) -> dict[str, PoolBlock]:
    by_name = {reviewer.name: reviewer for reviewer in selection.chain}
    resets = active_blocks(selection.pool_blocked_until, selection.now)
    for attempt in attempts:
        if attempt.absence == "quota" and attempt.reviewer in by_name:
            pool = by_name[attempt.reviewer].pool
            primary = bool(attempt.provider) and selection.pool_providers.get(pool) == attempt.provider
            requested = attempt.reset_at if primary and attempt.reset_at else selection.now + QUOTA_BACKOFF
            block = PoolBlock(min(requested, selection.now + QUOTA_BACKOFF), attempt.provider, selection.now, primary)
            kept = resets.get(pool)
            if kept is None or block.until >= kept.until:
                resets[pool] = block
    return resets
