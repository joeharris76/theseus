from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    adjudication,
    dedup,
    layout,
    protocol,
    providers,
    redaction_check,
    selection,
    status_text,
    suggestions,
    unavailable,
)
from .policy import SEVERITIES
from .retry import (
    ALL_ABSENT,
    FAILURE,
    INTEGRITY,
    MARKER_LIMIT,
    OMP_INSTALL,
    OVERSIZE,
    PENDING,
    STOPPED,
    SUCCESS,
    UNAVAILABLE,
    UNREPORTED,
)
from .selection import Attempt, Step
from .verdict import (
    MAX_DETAIL,
    MAX_EVIDENCE,
    Finding,
    Placement,
    PriorDefect,
    Verdict,
    comment_span,
    place,
    sanitize,
)

BODY_LIMIT = 65536
BODY_CUT_NOTICE = "Size limit: the rest of this review was cut to stay under GitHub's character limit."
TRUNCATED = "… [truncated]"
OVERSIZE_DESCRIPTION = status_text.OVERSIZE
OVERSIZE_NOTE = (
    "This pull request is too large for the review: the reading it needs is more than any reviewer can hold, "
    "so no reviewer can run and this head is not retried. Split it into smaller pull requests, or ask the "
    "owner for a stand-in review of this head."
)
MARKER_LIMIT_DESCRIPTION = status_text.MARKER_LIMIT
MARKER_LIMIT_NOTE = (
    "The decision and its defects are listed in full in this review, but the protocol record they must be saved in "
    "exceeds the size limit of a review marker even after shortening titles and paths. This head has no recorded "
    "decision and is not retried; push a new head with fewer or shorter findings. The defects below carry no "
    "identifiers and open no tracked threads, so they are not carried into the next review."
)
NOTICE_CAUSES = (ALL_ABSENT, UNAVAILABLE, UNREPORTED, STOPPED, OVERSIZE)
STATES = {selection.PASS: SUCCESS, selection.FAIL: FAILURE, selection.PENDING: PENDING}
STEP_BACK_NOTE = (
    "Step back: this pull request has had {rounds} review rounds with findings, so this round re-read the whole"
    " change for the classes of defect already listed. Before pushing again, reconsider the approach rather than"
    " patching each finding one at a time."
)


@dataclass(frozen=True)
class Final:
    state: str
    description: str
    body: str
    review: dict[str, Any] | None
    reviewer: str | None
    pending_cause: str | None = None
    strikes: int | None = None
    judgement: protocol.Judgement | None = None
    marker: str | None = None
    degraded: str = ""
    classification_note: str = ""
    detail: str = ""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + TRUNCATED


def _where(file: str, line: int, end_line: int | None) -> str:
    return f"{layout.shown_path(file)}:{layout.span(line, end_line)}"


def _item(
    severity: str,
    defect_id: str | None,
    title: str,
    file: str,
    line: int,
    end_line: int | None,
    detail: str,
    *,
    threaded: bool = False,
) -> list[str]:
    """One finding: a single line, or a collapsed block when something would otherwise be lost.

    A finding whose thread shows the detail is one line. Without a thread the detail goes in the block, with the
    title in full when the displayed one was shortened.
    """
    if threaded:
        return [layout.finding_line(severity, defect_id, title, layout.shown_path(file), " (thread below)")]
    held = [text for text in (layout.neutralize(title) if layout.is_shortened(title) else "", detail) if text]
    if not held:
        return [layout.finding_line(severity, defect_id, title, _where(file, line, end_line))]
    inner = [piece for index, text in enumerate(held) for piece in (*([""] if index else []), text)]
    plain = f"{layout.plain_path(file)}:{layout.span(line, end_line)}"
    return layout.collapsible(layout.finding_summary(severity, defect_id, title, plain), inner)


def _finding_item(
    finding: Finding,
    defect_id: str | None,
    cap: int,
    threaded: bool = False,
    severity: str | None = None,
) -> list[str]:
    detail = layout.neutralize(_cut(finding.detail, cap)) if finding.detail else ""
    return _item(
        severity or finding.severity,
        defect_id,
        finding.title,
        finding.file,
        finding.line,
        finding.end_line,
        detail,
        threaded=threaded,
    )


def _bullet(finding: Finding, cap: int) -> list[str]:
    head = layout.finding_line(
        finding.severity, None, finding.title, _where(finding.file, finding.line, finding.end_line), full=True
    )
    if not finding.detail:
        return [head]
    return [head, "  " + layout.neutralize_inline(_cut(finding.detail, cap))]


def _add(lines: list[str], block: list[str]) -> None:
    if lines and lines[-1] and block[0].startswith("<details"):
        lines.append("")
    lines.extend(block)


def _listing(blocks: list[list[str]], title: str = "") -> list[str]:
    if not blocks:
        return []
    lines = [f"**{title}**", ""] if title else []
    for block in blocks:
        _add(lines, block)
    if lines[-1]:
        lines.append("")
    return lines


def _threads_to_open(
    plan: Mapping[str, Any],
    judgement: protocol.Judgement | None,
    commentable: Mapping[str, frozenset[int]],
    delivery: str,
    cycle: int,
    shared_room: dict[str, int] | None = None,
) -> list[tuple[Finding, str]]:
    """Open findings carried from earlier rounds that have no thread: thread the anchorable ones."""
    if judgement is None or judgement.decision != protocol.SHIP_WITH_FIXES:
        return []
    if delivery != "review" or plan.get("threaded") is None:
        return []
    threaded = frozenset(plan["threaded"])
    known = plan.get("open_findings") or {}
    opened: list[tuple[Finding, str]] = []
    previous_head = (plan.get("protocol") or {}).get("previous_head")
    moved = previous_head is not None and previous_head != plan.get("head_sha")
    prints = {entry["id"]: dedup.fingerprint(entry["file"], entry["title"]) for entry in judgement.carried}
    counts = plan.get("open_counts")
    room = (
        shared_room
        if shared_room is not None
        else {key: int(count) for key, count in (counts if counts is not None else dict.fromkeys(known, 1)).items()}
    )
    if counts is None and shared_room is None:
        for entry in judgement.carried:
            if f"c{cycle}-{entry['id']}" in threaded:
                room[prints[entry["id"]]] = room.get(prints[entry["id"]], 0) - 1
    for entry in judgement.carried:
        if f"c{cycle}-{entry['id']}" in threaded:
            continue
        lines = commentable.get(entry["file"], frozenset())
        line, end = entry["line"], entry.get("end_line")
        if moved or line not in lines:
            # The recorded line belongs to an earlier head: open on the file's first changed line instead.
            if not moved or not lines:
                continue
            line, end = min(lines), None
        finding = Finding(
            entry["severity"],
            entry["file"],
            line,
            entry["title"],
            f"Opened on the first changed line of the file; the finding was recorded at line {entry['line']} of an earlier head."
            if line != entry["line"]
            else "",
            end if isinstance(end, int) and end > line else None,
        )
        key = prints[entry["id"]]
        if key in known and room.get(key, 0) > 0:
            room[key] -= 1
            continue
        opened.append((finding, entry["id"]))
    return opened


def _entry_item(entry: Mapping[str, Any], threaded: bool = False) -> list[str]:
    return _item(
        entry["severity"],
        entry["id"],
        entry["title"],
        entry["file"],
        entry["line"],
        entry.get("end_line"),
        "",
        threaded=threaded,
    )


def _size_notice(listed: list[Finding], cap: int) -> str:
    trimmed = sum(len(finding.detail) > cap for finding in listed)
    return (
        f"Size limit: the detail of {trimmed} of {len(listed)} listed defect(s) was shortened so this review stays"
        f" under GitHub's {BODY_LIMIT}-character limit; every defect is still listed with its title and location."
    )


def _fit(render: Callable[[int | None, int | None], str]) -> str:
    body = render(None, None)
    if len(body) <= BODY_LIMIT:
        return body
    low, high, best = 0, MAX_DETAIL, render(0, None)
    while low <= high:
        mid = (low + high) // 2
        candidate = render(mid, None)
        if len(candidate) <= BODY_LIMIT:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    if len(best) <= BODY_LIMIT:
        return best
    low, high = 0, MAX_EVIDENCE
    best = render(0, 0)
    while low <= high:
        mid = (low + high) // 2
        candidate = render(0, mid)
        if len(candidate) <= BODY_LIMIT:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best if len(best) <= BODY_LIMIT else _hard_cut(best)


def _hard_cut(body: str) -> str:
    opens = (unavailable.MARKER_OPEN, layout.VERDICT_MARKER_OPEN, protocol.MARKER_OPEN)
    starts = [at for at in (body.rfind(marker) for marker in opens) if at >= 0]
    split = min(starts, default=-1)
    head, tail = (body, "") if split < 0 else (body[:split], body[split:])
    notice = f"\n\n{BODY_CUT_NOTICE}\n"
    room = BODY_LIMIT - len(tail) - len(notice)
    return body if room <= 0 else head[:room].rstrip() + notice + tail


def _record(
    plan: Mapping[str, Any],
    *,
    kind: str,
    decision: str,
    reviewer: str,
    open_defects: list[dict[str, Any]],
    next_id: int,
    strikes_after: int,
    summary: str = "",
    carried: bool = False,
    secrets: tuple[str, ...] = (),
    escalated: Sequence[str] = (),
    adjudicated: Sequence[Mapping[str, str]] = (),
    disputed: Sequence[str] = (),
    promotion_record: Mapping[str, Any] | None = None,
    coverage: bool = False,
    advisory: Sequence[Mapping[str, Any]] = (),
) -> str:
    rules = plan["protocol"]
    record = {
        "v": protocol.MARKER_VERSION,
        "cycle": rules["cycle"],
        "round": rules["round"],
        "kind": kind,
        "decision": decision,
        "head_sha": plan["head_sha"],
        "base_ref": rules["base_ref"],
        "reviewer": reviewer,
        "tier": reviewer,
        "strikes_after": strikes_after,
        "open_defects": open_defects,
        "next_id": next_id,
        "patch_digest": rules["patch_digest"],
        "carried": carried,
        "forced": bool(rules.get("forced", False)),
        "summary": summary if decision == protocol.DO_NOT_SHIP else "",
    }
    if escalated:
        record["escalated"] = list(escalated)
    if disputed:
        record["disputed"] = list(disputed)
    if adjudicated:
        record["adjudicated"] = [dict(item) for item in adjudicated]
    if rules.get("step_back") and kind in (protocol.FIRST, protocol.FOLLOW_UP):
        record["step_back"] = 1
    if advisory:
        record["advisory"] = [dict(item) for item in advisory[: protocol.ADVISORY_RECORD_LIMIT]]
    if coverage and decision in (protocol.SHIP, protocol.SHIP_WITH_FIXES):
        record["coverage"] = int(coverage)
    if promotion_record is not None and not carried and decision == protocol.SHIP and not open_defects:
        record["promotion"] = dict(promotion_record)
    if bound_run(plan):
        record["run_uuid"] = bound_run(plan)
    return protocol.encode_marker(redaction_check.redact(record, secrets), rules.get("patch_map"))


def _strike_line(plan: Mapping[str, Any], strikes: int) -> list[str]:
    limit = (plan.get("protocol") or {}).get("max_do_not_ship")
    if not strikes or not limit:
        return []
    return [f"DO NOT SHIP decisions on this pull request: {strikes} of {limit}.", ""]


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"**{title}**", "", *lines, ""] if lines else []


def _head(plan: Mapping[str, Any], mark: str, text: str) -> list[str]:
    shadow = " · shadow (advisory)" if plan.get("mode") == "shadow" else ""
    icon = layout.DECISION_MARKS.get(mark)
    return [f"### {icon + ' ' if icon else ''}{text}{shadow}", ""]


def _ids(values: Sequence[str]) -> str:
    return ", ".join(f"**{value}**" for value in values)


def _meta(
    plan: Mapping[str, Any],
    severities: Sequence[str],
    fixed: Sequence[str],
    withdrawn: Sequence[str],
    dispute: bool,
) -> list[str]:
    """The second line: counts, the reviewed head, what changed, what is fixed, and how to contest a finding."""
    shown = layout.counts(severities)
    parts = [shown] if shown else []
    parts.append(f"{'reviewed' if shown else 'Reviewed'} {plan['head_sha']}")
    if plan.get("scope") == "changed" and plan.get("reviewed_head"):
        parts.append(f"changes since {plan['reviewed_head']}")
    for label, found in (("fixed", fixed), ("withdrawn as never defects", withdrawn)):
        if found:
            parts.append(f"{label}: {_ids(found)} (resolve {'its thread' if len(found) == 1 else 'their threads'})")
    if dispute:
        parts.append(f"fix each one, or contest it with a comment `{protocol.COMMENT_DISPUTE_GRAMMAR}`")
    return [" · ".join(parts), ""]


def _close(
    lines: Sequence[str], plan: Mapping[str, Any], state: str, record: str | None, unavailable_marker: str = ""
) -> str:
    """The body: the visible text, the verdict marker, any unavailable marker, then the record as the final bytes.

    Only the `review-verdict` context writes a verdict marker: a shadow or legacy context must never look like a
    receipt for the gate.
    """
    body = "\n".join(lines).rstrip()
    if plan.get("status_context") == layout.VERDICT_CONTEXT:
        body += "\n\n" + layout.verdict_marker(state, plan["head_sha"])
    for part in (unavailable_marker, record):
        if part:
            body += ("\n" if body.endswith("-->") else "\n\n") + part
    return body


def _reviewer_line(plan: Mapping[str, Any], name: str | None, ran: str = "") -> list[str]:
    spec = next((item for item in plan["chain"] if item["name"] == name), None)
    if spec is None:
        return []
    effort = f", effort {spec['effort']}" if spec.get("effort") else ""
    # Code spans: a model such as "@plan" would otherwise notify the GitHub user of that name.
    line = f"Reviewer: `{name} (role {spec['role']}, {spec['harness']} {spec['model']}{effort})`."
    return [f"{line} Model that ran: `{ran}`." if ran else line, ""]


def _served(plan: Mapping[str, Any]) -> providers.Served | None:
    return providers.Served.from_json(plan["served"]) if plan.get("served") else None


def _degraded(plan: Mapping[str, Any], attempts: Sequence[Attempt]) -> list[str]:
    return providers.degraded_providers(plan.get("routed") or (), _served(plan), selection.excluded_providers(attempts))


def _availability(plan: Mapping[str, Any], attempts: Sequence[Attempt], degraded: Sequence[str]) -> list[str]:
    served = _served(plan)
    lines: list[str] = []
    if served is None and plan.get("served_error"):
        return [
            f"- the gateway's served models are unknown: {unavailable.gateway_cause(str(plan['served_error'])).reason}"
        ]
    if served is None:
        return lines
    unserved = [item for item in degraded if item not in served.providers]
    excluded = sorted(selection.excluded_providers(attempts))
    if unserved:
        lines.append(f"- not served by the gateway: {', '.join(unserved)}")
    for attempt in attempts:
        if selection.provider_failed(attempt):
            lines.append(unavailable.excluded_line(attempt, plan))
    if degraded or excluded:
        lines.append(f"- served: {', '.join(sorted(served.providers)) or 'none'}")
    for attempt in attempts:
        if attempt.target and attempt.nominal and attempt.target != attempt.nominal:
            lines.append(f"- {attempt.reviewer} (attempt {attempt.slot}): {attempt.nominal} → {attempt.target}")
    return lines


def _classification_lines(note: str) -> list[str]:
    return [note, ""] if note else []


def _absences(attempts: list[Attempt], step: Step, plan: Mapping[str, Any]) -> list[str]:
    return unavailable.lines(attempts, step, plan)


def _review_comment(
    finding: Finding,
    commentable: Mapping[str, frozenset[int]],
    defect_id: str | None = None,
    cycle: int = 1,
    plan: Mapping[str, Any] | None = None,
    workspace: Path | str | None = None,
) -> dict[str, Any]:
    span = comment_span(finding, commentable)
    anchor: dict[str, Any] = {"line": finding.line, "side": "RIGHT"}
    if span is not None:
        anchor = {"start_line": span[0], "start_side": "RIGHT", "line": span[1], "side": "RIGHT"}
    # A thread with no recorded id cannot be disputed, so it keeps the plain title and carries no dispute footer.
    title = (
        layout.thread_title(finding.severity, defect_id, finding.title)
        if defect_id
        else f"**{finding.severity}**: {layout.neutralize_inline(finding.title, leading=False)}"
    )
    rules = (plan or {}).get("brief_rules") or {}
    detail = layout.neutralize(finding.detail)
    if rules.get("structured_findings"):
        detail = layout.label_paragraphs(detail)
    parts = [title, detail]
    if plan is not None and defect_id and rules.get("suggestions"):
        parts.append(suggestions.block(finding, plan, (span or (finding.line, finding.line)), commentable, workspace))
    body = "\n\n".join(part for part in parts if part).rstrip()
    if defect_id:
        body += f"\n\n{layout.THREAD_FOOTER}\n\n<!-- review-defect: c{cycle}-{defect_id} -->"
    return {"path": finding.file, **anchor, "body": body + f"\n\n{dedup.marker(finding)}"}


def _review_payload(
    plan: Mapping[str, Any],
    body: str,
    placement: Placement,
    commentable: Mapping[str, frozenset[int]],
    defect_ids: Mapping[Finding, str] | None = None,
    cycle: int = 1,
    workspace: Path | str | None = None,
) -> dict[str, Any]:
    return {
        "commit_id": plan["head_sha"],
        "event": "COMMENT",
        "body": body,
        "comments": [
            _review_comment(finding, commentable, (defect_ids or {}).get(finding), cycle, plan, workspace)
            for finding in placement.inline
        ],
    }


def _settled(plan: Mapping[str, Any], step: Step) -> tuple[Step, str]:
    if step.kind == selection.REVIEW:
        reason = f"{step.reviewer.name if step.reviewer else 'a reviewer'}: selected but did not report"
        return Step(selection.PENDING, None, (*step.reasons, reason)), UNREPORTED
    if step.kind == selection.PENDING and step.stopped:
        return step, STOPPED
    if step.kind == selection.PENDING and (step.oversize or plan.get("brief_mode") == "oversize"):
        return step, OVERSIZE
    if step.kind == selection.PENDING and step.unavailable:
        return step, UNAVAILABLE
    return step, ALL_ABSENT


def _marked(
    plan: Mapping[str, Any],
    judgement: protocol.Judgement,
    reviewer: str,
    ids: Mapping[Finding, str],
    next_id: int,
    strikes: int,
    secrets: tuple[str, ...],
    escalated: Sequence[str] = (),
    disputed: Sequence[str] = (),
    advisory: Mapping[Finding, str] | None = None,
) -> str | None:
    open_defects = [*judgement.carried, *(protocol.defect_entry(ids[item], item) for item in judgement.defects)]
    try:
        return _record(
            plan,
            kind=plan["protocol"]["kind"],
            decision=judgement.decision,
            reviewer=reviewer,
            open_defects=open_defects,
            next_id=next_id + len(judgement.defects) + len(advisory or {}),
            strikes_after=strikes,
            escalated=escalated,
            disputed=disputed,
            summary=judgement.summary,
            secrets=secrets,
            promotion_record=plan.get("promotion")
            if isinstance(plan.get("promotion"), Mapping) and judgement.decision == protocol.SHIP and not open_defects
            else None,
            coverage=bool(plan.get("coverage")),
            advisory=[protocol.defect_entry(defect_id, item) for item, defect_id in (advisory or {}).items()],
        )
    except protocol.MarkerError:
        return None


def defect_ids(plan: Mapping[str, Any], defects: Sequence[Finding]) -> dict[Finding, str]:
    next_id = int((plan.get("protocol") or {}).get("next_id", 1))
    return {finding: f"D{next_id + index}" for index, finding in enumerate(defects)}


def _severities(entries: Sequence[Mapping[str, Any]], findings: Sequence[Finding]) -> list[tuple[str, int]]:
    return status_text.severity_counts(
        [*(str(entry.get("severity")) for entry in entries), *(finding.severity for finding in findings)],
        SEVERITIES,
    )


def decision_description(
    decision: str,
    state: str,
    *,
    open_entries: Sequence[Mapping[str, Any]] = (),
    open_findings: Sequence[Finding] = (),
    escalated: Sequence[str] = (),
    fixed: Sequence[str] = (),
    strikes: int = 0,
    strike_limit: int | None = None,
    tail: str = "",
) -> str:
    limit = status_text.SUCCESS_LIMIT if state == SUCCESS else status_text.REPOST_LIMIT
    if decision == protocol.SHIP:
        return status_text.shipped(fixed, tail, limit)
    if decision == protocol.SHIP_WITH_FIXES:
        return status_text.with_findings(
            status_text.OPEN_FINDINGS_LABEL,
            len(open_entries) + len(open_findings),
            _severities(open_entries, open_findings),
            escalated,
            tail,
            limit,
        )
    return status_text.refused_decision(protocol.LABELS[decision], strikes, strike_limit, tail, limit)


def _unavailable_cause(attempts: Sequence[Attempt], plan: Mapping[str, Any]) -> unavailable.Cause | None:
    absent = [
        attempt for attempt in sorted(attempts, key=lambda item: item.slot) if attempt.outcome == selection.ABSENT
    ]
    if absent:
        return unavailable.attempt_cause(absent[-1])
    return unavailable.QUOTA if plan.get("pool_blocked_until") else None


SHORT_CAUSES = {
    unavailable.QUOTA.key: "provider quota",
    unavailable.PROVIDER_AUTH.key: "provider authentication",
    unavailable.TIMEOUT.key: "reviewer timed out",
    unavailable.INVALID.key: "reviewer returned no usable result",
    unavailable.EMPTY.key: "reviewer returned no usable result",
    unavailable.PROVIDER_ERROR.key: "provider error",
    unavailable.RUN_FAILED.key: "reviewer failed to run",
    unavailable.TOO_LARGE.key: "change exceeds the model context",
    unavailable.INCOMPLETE.key: "reviewer did not read every changed hunk",
}
DEFAULT_SHORT_CAUSE = "no reviewer could run"


GATEWAY_SHORT = {
    unavailable.GATEWAY_REJECTED.key: "model gateway rejected the client token",
    unavailable.GATEWAY_ERROR.key: "model gateway unusable or misconfigured",
}


def _pending_description(
    cause: str, attempts: Sequence[Attempt], plan: Mapping[str, Any], degraded: Sequence[str]
) -> str:
    if cause == OVERSIZE:
        return OVERSIZE_DESCRIPTION
    if cause == STOPPED:
        return status_text.STOPPED
    if cause == UNREPORTED:
        return status_text.UNREPORTED
    if cause == UNAVAILABLE and plan.get("served_error"):
        gateway = unavailable.gateway_cause(str(plan["served_error"]))
        if gateway is unavailable.GATEWAY:
            return status_text.GATEWAY_DOWN
        return status_text.unavailable(GATEWAY_SHORT[gateway.key])
    kind = _unavailable_cause(attempts, plan)
    if kind is unavailable.INCOMPLETE:
        return status_text.incomplete_read(len(attempts))
    if cause == UNAVAILABLE and kind is None:
        return status_text.unavailable("provider outage" if degraded else "no served model can hold this change")
    return status_text.unavailable(SHORT_CAUSES.get(kind.key, DEFAULT_SHORT_CAUSE) if kind else DEFAULT_SHORT_CAUSE)


def _detail(step: Step, cause: str, gone: str) -> str:
    if cause == UNAVAILABLE:
        return f"no reviewer available ({gone})"
    return "; ".join(step.reasons or ("no reviewer available",))


def _reviewer_label(plan: Mapping[str, Any], name: str | None) -> str:
    spec = next((item for item in plan["chain"] if item["name"] == name), None)
    return f"{name} ({spec['harness']} {spec['model']})" if spec is not None else str(name or "")


def _reviewer_notes(
    plan: Mapping[str, Any],
    judgement: protocol.Judgement,
    reviewer: str | None,
    ran: str,
    show_summary: bool,
) -> list[str]:
    """Everything the reviewer said that the author cannot act on: collapsed."""
    inner = _reviewer_line(plan, reviewer, ran)
    if show_summary and judgement.summary:
        inner += [layout.neutralize(judgement.summary), ""]
    if judgement.merged:
        inner += [f"{judgement.merged} identical finding(s) were reported more than once and are listed once.", ""]
    if not inner:
        return []
    return layout.collapsible(
        f"Reviewer notes · <code>{layout.summary_text(_reviewer_label(plan, reviewer))}</code>", inner
    )


def disposition_label(status: str) -> str:
    return {"not_fixed": "maintained", "withdrawn": "withdrawn", "fixed": "fixed"}.get(status, status)


def effective_status(plan: Mapping[str, Any], entry: PriorDefect) -> str:
    forced = bool((plan.get("protocol") or {}).get("forced"))
    if forced and entry.status == protocol.FIXED:
        return protocol.NOT_FIXED
    if forced and entry.status == protocol.WITHDRAWN and not protocol.withdrawal_cited(entry.evidence):
        return protocol.NOT_FIXED
    return entry.status


def _dispositions(plan: Mapping[str, Any], verdict: Verdict | None, evidence_cap: int | None) -> list[str]:
    disputes = protocol.bound_disputes(plan)
    if not disputes or verdict is None:
        return []
    entries = {item.id: item for item in verdict.prior_defects}
    lines = []
    seen: set[tuple[object, object]] = set()
    for dispute in disputes:
        key = (dispute["id"], dispute.get("cycle"))
        if key in seen:
            continue
        seen.add(key)
        entry = entries.get(dispute["id"])
        if entry is None:
            lines.append(f"- {dispute['id']}: no disposition recorded")
        else:
            shown = layout.neutralize_inline(
                entry.evidence if evidence_cap is None else _cut(entry.evidence, evidence_cap)
            )
            suffix = f": {shown}" if shown else ""
            lines.append(f"- {dispute['id']} ({disposition_label(effective_status(plan, entry))}){suffix}")
    return _section("Dispute dispositions", lines)


def _blocks(count: int) -> str:
    return f"{layout.noun(count)} {'blocks' if count == 1 else 'block'} merge"


def _not_reviewed_headline(description: str) -> str:
    """The headline of a not-reviewed body: the status description's own wording, so the two never disagree."""
    lead, _, rest = description.partition(status_text.SEP)
    fact = rest.split(status_text.SEP)[0]
    return f"{lead.upper()}{status_text.SEP}{fact}"


def _headline(
    judgement: protocol.Judgement | None, description: str, unrecorded: bool, strikes: int, limit: Any
) -> tuple[str, str]:
    """The first line of the body: the decision and the one fact that says what happens next."""
    if judgement is not None and not unrecorded:
        label = judgement.label
        if judgement.decision == protocol.SHIP_WITH_FIXES:
            fact = _blocks(judgement.open_count)
        elif judgement.decision == protocol.DO_NOT_SHIP:
            fact = "rework needed" + (f" · strike {strikes} of {limit}" if strikes and limit else "")
        else:
            fact = "no open findings"
        return label, f"{label} · {fact}"
    return "NOT REVIEWED", _not_reviewed_headline(description)


def _rework(summary: str, limit: Any) -> list[str]:
    closing = f" A DO NOT SHIP decision number {limit} closes this pull request to further review." if limit else ""
    return [
        *([layout.neutralize(summary), ""] if summary else []),
        f"**Next:** rework and push. A new head gets a full review.{closing}",
        "",
    ]


def _finding_lines(
    judgement: protocol.Judgement,
    placement: Placement,
    ids: Mapping[Finding, str],
    delivery: str,
    opened: Sequence[tuple[Finding, str]],
    repeated: Sequence[Finding],
    advisory_ids: Mapping[Finding, str],
    cap: int,
) -> list[str]:
    """Every listed finding, most severe first: one line each, collapsed when no thread holds the detail."""
    threaded = frozenset(placement.inline) if delivery == "review" else frozenset()
    new = sorted(
        enumerate((*placement.inline, *placement.summary)),
        key=lambda item: (layout.severity_rank(item[1].severity), item[0]),
    )
    lines = _listing([_finding_item(finding, ids.get(finding), cap, finding in threaded) for _, finding in new])
    opened_ids = {defect_id for _, defect_id in opened}
    still_open = sorted(judgement.carried, key=lambda entry: layout.severity_rank(entry["severity"]))
    lines += _listing(
        [_entry_item(entry, entry["id"] in opened_ids) for entry in still_open],
        "Still open",
    )
    lines += _listing(
        [
            [
                layout.finding_line(
                    finding.severity,
                    ids.get(finding),
                    finding.title,
                    layout.shown_path(finding.file),
                    " (existing thread)",
                )
            ]
            for finding in repeated
        ],
        "Already open as review threads",
    )
    lines += _listing(
        [
            _finding_item(finding, advisory_ids.get(finding), cap, severity=layout.ADVISORY)
            for finding in judgement.advisory
        ],
        "Pre-existing, not blocking",
    )
    if judgement.not_counted:
        lines += layout.collapsible(
            "Not counted: defects in files this follow-up did not review",
            [line for finding in judgement.not_counted for line in _bullet(finding, cap)],
        )
    return lines


def _run_details(
    plan: Mapping[str, Any],
    attempts: Sequence[Attempt],
    step: Step,
    degraded: Sequence[str],
    note: str,
    pending: bool,
) -> list[str]:
    """Attempt logs and provider availability: collapsed, because the author cannot act on them."""
    absences = [layout.neutralize(line) for line in _absences(list(attempts), step, plan)]
    availability = [layout.neutralize(line) for line in _availability(plan, attempts, degraded)]
    inner = [*_classification_lines(layout.neutralize(note)), *absences, *availability]
    if not inner:
        return []
    failed = sum(attempt.outcome == "absent" for attempt in attempts)
    if pending:
        counted = [
            text
            for text in (
                layout.noun(failed, "attempt failed", "attempts failed") if failed else "",
                f"{len(absences) - failed} skipped" if len(absences) > failed else "",
            )
            if text
        ]
        return layout.collapsible("What happened" + (f" · {', '.join(counted)}" if counted else ""), inner)
    did_not_finish = f" · {layout.noun(failed, 'earlier attempt', 'earlier attempts')} did not finish" if failed else ""
    return layout.collapsible(f"Run details{did_not_finish}", inner)


def finalize(
    plan: Mapping[str, Any],
    step: Step,
    errors: list[str],
    attempts: list[Attempt],
    verdicts: Mapping[int, Verdict],
    commentable: Mapping[str, frozenset[int]],
    workspace: Path | str | None = None,
    secrets: tuple[str, ...] = (),
    note: str = "",
) -> Final:
    if errors:
        withheld = (unavailable.ARTIFACTS,)
        lines = [
            *_head(plan, "NOT REVIEWED", _not_reviewed_headline(status_text.INTEGRITY)),
            f"Next step: {unavailable.next_step(withheld)}",
            "",
            *layout.collapsible(
                f"What happened · {layout.noun(len(errors), 'validation error', 'validation errors')}",
                [f"- {unavailable.ARTIFACTS.reason}"],
            ),
        ]
        body = _close(lines, plan, PENDING, None, unavailable.marker(plan["head_sha"], withheld))
        review = (
            _review_payload(plan, body, Placement((), ()), commentable)
            if plan.get("findings_delivery") == "review"
            else None
        )
        return Final(PENDING, status_text.INTEGRITY, body, review, None, INTEGRITY, detail="; ".join(errors))
    step, pending_cause = _settled(plan, step)
    state = STATES[step.kind]
    reviewer = step.reviewer.name if step.reviewer else None
    terminal = next(
        (attempt for attempt in attempts if attempt.reviewer == reviewer and attempt.outcome == step.kind),
        None,
    )
    verdict = verdicts.get(terminal.slot) if terminal is not None else None
    ws = workspace if workspace is not None else plan.get("workspace")
    judgement = protocol.judge_planned(verdict, plan, workspace=ws) if verdict is not None else None
    rules = plan.get("protocol") or {}
    defects = judgement.defects if judgement is not None else ()
    next_id = int(rules.get("next_id", 1))
    ids = defect_ids(plan, defects)
    cycle = rules.get("cycle", 1)
    open_severities = (
        [*(finding.severity for finding in defects), *(entry["severity"] for entry in judgement.carried)]
        if judgement is not None
        else []
    )
    delivery = plan.get("findings_delivery", "comment")
    threaded_ids = plan.get("threaded")
    widen = delivery == "review" and threaded_ids is not None
    carried_at = {
        (dedup.fingerprint(entry["file"], entry["title"]), entry["line"]): entry["severity"]
        for entry in (judgement.carried if judgement is not None else ())
        if widen or (threaded_ids is not None and f"c{cycle}-{entry['id']}" in threaded_ids)
    }
    open_counts = plan.get("open_counts")
    room = {key: int(count) for key, count in open_counts.items()} if open_counts is not None else None
    fresh, repeated = dedup.split(defects, plan.get("open_findings", ()), room, carried_at)
    placement = place(fresh, commentable)
    advisory_ids = (
        {finding: f"D{next_id + len(defects) + index}" for index, finding in enumerate(judgement.advisory)}
        if judgement is not None
        else {}
    )
    opened = _threads_to_open(plan, judgement, commentable, delivery, cycle, room)
    disputed = sorted(item["id"] for item in protocol.bound_disputes(plan)) if rules.get("forced") else []
    escalated = (
        protocol.escalated_ids(rules.get("maintained") or {}, judgement.carried, disputed)
        if judgement is not None and rules.get("forced") and judgement.decision == protocol.SHIP_WITH_FIXES
        else []
    )
    strikes = int(rules.get("strikes", 0))
    if (
        judgement is not None
        and judgement.decision == protocol.DO_NOT_SHIP
        and rules.get("patch_digest") not in rules.get("struck_digests", ())
    ):
        strikes += 1
    marker = (
        _marked(plan, judgement, reviewer or "", ids, next_id, strikes, secrets, escalated, disputed, advisory_ids)
        if judgement and rules
        else None
    )
    unrecorded = bool(judgement and rules and marker is None)
    if unrecorded:
        ids, advisory_ids = {}, {}
        opened = []
        state, pending_cause = PENDING, MARKER_LIMIT
    degraded = _degraded(plan, attempts)
    gone = (
        unavailable.gateway_cause(str(plan["served_error"])).reason
        if plan.get("served_error")
        else (f"{', '.join(degraded)} unavailable" if degraded else "no served model can hold this change")
    )
    if unrecorded:
        description = MARKER_LIMIT_DESCRIPTION
    elif judgement is None:
        description = _pending_description(pending_cause, attempts, plan, degraded)
    else:
        description = decision_description(
            judgement.decision,
            state,
            open_entries=judgement.carried,
            open_findings=judgement.defects,
            escalated=escalated,
            fixed=judgement.fixed,
            strikes=strikes,
            strike_limit=(rules.get("max_do_not_ship") or None),
        )
    absent_causes = unavailable.causes(step, attempts, plan)
    detail = _detail(step, pending_cause, gone) if judgement is None else ""
    degraded_suffix = f"{status_text.DEGRADED_MARK}{', '.join(degraded)}" if degraded else ""
    notice_causes = (
        (unavailable.PENDING_CAUSES[pending_cause],) if pending_cause in unavailable.PENDING_CAUSES else absent_causes
    )
    listed = [*placement.inline, *placement.summary, *(judgement.not_counted if judgement is not None else ())]
    decided = judgement is not None and not unrecorded
    shown_strikes = strikes if decided else int(rules.get("strikes", 0))
    limit = rules.get("max_do_not_ship")
    mark, headline = _headline(judgement, description, unrecorded, shown_strikes, limit)
    ran = terminal.target if terminal is not None and terminal.target != terminal.nominal else ""
    rework = decided and judgement.decision == protocol.DO_NOT_SHIP

    def render(cap: int | None, evidence_cap: int | None = None) -> str:
        active = MAX_DETAIL if cap is None else cap
        lines = _head(plan, mark, headline)
        if judgement is not None:
            lines += _meta(
                plan, open_severities, judgement.fixed, judgement.withdrawn, bool(open_severities) and decided
            )
        if unrecorded:
            lines += [MARKER_LIMIT_NOTE, ""]
        if pending_cause == OVERSIZE:
            lines += [OVERSIZE_NOTE, ""]
        if cap is not None:
            lines += [_size_notice(listed, cap), ""]
        if rules.get("step_back"):
            lines += [STEP_BACK_NOTE.format(rounds=int(rules.get("finding_rounds") or 0)), ""]
        if rework:
            lines += _rework(judgement.summary, limit)
        if judgement is not None:
            lines += _finding_lines(judgement, placement, ids, delivery, opened, repeated, advisory_ids, active)
        lines += _dispositions(plan, verdict, evidence_cap)
        if escalated:
            lines += [
                f"Escalated to the owner: {', '.join(escalated)} stayed open through {protocol.ESCALATION_ROUNDS}"
                f" dispute re-reviews of this patch. The owner may post `{adjudication.GRAMMAR}`.",
                "",
            ]
        if judgement is not None:
            lines += _reviewer_notes(plan, judgement, reviewer, ran, not rework)
        if not rework:
            lines += _strike_line(plan, shown_strikes)
        lines += _run_details(plan, attempts, step, degraded, note, judgement is None)
        hidden = ""
        if judgement is None and not unrecorded and pending_cause in NOTICE_CAUSES:
            if pending_cause != OVERSIZE:
                lines += [f"Next step: {unavailable.next_step(notice_causes)}", ""]
            hidden = unavailable.marker(plan["head_sha"], notice_causes)
        return _close(lines, plan, state, marker, hidden)

    body = _fit(render)
    has_content = bool(
        defects
        or (judgement is not None and judgement.advisory)
        or judgement is None
        or judgement.decision != protocol.SHIP
        or state != SUCCESS
    )
    review = (
        _review_payload(
            plan,
            body,
            Placement((*placement.inline, *(finding for finding, _ in opened)), placement.summary),
            commentable,
            {**ids, **dict(opened)},
            cycle,
            ws,
        )
        if delivery == "review"
        else None
    )
    cause = pending_cause if state == PENDING else None
    final_strikes = strikes if judgement is not None and not unrecorded else None
    return Final(
        state,
        status_text.with_degraded(
            description,
            degraded,
            status_text.SUCCESS_LIMIT if state == SUCCESS else status_text.DESCRIPTION_LIMIT,
        ),
        body if has_content else "",
        review,
        reviewer,
        cause,
        final_strikes,
        judgement,
        marker,
        degraded_suffix,
        detail=detail,
    )


def _carried_head(label: str, decision: str, count: int, origin: str) -> str:
    facts = []
    if decision == protocol.SHIP_WITH_FIXES:
        facts.append(f"{layout.noun(count)} still {'blocks' if count == 1 else 'block'} merge")
    elif decision == protocol.DO_NOT_SHIP:
        facts.append("rework needed")
    return " · ".join([label, *facts, origin])


def _still_open(open_defects: Sequence[Mapping[str, Any]]) -> list[str]:
    contest = (
        [f"Fix each one, or contest it with a comment `{protocol.COMMENT_DISPUTE_GRAMMAR}`.", ""]
        if open_defects
        else []
    )
    return [*contest, *_listing([_entry_item(entry) for entry in open_defects], "Still open")]


def carried(plan: Mapping[str, Any]) -> Final:
    rules = plan["protocol"]
    previous = rules["previous"]
    decision = previous["decision"]
    outcome = SUCCESS if decision == protocol.SHIP else FAILURE
    reviewed_head = plan["reviewed_head"]
    label = protocol.LABELS[decision]
    open_defects = protocol.unique_defects(previous["open_defects"])
    description = decision_description(
        decision,
        outcome,
        open_entries=open_defects,
        escalated=previous.get("escalated", ()),
        strikes=int(previous["strikes_after"]),
        strike_limit=rules.get("max_do_not_ship") or None,
        tail=status_text.carried_tail(reviewed_head),
    )
    moved = reviewed_head != plan["head_sha"]
    lines = [
        *_head(
            plan,
            label,
            _carried_head(
                label,
                decision,
                len(open_defects),
                f"carried from {reviewed_head}" if moved else "decision recorded again for this run",
            ),
        ),
        "The patch is unchanged since that head was reviewed, so no reviewer ran."
        if moved
        else "No reviewer ran: the decision on this head is recorded again, bound to this run.",
        "",
        *_still_open(open_defects),
        *_strike_line(plan, int(previous["strikes_after"])),
    ]
    body = _close(
        lines,
        plan,
        outcome,
        _record(
            plan,
            kind=protocol.CARRY,
            decision=decision,
            reviewer=previous["reviewer"],
            open_defects=open_defects,
            next_id=int(previous["next_id"]),
            strikes_after=int(previous["strikes_after"]),
            summary=previous["summary"],
            carried=True,
            coverage=bool(previous.get("coverage")),
            escalated=previous.get("escalated", ()),
            adjudicated=previous.get("adjudicated", ()),
        ),
    )
    review = {"commit_id": plan["head_sha"], "event": "COMMENT", "body": body, "comments": []}
    delivered = review if plan.get("findings_delivery") == "review" else None
    return Final(outcome, description, body, delivered, None, None, int(previous["strikes_after"]))


def adjudicated(plan: Mapping[str, Any]) -> Final:
    rules = plan["protocol"]
    previous = rules["previous"]
    ruling = plan["adjudication"]
    action, defect_id = ruling["action"], ruling["id"]
    open_defects = [
        item
        for item in protocol.unique_defects(previous["open_defects"])
        if not (action == adjudication.WITHDRAW and item["id"] == defect_id)
    ]
    decision = previous["decision"] if open_defects else protocol.SHIP
    outcome = SUCCESS if decision == protocol.SHIP else FAILURE
    label = protocol.LABELS[decision]
    escalated = [item for item in previous.get("escalated", ()) if item != defect_id]
    settled = [item for item in previous.get("adjudicated", ()) if item["id"] != defect_id]
    settled.append({"id": defect_id, "action": action})
    effect = (
        "The finding no longer blocks this head."
        if action == adjudication.WITHDRAW
        else "The finding stands and its dispute is closed; push a fix to clear it."
    )
    reason = layout.neutralize_inline(sanitize(ruling["reason"], adjudication.REASON_LIMIT))
    lines = [
        *_head(plan, label, _carried_head(label, decision, len(open_defects), f"owner ruling: {action} {defect_id}")),
        f"**Owner ruling:** {action} {defect_id} - {reason}",
        "",
        effect,
        "",
        *_still_open(open_defects),
        *_strike_line(plan, int(previous["strikes_after"])),
    ]
    body = _close(
        lines,
        plan,
        outcome,
        _record(
            plan,
            kind=protocol.CARRY,
            decision=decision,
            reviewer=previous["reviewer"],
            open_defects=open_defects,
            next_id=int(previous["next_id"]),
            strikes_after=int(previous["strikes_after"]),
            carried=True,
            coverage=bool(previous.get("coverage")),
            escalated=escalated,
            adjudicated=settled,
        ),
    )
    review = {"commit_id": plan["head_sha"], "event": "COMMENT", "body": body, "comments": []}
    delivered = review if plan.get("findings_delivery") == "review" else None
    description = decision_description(
        decision,
        outcome,
        open_entries=open_defects,
        escalated=escalated,
        strikes=int(previous["strikes_after"]),
        strike_limit=rules.get("max_do_not_ship") or None,
        tail=status_text.ruling_tail(action, defect_id),
    )
    return Final(outcome, description, body, delivered, None, None, int(previous["strikes_after"]))


REPOST_NOTE = (
    "A rerun does not re-judge an unchanged head. To get a new judgement, push a change, or reply `Dispute:` "
    "with file:line counter-evidence on the finding's review thread and then comment `/review`."
)


def reposted(plan: Mapping[str, Any]) -> Final:
    repost = plan["repost"]
    body = (
        "\n".join(
            [
                *_head(plan, repost["label"], f"{repost['label']} · unchanged (no reviewer ran)"),
                f"Reviewed {plan['head_sha']}",
                "",
                REPOST_NOTE,
            ]
        )
        + "\n"
    )
    base = repost["description"]
    if repost["state"] == SUCCESS:
        base = status_text.trim(base, status_text.SUCCESS_LIMIT)
    return Final(repost["state"], status_text.repost(base), body, None, None, None, None)


def refused(plan: Mapping[str, Any]) -> Final:
    rules = plan["protocol"]
    strikes = int(rules["strikes"])
    lines = [
        *_head(plan, "REFUSED", f"REFUSED · {strikes} DO NOT SHIP decisions"),
        "Close this pull request and open a new one with the reworked change. The limit stops review loops; it is"
        " not a security control, and a new pull request starts a new count.",
    ]
    body = _close(
        lines,
        plan,
        FAILURE,
        _record(
            plan,
            kind=protocol.REFUSAL,
            decision=protocol.REFUSED,
            reviewer="",
            open_defects=[],
            next_id=int(rules.get("next_id", 1)),
            strikes_after=strikes,
        ),
    )
    review = {"commit_id": plan["head_sha"], "event": "COMMENT", "body": body, "comments": []}
    delivered = review if plan.get("findings_delivery") == "review" else None
    return Final(FAILURE, status_text.refused(strikes), body, delivered, None, None, strikes)


def fixed(state: str, description: str) -> Final:
    return Final(state, status_text.trim(description, status_text.DESCRIPTION_LIMIT), "", None, None)


def omp_install_failed() -> Final:
    return Final(PENDING, status_text.OMP_INSTALL, "", None, None, OMP_INSTALL)


def bound_run(plan: Mapping[str, Any]) -> Any:
    return plan.get("run_uuid") or plan.get("run_id")


def status_payload(plan: Mapping[str, Any], final: Final, target_url: str) -> dict[str, Any]:
    description = final.description
    run_id = bound_run(plan)
    if final.state == SUCCESS and run_id:
        description = status_text.with_run_suffix(description, run_id)
    return {
        "state": final.state,
        "context": plan["status_context"],
        "description": description,
        "target_url": target_url,
    }
