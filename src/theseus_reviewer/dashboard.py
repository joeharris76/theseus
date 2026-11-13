"""The optional sticky dashboard comment: one bot comment per pull request, edited in place after each verdict.

It is a presentation surface only. No reader, the gate, deduplication or the digest parses it; the verdict review, its
protocol record and the statuses stay authoritative. The comment is the one post Theseus edits, and it is an issue
comment, not a review, so the review edit detectors never see it. Nothing here may change a verdict, a status or the
gate: the caller reports and swallows every failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import layout, protocol

MARKER = "<!-- review-dashboard: v1 -->"
NON_AUTHORITATIVE = (
    "<sub>A non-authoritative summary, edited in place after each review. The verdict review, its protocol record "
    "and the `review-verdict` status are authoritative; nothing reads this comment.</sub>"
)
NO_THREAD = "no thread"
TITLE_LIMIT = 90
# The workflow triggers match these phrases anywhere in a comment (the first two) or at its start, so model-written
# text never carries one intact.
_COMMAND_PHRASE = re.compile(r"(Stand-in review|Omp candidate check|Review adjudication)(\s*):", re.IGNORECASE)
_MENTION = re.compile(r"@(?=[^@ \t\r\n\f\v])")


def _cell(text: str) -> str:
    inert = layout.neutralize_inline(layout.short_title(text, TITLE_LIMIT))
    inert = _COMMAND_PHRASE.sub(lambda found: f"{found.group(1)} :", inert)
    return _MENTION.sub("\u200b@", inert).replace("|", "\\|")


def thread_urls(threads: Sequence[Mapping[str, Any]], bot_login: str, cycle: int) -> dict[str, str]:
    """Finding id to the url of the bot's thread that carries that finding in ``cycle``."""
    found: dict[str, str] = {}
    for thread in threads:
        if thread.get("author_type") != "Bot" or (thread.get("author") or "").removesuffix("[bot]") != bot_login:
            continue
        marker = protocol.thread_finding(thread.get("body") or "")
        url = thread.get("url")
        if marker is not None and marker[0] == cycle and isinstance(url, str) and url.startswith("https://"):
            found.setdefault(marker[1], url)
    return found


def render(record: Mapping[str, Any], urls: Mapping[str, str]) -> str:
    defects = sorted(
        record["open_defects"], key=lambda item: (layout.severity_rank(item["severity"]), int(item["id"][1:]))
    )
    label = protocol.LABELS.get(record["decision"], str(record["decision"]))
    mark = layout.DECISION_MARKS.get(label, "")
    open_text = f"{len(defects)} open" if defects else "no open findings"
    lines = [
        MARKER,
        f"### {mark + ' ' if mark else ''}Review status: {label} · {open_text} · head `{record['head_sha'][:7]}`",
        "",
    ]
    if defects:
        lines += ["| Finding | ID | Title | Thread |", "|---|---|---|---|"]
        for item in defects:
            url = urls.get(item["id"])
            link = f"[thread]({url})" if url else NO_THREAD
            lines.append(
                f"| {layout.severity_label(item['severity'])} | {item['id']} | {_cell(item['title'])} | {link} |"
            )
        lines.append("")
    lines.append(NON_AUTHORITATIVE)
    return "\n".join(lines) + "\n"


def is_dashboard(comment: Mapping[str, Any], bot_login: str) -> bool:
    user = comment.get("user") or {}
    return (
        user.get("type") == "Bot"
        and (user.get("login") or "") == f"{bot_login}[bot]"
        and str(comment.get("body") or "").startswith(MARKER)
    )


def existing(comments: Sequence[Mapping[str, Any]], bot_login: str) -> Mapping[str, Any] | None:
    own = [item for item in comments if is_dashboard(item, bot_login)]
    return min(own, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)), default=None)
