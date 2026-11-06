from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import CONSUMER_ALPHA
from tests.integration.fake_gateway import FakeGateway

from theseus_reviewer import cli, providers
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

TOKEN = "t" * 48
BASE = yaml.safe_load((CONSUMER_ALPHA / ".github" / "review-omp-config.yml").read_text(encoding="utf-8"))
FOURTH = "zeta-ai"


def _four_providers() -> dict[str, Any]:
    config = json.loads(json.dumps(BASE))
    config["modelRoles"]["zeta"] = f"{FOURTH}/zeta-1:high"
    config["retry"]["fallbackChains"][f"{FOURTH}/zeta-1"] = ["anthropic/claude-sonnet-5-5:medium"]
    return config


CONFIG = _four_providers()
ROUTED = providers.routed(CONFIG)
LOGINS = {provider: [f"email:{provider}@example.com"] for provider in ROUTED}


def _files(tmp_path: Path, logins: dict[str, list[str]] | None = None) -> dict[str, Path]:
    held = LOGINS if logins is None else logins
    paths = {
        "config": tmp_path / "config.yml",
        "accounts": tmp_path / "accounts.json",
        "token": tmp_path / "client.token",
        "out": tmp_path / "clients.json",
        "report": tmp_path / "report.json",
    }
    paths["config"].write_text(yaml.safe_dump(CONFIG, sort_keys=False), encoding="utf-8")
    paths["accounts"].write_text(
        json.dumps({"accounts": [{"provider": p, "identityKey": key} for p, keys in held.items() for key in keys]}),
        encoding="utf-8",
    )
    paths["token"].write_text(TOKEN, encoding="utf-8")
    return paths


def _clients(paths: dict[str, Path], *extra: str, accounts: bool = True) -> int:
    argv = ["gateway-clients", "--token-file", str(paths["token"]), "--out", str(paths["out"])]
    argv += ["--omp-config", str(paths["config"]), "--report", str(paths["report"]), *extra]
    if accounts:
        argv += ["--accounts", str(paths["accounts"])]
    return cli.main(argv)


def _models_of(provider: str) -> list[str]:
    return [model for model in providers.gateway_models(CONFIG) if model.startswith(f"{provider}/")]


@pytest.mark.parametrize("lost", ROUTED)
def test_a_provider_without_an_account_is_excluded_and_reported_and_the_file_stays_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lost: str
) -> None:
    paths = _files(tmp_path, {p: keys for p, keys in LOGINS.items() if p != lost})

    assert _clients(paths) == 0

    client = json.loads(paths["out"].read_text(encoding="utf-8"))["review-ci"]
    assert not any(model.startswith(f"{lost}/") for model in client["models"])
    assert lost not in client["accounts"]
    assert {model.split("/")[0] for model in client["models"]} == set(client["accounts"]) == set(ROUTED) - {lost}
    assert f"excluded: {lost} (no logged-in broker account): {', '.join(_models_of(lost))}" in capsys.readouterr().out
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert report["granted"] == [p for p in ROUTED if p != lost]
    assert report["excluded"] == {lost: {"reason": "no logged-in broker account", "models": _models_of(lost)}}


@pytest.mark.parametrize("kept", ROUTED)
def test_only_the_provider_with_an_account_is_granted(tmp_path: Path, kept: str) -> None:
    paths = _files(tmp_path, {kept: LOGINS[kept]})

    assert _clients(paths) == 0

    client = json.loads(paths["out"].read_text(encoding="utf-8"))["review-ci"]
    assert client["models"] == _models_of(kept) and list(client["accounts"]) == [kept]


def test_no_account_at_all_exits_1_writes_no_client_file_and_still_writes_the_report(tmp_path: Path) -> None:
    paths = _files(tmp_path, {})

    with pytest.raises(SystemExit, match="no configured provider has a broker login"):
        _clients(paths)

    assert not paths["out"].exists()
    assert json.loads(paths["report"].read_text(encoding="utf-8"))["granted"] == []


def test_an_owner_denylisted_provider_is_excluded_even_when_it_has_an_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _files(tmp_path)

    assert _clients(paths, "--exclude-provider", "anthropic") == 0

    assert "excluded: anthropic (excluded by owner)" in capsys.readouterr().out
    client = json.loads(paths["out"].read_text(encoding="utf-8"))["review-ci"]
    assert "anthropic" not in client["accounts"] and not any(m.startswith("anthropic/") for m in client["models"])


def test_the_owner_reason_wins_over_a_missing_account(tmp_path: Path) -> None:
    paths = _files(tmp_path, {p: keys for p, keys in LOGINS.items() if p != "anthropic"})

    assert _clients(paths, "--exclude-provider", "anthropic") == 0

    reason = json.loads(paths["report"].read_text(encoding="utf-8"))["excluded"]["anthropic"]["reason"]
    assert reason == "excluded by owner"


def test_without_an_accounts_file_every_routed_provider_is_granted_but_the_denylist_still_applies(
    tmp_path: Path,
) -> None:
    paths = _files(tmp_path)

    assert _clients(paths, "--exclude-provider", FOURTH, accounts=False) == 0

    client = json.loads(paths["out"].read_text(encoding="utf-8"))["review-ci"]
    assert set(client["models"]) == {m for m in providers.gateway_models(CONFIG) if not m.startswith(f"{FOURTH}/")}
    assert "accounts" not in client


def test_an_unfamiliar_provider_with_an_account_is_granted(tmp_path: Path) -> None:
    paths = _files(tmp_path)

    assert _clients(paths) == 0

    client = json.loads(paths["out"].read_text(encoding="utf-8"))["review-ci"]
    assert client["accounts"][FOURTH] == LOGINS[FOURTH] and f"{FOURTH}/zeta-1" in client["models"]


def _report(paths: dict[str, Path], capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict[str, Any]]:
    argv = ["gateway-providers", "--omp-config", str(paths["config"]), "--json", *extra]
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_the_provider_report_is_healthy_when_every_provider_has_a_login(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _files(tmp_path)

    code, report = _report(paths, capsys, "--accounts", str(paths["accounts"]))

    assert code == 0 and report["state"] == "healthy"
    assert all(row["logged_in"] and row["granted"] and row["reason"] == "" for row in report["providers"].values())
    assert all(row["served"] is None for row in report["providers"].values())


@pytest.mark.parametrize("lost", ROUTED)
def test_a_lost_login_is_degraded_exit_3_and_every_role_keeps_a_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lost: str
) -> None:
    paths = _files(tmp_path, {p: keys for p, keys in LOGINS.items() if p != lost})

    code, report = _report(paths, capsys, "--accounts", str(paths["accounts"]))

    assert code == 3 and report["state"] == "degraded"
    assert report["providers"][lost]["reason"] == "no logged-in broker account"
    assert report["providers"][lost]["granted"] is False
    assert all(role["first_served"] is not None for role in report["roles"].values())
    assert all(not role["first_served"].startswith(f"{lost}/") for role in report["roles"].values())


def test_every_login_lost_is_broken_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _files(tmp_path, {})

    code, report = _report(paths, capsys, "--accounts", str(paths["accounts"]))

    assert code == 1 and report["state"] == "broken"


def test_an_owner_removal_is_reported_without_degrading(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _files(tmp_path)

    code, report = _report(paths, capsys, "--accounts", str(paths["accounts"]), "--exclude-provider", "anthropic")

    assert code == 0 and report["state"] == "healthy"
    row = report["providers"]["anthropic"]
    assert row["excluded_by_owner"] is True and row["granted"] is False and row["reason"] == "excluded by owner"


def test_a_relogin_under_a_new_identity_is_a_stale_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _files(tmp_path)
    assert _clients(paths) == 0
    capsys.readouterr()
    relogged = {**LOGINS, "anthropic": ["email:new@example.com"]}
    paths["accounts"].write_text(
        json.dumps({"accounts": [{"provider": p, "identityKey": k} for p, keys in relogged.items() for k in keys]}),
        encoding="utf-8",
    )

    code, report = _report(paths, capsys, "--accounts", str(paths["accounts"]), "--clients", str(paths["out"]))

    assert code == 3
    assert report["providers"]["anthropic"]["stale_pin"] is True
    assert report["providers"]["anthropic"]["reason"] == "stale pin"
    assert report["providers"]["openai-codex"]["stale_pin"] is False


def test_the_report_accepts_the_config_alone_and_leaves_unknown_fields_null(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _files(tmp_path)

    code, report = _report(paths, capsys)

    assert code == 0
    assert all(row["logged_in"] is None and row["stale_pin"] is None for row in report["providers"].values())


@pytest.mark.parametrize("lost", ROUTED)
def test_a_listed_set_without_a_provider_is_degraded_granted_but_not_served(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lost: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", FakeGateway("x").certificate_file)
    gateway = FakeGateway(TOKEN).start()
    try:
        paths = _files(tmp_path)
        gateway.serve(model for model in providers.gateway_models(CONFIG) if not model.startswith(f"{lost}/"))
        extra = ["--served-url", gateway.url, "--token-file", str(paths["token"]), "--accounts", str(paths["accounts"])]

        code, report = _report(paths, capsys, *extra)
    finally:
        gateway.stop()

    assert code == 3
    assert report["providers"][lost]["served"] is False
    assert report["providers"][lost]["reason"] == "granted but not served"
    assert all(row["served"] for name, row in report["providers"].items() if name != lost)


def test_an_unreachable_gateway_is_broken_with_the_probe_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", FakeGateway("x").certificate_file)
    gateway = FakeGateway(TOKEN).start()
    url = gateway.url
    gateway.stop()
    paths = _files(tmp_path)

    code, report = _report(paths, capsys, "--served-url", url, "--token-file", str(paths["token"]))

    assert code == 1 and report["state"] == "broken" and report["served_error"] == "gateway unreachable"
