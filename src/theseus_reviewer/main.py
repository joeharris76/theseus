from __future__ import annotations

import sys

from . import cli


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["local-review"]:
        from . import local

        local.withhold_secrets()
    return cli.main(arguments)


def run() -> None:
    raise SystemExit(main())
