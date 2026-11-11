from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import legacy
from .policy import SEVERITIES
from .protocol import thread_finding
from .verdict import Finding

MARKER_OPEN = "<!-- review-finding:"
_MARKER = re.compile(
    rf"(?:{re.escape(MARKER_OPEN)}|{re.escape(legacy.FINDING_MARKER_OPEN)}) (?P<print>[0-9a-f]{{16}}) -->\s*\Z"
)
_SEVERITY = "(?P<severity>Critical|High|Medium|Low)"
_TITLES = (
    re.compile(rf"^\*\*{_SEVERITY}\*\*: (?P<title>.+)$", re.MULTILINE),
    re.compile(rf"^(?:🔴|🟠|🟡|🔵|⚪) \*\*{_SEVERITY}\*\* · D[1-9][0-9]{{0,3}} · (?P<title>.+)$", re.MULTILINE),
)
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def fingerprint(path: str, title: str) -> str:
    normal = _NOT_WORD.sub(" ", title.lower()).strip()
    return hashlib.sha256(f"{path}\0{normal}".encode()).hexdigest()[:16]


def marker(finding: Finding) -> str:
    return f"{MARKER_OPEN} {fingerprint(finding.file, finding.title)} -->"


def parse_title(body: str) -> tuple[str, str] | None:
    matches = [match for pattern in _TITLES if (match := pattern.search(body)) is not None]
    if not matches:
        return None
    first = min(matches, key=lambda match: match.start())
    return first.group("severity"), first.group("title").strip()


def first_line_title(body: str) -> str:
    lines = body.splitlines()
    first = lines[0] if lines else ""
    for pattern in _TITLES:
        match = pattern.fullmatch(first)
        if match is not None:
            return match.group("title").strip()
    return first


def thread_fingerprint(path: str | None, body: str) -> str | None:
    match = _MARKER.search(body.rstrip())
    if match is not None:
        return match.group("print")
    title = parse_title(body)
    return fingerprint(path, title[1]) if path and title else None


def open_fingerprints(threads: Iterable[Mapping[str, Any]], login: str) -> dict[str, str]:
    prints: dict[str, str] = {}
    for thread in threads:
        if (
            thread.get("resolved")
            or thread.get("edited")
            or thread.get("author_type") != "Bot"
            or (thread.get("author") or "").removesuffix("[bot]") != login
        ):
            continue
        body = thread.get("body") or ""
        key = thread_fingerprint(thread.get("path"), body)
        title = parse_title(body)
        if key is not None:
            severity = title[0] if title else SEVERITIES[-1]
            prints[key] = min(prints.get(key, severity), severity, key=SEVERITIES.index)
    return dict(sorted(prints.items()))


def open_thread_counts(threads: Iterable[Mapping[str, Any]], login: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for thread in threads:
        if (
            thread.get("resolved")
            or thread.get("edited")
            or thread.get("author_type") != "Bot"
            or (thread.get("author") or "").removesuffix("[bot]") != login
        ):
            continue
        body = thread.get("body") or ""
        if thread_finding(body) is not None:
            continue
        key = thread_fingerprint(thread.get("path"), body)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def split(
    findings: Iterable[Finding],
    already_open: Mapping[str, str] | Iterable[str],
    counts: Mapping[str, int] | None = None,
    carried_at: Mapping[tuple[str, int], str] | None = None,
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    known = dict(already_open) if isinstance(already_open, Mapping) else dict.fromkeys(already_open, SEVERITIES[0])
    fresh: list[Finding] = []
    repeated: list[Finding] = []
    placed: dict[tuple[str, int], str] = {}
    room = dict(counts or {})
    for finding in findings:
        key = fingerprint(finding.file, finding.title)
        posted = known.get(key)
        covered = posted is not None and SEVERITIES.index(posted) <= SEVERITIES.index(finding.severity)
        held = (carried_at or {}).get((key, finding.line))
        if held is not None and SEVERITIES.index(held) <= SEVERITIES.index(finding.severity):
            covered = True
        elif covered and counts is not None:
            covered = room.get(key, 0) > 0
            room[key] = room.get(key, 0) - 1
        here = (key, finding.line)
        twin = placed.get(here)
        twinned = twin is not None and SEVERITIES.index(twin) <= SEVERITIES.index(finding.severity)
        (repeated if covered or twinned else fresh).append(finding)
        if not covered and not twinned:
            placed[here] = finding.severity
    return tuple(fresh), tuple(repeated)
