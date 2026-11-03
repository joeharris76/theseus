from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .soundness_paths import Rule, glob_matches, load_rules, normalize_path, parse_rules, rules_match

INERT_KINDS = frozenset({"file", "glob", "prefix", "regex", "except"})
INERT_LABEL = "inert path"


@dataclass(frozen=True)
class InertMatcher:
    rules: tuple[Rule, ...]

    @classmethod
    def from_text(cls, text: str, source: str = "manifest") -> InertMatcher:
        return cls(parse_rules(text, source, INERT_KINDS, INERT_LABEL))

    @classmethod
    def from_file(cls, path: Path) -> InertMatcher:
        return cls(load_rules(path, INERT_KINDS, INERT_LABEL))

    def matches_exemption(self, path: str) -> bool:
        return rules_match(path, [rule for rule in self.rules if rule.kind != "except"])

    def matches_exception(self, path: str) -> bool:
        normalized = normalize_path(path)
        return any(glob_matches(normalized, rule.pattern) for rule in self.rules if rule.kind == "except")

    def is_machine_read_doc(self, path: str) -> bool:
        return self.matches_exception(path)

    def is_inert_path(self, path: str) -> bool:
        return self.matches_exemption(path) and not self.matches_exception(path)

    def any_inert(self, paths: Sequence[str]) -> bool:
        return any(self.is_inert_path(path) for path in paths)
