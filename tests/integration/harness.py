from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from theseus_reviewer import omp_pin

from .fake_github import APP_TOKEN, GH_HOST, JOB_TOKEN, OWNER_TOKEN, FakeGitHub
from .fake_jev import FakeJev

REPO_ROOT = Path(__file__).resolve().parents[2]
ALPHA_ROOT = REPO_ROOT / "tests" / "fixtures" / "consumer" / "alpha"
SLUG = "BenchBox-dev/BenchBox"
CHECKER = "benchbox/core/equivalence/checker.py"
RECORDED_STREAM = REPO_ROOT / "tests" / "fixtures" / "oracle_reviewers" / "omp_multi_turn_review.jsonl"
POLICY = ".github/oracle-reviewers.yml"
WORKFLOW = ".github/workflows/oracle-verdict.yml"
PINNED_OMP = omp_pin.pin_from_text((ALPHA_ROOT / POLICY).read_text(encoding="utf-8")).version
TRUSTED_FILES = (
    ".github/oracle-omp-config.yml",
    WORKFLOW,
    ".github/soundness-paths.txt",
    ".github/inert-paths.txt",
)
PACKAGE = [sys.executable, "-m", "theseus_reviewer"]
GATEWAY_TOKEN = "gwtok-" + "Q7mZ4kLp" * 5
HELD_SECRET = "oauth-secret-" + "W3xT9vNb" * 4
JEV_KEY = "jevk-" + "T5nB8wXc" * 4
OAUTH_CLIENT_ID = "oauth-client-" + "H6dK2sRq" * 3
APP_PRIVATE_KEY = "app-private-key-" + "M8vC4zJf" * 6
SHA256_LINE = re.compile(r"sha256: [0-9a-f]{64}")
URL_LINE = re.compile(r"url: https://\S+")
UNREACHABLE_BUILD = "https://127.0.0.1:9/omp"
STEP_TIMEOUT = 120
COMMIT_AGE = timedelta(hours=2)


def git(path: Path, *args: str) -> str:
    moment = (datetime.now(UTC) - COMMIT_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment},
    )
    return result.stdout.strip()


def ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def outputs(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    return values


@dataclass
class Step:
    name: str
    code: int
    stdout: str
    stderr: str
    outputs: dict[str, str]


@dataclass
class ReviewRun:
    run: dict[str, Any]
    temp: Path
    steps: dict[str, Step] = field(default_factory=dict)
    step_env: dict[str, dict[str, str]] = field(default_factory=dict)
    exported_env: dict[str, str] = field(default_factory=dict)
    loop_summary: str = ""

    @property
    def plan(self) -> dict[str, Any]:
        path = self.temp / "oracle-plan" / "plan.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def attempts(self) -> list[dict[str, Any]]:
        folder = self.temp / "oracle-attempts"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("attempt-*.json"))]

    def step_summary(self) -> str:
        path = self.temp / "summary.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def captures(self) -> list[dict[str, Any]]:
        folder = self.temp / "oracle-attempts" / "captures"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("capture-*.json"))]


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.home = root / "home"
        self.bin = root / "bin"
        for folder in (self.home, self.bin, root / "gh-config"):
            folder.mkdir(parents=True)
        self.trusted = root / "trusted"
        self._install_omp()
        self.policy_file = self.trusted / POLICY
        self.policy_file.parent.mkdir(parents=True)
        self.policy_file.write_text(self._pinned_policy(), encoding="utf-8")
        for path in TRUSTED_FILES:
            target = self.trusted / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ALPHA_ROOT / path).read_text(encoding="utf-8"), encoding="utf-8")
        self._seed()
        self._publish_remote()
        self.github = FakeGitHub(SLUG, self.repo).start()
        self.jev = FakeJev().start()

    @property
    def remote_root(self) -> Path:
        return self.root / "remote"

    def _publish_remote(self) -> None:
        link = self.remote_root / f"{SLUG}.git"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.repo / ".git", target_is_directory=True)
        git(self.repo, "config", "uploadpack.allowFilter", "true")
        git(self.repo, "config", "uploadpack.allowAnySHA1InWant", "true")

    @property
    def omp_cache(self) -> Path:
        return self.root / "omp-cache"

    def close(self) -> None:
        self.github.stop()
        self.jev.stop()

    def _pinned_policy(self) -> str:
        text = (ALPHA_ROOT / POLICY).read_text(encoding="utf-8")
        text = SHA256_LINE.sub(f"sha256: {omp_pin.sha256_of(self.bin / 'omp')}", text)
        return URL_LINE.sub(f"url: {UNREACHABLE_BUILD}", text)

    def swap_omp(self) -> None:
        binary = self.bin / "omp"
        binary.write_text(binary.read_text(encoding="utf-8") + "# swapped\n", encoding="utf-8")

    def _seed(self) -> None:
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch=develop")
        trusted = {path: (ALPHA_ROOT / path).read_text(encoding="utf-8") for path in TRUSTED_FILES}
        trusted[POLICY] = self.policy_file.read_text(encoding="utf-8")
        self.write({"README.md": "seed\n", CHECKER: "def checker(a, b):\n    return a == b\n", **trusted}, "seed")

    def write(self, files: dict[str, str | bytes], message: str) -> str:
        for relative, text in files.items():
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                target.write_bytes(text)
            else:
                target.write_text(text, encoding="utf-8")
            git(self.repo, "add", relative)
        git(self.repo, "commit", "--quiet", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def branch(self, name: str, files: dict[str, str | bytes], message: str, base: str = "develop") -> str:
        git(self.repo, "switch", "--quiet", "-C", name, base)
        sha = self.write(files, message)
        git(self.repo, "switch", "--quiet", "develop")
        return sha

    def rename_branch(self, name: str, old: str, new: str, message: str) -> str:
        git(self.repo, "switch", "--quiet", "-C", name, "develop")
        (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "mv", old, new)
        git(self.repo, "commit", "--quiet", "-m", message)
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "--quiet", "develop")
        return sha

    def push(self, name: str, files: dict[str, str | bytes], message: str) -> str:
        git(self.repo, "switch", "--quiet", name)
        sha = self.write(files, message)
        git(self.repo, "switch", "--quiet", "develop")
        return sha

    def _install_omp(self) -> None:
        source = (
            Path(__file__)
            .with_name("fake_omp.py")
            .read_text(encoding="utf-8")
            .replace("RENDEZVOUS_SECONDS = 20\n", f"RENDEZVOUS_SECONDS = {STEP_TIMEOUT // 2}\n", 1)
        )
        target = self.bin / "omp"
        target.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        cached = omp_pin.binary_path(omp_pin.Pin(PINNED_OMP, {}), omp_pin.current_platform(), self.omp_cache)
        cached.parent.mkdir(parents=True)
        cached.symlink_to(target)
        security = self.bin / "security"
        security.write_text("#!/bin/sh\nexit 44\n", encoding="utf-8")
        security.chmod(security.stat().st_mode | stat.S_IXUSR)
        self.script_omp([])

    def script_omp(
        self,
        replies: list[dict[str, Any]],
        leak_env: str | None = None,
        ancestor_probe: dict[str, str] | None = None,
    ) -> None:
        script = {
            "version": PINNED_OMP,
            "stream": str(RECORDED_STREAM),
            "replies": replies,
            "leak_env": leak_env,
            "ancestor_probe": {"stop": os.getpid(), "values": ancestor_probe} if ancestor_probe else None,
        }
        (self.bin / "script.json").write_text(json.dumps(script), encoding="utf-8")
        (self.bin / "calls.json").unlink(missing_ok=True)
        (self.bin / "ancestors.json").unlink(missing_ok=True)
        shutil.rmtree(self.bin / "arrivals", ignore_errors=True)

    def omp_log(self) -> list[dict[str, Any]]:
        path = self.bin / "calls.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def omp_ancestors(self) -> list[dict[str, Any]]:
        path = self.bin / "ancestors.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def omp_calls(self) -> int:
        return len(self.omp_log())

    def omp_prompts(self) -> list[str]:
        return [call["prompt"] for call in self.omp_log()]

    def base_env(self, temp: Path | None = None) -> dict[str, str]:
        proxy = f"http://127.0.0.1:{self.github.port}"
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            omp_pin.BINARY_ENV: str(self.bin / "omp"),
            omp_pin.CACHE_ENV: str(self.omp_cache),
            "LANG": "C.UTF-8",
            "GH_CONFIG_DIR": str(self.root / "gh-config"),
            "GH_HOST": GH_HOST,
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PROMPT_DISABLED": "1",
            "HTTP_PROXY": proxy,
            "http_proxy": proxy,
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
            "GITHUB_REPOSITORY": SLUG,
            "GITHUB_SERVER_URL": f"https://{GH_HOST}",
            "GITHUB_API_URL": self.github.url,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{self.remote_root}/.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://{GH_HOST}/",
        }
        if temp is not None:
            env["RUNNER_TEMP"] = str(temp)
            env["GITHUB_OUTPUT"] = str(temp / "output.txt")
            env["GITHUB_STEP_SUMMARY"] = str(temp / "summary.md")
        return env

    def run(
        self, name: str, argv: list[str], env: dict[str, str], cwd: Path | None = None, stdin: str | None = None
    ) -> Step:
        output = Path(env["GITHUB_OUTPUT"]) if "GITHUB_OUTPUT" in env else None
        if output is not None:
            output.write_text("", encoding="utf-8")
        result = subprocess.run(
            argv,
            cwd=cwd or self.trusted,
            env=env,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT,
            input=stdin,
            check=False,
        )
        return Step(name, result.returncode, result.stdout, result.stderr, outputs(output) if output else {})

    def cli(self, name: str, args: list[str], env: dict[str, str]) -> Step:
        return self.run(name, [*PACKAGE, *args], env)

    def gh(self, name: str, args: list[str], env: dict[str, str], token: str) -> Step:
        return self.run(name, ["gh", *args], {**env, "GH_TOKEN": token, "GH_ENTERPRISE_TOKEN": token})

    def review(
        self,
        pr: int,
        *,
        event_name: str = "pull_request_target",
        action: str = "synchronize",
        inputs: dict[str, str] | None = None,
        **options: Any,
    ) -> ReviewRun:
        from .runs import Options, WorkflowRunner

        pull = self.github.pulls[pr]
        self.github.refresh(pr)
        head = pull["head"]["sha"]
        trusted = event_name == "pull_request_target"
        run = self.github.add_run(
            "oracle-verdict.yml",
            event=event_name,
            pr=pr,
            head_sha=head if trusted else git(self.repo, "rev-parse", "develop"),
            title_head=head,
            head_branch=pull["head"]["ref"] if trusted else "develop",
            pull_requests=[
                {"number": pr, "base": {"ref": "develop"}, "head": {"ref": pull["head"]["ref"], "sha": head}}
            ]
            if trusted
            else [],
        )
        temp = self.root / f"run-{run['id']}"
        temp.mkdir()
        record = ReviewRun(run, temp)
        event: dict[str, Any] = {"action": action}
        if trusted:
            event["pull_request"] = {"number": pr, "head": {"sha": head}}
        elif event_name == "workflow_dispatch":
            event = {"inputs": {"pr": str(pr), "head": head, **(inputs or {})}}
        (temp / "event.json").write_text(json.dumps(event), encoding="utf-8")
        env = {
            **self.base_env(temp),
            "GITHUB_RUN_ID": str(run["id"]),
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(temp / "event.json"),
            "GITHUB_REF": "refs/heads/develop",
        }
        chosen = Options(**options)
        self.jev.classification = chosen.classification
        WorkflowRunner(self, pr, record, env, chosen).run()
        return record

    def gate(self, pr: int, head_sha: str | None = None, *, pending_first: bool = False) -> Step:
        env = {**self.base_env(), "GITHUB_TOKEN": APP_TOKEN, "GH_TOKEN": APP_TOKEN}
        args = [*PACKAGE, "gate", "--repo", SLUG, "--pr", str(pr), "--policy", POLICY, "--root", str(self.trusted)]
        if head_sha:
            args += ["--head-sha", head_sha]
        if pending_first:
            args.append("--pending-first")
        return self.run("gate", args, env)

    def sweep(self) -> Step:
        temp = self.root / f"sweep-{len(list(self.root.glob('sweep-*')))}"
        temp.mkdir()
        env = {
            **self.base_env(temp),
            "GH_TOKEN": JOB_TOKEN,
            "SURFACE_TOKEN": APP_TOKEN,
            "GITHUB_RUN_ID": "1",
            "GITHUB_EVENT_NAME": "schedule",
        }
        step = self.cli("sweep", ["drive", "--policy", POLICY, "sweep"], env)
        summary = Path(env["GITHUB_STEP_SUMMARY"])
        step.outputs["summary"] = summary.read_text(encoding="utf-8") if summary.is_file() else ""
        return step

    def local_review(
        self,
        branch: str,
        *,
        rebuttals: dict[str, str] | None = None,
        adjudicate: str | None = None,
        relative_path: bool = False,
        **options: Any,
    ) -> Step:
        argv, env, checkout = self._local_review_command(branch, **options)
        if rebuttals is not None:
            rebuttal_file = (checkout if relative_path else self.root) / "rebuttals.json"
            rebuttal_file.write_text(json.dumps(rebuttals), encoding="utf-8")
            argv = [*argv, "--rebuttals", rebuttal_file.name if relative_path else str(rebuttal_file)]
        if adjudicate is not None:
            argv = [*argv, "--adjudicate", adjudicate]
        self.record_out.unlink(missing_ok=True)
        return self.run("local-review", argv, {**env, "ORACLE_LOCAL_RECORD_OUT": str(self.record_out)}, cwd=checkout)

    @property
    def record_out(self) -> Path:
        return self.root / "record-out.txt"

    def local_records(self) -> list[dict[str, Any]]:
        paths = sorted((self.local_state / "records").glob("*.json"))
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def start_local_review(self, branch: str, **options: Any) -> subprocess.Popen[str]:
        argv, env, checkout = self._local_review_command(branch, **options)
        return subprocess.Popen(argv, cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish_local_review(self, process: subprocess.Popen[str]) -> Step:
        try:
            stdout, stderr = process.communicate(timeout=STEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return Step("local-review", process.returncode, stdout, stderr, {})

    def _local_review_command(
        self, branch: str, key: bool = True, env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str], Path]:
        checkout = self.root / f"local-{branch.replace('/', '-')}"
        if not checkout.exists():
            git(self.root, "clone", "--quiet", str(self.repo), str(checkout))
            git(checkout, "switch", "--quiet", branch)
            git(checkout, "branch", "--quiet", "-f", "develop", "origin/develop")
        values = {
            **self.base_env(),
            "ORACLE_GATEWAY_URL": self.github.url,
            "ORACLE_CLOUD_GATEWAY_TOKEN": GATEWAY_TOKEN,
            "GH_TOKEN": OWNER_TOKEN,
            "GH_ENTERPRISE_TOKEN": OWNER_TOKEN,
            "TYPESAFE_BASE_URL": self.jev.url,
            **({"TYPESAFE_API_KEY": JEV_KEY} if key else {}),
            **(env or {}),
        }
        argv = [*PACKAGE, "local-review", "--policy", POLICY, "--repo", SLUG]
        return [*argv, "--base-ref", "develop"], values, checkout

    @property
    def local_state(self) -> Path:
        return self.home / ".cache" / "theseus" / "local-oracle"

    def local_captures(self) -> list[dict[str, Any]]:
        paths = sorted((self.local_state / "captures").glob("*/capture-*.json"))
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]
