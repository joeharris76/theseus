#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import seed_hash  # noqa: E402

ABSENT = "<absent>"
BASELINE_PATHS = (
    "_project/scripts",
    "_project/inert-doc-readers.json",
    ".github/oracle-omp-config.yml",
    ".github/oracle-reviewers.yml",
    ".github/soundness-paths.txt",
    ".github/inert-paths.txt",
    "scripts/oracle_local_review.py",
)
EXCLUDED_MODULES = (
    "tests/integration/test_two_consumers.py",
    "tests/integration/test_gateway_host.py",
    "tests/integration/test_hosted_pins.py",
)


@dataclass(frozen=True)
class Intentional:
    capture: str
    path: str
    baseline: Any
    installed: Any
    reason: str

    def covers(self, capture: str, path: str, baseline: Any, installed: Any) -> bool:
        return (
            fnmatch.fnmatchcase(capture, self.capture)
            and path == self.path
            and baseline == self.baseline
            and installed == self.installed
        )


EXCLUDED_TESTS = (
    "tests/integration/test_local_driver_scenarios.py::test_the_ci_and_local_adapters_plan_judge_and_map_the_same_patch_identically",
)
INTENTIONAL_OUTCOMES = (
    (
        "test_a_cloud_review_without_the_recorded_proxy_refuses_and_says_how_to_start_it",
        "test_cloud_review_reachability_scenario.py:94: AssertionError",
        "the unreachable-gateway remedy reads `start the cloud gateway`; BenchBox's launcher said `run make cloud-gateway`, "
        "a consumer command the package must not name",
    ),
)
OMP_CONFIG = ".github/oracle-omp-config.yml"
INTENTIONAL = (
    Intentional(
        "*",
        "plan/omp_config",
        ABSENT,
        OMP_CONFIG,
        "the plan records the consumer-configured omp config path; BenchBox hard-coded it",
    ),
)
CONFIGURATION_DIFFERENCES = (
    "consumer identity comes from the policy `consumer` section (gated base, attesters, soundness/inert/omp-config "
    "manifests); the baseline side runs the baseline policy, which has no such section and hard-codes the same values",
    "`theseus gate` takes --policy and --root and runs from the installed package; the baseline script reads its own "
    "repository's policy",
    "`theseus local-review --repo OWNER/NAME` and `theseus gateway-clients --omp-config PATH` are required flags the "
    "baseline derived from its checkout; the baseline side runs without them against identical inputs",
    "`theseus matcher check --manifest PATH` is required; the baseline matcher reads its fixed manifest, which is "
    "byte-identical to the fixture manifest",
    "local state defaults to ~/.cache/theseus/local-oracle (override THESEUS_LOCAL_STATE); the baseline used "
    "~/.cache/benchbox/local-oracle; state trees are compared relative to their root",
    "review cache and member record file names are keyed by a digest that covers the installed package bytes, so the "
    "two sides name them differently; they are compared by content, with lock files (volatile holder text) skipped",
    "tool-owned names are neutral: THESEUS_OMP_CACHE, THESEUS_OMP_DOWNLOAD_MIRROR and THESEUS_EPHEMERAL_CLONE replace "
    "the BENCHBOX_ variables and the cloud gateway status file is theseus-cloud-gateway.status; the baseline side maps "
    "each to its old name",
    "scenarios with no baseline counterpart are excluded: test_two_consumers (consumer identity isolation), "
    "test_hosted_pins (separate reviewer and gate pins), test_gateway_host (installed commands), and the in-process "
    "CI-versus-local adapter comparison in test_local_driver_scenarios, which runs Theseus code on both sides",
)


def run(
    argv: list[str], cwd: Path, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    print("$", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"parity failed: exit {result.returncode}: {' '.join(argv)}")
    return result


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest() -> str:
    names = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "src/theseus_reviewer"], capture_output=True, text=True, check=True
    ).stdout.split()
    lines = sorted(f"{name}\t{sha256_file(ROOT / name)}\n" for name in names)
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def require_clean_source(allow_dirty: bool) -> str:
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty and not allow_dirty:
        raise SystemExit(f"parity refused: uncommitted changes in the shipped source would not be frozen:\n{dirty}")
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def recorded_seed_source_hash() -> str:
    match = re.search(r"^source\s+([0-9a-f]{64})$", (ROOT / "PROVENANCE.md").read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise SystemExit("PROVENANCE.md records no normalized seed source hash")
    return match.group(1)


def extract_baseline(repo: Path, commit: str, tree: Path) -> str:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit, "--", *BASELINE_PATHS],
        capture_output=True,
        check=True,
    ).stdout
    tree.mkdir(parents=True)
    with tarfile.open(fileobj=BytesIO(archive)) as bundle:
        bundle.extractall(tree, filter="data")
    entries = seed_hash.load_map()["entries"]
    return seed_hash.digest(entries, lambda entry: (tree / entry["benchbox"]).read_bytes(), frozenset({"source"}))


def build_installed(scratch: Path, python: str, wheel: Path | None) -> tuple[Path, Path]:
    if wheel is None:
        run(["uv", "build", "--wheel", "--out-dir", str(scratch / "dist")], ROOT)
        wheel = next((scratch / "dist").glob("theseus_reviewer-*.whl"))
    venv = scratch / "venv"
    run(["uv", "venv", "--python", python, str(venv)], scratch)
    interpreter = venv / "bin" / "python"
    run(["uv", "pip", "install", "--python", str(interpreter), str(wheel)], scratch)
    location = run(
        [str(interpreter), "-I", "-c", "import theseus_reviewer; print(theseus_reviewer.__file__)"], scratch
    ).stdout
    if str(ROOT) in location:
        raise SystemExit(f"parity failed: the installed package imported from the checkout: {location.strip()}")
    return wheel, interpreter


def run_target(name: str, interpreter: Path, tree: Path, out: Path) -> dict[str, Any]:
    target_out = out / name
    target_out.mkdir(parents=True)
    env = {
        **os.environ,
        "PARITY_TARGET": name,
        "PARITY_PYTHON": str(interpreter),
        "PARITY_OUT": str(target_out),
        "PARITY_BASELINE_TREE": str(tree),
        "PYTHONPATH": str(ROOT),
    }
    ignores = [f"--ignore={module}" for module in EXCLUDED_MODULES]
    ignores += [f"--deselect={test}" for test in EXCLUDED_TESTS]
    result = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration",
            "-p",
            "tools.parity_plugin",
            "-q",
            "-p",
            "no:cacheprovider",
            *ignores,
        ],
        ROOT,
        env,
        check=False,
    )
    (target_out / "pytest.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    outcomes: dict[str, dict[str, str]] = {}
    for results in sorted((target_out / "results").glob("*.jsonl")):
        for line in results.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            outcomes[item["nodeid"].split("::", 1)[-1] if "::" in item["nodeid"] else item["nodeid"]] = item
    captures = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((target_out / "captures").glob("*.json"))
    }
    return {"outcomes": outcomes, "captures": captures}


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key in sorted(value):
            yield from leaves(value[key], f"{path}/{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{path}/{index}" if path else str(index))
    else:
        yield path, value


def differences(baseline: Any, installed: Any) -> list[tuple[str, Any, Any]]:
    left = dict(leaves(baseline))
    right = dict(leaves(installed))
    found: list[tuple[str, Any, Any]] = []
    for path in sorted(set(left) | set(right)):
        before, after = left.get(path, ABSENT), right.get(path, ABSENT)
        if before != after:
            found.append((path, before, after))
    return found


def compare(baseline: dict[str, Any], installed: dict[str, Any]) -> dict[str, Any]:
    explained: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []
    identical: list[str] = []
    for name in sorted(set(baseline["captures"]) | set(installed["captures"])):
        left = baseline["captures"].get(name)
        right = installed["captures"].get(name)
        if left is None or right is None:
            unexplained.append(
                {"capture": name, "path": "", "baseline": left is not None, "installed": right is not None}
            )
            continue
        remaining = False
        for path, before, after in differences(left, right):
            rule = next((item for item in INTENTIONAL if item.covers(name, path, before, after)), None)
            if rule is None:
                unexplained.append({"capture": name, "path": path, "baseline": before, "installed": after})
                remaining = True
            else:
                explained.append({"capture": name, "path": path, "reason": rule.reason})
        if not remaining and not any(item["capture"] == name for item in explained):
            identical.append(name)
    outcome_problems: list[dict[str, Any]] = []
    explained_outcomes: list[dict[str, str]] = []
    for name in sorted(set(baseline["outcomes"]) | set(installed["outcomes"])):
        before = baseline["outcomes"].get(name, {"outcome": ABSENT})
        after = installed["outcomes"].get(name, {"outcome": ABSENT})
        rule = next(
            (
                item
                for item in INTENTIONAL_OUTCOMES
                if item[0] == name and before["outcome"] == "failed" and item[1] in before.get("detail", "")
            ),
            None,
        )
        if rule is not None and after["outcome"] == "passed":
            explained_outcomes.append({"test": name, "reason": rule[2]})
        elif before["outcome"] != after["outcome"] or "failed" in (before["outcome"], after["outcome"]):
            outcome_problems.append(
                {
                    "test": name,
                    "baseline": before["outcome"],
                    "installed": after["outcome"],
                    "baseline_detail": before.get("detail", ""),
                    "installed_detail": after.get("detail", ""),
                }
            )
    return {
        "identical_captures": identical,
        "explained": explained,
        "unexplained": unexplained,
        "outcome_problems": outcome_problems,
        "explained_outcomes": explained_outcomes,
        "tests": {"baseline": len(baseline["outcomes"]), "installed": len(installed["outcomes"])},
    }


def render(report: dict[str, Any], summary: dict[str, Any]) -> str:
    lines = [
        f"baseline commit        {summary['baseline_commit']}",
        f"baseline source hash   {summary['baseline_source_hash']} (matches PROVENANCE.md)",
        f"theseus commit         {summary['theseus_commit']}",
        f"frozen source hash     {summary['frozen_source_hash']}",
        f"wheel                  {summary['wheel']} sha256 {summary['wheel_sha256']}",
        f"scenario tests         baseline {report['tests']['baseline']}, installed {report['tests']['installed']}",
        f"captures compared      {len(report['identical_captures']) + len({item['capture'] for item in report['explained']})}",
        f"  identical            {len(report['identical_captures'])}",
        f"  differing only as listed  {len({item['capture'] for item in report['explained']})}",
        f"explained differences  {len(report['explained'])}",
    ]
    reasons: dict[str, int] = {}
    for item in report["explained"]:
        key = f"{item['path']}: {item['reason']}"
        reasons[key] = reasons.get(key, 0) + 1
    lines.extend(f"  {count} x {key}" for key, count in sorted(reasons.items()))
    lines.append(f"explained outcome differences  {len(report['explained_outcomes'])}")
    lines.extend(f"  {item['test']}: {item['reason']}" for item in report["explained_outcomes"])
    lines.append("intentional configuration differences")
    lines.extend(f"  - {text}" for text in CONFIGURATION_DIFFERENCES)
    lines.append(f"unexplained capture differences  {len(report['unexplained'])}")
    lines.extend(f"  {item}" for item in report["unexplained"][:50])
    lines.append(f"scenario outcome problems        {len(report['outcome_problems'])}")
    lines.extend(f"  {item}" for item in report["outcome_problems"][:50])
    verdict = "PASS" if not report["unexplained"] and not report["outcome_problems"] else "FAIL"
    lines.append(f"parity {verdict}: zero unexplained differences" if verdict == "PASS" else f"parity {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and install the wheel in a clean venv, then run the same deterministic scenarios through "
        "the installed CLI and through the baseline BenchBox scripts extracted with git archive."
    )
    parser.add_argument("--benchbox-repo", type=Path)
    parser.add_argument("--source-hash", action="store_true", help="Print the frozen source hash and exit.")
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--wheel", type=Path, help="Wheel to install; default builds one with uv.")
    parser.add_argument("--report", type=Path, help="Write the full JSON report here.")
    parser.add_argument("--allow-dirty", action="store_true", help="Run against uncommitted shipped source.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch directory.")
    args = parser.parse_args(argv)
    if args.source_hash:
        print(source_digest())
        return 0
    if args.benchbox_repo is None:
        parser.error("--benchbox-repo is required")
    commit = seed_hash.load_map()["baseline_commit"]
    head = require_clean_source(args.allow_dirty)
    scratch = Path(tempfile.mkdtemp(prefix="theseus-parity-"))
    try:
        tree = scratch / "baseline"
        baseline_hash = extract_baseline(args.benchbox_repo, commit, tree)
        if baseline_hash != recorded_seed_source_hash():
            raise SystemExit(f"parity failed: the extracted baseline hashes to {baseline_hash}, not the recorded seed")
        wheel, interpreter = build_installed(scratch, args.python, args.wheel)
        results = {
            "baseline": run_target("baseline", interpreter, tree, scratch / "out"),
            "installed": run_target("installed", interpreter, tree, scratch / "out"),
        }
        report = compare(results["baseline"], results["installed"])
        summary = {
            "baseline_commit": commit,
            "baseline_source_hash": baseline_hash,
            "theseus_commit": head,
            "frozen_source_hash": source_digest(),
            "wheel": wheel.name,
            "wheel_sha256": sha256_file(wheel),
        }
        print(render(report, summary))
        if args.report:
            args.report.write_text(
                json.dumps(
                    {"summary": summary, **report, "configuration_differences": CONFIGURATION_DIFFERENCES}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        return 0 if not report["unexplained"] and not report["outcome_problems"] else 1
    finally:
        if args.keep:
            print(f"scratch kept at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
