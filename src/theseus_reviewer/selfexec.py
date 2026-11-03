from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

BOOT = (
    "import sys\n"
    "sys.path.insert(0, sys.argv.pop(1))\n"
    "from theseus_reviewer.main import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)


def package_command() -> list[str]:
    parent = Path(__file__).resolve().parent.parent
    return [sys.executable, "-I", "-B", "-S", "-c", BOOT, str(parent)]


def scrubbed_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if not name.upper().startswith("PYTHON")}
