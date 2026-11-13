"""Presentation helpers shared by the review body and thread renderers.

Everything here is cosmetic. The machine contracts (the verdict marker, the protocol record, the defect and
finding markers) are owned by their readers; these helpers only format what a person reads.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from . import status_text
from .policy import SEVERITIES
from .verdict import display_path

ADVISORY = "Advisory"
SEVERITY_MARKS = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵", ADVISORY: "⚪"}
DECISION_MARKS = {
    status_text.SHIP_LABEL: "✅",
    status_text.OPEN_FINDINGS_LABEL: "🔧",
    status_text.DO_NOT_SHIP_LABEL: "⛔",
    "NOT REVIEWED": "⏳",
    status_text.REFUSED_LABEL: "🚫",
}
VERDICT_CONTEXT = "review-verdict"
VERDICT_MARKER_OPEN = f"<!-- {VERDICT_CONTEXT}: v2"
TITLE_DISPLAY_LIMIT = 90
STAGED_NEW_PREFIX = ".review-promotion/new/"
THREAD_FOOTER = "<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>"

_SEVERITY_RANK = {name: index for index, name in enumerate((*SEVERITIES, ADVISORY))}
_RAW_TAG = re.compile(r"<(?=/?\s*(?:details|summary)\b)", re.IGNORECASE)
# A backslash does not stop GitHub rendering "[ ]" as a checkbox (checked against its markdown API), a code span does.
_TASK_MARK = re.compile(r"\[([ xX])\]")
_BLOCK_START = re.compile(r"^(?:[#>=+*_-]|`{3,}|~{3,}|\d+[.)])")
# A fence run at the start of a prose line, behind any indent, quote or list prefix.
_PROSE_FENCE = re.compile(r"^((?:[ \t]*(?:>|[-*+]|\d+[.)]))*[ \t]*)(`{3,}|~{3,})", re.MULTILINE)
_FENCE_RUN = re.compile(r"`{3,}|~{3,}")
# A heading may also sit behind list markers ("- # x", "1. ## x"), where GitHub renders it inside the item.
_HEADING = re.compile(r"^((?:[ \t]{0,3}(?:>|(?:[-*+]|\d+[.)])(?=[ \t])))*[ \t]{0,3})(#{1,6})(?=[ \t]|$)", re.MULTILINE)
# A reference definition ("[label]: destination") is invisible and turns a later "[label]" into a link.
_REFERENCE = re.compile(r"^((?:[ \t]*(?:>|(?:[-*+]|\d+[.)])(?=[ \t])))*[ \t]*)\[(?=[^\]\n]*\]:)", re.MULTILINE)
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])(?:[ \t]|$)")
_LABEL = re.compile(r"^(Problem|Impact|Fix):(?=\s|$)")
_INDENTED = re.compile(r"^(?: {4,}|\t)")
_ENTITY_ESCAPED = str.maketrans({char: f"&#{ord(char)};" for char in "*_\\`~["})
_UNDERLINE = re.compile(r"^((?:[ \t]{0,3}>)*[ \t]{0,3})([=-])(?=[=-]*[ \t]*$)")
_BREAKS = re.compile(r"[\r\n\t]+")
# Only a fence at column 0 opens code. An indented or prefixed fence may sit in a list item or quote, where GFM
# judges it by container rules, so it is treated as prose and everything after it is escaped too.
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _scan(text: str) -> tuple[list[tuple[bool, str]], str | None]:
    """Split text into (is_code, line) pairs, fence lines included as code, and the fence left open, if any."""
    opened: str | None = None
    pairs: list[tuple[bool, str]] = []
    for line in text.split("\n"):
        code = opened is not None
        if opened is None:
            if (found := _FENCE_OPEN.match(line)) is not None:
                fence, rest = found.groups()
                if fence[0] != "`" or "`" not in rest:
                    opened, code = fence, True
        elif (found := _FENCE_CLOSE.match(line)) is not None:
            fence, rest = found.groups()
            if fence[0] == opened[0] and len(fence) >= len(opened) and not rest.strip():
                opened, code = None, True
        pairs.append((code, line))
    return pairs, opened


def _indented_code_rows(rows: Sequence[str]) -> frozenset[int]:
    """Rows that GitHub renders as an indented code block: indented 4+ columns, after a blank line, outside any list.

    Anything uncertain (a list item may be open, a paragraph may continue) is left out, so it is escaped.
    """
    code: set[int] = set()
    in_list = False
    after_blank = True
    for index, row in enumerate(rows):
        if not row.strip():
            after_blank = True
            continue
        if _INDENTED.match(row):
            if after_blank and not in_list:
                code.add(index)
            elif index - 1 in code:
                code.add(index)
        elif _LIST_ITEM.match(row):
            in_list = True
        elif after_blank and not row.startswith((" ", "\t")):
            in_list = False
        after_blank = False
    return frozenset(code)


def _prose(chunk: str) -> str:
    rows = chunk.split("\n")
    for index in range(1, len(rows)):
        if rows[index - 1].strip() and (found := _UNDERLINE.match(rows[index])):
            rows[index] = f"{found.group(1)}\\{rows[index][found.end(1) :]}"
    code = _indented_code_rows(rows)
    rows = [
        row if index in code else _PROSE_FENCE.sub(lambda match: f"{match.group(1)}\\{match.group(2)}", row)
        for index, row in enumerate(rows)
    ]
    chunk = "\n".join(rows)
    chunk = chunk.replace("<", "&lt;")  # no raw HTML in prose, at any position or prefix
    chunk = _TASK_MARK.sub(lambda match: f"`[{match.group(1)}]`", chunk)
    chunk = _REFERENCE.sub(lambda match: f"{match.group(1)}\\[", chunk)
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


def neutralize_inline(text: str, *, leading: bool = True) -> str:
    """Make model text inert where it follows other text on the same line (a list item, a label).

    No fence scanning is possible there, so the text is flattened to one line, `<` and task markers are escaped,
    and anything that could start a block or a fence is escaped. `leading` is false when the text sits mid-line,
    after a fixed prefix, where it can never open a block of its own.
    """
    flat = " ".join(display_path(text).split())
    flat = _TASK_MARK.sub(lambda match: f"`[{match.group(1)}]`", flat.replace("<", "&lt;"))
    flat = _FENCE_RUN.sub(lambda match: f"\\{match.group(0)}", flat)
    return f"\\{flat}" if leading and _BLOCK_START.match(flat) else flat


def label_paragraphs(text: str) -> str:
    """Bold a leading `Problem:`, `Impact:` or `Fix:` label on each prose line of already neutralized text.

    Text without such labels, and every line inside a code fence, comes back unchanged.
    """
    pairs, _ = _scan(text)
    return "\n".join(line if code else _LABEL.sub(r"**\1:**", line) for code, line in pairs)


def plain_path(path: str) -> str:
    """The path without the staging directory a promotion review reads from."""
    return _BREAKS.sub(" ", path.removeprefix(STAGED_NEW_PREFIX))


def shown_path(path: str) -> str:
    """The path for a `<code>` element: no character of it can open HTML or markdown, whatever GitHub parses inside."""
    return markup_safe(plain_path(path))


def markup_safe(text: str) -> str:
    return html.escape(text, quote=False).translate(_ENTITY_ESCAPED)


def span(line: int, end_line: int | None) -> str:
    return f"{line}-{end_line}" if end_line else str(line)


def short_title(title: str, limit: int = TITLE_DISPLAY_LIMIT) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def is_shortened(title: str) -> bool:
    return short_title(title) != " ".join(title.split())


def thread_title(severity: str, defect_id: str, title: str) -> str:
    """The first line of a review thread; the finding fingerprint hashes `title` unshortened."""
    return f"{severity_label(severity)} · {defect_id} · {neutralize_inline(title, leading=False)}"


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
    return f"{mark}<b>{severity}</b>{named} · {summary_text(short_title(title))} · <code>{markup_safe(where)}</code>"


def finding_line(
    severity: str, defect_id: str | None, title: str, where: str, note: str = "", *, full: bool = False
) -> str:
    named = f" · {defect_id}" if defect_id else ""
    shown = " ".join(title.split()) if full else short_title(title)
    return f"- {severity_label(severity)}{named} · {neutralize_inline(shown)} · <code>{where}</code>{note}"
