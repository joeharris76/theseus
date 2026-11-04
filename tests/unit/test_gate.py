from __future__ import annotations

import http.server
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import ALPHA_POLICY, CONSUMER_ALPHA
from theseus_reviewer import gate, review_check

pytestmark = [pytest.mark.unit, pytest.mark.fast]

POLICY = ALPHA_POLICY
POLICY_REL = ".github/oracle-reviewers.yml"
CONFIG = gate.load_gate_config(POLICY_REL, CONSUMER_ALPHA)
BINDING = CONFIG.binding
POLICY_ARGS = ["--policy", POLICY_REL, "--root", str(CONSUMER_ALPHA)]
BASE_ARGS = ["--repo", "o/r", "--pr", "7", *POLICY_ARGS]
HEAD = "a" * 40
AFTER_HEAD = "2026-10-01T13:00:00Z"
SOUNDNESS_FILES = ["benchbox/core/equivalence/checker.py", "docs/index.md"]
OTHER_FILES = ["docs/index.md", "notes.md"]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


RUN_UUID = "1" * 32


def _receipt_body(run_uuid: str = RUN_UUID) -> str:
    from theseus_reviewer import protocol

    record = {
        "v": protocol.MARKER_VERSION,
        "cycle": 1,
        "round": 1,
        "kind": protocol.FIRST,
        "decision": protocol.SHIP,
        "head_sha": HEAD,
        "base_ref": "develop",
        "reviewer": "large",
        "tier": "large",
        "strikes_after": 0,
        "open_defects": [],
        "next_id": 1,
        "patch_digest": "d" * 32,
        "carried": False,
        "summary": "",
        "run_uuid": run_uuid,
    }
    return f"### oracle-verdict: success for `{HEAD}`\n\n{protocol.encode_marker(record)}"


def _success_status(**over: Any) -> dict[str, Any]:
    status = {
        "id": 1,
        "context": "oracle-verdict",
        "state": "success",
        "description": f"large: SHIP [run {RUN_UUID}]",
        "login": "benchbox-oracle[bot]",
        "user_type": "Bot",
        "created_at": AFTER_HEAD,
    }
    return {**status, **over}


def _receipt(**over: Any) -> dict[str, Any]:
    review = {
        "login": "benchbox-oracle[bot]",
        "user_type": "Bot",
        "commit_id": HEAD,
        "state": "COMMENTED",
        "submitted_at": AFTER_HEAD,
        "body": _receipt_body(),
        "last_edited_at": None,
        "includes_created_edit": False,
    }
    return {**review, **over}


def _attestation(body: str | None = None) -> dict[str, Any]:
    return {
        "login": "joeharris76",
        "user_type": "User",
        "created_at": AFTER_HEAD,
        "updated_at": AFTER_HEAD,
        "body": body if body is not None else f"Stand-in oracle review: APPROVE {HEAD}",
    }


def _matcher() -> Any:
    classifier = CONFIG.policy.classifier
    return review_check.path_matcher(2, 2, CONFIG.matchers, classifier.exempt_paths, classifier.gate_paths)


def test_gate_reuses_the_checker_decision_and_helpers() -> None:
    assert gate.decide is review_check.decide
    assert gate.path_matcher is review_check.path_matcher
    assert gate.changed_paths is review_check.changed_paths
    assert (gate.PASS, gate.WAITING, gate.ERROR) == (
        review_check.PASS,
        review_check.WAITING,
        review_check.ERROR,
    )


def test_gate_config_carries_the_binding_and_matchers_of_the_policy() -> None:
    config = gate.load_gate_config(POLICY_REL, CONSUMER_ALPHA)
    assert config.binding == BINDING
    assert config.consumer.gated_base == "develop"
    assert config.policy_path == POLICY_REL
    assert config.matchers.is_soundness_path("benchbox/core/equivalence/checker.py")
    assert config.matchers.is_inert_path("docs/index.md")


def test_gate_config_follows_the_consumer_not_a_default(beta_root: Path) -> None:
    config = gate.load_gate_config(POLICY_REL, beta_root)
    assert config.binding.bot_login == "beta-oracle"
    assert config.binding.attesters == frozenset({"beta-owner", "beta-deputy"})
    assert config.binding.gated_base == "main"


def test_gate_config_fails_when_the_policy_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(gate.CheckError):
        gate.load_gate_config(POLICY_REL, tmp_path)


def test_gate_config_fails_when_a_manifest_is_missing(tmp_path: Path) -> None:
    github = tmp_path / ".github"
    github.mkdir()
    (github / "oracle-reviewers.yml").write_text(POLICY.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(gate.CheckError):
        gate.load_gate_config(POLICY_REL, tmp_path)


class GateApi:
    def __init__(self, head: str) -> None:
        self.head = head
        self.statuses: list[tuple[str, str]] = []

    def latest(self, sha: str) -> str | None:
        states = [state for posted, state in self.statuses if posted == sha]
        return states[-1] if states else None


@pytest.fixture
def gate_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[GateApi]:
    api = GateApi(HEAD)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            if self.path == "/graphql":
                reply: dict[str, Any] = {"data": {"repository": {"pullRequest": {"headRefOid": api.head}}}}
            else:
                api.statuses.append((self.path.rsplit("/", 1)[-1], body["state"]))
                reply = {}
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(gate, "API_ROOT", root)
    monkeypatch.setattr(review_check, "API_ROOT", root)
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _unloadable_root(tmp_path: Path, kind: str) -> Path:
    github = tmp_path / ".github"
    shutil.copytree(CONSUMER_ALPHA / ".github", github)
    if kind == "policy":
        (github / "oracle-reviewers.yml").write_text("promotion: [unterminated", encoding="utf-8")
    elif kind == "missing-policy":
        (github / "oracle-reviewers.yml").unlink()
    elif kind in SHAPE_BREAKS:
        with (github / "oracle-reviewers.yml").open("a", encoding="utf-8") as handle:
            handle.write(SHAPE_BREAKS[kind])
    else:
        (github / "soundness-paths.txt").unlink()
    return tmp_path


SHAPE_BREAKS = {"classifier-shape": "\nclassifier: [1]\n", "max-attempts-shape": "\nmax_attempts: [1]\n"}
UNLOADABLE = ["policy", "missing-policy", "soundness-manifest", *SHAPE_BREAKS]


def _unloadable_argv(root: Path, *extra: str) -> list[str]:
    return ["--repo", "o/r", "--pr", "7", "--policy", POLICY_REL, "--root", str(root), *extra]


@pytest.mark.parametrize("kind", UNLOADABLE)
def test_an_unloadable_config_posts_a_failure_over_an_earlier_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_api: GateApi, kind: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    gate_api.statuses.append((HEAD, "success"))
    assert gate.main(_unloadable_argv(_unloadable_root(tmp_path, kind))) == gate.ERROR
    assert gate_api.statuses == [(HEAD, "success"), (HEAD, "failure")]
    assert gate_api.latest(HEAD) == "failure"


@pytest.mark.parametrize("kind", UNLOADABLE)
def test_an_unloadable_config_with_the_owned_head_given_posts_a_failure_and_no_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_api: GateApi, kind: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    argv = _unloadable_argv(_unloadable_root(tmp_path, kind), "--head-sha", HEAD, "--pending-first")
    assert gate.main(argv) == gate.ERROR
    assert gate_api.statuses == [(HEAD, "failure")]


@pytest.mark.parametrize("kind", UNLOADABLE)
def test_an_unloadable_config_with_no_post_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_api: GateApi, kind: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    assert gate.main(_unloadable_argv(_unloadable_root(tmp_path, kind), "--no-post")) == gate.ERROR
    assert gate_api.statuses == []


@pytest.mark.parametrize("kind", UNLOADABLE)
def test_an_unloadable_config_with_a_head_the_pull_request_does_not_own_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_api: GateApi, kind: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    argv = _unloadable_argv(_unloadable_root(tmp_path, kind), "--head-sha", "b" * 40)
    assert gate.main(argv) == gate.ERROR
    assert gate_api.statuses == []


def test_an_unloadable_config_posts_nothing_when_the_head_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_api: GateApi
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    gate_api.head = "not-a-sha"
    assert gate.main(_unloadable_argv(_unloadable_root(tmp_path, "policy"))) == gate.ERROR
    assert gate_api.statuses == []


def test_exempt_only_change_passes() -> None:
    expected = review_check.decide(HEAD, OTHER_FILES, [], _matcher(), binding=BINDING)
    assert expected == (review_check.PASS, "oracle-review: only exempt paths changed")
    assert gate.decide(HEAD, OTHER_FILES, [], _matcher(), binding=BINDING) == expected


def test_ship_with_a_receipt_passes() -> None:
    statuses = [_success_status()]
    expected = review_check.decide(HEAD, SOUNDNESS_FILES, statuses, _matcher(), binding=BINDING, reviews=[_receipt()])
    assert expected[0] == review_check.PASS
    assert gate.decide(HEAD, SOUNDNESS_FILES, statuses, _matcher(), binding=BINDING, reviews=[_receipt()]) == expected


def test_ship_without_a_same_pr_receipt_waits() -> None:
    statuses = [_success_status()]
    expected = review_check.decide(HEAD, SOUNDNESS_FILES, statuses, _matcher(), binding=BINDING)
    assert expected[0] == review_check.WAITING
    assert "no unedited oracle review" in expected[1]
    assert gate.decide(HEAD, SOUNDNESS_FILES, statuses, _matcher(), binding=BINDING) == expected


def test_the_gate_decision_takes_no_review_thread_state() -> None:
    assert "threads" not in inspect.signature(gate.decide).parameters
    assert not hasattr(gate, "fetch_threads")


def test_exact_standin_for_the_head_passes() -> None:
    comments = [_attestation()]
    expected = review_check.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), None, comments, binding=BINDING)
    assert expected[0] == review_check.PASS
    assert "stand-in review attested by joeharris76" in expected[1]
    assert gate.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), None, comments, binding=BINDING) == expected


def test_standin_with_extra_text_waits() -> None:
    comments = [_attestation(f"note: Stand-in oracle review: APPROVE {HEAD} thanks")]
    expected = review_check.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), None, comments, binding=BINDING)
    assert expected[0] == review_check.WAITING
    assert gate.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), None, comments, binding=BINDING) == expected


def test_absent_verdict_waits() -> None:
    expected = review_check.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), binding=BINDING)
    assert expected[0] == review_check.WAITING
    assert gate.decide(HEAD, SOUNDNESS_FILES, [], _matcher(), binding=BINDING) == expected


def test_gate_state_maps_pass_to_success_and_everything_else_to_failure() -> None:
    assert gate.gate_state(gate.PASS) == "success"
    assert gate.gate_state(gate.WAITING) == "failure"
    assert gate.gate_state(gate.ERROR) == "failure"


def test_status_payload_posts_the_gate_context() -> None:
    payload = gate.status_payload(gate.PASS, "oracle-review: pass (oracle verdict for " + HEAD + ")")
    assert payload == {
        "state": "success",
        "context": "oracle-gate",
        "description": "oracle-review: pass (oracle verdict for " + HEAD + ")",
    }


def test_status_payload_clips_long_descriptions_to_the_api_limit() -> None:
    payload = gate.status_payload(gate.WAITING, "x" * 200, "https://example.invalid/run")
    assert payload["state"] == "failure"
    assert payload["context"] == "oracle-gate"
    assert len(payload["description"]) == gate.DESCRIPTION_LIMIT
    assert payload["target_url"] == "https://example.invalid/run"


@pytest.fixture
def status_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length).decode()),
                }
            )
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(gate, "API_ROOT", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_post_status_writes_the_gate_context_to_the_head(status_api: list[dict[str, Any]]) -> None:
    gate.post_status("token", "o/r", HEAD, gate.PASS, "oracle-review: pass")
    assert [(item["path"], item["auth"]) for item in status_api] == [(f"/repos/o/r/statuses/{HEAD}", "Bearer token")]
    assert status_api[0]["body"]["context"] == "oracle-gate"
    assert status_api[0]["body"]["state"] == "success"


def test_post_status_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    monkeypatch.setattr(gate, "API_ROOT", f"http://127.0.0.1:{port}")
    with pytest.raises(gate.CheckError):
        gate.post_status("token", "o/r", HEAD, gate.PASS, "msg")


def _stub_evaluate(monkeypatch: pytest.MonkeyPatch, status: int, message: str) -> dict[str, Any]:
    posted: dict[str, Any] = {}
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_evaluate(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        posted["repo"] = repo
        posted["pr"] = pr
        posted["token"] = token
        return HEAD, status, message

    def fake_post(token: str, repo: str, head_sha: str, post_status: int, post_message: str) -> None:
        posted["state"] = gate.gate_state(post_status)
        posted["context"] = gate.GATE_CONTEXT
        posted["head"] = head_sha
        posted["message"] = post_message

    monkeypatch.setattr(gate, "evaluate", fake_evaluate)
    monkeypatch.setattr(gate, "post_status", fake_post)
    return posted


def test_main_posts_success_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _stub_evaluate(monkeypatch, gate.PASS, "oracle-review: pass")
    assert gate.main([*BASE_ARGS]) == 0
    assert (posted["repo"], posted["pr"], posted["token"]) == ("o/r", 7, "token")
    assert posted["state"] == "success" and posted["head"] == HEAD


def test_main_posts_failure_and_exits_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_evaluate(monkeypatch, gate.WAITING, "oracle-review: waiting")
    assert gate.main([*BASE_ARGS]) == gate.WAITING


def test_main_no_post_skips_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "evaluate", lambda repo, pr, token, config: (HEAD, gate.PASS, "ok"))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not post")

    monkeypatch.setattr(gate, "post_status", forbidden)
    assert gate.main([*BASE_ARGS, "--no-post"]) == 0


def test_main_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert gate.main([*BASE_ARGS]) == gate.ERROR


def test_main_rejects_a_bad_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    assert gate.main(["--repo", "oops", "--pr", "7", *POLICY_ARGS]) == gate.ERROR


def test_main_reports_a_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def failing_evaluate(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        raise gate.CheckError("down")

    monkeypatch.setattr(gate, "evaluate", failing_evaluate)
    assert gate.main([*BASE_ARGS]) == gate.ERROR


def _stub_pull(monkeypatch: pytest.MonkeyPatch, base: str = "develop") -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        gate,
        "fetch_pull",
        lambda repo, pr, token: {"base": {"ref": base}, "head": {"sha": HEAD}, "changed_files": 1},
    )
    monkeypatch.setattr(gate, "fetch_files", lambda repo, pr, token: [{"filename": "x"}])


def _record_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, str]]:
    posts: list[tuple[str, int, str]] = []

    def fake_post(token: str, repo: str, head_sha: str, status: int, message: str) -> None:
        posts.append((head_sha, status, message))

    monkeypatch.setattr(gate, "post_status", fake_post)
    return posts


def _failing_matcher(*args: Any, **kwargs: Any) -> Any:
    raise gate.CheckError("no policy")


def test_main_posts_failure_when_evaluation_errors_after_the_head_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    monkeypatch.setattr(gate, "path_matcher", _failing_matcher)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert len(posts) == 1
    head, status, message = posts[0]
    assert head == HEAD and gate.gate_state(status) == "failure"
    assert "no policy" in message and len(gate.gate_description(message)) <= gate.DESCRIPTION_LIMIT


def test_main_posts_failure_when_a_status_fetch_raises_a_key_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)

    def broken(repo: str, pr: int, token: str) -> Any:
        raise KeyError("data")

    monkeypatch.setattr(gate, "fetch_statuses", broken)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert [head for head, _, _ in posts] == [HEAD]


def test_main_posts_failure_when_the_fresh_reevaluation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    calls: list[int] = []

    def flaky(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        calls.append(1)
        if len(calls) == 1:
            return HEAD, gate.PASS, "ok"
        raise gate.EvaluationFailed(HEAD, gate.CheckError("down"))

    monkeypatch.setattr(gate, "evaluate", flaky)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert len(posts) == 1 and posts[0][0] == HEAD and gate.gate_state(posts[0][1]) == "failure"


def test_main_returns_error_when_the_failure_post_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    monkeypatch.setattr(gate, "path_matcher", _failing_matcher)

    def failing_post(token: str, repo: str, head_sha: str, status: int, message: str) -> None:
        raise gate.CheckError("down")

    monkeypatch.setattr(gate, "post_status", failing_post)
    assert gate.main([*BASE_ARGS]) == gate.ERROR


def test_main_no_post_never_posts_an_evaluation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    monkeypatch.setattr(gate, "path_matcher", _failing_matcher)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--no-post"]) == gate.ERROR
    assert posts == []


def test_main_posts_nothing_when_the_pull_request_cannot_be_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def down(repo: str, pr: int, token: str) -> Any:
        raise gate.CheckError("down")

    monkeypatch.setattr(gate, "fetch_pull", down)

    def head_unavailable(token: str, repo: str, pr: int) -> str:
        raise gate.CheckError("graphql down")

    monkeypatch.setattr(gate, "fetch_head_oid", head_unavailable)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert posts == []


def test_main_posts_nothing_when_the_pull_request_has_no_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "fetch_pull", lambda repo, pr, token: {"base": {"ref": "develop"}})

    def head_unavailable(token: str, repo: str, pr: int) -> str:
        raise gate.CheckError("graphql down")

    monkeypatch.setattr(gate, "fetch_head_oid", head_unavailable)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert posts == []


def test_main_posts_nothing_for_an_ungated_base_even_when_evaluation_would_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pull(monkeypatch, base="feature")
    monkeypatch.setattr(gate, "path_matcher", _failing_matcher)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.PASS
    assert posts == []


def test_main_posts_nothing_for_a_pull_request_that_does_not_target_develop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        gate,
        "fetch_pull",
        lambda repo, pr, token: {"base": {"ref": "feature"}, "head": {"sha": HEAD}, "changed_files": 1},
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not evaluate or post")

    for name in ("fetch_files", "fetch_statuses", "fetch_reviews", "annotate_receipt"):
        monkeypatch.setattr(gate, name, forbidden)
    assert gate.main([*BASE_ARGS]) == gate.PASS


def test_main_posts_the_fresh_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    posted: dict[str, Any] = {}
    seen: list[str] = []

    def moving_evaluate(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        seen.append(repo)
        if len(seen) == 1:
            return HEAD, gate.WAITING, "first"
        return HEAD, gate.PASS, "second"

    def fake_post(token: str, repo: str, head_sha: str, status: int, message: str) -> None:
        posted["head"] = head_sha
        posted["message"] = message

    monkeypatch.setattr(gate, "evaluate", moving_evaluate)
    monkeypatch.setattr(gate, "post_status", fake_post)
    assert gate.main([*BASE_ARGS]) == 0
    assert posted == {"head": HEAD, "message": "second"}


def test_main_reports_a_post_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_evaluate(monkeypatch, gate.PASS, "ok")

    def failing_post(token: str, repo: str, head_sha: str, status: int, message: str) -> None:
        raise gate.CheckError("down")

    monkeypatch.setattr(gate, "post_status", failing_post)
    assert gate.main([*BASE_ARGS]) == gate.ERROR


def test_target_url_points_at_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert gate.target_url("o/r") is None
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    assert gate.target_url("o/r") == "https://github.com/o/r/actions/runs/123"


OTHER_HEAD = "b" * 40


def _test_pull(base: str = "develop", head: str = HEAD) -> dict[str, Any]:
    return {"base": {"ref": base}, "head": {"sha": head}, "changed_files": 1}


def _stub_unreadable_pull(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    calls: list[int] = []

    def flaky(repo: str, pr: int, token: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) <= failures:
            raise gate.CheckError("read down")
        return _test_pull()

    monkeypatch.setattr(gate, "fetch_pull", flaky)
    monkeypatch.setattr(gate, "fetch_files", lambda repo, pr, token: [{"filename": "x"}])
    monkeypatch.setattr(gate, "fetch_statuses", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda repo, pr, token: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "annotate_receipt", lambda token, reviews, head_sha, bot_login, base_date=None: None)
    return calls


@pytest.mark.parametrize(
    "error", [TypeError("pullRequest is null"), ConnectionResetError("reset"), AttributeError("x")]
)
def test_main_posts_failure_for_any_exception_after_the_head_is_known(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _stub_pull(monkeypatch)
    monkeypatch.setattr(gate, "fetch_statuses", lambda repo, pr, token: [])

    def broken(repo: str, pr: int, token: str) -> Any:
        raise error

    monkeypatch.setattr(gate, "fetch_base_change_date", broken)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert [(head, gate.gate_state(status)) for head, status, _ in posts] == [(HEAD, "failure")]


def test_main_posts_failure_when_the_fresh_reevaluation_raises_an_arbitrary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pull(monkeypatch)
    monkeypatch.setattr(gate, "fetch_statuses", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_comments", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "annotate_receipt", lambda token, reviews, head_sha, bot_login, base_date=None: None)
    calls: list[int] = []

    def base_change(repo: str, pr: int, token: str) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionResetError("reset")

    monkeypatch.setattr(gate, "fetch_base_change_date", base_change)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert [(head, gate.gate_state(status)) for head, status, _ in posts] == [(HEAD, "failure")]


def test_pull_read_that_fails_twice_then_succeeds_evaluates_normally(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _stub_unreadable_pull(monkeypatch, failures=2)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) in (gate.PASS, gate.WAITING)
    assert len(calls) == 4
    assert sleeps == [2, 4]
    assert [head for head, _, _ in posts] == [HEAD]


def test_pull_read_failing_three_times_posts_failure_to_the_given_head_when_the_pull_request_owns_it(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _stub_unreadable_pull(monkeypatch, failures=99)
    graphql = _stub_graphql_head(monkeypatch, OTHER_HEAD)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--head-sha", OTHER_HEAD]) == gate.ERROR
    assert len(calls) == 3
    assert sleeps == [2, 4]
    assert graphql == [("o/r", 7)]
    assert [(head, gate.gate_state(status)) for head, status, _ in posts] == [(OTHER_HEAD, "failure")]


def _stub_graphql_head(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def head(token: str, repo: str, pr: int) -> Any:
        calls.append((repo, pr))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(gate, "fetch_head_oid", head)
    return calls


def test_pull_read_failing_three_times_with_a_graphql_head_posts_failure_to_it(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _stub_unreadable_pull(monkeypatch, failures=99)
    graphql = _stub_graphql_head(monkeypatch, OTHER_HEAD)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert len(calls) == 3
    assert sleeps == [2, 4]
    assert graphql == [("o/r", 7)]
    assert [(head, gate.gate_state(status)) for head, status, _ in posts] == [(OTHER_HEAD, "failure")]


def test_pull_read_and_graphql_read_both_failing_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    graphql = _stub_graphql_head(monkeypatch, gate.CheckError("graphql down"))
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert graphql == [("o/r", 7)]
    assert posts == []


@pytest.mark.parametrize("value", [None, "", "abc", "A" * 40, 7])
def test_an_unusable_graphql_head_posts_nothing(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    _stub_graphql_head(monkeypatch, value)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert posts == []


def test_a_given_head_that_the_pull_request_does_not_own_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    graphql = _stub_graphql_head(monkeypatch, HEAD)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--head-sha", OTHER_HEAD]) == gate.ERROR
    assert graphql == [("o/r", 7)]
    assert posts == []


def test_a_bogus_pull_request_number_posts_nothing_on_a_foreign_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    _stub_graphql_head(monkeypatch, gate.CheckError("Could not resolve to a PullRequest"))
    posts = _record_posts(monkeypatch)
    assert gate.main(["--repo", "o/r", "--pr", "999999", *POLICY_ARGS, "--head-sha", OTHER_HEAD]) == gate.ERROR
    assert posts == []


def test_no_post_never_reads_the_graphql_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    graphql = _stub_graphql_head(monkeypatch, HEAD)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--no-post"]) == gate.ERROR
    assert graphql == []
    assert posts == []


def test_a_readable_pull_wins_over_the_graphql_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=0)
    graphql = _stub_graphql_head(monkeypatch, OTHER_HEAD)
    posts = _record_posts(monkeypatch)
    gate.main([*BASE_ARGS])
    assert graphql == []
    assert [head for head, _, _ in posts] == [HEAD]


def test_pull_read_failure_with_a_head_never_posts_under_no_post(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=99)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--head-sha", OTHER_HEAD, "--no-post"]) == gate.ERROR
    assert posts == []


def test_a_readable_pull_request_ignores_the_given_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_unreadable_pull(monkeypatch, failures=0)
    posts = _record_posts(monkeypatch)
    gate.main([*BASE_ARGS, "--head-sha", OTHER_HEAD])
    assert [head for head, _, _ in posts] == [HEAD]


def test_the_given_head_does_not_post_for_an_ungated_base(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch, base="feature")
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS, "--head-sha", OTHER_HEAD]) == gate.PASS
    assert posts == []


def test_pull_read_failing_on_the_fresh_reevaluation_posts_failure_to_the_known_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    calls: list[int] = []

    def flaky(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        calls.append(1)
        if len(calls) == 1:
            return HEAD, gate.PASS, "ok"
        raise gate.PullReadFailed("down")

    monkeypatch.setattr(gate, "evaluate", flaky)
    posts = _record_posts(monkeypatch)
    assert gate.main([*BASE_ARGS]) == gate.ERROR
    assert [(head, gate.gate_state(status)) for head, status, _ in posts] == [(HEAD, "failure")]


@pytest.mark.parametrize("value", ["", "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41])
def test_an_invalid_head_sha_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    posts = _record_posts(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        gate.main([*BASE_ARGS, "--head-sha", value])
    assert raised.value.code == 2
    assert posts == []


def test_fetch_head_oid_reads_the_pull_request_head_through_graphql(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def request(token: str, url: str, body: dict[str, Any] | None = None) -> Any:
        assert body is not None
        sent.append((url, body))
        return {"data": {"repository": {"pullRequest": {"headRefOid": HEAD}}}}

    monkeypatch.setattr(review_check, "_request", request)
    assert review_check.fetch_head_oid("token", "o/r", 7) == HEAD
    assert sent[0][0].endswith("/graphql")
    assert sent[0][1]["variables"] == {"owner": "o", "name": "r", "number": 7}
    assert "headRefOid" in sent[0][1]["query"]


def test_fetch_head_oid_raises_on_graphql_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_check, "_request", lambda token, url, body=None: {"errors": ["boom"]})
    with pytest.raises(review_check.CheckError):
        review_check.fetch_head_oid("token", "o/r", 7)


def test_process_exit_gives_waiting_a_code_distinct_from_crashes() -> None:
    assert gate.process_exit(gate.PASS) == 0
    assert gate.process_exit(gate.ERROR) == gate.ERROR
    assert gate.process_exit(gate.WAITING) == gate.WAITING_EXIT
    assert gate.WAITING_EXIT not in (0, 1, gate.ERROR)


@pytest.mark.parametrize("entry", [["-m", "theseus_reviewer.gate"], ["-m", "theseus_reviewer", "gate"]])
def test_script_entry_point_exits_through_the_process_mapping(entry: list[str]) -> None:
    env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_TOKEN", "GH_TOKEN")}
    result = subprocess.run(
        [sys.executable, *entry, *BASE_ARGS],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == gate.ERROR
    assert "GITHUB_TOKEN is not set" in result.stderr


@pytest.mark.parametrize(
    ("status", "expected"),
    [("PASS", "0"), ("WAITING", "3"), ("ERROR", "2")],
)
def test_run_reports_the_process_exit_in_the_result_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, expected: str
) -> None:
    target = tmp_path / "result"
    monkeypatch.setenv(gate.RESULT_FILE_ENV, str(target))
    monkeypatch.setattr(gate, "main", lambda argv=None: getattr(gate, status))
    assert gate.run([]) == int(expected)
    assert target.read_text(encoding="utf-8") == f"{expected}\n"


def test_run_writes_no_result_file_without_the_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(gate.RESULT_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate, "main", lambda argv=None: gate.WAITING)
    assert gate.run([]) == gate.WAITING_EXIT
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [["-m", "theseus_reviewer.gate"], ["-m", "theseus_reviewer", "gate"]])
def test_a_crash_before_the_exit_leaves_no_result(tmp_path: Path, entry: list[str]) -> None:
    target = tmp_path / "result"
    env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_TOKEN", "GH_TOKEN")}
    env[gate.RESULT_FILE_ENV] = str(target)
    result = subprocess.run(
        [sys.executable, *entry, "--repo", "o/r", "--pr", "not-a-number", *POLICY_ARGS],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not target.exists()


@pytest.mark.parametrize("entry", [["-m", "theseus_reviewer.gate"], ["-m", "theseus_reviewer", "gate"]])
def test_the_gate_cli_requires_a_policy(entry: list[str]) -> None:
    env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_TOKEN", "GH_TOKEN")}
    result = subprocess.run(
        [sys.executable, *entry, "--repo", "o/r", "--pr", "7"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--policy" in result.stderr


def _record_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "post_pending", lambda token, repo, head: events.append(("pending", head)))
    monkeypatch.setattr(gate, "post_status", lambda token, repo, head, status, message: events.append(("final", head)))
    return events


def test_a_head_change_posts_pending_before_any_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    events = _record_events(monkeypatch)

    def evaluation(repo: str, pr: int, token: str, config: Any) -> tuple[str, int, str]:
        events.append(("evaluate", HEAD))
        return HEAD, gate.PASS, "ok"

    monkeypatch.setattr(gate, "evaluate", evaluation)
    assert gate.main([*BASE_ARGS, "--pending-first"]) == gate.PASS
    assert events == [("pending", HEAD), ("evaluate", HEAD), ("evaluate", HEAD), ("final", HEAD)]


def test_pending_is_not_posted_when_the_flag_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    events = _record_events(monkeypatch)
    monkeypatch.setattr(gate, "evaluate", lambda repo, pr, token, config: (HEAD, gate.PASS, "ok"))
    assert gate.main([*BASE_ARGS]) == gate.PASS
    assert events == [("final", HEAD)]


def test_pending_is_not_posted_for_a_pull_request_that_does_not_target_develop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pull(monkeypatch, base="feature")
    events = _record_events(monkeypatch)
    assert gate.main([*BASE_ARGS, "--pending-first"]) == gate.PASS
    assert events == []


def test_a_failed_pending_post_does_not_stop_the_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_pull(monkeypatch)
    events = _record_events(monkeypatch)

    def failing_pending(token: str, repo: str, head: str) -> None:
        raise gate.CheckError("down")

    monkeypatch.setattr(gate, "post_pending", failing_pending)
    monkeypatch.setattr(gate, "evaluate", lambda repo, pr, token, config: (HEAD, gate.WAITING, "waiting"))
    assert gate.main([*BASE_ARGS, "--pending-first"]) == gate.WAITING
    assert events == [("final", HEAD)]


def test_pending_first_cannot_be_combined_with_no_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    assert gate.main([*BASE_ARGS, "--pending-first", "--no-post"]) == gate.ERROR


def test_the_pending_payload_is_a_pending_gate_status() -> None:
    assert gate.pending_payload("https://example.invalid/run") == {
        "state": "pending",
        "context": "oracle-gate",
        "description": gate.PENDING_DESCRIPTION,
        "target_url": "https://example.invalid/run",
    }


def test_the_gated_base_comes_from_the_consumer_policy(monkeypatch: pytest.MonkeyPatch, beta_root: Path) -> None:
    beta_args = ["--repo", "o/r", "--pr", "7", "--policy", POLICY_REL, "--root", str(beta_root)]
    _stub_pull(monkeypatch, base="develop")
    posts = _record_posts(monkeypatch)
    assert gate.main(beta_args) == gate.PASS
    assert posts == []
    monkeypatch.setattr(gate, "fetch_pull", lambda repo, pr, token: _test_pull(base="main"))
    monkeypatch.setattr(gate, "fetch_statuses", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda repo, pr, token: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda repo, pr, token: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda repo, pr, token: [])
    assert gate.main(beta_args) == gate.WAITING
    assert [head for head, _, _ in posts] == [HEAD]
