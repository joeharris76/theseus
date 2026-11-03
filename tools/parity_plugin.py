from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

TARGET = os.environ.get("PARITY_TARGET", "")
PYTHON = os.environ.get("PARITY_PYTHON", "")
OUT = Path(os.environ.get("PARITY_OUT", "."))
TREE = Path(os.environ.get("PARITY_BASELINE_TREE", "."))
CAPTURES = OUT / "captures"

SCAN_SOURCE = {
    "installed": "from theseus_reviewer import driver\n",
    "baseline": "from _project.scripts.oracle_reviewers import driver\n",
}
WRAPPER = textwrap.dedent(
    """\
    import os
    import runpy
    import subprocess
    import sys

    TREE = {tree!r}
    RENAMED = (
        ("THESEUS_OMP_CACHE", "BENCHBOX_OMP_CACHE"),
        ("THESEUS_OMP_DOWNLOAD_MIRROR", "BENCHBOX_OMP_DOWNLOAD_MIRROR"),
        ("THESEUS_EPHEMERAL_CLONE", "BENCHBOX_EPHEMERAL_CLONE"),
    )
    for new, old in RENAMED:
        if new in os.environ:
            os.environ[old] = os.environ[new]
    DROPPED = {{"gate": ("--policy", "--root"), "local-review": ("--repo",), "gateway-clients": ("--omp-config",)}}
    args = sys.argv[2:]


    def without(values, flags):
        kept = []
        index = 0
        while index < len(values):
            if values[index] in flags:
                index += 2
                continue
            kept.append(values[index])
            index += 1
        return kept


    def script(root, name, rest):
        sys.argv = [name, *rest]
        sys.path.insert(0, root + "/_project/scripts")
        runpy.run_path(root + "/_project/scripts/" + name, run_name="__main__")


    if args[:1] == ["gate"]:
        script(args[args.index("--root") + 1], "oracle_gate.py", without(args[1:], DROPPED["gate"]))
    elif args[:2] == ["matcher", "check"]:
        script(TREE, "soundness_paths.py", without(args[2:], ("--manifest",)))
    else:
        if args[:1] == ["local-review"]:
            probe = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, check=False
            )
            status = os.path.join(probe.stdout.strip(), "theseus-cloud-gateway.status")
            if probe.returncode == 0 and os.path.isfile(status):
                with open(status, encoding="utf-8") as source:
                    text = source.read()
                with open(os.path.join(os.path.dirname(status), "benchbox-cloud-gateway.status"), "w") as target:
                    target.write(text)
        root = os.getcwd() if os.path.isdir(os.path.join(os.getcwd(), "_project", "scripts")) else TREE
        sys.path.insert(0, root)
        from _project.scripts.oracle_reviewers.cli import main

        if args[:1] == ["local-review"]:
            from _project.scripts.oracle_reviewers import local

            local.withhold_secrets()

        raise SystemExit(main(args[:1] + without(args[1:], DROPPED.get(args[0], ()) if args else ())))
    """
)


def _harness_modules() -> list[Any]:
    return [module for name, module in list(sys.modules.items()) if name.endswith("integration.harness")]


def _sibling(module: Any, leaf: str) -> Any:
    return sys.modules.get(module.__name__.removesuffix("harness") + leaf)


def _scan(python: str, source: str) -> Any:
    def scan_step(harness: Any, name: str, env: dict[str, str], policy: str) -> Any:
        code = (
            "import dataclasses, os, sys\n"
            "from pathlib import Path\n"
            f"{source}"
            "env = driver.CiEnvironment.from_environ(os.environ, Path(sys.argv[1]))\n"
            "shared = {Path('/tmp'), Path('/var/tmp'), Path('/dev/shm')}\n"
            "kept = tuple(root for root in env.scan_roots if root not in shared)\n"
            "raise SystemExit(driver.scan(dataclasses.replace(env, scan_roots=kept), None))\n"
        )
        if TARGET == "baseline":
            env = {**env, "PYTHONPATH": str(TREE)}
        return harness.run(name, [python, "-c", code, policy], env)

    return scan_step


def _bind_baseline(module: Any) -> None:
    harness_class = module.Harness
    original_policy = harness_class._pinned_policy
    original_init = harness_class.__init__

    def pinned_policy(self: Any) -> str:
        data = yaml.safe_load(original_policy(self))
        data.pop("consumer", None)
        return yaml.safe_dump(data, sort_keys=False)

    def init(self: Any, root: Path) -> None:
        original_init(self, root)
        shutil.copytree(TREE / "_project", self.trusted / "_project")

    harness_class._pinned_policy = pinned_policy
    harness_class.__init__ = init
    harness_class.local_state = property(lambda self: self.home / ".cache" / "benchbox" / "local-oracle")


def _bind_golden(module: Any, baseline: bool) -> None:
    golden = _sibling(module, "golden")
    if golden is None:
        return
    if baseline:
        policy = module.ALPHA_ROOT / module.POLICY
        real_load = golden.load_policy
        golden.load_policy = lambda _path: real_load(policy)

    def record(name: str, value: Any, harness: Any) -> None:
        CAPTURES.mkdir(parents=True, exist_ok=True)
        (CAPTURES / f"golden-{name}.json").write_text(
            json.dumps(golden.normalize(value, harness), indent=2, sort_keys=True), encoding="utf-8"
        )

    golden.assert_golden = record
    for name, loaded in list(sys.modules.items()):
        if name.endswith("integration.test_golden_outputs"):
            loaded.assert_golden = record


def _bind(module: Any) -> None:
    python = PYTHON
    if TARGET == "baseline":
        wrapper = OUT / "baseline_cli.py"
        wrapper.write_text(WRAPPER.format(tree=str(TREE)), encoding="utf-8")
        module.PACKAGE[:] = [python, str(wrapper), "--baseline"]
        _bind_baseline(module)
    else:
        module.PACKAGE[:] = [python, "-m", "theseus_reviewer"]
    runs = _sibling(module, "runs")
    if runs is not None:
        runs.scan_step = _scan(python, SCAN_SOURCE[TARGET])
    _bind_golden(module, TARGET == "baseline")


def pytest_configure(config: pytest.Config) -> None:
    (OUT / "results").mkdir(parents=True, exist_ok=True)


def pytest_collection_finish(session: pytest.Session) -> None:
    for module in _harness_modules():
        if getattr(module, "_parity_bound", False):
            continue
        _bind(module)
        module._parity_bound = True


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        detail = str(report.longrepr).strip().splitlines()[-1][:300] if report.outcome == "failed" else ""
        line = json.dumps({"nodeid": report.nodeid, "outcome": report.outcome, "detail": detail})
        with (OUT / "results" / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
