from __future__ import annotations

import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diff import STAGED_DIFF_NAME, Hunk, binary_files, hunk_index
from .promotion import STAGED_PROMOTION_DIR, UPSTREAM_DIFF_NAME

Ranges = tuple[tuple[int, int], ...]
Returned = Mapping[str, Ranges]

VERSION = 1
UPSTREAM_DIFF = f"{STAGED_PROMOTION_DIR}/{UPSTREAM_DIFF_NAME}"
_NEW_PREFIX = f"{STAGED_PROMOTION_DIR}/new/"
_OLD_PREFIX = f"{STAGED_PROMOTION_DIR}/old/"
_SHOWN_SPANS = 4
_SHOWN_FILES = 4
_REASON_LIMIT = 600
_BINARY_PROBE = 8000
_HINT_SPANS = 6
_MAX_COLUMNS = 768


@dataclass(frozen=True)
class Target:
    path: str
    diff: str | None
    hunks: tuple[Hunk, ...]
    listed: bool
    removed: bool = False
    opaque: bool = False


@dataclass(frozen=True)
class Uncovered:
    path: str
    source: str
    start: int
    end: int


@dataclass(frozen=True)
class Report:
    total: int
    covered: int
    uncovered: tuple[Uncovered, ...] = ()
    untouched: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*(item.path for item in self.uncovered), *self.untouched]))

    @property
    def complete(self) -> bool:
        return not self.uncovered and not self.untouched

    @property
    def unseen(self) -> tuple[tuple[str, str, int, int], ...]:
        return tuple((item.path, item.source, item.start, item.end) for item in self.uncovered)


def canonical(path: str, workspace: Path) -> str:
    root = os.path.realpath(workspace)
    target = os.path.realpath(os.path.join(root, path))
    return target[len(root) + 1 :] if target.startswith(root + os.sep) else path


def merge(numbers: Iterable[int]) -> Ranges:
    merged: list[list[int]] = []
    for number in sorted(set(numbers)):
        if merged and number == merged[-1][1] + 1:
            merged[-1][1] = number
        else:
            merged.append([number, number])
    return tuple((start, end) for start, end in merged)


def join(*groups: Ranges) -> Ranges:
    spans = sorted(span for group in groups for span in group)
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def contains(ranges: Ranges, start: int, end: int) -> bool:
    return any(low <= start and end <= high for low, high in ranges)


def holds_line(returned: Returned | None, path: str, line: int) -> bool:
    if returned is None:
        return True
    return contains(returned.get(path, ()), line, line)


def _text(workspace: Path, name: str) -> str | None:
    target = workspace / name
    if not target.is_file() or target.is_symlink():
        return None
    try:
        return target.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def _line_count(workspace: Path, path: str) -> int | None:
    target = Path(os.path.realpath(workspace / path))
    try:
        data = target.read_bytes()
    except OSError:
        return None
    return data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


def build_targets(
    evidence_files: Iterable[str],
    request_text: str,
    request_diff: str | None,
    upstream_text: str,
    whole: Collection[str] = (),
) -> dict[str, Target]:
    request_index = hunk_index(request_text)
    request_opaque = binary_files(request_text)
    upstream_index = hunk_index(upstream_text)
    upstream_opaque = binary_files(upstream_text)
    found: dict[str, Target] = {}
    for path in evidence_files:
        if path in whole and path.startswith((_NEW_PREFIX, _OLD_PREFIX)):
            found[path] = Target(path, UPSTREAM_DIFF, (), True, path.startswith(_OLD_PREFIX), True)
            continue
        if path.startswith(_NEW_PREFIX):
            key, index, source, removed = path[len(_NEW_PREFIX) :], upstream_index, UPSTREAM_DIFF, False
            opaque = key in upstream_opaque
        elif path.startswith(_OLD_PREFIX):
            key, index, source, removed = path[len(_OLD_PREFIX) :], upstream_index, UPSTREAM_DIFF, True
            opaque = False
        else:
            key, index, source, removed = path, request_index, request_diff, False
            opaque = key in request_opaque
        found[path] = Target(path, source, tuple(index.get(key, ())), key in index, removed, opaque)
    return found


def targets(plan: Mapping[str, Any], workspace: Path) -> dict[str, Target]:
    inline = plan.get("diff_inline")
    if inline:
        request_text, request_diff = inline, None
    else:
        request_text, request_diff = _text(workspace, STAGED_DIFF_NAME) or "", STAGED_DIFF_NAME
    return build_targets(
        plan.get("evidence_files", ()),
        request_text,
        request_diff,
        _text(workspace, UPSTREAM_DIFF) or "",
        plan.get("evidence_whole_files", ()),
    )


def cut_lines(text: str) -> tuple[int, ...]:
    return tuple(
        number for number, line in enumerate(text.split("\n"), 1) if len(line.encode("utf-16-le")) > _MAX_COLUMNS * 2
    )


def _whole_hint(path: str, whole: Collection[str], staged: Path | None) -> str:
    if staged is not None and path in whole and path.startswith((_NEW_PREFIX, _OLD_PREFIX)):
        text = _text(staged, path[len(STAGED_PROMOTION_DIR) + 1 :])
        if text is not None and cut_lines(text):
            count = _line_count(staged, path[len(STAGED_PROMOTION_DIR) + 1 :]) or 1
            return f"(read the whole file; lines past {_MAX_COLUMNS} columns are cut, so read with :raw as {path}:raw:1-{count})"
    return "(read the whole file)"


def block_hints(
    evidence_files: Iterable[str],
    request_text: str,
    upstream_text: str,
    paths: Iterable[str],
    whole: Collection[str] = (),
    staged: Path | None = None,
) -> dict[str, str]:
    found = build_targets(evidence_files, request_text, STAGED_DIFF_NAME, upstream_text, whole)
    cut = {STAGED_DIFF_NAME: cut_lines(request_text), UPSTREAM_DIFF: cut_lines(upstream_text)}
    hints: dict[str, str] = {}
    for path in paths:
        target = found.get(path)
        if target is None or not target.listed:
            continue
        if target.opaque:
            hints[path] = _whole_hint(path, whole, staged)
        elif target.hunks and target.diff is not None:
            marks = cut[target.diff]
            plain: list[str] = []
            raw: list[str] = []
            for hunk in target.hunks[:_HINT_SPANS]:
                span = _span_text(hunk.block_start, hunk.block_end)
                if marks and marks[0] <= hunk.block_end and marks[-1] >= hunk.block_start:
                    raw.append(f"{target.diff}:raw:{hunk.block_start}-{hunk.block_end}")
                else:
                    plain.append(span)
            parts = []
            if plain:
                parts.append(f"{target.diff} lines {', '.join(plain)}")
            if raw:
                parts.append(f"read with :raw as {', '.join(raw)}")
            more = f" and {len(target.hunks) - _HINT_SPANS} more" if len(target.hunks) > _HINT_SPANS else ""
            hints[path] = f"({'; '.join(parts)}{more})"
    return hints


def unseen_hints(unseen: Iterable[Sequence[Any]], texts: Mapping[str, str]) -> dict[str, str]:
    """Name the hunks an earlier attempt left unseen, per file, so a retry reads exactly those."""
    cut = {name: cut_lines(text) for name, text in texts.items()}
    by_path: dict[str, list[tuple[str, int, int]]] = {}
    for path, source, start, end in unseen:
        by_path.setdefault(path, []).append((source, start, end))
    hints: dict[str, str] = {}
    for path, spans in by_path.items():
        parts: list[str] = []
        plain: dict[str, list[str]] = {}
        raw: list[str] = []
        for source, start, end in spans[:_HINT_SPANS]:
            if source not in cut:
                parts.append(
                    "read the whole file" if source == "the file itself" else f"{source} lines {_span_text(start, end)}"
                )
                continue
            marks = cut[source]
            if marks and marks[0] <= end and marks[-1] >= start:
                raw.append(f"{source}:raw:{start}-{end}")
            else:
                plain.setdefault(source, []).append(_span_text(start, end))
        parts += [f"{source} lines {', '.join(numbers)}" for source, numbers in plain.items()]
        if raw:
            parts.append(f"read with :raw as {', '.join(raw)}")
        more = f" and {len(spans) - _HINT_SPANS} more" if len(spans) > _HINT_SPANS else ""
        hints[path] = f"(unseen: {'; '.join(parts)}{more})"
    return hints


def retry_hint_bounds(
    evidence_files: Iterable[str], request_text: str, upstream_text: str, whole: Collection[str] = ()
) -> dict[str, int]:
    targets = build_targets(evidence_files, request_text, STAGED_DIFF_NAME, upstream_text, whole)
    bounds = {}
    for path, target in targets.items():
        if not target.hunks or target.diff is None:
            continue
        widths = sorted(
            (len(f"{target.diff}:raw:{hunk.block_start}-{hunk.block_end}".encode()) for hunk in target.hunks),
            reverse=True,
        )[:_HINT_SPANS]
        bounds[path] = (
            len("(unseen: )")
            + sum(width + len("read with :raw as ; ") for width in widths)
            + len(f" and {len(target.hunks)} more")
        )
    return bounds


def _span_text(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def _has_nul_head(workspace: Path, path: str) -> bool:
    try:
        with open(os.path.realpath(workspace / path), "rb") as handle:
            return b"\0" in handle.read(_BINARY_PROBE)
    except OSError:
        return False


def evaluate(
    found: Mapping[str, Target],
    needed: Iterable[str],
    returned: Returned,
    workspace: Path,
    touched: Collection[str] = (),
) -> Report:
    total = covered = 0
    uncovered: list[Uncovered] = []
    untouched: list[str] = []
    seen: dict[str, Ranges] = {}
    for name, ranges in returned.items():
        key = canonical(name, workspace)
        seen[key] = join(seen.get(key, ()), ranges)
    for path in needed:
        target = found.get(path)
        key = canonical(path, workspace)
        own = seen.get(key, ())
        if target is None or not target.listed:
            if not own and key not in touched:
                untouched.append(path)
            continue
        if target.opaque:
            count = _line_count(workspace, path)
            if not count or _has_nul_head(workspace, path):
                if not own and key not in touched:
                    untouched.append(path)
                continue
            total += 1
            if contains(own, 1, count):
                covered += 1
            else:
                uncovered.append(Uncovered(path, "the file itself", 1, count))
            continue
        whole = False
        if target.removed:
            count = _line_count(workspace, path)
            whole = bool(count) and contains(own, 1, count)
        source = seen.get(canonical(target.diff, workspace), ()) if target.diff else ()
        for hunk in target.hunks:
            total += 1
            if (
                target.diff is None
                or whole
                or contains(source, hunk.block_start, hunk.block_end)
                or (hunk.additive and contains(own, hunk.new_start, hunk.new_end))
            ):
                covered += 1
                continue
            uncovered.append(Uncovered(path, target.diff or "", hunk.block_start, hunk.block_end))
    return Report(total, covered, tuple(uncovered), tuple(untouched))


def reason(report: Report) -> str:
    parts = []
    by_file: dict[str, list[Uncovered]] = {}
    for item in report.uncovered:
        by_file.setdefault(item.path, []).append(item)
    for path, items in list(by_file.items())[:_SHOWN_FILES]:
        spans = ", ".join(f"{item.source} lines {_span_text(item.start, item.end)}" for item in items[:_SHOWN_SPANS])
        more = f" and {len(items) - _SHOWN_SPANS} more" if len(items) > _SHOWN_SPANS else ""
        parts.append(f"{path} ({spans}{more})")
    clauses = []
    if by_file:
        hidden = len(by_file) - _SHOWN_FILES
        clause = (
            f"{report.total - report.covered} of {report.total} changed hunks in {len(by_file)} required files"
            f" were not seen: {'; '.join(parts)}"
        )
        clauses.append(clause + (f"; and {hidden} more files" if hidden > 0 else ""))
    if report.untouched:
        shown = ", ".join(report.untouched[:_SHOWN_FILES])
        more = f" and {len(report.untouched) - _SHOWN_FILES} more" if len(report.untouched) > _SHOWN_FILES else ""
        clauses.append(f"its trace shows no read of {shown}{more}")
    return "; ".join(clauses)[:_REASON_LIMIT]
