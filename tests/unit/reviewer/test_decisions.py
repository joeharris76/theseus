from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from tests.integration.fake_decisions import MALFORMED, FakeDecisions

from theseus_reviewer.classify import (
    FILE_IMPACT_QUESTION,
    FILE_SCORE_QUESTION,
    PR_QUESTIONS,
    SIZE_QUESTION,
    decisions_batch,
)
from theseus_reviewer.decisions import FIXED_DATA_NOTICE, DecisionsError, DecisionsSession

pytestmark = [pytest.mark.unit, pytest.mark.fast]

STATE = {"text": "PR #7 in o/n: 2 files"}


@pytest.fixture
def fake() -> Iterator[FakeDecisions]:
    server = FakeDecisions().start()
    yield server
    server.stop()


def _session(fake: FakeDecisions, **options: Any) -> DecisionsSession:
    return DecisionsSession(base_url=fake.url, api_key="sk-test", **options)


def test_the_request_maps_questions_to_choices_and_levels(fake: FakeDecisions) -> None:
    questions = {**PR_QUESTIONS, "importance": FILE_SCORE_QUESTION}
    with _session(fake) as session:
        session.ask(STATE, questions)
    [body] = fake.requests
    assert body["model"] == "gpt-6-luna"
    assert body["input"][0] == {"role": "developer", "content": FIXED_DATA_NOTICE}
    assert body["input"][1] == {"role": "user", "content": [{"type": "input_json", "data": STATE}]}
    by_name = {item["name"]: item for item in body["questions"]}
    assert list(by_name) == ["size", "capability", "template", "importance"]
    assert [choice["value"] for choice in by_name["size"]["choices"]] == list(SIZE_QUESTION["criteria"])
    assert by_name["size"]["choices"][0]["description"] == SIZE_QUESTION["criteria"]["trivial"]
    assert [level["label"] for level in by_name["importance"]["levels"]] == [
        "mechanical",
        "skim",
        "careful",
        "adversarial",
        "critical",
    ]
    assert fake.authorizations == ["Bearer sk-test"]


def test_answers_come_back_in_the_jev_shape_with_zero_based_scores(fake: FakeDecisions) -> None:
    fake.choices["size"] = "large"
    fake.level = 3
    with _session(fake) as session:
        answers = session.ask(STATE, {"size": SIZE_QUESTION, "importance": FILE_SCORE_QUESTION})
    size = answers["size"]
    assert size["choice"] == "large" and size["probabilities"]["large"] == pytest.approx(0.9)
    assert size["confidence"] == pytest.approx((0.9 - 1 / 6) / (1 - 1 / 6))
    score = answers["importance"]
    assert score["score"] == 3.0 and set(score["probabilities"]) == {"0", "1", "2", "3", "4"}
    assert session.model == "gpt-6-luna"
    assert session.usage == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "requests": 1,
    }


def test_the_batch_stays_within_the_question_limit(fake: FakeDecisions) -> None:
    stats = [(f"p{index}.py", index + 1, 0, "modified") for index in range(60)]
    state, questions, paths = decisions_batch(stats)
    assert len(paths) == 40 and len(questions) == 80
    with _session(fake) as session:
        answers = session.ask(state, questions)
    assert len(answers) == 80 and len(fake.requests[0]["questions"]) == 80
    assert fake.requests[0]["questions"][0]["instructions"].startswith("Consider only the file with index 1 ")
    assert all(path not in str(fake.requests[0]["questions"]) for path in paths[:5])


@pytest.mark.parametrize("mode", MALFORMED)
def test_every_malformed_answer_class_is_rejected(fake: FakeDecisions, mode: str) -> None:
    fake.script = [mode]
    questions = {"size": SIZE_QUESTION, "capability": PR_QUESTIONS["capability"], "importance": FILE_SCORE_QUESTION}
    with _session(fake) as session, pytest.raises(DecisionsError) as caught:
        session.ask(STATE, questions)
    assert caught.value.status is None and not caught.value.missing_key
    assert len(fake.requests) == 1


def test_a_refusal_names_the_question(fake: FakeDecisions) -> None:
    fake.script = ["refusal"]
    with _session(fake) as session, pytest.raises(DecisionsError, match="refused size"):
        session.ask(STATE, {"size": SIZE_QUESTION})


def test_a_dated_model_snapshot_of_the_requested_model_is_accepted(fake: FakeDecisions) -> None:
    fake.model = "gpt-6-luna-2026-11-01"
    with _session(fake) as session:
        session.ask(STATE, {"size": SIZE_QUESTION})
        assert session.model == "gpt-6-luna-2026-11-01"


@pytest.mark.parametrize(
    "model", ["gpt-6-luna-mini", "gpt-6-luna-nano", "gpt-6-luna-2026-11", "gpt-6-luna-2026-11-01-x", "gpt-6"]
)
def test_other_models_are_rejected(fake: FakeDecisions, model: str) -> None:
    fake.model = model
    with _session(fake) as session, pytest.raises(DecisionsError, match="unexpected model"):
        session.ask(STATE, {"size": SIZE_QUESTION})


def test_the_exact_model_is_accepted(fake: FakeDecisions) -> None:
    with _session(fake) as session:
        session.ask(STATE, {"size": SIZE_QUESTION})
        assert session.model == "gpt-6-luna"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_errors_carry_the_status_and_are_not_retried(fake: FakeDecisions, status: int) -> None:
    fake.script = [f"status:{status}"]
    with _session(fake) as session, pytest.raises(DecisionsError) as caught:
        session.ask(STATE, {"size": SIZE_QUESTION})
    assert caught.value.status == status and caught.value.is_auth == (status in (401, 403))
    assert caught.value.message == f"Decisions answered {status}"
    assert len(fake.requests) == 1


def test_an_unreachable_service_is_an_error() -> None:
    with (
        DecisionsSession(base_url="http://127.0.0.1:9", api_key="k", timeout_seconds=5) as session,
        pytest.raises(DecisionsError, match="unreachable"),
    ):
        session.ask(STATE, {"size": SIZE_QUESTION})


def test_a_slow_service_times_out(fake: FakeDecisions) -> None:
    fake.delay = 3
    with _session(fake, timeout_seconds=0.5) as session, pytest.raises(DecisionsError, match="unreachable"):
        session.ask(STATE, {"size": SIZE_QUESTION})


def test_the_total_deadline_stops_the_second_request(fake: FakeDecisions) -> None:
    ticks = iter([0.0, 61.0])
    with _session(fake, clock=lambda: next(ticks)) as session:
        session.ask(STATE, {"size": SIZE_QUESTION})
        with pytest.raises(DecisionsError, match="deadline"):
            session.ask(STATE, {"size": SIZE_QUESTION})
    assert len(fake.requests) == 1


def test_a_missing_key_fails_before_any_request(fake: FakeDecisions, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DecisionsError, match="unkeyed") as caught, DecisionsSession(base_url=fake.url):
        pass
    assert caught.value.missing_key and fake.requests == []


def test_too_many_questions_are_refused_before_sending(fake: FakeDecisions) -> None:
    questions = {f"impact.{index}": FILE_IMPACT_QUESTION for index in range(401)}
    with _session(fake) as session, pytest.raises(DecisionsError, match="1-400"):
        session.ask(STATE, questions)
    assert fake.requests == []
