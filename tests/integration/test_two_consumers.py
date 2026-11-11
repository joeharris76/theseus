from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tests.conftest import CONSUMER_BETA

from theseus_reviewer._vendor import yaml

from . import fake_github
from .fake_github import APP_TOKEN, OWNER_TOKEN
from .harness import CHECKER, POLICY, SLUG, Harness

pytestmark = [pytest.mark.integration, pytest.mark.medium]

BETA_TOKEN = "gho_betaowner" + "B" * 27
FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"


@pytest.fixture
def beta_trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setitem(fake_github.ACCOUNTS, BETA_TOKEN, {"login": "beta-owner", "type": "User"})
    root = tmp_path / "beta-trusted"
    shutil.copytree(CONSUMER_BETA, root)
    policy = root / POLICY
    data = yaml.safe_load(policy.read_text(encoding="utf-8"))
    data["consumer"]["gated_base"] = "develop"
    policy.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return root


def _gate(harness: Harness, root: Path, pr: int, head: str):
    env = {**harness.base_env(), "GITHUB_TOKEN": APP_TOKEN, "GH_TOKEN": APP_TOKEN}
    args = [*harness_package(), "gate", "--repo", SLUG, "--pr", str(pr), "--policy", POLICY, "--root", str(root)]
    return harness.run("gate-" + root.name, [*args, "--head-sha", head], env)


def harness_package() -> list[str]:
    from .harness import PACKAGE

    return list(PACKAGE)


def _comment(harness: Harness, pr: int, body: str, token: str) -> None:
    harness.gh(
        "comment",
        ["api", "--method", "POST", f"repos/{SLUG}/issues/{pr}/comments", "-f", f"body={body}"],
        harness.base_env(),
        token,
    )


def test_a_verdict_from_one_consumers_app_does_not_satisfy_the_other_consumers_gate(
    harness: Harness, beta_trusted: Path
) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(41, "feat/checker")
    harness.script_omp([{"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}])
    harness.review(41)

    alpha = harness.gate(41, head)
    assert alpha.code == 0, alpha.stderr
    assert harness.github.gate_statuses(head)[-1]["state"] == "success"

    beta = _gate(harness, beta_trusted, 41, head)
    assert beta.code == 3, beta.stderr
    refused = harness.github.gate_statuses(head)[-1]
    assert refused["state"] == "pending"
    assert "waiting for the review's verdict" in refused["description"]


def test_a_standin_from_one_consumers_owner_is_rejected_by_the_other(harness: Harness, beta_trusted: Path) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(42, "feat/checker")
    _comment(harness, 42, f"Stand-in review: APPROVE {head}", OWNER_TOKEN)

    alpha = harness.gate(42, head)
    assert alpha.code == 0, alpha.stderr
    assert "stand-in review attested by joeharris76" in harness.github.gate_statuses(head)[-1]["description"]

    beta = _gate(harness, beta_trusted, 42, head)
    assert beta.code == 3, beta.stderr
    assert "waiting for the review's verdict" in harness.github.gate_statuses(head)[-1]["description"]

    _comment(harness, 42, f"Stand-in review: APPROVE {head}", BETA_TOKEN)
    accepted = _gate(harness, beta_trusted, 42, head)
    assert accepted.code == 0, accepted.stderr
    assert "stand-in review attested by beta-owner" in harness.github.gate_statuses(head)[-1]["description"]


def test_a_consumer_gates_only_its_own_base_branch(harness: Harness, beta_trusted: Path) -> None:
    policy = beta_trusted / POLICY
    data = yaml.safe_load(policy.read_text(encoding="utf-8"))
    data["consumer"]["gated_base"] = "main"
    policy.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(43, "feat/checker")

    ungated = _gate(harness, beta_trusted, 43, head)

    assert ungated.code == 0, ungated.stderr
    assert "targets develop, not main" in ungated.stdout
    assert harness.github.gate_statuses(head) == []


def test_a_gate_without_consumer_configuration_fails_instead_of_defaulting(harness: Harness, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    (bare / ".github").mkdir(parents=True)
    data = yaml.safe_load((CONSUMER_BETA / POLICY).read_text(encoding="utf-8"))
    data.pop("consumer")
    (bare / POLICY).write_text(yaml.safe_dump(data), encoding="utf-8")
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(44, "feat/checker")

    result = _gate(harness, bare, 44, head)

    assert result.code == 2
    assert "consumer section" in result.stderr
    assert [status["state"] for status in harness.github.gate_statuses(head)] == ["failure"]
