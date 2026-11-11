"""Presentation helpers shared by the review body and thread renderers.

Everything here is cosmetic. The machine contracts (the verdict marker, the protocol record, the defect and
finding markers) are owned by their readers; these helpers only format what a person reads.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from .policy import SEVERITIES
from .verdict import display_path

ADVISORY = "Advisory"
SEVERITY_MARKS = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵", ADVISORY: "⚪"}
DECISION_MARKS = {
    "SHIP": "✅",
    "SHIP WITH FIXES": "🔧",
    "DO NOT SHIP": "⛔",
    "NOT REVIEWED": "⏳",
    "REFUSED": "🚫",
}
VERDICT_CONTEXT = "review-verdict"
VERDICT_MARKER_OPEN = f"<!-- {VERDICT_CONTEXT}: v2"
TITLE_DISPLAY_LIMIT = 90
STAGED_NEW_PREFIX = ".review-promotion/new/"
THREAD_FOOTER = "<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>"

_SEVERITY_RANK = {name: index for index, name in enumerate((*SEVERITIES, ADVISORY))}
_RAW_TAG = re.compile(r"<(?=/?\s*(?:details|summary)\b)", re.IGNORECASE)
_TASK_ITEM = re.compile(r"^([ \t]*(?:>[ \t]*)*(?:[-*+]|\d+[.)])[ \t]+)\[([ xX])\]", re.MULTILINE)
_HEADING = re.compile(r"^([ \t]{0,3})(#{1,6})(?=[ \t]|$)", re.MULTILINE)
_SETEXT = re.compile(r"(?<=\S)\n([ \t]{0,3})([=-])(?=[=-]*[ \t]*(?:\n|$))", re.MULTILINE)
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")


def _scan(text: str) -> tuple[list[tuple[bool, str]], str | None]:
    """Split text into (is_code, line) pairs, fence lines included as code, and the fence left open, if any."""
    opened: str | None = None
    pairs: list[tuple[bool, str]] = []
    for line in text.split("\n"):
        found = _FENCE.match(line)
        code = opened is not None
        if found is not None:
            fence, rest = found.groups()
            if opened is None:
                if fence[0] != "`" or "`" not in rest:
                    opened, code = fence, True
            elif fence[0] == opened[0] and len(fence) >= len(opened) and not rest.strip():
                opened, code = None, True
        pairs.append((code, line))
    return pairs, opened


def _prose(chunk: str) -> str:
    chunk = _SETEXT.sub(lambda match: f"\n{match.group(1)}\\{match.group(2)}", chunk)
    chunk = _TASK_ITEM.sub(lambda match: f"{match.group(1)}`[{match.group(2)}]`", chunk)
    return _HEADING.sub(lambda match: f"{match.group(1)}\\{match.group(2)}", chunk)


def _outside_code(text: str) -> str:
    """Close a fence the text leaves open, and escape headings and task items only where they are prose."""
    pairs, opened = _scan(text)
    out: list[str] = []
    run: list[str] = []
    for code, line in pairs:
        if code:
            if run:
                out.append(_prose("\n".join(run)))
                run = []
            out.append(line)
        else:
            run.append(line)
    if run:
        out.append(_prose("\n".join(run)))
    return "\n".join(out + ([opened] if opened is not None else []))


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, len(_SEVERITY_RANK))


def severity_label(severity: str) -> str:
    mark = SEVERITY_MARKS.get(severity)
    return f"{mark} **{severity}**" if mark else f"**{severity}**"


def verdict_marker(state: str, head_sha: str) -> str:
    return f"{VERDICT_MARKER_OPEN} {state} {head_sha} -->"


def neutralize(text: str) -> str:
    """Make model-written text inert inside the review body.

    It can no longer open or close a collapsible, start a comment or heading, or turn a list item into a task
    list checkbox (ticking one would edit the review, and an edited review holds the pull request).
    """
    return _outside_code(_RAW_TAG.sub("&lt;", display_path(text)))


def plain_path(path: str) -> str:
    """The path without the staging directory a promotion review reads from."""
    return path.removeprefix(STAGED_NEW_PREFIX)


def shown_path(path: str) -> str:
    """The path as a reader should see it in markdown, with comment openers and closers made inert."""
    return display_path(plain_path(path))


def span(line: int, end_line: int | None) -> str:
    return f"{line}-{end_line}" if end_line else str(line)


def short_title(title: str, limit: int = TITLE_DISPLAY_LIMIT) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def is_shortened(title: str) -> bool:
    return short_title(title) != " ".join(title.split())


def thread_title(severity: str, defect_id: str, title: str) -> str:
    """The first line of a review thread; the finding fingerprint hashes `title` unshortened."""
    return f"{severity_label(severity)} · {defect_id} · {title}"


def counts(severities: Iterable[str]) -> str:
    found = Counter(severities)
    shown = [f"{found[name]} {name}" for name in (*SEVERITIES, ADVISORY) if found[name]]
    return f"**{' · '.join(shown)}**" if shown else ""


def noun(count: int, singular: str = "finding", plural: str = "findings") -> str:
    return f"{count} {singular if count == 1 else plural}"


def collapsible(summary: str, lines: Sequence[str]) -> list[str]:
    """A closed `<details>` block; `summary` is HTML and must already be escaped."""
    inner = list(lines)
    while inner and not inner[-1]:
        inner.pop()
    return [f"<details><summary>{summary}</summary>", "", *inner, "", "</details>", ""]


def summary_text(text: str) -> str:
    return html.escape(" ".join(text.split()), quote=False)


def finding_summary(severity: str, defect_id: str | None, title: str, where: str) -> str:
    named = f" · {defect_id}" if defect_id else ""
    mark = f"{SEVERITY_MARKS[severity]} " if severity in SEVERITY_MARKS else ""
    return (
        f"{mark}<b>{severity}</b>{named} · {summary_text(short_title(title))}"
        f" · <code>{html.escape(where, quote=False)}</code>"
    )


def finding_line(
    severity: str, defect_id: str | None, title: str, where: str, note: str = "", *, full: bool = False
) -> str:
    named = f" · {defect_id}" if defect_id else ""
    shown = " ".join(title.split()) if full else short_title(title)
    return f"- {severity_label(severity)}{named} · {neutralize(shown)} · `{where}`{note}"
