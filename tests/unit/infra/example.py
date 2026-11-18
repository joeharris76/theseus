"""The reference infra section the tests use, mirroring the documented example."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from theseus_reviewer._vendor import yaml
from theseus_reviewer.infra.config import InfraConfig, parse_infra

EXAMPLE: dict[str, Any] = {
    "store": {"backend": "file", "dir": "STORE"},
    "state": "STATE",
    "github": {
        "repository": "Example-Org/example",
        "environment": "review",
        "branch_policies": ["develop", "release/*"],
    },
    "tailnet": {
        "tag": "tag:review-ci",
        "tag_owners": ["autogroup:admin"],
        "gateway_port": 4000,
        "deny_port": 22,
        "gateway_dst": ["tag:review-gateway"],
        "oauth_client": {"scopes": ["auth_keys"], "description": "example-review-ci"},
    },
    "gateway": {"host_script": "scripts/gateway.sh", "client": "review-ci", "dir": "GATEWAY"},
    "secrets": {
        "REVIEW_APP_ID": {"source": "literal", "value": "123456"},
        "REVIEW_APP_PRIVATE_KEY": {
            "source": "app_key",
            "app_slug": "example-app",
            "pem_dir": "PEMS",
            "max_age_days": 180,
        },
        "REVIEW_GATEWAY_TOKEN": {"source": "gateway_token"},
        "REVIEW_GATEWAY_URL": {"source": "gateway_url"},
        "TS_OAUTH_CI_CLIENT_ID": {"source": "tailscale_oauth", "field": "id"},
        "TS_OAUTH_CI_SECRET": {"source": "tailscale_oauth", "field": "secret"},
        "OPENAI_API_KEY": {"source": "openai_service_account", "project": "proj_abc", "name": "review-classifier"},
        "TYPESAFE_API_KEY": {
            "source": "store",
            "rotator": "typesafe",
            "max_age_days": 90,
            "mirror_keychain": "TYPESAFE_API_KEY",
        },
    },
}


def example(tmp_path: Path, **changes: Any) -> InfraConfig:
    """The example with its placeholder paths under ``tmp_path``; ``changes`` replace whole top-level keys."""
    data = deepcopy(EXAMPLE)
    data["store"]["dir"] = str(tmp_path / "store")
    data["state"] = str(tmp_path / "state" / "state.json")
    data["gateway"]["dir"] = str(tmp_path / "gateway")
    data["secrets"]["REVIEW_APP_PRIVATE_KEY"]["pem_dir"] = str(tmp_path / "pems")
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return parse_infra(data, tmp_path / ".github" / "review.yml")


def write_policy(path: Path, infra: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"infra": infra}), encoding="utf-8")
    return path
