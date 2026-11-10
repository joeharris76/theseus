"""Optional, cosmetic minimising of the bot's superseded verdict reviews.

Minimising is presentation only. It sets no edit field on the review (so the edit detectors stay quiet), the review
keeps its protocol record (so history, dedup and the receipt check still read it), and nothing here ever feeds the
verdict, the status, the gate or the record: every failure is reported and swallowed by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import protocol


def carries_record(body: Any) -> bool:
    try:
        return isinstance(body, str) and protocol.decode_marker(body) is not None
    except protocol.MarkerError:
        return False


def superseded(reviews: Sequence[Mapping[str, Any]], bot_login: str, head_sha: str) -> list[str]:
    """Node ids of the bot's record-bearing reviews that a newer record-bearing review for ``head_sha`` replaced.

    Nothing is returned unless the newest record-bearing review is on ``head_sha``: that review (and every review
    without a record, every human review and every inline thread) is never a candidate.
    """
    bearing = [
        review
        for review in sorted(
            (item for item in reviews if protocol.is_app_review(item, bot_login)),
            key=lambda item: (str(item.get("submitted_at") or ""), int(item.get("id") or 0)),
        )
        if carries_record(review.get("body"))
    ]
    if len(bearing) < 2 or bearing[-1].get("commit_id") != head_sha:
        return []
    return [
        node_id
        for node_id in (review.get("node_id") for review in bearing[:-1])
        if isinstance(node_id, str) and node_id
    ]
