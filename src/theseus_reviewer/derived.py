from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .diff import binary_notice_count, drop_files, hunk_index
from .gitmodes import ABSENT_MODE, REGULAR_MODE
from .globs import glob_match
from .limits import MAX_PATH

if TYPE_CHECKING:
    from .classifier import ChangedFile
    from .protocol import FileState

ENTRY_KEYS = frozenset({"name", "files", "summary", "attested_by"})
CODE_SUFFIXES = (".py", ".pyi", ".sh", ".js", ".ts", ".toml", ".cfg", ".ini", ".yml", ".yaml")
DERIVED_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DERIVED_PATHS_MAX = 200
SUMMARY_SUFFIX = ".md"
ATTESTED_BY_MAX = 200
DATA_STATUS = "modified"
SUMMARY_STATUSES = ("modified", "added")
_CLASS = re.compile(r"\[[!^]?\]?[^\]]*\]")


class DerivedError(ValueError):
    pass


@dataclass(frozen=True)
class DerivedEntry:
    name: str
    files: tuple[str, ...]
    summary: str
    attested_by: str

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "name": self.name,
                "files": sorted(self.files),
                "summary": self.summary,
                "attested_by": self.attested_by,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_data(self, path: str) -> bool:
        return any(glob_match(path, pattern) for pattern in self.files)


@dataclass(frozen=True)
class Eligible:
    entry: DerivedEntry
    paths: tuple[str, ...]
    diff: str

    def to_plan(self) -> dict[str, Any]:
        return {
            "entry": self.entry.name,
            "digest": self.entry.digest(),
            "paths": list(self.paths),
            "summary": self.entry.summary,
            "attested_by": self.entry.attested_by,
        }


def _instance(pattern: str) -> str:
    text = _CLASS.sub(lambda match: _first_of(match.group(0)), pattern)
    return text.replace("*", "x").replace("?", "x")


def _first_of(group: str) -> str:
    body = group[1:-1].lstrip("!^")
    return body[:1] if body[:1] not in ("", "-") else "x"


def _bad_path(path: str) -> bool:
    return (
        not path
        or path.startswith("/")
        or path.endswith("/")
        or any(segment in ("", ".", "..") for segment in path.split("/"))
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    )


def _code_like(segment: str) -> bool:
    base = _instance(segment)
    names = {"conftest", "conftest.py"}
    for suffix in CODE_SUFFIXES:
        names.update((f"x{suffix}", f"{base}{suffix}"))
    return any(segment.endswith(suffix) for suffix in CODE_SUFFIXES) or any(
        fnmatch.fnmatchcase(name, segment) for name in names
    )


def _check_pattern(pattern: str, label: str, gate_paths: Sequence[str]) -> None:
    if not isinstance(pattern, str) or _bad_path(pattern):
        raise DerivedError(f"{label} must be a relative path pattern")
    segments = pattern.split("/")
    if "**" in segments or any("**" in segment for segment in segments):
        raise DerivedError(f"{label} {pattern!r} must not use **")
    if "[!" in pattern or "[^" in pattern:
        raise DerivedError(f"{label} {pattern!r} must not use a negated character class")
    if fnmatch.fnmatchcase(".github", segments[0]):
        raise DerivedError(f"{label} {pattern!r} may match a .github path")
    if _code_like(segments[-1]):
        raise DerivedError(f"{label} {pattern!r} may match a code file")
    instance = _instance(pattern)
    for gate in gate_paths:
        if fnmatch.fnmatchcase(instance, gate) or glob_match(gate, pattern):
            raise DerivedError(f"{label} {pattern!r} overlaps the gate path {gate!r}")


def parse_entries(raw: Any, gate_paths: Sequence[str]) -> tuple[DerivedEntry, ...]:
    if not isinstance(raw, list):
        raise DerivedError("evidence derived_paths must be a list")
    entries: list[DerivedEntry] = []
    for index, item in enumerate(raw):
        label = f"evidence derived_paths[{index}]"
        if not isinstance(item, Mapping):
            raise DerivedError(f"{label} must be a mapping")
        unknown = sorted(str(key) for key in set(item) - ENTRY_KEYS)
        if unknown:
            raise DerivedError(f"{label} has unknown key {unknown[0]!r}; allowed keys are {sorted(ENTRY_KEYS)}")
        missing = sorted(ENTRY_KEYS - set(item))
        if missing:
            raise DerivedError(f"{label} is missing {missing[0]!r}")
        name, files, summary, attested = item["name"], item["files"], item["summary"], item["attested_by"]
        if not isinstance(name, str) or DERIVED_NAME.fullmatch(name) is None:
            raise DerivedError(f"{label} name must match [a-z0-9]+(-[a-z0-9]+)*")
        label = f"evidence derived_paths entry {name!r}"
        if not isinstance(files, list) or not files or not all(isinstance(path, str) for path in files):
            raise DerivedError(f"{label} files must be a non-empty string list")
        if len(set(files)) != len(files):
            raise DerivedError(f"{label} files must not repeat a pattern")
        for pattern in files:
            _check_pattern(pattern, f"{label} file", gate_paths)
        if not isinstance(summary, str) or not summary.endswith(SUMMARY_SUFFIX) or _bad_path(summary):
            raise DerivedError(f"{label} summary must be an exact relative .md path")
        if any(char in summary for char in "*?["):
            raise DerivedError(f"{label} summary must be an exact path, not a pattern")
        _check_pattern(summary, f"{label} summary", gate_paths)
        entry = DerivedEntry(name, tuple(files), summary, "")
        if entry.is_data(summary):
            raise DerivedError(f"{label} summary must not be matched by files")
        if (
            not isinstance(attested, str)
            or not attested.strip()
            or attested != attested.strip()
            or len(attested) > ATTESTED_BY_MAX
            or any(ord(char) < 32 or ord(char) == 127 for char in attested)
        ):
            raise DerivedError(
                f"{label} attested_by must be one line of text without surrounding space, up to {ATTESTED_BY_MAX} characters"
            )
        entries.append(DerivedEntry(name, tuple(files), summary, attested))
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        raise DerivedError("evidence derived_paths names must be unique")
    return tuple(entries)


def _state_ok(state: FileState | None, summary: bool, added: bool) -> bool:
    if state is None:
        return False
    old_ok = state.old_mode == (ABSENT_MODE if added else REGULAR_MODE)
    return old_ok and state.new_mode == REGULAR_MODE and (summary or not added)


def _code_path(path: str) -> bool:
    return path.endswith(CODE_SUFFIXES) or path.rsplit("/", 1)[-1].startswith("conftest") or path.startswith(".github/")


def _fits(entry: DerivedEntry, files: Sequence[ChangedFile], states: Mapping[str, FileState]) -> bool:
    summary_changed = False
    data = 0
    for item in files:
        if item.previous_path:
            return False
        is_summary = item.path == entry.summary
        if not is_summary and not entry.is_data(item.path):
            return False
        if is_summary:
            if item.status not in SUMMARY_STATUSES:
                return False
            summary_changed = True
        else:
            if item.status != DATA_STATUS or _code_path(item.path):
                return False
            data += 1
        if not _state_ok(states.get(item.path), is_summary, item.status == "added"):
            return False
    return summary_changed and data > 0


def assess(
    entries: Iterable[DerivedEntry],
    files: Sequence[ChangedFile],
    states: Sequence[FileState] | None,
    full_diff: str | None,
    is_held: Callable[[str], bool],
) -> Eligible | None:
    if states is None or full_diff is None or not files:
        return None
    if binary_notice_count(full_diff) or any(path for item in files for path in item.paths if is_held(path)):
        return None
    by_path = {state.new_path: state for state in states}
    if len(by_path) != len(states) or {item.path for item in files} != set(by_path):
        return None
    for entry in entries:
        if not _fits(entry, files, by_path):
            continue
        paths = tuple(sorted(item.path for item in files if item.path != entry.summary))
        if len(paths) > DERIVED_PATHS_MAX or any(len(path) > MAX_PATH for path in paths):
            return None
        stripped = drop_files(full_diff, frozenset(paths))
        if set(hunk_index(stripped)) & set(paths):
            return None
        return Eligible(entry, paths, stripped)
    return None


def record_of(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    held = plan.get("derived")
    if not isinstance(held, Mapping):
        return None
    return {"entry": held["entry"], "digest": held["digest"], "paths": list(held["paths"])}


def recorded_plan(entries: Iterable[DerivedEntry], recorded: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not recorded:
        return None
    for entry in entries:
        if entry.digest() == recorded["digest"]:
            return {
                "entry": entry.name,
                "digest": recorded["digest"],
                "paths": list(recorded["paths"]),
                "summary": entry.summary,
                "attested_by": entry.attested_by,
            }
    return None
