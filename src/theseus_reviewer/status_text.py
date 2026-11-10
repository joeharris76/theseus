from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

DESCRIPTION_LIMIT = 140
SEP = " · "
ELLIPSIS = "…"

RUN_SUFFIX_OPEN = " [run "
RUN_SUFFIX_CLOSE = "]"
RUN_SUFFIX_RESERVE = 45
RUN_SUFFIX = re.compile(rf"{re.escape(RUN_SUFFIX_OPEN)}(?P<run>[0-9a-z]+){re.escape(RUN_SUFFIX_CLOSE)}$")
REPOSTED_MARK = " (reposted)"
REPOST_LIMIT = DESCRIPTION_LIMIT - len(REPOSTED_MARK)
SUCCESS_LIMIT = REPOST_LIMIT - RUN_SUFFIX_RESERVE

SHIP_LABEL = "SHIP"
OPEN_FINDINGS_LABEL = "SHIP WITH FIXES"
DO_NOT_SHIP_LABEL = "DO NOT SHIP"
REFUSED_LABEL = "REFUSED"
ESCALATED_LABEL = "escalated"
ESCALATED = re.compile(rf"{ESCALATED_LABEL} (?P<ids>D[0-9]+(?:,D[0-9]+)*)")
EXEMPT_SUCCESS_DESCRIPTION = "only exempt paths changed"
REFUSED_DESCRIPTION_PREFIX = "refused after"
EDITED_REASON = "a review was edited"
SWEEP_FAILURE_SURFACE_PREFIX = "review-verdict failed twice without a result at: "
DEGRADED_MARK = f"{SEP}degraded: "
STANDIN_APPROVAL = "Stand-in review: APPROVE"
GATE_WAITING_PREFIX = "Waiting for review-verdict"

NO_OPEN_FINDINGS = "no open findings"
OPEN_FINDINGS_HINT = "see review threads"
RULING_HINT = "owner ruling needed"
REWORK_HINT = "rework needed"
NOT_REVIEWED = "Not reviewed"
NOT_REVIEWED_YET = "Not reviewed yet"
RETRIES_HOURLY = "retries hourly"
RETRIES_NOW = "/review retries now"
PUSH_NEW_HEAD = "push a new head"

OVERSIZE = SEP.join((NOT_REVIEWED, "too large to review", "split the PR or ask an owner for a stand-in"))
FORK = SEP.join((NOT_REVIEWED, "fork PR", "needs an owner stand-in"))
INTEGRITY = SEP.join((NOT_REVIEWED, "run artifacts failed validation", PUSH_NEW_HEAD))
MARKER_LIMIT = SEP.join(
    (NOT_REVIEWED, "decision record too large for a review marker", f"{PUSH_NEW_HEAD} with fewer or shorter findings")
)
STOPPED = SEP.join(
    (NOT_REVIEWED, "review stopped, a reviewer process is blocked", "see the run log", "/review retries")
)
UNREPORTED = SEP.join((NOT_REVIEWED, "a selected reviewer did not report", PUSH_NEW_HEAD))
OMP_INSTALL = SEP.join((NOT_REVIEWED, "pinned omp build could not be installed", "/review retries"))
GATEWAY_DOWN = SEP.join((NOT_REVIEWED_YET, "model gateway unreachable", RETRIES_HOURLY, RETRIES_NOW))
EDITED = SEP.join((EDITED_REASON, "verdict withheld", PUSH_NEW_HEAD))

GATE_EVALUATING = "Evaluating this head"
GATE_PASS = "Pass"
GATE_WAITING = "Waiting"
GATE_BLOCKED = "Blocked"
GATE_ERROR = "Gate error"
OWNER_STANDIN = "owner stand-in"
GATE_NOT_REVIEWED = SEP.join(
    (GATE_WAITING, "not reviewed yet", "see the review-verdict status", "owner may post a stand-in")
)
GATE_DO_NOT_SHIP = SEP.join((GATE_BLOCKED, DO_NOT_SHIP_LABEL, f"rework and {PUSH_NEW_HEAD}, or {OWNER_STANDIN}"))
GATE_REFUSED = SEP.join(
    (
        GATE_BLOCKED,
        f"review {REFUSED_DESCRIPTION_PREFIX} repeated {DO_NOT_SHIP_LABEL}",
        f"open a new PR, or {OWNER_STANDIN}",
    )
)
GATE_EXEMPT_STALE = SEP.join(
    (
        GATE_WAITING,
        "review-verdict reports exempt paths only, but this head needs review",
        f"{PUSH_NEW_HEAD} or {OWNER_STANDIN}",
    )
)
GATE_UNMATCHED_SUCCESS = SEP.join(
    (
        GATE_WAITING,
        f"review-verdict {SHIP_LABEL} has no matching unedited review",
        f"{PUSH_NEW_HEAD}, or {OWNER_STANDIN}",
    )
)
GATE_PASS_VERDICT = SEP.join((GATE_PASS, f"review-verdict: {SHIP_LABEL}"))
GATE_PASS_EXEMPT = SEP.join((GATE_PASS, EXEMPT_SUCCESS_DESCRIPTION))

_URL = re.compile(r"\s*https?://\S+")
_HTTP_FAILURE = re.compile(r":?\s*GitHub returned HTTP (?P<code>[0-9]{3}) for\s+\S+")
_SHA = re.compile(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40})\b")
SHORT_SHA = 12


def run_suffix_text(run: object) -> str:
    return f"{RUN_SUFFIX_OPEN}{run}{RUN_SUFFIX_CLOSE}"


def run_suffix(description: str | None) -> str | None:
    match = RUN_SUFFIX.search(description or "")
    return match.group("run") if match else None


def strip_run_suffix(description: str | None) -> str:
    return RUN_SUFFIX.sub("", description or "")


def hard_clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + ELLIPSIS


def trim(text: str, limit: int) -> str:
    while len(text) > limit and SEP in text:
        text = text.rpartition(SEP)[0]
    return hard_clip(text, limit)


def _pick(variants: Sequence[str], limit: int) -> str:
    return next((variant for variant in variants if len(variant) <= limit), trim(variants[-1], limit))


def short_sha(text: str) -> str:
    return _SHA.sub(lambda match: match.group(0)[:SHORT_SHA], text)


def _escalated_text(ids: Sequence[str], room: int) -> str:
    for shown in range(len(ids), 0, -1):
        hidden = f" +{len(ids) - shown} more" if shown < len(ids) else ""
        text = f"{ESCALATED_LABEL} {','.join(ids[:shown])}{hidden}"
        if len(text) <= room:
            return text
    return f"{ESCALATED_LABEL} {ids[0]}"


def ids_text(ids: Sequence[str], room: int, joiner: str = ",") -> str:
    for shown in range(len(ids), 0, -1):
        hidden = f" +{len(ids) - shown} more" if shown < len(ids) else ""
        text = f"{joiner.join(ids[:shown])}{hidden}"
        if len(text) <= room:
            return text
    return ids[0]


def carried_tail(reviewed_head: str) -> str:
    return f"carried from {reviewed_head[:SHORT_SHA]} (same patch)"


def ruling_tail(action: str, defect_id: str) -> str:
    return f"owner ruling: {action} {defect_id}"


def severity_counts(severities: Iterable[str], order: Sequence[str]) -> list[tuple[str, int]]:
    found = list(severities)
    return [(name, found.count(name)) for name in order if name in found]


def shipped(fixed: Sequence[str], tail: str, limit: int) -> str:
    if tail:
        return trim(SEP.join((SHIP_LABEL, tail)), limit)
    if not fixed:
        return SEP.join((SHIP_LABEL, NO_OPEN_FINDINGS))
    return _pick(
        [
            SEP.join((SHIP_LABEL, f"{', '.join(fixed)} fixed", NO_OPEN_FINDINGS)),
            SEP.join((SHIP_LABEL, f"{len(fixed)} fixed", NO_OPEN_FINDINGS)),
            SEP.join((SHIP_LABEL, NO_OPEN_FINDINGS)),
        ],
        limit,
    )


def with_findings(
    label: str,
    total: int,
    severities: Sequence[tuple[str, int]],
    escalated: Sequence[str],
    tail: str,
    limit: int,
) -> str:
    head = f"{label}{SEP}{total} to fix"
    if escalated:
        rest = [tail or RULING_HINT]
        used = len(head) + len(SEP) + sum(len(SEP) + len(part) for part in rest)
        return trim(SEP.join((head, _escalated_text(escalated, limit - used), *rest)), limit)
    counted = f"{head} ({', '.join(f'{count} {name}' for name, count in severities)})" if severities else head
    closing = tail or OPEN_FINDINGS_HINT
    return _pick([SEP.join((counted, closing)), SEP.join((head, closing)), head], limit)


def refused_decision(label: str, strikes: int, limit_strikes: int | None, tail: str, limit: int) -> str:
    parts = [label, tail or REWORK_HINT]
    if strikes and limit_strikes:
        parts.append(f"strike {strikes} of {limit_strikes}")
    return trim(SEP.join(parts), limit)


def refused(strikes: int) -> str:
    return f"{REFUSED_DESCRIPTION_PREFIX} {strikes} {DO_NOT_SHIP_LABEL} decisions{SEP}open a new pull request"


def unavailable(cause: str) -> str:
    return SEP.join((NOT_REVIEWED_YET, f"reviewers unavailable ({cause})", RETRIES_HOURLY, RETRIES_NOW))


def incomplete_read(attempts: int) -> str:
    noun = "attempt" if attempts == 1 else "attempts"
    return SEP.join((NOT_REVIEWED_YET, f"reviewer did not read every changed hunk ({attempts} {noun})", RETRIES_HOURLY))


def not_reviewed_reason(reason: str) -> str:
    text = " ".join(short_sha(reason).split())
    if text.startswith(EDITED_REASON):
        return EDITED
    fixed = SEP.join((NOT_REVIEWED, "", "see the run log"))
    return SEP.join((NOT_REVIEWED, hard_clip(text, DESCRIPTION_LIMIT - len(fixed)), "see the run log"))


def with_degraded(description: str, providers: Sequence[str], limit: int) -> str:
    if not providers:
        return description
    room = limit - len(description) - len(DEGRADED_MARK)
    if room < 1:
        return description
    return (
        f"{description}{DEGRADED_MARK}{ids_text(providers, room, ', ')}" if len(providers[0]) <= room else description
    )


def repost(base: str) -> str:
    return trim(base.removesuffix(REPOSTED_MARK), REPOST_LIMIT) + REPOSTED_MARK


def with_run_suffix(description: str, run: object) -> str:
    suffix = run_suffix_text(run)
    return trim(description, DESCRIPTION_LIMIT - len(suffix)) + suffix


def sweep_failure(steps: Iterable[str]) -> str:
    tail = ". Rerun with /review or push a fix"
    named = "; ".join(steps) or "an earlier step"
    room = DESCRIPTION_LIMIT - len(SWEEP_FAILURE_SURFACE_PREFIX) - len(tail)
    return f"{SWEEP_FAILURE_SURFACE_PREFIX}{hard_clip(named, room)}{tail}"


def gate_pass_standin(login: object) -> str:
    return SEP.join((GATE_PASS, f"stand-in review by {login}"))


def gate_waiting(head_sha: str) -> str:
    return SEP.join((GATE_WAITING_PREFIX, f"owner bypass: '{STANDIN_APPROVAL} {head_sha}'"))


def gate_open_findings(dispute_grammar: str) -> str:
    return SEP.join(
        (
            GATE_BLOCKED,
            "open review findings",
            f"fix them or reply '{dispute_grammar}' in the thread (a stand-in cannot waive findings)",
        )
    )


def gate_escalated(ids: Sequence[str], adjudication_grammar: str) -> str:
    stem = f"{GATE_BLOCKED}{SEP}"
    ruling = f" need an owner ruling: '{adjudication_grammar}', or push a fix"
    short = f" need an owner ruling{SEP}see the review-verdict status"
    for tail in (ruling, short):
        room = DESCRIPTION_LIMIT - len(stem) - len(tail)
        shown = ids_text(ids, room)
        if len(shown) <= room:
            return f"{stem}{shown}{tail}"
    return f"{stem}{ids[0]}{short}"


def gate_state(verdict: object) -> str:
    return SEP.join((GATE_BLOCKED, f"review-verdict is {verdict}", f"{PUSH_NEW_HEAD}, or {OWNER_STANDIN}"))


def gate_refusals(what: str, reasons: Sequence[str]) -> str:
    stem = f"{GATE_BLOCKED}{SEP}{what}{SEP}"
    more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
    first = " ".join(short_sha(reasons[0]).split()) if reasons else ""
    return f"{stem}{hard_clip(first, DESCRIPTION_LIMIT - len(stem) - len(more))}{more}"


def gate_error(cause: object) -> str:
    text = " ".join(str(cause).split())
    text = _HTTP_FAILURE.sub(" (HTTP \\g<code>)", text)
    text = _URL.sub("", text).strip()
    tail = f"{SEP}see the run log"
    stem = f"{GATE_ERROR}{SEP}"
    return f"{stem}{hard_clip(short_sha(text) or 'evaluation failed', DESCRIPTION_LIMIT - len(stem) - len(tail))}{tail}"
