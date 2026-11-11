from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import absence, capacity, providers, recovery, retry, selection
from .github import parse_time
from .selection import Attempt, Step

MARKER_VERSION = "v1"
MARKER_OPEN = "<!-- review-unavailable:"
MARKER_PATTERN = re.compile(
    rf"{re.escape(MARKER_OPEN)} {MARKER_VERSION} (?P<head>[0-9a-f]{{40}}) (?P<key>[a-z0-9+-]+) -->"
)
ABSENT_KIND = re.compile(r"absent \((?P<kind>[a-z-]+)\)")
KEY_SEPARATOR = "+"

SWEEP_RETRIES = "The hourly sweep retries this head; a maintainer can comment `/review` to retry now."
MANUAL_RETRY = "A maintainer can comment `/review` to retry."
SPLIT = "Split this pull request into smaller ones, or ask an owner for a stand-in review of this head."
NEW_HEAD = "Push a new head to run the review again."


@dataclass(frozen=True)
class Cause:
    key: str
    reason: str
    next_step: str


QUOTA = Cause("quota", "the model provider's usage quota is exhausted", SWEEP_RETRIES)
PROVIDER_AUTH = Cause("provider-auth", "the model provider rejected the review's credentials", SWEEP_RETRIES)
TIMEOUT = Cause("timeout", "the reviewer did not finish within its time limit", SWEEP_RETRIES)
INVALID = Cause("invalid-answer", "the reviewer's answer was not a valid review", SWEEP_RETRIES)
EMPTY = Cause("empty-answer", "the reviewer returned no answer", SWEEP_RETRIES)
INCOMPLETE = Cause("incomplete-read", "the reviewer did not read every changed hunk", SWEEP_RETRIES)
PROVIDER_ERROR = Cause(
    "provider-error", "the model provider could not be reached or returned a server error", SWEEP_RETRIES
)
RUN_FAILED = Cause("run-failed", "the reviewer failed to run", SWEEP_RETRIES)
TOO_LARGE = Cause("too-large", "the change is too large for the reviewer to read", SPLIT)
GATEWAY = Cause("gateway-unreachable", "the model gateway could not be reached", SWEEP_RETRIES)
GATEWAY_REJECTED = Cause("gateway-rejected", "the model gateway rejected the review's client token", SWEEP_RETRIES)
GATEWAY_ERROR = Cause("gateway-error", "the model gateway gave an unusable answer or is misconfigured", SWEEP_RETRIES)
NOT_SERVED = Cause("not-served", "no served model can review this change", SWEEP_RETRIES)
CAPACITY_UNKNOWN = Cause("capacity-unknown", "the reviewer's reading capacity could not be measured", SWEEP_RETRIES)
BLOCKED = Cause("blocked", "the reviewer's process is blocked", MANUAL_RETRY)
NO_REPORT = Cause("no-report", "a selected reviewer did not report", NEW_HEAD)
ARTIFACTS = Cause("artifacts-invalid", "the run's artifacts failed validation", NEW_HEAD)
DISABLED = Cause("reviewer-disabled", "the reviewer is disabled by configuration", SWEEP_RETRIES)
ATTEMPT_LIMIT = Cause("attempt-limit", "no attempt slot was left in this run", SWEEP_RETRIES)
GENERIC = Cause("no-reviewer", "no reviewer could run", SWEEP_RETRIES)

BOOKKEEPING = (DISABLED, ATTEMPT_LIMIT)

ABSENCE_CAUSES: dict[str, Cause] = {
    absence.QUOTA: QUOTA,
    absence.AUTH: PROVIDER_AUTH,
    absence.TIMEOUT: TIMEOUT,
    absence.INVALID: INVALID,
    absence.EMPTY: EMPTY,
    absence.ERROR: PROVIDER_ERROR,
    absence.INCOMPLETE: INCOMPLETE,
    absence.CONTEXT_OVERFLOW: TOO_LARGE,
}
PENDING_CAUSES: dict[str, Cause] = {
    retry.INTEGRITY: ARTIFACTS,
    retry.OVERSIZE: TOO_LARGE,
    retry.STOPPED: BLOCKED,
    retry.UNREPORTED: NO_REPORT,
}
CAUSES = (
    QUOTA,
    PROVIDER_AUTH,
    TIMEOUT,
    INVALID,
    EMPTY,
    INCOMPLETE,
    PROVIDER_ERROR,
    RUN_FAILED,
    TOO_LARGE,
    GATEWAY,
    GATEWAY_REJECTED,
    GATEWAY_ERROR,
    NOT_SERVED,
    CAPACITY_UNKNOWN,
    BLOCKED,
    NO_REPORT,
    ARTIFACTS,
    DISABLED,
    ATTEMPT_LIMIT,
    GENERIC,
)


def attempt_cause(item: Attempt) -> Cause:
    if item.absence == absence.ERROR and not item.provider:
        return RUN_FAILED
    return ABSENCE_CAUSES.get(item.absence, GENERIC)


def gateway_cause(served_error: str) -> Cause:
    if "rejected" in served_error:
        return GATEWAY_REJECTED
    return GATEWAY if served_error.startswith("gateway unreachable") else GATEWAY_ERROR


def _routed(provider: str, plan: Mapping[str, Any]) -> bool:
    return bool(provider) and provider in (plan.get("routed") or ())


def _named(provider: str, plan: Mapping[str, Any]) -> str:
    return f" ({provider})" if _routed(provider, plan) else ""


def _skip_cause(text: str, plan: Mapping[str, Any]) -> Cause:
    prefixes = (capacity.SKIP_PREFIX, providers.DEGRADED_CAPACITY_PREFIX, "skipped: the brief exceeds")
    if text.startswith("disabled"):
        return DISABLED
    if text.startswith(prefixes) or text.startswith("skipped: the diff exceeds"):
        return TOO_LARGE
    if text.startswith(capacity.UNKNOWN_PREFIX):
        return CAPACITY_UNKNOWN
    if text.startswith("skipped: no attempt slot left"):
        return ATTEMPT_LIMIT
    if text.startswith("skipped:") and "pool blocked" in text:
        return QUOTA
    if text.startswith(providers.UNAVAILABLE_PREFIX):
        return gateway_cause(str(plan["served_error"])) if plan.get("served_error") else NOT_SERVED
    if "selected but did not report" in text:
        return NO_REPORT
    absent = ABSENT_KIND.match(text)
    if absent is not None:
        return ABSENCE_CAUSES.get(absent.group("kind"), GENERIC)
    return GENERIC


def _reason_cause(reason: str, plan: Mapping[str, Any]) -> tuple[str, Cause | None]:
    if reason.startswith("stopped:"):
        return "", BLOCKED
    if reason == capacity.SPLIT_INSTRUCTION:
        return "", None
    name, _, rest = reason.partition(":")
    return name, _skip_cause(rest.strip(), plan)


def _unattempted(step: Step, attempts: Sequence[Attempt], plan: Mapping[str, Any]) -> list[tuple[str, Cause | None]]:
    attempted = {item.reviewer for item in attempts}
    return [pair for reason in step.reasons if (pair := _reason_cause(reason, plan))[0] not in attempted or not pair[0]]


def causes(step: Step, attempts: Sequence[Attempt], plan: Mapping[str, Any]) -> tuple[Cause, ...]:
    found = [attempt_cause(item) for item in attempts if item.outcome == selection.ABSENT]
    found += [cause for _, cause in _unattempted(step, attempts, plan) if cause is not None]
    kept = [cause for cause in found if cause not in BOOKKEEPING] or found
    return tuple(sorted(dict.fromkeys(kept), key=lambda cause: cause.key)) or (GENERIC,)


def cause_key(found: Sequence[Cause]) -> str:
    return KEY_SEPARATOR.join(cause.key for cause in found)


def next_step(found: Sequence[Cause]) -> str:
    return " ".join(dict.fromkeys(cause.next_step for cause in found))


def reason_text(found: Sequence[Cause]) -> str:
    return "; ".join(dict.fromkeys(cause.reason for cause in found))


def lines(attempts: Sequence[Attempt], step: Step, plan: Mapping[str, Any]) -> list[str]:
    out = []
    for item in attempts:
        if item.outcome != selection.ABSENT:
            continue
        held = f"; {recovery_text(item)}" if item.recovery is not None else ""
        out.append(
            f"- {item.reviewer} (attempt {item.slot}): {attempt_cause(item).reason}{_named(item.provider, plan)}{held}"
        )
    for name, cause in _unattempted(step, attempts, plan):
        if cause is not None:
            out.append(f"- {name}: {cause.reason}" if name else f"- {cause.reason}")
    return out


def recovery_text(item: Attempt) -> str:
    held = item.recovery
    if held is None:
        return ""
    if held.source == recovery.JEV:
        return f"recovery: {held.choice} (Jev, probability {held.probability:.2f}, confidence {held.confidence:.2f})"
    return f"recovery: {held.choice} (fallback, {held.reason})"


def excluded_line(item: Attempt, plan: Mapping[str, Any]) -> str:
    provider = item.provider if _routed(item.provider, plan) else "a provider"
    return f"- excluded after {provider} failed (attempt {item.slot}, {item.reviewer}): {attempt_cause(item).reason}"


def marker(head: str, found: Sequence[Cause]) -> str:
    return f"{MARKER_OPEN} {MARKER_VERSION} {head} {cause_key(found)} -->"


def notice(head: str, found: Sequence[Cause], *, with_next_step: bool = True) -> list[str]:
    return [*([f"Next step: {next_step(found)}", ""] if with_next_step else []), marker(head, found)]


def _post_head(post: Mapping[str, Any], head: str) -> bool:
    body = str(post.get("body") or "")
    return (
        post.get("commit_id") == head
        or f"for `{head}`" in body.split("\n", 1)[0]
        or any(found[0] == head for found in MARKER_PATTERN.findall(body))
    )


def _bot_post(post: Mapping[str, Any], bot_login: str) -> bool:
    return (
        post.get("login") == f"{bot_login}[bot]" and post.get("user_type") == "Bot" and post.get("state") != "DISMISSED"
    )


def posts_of(comments: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        *(
            {
                "login": (item.get("user") or {}).get("login"),
                "user_type": (item.get("user") or {}).get("type"),
                "body": item.get("body") or "",
                "at": item.get("created_at"),
            }
            for item in comments
        ),
        *(
            {
                "login": item.get("login"),
                "user_type": item.get("user_type"),
                "state": item.get("state"),
                "body": item.get("body") or "",
                "commit_id": item.get("commit_id"),
                "at": item.get("submitted_at"),
            }
            for item in reviews
        ),
    ]


def previous_key(posts: Sequence[Mapping[str, Any]], head: str, bot_login: str) -> tuple[str, str] | None:
    own = [post for post in posts if _bot_post(post, bot_login) and _post_head(post, head)]
    stamped = [(parse_time(post.get("at")), post) for post in own]
    if not stamped or any(moment is None for moment, _ in stamped):
        return None
    latest = max(stamped, key=lambda pair: pair[0])[1]
    found = MARKER_PATTERN.findall(str(latest.get("body") or ""))
    return found[-1] if found else None


def repeats(posts: Sequence[Mapping[str, Any]], head: str, body: str, bot_login: str) -> bool:
    current = MARKER_PATTERN.findall(body)
    return bool(current) and current[-1][0] == head and previous_key(posts, head, bot_login) == current[-1]
