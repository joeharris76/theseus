from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable

LITERAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
WILDCARD = re.compile(r"(?P<prefix>[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*)/(?P<star>\*{1,2})")
TRUSTED_BASE_ENV = "REVIEW_TRUSTED_BASE"
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HEADS = "refs/heads/"


class BasePatternError(ValueError):
    pass


def split_bases(raw: str) -> tuple[str, ...]:
    return tuple(item for item in re.split(r"[\s,]+", raw) if item)


def is_wildcard(pattern: str) -> bool:
    return "*" in pattern


def validate(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise BasePatternError("a gated base must be a branch name or a prefix pattern such as release/*")
    if is_wildcard(pattern):
        if WILDCARD.fullmatch(pattern) is None:
            raise BasePatternError(
                f"gated base {pattern!r} is not allowed: a wildcard must be the final segment after a literal "
                "prefix, as in release/* or release/**"
            )
    elif LITERAL.fullmatch(pattern) is None:
        raise BasePatternError(f"gated base {pattern!r} is not a branch name")
    return pattern


def parse_bases(patterns: Iterable[str]) -> tuple[str, ...]:
    found = tuple(validate(pattern) for pattern in patterns)
    if not found:
        raise BasePatternError("at least one gated base is required")
    if is_wildcard(found[0]):
        raise BasePatternError(f"the first gated base {found[0]!r} must be a literal branch name")
    if len(set(found)) != len(found):
        raise BasePatternError("gated bases must not repeat")
    return found


def pattern_matches(pattern: str, branch: str) -> bool:
    if not is_wildcard(pattern):
        return pattern == branch
    parsed = WILDCARD.fullmatch(pattern)
    if parsed is None:
        return False
    prefix = parsed["prefix"] + "/"
    if not branch.startswith(prefix):
        return False
    rest = branch[len(prefix) :]
    if parsed["star"] == "*":
        return SEGMENT.fullmatch(rest) is not None
    return all(SEGMENT.fullmatch(part) is not None for part in rest.split("/"))


def matches(patterns: Iterable[str], branch: str) -> bool:
    return any(pattern_matches(pattern, branch) for pattern in patterns)


def branch_of(ref: str) -> str | None:
    return ref[len(HEADS) :] if ref.startswith(HEADS) and len(ref) > len(HEADS) else None


def trusted_base(default: str) -> str:
    return os.environ.get(TRUSTED_BASE_ENV, "").strip() or default


def _emit(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    print(f"{name}={value}")


def _resolve() -> int:
    try:
        patterns = parse_bases(split_bases(os.environ.get("GATED_BASES", "")))
    except BasePatternError as exc:
        print(f"theseus: error: {exc}", file=sys.stderr)
        return 2
    branch = branch_of(os.environ.get("CURRENT_REF", ""))
    gated = branch is not None and matches(patterns, branch)
    if os.environ.get("EVENT_NAME") == "workflow_dispatch" and not gated:
        print(
            f"theseus: error: dispatch runs only from a gated base ({json.dumps(list(patterns))}), not from "
            f"{os.environ.get('CURRENT_REF', '')}",
            file=sys.stderr,
        )
        return 2
    _emit("gated", "true" if gated else "false")
    _emit("base", branch if gated and branch is not None else "")
    return 0


def _check(raw: str, branch: str) -> int:
    try:
        patterns = parse_bases(split_bases(raw))
    except BasePatternError as exc:
        print(f"theseus: error: {exc}", file=sys.stderr)
        return 2
    return 0 if matches(patterns, branch) else 1


def main(argv: list[str]) -> int:
    if argv == ["resolve"]:
        return _resolve()
    if len(argv) == 3 and argv[0] == "check":
        return _check(argv[1], argv[2])
    print("usage: bases.py resolve | bases.py check PATTERNS BRANCH", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
