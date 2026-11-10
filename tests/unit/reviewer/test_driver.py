from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import cli, driver, github, isolation, omp_pin, protocol
from theseus_reviewer.driver import CiEnvironment, GitHubSink
from theseus_reviewer.policy import load_policy

from .conftest import POLICY_PATH
from .test_cli_plan import TWO_FILE_DIFF, _record

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
REPO = "owner/repo"


def _environment(tmp_path: Path, **overrides: str) -> CiEnvironment:
    environ = {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_REPOSITORY": REPO,
        "PR_NUMBER": "7",
        "GH_TOKEN": "app-token",
        "JOB_TOKEN": "job-token",
        "HOME": str(tmp_path / "home"),
        **overrides,
    }
    policy = tmp_path / "policy.yml"
    shutil.copy(POLICY_PATH, policy)
    return CiEnvironment.from_environ(environ, policy)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _final(root: Path, *, comment: bool = False, review: bool = False, state: bool = True) -> None:
    _write(root / "status.json", {"state": "success", "context": "review-verdict"})
    if comment:
        _write(root / "comment.json", {"body": "comment"})
    if review:
        _write(root / "review.json", {"body": "review", "comments": []})
    if state:
        _write(root / "state" / "state.json", {"head_sha": HEAD, "updated_at": "2026-10-28T12:00:00+00:00"})
        _write(root / "attempt" / "state.json", {"head_sha": HEAD})


def _plan(layout: driver.Layout, **fields: Any) -> None:
    _write(
        layout.plan_file,
        {"pr": 7, "head_sha": HEAD, "decision": "review", "created_at": "now", **fields},
    )


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "outputs.txt"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return target


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.refuse = ""
        self.minimize_failures: list[str] = []
        self.minimize_error: Exception | None = None

    def _record(self, kind: str, repo: str, target: Any) -> None:
        if self.refuse == kind:
            raise github.GitHubError(f"the {kind} was refused")
        self.calls.append((kind, repo, target))

    def status(self, repo: str, head: str, payload: Path) -> None:
        self._record("status", repo, head)

    def comment(self, repo: str, pr: int, payload: Path) -> None:
        self._record("comment", repo, pr)

    def review(self, repo: str, pr: int, payload: Path) -> None:
        self._record("review", repo, pr)

    def dispatch_gate(self, repo: str, pr: int, head: str) -> None:
        self._record("dispatch", repo, head)

    def gate(self, repo: str, pr: int, head: str) -> None:
        self._record("gate", repo, head)

    def minimize_superseded(self, repo: str, pr: int, head: str, bot_login: str) -> list[str]:
        self._record("minimize", repo, (pr, head, bot_login))
        if self.minimize_error is not None:
            raise self.minimize_error
        return self.minimize_failures


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> RecordingSink:
    recording = RecordingSink()
    monkeypatch.setattr(driver, "GitHubSink", lambda app_token, job_token, gated_base, policy: recording)
    return recording


def test_every_phase_is_a_drive_subcommand() -> None:
    parser = cli._parser()
    for name in driver.PHASES:
        parsed = parser.parse_args(["drive", "--policy", str(POLICY_PATH), name])
        assert parsed.phase == name and parsed.handler is driver.command_drive


def test_the_ci_environment_derives_its_layout_and_scan_roots_from_the_runner(tmp_path: Path) -> None:
    environment = _environment(tmp_path, TMPDIR="/custom/scratch")

    assert environment.layout.plan == tmp_path / "review-plan"
    assert environment.layout.workspace == tmp_path / "workspace"
    assert environment.layout.classification == tmp_path / "review-classification" / "classification.json"
    assert environment.layout.final == tmp_path / "review-final"
    assert Path("/custom/scratch") in environment.scan_roots
    assert tmp_path / "trusted" in environment.scan_roots and tmp_path / "workspace" in environment.scan_roots


def test_the_scan_phase_fails_when_a_secret_value_is_on_disk_and_passes_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    environment = replace(_environment(tmp_path), scan_roots=(root,))
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-secret-value")

    assert driver.scan(environment, argparse.Namespace()) == 0

    (root / "leak.txt").write_text("key=typesafe-secret-value", encoding="utf-8")
    assert driver.scan(environment, argparse.Namespace()) == 1


def test_the_scan_phase_also_catches_the_classifier_fallback_key_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    environment = replace(_environment(tmp_path), scan_roots=(root,))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")

    assert driver.scan(environment, argparse.Namespace()) == 0

    (root / "leak.txt").write_text("key=openai-secret-value", encoding="utf-8")
    assert driver.scan(environment, argparse.Namespace()) == 1


def test_the_isolation_phase_returns_the_adapter_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolation, "main", lambda: 1)
    assert driver.isolate(_environment(tmp_path), argparse.Namespace()) == 1
    monkeypatch.setattr(isolation, "main", lambda: 0)
    assert driver.isolate(_environment(tmp_path), argparse.Namespace()) == 0


def test_the_harness_phase_records_the_installed_binary_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    environment = _environment(tmp_path)
    environment.policy.write_text("omp: pinned", encoding="utf-8")
    binary = tmp_path / "omp"
    binary.write_text("#!/bin/sh\necho 18.12.1\n", encoding="utf-8")
    binary.chmod(0o755)
    env_file = tmp_path / "github-env.txt"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.setattr(omp_pin, "pin_from_text", lambda text: text)
    monkeypatch.setattr(omp_pin, "install", lambda pin: binary)

    assert driver.harness(environment, argparse.Namespace()) == 0

    assert env_file.read_text(encoding="utf-8") == f"REVIEW_OMP_BINARY={binary}\n"
    assert "state=installed" in outputs.read_text(encoding="utf-8")
    assert "18.12.1" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_the_harness_phase_reports_a_failed_install_without_failing_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _environment(tmp_path)
    environment.policy.write_text("omp: pinned", encoding="utf-8")

    def refuse(pin: object) -> Path:
        raise omp_pin.PinError("the omp download failed")

    monkeypatch.setattr(omp_pin, "pin_from_text", lambda text: text)
    monkeypatch.setattr(omp_pin, "install", refuse)

    assert driver.harness(environment, argparse.Namespace()) == 0

    assert "state=failed" in outputs.read_text(encoding="utf-8")
    assert "the pinned omp build could not be installed" in capsys.readouterr().out


def test_the_phases_are_exactly_the_cutover_set_and_none_accepts_shadow() -> None:
    assert set(driver.PHASES) == {
        "plan",
        "prepare",
        "harness",
        "scan",
        "isolation",
        "attempts",
        "recover",
        "finalize",
        "publish",
        "dispatch",
        "health",
        "sweep",
    }
    parser = cli._parser()
    for name in driver.PHASES:
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", name, "--shadow"])
    for removed in ("compare", "shadow"):
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", removed])


def test_prepare_classifies_only_a_plan_that_will_be_reviewed_and_survives_a_classifier_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    environment = _environment(tmp_path)
    selected: list[int] = []

    def plan(args: argparse.Namespace) -> int:
        _write(Path(args.out) / cli.PLAN_FILE, {"decision": environment_decision[0]})
        return 0

    def select(args: argparse.Namespace) -> int:
        selected.append((args.slot, args.classify_only))
        raise RuntimeError("the classifier crashed")

    environment_decision = ["success"]
    monkeypatch.setattr(cli, "command_plan", plan)
    monkeypatch.setattr(cli, "command_select", select)

    assert driver.prepare(environment, argparse.Namespace()) == 0
    assert selected == []

    environment_decision[0] = "review"
    assert driver.prepare(environment, argparse.Namespace()) == 0
    assert selected == [(1, True)]
    assert "classification failed: RuntimeError" in (tmp_path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", ["posts", "skipped", "guard_failed"])
def test_publish_posts_status_comment_review_then_the_gate_and_never_dispatches_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    scenario: str,
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    _final(environment.layout.final, comment=True, review=True)
    decisions = {"posts": (0, False), "skipped": (0, True), "guard_failed": (2, False)}
    monkeypatch.setattr(cli, "guard_decision", lambda plan: decisions[scenario])

    code = driver.publish(environment, argparse.Namespace())

    written = outputs.read_text(encoding="utf-8")
    assert "dispatch" not in [item[0] for item in sink.calls]
    if scenario == "posts":
        assert code == 0
        assert [item[0] for item in sink.calls] == ["comment", "review", "status", "gate"]
        assert sink.calls[-2][1:] == (REPO, HEAD) and sink.calls[-1][1:] == (REPO, HEAD)
        assert "skip=false" in written and "state_recorded=true" in written
    elif scenario == "skipped":
        assert code == 0 and sink.calls == []
        assert "skip=true" in written and "state_recorded" not in written
    else:
        assert code == 2 and sink.calls == []
        assert "skip=" not in written and "state_recorded" not in written


RECORDED_REVIEW = {
    "body": "Decision.\n\n"
    + protocol.encode_marker({k: v for k, v in _record(TWO_FILE_DIFF, "SHIP").items() if k != "patch_map"}),
    "comments": [],
}


def _publish_with_minimizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sink: RecordingSink,
    *,
    flag: bool,
    review: dict[str, Any] | None = RECORDED_REVIEW,
) -> tuple[int, list[str], str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    environment = _environment(tmp_path)
    if flag:
        environment.policy.write_text(
            environment.policy.read_text(encoding="utf-8") + "\nminimize_superseded: true\n", encoding="utf-8"
        )
    _plan(environment.layout, bot_login="acme-review")
    _final(environment.layout.final, comment=True, review=review is not None)
    if review is not None:
        _write(environment.layout.final / "review.json", review)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    code = driver.publish(environment, argparse.Namespace())
    return code, [item[0] for item in sink.calls], Path(os.environ["GITHUB_OUTPUT"]).read_text(encoding="utf-8")


def test_publish_minimises_after_the_gate_when_the_flag_is_on_and_a_record_was_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    code, kinds, _ = _publish_with_minimizing(tmp_path, monkeypatch, sink, flag=True)

    assert code == 0
    assert kinds == ["comment", "review", "status", "gate", "minimize"]
    assert sink.calls[-1] == ("minimize", REPO, (7, HEAD, "acme-review"))


def test_publish_minimises_nothing_when_the_flag_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    code, kinds, _ = _publish_with_minimizing(tmp_path, monkeypatch, sink, flag=False)

    assert code == 0 and "minimize" not in kinds


@pytest.mark.parametrize(
    "review",
    [None, {"body": "### review-verdict: pending\n\nReviewers not available.", "comments": []}],
    ids=["no-review-posted", "not-reviewed-post"],
)
def test_publish_minimises_nothing_unless_the_posted_review_carries_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink, review: dict[str, Any] | None
) -> None:
    code, kinds, _ = _publish_with_minimizing(tmp_path, monkeypatch, sink, flag=True, review=review)

    assert code == 0 and "minimize" not in kinds


@pytest.mark.parametrize(
    "failure",
    [
        github.GitHubError("Resource not accessible by integration"),
        RuntimeError("boom"),
        ["PRR_1: GitHub GraphQL returned errors"],
    ],
    ids=["permission-error", "unexpected-error", "per-review-failure"],
)
def test_a_minimise_failure_changes_neither_the_exit_code_nor_what_was_posted_nor_the_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
    failure: Any,
) -> None:
    baseline = _publish_with_minimizing(tmp_path / "off", monkeypatch, sink, flag=False)
    sink.calls.clear()
    outputs.write_text("", encoding="utf-8")
    if isinstance(failure, Exception):
        sink.minimize_error = failure
    else:
        sink.minimize_failures = failure

    code, kinds, written = _publish_with_minimizing(tmp_path / "on", monkeypatch, sink, flag=True)

    assert (code, kinds[:-1], written) == baseline
    assert kinds[-1] == "minimize"
    assert "::warning::" in capsys.readouterr().err


def test_publish_still_succeeds_when_the_minimise_step_cannot_read_the_policy_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout, bot_login="acme-review")
    _final(environment.layout.final, comment=True, review=True)
    _write(environment.layout.final / "review.json", RECORDED_REVIEW)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    calls = iter([environment.policy])

    def policy_vanishes(path: Path) -> Any:
        if next(calls, None) is None:
            raise OSError("policy vanished")
        return load_policy(path)

    monkeypatch.setattr(driver, "load_policy", policy_vanishes)

    assert driver.publish(environment, argparse.Namespace()) == 0
    assert [item[0] for item in sink.calls] == ["comment", "review", "status", "gate"]


def test_publish_posts_only_the_status_and_gate_when_no_comment_or_review_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    _final(environment.layout.final, state=False)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))

    assert driver.publish(environment, argparse.Namespace()) == 0

    assert [item[0] for item in sink.calls] == ["status", "gate"]
    assert "state_recorded" not in outputs.read_text(encoding="utf-8")


def test_a_refused_status_leaves_the_findings_posted_and_the_gate_unposted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    _final(environment.layout.final, comment=True, review=True)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    sink.refuse = "status"

    assert driver.publish(environment, argparse.Namespace()) == 1

    assert [item[0] for item in sink.calls] == ["comment", "review"]
    written = outputs.read_text(encoding="utf-8")
    assert "state_recorded" not in written
    assert "the status was refused" in capsys.readouterr().err


def test_a_refused_gate_post_leaves_the_published_result_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    _final(environment.layout.final, comment=True, review=True)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    sink.refuse = "gate"

    assert driver.publish(environment, argparse.Namespace()) == 0

    assert [item[0] for item in sink.calls] == ["comment", "review", "status"]
    written = outputs.read_text(encoding="utf-8")
    assert "state_recorded=true" in written
    assert "gate_failed=true" in written
    assert "::warning::review-gate was not posted from this run: the gate was refused" in capsys.readouterr().err


def test_a_refused_review_leaves_no_verdict_status_and_no_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    _final(environment.layout.final, comment=True, review=True)
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    sink.refuse = "review"

    assert driver.publish(environment, argparse.Namespace()) == 1

    assert [item[0] for item in sink.calls] == ["comment"]
    assert "state_recorded" not in outputs.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", [{"GH_TOKEN": ""}, {"JOB_TOKEN": ""}, {"GH_TOKEN": "", "JOB_TOKEN": ""}])
def test_publish_fails_before_the_guard_and_any_post_when_a_token_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
    missing: dict[str, str],
) -> None:
    environment = _environment(tmp_path, **missing)
    _plan(environment.layout)
    _final(environment.layout.final, comment=True, review=True)
    guarded: list[Any] = []
    monkeypatch.setattr(cli, "guard_decision", lambda plan: guarded.append(plan) or (0, False))

    assert driver.publish(environment, argparse.Namespace()) == 1

    assert sink.calls == [] and guarded == []
    written = outputs.read_text(encoding="utf-8")
    assert "skip=" not in written and "state_recorded" not in written
    assert "is empty" in capsys.readouterr().err


@pytest.mark.parametrize(("base", "dispatched"), [("develop", True), ("release", False)])
def test_the_dispatch_phase_dispatches_the_gate_only_for_a_develop_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str, dispatched: bool
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(github, "pull_base_ref", lambda repo, pr, token: base)
    monkeypatch.setattr(github, "dispatch_gate", lambda repo, pr, head, token, base: calls.append((token, head)))

    assert driver.dispatch(environment, argparse.Namespace()) == 0

    assert calls == ([("job-token", HEAD)] if dispatched else [])


def test_the_dispatch_phase_reports_a_github_error_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _environment(tmp_path)
    _plan(environment.layout)

    def refuse(repo: str, pr: int, token: str) -> str:
        raise github.GitHubError("the base lookup was refused")

    monkeypatch.setattr(github, "pull_base_ref", refuse)

    assert driver.dispatch(environment, argparse.Namespace()) == 1

    assert "the base lookup was refused" in capsys.readouterr().err


@pytest.mark.parametrize("missing", [{"GH_TOKEN": ""}, {"JOB_TOKEN": ""}])
def test_the_dispatch_phase_fails_before_any_request_when_a_token_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: dict[str, str]
) -> None:
    environment = _environment(tmp_path, **missing)
    _plan(environment.layout)
    requests: list[str] = []
    monkeypatch.setattr(github, "pull_base_ref", lambda *args: requests.append("base") or "develop")
    monkeypatch.setattr(github, "dispatch_gate", lambda *args: requests.append("dispatch"))

    assert driver.dispatch(environment, argparse.Namespace()) == 1

    assert requests == []
    assert "is empty" in capsys.readouterr().err


def test_the_github_sink_posts_each_payload_with_the_app_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(github, "post_file", lambda path, payload, token: posted.append((path, token)))
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"state": "success", "context": "review-verdict", "description": "d"}), "utf-8")

    sink = GitHubSink("app-token", "job-token", "develop", POLICY_PATH)
    sink.status(REPO, HEAD, payload)
    sink.comment(REPO, 7, payload)
    sink.review(REPO, 7, payload)

    assert posted == [
        (f"repos/{REPO}/statuses/{HEAD}", "app-token"),
        (f"repos/{REPO}/issues/7/comments", "app-token"),
        (f"repos/{REPO}/pulls/7/reviews", "app-token"),
    ]


def test_the_github_sink_posts_the_gate_with_the_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, int, str, str]] = []
    monkeypatch.setattr(
        github, "post_gate", lambda repo, pr, head, token, policy: posted.append((repo, pr, head, token))
    )

    GitHubSink("app-token", "job-token", "develop", POLICY_PATH).gate(REPO, 7, HEAD)

    assert posted == [(REPO, 7, HEAD, "app-token")]


def test_the_harness_phase_does_not_swallow_a_missing_yaml_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    environment = _environment(tmp_path)
    environment.policy.write_text("omp: pinned", encoding="utf-8")

    def missing_yaml(text: str) -> object:
        raise ImportError("No module named 'yaml'")

    monkeypatch.setattr(omp_pin, "pin_from_text", missing_yaml)

    with pytest.raises(ImportError, match="yaml"):
        driver.harness(environment, argparse.Namespace())

    assert outputs.read_text(encoding="utf-8") == ""
