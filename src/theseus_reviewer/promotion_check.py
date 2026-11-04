from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import promotion
from .policy import PolicyError, load_policy


def command_check(args: argparse.Namespace) -> int:
    try:
        rules = load_policy(Path(args.policy)).promotion
        if not rules.repository:
            raise promotion.PromotionError("the consumer policy has no promotion section")
        lock = promotion.parse_lock(Path(args.lock).read_text(encoding="utf-8"))
        if (lock.package, lock.repository.casefold()) != (rules.package, rules.repository.casefold()):
            raise promotion.PromotionError("theseus lock package or repository does not match consumer policy")
        tree = promotion.load_tree_from_dir(Path(args.source))
        actual = promotion.tree_sha256(tree)
        if actual != lock.source_sha256:
            raise promotion.PromotionError(
                f"the source tree under {args.source} hashes to {actual}, but the lock pins source_sha256 "
                f"{lock.source_sha256}"
            )
        promotion.verify_lock_artifacts(lock, tree, promotion.default_artifact_loader(), rules)
    except (OSError, PolicyError, promotion.PromotionError) as exc:
        print(f"theseus promotion-check: refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"theseus promotion-check: {lock.package} {lock.version} source {lock.source_commit} "
        f"artifact {lock.artifact_sha256} lock {promotion.lock_digest(lock)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theseus promotion-check",
        description="Verify a theseus lock's artifact bytes and dependency closure against a reviewed source tree.",
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--source", required=True, help="Directory holding the reviewed upstream source tree.")
    return command_check(parser.parse_args(argv))
