from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from . import protocol

REPLIES_PER_FINDING = 5


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def valid_comment(comment: Mapping[str, Any], attesters: frozenset[str]) -> tuple[str, str, datetime] | None:
    """An unedited attester comment in the exact grammar: (finding id, comment text, created_at)."""
    user = comment.get("user") or {}
    created = _moment(comment.get("created_at"))
    if (
        user.get("login") not in attesters
        or user.get("type") != "User"
        or created is None
        or comment.get("updated_at") not in (None, comment.get("created_at"))
    ):
        return None
    parsed = protocol.parse_comment_dispute(comment.get("body"))
    return (parsed[0], str(comment.get("body") or ""), created) if parsed else None


def latest_moments(comments: Iterable[Mapping[str, Any]], attesters: frozenset[str]) -> list[datetime]:
    found = (valid_comment(comment, attesters) for comment in comments)
    return [item[2] for item in found if item is not None]


def collect(
    comments: Iterable[Mapping[str, Any]], found: protocol.History, attesters: frozenset[str]
) -> list[dict[str, Any]]:
    """Disputes of the latest record's open findings, raised by attester comments in the exact grammar.

    A comment disputes a finding only when it was posted after the review that first listed that finding in the
    current cycle, so an id reused by an earlier cycle cannot be disputed in advance.
    """
    latest = found.latest
    if latest is None or len(found.submitted) != len(found.records):
        return []
    cycle = latest["cycle"]
    open_defects = {item["id"]: item for item in latest.get("open_defects") or ()}
    appeared: dict[str, datetime] = {}
    for record, moment in zip(found.records, found.submitted, strict=True):
        if record["cycle"] != cycle or moment is None:
            continue
        for item in record["open_defects"]:
            appeared.setdefault(item["id"], moment)
    replies: dict[str, list[tuple[datetime, str, str]]] = {}
    for comment in comments:
        valid = valid_comment(comment, attesters)
        if valid is None:
            continue
        defect_id, body, created = valid
        if defect_id not in open_defects or defect_id not in appeared or created <= appeared[defect_id]:
            continue
        login = str((comment.get("user") or {}).get("login"))
        replies.setdefault(defect_id, []).append((created, login, protocol.dispute_claim(body)))
    disputes = []
    for defect_id in sorted(replies):
        chosen = sorted(replies[defect_id])[-REPLIES_PER_FINDING:]
        item = open_defects[defect_id]
        bodies = [{"author": login, "body": text} for _, login, text in chosen]
        disputes.append(
            {
                "id": defect_id,
                "cycle": cycle,
                "path": item["file"],
                "title": item["title"][:300],
                "replies": bodies,
                "claim_hashes": [protocol.claim_hash(protocol.canonical_claim(reply["body"])) for reply in bodies],
            }
        )
    return disputes
