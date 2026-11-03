from __future__ import annotations

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
FIXTURES = TESTS_ROOT / "fixtures"
CONSUMER_ALPHA = FIXTURES / "consumer" / "alpha"
CONSUMER_BETA = FIXTURES / "consumer" / "beta"
ALPHA_POLICY = CONSUMER_ALPHA / ".github" / "oracle-reviewers.yml"
BETA_POLICY = CONSUMER_BETA / ".github" / "oracle-reviewers.yml"


@pytest.fixture(scope="session")
def alpha_root() -> Path:
    return CONSUMER_ALPHA


@pytest.fixture(scope="session")
def beta_root() -> Path:
    return CONSUMER_BETA
