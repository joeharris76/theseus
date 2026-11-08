from __future__ import annotations

from typing import Any

from .workflow_support import load

SECRET_SOURCES = {
    "REVIEW_APP_ID": "ORACLE_APP_ID",
    "REVIEW_APP_PRIVATE_KEY": "ORACLE_APP_PRIVATE_KEY",
    "REVIEW_GATEWAY_TOKEN": "ORACLE_GATEWAY_TOKEN",
    "REVIEW_GATEWAY_URL": "ORACLE_GATEWAY_URL",
    "TS_OAUTH_CI_CLIENT_ID": "TS_OAUTH_CI_CLIENT_ID",
    "TS_OAUTH_CI_SECRET": "TS_OAUTH_CI_SECRET",
    "TYPESAFE_API_KEY": "TYPESAFE_API_KEY",
}


def _job() -> dict[str, Any]:
    return load("review-verdict.yml")["jobs"]["verdict"]


def test_every_caller_secret_maps_to_the_secret_it_is_named_for() -> None:
    secrets = _job()["secrets"]

    assert set(secrets) == set(SECRET_SOURCES)
    for name in ("REVIEW_APP_ID", "REVIEW_APP_PRIVATE_KEY", "REVIEW_GATEWAY_TOKEN", "REVIEW_GATEWAY_URL"):
        assert secrets[name] == f"${{{{ secrets.{SECRET_SOURCES[name]} }}}}"


def test_the_caller_grants_only_contents_read() -> None:
    assert _job()["permissions"] == {"contents": "read"}
