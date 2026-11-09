from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from tests.integration.fake_decisions import MALFORMED, FakeDecisions

from theseus_reviewer import cli
from theseus_reviewer.jev import JevError

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
SIZE_RULES = {"min_lines": 1500, "exclude": ["data/*"]}


def _plan(**options: Any) -> dict[str, Any]:
    files = options.pop("files", [{"path": "src/a.py", "additions": 10, "deletions": 2, "status": "modified"}])
    return {
        "repo": "o/n",
        "pr": 7,
        "head_sha": HEAD,
        "base_sha": "b" * 40,
        "run_id": "1",
        "chain": [],
        "buckets": {},
        "reviewed_files": files,
        "gate_path_list": ["gate/*"],
        "classifier_unavailable_bucket": "max",
        "classifier_fallback": "decisions",
        "classifier_fallback_min_bucket": None,
        "classifier_size_floor": None,
        **options,
    }


def _pr_answers(size: str = "medium", template: str = "deep") -> dict[str, Any]:
    def answer(choice: str) -> dict[str, Any]:
        return {"choice": choice, "probabilities": {choice: 1.0}, "confidence": 1.0}

    return {"size": answer(size), "capability": answer("routine"), "template": answer(template)}


def _file_answers() -> dict[str, Any]:
    return {
        "impact": {"choice": "low", "probabilities": {"low": 1.0}, "confidence": 1.0},
        "importance": {"score": 1.0},
    }


class _Jev:
    model = "jev-1.14.0"

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.asked: list[Mapping[str, Any]] = []

    def __enter__(self) -> _Jev:
        return self

    def __exit__(self, *ignored: object) -> None:
        return None

    def ask(self, state: object, questions: Mapping[str, Any]) -> dict[str, Any]:
        self.asked.append(questions)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDecisions]:
    server = FakeDecisions().start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-value")
    yield server
    server.stop()


def _jev(monkeypatch: pytest.MonkeyPatch, *script: Any) -> _Jev:
    session = _Jev(list(script))
    monkeypatch.setattr(cli, "JevSession", lambda: session)
    return session


def test_a_jev_outage_takes_decisions_and_records_the_backend_and_model(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.choices.update(size="small", template="standard")
    held = cli._classify_plan(_plan())
    assert (held.backend, held.model, held.size_bucket, held.template) == (
        "decisions",
        "gpt-6-luna",
        "small",
        "standard",
    )
    assert held.degraded_from == "TypeSafe answered 503" and [row.path for row in held.files] == ["src/a.py"]
    assert held.usage["input_tokens"] == 200 and len(fake.requests) == 2


def test_decisions_scores_files_in_one_batched_request(monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions) -> None:
    _jev(monkeypatch, JevError(None, "the judgment service is unreachable"))
    fake.choices.update(size="medium", template="deep", impact="high")
    fake.level = 2
    held = cli._classify_plan(_plan())
    assert [(row.path, row.impact, row.score) for row in held.files] == [("src/a.py", "high", 3)]
    assert len(fake.requests) == 2 and len(fake.requests[1]["questions"]) == 2


def test_a_jev_deadline_or_a_malformed_answer_takes_the_same_ladder(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    bad = _pr_answers("colossal")
    _jev(monkeypatch, bad)
    assert cli._classify_plan(_plan()).backend == "decisions"
    _jev(monkeypatch, {"size": _pr_answers()["size"]})
    assert cli._classify_plan(_plan()).backend == "decisions"
    assert len(fake.requests) == 4


def test_a_partial_jev_outage_keeps_the_change_answer_and_takes_the_rows_from_decisions(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, _pr_answers("large", "deep"), JevError(503, "down"))
    fake.choices.update(impact="high")
    held = cli._classify_plan(_plan())
    assert (held.backend, held.model, held.size_bucket) == ("jev", "jev-1.14.0", "large")
    assert (held.file_backend, held.file_model) == ("decisions", "gpt-6-luna")
    assert [row.path for row in held.files] == ["src/a.py"] and len(fake.requests) == 1
    assert held.degraded_from == "TypeSafe answered 503"


def test_a_partial_jev_outage_with_a_failing_decisions_keeps_the_change_answer_without_rows(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, _pr_answers("large", "deep"), JevError(503, "down"))
    fake.script = ["status:500"]
    held = cli._classify_plan(_plan())
    assert (held.backend, held.size_bucket, held.files) == ("jev", "large", ())
    assert held.degraded_from == "TypeSafe answered 503; Decisions answered 500"


def test_decisions_floors_and_size_floor_apply_to_its_answer(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.choices.update(size="trivial", template="standard")
    plan = _plan(
        files=[
            {"path": "src/big.py", "additions": 1600, "deletions": 0, "status": "added"},
            {"path": "data/x.json", "additions": 9000, "deletions": 0, "status": "added"},
        ],
        classifier_size_floor=SIZE_RULES,
    )
    held = cli._classify_plan(plan)
    assert held.size_bucket == "large" and held.floors["size_lines"] == 1600
    _jev(monkeypatch, JevError(503, "down"))
    plan["reviewed_files"] = [{"path": "data/x.json", "additions": 9000, "deletions": 0, "status": "added"}]
    assert cli._classify_plan(plan).size_bucket == "trivial"
    _jev(monkeypatch, JevError(503, "down"))
    held = cli._classify_plan({**plan, "classifier_fallback_min_bucket": "medium"})
    assert held.size_bucket == "medium" and held.floors["raised_by"] == ["min_bucket"]

    _jev(monkeypatch, JevError(503, "down"))
    plan["reviewed_files"] = [
        {"path": "data/x.json", "previous_path": "src/x.py", "additions": 1600, "deletions": 0, "status": "renamed"}
    ]
    assert cli._classify_plan(plan).size_bucket == "large"


def test_a_gate_path_gets_large_and_the_gate_template_from_decisions(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.choices.update(size="trivial", template="standard")
    held = cli._classify_plan(
        _plan(files=[{"path": "gate/x.py", "additions": 1, "deletions": 0, "status": "modified"}])
    )
    assert (held.size_bucket, held.template, held.gate_flags) == ("large", "gate", ("gate/x.py",))


@pytest.mark.parametrize("mode", [mode for mode in MALFORMED if mode not in ("level", "score-range")])
def test_a_refusal_or_malformed_change_level_answer_ends_at_the_unavailable_bucket(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, mode: str
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.script = [mode]
    held = cli._classify_plan(_plan())
    assert (held.backend, held.size_bucket, held.template) == ("unavailable", "max", "deep")
    assert held.degraded_from.startswith("TypeSafe answered 503; Decisions ")
    assert len(fake.requests) == 1


def test_a_failing_per_file_batch_preserves_the_larger_answered_size_bucket(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.choices.update(size="huge", template="deep")
    fake.script = ["ok", "refusal"]
    held = cli._classify_plan(_plan(classifier_unavailable_bucket="small"))
    assert (held.backend, held.size_bucket, held.template) == ("unavailable", "huge", "deep")
    assert held.files == ()
    assert len(fake.requests) == 2

    _jev(monkeypatch, JevError(503, "down"))
    fake.choices.update(size="small", template="deep")
    fake.script = ["ok", "status:429"]
    held = cli._classify_plan(_plan(classifier_unavailable_bucket="large"))
    assert (held.backend, held.size_bucket, held.template) == ("unavailable", "large", "deep")
    assert held.files == ()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_decisions_http_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, status: int
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.script = [f"status:{status}"]
    held = cli._classify_plan(_plan())
    assert held.backend == "unavailable" and held.degraded_from.endswith(f"Decisions answered {status}")
    assert len(fake.requests) == 1


def test_a_refused_key_is_an_error_annotation_and_the_unavailable_bucket(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, capsys: pytest.CaptureFixture[str]
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    fake.script = ["status:401"]
    assert cli._classify_plan(_plan()).backend == "unavailable"
    assert "::error::" in capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_jev_client_error_or_missing_key_never_calls_decisions(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, status: int
) -> None:
    _jev(monkeypatch, JevError(status, "rejected"))
    with pytest.raises(JevError):
        cli._classify_plan(_plan())
    _jev(monkeypatch, JevError(None, "no key", missing_key=True))
    with pytest.raises(JevError):
        cli._classify_plan(_plan())
    assert fake.requests == []


def test_without_the_fallback_policy_a_jev_outage_still_raises(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions
) -> None:
    _jev(monkeypatch, JevError(503, "down"))
    with pytest.raises(JevError):
        cli._classify_plan(_plan(classifier_fallback=None))
    assert fake.requests == []


def _select(tmp_path: Path, plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    args = argparse.Namespace(
        plan=str(tmp_path / "plan.json"),
        attempts_dir=str(tmp_path / "attempts"),
        slot=1,
        classification=str(tmp_path / "classification.json"),
        workspace=None,
        classify_only=True,
    )
    assert cli.command_select(args) == 0
    path = tmp_path / "classification.json"
    summary = tmp_path / "summary.md"
    return (
        json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {},
        summary.read_text(encoding="utf-8") if summary.is_file() else "",
    )


def test_no_key_during_a_jev_outage_warns_and_uses_the_unavailable_bucket(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    _jev(monkeypatch, JevError(503, "down"))
    held, summary = _select(tmp_path, _plan(), monkeypatch)
    assert (held["backend"], held["size_bucket"]) == ("unavailable", "max")
    assert held["degraded_from"] == "TypeSafe answered 503; Decisions unkeyed"
    assert "OPENAI_API_KEY is empty" in summary and fake.requests == []


def test_no_key_with_a_working_jev_is_a_normal_run_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    _jev(monkeypatch, _pr_answers("small", "standard"), _file_answers())
    held, summary = _select(tmp_path, _plan(), monkeypatch)
    assert (held["backend"], held["size_bucket"]) == ("jev", "small")
    assert "degraded_from" not in held and "OPENAI_API_KEY is empty" in summary


def test_a_key_and_a_working_jev_make_no_decisions_request_and_no_warning(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, tmp_path: Path
) -> None:
    _jev(monkeypatch, _pr_answers("small", "standard"), _file_answers())
    held, summary = _select(tmp_path, _plan(), monkeypatch)
    assert held["backend"] == "jev" and fake.requests == [] and "OPENAI_API_KEY" not in summary


def test_the_policy_without_a_fallback_never_warns_about_the_key(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDecisions, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    _jev(monkeypatch, _pr_answers("small", "standard"), _file_answers())
    _, summary = _select(tmp_path, _plan(classifier_fallback=None), monkeypatch)
    assert "OPENAI_API_KEY" not in summary
