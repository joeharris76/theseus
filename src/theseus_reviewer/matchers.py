from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import guidance
from .inert_paths import InertMatcher
from .policy import Consumer, PolicyError
from .soundness_paths import Matcher, load_rules


@dataclass(frozen=True)
class Matchers:
    soundness: Matcher
    inert: InertMatcher | None
    guidance: str | None = None

    def is_soundness_path(self, path: str) -> bool:
        return self.soundness.is_soundness_path(path)

    def is_inert_path(self, path: str) -> bool:
        return self.inert is not None and self.inert.is_inert_path(path)

    def is_machine_read_doc(self, path: str) -> bool:
        return self.inert is not None and self.inert.is_machine_read_doc(path)

    def is_protected(self, path: str) -> bool:
        return self.is_soundness_path(path) or self.is_machine_read_doc(path)


def load_matchers(consumer: Consumer, root: Path) -> Matchers:
    try:
        soundness = Matcher(load_rules(root / consumer.soundness_manifest))
        inert = InertMatcher.from_file(root / consumer.inert_manifest) if consumer.inert_manifest else None
        text = guidance.read(root, consumer.review_guidance) if consumer.review_guidance else None
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot load the consumer path manifests under {root}: {exc}") from exc
    return Matchers(soundness, inert, text)
