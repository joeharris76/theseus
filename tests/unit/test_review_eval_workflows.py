from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / ".github" / "workflows" / "review-eval.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
SECRET = re.compile(r"secrets\.(\w+)")
GATEWAY_CLIENT_SECRETS = {
    "REVIEW_GATEWAY_URL",
    "REVIEW_GATEWAY_TOKEN",
    "TS_OAUTH_CI_CLIENT_ID",
    "TS_OAUTH_CI_SECRET",
    "TYPESAFE_API_KEY",
}


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_the_weekly_eval_is_schedule_only_with_read_only_contents() -> None:
    document = _load(EVAL)

    assert set(document["on"]) == {"schedule"}
    assert len(document["on"]["schedule"]) == 1 and len(document["on"]["schedule"][0]["cron"].split()) == 5
    assert document["permissions"] == {"contents": "read"}
    assert [body["permissions"] for body in document["jobs"].values()] == [{"contents": "read"}]


def test_the_weekly_eval_reads_only_the_gateway_client_and_classifier_secrets_from_its_own_environment() -> None:
    document = _load(EVAL)
    [job] = document["jobs"].values()

    assert job["environment"] == "review-eval"
    assert set(SECRET.findall(EVAL.read_text(encoding="utf-8"))) == GATEWAY_CLIENT_SECRETS
    assert "secrets: inherit" not in EVAL.read_text(encoding="utf-8")
    assert "id-token" not in EVAL.read_text(encoding="utf-8")


def test_the_weekly_eval_checks_out_without_credentials_and_hands_secrets_only_to_the_run_step() -> None:
    [job] = _load(EVAL)["jobs"].values()

    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    holders = [step for step in job["steps"] if "REVIEW_CLOUD_GATEWAY_TOKEN" in step.get("env", {})]
    assert len(holders) == 1 and "tools/review_eval.py run --mode live" in holders[0]["run"]
    assert "TYPESAFE_API_KEY" not in job.get("env", {})


def test_the_weekly_eval_keeps_its_result_without_writing_to_the_repository() -> None:
    [job] = _load(EVAL)["jobs"].values()

    uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert [step["with"]["name"] for step in uploads] == ["review-eval-result"]
    text = EVAL.read_text(encoding="utf-8")
    assert "git push" not in text and "contents: write" not in text and "gh pr" not in text


def test_the_ci_job_validates_the_corpus_and_runs_the_score_rule_from_the_base_copy_of_the_tool() -> None:
    job = _load(CI)["jobs"]["review-eval"]

    assert job["name"] == "review eval corpus and score rule"
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 2 and checkout["with"]["persist-credentials"] is False
    commands = {step["name"]: step for step in job["steps"] if "run" in step}
    assert commands["Validate the eval corpus"]["run"] == "python3 tools/review_eval.py validate"
    rule = commands["Refuse a template, policy or guidance change that lowers the eval score"]
    assert rule["if"] == "github.event_name == 'pull_request'"
    assert 'git show "$base:$tool"' in rule["run"] and "check-change" in rule["run"]
    assert not any("secrets." in str(step) for step in job["steps"])


def test_the_eval_tool_and_corpus_are_soundness_and_gate_paths() -> None:
    manifest = (ROOT / ".github" / "soundness-paths.txt").read_text(encoding="utf-8")
    policy = (ROOT / ".github" / "review.yml").read_text(encoding="utf-8")

    assert "prefix\tevals/" in manifest and "file\ttools/review_eval.py" in manifest
    assert "    - tools/review_eval.py\n" in policy and "    - evals/*\n" in policy
