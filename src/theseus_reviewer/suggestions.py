"""One-click suggestion blocks for mechanical findings.

A suggestion is a presentation convenience. It never carries trust: the committed change is reviewed like any
other on the next head, and nothing reads a suggestion back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .classifier import matching
from .matchers import Matchers
from .policy import Policy
from .verdict import Finding

MAX_LINES = 20
PROTECTED_NAMES = frozenset({"review.yml", "review.yaml", "theseus-lock.json"})
_BACKTICKS = re.compile(r"`+")


def blocked_paths(paths: Iterable[str], policy: Policy, matchers: Matchers) -> list[str]:
    """The changed paths that never receive a suggestion: gate, soundness, policy, lock and workflow files."""
    consumer = policy.consumer
    named = {
        consumer.soundness_manifest,
        consumer.omp_config,
        *(item for item in (consumer.inert_manifest, consumer.review_guidance) if item),
    }
    return sorted(
        {
            path
            for path in paths
            if path.startswith(".github/")
            or path.rsplit("/", 1)[-1] in PROTECTED_NAMES
            or path in named
            or matchers.is_protected(path)
            or matching([path], policy.classifier.gate_paths)
        }
    )


def _head_lines(workspace: Path | str | None, path: str) -> list[str] | None:
    if workspace is None:
        return None
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    if root != target and root not in target.parents or not target.is_file() or target.is_symlink():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return None if "\r" in text else text.split("\n")


def block(
    finding: Finding,
    plan: Mapping[str, Any],
    anchor: tuple[int, int],
    commentable: Mapping[str, frozenset[int]],
    workspace: Path | str | None,
) -> str:
    """The suggestion block for `finding`, or "" when any condition fails.

    `anchor` is the line span the thread comment covers; the suggestion replaces exactly those lines, so it is
    offered only when they are the finding's own span, are all inside the diff, exist in the head file and are
    changed by the replacement.
    """
    replacement = finding.replacement
    if replacement is None or plan.get("suggestion_blocked") is None:
        return ""
    start, end = anchor
    wanted = (finding.line, finding.end_line or finding.line)
    if (start, end) != wanted or end - start + 1 > MAX_LINES or finding.file in plan["suggestion_blocked"]:
        return ""
    lines = commentable.get(finding.file, frozenset())
    if not all(number in lines for number in range(start, end + 1)):
        return ""
    new = replacement.removesuffix("\n").split("\n")
    head = _head_lines(workspace, finding.file)
    if head is None or end > len(head) or len(new) > MAX_LINES or new == head[start - 1 : end]:
        return ""
    longest = max((len(run) for run in _BACKTICKS.findall(replacement)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}suggestion\n{replacement.removesuffix(chr(10))}\n{fence}"
