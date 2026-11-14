from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PREFIX = "Review adjudication: "
TRIGGERS = (PREFIX.rstrip(),)
WITHDRAW = "WITHDRAW"
MAINTAIN = "MAINTAIN"
ACTIONS = (WITHDRAW, MAINTAIN)
GRAMMAR = f"{PREFIX}WITHDRAW|MAINTAIN D<n> - <reason>"
REASON_LIMIT = 200
_COMMENT = re.compile(
    rf"{re.escape(PREFIX)}(?P<action>{'|'.join(ACTIONS)}) (?P<id>D[1-9][0-9]{{0,3}}) - "
    rf"(?P<reason>\S[^\n]{{0,{REASON_LIMIT - 1}}})"
)


@dataclass(frozen=True)
class Adjudication:
    action: str
    defect_id: str
    reason: str


def parse(body: str | None) -> Adjudication | None:
    match = _COMMENT.fullmatch((body or "").strip())
    return Adjudication(match["action"], match["id"], match["reason"].rstrip()) if match else None


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def assess(
    comment: Mapping[str, Any], after: datetime | None, attesters: frozenset[str]
) -> tuple[Adjudication | None, str]:
    if comment.get("login") not in attesters or comment.get("user_type") != "User":
        return None, "it is not from the repository owner"
    if comment.get("updated_at") not in (None, comment.get("created_at")):
        return None, "it was edited"
    ruling = parse(comment.get("body"))
    if ruling is None:
        return None, f"it must read {GRAMMAR.removeprefix(PREFIX)}"
    created = _moment(comment.get("created_at"))
    if created is None or after is None or created <= after:
        return None, "it was posted before the latest review of this head"
    return ruling, ""


def valid(comment: Mapping[str, Any], after: datetime | None, attesters: frozenset[str]) -> Adjudication | None:
    return assess(comment, after, attesters)[0]
