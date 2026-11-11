from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import providers
from theseus_reviewer._vendor import yaml

from .harness import ALPHA_ROOT, CHECKER, POLICY, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"
SHIP = {"decision": "SHIP", "summary": "The change keeps checker semantics; no defects.", "files_examined": [CHECKER]}
CONFIG = yaml.safe_load((ALPHA_ROOT / ".github" / "review-omp-config.yml").read_text(encoding="utf-8"))
POLICY_DATA = yaml.safe_load((ALPHA_ROOT / POLICY).read_text(encoding="utf-8"))
PROVIDERS = providers.routed(CONFIG)
LOSS_BUCKETS = ("trivial", "medium", "max")
SOLE_BUCKETS = ("trivial", "max")
HEAD_CONFIG = ".github/review-omp-config.yml"


def _options(bucket: str) -> list[str]:
    return providers.candidates(POLICY_DATA["reviewers"][POLICY_DATA["buckets"][bucket]]["model"], CONFIG)


def _reviewed(harness: Harness, pr: int, bucket: str, replies: list[dict[str, Any]] | None = None, **script: Any):
    head = harness.branch(f"feat/loss-{pr}", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(pr, f"feat/loss-{pr}")
    harness.script_omp(replies or [SHIP], **script)
    return head, harness.review(pr, classification=(bucket, "standard"))


def _ran(harness: Harness) -> list[str]:
    return [call["model"] for call in harness.omp_log()]


def _status(harness: Harness, head: str) -> dict[str, Any]:
    [status] = harness.github.review_statuses(head)
    return status


@pytest.mark.parametrize("bucket", LOSS_BUCKETS)
@pytest.mark.parametrize("lost", PROVIDERS)
def test_losing_any_one_provider_still_reviews_every_bucket_on_a_served_candidate(
    harness: Harness, lost: str, bucket: str
) -> None:
    harness.gateway.serve_all_but(lost)
    options = _options(bucket)
    expected = next(item for item in options if providers.provider_of(item) != lost)

    head, run = _reviewed(harness, 301, bucket)

    assert _ran(harness) == [expected]
    assert run.plan["served"]["providers"] == sorted(set(PROVIDERS) - {lost})
    status = _status(harness, head)
    assert status["state"] == "success"
    assert (
        status["description"].endswith(f" · degraded: {lost}") or f" · degraded: {lost} [run" in status["description"]
    )
    body = harness.github.reviews[301][-1]["body"]
    assert "Run details" in body and f"not served by the gateway: {lost}" in body
    if expected != options[0]:
        assert f"{options[0]} → {expected}" in body
        assert f"Model that ran: {expected}." in body


@pytest.mark.parametrize("bucket", SOLE_BUCKETS)
@pytest.mark.parametrize("kept", PROVIDERS)
def test_the_last_served_provider_reviews_each_bucket_it_has_a_candidate_for(
    harness: Harness, kept: str, bucket: str
) -> None:
    harness.gateway.serve_only(kept)
    options = _options(bucket)
    expected = next((item for item in options if providers.provider_of(item) == kept), None)

    head, run = _reviewed(harness, 302, bucket)

    status = _status(harness, head)
    if expected is None:
        assert harness.omp_calls() == 0
        assert status["state"] == "pending" and status["description"].startswith("pending: no reviewer available")
        state = harness.github.artifact_json("review-verdict-state-302", "state.json")
        assert state["pending_cause"] == "unavailable" and state["retries"] == []
        assert run.attempts() == []
    else:
        assert _ran(harness) == [expected]
        assert status["state"] == "success"


@pytest.mark.parametrize("mode", ["serve-none", "down", "rejected", "malformed"])
def test_an_unknown_or_empty_served_set_fails_closed_as_unavailable_without_spending_an_attempt(
    harness: Harness, mode: str
) -> None:
    if mode == "serve-none":
        harness.gateway.serve([])
    else:
        harness.gateway.mode = mode

    head, run = _reviewed(harness, 303, "medium")

    status = _status(harness, head)
    assert status["state"] == "pending"
    assert status["description"].startswith("pending: no reviewer available")
    assert harness.omp_calls() == 0 and run.attempts() == []
    state = harness.github.artifact_json("review-verdict-state-303", "state.json")
    assert state["pending_cause"] == "unavailable" and state["retries"] == []
    assert "success" not in [item["state"] for item in harness.github.gate_statuses(head)]
    detail = {
        "serve-none": "unavailable",
        "down": "gateway answered 503",
        "rejected": "client token rejected",
        "malformed": "malformed model list",
    }[mode]
    assert detail in harness.github.reviews[303][-1]["body"] or detail in status["description"]


@pytest.mark.parametrize("failure", [(401, "Unauthorized"), (404, "Unknown model"), (503, "upstream overloaded")])
def test_a_listed_provider_that_fails_at_request_time_is_excluded_and_its_reviewer_runs_a_substitute(
    harness: Harness, failure: tuple[int, str]
) -> None:
    status_code, message = failure
    dead = {"anthropic": {"status": status_code, "message": message}}

    head, run = _reviewed(harness, 304, "large", [SHIP, SHIP], dead=dead)

    options = _options("large")
    substitute = next(item for item in options if providers.provider_of(item) != "anthropic")
    assert _ran(harness) == [options[0], substitute]
    assert [item["outcome"] for item in run.attempts()] == ["absent", "verdict"]
    assert _status(harness, head)["state"] == "success"
    body = harness.github.reviews[304][-1]["body"]
    assert "excluded after anthropic failed" in body and "large (attempt 1)" in body
    assert f"{options[0]} → {substitute}" in body


def test_every_provider_failing_at_request_time_ends_all_absent_after_the_candidates_are_spent(
    harness: Harness,
) -> None:
    dead = {name: {"status": 503, "message": "down"} for name in PROVIDERS}

    head, run = _reviewed(harness, 305, "large", [SHIP], dead=dead)

    assert len(_ran(harness)) == len(_options("large"))
    status = _status(harness, head)
    assert status["state"] == "pending"
    assert harness.github.artifact_json("review-verdict-state-305", "state.json")["pending_cause"] == "all-absent"


@pytest.mark.parametrize("edit", ["drop-primary-provider", "add-provider"])
def test_a_head_that_edits_the_omp_config_never_changes_what_the_review_selects(harness: Harness, edit: str) -> None:
    harness.gateway.serve_all_but("anthropic")
    config = json.loads(json.dumps(CONFIG))
    if edit == "drop-primary-provider":
        config["modelRoles"] = {
            role: target for role, target in config["modelRoles"].items() if "anthropic" not in target
        }
        config["retry"]["fallbackChains"] = {
            leader: chain for leader, chain in config["retry"]["fallbackChains"].items() if "anthropic" not in leader
        }
    else:
        config["modelRoles"]["default"] = "openai-codex/gpt-6-astra:high"
    head = harness.branch("feat/loss-306", {HEAD_CONFIG: yaml.safe_dump(config)}, "edit the omp config")
    harness.github.open_pull(306, "feat/loss-306")
    harness.script_omp([SHIP])

    run = harness.review(306, classification=("large", "standard"))

    assert run.plan["candidates"]["large"] == _options("large")
    assert run.plan["routed"] == list(PROVIDERS)
    assert _ran(harness) == [next(item for item in _options("large") if not item.startswith("anthropic"))]
    assert _status(harness, head)["state"] in {"success", "pending", "failure"}


def _health(harness: Harness, tmp_path: Path) -> Any:
    temp = tmp_path / "health"
    temp.mkdir()
    env = {
        **harness.base_env(temp),
        "OMP_GATEWAY_TOKEN": harness.gateway.token,
        "REVIEW_GATEWAY_URL": harness.gateway.url,
        "GITHUB_RUN_ID": "1",
    }
    return harness.cli("health", ["drive", "--policy", POLICY, "health"], env)


def test_the_health_phase_passes_only_when_every_configured_provider_is_served(
    harness: Harness, tmp_path: Path
) -> None:
    assert _health(harness, tmp_path).code == 0


@pytest.mark.parametrize("lost", PROVIDERS)
def test_the_health_phase_names_the_provider_the_gateway_stopped_serving(
    harness: Harness, tmp_path: Path, lost: str
) -> None:
    harness.gateway.serve_all_but(lost)

    step = _health(harness, tmp_path)

    assert step.code == 1 and f"not served: {lost}" in step.stdout


@pytest.mark.parametrize(("mode", "text"), [("down", "gateway answered 503"), ("rejected", "client token rejected")])
def test_the_health_phase_fails_when_the_gateway_is_not_answering_the_model_listing(
    harness: Harness, tmp_path: Path, mode: str, text: str
) -> None:
    harness.gateway.mode = mode

    step = _health(harness, tmp_path)

    assert step.code == 1 and text in step.stdout


def test_the_health_phase_fails_when_the_gateway_is_unreachable(harness: Harness, tmp_path: Path) -> None:
    harness.gateway.stop()

    step = _health(harness, tmp_path)

    assert step.code == 1 and "gateway unreachable" in step.stdout


def test_a_local_review_on_a_degraded_gateway_reports_the_degradation_and_the_model_that_ran(
    harness: Harness,
) -> None:
    harness.branch("feat/local", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.gateway.serve_all_but("anthropic")
    harness.jev.classification = ("medium", "standard")
    harness.script_omp([SHIP])

    review = harness.local_review("feat/local")

    assert review.code == 0, review.stdout + review.stderr
    assert "degraded: anthropic" in review.stdout
    assert [model.split(":")[0] for model in _ran(harness)] == ["openai-codex/gpt-6.1-sol"]
    assert "reviewed by openai-codex/gpt-6.1-sol" in review.stdout


@pytest.mark.parametrize("mode", ["down", "rejected"])
def test_a_local_review_with_nothing_servable_exits_2_and_names_an_availability_cause(
    harness: Harness, tmp_path: Path, mode: str
) -> None:
    harness.branch("feat/local", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.gateway.mode = mode
    out = tmp_path / "unavailable.txt"

    review = harness.local_review("feat/local", env={"REVIEW_LOCAL_UNAVAILABLE_OUT": str(out)})

    assert review.code == 2, review.stdout + review.stderr
    assert out.read_text(encoding="utf-8").startswith("availability: ")
    assert harness.omp_calls() == 0


def test_a_local_review_unavailable_for_another_reason_leaves_the_availability_file_empty(
    harness: Harness, tmp_path: Path
) -> None:
    harness.branch("feat/local", {CHECKER: FIXED_CHECKER}, "guard None operands")
    out = tmp_path / "unavailable.txt"

    review = harness.local_review("feat/local", key=False, env={"REVIEW_LOCAL_UNAVAILABLE_OUT": str(out)})

    assert review.code == 2
    assert out.read_text(encoding="utf-8") == ""


def test_the_probe_never_puts_the_gateway_token_in_a_published_surface(harness: Harness) -> None:
    harness.gateway.mode = "rejected"

    head, run = _reviewed(harness, 307, "medium")

    published = json.dumps(harness.github.reviews[307]) + json.dumps(harness.github.statuses[head])
    assert harness.gateway.token not in published and harness.gateway.url not in published
    assert os.environ.get("OMP_GATEWAY_TOKEN") is None


HIGH = {
    "severity": "High",
    "file": CHECKER,
    "line": 2,
    "title": "Equality ignores the operand types",
    "detail": "checker treats 1 and True as equal, so a boolean column matches an integer column.",
}


def _recorded_high(harness: Harness) -> None:
    harness.branch("feat/local", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.script_omp(
        [{"decision": "SHIP_WITH_FIXES", "summary": "One defect.", "files_examined": [CHECKER], "defects": [HIGH]}]
    )
    assert harness.local_review("feat/local").code == 1


@pytest.mark.parametrize("mode", ["down", "rejected"])
def test_open_blocking_findings_keep_an_unavailable_review_from_being_an_availability_cause(
    harness: Harness, tmp_path: Path, mode: str
) -> None:
    _recorded_high(harness)
    harness.gateway.mode = mode
    out = tmp_path / "unavailable.txt"

    review = harness.local_review(
        "feat/local",
        rebuttals={"c1-D1": f"Dispute: {CHECKER}:2 both operands are guarded before the comparison"},
        env={"REVIEW_LOCAL_UNAVAILABLE_OUT": str(out)},
    )

    assert review.code == 2, review.stdout + review.stderr
    assert out.read_text(encoding="utf-8") == ""
