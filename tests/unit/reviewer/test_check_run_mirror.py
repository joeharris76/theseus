from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import check_run, cli, driver, github, selection
from theseus_reviewer.policy import Policy, PolicyError, parse_policy

from . import test_driver
from .test_attempts import CHECKER, HEAD, _v
from .test_driver import REPO, RecordingSink, _environment, _final, _plan, _write
from .test_policy import _raw
from .test_report_outcomes import _comment_plan, _defect, _finalize
from .test_rounds import _protocol_plan

pytestmark = [pytest.mark.unit, pytest.mark.fast]

outputs = test_driver.outputs
sink = test_driver.sink


def _verdict() -> dict[str, Any]:
    return _v(
        "x",
        [
            _defect("Far high", 40, "far <!-- x --> detail", severity="High"),
            _defect("Far low", 41, "low detail", severity="Low"),
            _defect("Inside", 2),
        ],
    )


def test_the_flag_defaults_to_off_and_must_be_a_boolean() -> None:
    assert parse_policy(_raw()).check_run_mirror is False
    assert parse_policy({**_raw(), "check_run_mirror": True}).check_run_mirror is True
    for value in ("true", 1, None):
        with pytest.raises(PolicyError, match="check_run_mirror must be a boolean"):
            parse_policy({**_raw(), "check_run_mirror": value})


def test_review_delivery_mirrors_only_the_findings_without_a_thread(policy: Policy) -> None:
    final = _finalize(policy, _protocol_plan(policy), _verdict(), kind=selection.FAIL)
    assert sorted(finding.title for finding, _ in final.threadless) == ["Far high", "Far low"]
    assert all(defect_id for _, defect_id in final.threadless)


def test_comment_delivery_mirrors_every_finding(policy: Policy) -> None:
    final = _finalize(policy, _comment_plan(policy), _verdict(), kind=selection.FAIL)
    assert len(final.threadless) == 3


def test_a_clean_verdict_mirrors_nothing(policy: Policy) -> None:
    assert _finalize(policy, _protocol_plan(policy), _v("fine")).threadless == ()


def test_annotations_carry_path_lines_level_and_neutralised_text(policy: Policy) -> None:
    final = _finalize(policy, _protocol_plan(policy), _verdict(), kind=selection.FAIL)
    mirror = check_run.payload({"head_sha": HEAD}, final.threadless)
    assert mirror is not None and mirror["name"] == "review-findings" and mirror["head_sha"] == HEAD
    by_title = {item["title"].split(" · ")[-1]: item for item in mirror["annotations"]}
    high, low = by_title["Far high"], by_title["Far low"]
    assert (high["path"], high["start_line"], high["end_line"], high["annotation_level"]) == (
        CHECKER,
        40,
        40,
        "failure",
    )
    assert low["annotation_level"] == "notice" and high["title"].startswith("High · D")
    assert "<!--" not in high["message"] and "far" in high["message"]
    assert check_run.MIRROR_NOTE in high["message"] and mirror["summary"].endswith(check_run.MIRROR_NOTE)


@pytest.mark.parametrize(
    ("severity", "level"), [("Critical", "failure"), ("High", "failure"), ("Medium", "warning"), ("Low", "notice")]
)
def test_severity_maps_to_an_annotation_level(policy: Policy, severity: str, level: str) -> None:
    final = _finalize(
        policy, _protocol_plan(policy), _v("x", [_defect("Far", 40, severity=severity)]), kind=selection.FAIL
    )
    assert check_run.annotation(*final.threadless[0])["annotation_level"] == level


def test_no_threadless_findings_means_no_payload() -> None:
    assert check_run.payload({"head_sha": HEAD}, ()) is None


def test_long_text_is_cut_to_the_api_limits(policy: Policy) -> None:
    final = _finalize(
        policy,
        _protocol_plan(policy),
        _v("x", [_defect("T" * 190, 40, "d" * 3900)]),
        kind=selection.FAIL,
    )
    item = check_run.annotation(*final.threadless[0])
    assert len(item["title"]) <= check_run.MAX_TITLE and len(item["message"]) <= check_run.MAX_MESSAGE


def _mirror(count: int) -> dict[str, Any]:
    note = [
        {"path": "a.py", "start_line": n, "end_line": n, "annotation_level": "warning", "title": "t", "message": "m"}
        for n in range(1, count + 1)
    ]
    return {"name": "review-findings", "head_sha": HEAD, "title": "t", "summary": "s", "annotations": note}


def test_requests_batch_annotations_fifty_at_a_time_and_never_conclude_failure() -> None:
    bodies = check_run.requests(_mirror(120))
    assert [len(body["output"]["annotations"]) for body in bodies] == [50, 50, 20]
    assert bodies[0]["conclusion"] == "neutral" and bodies[0]["status"] == "completed"
    assert all("conclusion" not in body and "name" not in body for body in bodies[1:])


class FakeChecksApi:
    def __init__(self, deny_after: int | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any], str]] = []
        self.deny_after = deny_after

    def __call__(
        self, argv: list[str], *, token: str, timeout: float, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), json.loads(stdin or "{}"), token))
        if self.deny_after is not None and len(self.calls) > self.deny_after:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Resource not accessible by integration (HTTP 403)")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"id": 99}), "")


def test_the_first_request_creates_the_run_and_later_ones_append_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeChecksApi()
    monkeypatch.setattr(github, "run_gh", api)
    github.post_check_run(REPO, check_run.requests(_mirror(75)), "checks-token")
    assert [(argv[3], argv[4]) for argv, _, _ in api.calls] == [
        ("POST", f"repos/{REPO}/check-runs"),
        ("PATCH", f"repos/{REPO}/check-runs/99"),
    ]
    assert {token for _, _, token in api.calls} == {"checks-token"}
    assert [len(body["output"]["annotations"]) for _, body, _ in api.calls] == [50, 25]


def test_a_permission_error_raises_once_and_stops_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeChecksApi(deny_after=0)
    monkeypatch.setattr(github, "run_gh", api)
    with pytest.raises(github.GitHubError, match="not accessible by integration"):
        github.post_check_run(REPO, check_run.requests(_mirror(75)), "t")
    assert len(api.calls) == 1


def _publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink: RecordingSink, *, mirror: bool) -> tuple[int, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    environment = _environment(tmp_path)
    _plan(environment.layout, bot_login="acme-review")
    _final(environment.layout.final, comment=True, review=True)
    if mirror:
        _write(environment.layout.final / check_run.FILE, _mirror(1))
    monkeypatch.setattr(cli, "guard_decision", lambda plan: (0, False))
    code = driver.publish(environment, argparse.Namespace())
    return code, Path(os.environ["GITHUB_OUTPUT"]).read_text(encoding="utf-8")


def test_publish_mirrors_after_the_gate_only_when_a_mirror_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path, sink: RecordingSink
) -> None:
    assert _publish(tmp_path / "off", monkeypatch, sink, mirror=False)[0] == 0
    assert "check_run" not in [call[0] for call in sink.calls]
    sink.calls.clear()
    assert _publish(tmp_path / "on", monkeypatch, sink, mirror=True)[0] == 0
    assert [call[0] for call in sink.calls] == ["comment", "review", "status", "gate", "check_run"]


def test_a_failed_mirror_changes_neither_the_exit_code_nor_what_was_posted_and_is_logged_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = _publish(tmp_path / "off", monkeypatch, sink, mirror=False)
    kinds = [call[0] for call in sink.calls]
    sink.calls.clear()
    outputs.write_text("", encoding="utf-8")
    capsys.readouterr()
    sink.refuse = "check_run"

    assert _publish(tmp_path / "on", monkeypatch, sink, mirror=True) == baseline
    assert [call[0] for call in sink.calls] == kinds
    assert capsys.readouterr().err.count("::warning::findings were not mirrored") == 1


def test_the_real_sink_refuses_without_a_checks_token_and_the_run_is_unaffected(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    real = driver.GitHubSink("app", "job", environment.sink().consumer, environment.policy)  # type: ignore[attr-defined]
    _write(tmp_path / "m.json", _mirror(1))
    with pytest.raises(github.GitHubError, match="checks: write"):
        real.check_run(REPO, tmp_path / "m.json")


def test_the_checks_token_comes_from_its_own_variable(tmp_path: Path) -> None:
    assert _environment(tmp_path, CHECKS_TOKEN="checks").checks_token == "checks"
    assert _environment(tmp_path).checks_token == ""
