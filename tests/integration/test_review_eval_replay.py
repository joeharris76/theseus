from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .harness import GATEWAY_TOKEN, JEV_KEY, POLICY, TRUSTED_FILES, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

ROOT = Path(__file__).resolve().parents[2]


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_eval", ROOT / "tools" / "review_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _tool()
CASES = TOOL.load_cases(ROOT)


def _observe(harness: Harness, case: Any, variant: str) -> dict[str, Any]:
    branch = f"eval/{case.id}"
    if case.base:
        harness.write(dict(case.base), f"base of {case.id}")
    harness.branch(branch, dict(case.head), case.message)
    harness.script_omp([dict(reply) for reply in case.fake[variant]])
    review = harness.local_review(branch)
    assert review.code in (0, 1), review.stdout + review.stderr
    outcome = TOOL.parse_outcome(harness.local_state)
    assert outcome is not None, review.stdout + review.stderr
    return TOOL.observation_from_outcome(outcome)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_recorded_good_review_of_every_case_scores_one(harness: Harness, case_id: str) -> None:
    case = CASES[case_id]

    observation = _observe(harness, case, "good")
    score = TOOL.score_observation(case, observation)

    assert score["score"] == 1.0, (observation, score)
    assert score["missed"] == [] and score["noise"] == 0


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_recorded_regressed_review_of_every_case_scores_lower(harness: Harness, case_id: str) -> None:
    case = CASES[case_id]

    score = TOOL.score_observation(case, _observe(harness, case, "regressed"))

    assert score["score"] < 1.0
    if case.findings:
        assert score["recall"] == 0.0 and score["decision"] == 0.0
    else:
        assert score["precision"] == 0.0 and score["noise"] == 1


def test_a_regressed_corpus_run_lowers_the_aggregate_the_change_gate_compares(tmp_path: Path) -> None:
    good = {case_id: [_scripted(case, "good")] for case_id, case in CASES.items()}
    regressed = {case_id: [_scripted(case, "regressed")] for case_id, case in CASES.items()}
    meta = {"date": "2026-11-08", "git_sha": "0" * 40, "dirty": False, "config_digest": "a", "corpus_digest": "b"}

    before = TOOL.build_result(CASES, good, mode="live", meta=meta)
    after = TOOL.build_result(CASES, regressed, mode="live", meta=meta)

    assert before["aggregate"]["score"] == 1.0
    assert after["aggregate"]["score"] < before["aggregate"]["score"]
    assert TOOL.rescored(after, CASES)["score"] == after["aggregate"]["score"]


def _scripted(case: Any, variant: str) -> dict[str, Any]:
    reply = case.fake[variant][0]
    return {
        "status": TOOL.REVIEWED,
        "decision": reply["decision"],
        "reviewer": "scripted",
        "summary": reply["summary"],
        "findings": [dict(defect) for defect in reply.get("defects", [])],
    }


def _consumer(harness: Harness, directory: Path) -> Path:
    for relative in (*TRUSTED_FILES, POLICY):
        source = harness.trusted / relative
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    shutil.rmtree(directory / ".github" / "workflows")
    return directory


@pytest.mark.parametrize("case_id", ["pr41-forced-fixed-dispute", "control-private-helper-rename"])
def test_the_live_runner_builds_the_case_repository_and_reads_the_engine_outcome(
    harness: Harness, tmp_path: Path, case_id: str
) -> None:
    case = CASES[case_id]
    skeleton = TOOL.skeleton_files(_consumer(harness, tmp_path / "consumer"))
    harness.script_omp([dict(reply) for reply in case.fake["good"]])
    environment = {
        **harness.base_env(),
        "REVIEW_GATEWAY_URL": harness.gateway.url,
        "REVIEW_CLOUD_GATEWAY_TOKEN": GATEWAY_TOKEN,
        "TYPESAFE_BASE_URL": harness.jev.url,
        "TYPESAFE_API_KEY": JEV_KEY,
    }

    observation = TOOL.run_live_case(case, skeleton, ROOT, 300, environment)

    assert observation["status"] == TOOL.REVIEWED, observation
    assert TOOL.score_observation(case, observation)["score"] == 1.0
    assert harness.omp_calls() == 1
