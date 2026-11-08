from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import CONSUMER_ALPHA

from theseus_reviewer import bases, cli, driver, gate, github
from theseus_reviewer._vendor import yaml
from theseus_reviewer.policy import PolicyError, parse_policy
from theseus_reviewer.review_check import PASS, WAITING

pytestmark = [pytest.mark.unit, pytest.mark.fast]

POLICY_REL = ".github/review.yml"
HEAD = "c" * 40
RELEASE = "release/0.5.x"
BASES_SCRIPT = Path(bases.__file__)


def _consumer_root(tmp_path: Path, name: str, gated_base: str, attester: str) -> Path:
    root = tmp_path / name
    shutil.copytree(CONSUMER_ALPHA, root)
    policy = root / POLICY_REL
    text = policy.read_text(encoding="utf-8")
    text = text.replace("  gated_base: develop\n", gated_base).replace("    - joeharris76\n", f"    - {attester}\n")
    policy.write_text(text, encoding="utf-8")
    return root


MULTI = "  gated_base:\n    - develop\n    - release/*\n"


@pytest.fixture
def develop_root(tmp_path: Path) -> Path:
    return _consumer_root(tmp_path, "develop", MULTI, "develop-owner")


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    return _consumer_root(tmp_path, "release", MULTI, "release-owner")


def _stub_pull(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    pull = {
        "base": {"ref": base, "sha": "e" * 40},
        "head": {"sha": HEAD, "repo": {"full_name": "acme/r"}},
        "changed_files": 1,
    }
    monkeypatch.setattr(gate, "read_pull", lambda repo, pr, token: (pull, base, HEAD))
    monkeypatch.setattr(gate, "fetch_files", lambda *a: [{"filename": "src/engine/join.py"}])
    monkeypatch.setattr(gate, "fetch_statuses", lambda *a: [])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda *a: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda *a: [])
    monkeypatch.setattr(gate, "annotate_receipt", lambda *a: None)
    monkeypatch.setattr(gate, "fetch_bump_refusals", lambda *a, **k: [])
    monkeypatch.setattr(gate, "fetch_promotion_refusals", lambda *a, **k: (False, []))


def _attestation(login: str) -> dict[str, Any]:
    return {
        "login": login,
        "user_type": "User",
        "body": f"Stand-in review: APPROVE {HEAD}",
        "created_at": "2026-11-01T11:00:00Z",
        "updated_at": "2026-11-01T11:00:00Z",
    }


@pytest.mark.parametrize(
    ("patterns", "branch", "expected"),
    [
        (("develop",), "develop", True),
        (("develop",), "develop2", False),
        (("develop",), "release/0.5.x", False),
        (("develop", "release/*"), "release/0.5.x", True),
        (("develop", "release/*"), "release/0.5.x/hotfix", False),
        (("develop", "release/*"), "release/", False),
        (("develop", "release/*"), "release", False),
        (("develop", "release/*"), "releases/0.5.x", False),
        (("develop", "release/*"), "feature/release/0.5.x", False),
        (("develop", "release/**"), "release/0.5.x/hotfix", True),
        (("develop", "release/**"), "release/0.5.x", True),
        (("develop", "release/**"), "release//x", False),
        (("develop", "release/*"), "main", False),
    ],
)
def test_a_branch_is_gated_only_when_a_listed_name_or_prefix_pattern_matches(
    patterns: tuple[str, ...], branch: str, expected: bool
) -> None:
    assert bases.matches(patterns, branch) is expected


@pytest.mark.parametrize(
    "patterns",
    [
        ["*"],
        ["**"],
        ["develop", "*"],
        ["develop", "**"],
        ["develop", "release*"],
        ["develop", "rel*/x"],
        ["develop", "release/*/x"],
        ["develop", "release/*.x"],
        ["release/*"],
        ["develop", "develop"],
        ["-develop"],
        ["has space"],
        [],
    ],
)
def test_a_pattern_list_that_could_gate_every_branch_or_that_has_no_literal_first_base_is_refused(
    patterns: list[str],
) -> None:
    with pytest.raises(bases.BasePatternError):
        bases.parse_bases(patterns)


def test_the_workflow_input_accepts_newline_and_comma_separated_lists() -> None:
    assert bases.split_bases("develop\nrelease/*") == ("develop", "release/*")
    assert bases.split_bases("develop, release/*") == ("develop", "release/*")
    assert bases.split_bases("main") == ("main",)


def test_a_single_string_gated_base_still_parses_and_a_list_parses_in_order(tmp_path: Path) -> None:
    raw = yaml.safe_load((CONSUMER_ALPHA / POLICY_REL).read_text(encoding="utf-8"))
    assert parse_policy(raw).consumer.gated_bases == ("develop",)
    raw["consumer"]["gated_base"] = ["develop", "release/*"]
    consumer = parse_policy(raw).consumer
    assert consumer.gated_bases == ("develop", "release/*")
    assert consumer.primary_base == "develop"
    assert consumer.is_gated("develop") and consumer.is_gated(RELEASE)
    assert not consumer.is_gated("main") and not consumer.is_gated("feature/x")


@pytest.mark.parametrize("value", [["release/*"], ["*"], [], [3], ["develop", "release/*/x"]])
def test_the_policy_refuses_an_unsafe_gated_base_list(value: list[Any]) -> None:
    raw = yaml.safe_load((CONSUMER_ALPHA / POLICY_REL).read_text(encoding="utf-8"))
    raw["consumer"]["gated_base"] = value
    with pytest.raises(PolicyError, match="consumer gated_base must be a branch name or a list of branch names"):
        parse_policy(raw)


def test_a_pull_request_into_a_release_branch_is_gated_with_that_branchs_own_policy(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(bases.TRUSTED_BASE_ENV, RELEASE)
    config = gate.load_gate_config(POLICY_REL, release_root)
    assert config.trusted_base == RELEASE
    assert config.binding.attesters == frozenset({"release-owner"})
    _stub_pull(monkeypatch, RELEASE)

    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [_attestation("release-owner")])
    head, status, message = gate.evaluate("acme/r", 7, "token", config)
    assert (head, status) == (HEAD, PASS)
    assert "release-owner" in message

    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [_attestation("develop-owner")])
    assert gate.evaluate("acme/r", 7, "token", config)[1] == WAITING


def test_develop_is_gated_exactly_as_before_when_the_policy_lists_release_branches_too(
    develop_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bases.TRUSTED_BASE_ENV, raising=False)
    config = gate.load_gate_config(POLICY_REL, develop_root)
    assert config.trusted_base == "develop"
    _stub_pull(monkeypatch, "develop")
    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [_attestation("develop-owner")])
    assert gate.evaluate("acme/r", 7, "token", config)[1] == PASS
    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [_attestation("release-owner")])
    assert gate.evaluate("acme/r", 7, "token", config)[1] == WAITING


@pytest.mark.parametrize("base", ["main", "feature/x", "release/0.5.x/hotfix", "hotfix/0.5.x"])
def test_a_pull_request_into_an_unlisted_base_is_refused(
    base: str, develop_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bases.TRUSTED_BASE_ENV, raising=False)
    config = gate.load_gate_config(POLICY_REL, develop_root)
    _stub_pull(monkeypatch, base)
    with pytest.raises(gate.UngatedBase, match="posted only for develop, release/\\*"):
        gate.evaluate("acme/r", 7, "token", config)
    assert not config.admits(base)


def test_a_policy_is_never_applied_to_a_pull_request_whose_own_base_supplied_a_different_policy(
    develop_root: Path, release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bases.TRUSTED_BASE_ENV, raising=False)
    from_develop = gate.load_gate_config(POLICY_REL, develop_root)
    _stub_pull(monkeypatch, RELEASE)
    with pytest.raises(gate.UngatedBase, match="policy was read from develop"):
        gate.evaluate("acme/r", 7, "token", from_develop)

    monkeypatch.setenv(bases.TRUSTED_BASE_ENV, RELEASE)
    from_release = gate.load_gate_config(POLICY_REL, release_root)
    _stub_pull(monkeypatch, "develop")
    with pytest.raises(gate.UngatedBase, match="policy was read from release/0.5.x"):
        gate.evaluate("acme/r", 7, "token", from_release)


def test_the_pending_status_is_posted_only_for_the_trusted_base(
    develop_root: Path, release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []
    monkeypatch.setattr(gate, "post_pending", lambda token, repo, head: posted.append(head))
    args = type("Args", (), {"repo": "acme/r", "pr": 7})()

    monkeypatch.setenv(bases.TRUSTED_BASE_ENV, RELEASE)
    config = gate.load_gate_config(POLICY_REL, release_root)
    _stub_pull(monkeypatch, RELEASE)
    gate.mark_pending(args, "token", config)
    assert posted == [HEAD]

    _stub_pull(monkeypatch, "develop")
    gate.mark_pending(args, "token", config)
    _stub_pull(monkeypatch, "feature/x")
    gate.mark_pending(args, "token", config)
    assert posted == [HEAD]


def test_the_review_plan_guard_follows_the_trusted_base_and_refuses_unlisted_bases(release_root: Path) -> None:
    consumer = parse_policy(yaml.safe_load((release_root / POLICY_REL).read_text(encoding="utf-8"))).consumer
    event: dict[str, Any] = {}

    def pull(base: str) -> dict[str, Any]:
        return {"state": "open", "base": {"ref": base}, "draft": False, "head": {"sha": HEAD}}

    assert cli._pull_guard(pull(RELEASE), event, HEAD, consumer, RELEASE) is None
    assert cli._pull_guard(pull("develop"), event, HEAD, consumer, "develop") is None
    assert cli._pull_guard(pull("develop"), event, HEAD, consumer, RELEASE) == (
        f"the pull request does not target {RELEASE}"
    )
    assert cli._pull_guard(pull("feature/x"), event, HEAD, consumer, "feature/x") is not None
    assert cli._pull_guard(pull("main"), event, HEAD, consumer, "develop") == (
        "the pull request does not target develop"
    )


def test_the_sink_dispatches_the_gate_on_the_pull_requests_own_gated_base(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = parse_policy(yaml.safe_load((release_root / POLICY_REL).read_text(encoding="utf-8"))).consumer
    sink = driver.GitHubSink("app", "job", consumer, release_root / POLICY_REL)
    dispatched: list[tuple[int, str, str]] = []
    monkeypatch.setattr(
        github, "dispatch_gate", lambda repo, pr, head, token, base: dispatched.append((pr, head, base))
    )
    for base in (RELEASE, "develop", "feature/x"):
        monkeypatch.setattr(github, "pull_base_ref", lambda repo, pr, token, base=base: base)
        sink.dispatch_gate("acme/r", 7, HEAD)
    assert dispatched == [(7, HEAD, RELEASE), (7, HEAD, "develop")]


def test_the_review_sweep_covers_open_pull_requests_into_every_gated_base(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulls = [
        {
            "number": number,
            "draft": False,
            "base": {"ref": base},
            "head": {"sha": f"{number:040d}", "repo": {"full_name": "o/r"}},
        }
        for number, base in ((1, "develop"), (2, RELEASE), (3, "feature/x"), (4, "main"))
    ]
    listed: list[str] = []
    dispatched: list[tuple[int, str]] = []

    def get_paginated(path: str) -> Any:
        listed.append(path)
        return iter(pulls)

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("SURFACE_TOKEN", "token")
    monkeypatch.setattr(github, "verdict_runs", lambda repo, workflow, since: [])
    monkeypatch.setattr(github, "get_paginated", get_paginated)
    monkeypatch.setattr(cli, "_retry_reason", lambda *a, **k: "its retry is due")
    monkeypatch.setattr(
        github, "dispatch", lambda repo, workflow, pr, head, base, reason=None: dispatched.append((pr, base))
    )
    assert cli.command_sweep(argparse.Namespace(policy=str(release_root / POLICY_REL))) == 0
    assert listed == ["repos/o/r/pulls?state=open&per_page=100"]
    assert dispatched == [(1, "develop"), (2, RELEASE)]


def _resolve(patterns: str, ref: str, event: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(BASES_SCRIPT), "resolve"],
        env={"GATED_BASES": patterns, "CURRENT_REF": ref, "EVENT_NAME": event},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("ref", "event", "gated", "base"),
    [
        ("refs/heads/develop", "pull_request_target", "true", "develop"),
        ("refs/heads/release/0.5.x", "pull_request_target", "true", "release/0.5.x"),
        ("refs/heads/release/0.5.x", "workflow_dispatch", "true", "release/0.5.x"),
        ("refs/heads/feature/x", "pull_request_target", "false", ""),
        ("refs/heads/main", "schedule", "false", ""),
        ("refs/pull/7/merge", "pull_request_target", "false", ""),
    ],
)
def test_the_guard_marks_a_ref_gated_only_when_the_listed_bases_match_it(
    ref: str, event: str, gated: str, base: str
) -> None:
    done = _resolve("develop\nrelease/*", ref, event)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f"gated={gated}", f"base={base}"]


@pytest.mark.parametrize("ref", ["refs/heads/feature/x", "refs/heads/main", "refs/tags/v1", "refs/heads/release"])
def test_a_dispatch_from_an_unlisted_ref_is_refused(ref: str) -> None:
    done = _resolve("develop\nrelease/*", ref, "workflow_dispatch")
    assert done.returncode == 2
    assert "dispatch runs only from a gated base" in done.stderr


def test_the_guard_refuses_an_unsafe_base_list() -> None:
    for patterns in ("", "*", "release/*", "develop,**"):
        assert _resolve(patterns, "refs/heads/develop", "push").returncode == 2


@pytest.mark.parametrize(
    ("branch", "code"),
    [("develop", 0), ("release/0.5.x", 0), ("feature/x", 1), ("release/0.5.x/hotfix", 1)],
)
def test_the_sweep_and_bridge_check_each_pull_request_base_with_the_same_matcher(branch: str, code: int) -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(BASES_SCRIPT), "check", "develop\nrelease/*", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == code
