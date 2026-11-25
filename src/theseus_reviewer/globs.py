from __future__ import annotations

import fnmatch


def _segments_match(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_segments_match(path[skip:], pattern[1:]) for skip in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], pattern[0]) and _segments_match(path[1:], pattern[1:])


def glob_match(path: str, pattern: str) -> bool:
    return _segments_match(tuple(path.split("/")), tuple(pattern.split("/")))
