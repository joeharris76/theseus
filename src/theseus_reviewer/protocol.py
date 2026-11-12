from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import unicodedata
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import adjudication, legacy, promotion
from .coverage import Returned
from .edits import EditStateError, is_edited
from .evidence import withdrawal_cited
from .policy import SEVERITIES
from .status_text import DO_NOT_SHIP_LABEL, EDITED_REASON, OPEN_FINDINGS_LABEL, REFUSED_LABEL, SHIP_LABEL
from .verdict import (
    COMPLETE,
    DEMOTED_DISPOSITION,
    DEMOTED_STATUSES,
    DO_NOT_SHIP,
    FIXED,
    LINK_REMOVED,
    MAX_CLAIM,
    MAX_LINE,
    NOT_FIXED,
    REDACTED,
    SHIP,
    SHIP_WITH_FIXES,
    WITHDRAWN,
    Finding,
    PriorDefect,
    Verdict,
    sanitize,
)

DO_NOT_SHIP_SUMMARY = 1500
REFUSED = REFUSED_LABEL
LABELS = {
    SHIP: SHIP_LABEL,
    SHIP_WITH_FIXES: OPEN_FINDINGS_LABEL,
    DO_NOT_SHIP: DO_NOT_SHIP_LABEL,
    REFUSED: REFUSED_LABEL,
}
RECORD_DECISIONS = (SHIP, SHIP_WITH_FIXES, DO_NOT_SHIP, REFUSED)

FIRST = "first"
FOLLOW_UP = "follow-up"
CARRY = "carry"
SKIP = "skip"
REPOST = "repost"
REFUSAL = "refused"
KINDS = (FIRST, FOLLOW_UP, CARRY, REFUSAL)

UNREAD_DIGEST = "unread-"
MARKER_VERSION = 1
MARKER_LIMIT = 4096
MARKER_TRIMS = ((120, 120), (80, 80), (50, 60), (32, 40), (24, 24))
MARKER_OPEN = "<!-- review-protocol:"
_MARKER = re.compile(
    rf"(?:{re.escape(MARKER_OPEN)}|{re.escape(legacy.PROTOCOL_MARKER_OPEN)}) v1 (?P<data>[A-Za-z0-9_-]+) -->\s*\Z"
)
MARKER_INFLATE_LIMIT = 64 << 20
DEFECT_ID = re.compile(r"D([1-9][0-9]{0,3})")
NOT_COUNTED_LIMIT = 5
PATCH_HEX = 16
MISSING = "missing"
ABSENT_MODE = "000000"
ABSENT_ID = "0" * 40
ADVISORY_RECORD_LIMIT = 5
RECORD_KEYS = frozenset(
    {
        "v",
        "cycle",
        "round",
        "kind",
        "decision",
        "head_sha",
        "base_ref",
        "reviewer",
        "tier",
        "strikes_after",
        "open_defects",
        "next_id",
        "patch_digest",
        "carried",
        "summary",
    }
)
OPTIONAL_RECORD_KEYS = frozenset(
    {
        "patch_map",
        "forced",
        "run_uuid",
        "escalated",
        "adjudicated",
        "disputed",
        "promotion",
        "coverage",
        "step_back",
        "advisory",
    }
)
DEFECT_KEYS = ("id", "severity", "file", "line", "end_line", "title")


class MarkerError(ValueError):
    pass


@dataclass(frozen=True)
class Judgement:
    decision: str
    defects: tuple[Finding, ...]
    summary: str
    carried: tuple[dict[str, Any], ...] = ()
    fixed: tuple[str, ...] = ()
    not_counted: tuple[Finding, ...] = ()
    withdrawn: tuple[str, ...] = ()
    merged: int = 0
    advisory: tuple[Finding, ...] = ()

    @property
    def label(self) -> str:
        return LABELS[self.decision]

    @property
    def open_count(self) -> int:
        return len(self.carried) + len(self.defects)


@dataclass(frozen=True)
class Round:
    kind: str
    reason: str
    cycle: int = 1
    number: int = 1
    previous: Mapping[str, Any] | None = None
    changed: frozenset[str] | None = None
    forced: bool = False
    step_back: bool = False


@dataclass(frozen=True)
class FileState:
    old_path: str
    new_path: str
    old_mode: str
    new_mode: str
    old_id: str
    new_id: str

    def fields(self) -> list[str]:
        return [self.old_path, self.new_path, self.old_mode, self.new_mode, self.old_id, self.new_id]


@dataclass
class History:
    records: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    submitted: list[datetime | None] = field(default_factory=list)
    ids: list[int | None] = field(default_factory=list)

    @property
    def latest(self) -> dict[str, Any] | None:
        return self.records[-1] if self.records else None

    @property
    def latest_at(self) -> datetime | None:
        return self.submitted[-1] if len(self.submitted) == len(self.records) and self.records else None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _fingerprint(entry: FileState) -> str:
    return hashlib.sha256(json.dumps(entry.fields(), separators=(",", ":")).encode("utf-8")).hexdigest()


def file_state(
    path: str,
    previous_path: str | None,
    old: tuple[str, str] | None,
    new: tuple[str, str] | None,
) -> FileState:
    return FileState(
        old_path=previous_path or path,
        new_path=path,
        old_mode=old[0] if old else ABSENT_MODE,
        new_mode=new[0] if new else ABSENT_MODE,
        old_id=old[1] if old else ABSENT_ID,
        new_id=new[1] if new else ABSENT_ID,
    )


def fingerprints(states: Iterable[FileState]) -> dict[str, str]:
    return {state.new_path: _fingerprint(state) for state in states}


def patch_map(prints: Mapping[str, str], paths: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(paths))
    collided = len({_short(path) for path in wanted}) < len(wanted)
    return {_short(path): MISSING if collided or path not in prints else prints[path][:PATCH_HEX] for path in wanted}


def patch_digest(prints: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(prints, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def changed_paths(previous: Mapping[str, str] | None, current: Mapping[str, str], paths: Iterable[str]) -> frozenset:
    every = frozenset(paths)
    if previous is None or MISSING in previous.values() or MISSING in current.values() or set(previous) - set(current):
        return every
    return frozenset(path for path in every if previous.get(_short(path)) != current.get(_short(path)))


def encode_marker(record: Mapping[str, Any], patches: Mapping[str, str] | None = None) -> str:
    def render(data: Mapping[str, Any]) -> str:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        packed = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")
        return f"{MARKER_OPEN} v{MARKER_VERSION} {packed} -->"

    full = render({**record, "patch_map": dict(patches)}) if patches is not None else ""
    if full and len(full) <= MARKER_LIMIT:
        return full
    for stripped in (
        {**record},
        {key: value for key, value in record.items() if key != "advisory"},
        {key: value for key, value in record.items() if key not in ("advisory", "summary")} | {"summary": ""},
    ):
        trimmed = render(stripped)
        if len(trimmed) <= MARKER_LIMIT:
            return trimmed
    for title_limit, file_limit in MARKER_TRIMS:
        trimmed = render(
            {
                **{key: value for key, value in record.items() if key != "advisory"},
                "summary": "",
                "open_defects": _trim_defects(record["open_defects"], title_limit, file_limit),
            }
        )
        if len(trimmed) <= MARKER_LIMIT:
            return trimmed
    raise MarkerError("the protocol record does not fit in a review marker")


def _clip_head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"[:limit]


def _clip_tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else ("…" + text[len(text) - limit + 1 :])[:limit]


def _trim_defects(defects: Iterable[Mapping[str, Any]], title_limit: int, file_limit: int) -> list[dict[str, Any]]:
    return [
        {**item, "title": _clip_head(item["title"], title_limit), "file": _clip_tail(item["file"], file_limit)}
        for item in defects
    ]


def _valid_defect(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == set(DEFECT_KEYS)
        and isinstance(item["id"], str)
        and DEFECT_ID.fullmatch(item["id"]) is not None
        and item["severity"] in SEVERITIES
        and isinstance(item["file"], str)
        and isinstance(item["line"], int)
        and (item["end_line"] is None or isinstance(item["end_line"], int))
        and isinstance(item["title"], str)
    )


def _valid_patches(patches: Any) -> bool:
    return isinstance(patches, dict) and all(isinstance(v, str) for v in patches.values())


def _valid_escalation(record: Mapping[str, Any]) -> bool:
    escalated = record.get("escalated", [])
    disputed = record.get("disputed", [])
    adjudicated = record.get("adjudicated", [])
    return (
        isinstance(escalated, list)
        and all(isinstance(item, str) and DEFECT_ID.fullmatch(item) for item in escalated)
        and isinstance(disputed, list)
        and all(isinstance(item, str) and DEFECT_ID.fullmatch(item) for item in disputed)
        and isinstance(adjudicated, list)
        and all(
            isinstance(item, dict)
            and set(item) == {"id", "action"}
            and isinstance(item["id"], str)
            and DEFECT_ID.fullmatch(item["id"]) is not None
            and item["action"] in adjudication.ACTIONS
            for item in adjudicated
        )
    )


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict) or not RECORD_KEYS <= set(record) <= RECORD_KEYS | OPTIONAL_RECORD_KEYS:
        return False
    return (
        record["v"] == MARKER_VERSION
        and record["kind"] in KINDS
        and record["decision"] in RECORD_DECISIONS
        and all(isinstance(record[key], int) and record[key] >= 0 for key in ("cycle", "round", "strikes_after"))
        and isinstance(record["next_id"], int)
        and re.fullmatch(r"[0-9a-f]{40}", str(record["head_sha"])) is not None
        and all(isinstance(record[key], str) for key in ("base_ref", "reviewer", "tier", "patch_digest", "summary"))
        and isinstance(record["carried"], bool)
        and ("forced" not in record or isinstance(record["forced"], bool))
        and _valid_escalation(record)
        and ("run_uuid" not in record or re.fullmatch(r"[0-9a-f]{32}|[0-9]+", str(record["run_uuid"])) is not None)
        and isinstance(record["open_defects"], list)
        and all(_valid_defect(item) for item in record["open_defects"])
        and (record.get("patch_map") is None or _valid_patches(record["patch_map"]))
        and ("promotion" not in record or promotion.valid_promotion_record(record["promotion"]))
        and ("coverage" not in record or (type(record["coverage"]) is int and record["coverage"] >= 1))
        and ("step_back" not in record or (type(record["step_back"]) is int and record["step_back"] == 1))
        and (
            "advisory" not in record
            or (
                isinstance(record["advisory"], list)
                and len(record["advisory"]) <= ADVISORY_RECORD_LIMIT
                and all(_valid_defect(item) for item in record["advisory"])
            )
        )
    )


def _clamped(item: dict[str, Any]) -> dict[str, Any]:
    end = item["end_line"]
    return {
        **item,
        "line": max(min(item["line"], MAX_LINE), 1),
        "end_line": None if end is None else max(min(end, MAX_LINE), 1),
    }


def decode_marker(body: str) -> dict[str, Any] | None:
    count = body.count(MARKER_OPEN) + body.count(legacy.PROTOCOL_MARKER_OPEN)
    if count == 0:
        return None
    match = _MARKER.search(body)
    if count > 1 or match is None:
        raise MarkerError("a review carries a malformed or repeated protocol marker")
    data = match.group("data")
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        record = json.loads(raw)
    except (binascii.Error, ValueError, zlib.error) as exc:
        raise MarkerError(f"a protocol marker cannot be decoded: {exc}") from exc
    checked = checked_record(record)
    if checked is None:
        raise MarkerError("a protocol marker has an unexpected shape")
    return checked


def checked_record(record: Any) -> dict[str, Any] | None:
    if not _valid_record(record):
        return None
    return {**record, "open_defects": [_clamped(item) for item in record["open_defects"]]}


def marker_payload(text: str) -> Any:
    match = _MARKER.search(text)
    if match is None:
        return None
    data = match.group("data")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)), MARKER_INFLATE_LIMIT)
        return json.loads(raw) if inflater.eof else None
    except (binascii.Error, ValueError, zlib.error, RecursionError):
        return None


def _review_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_app_review(review: Mapping[str, Any], bot_login: str) -> bool:
    return (
        (review.get("login") or "") == f"{bot_login}[bot]"
        and review.get("user_type") == "Bot"
        and review.get("state") != "DISMISSED"
    )


def history(reviews: Iterable[Mapping[str, Any]], bot_login: str) -> History:
    own = [review for review in reviews if is_app_review(review, bot_login)]
    for review in own:
        if "last_edited_at" not in review or "includes_created_edit" not in review:
            raise EditStateError(f"review {review.get('id')} has no readable edit state")
        if is_edited(review["last_edited_at"], review["includes_created_edit"]):
            return History([], EDITED_REASON)
    found = History()
    for review in sorted(own, key=lambda item: (str(item.get("submitted_at") or ""), int(item.get("id") or 0))):
        try:
            record = decode_marker(review.get("body") or "")
        except MarkerError as exc:
            return History([], str(exc))
        if record is not None:
            found.records.append(record)
            found.submitted.append(_review_time(review.get("submitted_at")))
            found.ids.append(review.get("id"))
    return found


def struck_digests(records: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(
        record["patch_digest"]
        for record in records
        if record["decision"] == DO_NOT_SHIP and record["kind"] in (FIRST, FOLLOW_UP) and not record["carried"]
    )


def strikes(records: Iterable[Mapping[str, Any]]) -> int:
    return len(struck_digests(records))


FORCED_ROUND_LIMIT = 2
ESCALATION_ROUNDS = 2


def forced_rounds(records: Iterable[Mapping[str, Any]], digest: str) -> int:
    return sum(
        1
        for record in records
        if record.get("patch_digest") == digest and record.get("kind") == FOLLOW_UP and record.get("forced")
    )


def maintained_counts(records: Iterable[Mapping[str, Any]], digest: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if record.get("patch_digest") == digest and record.get("kind") == FOLLOW_UP and record.get("forced"):
            disputed = set(record.get("disputed") or ())
            for item in record["open_defects"]:
                if item["id"] in disputed:
                    counts[item["id"]] = counts.get(item["id"], 0) + 1
    return counts


def escalated_ids(
    counts: Mapping[str, int], carried: Iterable[Mapping[str, Any]], disputed: Iterable[str]
) -> list[str]:
    live = set(disputed)
    return [item["id"] for item in carried if item["id"] in live and counts.get(item["id"], 0) + 1 >= ESCALATION_ROUNDS]


def _dispute_limit_reason(latest: Mapping[str, Any]) -> str:
    settled = maintained_ids(latest)
    escalated = [item for item in latest.get("escalated") or () if item not in settled]
    if not escalated:
        return f"the patch already had {FORCED_ROUND_LIMIT} dispute re-reviews; change the patch"
    return (
        f"the patch already had {FORCED_ROUND_LIMIT} dispute re-reviews; {', '.join(escalated)} escalated to the "
        f"owner: post '{adjudication.GRAMMAR}' for each, or change the patch"
    )


def _forced_round(
    records: Iterable[Mapping[str, Any]],
    *,
    digest: str,
    latest: Mapping[str, Any],
    paths: Sequence[str],
    reason: str,
) -> Round:
    if forced_rounds(records, digest) >= FORCED_ROUND_LIMIT:
        return Round(SKIP, _dispute_limit_reason(latest))
    return Round(
        FOLLOW_UP,
        reason,
        latest["cycle"],
        latest["round"] + 1,
        latest,
        frozenset(paths),
        True,
    )


def _decided_again(latest: Mapping[str, Any], head_sha: str, manual: bool, *, allow_carry: bool = True) -> Round:
    label = LABELS[latest["decision"]]
    if not manual or latest["decision"] == REFUSED:
        return Round(SKIP, f"head {head_sha} already has a {label} decision")
    if latest.get("run_uuid"):
        return Round(
            REPOST,
            f"head {head_sha} already has a {label} decision; it is posted again unchanged",
            latest["cycle"],
            latest["round"],
            latest,
        )
    if not allow_carry:
        return Round(
            FIRST,
            f"the {label} decision on head {head_sha} has no run binding; promotion requires a fresh review",
            latest["cycle"] + 1,
            1,
            latest,
        )
    return Round(
        CARRY,
        f"the {label} decision on head {head_sha} has no run binding; it is posted again bound to this run",
        latest["cycle"],
        latest["round"],
        latest,
    )


def _legacy_clearance(record: Mapping[str, Any]) -> bool:
    return record["decision"] in (SHIP, SHIP_WITH_FIXES) and not record.get("coverage")


def finding_streak(records: Sequence[Mapping[str, Any]], cycle: int) -> list[Mapping[str, Any]]:
    streak: list[Mapping[str, Any]] = []
    for record in reversed(records):
        if record.get("cycle") != cycle or not record.get("open_defects"):
            break
        if record.get("kind") in (FIRST, FOLLOW_UP):
            streak.append(record)
    return streak


def plan_round(
    found: History,
    *,
    head_sha: str,
    base_ref: str,
    digest: str,
    patches: Mapping[str, str],
    paths: Sequence[str],
    strike_count: int,
    max_do_not_ship: int,
    force: bool = False,
    base_changed_at: datetime | None = None,
    base_change_unreadable: bool = False,
    manual: bool = False,
    is_promotion: bool = False,
    max_finding_rounds: int | None = None,
) -> Round:
    latest = found.latest
    if strike_count >= max_do_not_ship:
        if latest is not None and latest["kind"] == REFUSAL and latest["head_sha"] == head_sha:
            return Round(SKIP, f"this head already has the refusal after {strike_count} DO NOT SHIP decisions")
        cycle = latest["cycle"] if latest else 1
        return Round(REFUSAL, f"{strike_count} DO NOT SHIP decisions on this pull request", cycle, 0, latest)
    if latest is None:
        return Round(FIRST, "first review of this pull request")
    if latest["head_sha"] == head_sha:
        if force and latest["decision"] in (SHIP, SHIP_WITH_FIXES):
            return _forced_round(
                found.records,
                digest=digest,
                latest=latest,
                paths=paths,
                reason=(
                    f"forced re-review after {LABELS[latest['decision']]}: an author rebuttal disputes an open finding"
                ),
            )
        decided_at = found.latest_at
        stale = latest["decision"] != REFUSED and (
            base_change_unreadable
            or (base_changed_at is not None and (decided_at is None or decided_at <= base_changed_at))
        )
        promotion_bound = is_promotion or latest.get("promotion") is not None
        if not stale:
            return _decided_again(latest, head_sha, manual, allow_carry=not promotion_bound)
        if (
            latest["patch_digest"] == digest
            and not digest.startswith(UNREAD_DIGEST)
            and not promotion_bound
            and not _legacy_clearance(latest)
        ):
            return Round(
                CARRY,
                f"the base changed after the {LABELS[latest['decision']]} decision on head {head_sha}",
                latest["cycle"],
                latest["round"],
                latest,
            )
        if promotion_bound:
            return Round(
                FIRST,
                f"the base changed after the {LABELS[latest['decision']]} promotion review on head {head_sha}",
                latest["cycle"] + 1,
                1,
                latest,
            )
    restart = latest["cycle"] + 1
    if latest["base_ref"] != base_ref:
        return Round(FIRST, f"the base changed from {latest['base_ref']} to {base_ref}", restart, 1, latest)
    if latest["decision"] == REFUSED:
        return Round(FIRST, "the strike limit no longer applies", restart, 1, latest)
    promotion_bound = is_promotion or latest.get("promotion") is not None
    if latest["patch_digest"] == digest and not digest.startswith(UNREAD_DIGEST):
        if force and latest["decision"] in (SHIP, SHIP_WITH_FIXES):
            return _forced_round(
                found.records,
                digest=digest,
                latest=latest,
                paths=paths,
                reason=(
                    f"the patch is unchanged since head {latest['head_sha']},"
                    " but an author rebuttal disputes an open finding: forced re-review"
                ),
            )
        if not promotion_bound:
            if _legacy_clearance(latest):
                return Round(
                    FIRST,
                    f"the {LABELS[latest['decision']]} decision on head {latest['head_sha']} has no hunk coverage",
                    restart,
                    1,
                    latest,
                )
            return Round(
                CARRY,
                f"the patch is unchanged since head {latest['head_sha']}",
                latest["cycle"],
                latest["round"],
                latest,
            )
        if latest["decision"] == SHIP_WITH_FIXES and latest.get("open_defects"):
            return Round(
                FOLLOW_UP,
                f"promotion pull requests cannot carry across heads (since {latest['head_sha']})",
                latest["cycle"],
                latest["round"] + 1,
                latest,
                frozenset(paths),
            )
        return Round(
            FIRST,
            f"promotion pull requests cannot carry across heads (since {latest['head_sha']})",
            restart,
            1,
            latest,
        )
    if latest["decision"] == DO_NOT_SHIP:
        return Round(FIRST, "the patch changed after DO NOT SHIP", restart, 1, latest)
    changed = (
        (changed_paths(latest.get("patch_map"), patches, paths) or frozenset(paths))
        if latest.get("coverage")
        else frozenset(paths)
    )
    streak = finding_streak(found.records, latest["cycle"]) if max_finding_rounds is not None else []
    if (
        max_finding_rounds is not None
        and len(streak) >= max_finding_rounds
        and not any(record.get("step_back") for record in streak)
    ):
        return Round(
            FOLLOW_UP,
            f"the patch changed after {LABELS[latest['decision']]}, and this pull request has had {len(streak)} rounds"
            " with findings: one step-back re-review of the whole change",
            latest["cycle"],
            latest["round"] + 1,
            latest,
            frozenset(paths),
            step_back=True,
        )
    return Round(
        FOLLOW_UP,
        f"the patch changed after {LABELS[latest['decision']]}",
        latest["cycle"],
        latest["round"] + 1,
        latest,
        changed,
    )


def _clean_path(path: str) -> str:
    norm = unicodedata.normalize("NFC", path)
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def _resolve_tree(path: str, workspace: Path | str | None) -> str:
    cleaned = _clean_path(path)
    if not cleaned or workspace is None:
        return cleaned
    try:
        root = os.path.realpath(str(workspace))
        candidate = os.path.normpath(os.path.join(root, path))
        if candidate == root or candidate.startswith(root + os.sep):
            real = os.path.realpath(candidate)
            if real == root or real.startswith(root + os.sep):
                rel = os.path.relpath(real, root).replace("\\", "/")
                return _clean_path(rel)
    except (OSError, ValueError):
        return cleaned
    return cleaned


def _exists_exact(path: str, workspace: Path | str | None) -> bool:
    if workspace is None or not path:
        return False
    try:
        root = os.path.realpath(str(workspace))
        current = root
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part not in os.listdir(current):
                return False
            current = os.path.join(current, part)
        return current != root
    except OSError:
        return False


PRE_EXISTING = "pre-existing"
KNOWN_LIMIT = 5
TRIGGER_SCAN_FILES = 300
NON_SOURCE_EXTENSIONS = frozenset(
    "json jsonl yaml yml toml ini cfg conf csv tsv xml sql properties env lock txt parquet avro schema proto graphql".split()
)
_CONFIG_NAMES = frozenset(
    "makefile dockerfile pyproject.toml setup.cfg setup.py tox.ini noxfile.py conftest.py package.json".split()
)
_DEFINITION = re.compile(r"^[ \t]*(?:async[ \t]+)?(?:def|class|function)[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
TRIGGER_SCAN_BYTES = 262144
_SYMBOL = re.compile(r"`(?P<quoted>[A-Za-z_][\w.]*)`|\b(?P<called>[A-Za-z_]\w{2,})\(")
_LINE_LIST = (
    r"(?:[ \t]*(?:[:#]|[ \t]lines?[ \t]+|[ \t]*\([ \t]*L?)[ \t]*L?(?P<first>\d+)"
    r"(?:[ \t]*[-\u2013][ \t]*L?(?P<last>\d+))?(?P<more>(?:[ \t]*(?:,|and|&)[ \t]*L?\d+(?:[ \t]*[-\u2013][ \t]*L?\d+)?)*))?"
)
_TOKEN = re.compile(rf"(?<![\w./-])(?P<token>[\w.@+~-]+(?:/[\w.@+~-]+)*)(?![\w]){_LINE_LIST}")
_NUMBER_RANGE = re.compile(r"(\d+)(?:[ \t]*[-\u2013][ \t]*L?(\d+))?")
_BARE_LINE = re.compile(
    r"#L(?P<a>\d+)(?:-L?(?P<b>\d+))?|\bL(?P<c>\d+)(?:-L?(?P<d>\d+))?\b"
    r"|\blines?[ \t]+(?P<e>\d+)(?:[ \t]*[-\u2013][ \t]*(?P<f>\d+))?",
    re.IGNORECASE,
)


def _ranges(first: str | None, last: str | None, more: str | None) -> list[tuple[int, int]]:
    found = [(int(first), int(last or first))] if first else []
    found.extend((int(a), int(b or a)) for a, b in _NUMBER_RANGE.findall(more or ""))
    return found


def _plain(path: str) -> str:
    return _clean_path(path).casefold()


def _suffixes(path: str) -> list[str]:
    parts = _plain(path).split("/")
    return ["/".join(parts[index:]) for index in range(len(parts))]


def _unparsed_markers(finding: Finding) -> bool:
    text = f"{finding.title}\n{finding.detail}"
    return LINK_REMOVED in text or REDACTED in text


@dataclass(frozen=True)
class KnownScope:
    added: Mapping[str, Sequence[Sequence[int]]]
    new_files: frozenset[str]
    enabled: bool
    changed: frozenset[str] = frozenset()
    workspace: Path | str | None = None

    def _paths(self) -> list[str]:
        return list(dict.fromkeys((*self.new_files, *self.changed, *self.added)))

    def _real(self, path: str) -> str:
        return _plain(_resolve_tree(path, self.workspace))

    def _candidates(self, name: str) -> list[str]:
        folded, real = _plain(name), self._real(name)
        return [path for path in self._paths() if _plain(path) in (folded, real) or self._real(path) in (folded, real)]

    def _citations(self, finding: Finding) -> tuple[list[tuple[str, int | None, int | None]], int]:
        original = unicodedata.normalize("NFC", f"{finding.title}\n{finding.detail}").replace("\\", "/")
        original = re.sub(r"(?<![\w.])\./", "", original)
        text = original.casefold()
        own = _plain(finding.file)
        cited: list[tuple[str, int | None, int | None]] = []
        mentions = 0
        owners: dict[str, list[str]] = {}
        for path in self._paths():
            for name in dict.fromkeys(_suffixes(path)):
                owners.setdefault(name, []).append(path)
        owners.setdefault(own, [finding.file])
        for name in dict.fromkeys(_suffixes(finding.file)):
            owners.setdefault(name, [finding.file])
        for name in sorted(owners, key=len, reverse=True):
            if name not in text:
                continue
            pattern = re.compile(rf"(?<![\w./-]){re.escape(name)}(?!\w){_LINE_LIST}")
            for match in pattern.finditer(text):
                mentions += 1
                spans = _ranges(match["first"], match["last"], match["more"])
                text = text[: match.start()] + " " * (match.end() - match.start()) + text[match.end() :]
                for path in owners[name]:
                    if spans:
                        cited.extend((path, start, end) for start, end in spans)
                    else:
                        cited.append((path, None, None))
        for match in _TOKEN.finditer(original):
            token = match["token"]
            if ("." in token or "/" in token) and self._on_disk(token):
                mentions += 1
                spans = _ranges(match["first"], match["last"], match["more"])
                if spans:
                    cited.extend((token, start, end) for start, end in spans)
                else:
                    cited.append((token, None, None))
        for match in _BARE_LINE.finditer(text):
            mentions += 1
            numbers = [int(value) for value in match.groups() if value]
            cited.append((finding.file, min(numbers), max(numbers)))
        return cited, mentions

    def _on_disk(self, token: str) -> bool:
        if self.workspace is None:
            return False
        root = os.path.realpath(str(self.workspace))
        full = os.path.realpath(os.path.join(root, token))
        return full.startswith(root + os.sep) and os.path.isfile(full)

    def _definitions(self, path: str) -> list[str]:
        if self.workspace is None:
            return []
        root = os.path.realpath(str(self.workspace))
        full = os.path.realpath(os.path.join(root, path))
        try:
            if full.startswith(root + os.sep) and os.path.isfile(full) and os.path.getsize(full) <= TRIGGER_SCAN_BYTES:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    return [name for name in _DEFINITION.findall(handle.read()) if not name.startswith("__")][:50]
        except OSError:
            return []
        return []

    def _needles(self, finding: Finding, files: Sequence[str]) -> set[str]:
        needles: set[str] = set()
        for path in files:
            parts = _clean_path(path).rsplit(".", 1)[0].split("/")
            if parts[-1] == "__init__" and len(parts) > 1:
                parts = parts[:-1]
            needles.add(parts[-1])
            needles.add(".".join(parts))
        for path in files:
            needles.update(self._definitions(path))
        text = f"{finding.title}\n{finding.detail}"
        for match in _SYMBOL.finditer(text):
            needles.add(match["quoted"] or match["called"])
        return {item for item in needles if item}

    def _trigger_in_diff(self, finding: Finding, files: Sequence[str]) -> bool:
        if self.workspace is None:
            return True
        needles = self._needles(finding, files)
        if not needles:
            return True
        pattern = re.compile(
            r"(?<![\w])(?:" + "|".join(re.escape(item) for item in sorted(needles)) + r")(?![\w])", re.IGNORECASE
        )
        root = os.path.realpath(str(self.workspace))
        cited = {self._real(item) for item in files}
        scanned = [path for path in self._paths() if self._real(path) not in cited and _plain(path) not in cited]
        if len(scanned) > TRIGGER_SCAN_FILES:
            return True
        packages = {_clean_path(item).split("/", 1)[0] if "/" in _clean_path(item) else "" for item in files}
        names = {_clean_path(item).rsplit("/", 1)[-1].casefold() for item in files}
        for path in scanned:
            if self._non_source_trigger(path, packages):
                return True
            full = os.path.realpath(os.path.join(root, path))
            if not (full.startswith(root + os.sep) and os.path.isfile(full)):
                return True
            try:
                if os.path.getsize(full) > TRIGGER_SCAN_BYTES:
                    return True
                with open(full, encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                return True
            if pattern.search(content) or any(name in content.casefold() for name in names):
                return True
        return False

    @staticmethod
    def _non_source_trigger(path: str, packages: set[str]) -> bool:
        clean = _clean_path(path)
        extension = clean.rsplit(".", 1)[-1].casefold() if "." in clean.rsplit("/", 1)[-1] else ""
        top = clean.split("/", 1)[0] if "/" in clean else ""
        if clean.startswith(".github/") or clean.rsplit("/", 1)[-1].casefold() in _CONFIG_NAMES:
            return True
        if extension not in NON_SOURCE_EXTENSIONS:
            return False
        return top == "" or top in packages or "" in packages

    def unchanged(self, finding: Finding) -> bool:
        if finding.origin != PRE_EXISTING or _unparsed_markers(finding):
            return False
        cited, mentions = self._citations(finding)
        if mentions == 0:
            return False
        files = list(dict.fromkeys([finding.file, *(path for path, _, _ in cited)]))
        if any(self._candidates(path) for path in files):
            return False
        return not self._trigger_in_diff(finding, files)

    def aliased(self, targets: Sequence[str], workspace: Path | str | None) -> bool:
        if workspace is None:
            return False
        goals = {_clean_path(target) for target in targets}
        for path in (*self.new_files, *self.changed, *self.added):
            own = _clean_path(path)
            real = _resolve_tree(path, workspace)
            for goal in goals:
                if real in ("", ".") and own != goal:
                    return True
                if own != goal and (real == goal or goal.startswith(f"{real}/") or goal.startswith(f"{own}/")):
                    return True
        return False

    def demotes(self, finding: Finding) -> bool:
        return self.enabled and finding.severity != "Critical" and self.unchanged(finding)


def known_scope(plan: Mapping[str, Any], workspace: Path | str | None = None) -> KnownScope | None:
    added = plan.get("added_lines")
    if not isinstance(added, Mapping):
        return None
    rules = plan.get("protocol") or {}
    return KnownScope(
        added,
        frozenset(plan.get("new_files") or ()),
        rules.get("pre_existing") == "known" and not plan.get("promotion"),
        frozenset(plan.get("changed_paths") or ()),
        workspace,
    )


def _is_file(path: str, workspace: Path | str) -> bool:
    return _exists_exact(path, workspace) and os.path.isfile(os.path.join(os.path.realpath(str(workspace)), path))


def judge(
    verdict: Verdict,
    max_defects: int,
    prior: Sequence[Mapping[str, Any]] = (),
    changed: Iterable[str] | None = None,
    rejudge_fixed: bool = False,
    workspace: Path | str | None = None,
    read: Returned | None = None,
    claimed: frozenset[str] = frozenset(),
    known: KnownScope | None = None,
) -> Judgement:
    if verdict.status != COMPLETE:
        raise ValueError("only a complete review can be judged")
    statuses = {item.id: item.status for item in verdict.prior_defects}
    cited = {item.id: item.evidence for item in verdict.prior_defects}
    if rejudge_fixed:
        statuses = {
            key: (
                NOT_FIXED
                if value == FIXED or (value == WITHDRAWN and not withdrawal_cited(cited.get(key), read))
                else value
            )
            for key, value in statuses.items()
        }
    carried = tuple(item for item in prior if statuses.get(item["id"], NOT_FIXED) == NOT_FIXED)
    fixed = tuple(item["id"] for item in prior if statuses.get(item["id"], NOT_FIXED) == FIXED)
    withdrawn = tuple(item["id"] for item in prior if statuses.get(item["id"]) == WITHDRAWN)
    if changed is None:
        in_scope = verdict.defects
        outside = ()
    else:
        changed_list = list(changed)
        scope = frozenset(changed_list)
        changed_map = []
        for path in changed_list:
            canonical = _resolve_tree(path, workspace)
            changed_map.append((path, canonical, canonical.casefold()))
        in_scope_list: list[Finding] = []
        outside_list: list[Finding] = []
        for item in verdict.defects:
            if item.file in scope:
                in_scope_list.append(item)
                continue
            f_canon = _resolve_tree(item.file, workspace)
            exact_matches = [c for c, c_canon, _ in changed_map if c_canon == f_canon]
            if exact_matches:
                chosen = f_canon if f_canon in exact_matches else exact_matches[0]
                in_scope_list.append(replace(item, file=chosen))
                continue
            if _exists_exact(f_canon, workspace):
                outside_list.append(item)
                continue
            f_fold = f_canon.casefold()
            case_matches = [c for c, _, c_fold in changed_map if c_fold == f_fold]
            if len(case_matches) == 1:
                in_scope_list.append(replace(item, file=case_matches[0]))
            elif len(case_matches) > 1:
                in_scope_list.append(item)
            else:
                outside_list.append(item)
        in_scope = tuple(in_scope_list)
        outside = tuple(outside_list)[:NOT_COUNTED_LIMIT]
    counted = tuple(dict.fromkeys(in_scope))
    merged = len(in_scope) - len(counted)
    uncounted = verdict.listed - len(verdict.defects)
    eligible = (
        tuple(
            item
            for item in counted
            if known.demotes(item)
            and known.demotes(replace(item, file=_resolve_tree(item.file, workspace)))
            and not known.aliased((item.file, _resolve_tree(item.file, workspace)), workspace)
            and workspace is not None
            and _is_file(_resolve_tree(item.file, workspace), workspace)
        )
        if known is not None and not uncounted
        else ()
    )
    advisory = eligible if len(eligible) <= KNOWN_LIMIT else ()
    counted = tuple(item for item in counted if item not in advisory)
    confirmed_claims = sum(1 for item in carried if item["id"] in claimed)
    total = (
        sum(1 for item in carried if item["id"] not in claimed)
        + min(confirmed_claims, max(max_defects - 1, 0))
        + len(counted)
        + uncounted
    )
    if verdict.decision == DO_NOT_SHIP or total > max_defects:
        note = ""
        if total > max_defects:
            note = f" The review left {total} defects open, more than the {max_defects} allowed."
        summary = _clip(verdict.summary, DO_NOT_SHIP_SUMMARY - len(note)) + note
        return Judgement(DO_NOT_SHIP, (), summary, fixed=fixed, withdrawn=withdrawn)
    if carried or counted:
        return Judgement(
            SHIP_WITH_FIXES, counted, verdict.summary, carried, fixed, outside, withdrawn, merged, advisory
        )
    return Judgement(SHIP, (), verdict.summary, (), fixed, outside, withdrawn, advisory=advisory)


def judge_planned(
    verdict: Verdict,
    plan: Mapping[str, Any],
    workspace: Path | str | None = None,
    read: Returned | None = None,
) -> Judgement:
    rules = plan.get("protocol") or {}
    changed = rules.get("changed") if rules.get("kind") == FOLLOW_UP else None
    ws = workspace if workspace is not None else plan.get("workspace")
    return judge(
        verdict,
        int(plan["max_defects"]),
        rules.get("prior", ()),
        changed,
        bool(rules.get("forced")),
        workspace=ws,
        read=read,
        claimed=frozenset(rules.get("claimed") or ()),
        known=known_scope(plan, ws),
    )


THREAD_MARKER = re.compile(
    rf"<!--\s*(?:review-defect|{re.escape(legacy.DEFECT_MARKER_NAME)}):\s*c([0-9]+)-(D[1-9][0-9]{{0,3}})\s*-->"
)
MARKER_LINE = re.compile(
    rf"^\s*(?:<!--\s*(?:review-|{re.escape(legacy.MARKER_PREFIX)})[\s\S]*?-->\s*"
    rf"|(?:review-defect|{re.escape(legacy.DEFECT_MARKER_NAME)})\s*:.*)$"
)
REPLY_LIMIT = 1000
DISPUTE_MARKER = "Dispute:"


def is_dispute(body: str) -> bool:
    return any(line.strip().startswith(DISPUTE_MARKER) for line in body.splitlines())


COMMENT_DISPUTE_GRAMMAR = f"{DISPUTE_MARKER} D<n> - <reason>"
_COMMENT_DISPUTE = re.compile(r"(?P<id>D[1-9][0-9]{0,3}) - (?P<reason>\S.*)", re.DOTALL)


def is_comment_dispute_trigger(body: str | None) -> bool:
    return (body or "").lstrip().casefold().startswith(DISPUTE_MARKER.casefold())


def parse_comment_dispute(body: str | None) -> tuple[str, str] | None:
    text = (body or "").strip()
    if not is_comment_dispute_trigger(text) or not text[len(DISPUTE_MARKER) :].startswith(" "):
        return None
    match = _COMMENT_DISPUTE.fullmatch(text[len(DISPUTE_MARKER) + 1 :])
    return (match["id"], match["reason"]) if match else None


def thread_finding(body: str) -> tuple[int, str] | None:
    matches = THREAD_MARKER.findall(body or "")
    if not matches:
        return None
    cycle, defect_id = matches[-1]
    return int(cycle), defect_id


def thread_finding_id(body: str) -> str | None:
    found = thread_finding(body)
    return found[1] if found is not None else None


def strip_marker_lines(text: str) -> str:
    return "\n".join(line for line in (text or "").splitlines() if not MARKER_LINE.match(line))


def normalize_claim(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def canonical_claim(text: str) -> str:
    return sanitize(text or "", MAX_CLAIM)


def claim_hash(text: str) -> str:
    return hashlib.sha256(normalize_claim(text or "").encode("utf-8")).hexdigest()


def dispute_claim(text: str) -> str:
    return strip_marker_lines(text)[:REPLY_LIMIT]


def disputed_claims(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    rules = plan.get("protocol") or {}
    prior_ids = {item.get("id") for item in rules.get("prior") or () if isinstance(item, Mapping) and item.get("id")}
    if not prior_ids:
        return {}
    cycle = rules.get("cycle")
    claims: dict[str, list[str]] = {}
    for item in plan.get("disputes") or ():
        if not isinstance(item, Mapping) or not item.get("id") or not item.get("claim_hashes"):
            continue
        if item["id"] not in prior_ids:
            continue
        if cycle is not None and item.get("cycle") is not None and item["cycle"] != cycle:
            continue
        claims.setdefault(item["id"], []).extend(
            [
                known
                for known in item["claim_hashes"]
                if isinstance(known, str) and known not in claims.get(item["id"], [])
            ]
        )
    return claims


def maintained_ids(latest: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        item["id"]
        for item in latest.get("adjudicated") or ()
        if isinstance(item, Mapping) and item.get("action") == adjudication.MAINTAIN
    )


def live_disputes(disputes: Any, latest: Any) -> list[dict[str, Any]]:
    if not isinstance(latest, Mapping):
        return []
    settled = maintained_ids(latest)
    open_ids = {
        item.get("id")
        for item in latest.get("open_defects") or ()
        if isinstance(item, Mapping) and item.get("id") and item.get("id") not in settled
    }
    cycle = latest.get("cycle")
    return [
        item
        for item in disputes or ()
        if isinstance(item, Mapping)
        and item.get("id") in open_ids
        and (cycle is None or item.get("cycle") is None or item.get("cycle") == cycle)
    ]


def bound_disputes(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    allowed = disputed_claims(plan)
    cycle = (plan.get("protocol") or {}).get("cycle")
    return [
        item
        for item in plan.get("disputes") or ()
        if isinstance(item, Mapping)
        and item.get("id") in allowed
        and (cycle is None or item.get("cycle") is None or item.get("cycle") == cycle)
    ]


def dispute_problems(verdict: Verdict, plan: Mapping[str, Any]) -> dict[str, str]:
    claims = disputed_claims(plan)
    if verdict.status != COMPLETE or not claims:
        return {}
    entries = {item.id: item for item in verdict.prior_defects}
    problems: dict[str, str] = {}
    for defect_id in sorted(claims):
        entry = entries.get(defect_id)
        if entry is None:
            problems[defect_id] = f"disputed finding {defect_id} has no disposition, so the verdict is invalid"
            continue
        demoted = entry.demoted in DEMOTED_STATUSES
        if not isinstance(entry.concedes, bool) and not demoted:
            problems[defect_id] = f"disputed finding {defect_id} has no concedes boolean, so the verdict is invalid"
            continue
        if entry.status == NOT_FIXED and entry.concedes and not demoted:
            problems[defect_id] = (
                f"disputed finding {defect_id} concedes the rebuttal yet stays not fixed, so the verdict is invalid"
            )
        elif entry.status == WITHDRAWN and not entry.concedes:
            problems[defect_id] = (
                f"disputed finding {defect_id} is withdrawn without conceding the rebuttal, so the verdict is invalid"
            )
        elif (
            entry.status in (NOT_FIXED, WITHDRAWN)
            and not demoted
            and (not entry.claim.strip() or all(claim_hash(entry.claim) != known for known in claims[defect_id]))
        ):
            problems[defect_id] = (
                f"disputed finding {defect_id} does not restate one of its rebuttal replies, so the verdict is invalid"
            )
        elif entry.status == FIXED and not (entry.evidence or "").strip():
            problems[defect_id] = (
                f"disputed finding {defect_id} is marked fixed without evidence, so the verdict is invalid"
            )
    return problems


def forced_fixed_problems(verdict: Verdict, plan: Mapping[str, Any]) -> dict[str, str]:
    if verdict.status != COMPLETE or not (plan.get("protocol") or {}).get("forced"):
        return {}
    claims = disputed_claims(plan)
    return {
        item.id: (
            f"disputed finding {item.id} is marked fixed on a dispute re-review of an unchanged patch, which cannot"
            " fix anything; withdraw it with concedes true or keep it not_fixed, so the verdict is invalid"
        )
        for item in verdict.prior_defects
        if item.id in claims and item.status == FIXED and item.demoted not in DEMOTED_STATUSES
    }


def dispute_problem(verdict: Verdict, plan: Mapping[str, Any]) -> str | None:
    problems = dispute_problems(verdict, plan)
    return problems[min(problems)] if problems else None


def keep_disputes_open(verdict: Verdict, plan: Mapping[str, Any]) -> Verdict:
    problems = dispute_problems(verdict, plan)
    if not problems:
        return verdict
    entries = {item.id: item for item in verdict.prior_defects}
    for defect_id, problem in problems.items():
        note = problem.removesuffix(", so the verdict is invalid") + "; the defect stays open"
        entry = entries.get(defect_id)
        if entry is None:
            entries[defect_id] = PriorDefect(defect_id, NOT_FIXED, note, demoted=DEMOTED_DISPOSITION)
        else:
            entries[defect_id] = replace(
                entry, status=NOT_FIXED, evidence=f"{entry.evidence} {note}".strip(), demoted=DEMOTED_DISPOSITION
            )
    return replace(verdict, prior_defects=tuple(entries.values()))


def contradiction(
    verdict: Verdict,
    plan: Mapping[str, Any],
    workspace: Path | str | None = None,
) -> str | None:
    forced_fixed = forced_fixed_problems(verdict, plan)
    if forced_fixed:
        return forced_fixed[min(forced_fixed)]
    if verdict.status != COMPLETE or judge_planned(verdict, plan, workspace=workspace).decision != SHIP:
        return None
    problem = dispute_problem(verdict, plan)
    if problem is not None:
        return problem
    if verdict.incomplete_reason.strip():
        return "the verdict would ship but reports that the review could not be finished"
    priors = (plan.get("protocol") or {}).get("prior") or ()
    known = {item.get("id") for item in priors if isinstance(item, Mapping)}
    unknown = {item.id for item in verdict.prior_defects} - known
    if unknown:
        return f"the verdict would ship but cites {len(unknown)} prior defect ID(s) the plan does not list"
    unsupported = sorted(
        item.id for item in verdict.prior_defects if item.status in (FIXED, WITHDRAWN) and not item.evidence.strip()
    )
    if unsupported:
        return f"the verdict would ship but clears prior defect(s) {', '.join(unsupported)} without evidence"
    if verdict.decision == SHIP_WITH_FIXES and not verdict.defects and verdict.listed == 0:
        return "SHIP_WITH_FIXES names no current or open prior defects"
    return None


def unique_defects(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for entry in entries:
        seen.setdefault(entry["id"], dict(entry))
    return list(seen.values())


def defect_entry(defect_id: str, finding: Finding) -> dict[str, Any]:
    return {
        "id": defect_id,
        "severity": finding.severity,
        "file": finding.file,
        "line": finding.line,
        "end_line": finding.end_line,
        "title": finding.title,
    }
