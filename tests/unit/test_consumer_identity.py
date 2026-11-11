from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import ALPHA_POLICY, BETA_POLICY, CONSUMER_ALPHA, CONSUMER_BETA

from theseus_reviewer import gate, github, omp_bump_gate, review_check, status_text
from theseus_reviewer._vendor import yaml
from theseus_reviewer.policy import PolicyError, load_policy, parse_policy
from theseus_reviewer.review_check import WAITING

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "c" * 40
OTHER_HEAD = "d" * 40
ALPHA = {"root": CONSUMER_ALPHA, "bot": "benchbox-review", "owner": "joeharris76", "base": "develop"}
BETA = {"root": CONSUMER_BETA, "bot": "beta-review", "owner": "beta-owner", "base": "main"}
CONSUMERS = {"alpha": ALPHA, "beta": BETA}
POLICY_PATH = ".github/review.yml"


def _config(consumer):
    return gate.load_gate_config(POLICY_PATH, consumer["root"])


def _status(login: str, state: str = "failure", description: str = "refused", created: str = "2026-11-01T10:00:00Z"):
    return {
        "id": 1,
        "context": "review-verdict",
        "state": state,
        "description": description,
        "login": login,
        "user_type": "Bot",
        "created_at": created,
    }


def _comment(login: str, body: str, user_type: str = "User"):
    return {
        "login": login,
        "user_type": user_type,
        "body": body,
        "created_at": "2026-11-01T11:00:00Z",
        "updated_at": "2026-11-01T11:00:00Z",
    }


def _decide(config, statuses, comments):
    matcher = review_check.path_matcher(1, 1, config.matchers, (), ())
    return review_check.decide(HEAD, ["src/engine/join.py"], statuses, matcher, None, comments, binding=config.binding)


def test_each_consumer_loads_its_own_identity():
    alpha, beta = _config(ALPHA), _config(BETA)
    assert (alpha.binding.bot_login, beta.binding.bot_login) == ("benchbox-review", "beta-review")
    assert alpha.binding.attesters == {"joeharris76"}
    assert beta.binding.attesters == {"beta-owner", "beta-deputy"}
    assert (alpha.consumer.gated_bases, beta.consumer.gated_bases) == (("develop",), ("main",))
    assert alpha.matchers.is_soundness_path("benchbox/core/equivalence/x.py")
    assert not beta.matchers.is_soundness_path("benchbox/core/equivalence/x.py")
    assert beta.matchers.is_soundness_path("src/engine/x.py")


@pytest.mark.parametrize("own,other", [("alpha", "beta"), ("beta", "alpha")])
def test_standin_attestation_is_accepted_only_from_the_consumers_own_attesters(own, other):
    config = _config(CONSUMERS[own])
    attestation = f"Stand-in review: APPROVE {HEAD}"
    status, message = _decide(config, [], [_comment(CONSUMERS[own]["owner"], attestation)])
    assert status == review_check.PASS
    assert message == status_text.gate_pass_standin(CONSUMERS[own]["owner"])
    status, message = _decide(config, [], [_comment(CONSUMERS[other]["owner"], attestation)])
    assert status == WAITING
    assert message == status_text.gate_waiting(HEAD)


def test_beta_deputy_is_an_attester_for_beta_only():
    attestation = f"Stand-in review: APPROVE {HEAD}"
    assert _decide(_config(BETA), [], [_comment("beta-deputy", attestation)])[0] == review_check.PASS
    assert _decide(_config(ALPHA), [], [_comment("beta-deputy", attestation)])[0] == WAITING


@pytest.mark.parametrize("own,other", [("alpha", "beta"), ("beta", "alpha")])
def test_verdict_statuses_count_only_from_the_consumers_own_app(own, other):
    config = _config(CONSUMERS[own])
    status, message = _decide(config, [_status(CONSUMERS[own]["bot"])], [])
    assert status == WAITING
    assert message == status_text.GATE_DO_NOT_SHIP
    status, message = _decide(config, [_status(CONSUMERS[other]["bot"])], [])
    assert status == WAITING
    assert message == status_text.gate_waiting(HEAD)


def test_the_app_login_matches_the_bot_account_type_and_suffix():
    config = _config(BETA)
    assert not review_check._is_review("beta-review", "User", config.binding.bot_login)
    assert review_check._is_review("beta-review[bot]", "Bot", config.binding.bot_login)
    assert not review_check._is_review("benchbox-review", "Bot", config.binding.bot_login)


@pytest.mark.parametrize("own,other", [("alpha", "beta"), ("beta", "alpha")])
def test_gate_posts_only_for_the_consumers_gated_base(own, other, monkeypatch):
    config = _config(CONSUMERS[own])

    def pull_for(base: str):
        pull = {
            "base": {"ref": base, "sha": "e" * 40},
            "head": {"sha": HEAD, "repo": {"full_name": "acme/r"}},
            "changed_files": 1,
        }
        return lambda repo, pr, token: (pull, base, HEAD)

    monkeypatch.setattr(gate, "read_pull", pull_for(CONSUMERS[other]["base"]))
    with pytest.raises(gate.UngatedBase, match=f"posted only for {CONSUMERS[own]['base']}"):
        gate.evaluate("acme/r", 7, "token", config)

    monkeypatch.setattr(gate, "read_pull", pull_for(CONSUMERS[own]["base"]))
    monkeypatch.setattr(gate, "fetch_files", lambda *a: [{"filename": "src/engine/join.py"}])
    monkeypatch.setattr(gate, "fetch_statuses", lambda *a: [_status(CONSUMERS[own]["bot"], "failure")])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda *a: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda *a: [])
    monkeypatch.setattr(gate, "annotate_receipt", lambda *a: None)
    monkeypatch.setattr(gate, "fetch_bump_refusals", lambda *a, **k: [])
    monkeypatch.setattr(gate, "fetch_promotion_refusals", lambda *a, **k: (False, []))
    head, status, message = gate.evaluate("acme/r", 7, "token", config)
    assert (head, status) == (HEAD, WAITING)
    assert message == status_text.GATE_DO_NOT_SHIP


def test_gate_passes_the_configured_login_to_receipt_and_promotion_checks(monkeypatch):
    config = _config(BETA)
    seen: dict[str, object] = {}
    pull = {"base": {"ref": "main", "sha": "e" * 40}, "head": {"sha": HEAD}, "changed_files": 1}
    monkeypatch.setattr(gate, "read_pull", lambda *a: (pull, "main", HEAD))
    monkeypatch.setattr(gate, "fetch_files", lambda *a: [{"filename": "src/engine/join.py"}])
    monkeypatch.setattr(gate, "fetch_statuses", lambda *a: [])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda *a: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda *a: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda *a: [])
    monkeypatch.setattr(
        gate, "annotate_receipt", lambda token, reviews, head, login, base_date=None: seen.update(receipt=login)
    )
    monkeypatch.setattr(gate, "fetch_bump_refusals", lambda *a, **k: seen.update(bump=k) or [])
    monkeypatch.setattr(
        gate, "fetch_promotion_refusals", lambda *a, **k: seen.update(promotion=k["review_login"]) or (False, [])
    )
    gate.evaluate("acme/r", 7, "token", config)
    assert seen["receipt"] == "beta-review"
    assert seen["promotion"] == "beta-review"
    assert seen["bump"] == {
        "policy_path": POLICY_PATH,
        "fixture_prefix": "fixtures/omp/",
        "attesters": frozenset({"beta-owner", "beta-deputy"}),
    }


def test_bump_evidence_comes_only_from_the_consumers_attesters():
    line = f"Omp candidate check: version=1.2.3 platform=darwin-arm64 sha256={'1' * 64} pin={'2' * 64} head={HEAD} result=releasable"
    comment = {"login": "beta-owner", "user_type": "User", "body": line, "created_at": "t", "updated_at": "t"}
    assert omp_bump_gate.owner_evidence([comment], "1.2.3", "1" * 64, "2" * 64, HEAD, frozenset({"beta-owner"}))
    assert not omp_bump_gate.owner_evidence([comment], "1.2.3", "1" * 64, "2" * 64, HEAD, frozenset({"joeharris76"}))


def test_bump_refusal_uses_the_consumers_policy_path_and_fixture_prefix():
    base = yaml.safe_dump({"omp": {"version": "1.0.0"}, "x": 1})
    head = yaml.safe_dump({"omp": {"version": "1.0.1"}, "x": 1})
    files = ["config/review.yml", "fixtures/omp/a.jsonl", "docs/unrelated.md"]
    reasons = omp_bump_gate.refusals(
        files,
        base,
        head,
        [],
        HEAD,
        policy_path="config/review.yml",
        fixture_prefix="fixtures/omp/",
        attesters=frozenset({"beta-owner"}),
    )
    assert any("docs/unrelated.md" in reason for reason in reasons)
    assert not any("fixtures/omp/a.jsonl" in reason for reason in reasons)
    assert (
        omp_bump_gate.refusals(
            ["other.yml"],
            base,
            head,
            [],
            HEAD,
            policy_path="config/review.yml",
            fixture_prefix=None,
            attesters=frozenset(),
        )
        == []
    )


def test_owner_instruction_and_adjudication_follow_the_consumers_attesters():
    from theseus_reviewer import adjudication, reset

    owner_comment = {
        "user": {"login": "beta-owner", "type": "User"},
        "body": f"Owner instruction for {HEAD}",
        "created_at": "2026-11-01T10:00:00+00:00",
        "updated_at": "2026-11-01T10:00:00+00:00",
    }
    beta, alpha = _config(BETA).binding.attesters, _config(ALPHA).binding.attesters
    assert reset.has_owner_instruction([owner_comment], HEAD, beta)
    assert not reset.has_owner_instruction([owner_comment], HEAD, alpha)
    ruling = {
        "login": "beta-owner",
        "user_type": "User",
        "created_at": "2026-11-01T12:00:00Z",
        "updated_at": "2026-11-01T12:00:00Z",
        "body": "Review adjudication: WITHDRAW D1 - wrong",
    }
    from datetime import UTC, datetime

    after = datetime(2026, 11, 1, 11, 0, tzinfo=UTC)
    assert adjudication.valid(ruling, after, beta) is not None
    assert adjudication.valid(ruling, after, alpha) is None


def test_trusted_runs_and_state_follow_the_consumers_base():
    run = {
        "path": ".github/workflows/review-verdict.yml@refs/heads/x",
        "repository": {"full_name": "acme/r"},
        "event": "workflow_dispatch",
        "head_sha": HEAD,
    }
    on_base = lambda repo, sha, base: base == "main"  # noqa: E731
    assert github.trusted_run(run, "acme/r", 1, "main", is_on_base=on_base)
    assert not github.trusted_run(run, "acme/r", 1, "develop", is_on_base=on_base)


def test_missing_configuration_fails_clearly_and_never_defaults(tmp_path):
    data = yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))
    data.pop("consumer")
    with pytest.raises(PolicyError, match="policy needs a consumer section"):
        parse_policy(data)
    root = tmp_path / "root"
    (root / ".github").mkdir(parents=True)
    (root / ".github" / "review.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(gate.CheckError, match="consumer section"):
        gate.load_gate_config(POLICY_PATH, root)
    complete = yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))
    (root / ".github" / "review.yml").write_text(yaml.safe_dump(complete), encoding="utf-8")
    with pytest.raises(gate.CheckError, match="consumer path manifests"):
        gate.load_gate_config(POLICY_PATH, root)


@pytest.mark.parametrize("key", ["gated_base", "attesters", "soundness_manifest", "omp_config"])
def test_each_required_consumer_key_fails_when_missing(key):
    data = yaml.safe_load(BETA_POLICY.read_text(encoding="utf-8"))
    data["consumer"].pop(key)
    with pytest.raises(PolicyError):
        parse_policy(data)


def test_bot_login_is_required():
    data = yaml.safe_load(BETA_POLICY.read_text(encoding="utf-8"))
    data.pop("bot_login")
    with pytest.raises(PolicyError, match="bot_login"):
        parse_policy(data)


def test_gate_cli_requires_a_policy_and_names_no_default():
    result = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "gate", "--repo", "acme/r", "--pr", "1"],
        capture_output=True,
        text=True,
        env={"PATH": "", "GITHUB_TOKEN": "x"},
        check=False,
    )
    assert result.returncode == 2
    assert "--policy" in result.stderr


def test_gate_cli_reports_missing_manifest_configuration(tmp_path):
    (tmp_path / ".github").mkdir()
    data = yaml.safe_load(BETA_POLICY.read_text(encoding="utf-8"))
    (tmp_path / ".github" / "review.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "theseus_reviewer",
            "gate",
            "--repo",
            "acme/r",
            "--pr",
            "1",
            "--policy",
            POLICY_PATH,
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "", "GITHUB_TOKEN": "x"},
        check=False,
    )
    assert result.returncode == review_check.ERROR
    assert "consumer path manifests" in result.stderr


def test_wheel_sources_name_no_consumer_identity():
    package = Path(gate.__file__).parent
    offenders = {}
    for path in package.rglob("*"):
        if path.suffix in (".py", ".md") and path.is_file():
            text = path.read_text(encoding="utf-8").lower()
            hits = [
                token for token in ("joeharris76", "benchbox-review", "benchbox-dev", "origin/develop") if token in text
            ]
            if hits:
                offenders[path.name] = hits
    assert offenders == {}


def test_policy_round_trips_through_json_for_the_plan():
    policy = load_policy(BETA_POLICY)
    plan = json.dumps({"consumer": list(policy.consumer.gated_bases), "bot": policy.bot_login})
    assert json.loads(plan) == {"consumer": ["main"], "bot": "beta-review"}
