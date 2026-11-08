from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from tests.conftest import BETA_POLICY

from theseus_reviewer import review_check
from theseus_reviewer._vendor import yaml
from theseus_reviewer.policy import SIZE_BUCKETS, Policy, PolicyError, PromotionRules, parse_policy

from .conftest import POLICY_PATH

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def _raw() -> dict[str, Any]:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_the_live_policy_enforces_and_posts_where_the_gate_reads(policy: Policy) -> None:
    assert (policy.mode, policy.findings_delivery) == ("enforce", "review")
    assert policy.status_context == review_check.REVIEW_CONTEXT
    assert policy.bot_login == "benchbox-review"


def test_live_reviewers_match_buckets_one_to_one(policy: Policy) -> None:
    assert set(policy.reviewers) == set(SIZE_BUCKETS)
    assert set(policy.buckets) == set(SIZE_BUCKETS)
    for bucket in SIZE_BUCKETS:
        assert policy.buckets[bucket] in policy.reviewers
    for reviewer in policy.reviewers.values():
        assert reviewer.harness == "omp"


def test_chain_returns_single_bucket_reviewer(policy: Policy) -> None:
    for bucket in SIZE_BUCKETS:
        assert [reviewer.name for reviewer in policy.chain(bucket)] == [policy.buckets[bucket]]


def test_empty_effort_inherits_role_level(policy: Policy) -> None:
    assert policy.reviewers["trivial"].effort == ""


def test_version_one_is_rejected() -> None:
    raw = _raw()
    raw["version"] = 1
    with pytest.raises(PolicyError, match="policy version must be 2"):
        parse_policy(raw)


def test_missing_bucket_is_rejected() -> None:
    raw = _raw()
    del raw["buckets"]["max"]
    with pytest.raises(PolicyError, match=r"buckets must be exactly"):
        parse_policy(raw)


def test_extra_bucket_is_rejected() -> None:
    raw = _raw()
    raw["buckets"]["bonus"] = "trivial"
    with pytest.raises(PolicyError, match=r"buckets must be exactly"):
        parse_policy(raw)


def test_bucket_naming_unknown_reviewer_is_rejected() -> None:
    raw = _raw()
    raw["buckets"]["max"] = "ghost"
    with pytest.raises(PolicyError, match="names unknown reviewer"):
        parse_policy(raw)


def test_max_attempts_must_cover_longest_enabled_chain() -> None:
    raw = _raw()
    raw["max_attempts"] = 0
    with pytest.raises(PolicyError, match=r"must cover the longest enabled chain \(1\)"):
        parse_policy(raw)


def test_fully_disabled_policy_needs_no_attempts() -> None:
    raw = _raw()
    for reviewer in raw["reviewers"].values():
        reviewer.update(enabled=False, disabled_reason="off")
    raw["max_attempts"] = 0
    assert parse_policy(raw).longest_enabled_chain() == 0


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update(version=1), r"policy version must be 2"),
        (lambda raw: raw.pop("version"), r"policy version must be 2"),
        (lambda raw: raw.update(version="2"), r"policy version must be 2"),
        (lambda raw: raw.update(pools="claude"), r"pools must be a string list"),
        (lambda raw: raw.update(pools=[42]), r"pools must be a string list"),
        (lambda raw: raw.update(reviewers={}), r"reviewers must be a mapping"),
        (lambda raw: raw.pop("reviewers"), r"reviewers must be a mapping"),
        (lambda raw: raw["reviewers"].update({"ghost": "nope"}), r"reviewer ghost must be a mapping"),
        (lambda raw: raw["reviewers"]["small"].update(harness="codex"), r"unknown harness"),
        (lambda raw: raw["reviewers"]["small"].pop("harness"), r"unknown harness"),
        (lambda raw: raw["reviewers"]["small"].update(role="worker"), r"unknown role"),
        (lambda raw: raw["reviewers"]["small"].pop("role"), r"unknown role"),
        (lambda raw: raw["reviewers"]["small"].update(pool="openai"), r"unknown pool"),
        (lambda raw: raw["reviewers"]["small"].update(family=""), r"needs a family label"),
        (lambda raw: raw["reviewers"]["small"].pop("family"), r"needs a family label"),
        (lambda raw: raw["reviewers"]["small"].update(model=""), r"needs a model"),
        (lambda raw: raw["reviewers"]["small"].update(effort="turbo"), r"unknown effort"),
        (lambda raw: raw["reviewers"]["small"].update(read_only="full"), r"unknown read_only"),
        (lambda raw: raw["reviewers"]["small"].update(reads_files="yes"), r"reads_files must be a boolean"),
        (lambda raw: raw["reviewers"]["small"].pop("reads_files"), r"reads_files must be a boolean"),
        (lambda raw: raw["reviewers"]["small"].update(timeout_minutes=0), r"timeout_minutes must be between 1 and 60"),
        (lambda raw: raw["reviewers"]["small"].update(timeout_minutes=61), r"timeout_minutes must be between 1 and 60"),
        (lambda raw: raw["reviewers"]["small"].update(enabled="yes"), r"enabled must be a boolean"),
        (lambda raw: raw["reviewers"]["small"].update(enabled=False), r"needs a disabled_reason"),
        (lambda raw: raw["buckets"].pop("small"), r"buckets must be exactly"),
        (lambda raw: raw.update(buckets={"small": "small"}), r"buckets must be exactly"),
        (lambda raw: raw["buckets"].update({"small": "ghost"}), r"names unknown reviewer"),
        (lambda raw: raw["classifier"].update(gate_paths="x"), r"classifier gate_paths must be a string list"),
        (lambda raw: raw["classifier"].pop("logic_paths"), r"classifier logic_paths must be a string list"),
        (lambda raw: raw["classifier"].update(data_paths=[42]), r"classifier data_paths must be a string list"),
        (lambda raw: raw["classifier"].update(exempt_paths="x"), r"classifier exempt_paths must be a string list"),
        (lambda raw: raw.update(mode="audit"), r"mode must be shadow or enforce"),
        (lambda raw: raw.update(status_context=""), r"status_context is required"),
        (lambda raw: raw.update(findings_delivery="pr"), r"findings_delivery must be one of"),
        (lambda raw: raw.update(bot_login="BenchBox!"), r"bot_login must be an App slug"),
        (lambda raw: raw.update(bot_login="-lead"), r"bot_login must be an App slug"),
        (lambda raw: raw["protocol"].update(max_defects=0), r"protocol limits must be positive"),
        (lambda raw: raw.pop("protocol"), r"protocol limits must be positive"),
        (lambda raw: raw["protocol"].update(max_do_not_ship=0), r"protocol limits must be positive"),
        (lambda raw: raw["retry"].update(daily_budget=0), r"retry rules must be positive"),
        (lambda raw: raw["retry"].update(backoff_start_minutes=0), r"retry rules must be positive"),
        (lambda raw: raw.pop("retry"), r"retry rules must be positive"),
        (lambda raw: raw.update(max_attempts=0), r"must cover the longest enabled chain"),
    ],
)
def test_invalid_policies_are_rejected(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(PolicyError, match=message):
        parse_policy(raw)


def test_missing_exempt_paths_defaults_to_empty() -> None:
    raw = _raw()
    raw["classifier"].pop("exempt_paths", None)
    assert parse_policy(raw).classifier.exempt_paths == ()


def test_evidence_exempt_paths_parsed_and_defaults_to_empty() -> None:
    raw = _raw()
    policy = parse_policy(raw)
    assert "README*.md" in policy.evidence.exempt_paths
    raw.pop("evidence", None)
    assert parse_policy(raw).evidence.exempt_paths == ()
    raw["evidence"] = {"exempt_paths": "not-a-list"}
    with pytest.raises(PolicyError, match="evidence exempt_paths must be a string list"):
        parse_policy(raw)


@pytest.mark.parametrize("effort", ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto", ""])
def test_known_effort_levels_are_accepted(effort: str) -> None:
    raw = _raw()
    raw["reviewers"]["small"]["effort"] = effort
    assert parse_policy(raw).reviewers["small"].effort == effort


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_shadow_and_enforce_modes_are_accepted(mode: str) -> None:
    raw = _raw()
    raw["mode"] = mode
    assert parse_policy(raw).mode == mode


def test_the_policy_carries_the_pinned_hash_of_every_platform_build(policy: Policy) -> None:
    builds = _raw()["omp"]["builds"]
    assert dict(policy.omp_sha256) == {name: build["sha256"] for name, build in builds.items()}


def test_a_policy_with_a_bad_build_key_is_rejected() -> None:
    data = _raw()
    data["omp"]["builds"]["../evil-x64"] = data["omp"]["builds"]["linux-x64"]
    with pytest.raises(PolicyError, match="not a platform name"):
        parse_policy(data)


def test_a_plan_carries_the_hash_pinned_for_the_running_platform(
    policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from theseus_reviewer import cli, omp_pin

    monkeypatch.setattr(omp_pin, "current_platform", lambda: "linux-x64")
    assert cli._base_plan(policy, "o/r", 7, "x")["omp_sha256"] == _raw()["omp"]["builds"]["linux-x64"]["sha256"]
    monkeypatch.setattr(omp_pin, "current_platform", lambda: "plan9-mips")
    assert cli._base_plan(policy, "o/r", 7, "x")["omp_sha256"] == ""


PROMOTION = {"repository": "owner/theseus", "bootstrap_reviewers": ["owner-reviewer"]}


def _promotion(**extra: Any) -> dict[str, Any]:
    return {**PROMOTION, **extra}


def test_promotion_dependency_pins_default_to_deny_all_and_parse_exact_hashes() -> None:
    raw = _raw()
    raw["promotion"] = _promotion()
    assert parse_policy(raw).promotion.dependencies == {}
    raw["promotion"]["dependencies"] = {"my-package==1.2.3": "a" * 64}
    assert parse_policy(raw).promotion.dependencies == {"my-package==1.2.3": "a" * 64}


@pytest.mark.parametrize(
    "dependencies",
    [
        None,
        [],
        {"PyYAML==6.0.3": "a" * 64},
        {"my_package==1.2.3": "a" * 64},
        {"my.package==1.2.3": "a" * 64},
        {"pyyaml>=6": "a" * 64},
        {"pyyaml==": "a" * 64},
        {"pyyaml==6.0.3": "A" * 64},
        {"pyyaml==6.0.3": "a" * 63},
        {"pyyaml==6.0.3": 123},
    ],
)
def test_invalid_promotion_dependency_pins_are_rejected(dependencies: Any) -> None:
    raw = _raw()
    raw["promotion"] = _promotion(dependencies=dependencies)
    with pytest.raises(PolicyError, match="promotion dependencies"):
        parse_policy(raw)


@pytest.mark.parametrize("key", ["bootstrap_reviewers", "allowed_artifact_hosts"])
@pytest.mark.parametrize("value", [[], None, ""])
def test_explicit_empty_promotion_allowlists_do_not_restore_defaults(key: str, value: Any) -> None:
    raw = _raw()
    raw["promotion"] = _promotion(**{key: value})
    with pytest.raises(PolicyError, match=key):
        parse_policy(raw)


def test_missing_promotion_artifact_hosts_keep_the_public_defaults() -> None:
    raw = _raw()
    raw["promotion"] = _promotion()
    rules = parse_policy(raw).promotion
    assert rules.bootstrap_reviewers == ("owner-reviewer",)
    assert rules.repository == "owner/theseus"
    assert "github.com" in rules.allowed_artifact_hosts


def test_a_policy_without_a_promotion_section_parses_with_empty_promotion_rules() -> None:
    raw = _raw()
    raw.pop("promotion", None)
    rules = parse_policy(raw).promotion
    assert rules == PromotionRules()
    assert (rules.repository, rules.bootstrap_reviewers) == ("", ())


def test_a_promotion_section_without_a_repository_is_rejected() -> None:
    raw = _raw()
    raw["promotion"] = {key: value for key, value in PROMOTION.items() if key != "repository"}
    with pytest.raises(PolicyError, match="promotion repository is required when the policy has a promotion section"):
        parse_policy(raw)


def test_a_promotion_section_without_bootstrap_keys_means_no_bootstrap_is_configured() -> None:
    raw = _raw()
    raw["promotion"] = {"repository": "owner/theseus"}
    rules = parse_policy(raw).promotion
    assert rules.repository == "owner/theseus"
    assert rules.bootstrap_reviewers == ()
    assert rules.bootstrap_baseline_commit is None
    assert dict(rules.bootstrap_path_map) == {}


def test_a_policy_carrying_the_removed_brief_size_key_is_rejected() -> None:
    raw = _raw()
    raw["brief_max_bytes"] = 100000
    with pytest.raises(PolicyError, match="unknown top-level key 'brief_max_bytes'"):
        parse_policy(raw)


def test_a_policy_with_an_unknown_top_level_key_is_rejected_by_name() -> None:
    raw = _raw()
    raw["surprise"] = True
    with pytest.raises(PolicyError, match="unknown top-level key 'surprise'"):
        parse_policy(raw)


def test_the_brief_size_bound_is_a_fixed_constant() -> None:
    from theseus_reviewer.policy import BRIEF_MAX_BYTES, INLINE_BRIEF_MAX_BYTES

    assert BRIEF_MAX_BYTES == 400_000
    assert INLINE_BRIEF_MAX_BYTES == 100_000
    assert not hasattr(parse_policy(_raw()), "brief_max_bytes")


def test_the_live_policy_carries_its_consumer_binding(policy: Policy) -> None:
    assert policy.consumer.gated_base == "develop"
    assert policy.consumer.attesters == ("joeharris76",)
    assert policy.consumer.soundness_manifest == ".github/soundness-paths.txt"
    assert policy.consumer.inert_manifest == ".github/inert-paths.txt"
    assert policy.consumer.omp_config == ".github/review-omp-config.yml"
    assert policy.consumer.omp_fixture_prefix == "tests/fixtures/reviewers/omp_candidate/"
    binding = policy.binding()
    assert (binding.bot_login, binding.attesters, binding.gated_base) == (
        "benchbox-review",
        frozenset({"joeharris76"}),
        "develop",
    )


def test_a_beta_consumer_policy_binds_its_own_identities() -> None:
    policy = parse_policy(yaml.safe_load(BETA_POLICY.read_text(encoding="utf-8")))
    binding = policy.binding()
    assert binding.bot_login == "beta-review"
    assert binding.attesters == frozenset({"beta-owner", "beta-deputy"})
    assert binding.gated_base == "main"


def test_optional_consumer_keys_default_to_none() -> None:
    raw = _raw()
    del raw["consumer"]["inert_manifest"], raw["consumer"]["omp_fixture_prefix"]
    consumer = parse_policy(raw).consumer
    assert (consumer.inert_manifest, consumer.omp_fixture_prefix) == (None, None)


def test_a_policy_without_a_consumer_section_fails_with_the_documented_message() -> None:
    raw = _raw()
    del raw["consumer"]
    with pytest.raises(
        PolicyError,
        match=(
            "policy needs a consumer section naming gated_base, attesters, soundness_manifest and omp_config; "
            "the package has no consumer defaults"
        ),
    ):
        parse_policy(raw)


@pytest.mark.parametrize("consumer", [None, [], "alpha", 7])
def test_a_consumer_section_that_is_not_a_mapping_fails(consumer: Any) -> None:
    raw = _raw()
    raw["consumer"] = consumer
    with pytest.raises(PolicyError, match="policy needs a consumer section"):
        parse_policy(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda consumer: consumer.pop("gated_base"), "consumer gated_base must be a branch name"),
        (lambda consumer: consumer.update(gated_base=""), "consumer gated_base must be a branch name"),
        (lambda consumer: consumer.update(gated_base="-develop"), "consumer gated_base must be a branch name"),
        (lambda consumer: consumer.update(gated_base="has space"), "consumer gated_base must be a branch name"),
        (lambda consumer: consumer.update(gated_base=3), "consumer gated_base must be a branch name"),
        (lambda consumer: consumer.pop("attesters"), "consumer attesters must be a string list"),
        (lambda consumer: consumer.update(attesters=[]), "consumer attesters must be a non-empty list"),
        (lambda consumer: consumer.update(attesters="joe"), "consumer attesters must be a string list"),
        (lambda consumer: consumer.update(attesters=["ok", "-bad"]), "consumer attesters must be a non-empty list"),
        (lambda consumer: consumer.update(attesters=["has space"]), "consumer attesters must be a non-empty list"),
        (lambda consumer: consumer.pop("soundness_manifest"), "consumer soundness_manifest is required"),
        (lambda consumer: consumer.update(soundness_manifest=""), "consumer soundness_manifest is required"),
        (lambda consumer: consumer.update(soundness_manifest=4), "consumer soundness_manifest is required"),
        (lambda consumer: consumer.pop("omp_config"), "consumer omp_config is required"),
        (lambda consumer: consumer.update(omp_config="  "), "consumer omp_config is required"),
        (lambda consumer: consumer.update(unknown="x"), r"consumer has unknown keys \['unknown'\]"),
        (lambda consumer: consumer.update(bot_login="x", extra=1), "consumer has unknown keys"),
    ],
)
def test_an_invalid_consumer_section_is_rejected(mutate: Callable[[dict[str, Any]], Any], message: str) -> None:
    raw = copy.deepcopy(_raw())
    mutate(raw["consumer"])
    with pytest.raises(PolicyError, match=message):
        parse_policy(raw)


@pytest.mark.parametrize("key", ["soundness_manifest", "omp_config", "inert_manifest", "omp_fixture_prefix"])
@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.txt", "a/../b.txt", ".github/../x", "a//b", "./a.txt", "a\\b.txt"]
)
def test_a_consumer_path_must_be_repository_relative(key: str, path: str) -> None:
    raw = _raw()
    raw["consumer"][key] = path
    with pytest.raises(PolicyError, match=f"consumer {key} must be a repository-relative path"):
        parse_policy(raw)


def test_a_consumer_fixture_prefix_is_normalised_to_one_trailing_slash() -> None:
    for prefix in ("fixtures/omp", "fixtures/omp/"):
        raw = _raw()
        raw["consumer"]["omp_fixture_prefix"] = prefix
        assert parse_policy(raw).consumer.omp_fixture_prefix == "fixtures/omp/"
