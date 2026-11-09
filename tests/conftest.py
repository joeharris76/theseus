from __future__ import annotations

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
FIXTURES = TESTS_ROOT / "fixtures"
CONSUMER_ALPHA = FIXTURES / "consumer" / "alpha"
CONSUMER_BETA = FIXTURES / "consumer" / "beta"
ALPHA_POLICY = CONSUMER_ALPHA / ".github" / "review.yml"
BETA_POLICY = CONSUMER_BETA / ".github" / "review.yml"


@pytest.fixture(scope="session")
def alpha_root() -> Path:
    return CONSUMER_ALPHA


@pytest.fixture(scope="session")
def beta_root() -> Path:
    return CONSUMER_BETA


@pytest.fixture(autouse=True)
def no_other_pull_requests_of_the_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from theseus_reviewer import gate

    monkeypatch.setattr(gate, "fetch_head_pulls", lambda *args: [])
