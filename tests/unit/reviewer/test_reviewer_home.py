from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from tests.conftest import ALPHA_POLICY, CONSUMER_ALPHA

from theseus_reviewer import cli, providers
from theseus_reviewer._vendor import yaml
from theseus_reviewer.classifier import matching
from theseus_reviewer.matchers import load_matchers
from theseus_reviewer.policy import Policy

pytestmark = [pytest.mark.unit, pytest.mark.fast]

CONFIG_PATH = CONSUMER_ALPHA / ".github" / "review-omp-config.yml"
POLICY_PATH = ALPHA_POLICY
GATEWAY_URL = "https://example.ts.net:4000"
PROVIDERS = {"anthropic", "openai-codex", "google-antigravity"}
TOKEN = "t" * 48
ACCOUNTS = {
    "accounts": [
        {"provider": "anthropic", "identityKey": "email:a@example.com"},
        {"provider": "openai-codex", "identityKey": "email:o@example.com"},
        {"provider": "google-antigravity", "identityKey": "email:g@example.com"},
        {"provider": "xai", "identityKey": "email:x@example.com"},
    ]
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gateway_clients(tmp_path: Path, token: str = TOKEN, accounts: dict | None = None) -> Path:
    token_file = tmp_path / "client.token"
    token_file.write_text(token, encoding="utf-8")
    out = tmp_path / "clients.json"
    argv = ["gateway-clients", "--token-file", str(token_file), "--out", str(out), "--omp-config", str(CONFIG_PATH)]
    if accounts is not None:
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps(accounts), encoding="utf-8")
        argv += ["--accounts", str(accounts_file)]
    assert cli.main(argv) == 0
    return out


def test_reviewer_home_writes_the_config_and_the_gateway_providers(tmp_path: Path) -> None:
    assert (
        cli.main(
            ["reviewer-home", "--home", str(tmp_path), "--gateway-url", GATEWAY_URL, "--omp-config", str(CONFIG_PATH)]
        )
        == 0
    )
    agent = tmp_path / ".omp" / "agent"
    assert _load(agent / "config.yml") == _load(CONFIG_PATH)
    routed = _load(agent / "models.yml")["providers"]
    assert set(routed) == PROVIDERS
    for provider in routed.values():
        assert provider == {"transport": "pi-native", "baseUrl": GATEWAY_URL, "apiKey": "OMP_GATEWAY_TOKEN"}


@pytest.mark.parametrize("url", ["", "   ", "http://example.ts.net:4000", "example.ts.net:4000", "https://"])
def test_reviewer_home_refuses_a_missing_or_insecure_gateway_url(tmp_path: Path, url: str) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["reviewer-home", "--home", str(tmp_path), "--gateway-url", url, "--omp-config", str(CONFIG_PATH)])
    assert "gateway URL" in str(refused.value)
    assert not (tmp_path / ".omp").exists()


def test_committed_config_has_only_roles_and_retry() -> None:
    assert set(_load(CONFIG_PATH)) == {"modelRoles", "retry"}


def test_every_policy_model_alias_has_a_role() -> None:
    roles = _load(CONFIG_PATH)["modelRoles"]
    aliases = {reviewer["model"].removeprefix("@") for reviewer in _load(POLICY_PATH)["reviewers"].values()}
    assert aliases <= set(roles)


def test_each_role_primary_has_exactly_one_model_keyed_chain() -> None:
    config = _load(CONFIG_PATH)
    chains = config["retry"]["fallbackChains"]
    primaries = {target.rsplit(":", 1)[0] for target in config["modelRoles"].values()}
    assert set(chains) == primaries
    assert not set(chains) & set(config["modelRoles"])
    assert all("/" in key and chain for key, chain in chains.items())


def test_fallback_walks_only_on_usage_limits() -> None:
    assert _load(CONFIG_PATH)["retry"]["fallbackOn"] == "usage-limit"


def test_the_consumer_omp_config_is_a_fixture_file() -> None:
    assert CONFIG_PATH.is_file()


@pytest.mark.parametrize("path", [".github/review-omp-config.yml", "_project/scripts/review_gateway_host.sh"])
def test_reviewer_credential_scope_files_are_soundness_and_gate_paths(policy: Policy, path: str) -> None:
    assert load_matchers(policy.consumer, CONSUMER_ALPHA).is_soundness_path(path)
    assert matching([path], policy.classifier.gate_paths) == [path]


def test_gateway_clients_scope_the_token_to_every_reviewer_model(tmp_path: Path) -> None:
    out = _gateway_clients(tmp_path)
    config = _load(CONFIG_PATH)
    expected = {target.rsplit(":", 1)[0] for target in config["modelRoles"].values()}
    expected |= {target.rsplit(":", 1)[0] for chain in config["retry"]["fallbackChains"].values() for target in chain}
    client = json.loads(out.read_text(encoding="utf-8"))["review-ci"]
    assert client["tokenSha256"] == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert sorted(client["models"]) == sorted(expected)
    assert "accounts" not in client
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_gateway_clients_pin_each_gateway_provider_to_its_logged_in_accounts(tmp_path: Path) -> None:
    client = json.loads(_gateway_clients(tmp_path, accounts=ACCOUNTS).read_text(encoding="utf-8"))["review-ci"]
    assert client["accounts"] == {
        "anthropic": ["email:a@example.com"],
        "openai-codex": ["email:o@example.com"],
        "google-antigravity": ["email:g@example.com"],
    }


@pytest.mark.parametrize("token", [TOKEN + "\n", "short"])
def test_gateway_clients_refuse_a_token_the_secret_would_not_match(tmp_path: Path, token: str) -> None:
    with pytest.raises(SystemExit, match="client token"):
        _gateway_clients(tmp_path, token=token)
    assert not (tmp_path / "clients.json").exists()


def test_gateway_clients_replace_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / "clients.json").write_text("{}", encoding="utf-8")
    out = _gateway_clients(tmp_path)
    assert "review-ci" in json.loads(out.read_text(encoding="utf-8"))
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


LOCAL_TOKEN = "l" * 48
CLOUD_TOKEN = "c" * 48


def _named_gateway_clients(tmp_path: Path, tokens: dict[str, str | None], accounts: dict | None = None) -> Path:
    token_file = tmp_path / "client.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    out = tmp_path / "clients.json"
    argv = ["gateway-clients", "--token-file", str(token_file), "--out", str(out), "--omp-config", str(CONFIG_PATH)]
    for name, token in tokens.items():
        named_file = tmp_path / f"{name}.token"
        if token is not None:
            named_file.write_text(token, encoding="utf-8")
        argv += ["--client", f"{name}={named_file}"]
    if accounts is not None:
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps(accounts), encoding="utf-8")
        argv += ["--accounts", str(accounts_file)]
    assert cli.main(argv) == 0
    return out


def test_gateway_clients_write_one_entry_per_named_client_with_identical_scope(tmp_path: Path) -> None:
    out = _named_gateway_clients(tmp_path, {"review-local": LOCAL_TOKEN, "review-cloud": CLOUD_TOKEN}, ACCOUNTS)
    clients = json.loads(out.read_text(encoding="utf-8"))
    assert list(clients) == ["review-ci", "review-local", "review-cloud"]
    assert [clients[name]["tokenSha256"] for name in clients] == [
        hashlib.sha256(token.encode()).hexdigest() for token in (TOKEN, LOCAL_TOKEN, CLOUD_TOKEN)
    ]
    assert len({client["tokenSha256"] for client in clients.values()}) == 3
    for client in clients.values():
        assert client["models"] == clients["review-ci"]["models"]
        assert client["accounts"] == clients["review-ci"]["accounts"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_gateway_clients_with_only_review_ci_write_the_single_client_file(tmp_path: Path) -> None:
    out = _gateway_clients(tmp_path, accounts=ACCOUNTS)
    expected = {
        "review-ci": {
            "tokenSha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
            "models": providers.gateway_models(_load(CONFIG_PATH)),
            "accounts": {
                "openai-codex": ["email:o@example.com"],
                "anthropic": ["email:a@example.com"],
                "google-antigravity": ["email:g@example.com"],
            },
        }
    }
    assert out.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"


def test_gateway_clients_report_each_client(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _named_gateway_clients(tmp_path, {"review-local": LOCAL_TOKEN}, ACCOUNTS)
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":", 1)[0] for line in lines] == ["review-ci", "review-local"]
    assert lines[0].split(":", 1)[1] == lines[1].split(":", 1)[1]
    assert TOKEN not in "".join(lines) and LOCAL_TOKEN not in "".join(lines)


@pytest.mark.parametrize("missing", ["review-local", "review-cloud"])
def test_gateway_clients_refuse_a_missing_named_token_file(tmp_path: Path, missing: str) -> None:
    tokens: dict[str, str | None] = {"review-local": LOCAL_TOKEN, "review-cloud": CLOUD_TOKEN}
    tokens[missing] = None
    with pytest.raises(SystemExit, match="cannot read the client token file"):
        _named_gateway_clients(tmp_path, tokens)
    assert not (tmp_path / "clients.json").exists()


def test_gateway_clients_refuse_an_unreadable_named_token_file(tmp_path: Path) -> None:
    (tmp_path / "review-local.token").mkdir()
    with pytest.raises(SystemExit, match="cannot read the client token file"):
        _named_gateway_clients(tmp_path, {"review-local": None})
    assert not (tmp_path / "clients.json").exists()


def test_gateway_clients_refuse_a_named_token_the_secret_would_not_match(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="client token"):
        _named_gateway_clients(tmp_path, {"review-local": LOCAL_TOKEN + "\n"})
    assert not (tmp_path / "clients.json").exists()


def test_gateway_clients_refuse_a_token_shared_between_clients(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="review-local shares its token"):
        _named_gateway_clients(tmp_path, {"review-local": TOKEN})
    assert not (tmp_path / "clients.json").exists()


@pytest.mark.parametrize("spec", ["review-ci=x", "review-local", "=x", "review-local="])
def test_gateway_clients_refuse_a_malformed_or_reserved_client_spec(tmp_path: Path, spec: str) -> None:
    token_file = tmp_path / "client.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    argv = [
        "gateway-clients",
        "--token-file",
        str(token_file),
        "--out",
        str(tmp_path / "clients.json"),
        "--omp-config",
        str(CONFIG_PATH),
    ]
    with pytest.raises(SystemExit, match="--client expects|duplicate gateway client"):
        cli.main([*argv, "--client", spec])
    assert not (tmp_path / "clients.json").exists()


def test_gateway_clients_refuse_a_repeated_client_name(tmp_path: Path) -> None:
    token_file = tmp_path / "client.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    argv = [
        "gateway-clients",
        "--token-file",
        str(token_file),
        "--out",
        str(tmp_path / "clients.json"),
        "--omp-config",
        str(CONFIG_PATH),
    ]
    with pytest.raises(SystemExit, match="duplicate gateway client name review-local"):
        cli.main([*argv, "--client", "review-local=a", "--client", "review-local=b"])
    assert not (tmp_path / "clients.json").exists()


def _gate_machinery_files() -> list[str]:
    declared = [
        line.split("\t", 1)[1]
        for line in (CONSUMER_ALPHA / ".github" / "soundness-paths.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("file\t") and "review" in line and not line.split("\t", 1)[1].startswith("tests/")
    ]
    return sorted(set(declared))


@pytest.mark.parametrize("path", _gate_machinery_files())
def test_every_gate_machinery_file_is_classified_as_gate(policy: Policy, path: str) -> None:
    assert matching([path], policy.classifier.gate_paths) == [path]


def test_gate_machinery_discovery_includes_the_gate_workflow_and_entrypoint() -> None:
    found = _gate_machinery_files()
    assert ".github/workflows/review-gate.yml" in found
    assert "_project/scripts/review_gate.py" in found
    assert ".github/workflows/review-verdict.yml" in found
