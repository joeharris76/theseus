from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_FULL_HUNK = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
STAGED_DIFF_NAME = ".review-pull-request.diff"
SINCE_STAGED_NAME = ".review-since-last-review.diff"


@dataclass(frozen=True)
class Hunk:
    block_start: int
    block_end: int
    new_start: int
    new_count: int
    additive: bool
    size: int

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1) - 1


def _target_path(line: str) -> str | None:
    target = line[4:].rstrip("\n")
    if target == "/dev/null":
        return None
    return target[2:] if target.startswith("b/") else target


def _walk(diff_text: str) -> Iterator[tuple[str, int, bool]]:
    path: str | None = None
    new_line = 0
    remaining = 0
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            path = None
            remaining = 0
            continue
        if remaining == 0 and line.startswith("+++ "):
            path = _target_path(line)
            continue
        match = _HUNK.match(line)
        if match is not None:
            new_line = int(match.group("start"))
            remaining = int(match.group("count") or "1")
            continue
        if path is None or remaining <= 0:
            continue
        if line.startswith("+") or line.startswith(" "):
            yield path, new_line, line.startswith("+")
            new_line += 1
            remaining -= 1


def commentable_lines(diff_text: str) -> dict[str, frozenset[int]]:
    lines_by_path: dict[str, set[int]] = {}
    for path, number, _ in _walk(diff_text):
        lines_by_path.setdefault(path, set()).add(number)
    return {name: frozenset(numbers) for name, numbers in lines_by_path.items()}


def changed_lines(diff_text: str) -> dict[str, frozenset[int]]:
    """Lines the change touches on the new side: added lines, plus every new-side line of a hunk that deletes."""
    found: dict[str, set[int]] = {name: set(numbers) for name, numbers in added_lines(diff_text).items()}
    path: str | None = None
    span: tuple[int, int] | None = None
    remaining = 0
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            path, span, remaining = None, None, 0
            continue
        if remaining == 0 and line.startswith("+++ "):
            path = _target_path(line)
            continue
        match = _HUNK.match(line)
        if match is not None:
            start = int(match.group("start"))
            count = int(match.group("count") or "1")
            span = (start, start + max(count, 1))
            remaining = count
            continue
        if line.startswith("+") or line.startswith(" "):
            remaining -= 1
        elif line.startswith("-") and path is not None and span is not None:
            found.setdefault(path, set()).update(range(span[0], span[1] + 1))
    return {name: frozenset(numbers) for name, numbers in found.items()}


def added_lines(diff_text: str) -> dict[str, frozenset[int]]:
    lines_by_path: dict[str, set[int]] = {}
    for path, number, added in _walk(diff_text):
        if added:
            lines_by_path.setdefault(path, set()).add(number)
    return {name: frozenset(numbers) for name, numbers in lines_by_path.items()}


def select_files(diff_text: str, paths: frozenset[str]) -> str:
    sections = re.split(r"(?m)^(?=diff --git )", diff_text)
    kept = []
    for section in sections:
        header = section.split("\n", 1)[0]
        match = re.fullmatch(r"diff --git a/(?P<old>.+) b/(?P<new>.+)", header)
        if match is not None and (match.group("new") in paths or match.group("old") in paths):
            kept.append(section)
    return "".join(kept)


def binary_notice_count(diff_text: str) -> int:
    return len(re.findall(r"(?m)^(?:Binary files .+ differ|GIT binary patch)$", diff_text))


def binary_files(diff_text: str) -> frozenset[str]:
    names: set[str] = set()
    for section in re.split(r"(?m)^(?=diff --git )", diff_text):
        lines = section.split("\n")
        match = re.fullmatch(r"diff --git a/(?P<old>.+) b/(?P<new>.+)", lines[0])
        if match is None:
            continue
        old, new = match.group("old"), match.group("new")
        notices = {
            f"Binary files a/{old} and b/{new} differ",
            f"Binary files /dev/null and b/{new} differ",
            f"Binary files a/{old} and /dev/null differ",
        }
        for line in lines[1:]:
            if line.startswith("@@"):
                break
            if line == "GIT binary patch" or line in notices:
                names.add(new)
                break
    return frozenset(names)


_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}
_OCTAL = re.compile(r"[0-7]{1,3}")


def _unquote(token: str) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token
    out = bytearray()
    position = 1
    end = len(token) - 1
    while position < end:
        char = token[position]
        if char != "\\" or position + 1 >= end:
            out += char.encode("utf-8")
            position += 1
            continue
        octal = _OCTAL.match(token, position + 1, end)
        if octal is not None:
            out.append(int(octal.group(), 8) & 0xFF)
            position = octal.end()
        elif token[position + 1] in _ESCAPES:
            out.append(_ESCAPES[token[position + 1]])
            position += 2
        else:
            out += token[position : position + 2].encode("utf-8")
            position += 2
    return out.decode("utf-8", "replace")


def _side_path(line: str, prefix: str) -> str | None:
    target = _unquote(line[4:].rstrip("\n\t"))
    if target == "/dev/null":
        return None
    return target[len(prefix) :] if target.startswith(prefix) else target


def _header_path(line: str) -> str | None:
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        end = 1
        while end < len(rest) and not (rest[end] == '"' and rest[end - 1] != "\\"):
            end += 1
        target = _unquote(rest[end + 1 :].lstrip())
        return (target[2:] if target.startswith("b/") else target) or None
    if not rest.startswith("a/"):
        return None
    candidates = [match.start() for match in re.finditer(r' (?=b/|")', rest)]
    for position in candidates:
        right = _unquote(rest[position + 1 :])
        right = right[2:] if right.startswith("b/") else right
        if rest[2:position] == right:
            return right
    if not candidates:
        return None
    tail = _unquote(rest[candidates[0] + 1 :])
    return tail[2:] if tail.startswith("b/") else tail


def hunk_index(diff_text: str) -> dict[str, list[Hunk]]:
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    index: dict[str, list[Hunk]] = {}
    path: str | None = None
    old_path: str | None = None
    position = 0
    while position < len(lines):
        line = lines[position]
        if line.startswith("diff --git "):
            old_path = None
            path = _header_path(line)
            if path is not None:
                index.setdefault(path, [])
            position += 1
            continue
        if line.startswith("--- "):
            old_path = _side_path(line, "a/")
            position += 1
            continue
        if line.startswith("+++ "):
            path = _side_path(line, "b/") or old_path or path
            if path is not None:
                index.setdefault(path, [])
            position += 1
            continue
        match = _FULL_HUNK.match(line)
        if match is None or path is None:
            position += 1
            continue
        old_left = int(match.group("old_count") or "1")
        new_left = int(match.group("count") or "1")
        new_count = new_left
        start = position
        additive = True
        position += 1
        while position < len(lines) and (old_left > 0 or new_left > 0 or lines[position].startswith("\\")):
            body = lines[position]
            if body.startswith("\\"):
                position += 1
                continue
            if body.startswith("-"):
                old_left -= 1
                additive = False
            elif body.startswith("+"):
                new_left -= 1
            else:
                old_left -= 1
                new_left -= 1
            position += 1
        size = sum(len(item.encode("utf-8")) + 1 for item in lines[start:position])
        index.setdefault(path, []).append(
            Hunk(start + 1, position, int(match.group("start")), new_count, additive and new_count > 0, size)
        )
    return index


def deleting_files(diff_text: str) -> frozenset[str]:
    """Files with at least one hunk that deletes a line."""
    found: set[str] = set()
    path: str | None = None
    remaining = 0
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            path, remaining = None, 0
        elif remaining == 0 and line.startswith("+++ "):
            path = _target_path(line)
        elif (match := _HUNK.match(line)) is not None:
            remaining = int(match.group("count") or "1")
        elif line.startswith("+") or line.startswith(" "):
            remaining -= 1
        elif line.startswith("-") and path is not None:
            found.add(path)
    return frozenset(found)
