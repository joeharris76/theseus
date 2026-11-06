from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import CONSUMER_ALPHA
from theseus_reviewer import (
    adjudication,
    bootstrap,
    cli,
    credentials,
    dedup,
    edits,
    findings,
    gate,
    github,
    legacy,
    local,
    omp_pin,
    omp_watch,
    promotion,
    protocol,
    reset,
    review_check,
    sweep,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
LATER = "2026-10-01T13:00:00Z"
BOT = "benchbox-review[bot]"
OWNER = "joeharris76"
OWNER_NOTE = f"Owner instruction: reset {HEAD}"


def _record() -> dict[str, Any]:
    return {
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
    }


def _as_legacy(text: str, new_open: str, legacy_open: str) -> str:
    assert new_open in text
    return text.replace(new_open, legacy_open)


def test_the_legacy_module_is_the_only_home_of_the_old_names() -> None:
    assert legacy.mirror_contexts("review-verdict") == ("oracle-verdict",)
    assert legacy.mirror_contexts("review-gate") == ("oracle-gate",)
    assert legacy.mirror_contexts("something-else") == ()
    assert legacy.context_aliases("review-gate") == {"review-gate", "oracle-gate"}


def test_the_protocol_marker_is_written_with_the_new_name_and_both_forms_are_read() -> None:
    marker = protocol.encode_marker(_record())
    assert marker.startswith("<!-- review-protocol: v1 ")
    old = _as_legacy(marker, protocol.MARKER_OPEN, legacy.PROTOCOL_MARKER_OPEN)
    assert old.startswith("<!-- oracle-protocol: v1 ")
    body = "### verdict\n\n"
    assert protocol.decode_marker(body + marker) == protocol.decode_marker(body + old) is not None
    assert protocol.marker_payload(old) == protocol.marker_payload(marker) is not None


def test_a_body_with_a_new_and_a_legacy_marker_is_malformed() -> None:
    marker = protocol.encode_marker(_record())
    old = _as_legacy(marker, protocol.MARKER_OPEN, legacy.PROTOCOL_MARKER_OPEN)
    with pytest.raises(protocol.MarkerError):
        protocol.decode_marker(f"{old}\n\n{marker}")


def test_defect_thread_markers_are_written_new_and_read_in_both_forms() -> None:
    assert protocol.thread_finding("<!-- review-defect: c2-D3 -->") == (2, "D3")
    assert protocol.thread_finding("body\n<!-- oracle-defect: c4-D12 -->") == (4, "D12")
    assert protocol.thread_finding_id("<!--oracle-defect:c1-D1-->") == "D1"
    text = "**High**: x\n<!-- oracle-defect: c1-D1 -->\noracle-defect: c1-D1\n<!-- review-defect: c1-D1 -->\nkept"
    assert protocol.strip_marker_lines(text) == "**High**: x\nkept"
    assert protocol.strip_marker_lines("<!-- oracle-finding: 0123456789abcdef -->\nkept") == "kept"


def test_the_finding_fingerprint_marker_is_written_new_and_read_in_both_forms() -> None:
    from theseus_reviewer.verdict import Finding

    finding = Finding("High", "src/a.py", 3, "Title", "detail")
    written = dedup.marker(finding)
    assert written.startswith("<!-- review-finding: ")
    old = written.replace(dedup.MARKER_OPEN, legacy.FINDING_MARKER_OPEN)
    body = "**High**: unrelated heading\n\n{}\n"
    assert dedup.thread_fingerprint("src/a.py", body.format(written)) == dedup.thread_fingerprint(
        "src/a.py", body.format(old)
    )
    assert old != written and legacy.FINDING_MARKER_OPEN in old
    fingerprint = written.split(": ", 1)[1].split(" ", 1)[0]
    assert dedup.thread_fingerprint(None, old) == fingerprint


def test_the_local_record_marker_is_written_new_and_read_in_both_forms() -> None:
    encoded = findings._encode({"v": findings.RECORD_VERSION, "findings": []})
    assert encoded.startswith("<!-- review-local-record: ")
    old = encoded.replace(findings.MARKER_OPEN, legacy.LOCAL_RECORD_MARKER_OPEN)
    assert findings.decode_body(old) == findings.decode_body(encoded) is not None
    assert findings.decode_body(f"{encoded}\n{old}") is None


def test_adjudication_comments_are_written_new_and_read_in_both_forms() -> None:
    assert adjudication.PREFIX == "Review adjudication: "
    for prefix in (adjudication.PREFIX, legacy.ADJUDICATION_PREFIX):
        ruling = adjudication.parse(f"{prefix}WITHDRAW D3 - the citation was wrong")
        assert ruling == adjudication.Adjudication("WITHDRAW", "D3", "the citation was wrong")
        event = {"comment": {"body": f"{prefix}MAINTAIN D1 - still true"}}
        assert cli._is_adjudication("issue_comment", event)


def test_backoff_resets_are_written_new_and_read_in_both_forms() -> None:
    assert reset.reset_body(HEAD, "why").startswith("Review backoff reset: ")
    attesters = frozenset({OWNER})
    for prefix in (reset.RESET_PREFIX, legacy.BACKOFF_RESET_PREFIX):
        comments = [
            {"user": {"login": OWNER, "type": "User"}, "body": OWNER_NOTE, "created_at": "2026-10-01T10:00:00Z"},
            {
                "user": {"login": OWNER, "type": "User"},
                "body": f"{prefix}{HEAD}\nReason: because",
                "created_at": "2026-10-01T11:00:00Z",
            },
        ]
        assert reset.reset_moment(comments, HEAD, attesters) == datetime(2026, 10, 1, 11, tzinfo=UTC)


CONFIG = gate.load_gate_config(".github/review.yml", CONSUMER_ALPHA)
FILES = ["benchbox/core/equivalence/checker.py"]


def _status(context: str, state: str = "success", created_at: str = LATER) -> dict[str, Any]:
    return {
        "id": 1,
        "context": context,
        "state": state,
        "description": f"large: {state}",
        "login": BOT,
        "user_type": "Bot",
        "created_at": created_at,
    }


def _decide(statuses: list[dict[str, Any]], comments: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> int:
    classifier = CONFIG.policy.classifier
    paths = review_check.path_matcher(1, 1, CONFIG.matchers, classifier.exempt_paths, classifier.gate_paths)
    return review_check.decide(HEAD, FILES, statuses, paths, None, comments, binding=CONFIG.binding, reviews=reviews)[0]


def _standin(text: str) -> dict[str, Any]:
    return {
        "login": OWNER,
        "user_type": "User",
        "body": text,
        "created_at": LATER,
        "updated_at": LATER,
    }


def test_the_gate_reads_the_legacy_verdict_status_context() -> None:
    receipt = {
        "login": BOT,
        "user_type": "Bot",
        "commit_id": HEAD,
        "state": "COMMENTED",
        "submitted_at": "2026-10-01T12:00:00Z",
        "body": "",
        "last_edited_at": None,
        "includes_created_edit": False,
    }
    unbound = [_status("review-verdict")]
    assert _decide(unbound, [], [receipt]) == _decide([_status("oracle-verdict")], [], [receipt])
    assert _decide([_status("oracle-verdict", "failure")], [], [receipt]) != 0


@pytest.mark.parametrize("phrase", [review_check.STANDIN_APPROVAL, legacy.STANDIN_APPROVAL])
def test_a_standin_attestation_passes_the_gate_in_both_spellings(phrase: str) -> None:
    assert phrase.endswith("APPROVE")
    failing = [_status("review-verdict", "failure", "2026-10-01T11:00:00Z")]
    assert _decide(failing, [_standin(f"{phrase} {HEAD}")], []) == 0
    assert _decide(failing, [_standin(f"{phrase} {'b' * 40}")], []) != 0


def test_the_verdict_heading_is_read_in_both_forms() -> None:
    for context in ("review-verdict", legacy.VERDICT_HEADING_CONTEXT):
        body = f"### {context}: success for `{HEAD}`\n\nDetails."
        assert review_check.verdict_state(body, HEAD, review_check._REVIEW_VERDICT) == "success"


def test_app_statuses_under_the_legacy_context_still_count_for_the_new_context() -> None:
    statuses = [
        {
            "context": "oracle-verdict",
            "state": "success",
            "created_at": LATER,
            "creator": {"login": BOT, "type": "Bot"},
        }
    ]
    assert len(edits.app_verdict_statuses(statuses, "review-verdict", "benchbox-review")) == 1
    assert edits.app_verdict_statuses(statuses, "review-gate", "benchbox-review") == []


def test_a_surfaced_sweep_failure_is_recognised_under_the_legacy_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in (github.SWEEP_FAILURE_SURFACE_PREFIX, legacy.SWEEP_FAILURE_SURFACE_PREFIX):
        pending = [{"state": "pending", "description": f"{prefix}Run the attempt loop."}]
        monkeypatch.setattr(cli, "_own_statuses", lambda *args, _p=pending: _p)
        assert cli._already_surfaced("o/r", HEAD, "review-verdict", "bot", 7, None)
    assert sweep.surface_description(["Run it"]).startswith(github.SWEEP_FAILURE_SURFACE_PREFIX)
    assert "/review" in sweep.surface_description([])


def test_the_watch_collects_attempt_artifacts_under_both_name_families() -> None:
    names = omp_watch.ATTEMPT_ARTIFACT_NAMES
    assert [f"review-verdict-attempt-{slot}" for slot in range(1, 6)] == list(names[:5])
    assert [f"{legacy.ATTEMPT_ARTIFACT_PREFIX}{slot}" for slot in range(1, 6)] == list(names[5:])
    assert omp_watch.REVIEW_WORKFLOWS == {".github/workflows/review-verdict.yml", legacy.WORKFLOW_PATH}


def _state_sources(monkeypatch: pytest.MonkeyPatch, names: dict[str, int], path: str) -> list[str]:
    from theseus_reviewer.retry import State

    downloaded: list[str] = []

    def get_json(url: str) -> Any:
        if "actions/artifacts?name=" in url:
            name = url.split("name=", 1)[1].split("&", 1)[0]
            if name in names:
                return {
                    "artifacts": [
                        {"created_at": "2026-10-05T12:00:00Z", "workflow_run": {"id": names[name]}, "expired": False}
                    ]
                }
            return {"artifacts": []}
        if "actions/runs/" in url:
            return {
                "path": path,
                "event": "schedule",
                "head_sha": HEAD,
                "repository": {"full_name": "o/r"},
            }
        if "/compare/" in url:
            return {"status": "ahead"}
        raise AssertionError(url)

    def download(repo: str, run_id: str, name: str, directory: str) -> None:
        downloaded.append(name)
        state = State(7, HEAD, "failure", datetime(2026, 10, 5, tzinfo=UTC), strikes=1)
        (Path(directory) / github.STATE_FILE).write_text(json.dumps(state.to_json()), "utf-8")

    monkeypatch.setattr(github, "get_json", get_json)
    monkeypatch.setattr(github, "get_paginated", lambda path: [])
    monkeypatch.setattr(github, "download_state_artifact", download)
    return downloaded


def test_state_is_read_from_a_legacy_artifact_published_by_a_legacy_workflow_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = github.state_artifact_name(7, legacy.STATE_ARTIFACT_PREFIX)
    assert name == "oracle-verdict-state-7"
    downloaded = _state_sources(monkeypatch, {name: 1}, legacy.WORKFLOW_PATH)
    assert github.latest_state("o/r", 7, "main") is not None
    assert downloaded == [name]


def test_state_is_written_under_the_new_artifact_name_and_the_new_name_is_read() -> None:
    assert github.state_artifact_name(7) == "review-verdict-state-7"
    assert github.attempt_state_artifact_name(7) == "review-verdict-state-attempt-7"
    assert github.WORKFLOW_PATH == ".github/workflows/review-verdict.yml"


def test_a_legacy_attempt_artifact_is_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    name = github.attempt_state_artifact_name(7, legacy.STATE_ARTIFACT_PREFIX)
    downloaded = _state_sources(monkeypatch, {name: 1}, github.WORKFLOW_PATH)
    assert github.latest_state("o/r", 7, "main") is not None
    assert downloaded == [name]


class _Posted:
    def __init__(self) -> None:
        self.contexts: list[tuple[str, str, str]] = []

    def run(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        fields = dict(item.split("=", 1) for item in argv if "=" in item and not item.startswith("repos/"))
        self.contexts.append((fields["context"], fields["state"], fields["description"]))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_a_verdict_status_is_posted_under_the_new_context_and_mirrored_under_the_legacy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = _Posted()
    monkeypatch.setattr(subprocess, "run", posted.run)
    payload = {
        "state": "failure",
        "context": "review-verdict",
        "description": "large: lists defects",
        "target_url": "u",
    }
    github.post_status("o/r", HEAD, payload, "token")
    assert posted.contexts == [
        ("oracle-verdict", "failure", "large: lists defects"),
        ("review-verdict", "failure", "large: lists defects"),
    ]


def test_a_context_without_a_legacy_name_is_posted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _Posted()
    monkeypatch.setattr(subprocess, "run", posted.run)
    github.post_status(
        "o/r", HEAD, {"state": "pending", "context": "other", "description": "d", "target_url": "u"}, "t"
    )
    assert [item[0] for item in posted.contexts] == ["other"]


@pytest.mark.parametrize("failing", ["review-verdict", "oracle-verdict"])
def test_each_context_is_attempted_even_when_the_other_fails_and_the_post_then_fails(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    calls: list[str] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        context = next(item for item in argv if item.startswith("context=")).removeprefix("context=")
        calls.append(context)
        return subprocess.CompletedProcess(argv, 1 if context == failing else 0, "", "denied")

    monkeypatch.setattr(subprocess, "run", run)
    payload = {"state": "success", "context": "review-verdict", "description": "d", "target_url": "u"}
    with pytest.raises(github.GitHubError):
        github.post_status("o/r", HEAD, payload, "t")
    assert calls == ["oracle-verdict", "review-verdict"]


@pytest.mark.parametrize("failing", ["review-gate", "oracle-gate"])
def test_each_gate_context_is_attempted_even_when_the_other_fails(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    attempted: list[str] = []

    def urlopen(request: Any) -> _Response:
        context = json.loads(request.data)["context"]
        attempted.append(context)
        if context == failing:
            raise gate.urllib.error.URLError("down")
        return _Response()

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    with pytest.raises(review_check.CheckError):
        gate.post_status("token", "o/r", HEAD, review_check.PASS, "review: pass")
    assert attempted == ["oracle-gate", "review-gate"]


def test_each_status_file_context_is_attempted_even_when_the_other_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempted: list[str] = []

    def post_file(path: str, payload: Path, token: str) -> None:
        context = json.loads(payload.read_text("utf-8"))["context"]
        attempted.append(context)
        if context == "oracle-verdict":
            raise github.GitHubError("denied")

    monkeypatch.setattr(github, "post_file", post_file)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"state": "success", "context": "review-verdict", "description": "d"}), "utf-8")
    with pytest.raises(github.GitHubError):
        github.post_status_file("o/r", HEAD, status, "t")
    assert attempted == ["oracle-verdict", "review-verdict"]


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def test_the_gate_status_is_posted_under_the_new_context_and_mirrored_under_the_legacy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict[str, Any]] = []

    def urlopen(request: Any) -> _Response:
        bodies.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    gate.post_status("token", "o/r", HEAD, review_check.PASS, "review: pass")
    gate.post_pending("token", "o/r", HEAD)
    assert [(body["context"], body["state"], body["description"]) for body in bodies] == [
        ("oracle-gate", "success", "review: pass"),
        ("review-gate", "success", "review: pass"),
        ("oracle-gate", "pending", gate.PENDING_DESCRIPTION),
        ("review-gate", "pending", gate.PENDING_DESCRIPTION),
    ]


def test_a_status_file_is_posted_under_the_new_context_and_mirrored_under_the_legacy_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        github, "post_file", lambda path, payload, token: seen.append((path, json.loads(payload.read_text("utf-8"))))
    )
    status = tmp_path / "status.json"
    document = {"state": "success", "context": "review-verdict", "description": "large: SHIP", "target_url": "u"}
    status.write_text(json.dumps(document), encoding="utf-8")
    github.post_status_file("o/r", HEAD, status, "token")
    assert seen == [
        (f"repos/o/r/statuses/{HEAD}", {**document, "context": "oracle-verdict"}),
        (f"repos/o/r/statuses/{HEAD}", document),
    ]


def test_local_configuration_reads_only_the_new_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_OMP_BINARY", raising=False)
    monkeypatch.setenv("ORACLE_OMP_BINARY", "/opt/omp")
    assert omp_pin.configured_binary() is None
    monkeypatch.setenv("REVIEW_OMP_BINARY", "/opt/new-omp")
    assert omp_pin.configured_binary() == Path("/opt/new-omp").resolve()


def test_every_environment_name_theseus_reads_uses_the_new_prefix() -> None:
    names = [
        bootstrap.MIRROR_ENV,
        promotion.SOURCES_ENV,
        promotion.ARTIFACT_MIRROR_ENV,
        gate.RESULT_FILE_ENV,
        omp_pin.BINARY_ENV,
        local.ROOT_ENV,
        local.WORK_ENV,
        local.BASE_ENV,
        local.HEAD_ENV,
        local.PR_ENV,
        local.REPOSITORY_ENV,
        local.STATE_ENV,
        local.PROXY_ENV,
        local.SCAN_ENV,
        local.REBUTTALS_ENV,
        local.RECORD_OUT_ENV,
        local.ADJUDICATE_ENV,
        local.EXCLUDE_ENV,
        local.TRUSTED_ENV,
        local.HANDOVER_ENV,
        local.CLOUD_TOKEN_ENV,
        *local.HOST_TOKEN_FILES,
        *credentials.RUNNER_ENV,
    ]
    assert [name for name in names if name.startswith("REVIEW_")]
    assert not [name for name in names if "ORACLE" in name.upper()]


def test_a_coverage_record_written_under_the_legacy_marker_is_read_with_its_coverage() -> None:
    record = {**_record(), "coverage": 2}
    marker = protocol.encode_marker(record)
    old = _as_legacy(marker, protocol.MARKER_OPEN, legacy.PROTOCOL_MARKER_OPEN)
    decoded = protocol.decode_marker(f"### verdict\n\n{old}")
    assert decoded is not None and decoded["coverage"] == 2
    assert protocol.decode_marker(f"### verdict\n\n{marker}") == decoded
