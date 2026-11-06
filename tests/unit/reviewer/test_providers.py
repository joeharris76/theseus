from __future__ import annotations

import copy
from typing import Any

import pytest
from tests.conftest import CONSUMER_ALPHA
from tests.integration.fake_gateway import DEFAULT_MODELS, FakeGateway

from theseus_reviewer import providers
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

CONFIG = yaml.safe_load((CONSUMER_ALPHA / ".github" / "review-omp-config.yml").read_text(encoding="utf-8"))
WINDOWS = {
    "anthropic/claude-sonnet-5-5": 872_000,
    "anthropic/claude-opus-5-5": 872_000,
    "openai-codex/gpt-6.1-sol": 372_000,
    "openai-codex/gpt-6-astra": 144_000,
    "google-antigravity/gemini-3.8-flash": 936_000,
    "google-antigravity/gemini-4-argon": 983_040,
}


def _served(*models: str) -> providers.Served:
    return providers.Served(frozenset(models))


def _resolve(options: list[str], served: providers.Served | None, excluded: set[str] = frozenset(), load: int = 0):
    return providers.resolve(options, served, "", excluded, WINDOWS, load)


OPUS = providers.candidates("@review", CONFIG)


def test_routed_providers_come_from_the_config_in_first_use_order() -> None:
    assert providers.routed(CONFIG) == ("openai-codex", "anthropic", "google-antigravity")


def test_an_unfamiliar_provider_in_the_config_is_routed() -> None:
    config = copy.deepcopy(CONFIG)
    config["modelRoles"]["extra"] = "zeta-ai/zeta-1:high"
    assert "zeta-ai" in providers.routed(config)
    assert "zeta-ai/zeta-1" in providers.gateway_models(config)


def test_candidates_follow_the_role_then_its_model_keyed_chain() -> None:
    assert OPUS[0] == "anthropic/claude-opus-5-5:medium"
    assert [providers.provider_of(item) for item in OPUS] == ["anthropic", "openai-codex", "google-antigravity"]


def test_a_direct_model_has_its_own_chain_and_an_unknown_role_has_none() -> None:
    direct = providers.candidates("anthropic/claude-opus-5-5", CONFIG)
    assert [providers.strip_effort(item) for item in direct] == [providers.strip_effort(item) for item in OPUS]
    assert providers.candidates("@nope", CONFIG) == []


def test_a_served_first_candidate_is_kept_and_the_chain_is_trimmed_to_served_candidates() -> None:
    served = _served("anthropic/claude-opus-5-5", "google-antigravity/gemini-4-argon")
    resolution = _resolve(OPUS, served)
    assert resolution.target == OPUS[0] and not resolution.substituted
    assert resolution.followers == (OPUS[2],) and resolution.rewrite


def test_a_fully_served_chain_needs_no_rewrite() -> None:
    resolution = _resolve(OPUS, _served(*(providers.strip_effort(item) for item in OPUS)))
    assert resolution.target == OPUS[0] and not resolution.rewrite


def test_the_first_served_substitute_runs_when_the_first_candidate_is_gone() -> None:
    resolution = _resolve(OPUS, _served(*(providers.strip_effort(item) for item in OPUS[1:])))
    assert resolution.target == OPUS[1] and resolution.substituted and resolution.followers == (OPUS[2],)


def test_an_excluded_provider_is_skipped_even_when_listed() -> None:
    resolution = _resolve(OPUS, _served(*(providers.strip_effort(item) for item in OPUS)), {"anthropic"})
    assert resolution.target == OPUS[1]


def test_a_substitute_must_hold_the_load() -> None:
    served = _served(*(providers.strip_effort(item) for item in OPUS[1:]))
    assert _resolve(OPUS, served, load=500_000).target == OPUS[2]
    assert _resolve(OPUS, served, load=300_000).target == OPUS[1]


def test_served_candidates_too_small_for_the_load_end_as_capacity_degraded() -> None:
    served = _served("openai-codex/gpt-6.1-sol")
    resolution = _resolve(OPUS, served, load=500_000)
    assert resolution.target is None and resolution.degraded_capacity
    assert resolution.reason.startswith(providers.DEGRADED_CAPACITY_PREFIX)


def test_nothing_served_ends_as_unavailable_naming_each_candidate() -> None:
    resolution = _resolve(OPUS, _served("other/model"))
    assert resolution.target is None and resolution.reason.startswith(providers.UNAVAILABLE_PREFIX)
    assert all(providers.strip_effort(item) in resolution.reason for item in OPUS)


def test_an_unknown_served_set_is_unavailable_with_the_probe_error() -> None:
    resolution = providers.resolve(OPUS, None, "gateway unreachable", set(), WINDOWS, 0)
    assert resolution.reason == "unavailable: gateway unreachable"


def test_the_effective_config_runs_the_chosen_model_and_keeps_the_other_roles() -> None:
    resolution = _resolve(OPUS, _served(*(providers.strip_effort(item) for item in OPUS[1:])))
    effective = providers.effective_config(CONFIG, "review", resolution, OPUS)
    assert effective["modelRoles"]["review"] == OPUS[1]
    assert {key: value for key, value in effective["modelRoles"].items() if key != "review"} == {
        key: value for key, value in CONFIG["modelRoles"].items() if key != "review"
    }
    chains = effective["retry"]["fallbackChains"]
    assert chains["openai-codex/gpt-6.1-sol"] == [OPUS[2]]
    assert "anthropic/claude-opus-5-5" not in chains
    assert CONFIG["modelRoles"]["review"] == OPUS[0]


def test_degraded_providers_are_the_routed_ones_not_served_or_excluded() -> None:
    served = _served("anthropic/claude-opus-5-5", "openai-codex/gpt-6.1-sol")
    routed = providers.routed(CONFIG)
    assert providers.degraded_providers(routed, served, set()) == ["google-antigravity"]
    assert providers.degraded_providers(routed, served, {"anthropic"}) == ["anthropic", "google-antigravity"]
    assert providers.degraded_providers(routed, None, set()) == []


@pytest.fixture
def gateway() -> Any:
    held = FakeGateway("t" * 48).start()
    try:
        yield held
    finally:
        held.stop()


@pytest.fixture(autouse=True)
def _trust_the_fake_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", FakeGateway("x").certificate_file)


def test_the_probe_reads_the_served_models_with_the_client_token(gateway: Any) -> None:
    gateway.serve(["anthropic/claude-opus-5-5", "openai-codex/gpt-6.1-sol"])
    served = providers.probe(gateway.url, gateway.token)
    assert served.models == {"anthropic/claude-opus-5-5", "openai-codex/gpt-6.1-sol"}
    assert served.providers == {"anthropic", "openai-codex"}
    assert gateway.requests == ["/healthz", "/v1/models"]


def test_the_probe_reports_a_rejected_token_a_malformed_listing_and_a_down_gateway(gateway: Any) -> None:
    with pytest.raises(providers.ProbeError, match="client token rejected"):
        providers.probe(gateway.url, "w" * 48)
    gateway.mode = "malformed"
    with pytest.raises(providers.ProbeError, match="malformed model list"):
        providers.probe(gateway.url, gateway.token)
    gateway.mode = "down"
    with pytest.raises(providers.ProbeError, match="answered 503"):
        providers.probe(gateway.url, gateway.token)
    gateway.stop()
    with pytest.raises(providers.ProbeError, match="unreachable"):
        providers.probe(gateway.url, gateway.token)


@pytest.mark.parametrize("url", ["", "http://example.test", "example.test", "https://"])
def test_the_probe_refuses_a_url_that_is_not_https(url: str) -> None:
    with pytest.raises(providers.ProbeError, match="https"):
        providers.probe(url, "t" * 48)


def test_the_fake_gateway_default_models_cover_every_configured_model() -> None:
    assert set(providers.gateway_models(CONFIG)) <= set(DEFAULT_MODELS)
