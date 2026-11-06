from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from theseus_reviewer import omp_pin

from .harness import PINNED_OMP, REPO_ROOT, Harness, git

pytestmark = [pytest.mark.integration, pytest.mark.medium]

HOST = REPO_ROOT / "tests" / "fixtures" / "gateway_host" / "host.sh"
PROVIDERS = ("anthropic", "openai-codex", "muse-code", "google-antigravity")


def _host(harness: Harness) -> tuple[Path, dict[str, str]]:
    launcher = harness.root / "theseus"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m theseus_reviewer "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    gateway = harness.root / "gateway"
    gateway.mkdir()
    (gateway / "client.token").write_text("c" * 48, encoding="utf-8")
    (gateway / "local-client.token").write_text("l" * 48, encoding="utf-8")
    accounts = harness.root / "accounts.json"
    accounts.write_text(
        json.dumps(
            {"accounts": [{"provider": name, "identityKey": f"email:{name}@example.com"} for name in PROVIDERS]}
        ),
        encoding="utf-8",
    )
    env = {
        **harness.base_env(),
        "THESEUS": str(launcher),
        "CONSUMER_REPO": str(harness.repo),
        "TRUSTED_COMMIT": git(harness.repo, "rev-parse", "develop"),
        "GATEWAY_DIR": str(gateway),
        "ACCOUNTS_FILE": str(accounts),
    }
    return gateway, env


def test_the_host_installs_the_pinned_omp_and_writes_the_clients_through_the_installed_package(
    harness: Harness,
) -> None:
    gateway, env = _host(harness)

    first = harness.run("host", ["bash", str(HOST)], env)

    assert first.code == 0, first.stdout + first.stderr
    pin = omp_pin.pin_from_text((harness.repo / ".github" / "review.yml").read_text(encoding="utf-8"))
    assert pin.version == PINNED_OMP
    expected = omp_pin.binary_path(pin, omp_pin.current_platform(), harness.omp_cache)
    assert f"omp: {expected}" in first.stdout
    clients = json.loads((gateway / "clients.json").read_text(encoding="utf-8"))
    assert sorted(clients) == ["review-ci", "review-local"]
    assert clients["review-ci"]["tokenSha256"] == hashlib.sha256(b"c" * 48).hexdigest()
    assert clients["review-local"]["tokenSha256"] == hashlib.sha256(b"l" * 48).hexdigest()
    assert {entry.split("/", 1)[0] for entry in clients["review-ci"]["models"]} <= set(PROVIDERS)
    assert sorted(clients["review-ci"]["accounts"]) == sorted(PROVIDERS)
    before = (gateway / "clients.json").read_bytes()

    second = harness.run("host-again", ["bash", str(HOST)], env)

    assert second.code == 0, second.stdout + second.stderr
    assert (gateway / "clients.json").read_bytes() == before
    assert second.stdout == first.stdout


def test_the_host_stops_when_the_trusted_commit_does_not_hold_the_policy(harness: Harness) -> None:
    gateway, env = _host(harness)
    env["TRUSTED_COMMIT"] = "0" * 40

    refused = harness.run("host-refused", ["bash", str(HOST)], env)

    assert refused.code != 0
    assert not (gateway / "clients.json").exists()


def test_the_host_fixture_never_names_the_embedded_engine() -> None:
    text = HOST.read_text(encoding="utf-8")
    assert "_project" not in text and "reviewers" not in text and "PYTHONPATH" not in text
