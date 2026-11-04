from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

from theseus_reviewer import isolation, omp_pin
from theseus_reviewer._vendor import yaml

from .fake_github import APP_TOKEN, JOB_TOKEN
from .harness import (
    APP_PRIVATE_KEY,
    GATEWAY_TOKEN,
    HELD_SECRET,
    JEV_KEY,
    OAUTH_CLIENT_ID,
    PACKAGE,
    POLICY,
    REPO_ROOT,
    ReviewRun,
    Step,
    git,
)
from .harness import (
    outputs as outputs_of,
)

if TYPE_CHECKING:
    from .harness import Harness

GATEWAY_URL = "https://gateway.example.invalid"
TRUSTED_CHECKOUT = "Check out the trusted gated-branch copy"
BOOTSTRAP_CHECKOUT = "Check out the pinned Theseus bootstrap"
HOSTED_REVIEW = REPO_ROOT / ".github" / "workflows" / "review.yml"
ENVIRONMENT_STEPS = frozenset(
    {
        TRUSTED_CHECKOUT,
        BOOTSTRAP_CHECKOUT,
        "Join the tailnet to reach the credential gateway",
    }
)
REVIEWER_PIN = "Install the reviewer pin from the trusted base lock"
GATE_PIN = "Install the gate pin from the current gated-branch lock"
GATE_CHECKOUT = "Check out the current gated-branch gate"
GATE_FAILURE = "Fail the run when the gate was not posted from it"
HEAD_CHECKOUT = "Check out the pull request head for reading only"
MINT_STEP = "Mint the App token"
APP_REFUSAL = "Refuse a caller that did not pass the App secrets"
ISOLATION_PHASE = "isolation"
DECOYED_STEPS = frozenset({"prepare", "finalize", "publish"})
SCANS = ("scan-before", "scan-after-join", "scan-after-exit")
DRIVE_PHASE = re.compile(r"\btheseus drive --policy \"\$(?P<policy>\w+)\" (?P<phase>\w+)\s*$")
CONDITION = re.compile(
    r"(?P<left>steps\.\w+\.outputs\.\w+|steps\.\w+\.outcome|env\.\w+) (?P<operator>==|!=|>=) (?P<right>'[^']*'|\d+)"
)
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
STEP_OUTPUT = re.compile(r"steps\.(\w+)\.outputs\.(\w+)")
AFTER = re.compile(r"after attempt (\d+)$")


SCAN_WITHOUT_SHARED_SCRATCH = (
    "import dataclasses, os, sys\n"
    "from pathlib import Path\n"
    "from theseus_reviewer import driver\n"
    "env = driver.CiEnvironment.from_environ(os.environ, Path(sys.argv[1]))\n"
    "shared = {Path('/tmp'), Path('/var/tmp'), Path('/dev/shm')}\n"
    "kept = tuple(root for root in env.scan_roots if root not in shared)\n"
    "raise SystemExit(driver.scan(dataclasses.replace(env, scan_roots=kept), None))\n"
)


def scan_step(harness: Harness, name: str, env: dict[str, str], policy: str) -> Step:
    return harness.run(name, [sys.executable, "-c", SCAN_WITHOUT_SHARED_SCRATCH, policy], env)


@dataclass(frozen=True)
class Pins:
    bootstrap: Path
    mirror: str


@dataclass
class Options:
    classification: tuple[str, str] = ("medium", "standard")
    installable: bool = True
    swap_after_install: bool = False
    tamper_attempts: Callable[[Path], None] | None = None
    plan_env: dict[str, str] | None = None
    before_guard: Callable[[], None] | None = None
    before_gate: Callable[[], None] | None = None
    app: bool = True
    pins: Pins | None = None


def review_steps() -> list[dict[str, Any]]:
    return yaml.safe_load(HOSTED_REVIEW.read_text(encoding="utf-8"))["jobs"]["review"]["steps"]


def review_job_env() -> dict[str, str]:
    return yaml.safe_load(HOSTED_REVIEW.read_text(encoding="utf-8"))["jobs"]["review"]["env"]


def phase_keys() -> dict[str, tuple[str, dict[str, Any]]]:
    keyed: dict[str, tuple[str, dict[str, Any]]] = {}
    scans = 0
    for step in review_steps():
        match = DRIVE_PHASE.search(str(step.get("run", "")))
        if match is None:
            continue
        phase = match.group("phase")
        after = AFTER.search(step["name"])
        if "id" in step:
            key = str(step["id"])
        elif phase == "scan":
            key = SCANS[scans]
            scans += 1
        elif phase == "recover" and after:
            key = f"recover-{after.group(1)}"
        elif phase == "attempts":
            key = f"resume-{after.group(1)}" if after else "attempts"
        else:
            key = phase
        keyed[step["name"]] = (key, step)
    return keyed


def step_by_name(name: str) -> dict[str, Any]:
    return next(step for step in review_steps() if step["name"] == name)


def _bare(expression: str) -> str:
    match = EXPRESSION.fullmatch(expression.strip())
    return match.group(1) if match else expression.strip()


class WorkflowRunner:
    def __init__(self, harness: Harness, pr: int, record: ReviewRun, env: dict[str, str], options: Options) -> None:
        self.harness = harness
        self.pr = pr
        self.record = record
        self.options = options
        self.temp = record.temp
        self.env = {key: value for key, value in env.items() if key != omp_pin.BINARY_ENV}
        if not options.installable:
            self.env[omp_pin.CACHE_ENV] = str(harness.root / "omp-cache-empty")
        self.attempts_dir = self.temp / "oracle-attempts"
        self.final_dir = self.temp / "oracle-final"
        self.workspace = self.temp / "workspace"
        self.github_env: dict[str, str] = {}
        self.job_env = {key: self._expression(value) for key, value in review_job_env().items()}
        self.keys = {name: key for name, (key, _) in phase_keys().items()}
        self.waived: set[str] = set()
        self.pin_path: list[str] = []

    def _output(self, step: str, key: str) -> str:
        found = self.record.steps.get(step)
        return found.outputs.get(key, "") if found is not None else ""

    def _expression(self, raw: str) -> str:
        text = str(raw)
        if EXPRESSION.fullmatch(text.strip()) is None and EXPRESSION.search(text):
            return EXPRESSION.sub(lambda found: self._expression(found.group(0)), text)
        expression = _bare(text)
        known: dict[str, str] = {
            "inputs.environment": "oracle",
            "github.token": JOB_TOKEN,
            "github.workspace": str(self.temp),
            "runner.temp": str(self.temp),
            "inputs.policy_path": POLICY,
            "secrets.TYPESAFE_API_KEY": JEV_KEY,
            "secrets.TS_OAUTH_CI_SECRET": HELD_SECRET,
            "secrets.TS_OAUTH_CI_CLIENT_ID": OAUTH_CLIENT_ID,
            "secrets.ORACLE_GATEWAY_TOKEN": GATEWAY_TOKEN,
            "secrets.ORACLE_GATEWAY_URL": GATEWAY_URL,
            "github.event.pull_request.number || github.event.issue.number || github.event.inputs.pr": str(self.pr),
            "secrets.ORACLE_APP_ID != ''": "true" if self.options.app else "false",
            "secrets.TS_OAUTH_CI_CLIENT_ID != '' && secrets.TS_OAUTH_CI_SECRET != ''": "true",
        }
        if expression == "github.sha":
            return git(self.harness.repo, "rev-parse", "develop")
        step_output = STEP_OUTPUT.fullmatch(expression)
        if step_output:
            return self._output(*step_output.group(1, 2))
        assert expression in known, f"workflow expression is not modelled by the runner: {expression}"
        return known[expression]

    def _clause(self, clause: str) -> bool:
        if clause in ("always()", "success()"):
            return True
        match = CONDITION.fullmatch(clause)
        assert match is not None, f"workflow condition clause is not modelled by the runner: {clause}"
        left, operator, right = match.group("left", "operator", "right")
        if left.startswith("env."):
            actual = self.job_env[left.removeprefix("env.")]
        elif left.endswith(".outcome"):
            found = self.record.steps.get(left.split(".")[1])
            actual = "skipped" if found is None else ("success" if found.code == 0 else "failure")
        else:
            _, step, _, key = left.split(".")
            actual = self._output(step, key)
        if operator == ">=":
            return int(actual or 0) >= int(right)
        expected = right.strip("'")
        return (actual == expected) if operator == "==" else (actual != expected)

    def _runs(self, step: dict[str, Any]) -> bool:
        condition = _bare(str(step.get("if", "success()")))
        clauses = [clause.strip() for clause in condition.split("&&")]
        if "always()" not in clauses and any(
            found.code != 0 for key, found in self.record.steps.items() if key not in self.waived
        ):
            return False
        return all(self._clause(clause) for clause in clauses)

    def _step_env(self, step: dict[str, Any], key: str) -> dict[str, str]:
        scoped = {name: self._expression(value) for name, value in step.get("env", {}).items()}
        if key in DECOYED_STEPS:
            scoped["ORACLE_APP_PRIVATE_KEY"] = APP_PRIVATE_KEY
        if key == "prepare":
            scoped.update(self.options.plan_env or {})
        self.record.step_env[key] = scoped
        return {
            **self.env,
            **self.job_env,
            **self.github_env,
            "GITHUB_WORKSPACE": str(self.temp),
            "TYPESAFE_BASE_URL": self.harness.jev.url,
            **scoped,
        }

    def _drive(self, step: dict[str, Any], phase: str, policy: str) -> Step:
        key = self.keys[step["name"]]
        env = self._step_env(step, key)
        env_file = self.temp / f"github-env-{key}.txt"
        env_file.write_text("", encoding="utf-8")
        extra = {"GITHUB_ENV": str(env_file)}
        if phase == "scan":
            result = scan_step(self.harness, key, {**env, **extra}, policy)
        elif self.options.pins is not None:
            path = os.pathsep.join([*self.pin_path, env["PATH"]])
            result = self.harness.run(
                key, ["bash", "-e", "-c", step["run"]], {**env, **extra, "PATH": path}, cwd=self.temp / "trusted"
            )
        else:
            result = self.harness.cli(key, ["drive", "--policy", policy, phase], {**env, **extra})
        written = env_file.read_text(encoding="utf-8")
        if phase == "harness":
            result.outputs["env"] = written
        self.github_env.update(dict(line.split("=", 1) for line in written.splitlines()))
        self.record.exported_env = dict(self.github_env)
        if phase == ISOLATION_PHASE:
            self._check_isolation(result, env)
        self.record.steps[key] = result
        return result

    def _check_isolation(self, result: Step, env: dict[str, str]) -> None:
        with mock.patch.dict(os.environ, env, clear=True):
            try:
                isolation.require_isolation()
                expected = 0
            except (OSError, ValueError, KeyError):
                expected = 1
        assert result.code == expected, result.stderr
        self.waived.add(ISOLATION_PHASE)

    def _checkout_head(self) -> None:
        head = self._output("prepare", "head_sha")
        git(self.harness.repo, "clone", "--quiet", "--no-checkout", str(self.harness.repo), str(self.workspace))
        git(self.workspace, "checkout", "--quiet", "--detach", head)

    def _mint(self) -> None:
        self.record.steps["app"] = Step("app", 0, "", "", {"token": APP_TOKEN})

    def _clone(self, name: str) -> None:
        target = self.temp / name
        if target.exists():
            shutil.rmtree(target)
        git(self.harness.root, "clone", "--quiet", str(self.harness.repo), str(target))

    def _bash(self, key: str, step: dict[str, Any]) -> Step:
        pins = self.options.pins
        assert pins is not None
        github_output = self.temp / f"github-output-{key}.txt"
        github_path = self.temp / f"github-path-{key}.txt"
        for file in (github_output, github_path):
            file.write_text("", encoding="utf-8")
        env = {
            **self.env,
            "GITHUB_WORKSPACE": str(self.temp),
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_PATH": str(github_path),
            "ORACLE_PROMOTION_MIRROR": pins.mirror,
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), self.env["PATH"]]),
            **{name: self._expression(value) for name, value in step.get("env", {}).items()},
        }
        result = self.harness.run(key, ["bash", "-e", "-c", step["run"]], env, cwd=self.temp)
        result.outputs = outputs_of(github_output)
        self.pin_path = [
            *(line for line in github_path.read_text(encoding="utf-8").splitlines() if line),
            *self.pin_path,
        ]
        return result

    def _install_pin(self, key: str, step: dict[str, Any]) -> None:
        if self.options.pins is None:
            if key == "gate_pin":
                self.record.steps[key] = Step(key, 0, "", "", {"command": " ".join(PACKAGE)})
            return
        self.record.steps[key] = self._bash(key, step)

    def _upload(self, step: dict[str, Any]) -> None:
        settings = step["with"]
        name = self._substitute(settings["name"])
        path = Path(self._substitute(settings["path"]))
        files = (
            {item.name: item.read_bytes() for item in sorted(path.glob("*")) if item.is_file()}
            if path.is_dir()
            else ({path.name: path.read_bytes()} if path.is_file() else {})
        )
        if files:
            self.harness.github.upload_artifact(self.record.run, name, files)
        elif settings["if-no-files-found"] == "error":
            self.record.steps[f"upload:{name}"] = Step(name, 1, "", "no files were found", {})

    def _substitute(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expression = match.group(1)
            if expression == "runner.temp":
                return str(self.temp)
            return self._expression(match.group(0))

        return EXPRESSION.sub(replace, str(text))

    def _before(self, key: str) -> None:
        if key == "finalize":
            self.record.loop_summary = self.record.step_summary()
            if self.options.tamper_attempts is not None:
                self.options.tamper_attempts(self.attempts_dir)
        if key == "publish" and self.options.before_guard is not None:
            self.options.before_guard()

    def _execute(self, step: dict[str, Any]) -> None:
        name = step["name"]
        if name in ENVIRONMENT_STEPS:
            if self.options.pins is not None and name == TRUSTED_CHECKOUT:
                self._clone("trusted")
            if self.options.pins is not None and name == BOOTSTRAP_CHECKOUT:
                target = self.temp / "theseus-bootstrap" / "src" / "theseus_reviewer"
                target.mkdir(parents=True)
                shutil.copy(self.options.pins.bootstrap, target / "bootstrap.py")
            return
        if not self._runs(step):
            return
        if name == HEAD_CHECKOUT:
            self._checkout_head()
        elif name == GATE_CHECKOUT:
            if self.options.before_gate is not None:
                self.options.before_gate()
            self._clone("gate")
        elif name == MINT_STEP:
            self._mint()
        elif name == REVIEWER_PIN:
            self._install_pin("reviewer_pin", step)
        elif name == GATE_PIN:
            self._install_pin("gate_pin", step)
            if step.get("continue-on-error"):
                self.waived.add("gate_pin")
        elif name == APP_REFUSAL:
            scoped = {key: self._expression(value) for key, value in step.get("env", {}).items()}
            command = ["bash", "-e", "-c", step["run"]]
            self.record.steps["app_refusal"] = self.harness.run(
                "app_refusal", command, {"PATH": self.env["PATH"], **scoped}, cwd=self.temp
            )
        elif name == GATE_FAILURE:
            self.record.steps["gate_failure"] = Step("gate_failure", 1, "", "oracle-gate was not posted", {})
        elif str(step.get("uses", "")).startswith("actions/upload-artifact"):
            self._upload(step)
        else:
            match = DRIVE_PHASE.search(step["run"])
            assert match is not None, f"workflow step is not modelled by the runner: {name}"
            phase = match.group("phase")
            self._before(step.get("id", phase))
            self._drive(step, phase, self.job_env[match.group("policy")])
            if phase == "harness" and self.options.swap_after_install:
                self.harness.swap_omp()

    def run(self) -> None:
        (self.temp / "trusted").mkdir(exist_ok=True)
        for step in review_steps():
            self._execute(step)
        refused = self.record.steps.get("app_refusal")
        conclusion = "failure" if refused is not None and refused.code != 0 else "success"
        self.harness.github.complete_run(self.record.run, conclusion)
