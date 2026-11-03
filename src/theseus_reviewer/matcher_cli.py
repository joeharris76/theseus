from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

from . import soundness_paths

SHA256_LENGTH = 64


def packaged_bytes() -> bytes:
    return Path(soundness_paths.__file__).read_bytes()


def packaged_sha256() -> str:
    return hashlib.sha256(packaged_bytes()).hexdigest()


def drift(copy: Path, expected_sha256: str | None = None) -> list[str]:
    problems: list[str] = []
    try:
        actual = copy.read_bytes()
    except OSError as exc:
        return [f"cannot read the vendored matcher {copy}: {exc}"]
    digest = hashlib.sha256(actual).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        problems.append(f"vendored matcher {copy} has sha256 {digest}, but its lock pins {expected_sha256}")
    if actual != packaged_bytes():
        problems.append(
            f"vendored matcher {copy} (sha256 {digest}) differs from the installed theseus matcher "
            f"(sha256 {packaged_sha256()}); promote the pinned release or restore the copy"
        )
    return problems


def _verify(args: argparse.Namespace) -> int:
    if args.sha256 is not None and len(args.sha256) != SHA256_LENGTH:
        print("theseus matcher: --sha256 must be a 64-character hex digest", file=sys.stderr)
        return 2
    problems = drift(Path(args.copy), args.sha256)
    for problem in problems:
        print(f"theseus matcher: {problem}", file=sys.stderr)
    if not problems:
        print(f"theseus matcher: {args.copy} is byte-identical to the installed matcher ({packaged_sha256()})")
    return 1 if problems else 0


def _path_check(args: argparse.Namespace) -> int:
    return soundness_paths.main(list(args.rest))


def _digest(args: argparse.Namespace) -> int:
    print(packaged_sha256())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="theseus matcher", description="Soundness-path matcher tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Fail when a vendored matcher copy drifts from the installed one.")
    verify.add_argument("--copy", required=True)
    verify.add_argument("--sha256", help="Digest the consumer lock pins for the copy.")
    verify.set_defaults(handler=_verify)
    digest = commands.add_parser("digest", help="Print the sha256 of the installed matcher module.")
    digest.set_defaults(handler=_digest)
    check = commands.add_parser("check", add_help=False, help="Run the path predicate (see soundness_paths.py).")
    check.add_argument("rest", nargs=argparse.REMAINDER)
    check.set_defaults(handler=_path_check)
    args = parser.parse_args(argv)
    return int(args.handler(args))
