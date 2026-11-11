from __future__ import annotations

import json
import os
import sys
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
    local,
    omp_pin,
    omp_watch,
    promotion,
    protocol,
    reset,
    review_check,
    sweep,
)
from theseus_reviewer.retry import State

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
LATER = "2026-10-01T13:00:00Z"
BOT = "benchbox-review[bot]"
OWNER = "joeharris76"
OWNER_NOTE = f"Owner instruction: reset {HEAD}"
CONFIG = gate.load_gate_config(".github/review.yml", CONSUMER_ALPHA)
FILES = ["benchbox/core/equivalence/checker.py"]


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
    return {"login": OWNER, "user_type": "User", "body": text, "created_at": LATER, "updated_at": LATER}


def test_the_protocol_marker_is_written_with_the_review_name_and_read_under_the_retired_name() -> None:
    marker = protocol.encode_marker(_record())
    assert marker.startswith("<!-- review-protocol: v1 ")
    retired = marker.replace("review-protocol", "oracle-protocol")
    assert protocol.decode_marker(f"### verdict\n\n{marker}") is not None
    assert protocol.decode_marker(f"### verdict\n\n{retired}") == protocol.decode_marker(marker)
    assert protocol.marker_payload(retired) is not None


def test_a_retired_name_record_keeps_its_open_defects_and_strikes() -> None:
    defect = {"id": "D1", "severity": "High", "file": "a.py", "line": 3, "end_line": None, "title": "t"}
    record = {
        **_record(),
        "decision": protocol.DO_NOT_SHIP,
        "strikes_after": 1,
        "open_defects": [defect],
        "next_id": 2,
    }
    body = "### review-verdict: failure for `" + HEAD + "`\n\n" + protocol.encode_marker(record)
    retired = body.replace("review-protocol", "oracle-protocol")
    review = {
        "id": 1,
        "login": BOT,
        "user_type": "Bot",
        "body": retired,
        "submitted_at": "2026-10-01T10:00:00Z",
        "last_edited_at": None,
        "includes_created_edit": False,
    }
    found = protocol.history([review], BOT.removesuffix("[bot]"))
    assert found.error is None
    assert [item["open_defects"] for item in found.records] == [[defect]]
    assert protocol.strikes(found.records) == 1
    assert "oracle" not in protocol.encode_marker(found.records[0]).lower()


def test_a_retired_name_record_that_is_not_the_last_bytes_or_is_repeated_fails_closed() -> None:
    marker = protocol.encode_marker(_record()).replace("review-protocol", "oracle-protocol")
    with pytest.raises(protocol.MarkerError):
        protocol.decode_marker(f"{marker}\ntrailing text")
    with pytest.raises(protocol.MarkerError):
        protocol.decode_marker(f"{marker}\n{protocol.encode_marker(_record())}")
    with pytest.raises(protocol.MarkerError):
        protocol.decode_marker(f"{marker}\n{marker}")


def test_a_coverage_record_keeps_its_coverage() -> None:
    marker = protocol.encode_marker({**_record(), "coverage": 2})
    decoded = protocol.decode_marker(f"### verdict\n\n{marker}")
    assert decoded is not None and decoded["coverage"] == 2


def test_defect_thread_markers_are_read_under_both_names() -> None:
    assert protocol.thread_finding("<!-- review-defect: c2-D3 -->") == (2, "D3")
    assert protocol.thread_finding("body\n<!-- oracle-defect: c4-D12 -->") == (4, "D12")
    assert protocol.thread_finding_id("<!--oracle-defect:c1-D1-->") == "D1"
    text = "**High**: x\n<!-- oracle-defect: c1-D1 -->\n<!-- review-defect: c1-D1 -->\nkept"
    assert protocol.strip_marker_lines(text) == "**High**: x\nkept"


def test_the_finding_fingerprint_marker_is_written_as_review_and_read_under_both_names() -> None:
    from theseus_reviewer.verdict import Finding

    written = dedup.marker(Finding("High", "src/a.py", 3, "Title", "detail"))
    assert written.startswith("<!-- review-finding: ")
    retired = written.replace("review-finding", "oracle-finding")
    fingerprint = written.split(": ", 1)[1].split(" ", 1)[0]
    assert dedup.thread_fingerprint(None, written) == fingerprint
    assert dedup.thread_fingerprint(None, retired) == fingerprint


def test_the_local_record_marker_is_written_as_review_and_read_under_both_names() -> None:
    encoded = findings._encode({"v": findings.RECORD_VERSION, "findings": []})
    assert encoded.startswith("<!-- review-local-record: ")
    retired = encoded.replace("review-local-record", "oracle-local-record")
    assert findings.decode_body(encoded) is not None
    assert findings.decode_body(retired) == findings.decode_body(encoded)


def test_adjudication_comments_use_the_review_prefix_only() -> None:
    assert adjudication.PREFIX == "Review adjudication: "
    ruling = adjudication.parse(f"{adjudication.PREFIX}WITHDRAW D3 - the citation was wrong")
    assert ruling == adjudication.Adjudication("WITHDRAW", "D3", "the citation was wrong")
    assert cli._is_adjudication(
        "issue_comment", {"comment": {"body": f"{adjudication.PREFIX}MAINTAIN D1 - still true"}}
    )
    retired = "Oracle adjudication: MAINTAIN D1 - still true"
    assert adjudication.parse(retired) is None
    assert not cli._is_adjudication("issue_comment", {"comment": {"body": retired}})


def _reset_comments(prefix: str) -> list[dict[str, Any]]:
    return [
        {"user": {"login": OWNER, "type": "User"}, "body": OWNER_NOTE, "created_at": "2026-10-01T10:00:00Z"},
        {
            "user": {"login": OWNER, "type": "User"},
            "body": f"{prefix}{HEAD}\nReason: because",
            "created_at": "2026-10-01T11:00:00Z",
        },
    ]


def test_backoff_resets_are_written_with_the_review_prefix_and_read_under_both() -> None:
    assert reset.reset_body(HEAD, "why").startswith("Review backoff reset: ")
    attesters = frozenset({OWNER})
    assert reset.reset_moment(_reset_comments(reset.RESET_PREFIX), HEAD, attesters) == datetime(
        2026, 10, 1, 11, tzinfo=UTC
    )
    assert reset.reset_moment(_reset_comments("Oracle backoff reset: "), HEAD, attesters) == datetime(
        2026, 10, 1, 11, tzinfo=UTC
    )


def test_a_retired_name_backoff_reset_from_a_non_attester_is_rejected() -> None:
    comments = _reset_comments("Oracle backoff reset: ")
    comments[1]["user"] = {"login": "someone-else", "type": "User"}
    assert reset.reset_moment(comments, HEAD, frozenset({OWNER})) is None
    comments[1]["user"] = {"login": OWNER, "type": "Bot"}
    assert reset.reset_moment(comments, HEAD, frozenset({OWNER})) is None


def test_the_gate_reads_only_the_review_verdict_status_context() -> None:
    standin = [_standin(f"{review_check.STANDIN_APPROVAL} {HEAD}")]
    assert _decide([], standin, []) == 0
    assert _decide([_status("oracle-verdict")], standin, []) == 0
    assert _decide([_status("oracle-verdict", "failure")], standin, []) == 0
    assert _decide([_status("review-verdict")], standin, []) != 0


def test_a_standin_attestation_passes_the_gate_only_in_the_review_spelling() -> None:
    phrase = review_check.STANDIN_APPROVAL
    failing = [_status("review-verdict", "failure", "2026-10-01T11:00:00Z")]
    assert _decide(failing, [_standin(f"{phrase} {HEAD}")], []) == 0
    assert _decide(failing, [_standin(f"{phrase} {'b' * 40}")], []) != 0
    retired = phrase.replace("review", "oracle review")
    assert _decide(failing, [_standin(f"{retired} {HEAD}")], []) != 0


def test_the_verdict_heading_is_read_only_in_the_review_form() -> None:
    for context, expected in (("review-verdict", "success"), ("oracle-verdict", None)):
        body = f"### {context}: success for `{HEAD}`\n\nDetails."
        assert review_check.verdict_state(body, HEAD, review_check._REVIEW_VERDICT) == expected


def test_app_statuses_count_only_under_their_own_context() -> None:
    def statuses(context: str) -> list[dict[str, Any]]:
        return [
            {
                "context": context,
                "state": "success",
                "created_at": LATER,
                "creator": {"login": BOT, "type": "Bot"},
            }
        ]

    assert len(edits.app_verdict_statuses(statuses("review-verdict"), "review-verdict", "benchbox-review")) == 1
    assert edits.app_verdict_statuses(statuses("oracle-verdict"), "review-verdict", "benchbox-review") == []
    assert edits.app_verdict_statuses(statuses("review-verdict"), "review-gate", "benchbox-review") == []


def test_a_surfaced_sweep_failure_is_recognised_under_its_current_prefix_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix, expected in ((github.SWEEP_FAILURE_SURFACE_PREFIX, True), ("oracle-verdict failed: ", False)):
        pending = [{"state": "pending", "description": f"{prefix}Run the attempt loop."}]
        monkeypatch.setattr(cli, "_own_statuses", lambda *args, _p=pending: _p)
        assert cli._already_surfaced("o/r", HEAD, "review-verdict", "bot", 7, None) is expected
    assert sweep.surface_description(["Run it"]).startswith(github.SWEEP_FAILURE_SURFACE_PREFIX)
    assert "/review" in sweep.surface_description([])


def test_the_watch_collects_attempt_artifacts_under_the_review_name_only() -> None:
    assert list(omp_watch.ATTEMPT_ARTIFACT_NAMES) == [f"review-verdict-attempt-{slot}" for slot in range(1, 6)]
    assert omp_watch.REVIEW_WORKFLOWS == {".github/workflows/review-verdict.yml"}


def _state_sources(monkeypatch: pytest.MonkeyPatch, names: dict[str, int], path: str) -> list[str]:
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
            return {"path": path, "event": "schedule", "head_sha": HEAD, "repository": {"full_name": "o/r"}}
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


def test_state_is_written_and_read_under_the_review_artifact_names() -> None:
    assert github.state_artifact_name(7) == "review-verdict-state-7"
    assert github.attempt_state_artifact_name(7) == "review-verdict-state-attempt-7"
    assert github.WORKFLOW_PATH == ".github/workflows/review-verdict.yml"
    assert github.WORKFLOW_PATHS == (github.WORKFLOW_PATH,)


@pytest.mark.parametrize("name", ["review-verdict-state-7", "review-verdict-state-attempt-7"])
def test_state_is_read_from_the_review_artifacts(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    downloaded = _state_sources(monkeypatch, {name: 1}, github.WORKFLOW_PATH)
    assert github.latest_state("o/r", 7, "main") is not None
    assert downloaded == [name]


@pytest.mark.parametrize("name", ["oracle-verdict-state-7", "oracle-verdict-state-attempt-7"])
def test_state_published_under_the_retired_artifact_names_is_ignored(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    downloaded = _state_sources(monkeypatch, {name: 1}, ".github/workflows/oracle-verdict.yml")
    assert github.latest_state("o/r", 7, "main") is None
    assert downloaded == []


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a POSIX script")
def test_a_verdict_status_is_posted_once_under_the_review_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "calls.txt"
    script = tmp_path / "gh"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    payload = {
        "state": "failure",
        "context": "review-verdict",
        "description": "large: lists defects",
        "target_url": "u",
    }
    github.post_status("o/r", HEAD, payload, "token")
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1 and "context=review-verdict" in calls[0]


def test_the_gate_status_is_posted_once_per_call_under_the_review_context(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(gate, "_post_payload", lambda token, repo, head, payload: bodies.append(payload))
    gate.post_status("token", "o/r", HEAD, review_check.PASS, "review: pass")
    gate.post_pending("token", "o/r", HEAD)
    assert [(body["context"], body["state"]) for body in bodies] == [
        ("review-gate", "success"),
        ("review-gate", "pending"),
    ]


def test_a_status_file_is_posted_once_as_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        github, "post_file", lambda path, payload, token: seen.append((path, json.loads(payload.read_text("utf-8"))))
    )
    status = tmp_path / "status.json"
    document = {"state": "success", "context": "review-verdict", "description": "large: SHIP", "target_url": "u"}
    status.write_text(json.dumps(document), encoding="utf-8")
    github.post_status_file("o/r", HEAD, status, "token")
    assert seen == [(f"repos/o/r/statuses/{HEAD}", document)]


def test_local_configuration_reads_only_the_review_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_OMP_BINARY", raising=False)
    monkeypatch.setenv("ORACLE_OMP_BINARY", "/opt/omp")
    assert omp_pin.configured_binary() is None
    monkeypatch.setenv("REVIEW_OMP_BINARY", "/opt/new-omp")
    assert omp_pin.configured_binary() == Path("/opt/new-omp").resolve()


def test_every_environment_name_theseus_reads_uses_the_review_prefix() -> None:
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
