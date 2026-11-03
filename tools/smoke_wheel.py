#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    "tests/integration/test_review_scenarios.py",
    "tests/integration/test_two_consumers.py",
    "tests/integration/test_theseus_promotion_scenario.py",
    "tests/integration/test_local_review_scenario.py",
    "tests/integration/test_local_findings_scenarios.py",
    "tests/integration/test_local_concurrency_scenarios.py",
    "tests/integration/test_local_driver_scenarios.py",
    "tests/integration/test_cloud_review_reachability_scenario.py",
    "tests/integration/test_redaction_scenarios.py",
    "tests/integration/test_gateway_host.py",
)


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    print("$", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise SystemExit(f"smoke failed: exit {result.returncode}: {' '.join(argv)}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the built wheel outside the checkout and exercise it.")
    parser.add_argument("--wheel", type=Path, help="Wheel to install; default builds one with uv.")
    parser.add_argument("--python", default="3.11")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="theseus-smoke-") as scratch_name:
        scratch = Path(scratch_name)
        wheel = args.wheel
        if wheel is None:
            run(["uv", "build", "--wheel", "--out-dir", str(scratch / "dist")], ROOT)
            wheel = next((scratch / "dist").glob("theseus_reviewer-*.whl"))
        venv = scratch / "venv"
        run(["uv", "venv", "--python", args.python, str(venv)], scratch)
        python = venv / "bin" / "python"
        run(["uv", "pip", "install", "--python", str(python), str(wheel), "pytest", "pytest-xdist"], scratch)
        run([str(venv / "bin" / "theseus"), "--help"], scratch)
        location = run([str(python), "-I", "-c", "import theseus_reviewer; print(theseus_reviewer.__file__)"], scratch)
        if str(ROOT) in location:
            raise SystemExit(f"smoke failed: the package imported from the checkout: {location.strip()}")
        shutil.copytree(ROOT / "tests", scratch / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / ".github" / "workflows", scratch / ".github" / "workflows")
        env = {"PATH": f"{venv / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(scratch / "home")}
        (scratch / "home").mkdir()
        run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "0", *SCENARIOS], scratch, env)
    print("smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
