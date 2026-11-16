"""The GitHub environment, its deployment branch policies and its secrets, through the host's ``gh``.

A secret value reaches ``gh secret set`` on stdin, so Theseus needs no libsodium and the value is never in argv.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .plan import Context
from .store import Secret

NOT_FOUND = "HTTP 404"
ACCEPT = "Accept: application/vnd.github+json"


class GithubError(RuntimeError):
    pass


def _api(ctx: Context, method: str, path: str, body: dict[str, Any] | None = None, *, missing_ok: bool = False) -> Any:
    argv = ["gh", "api", "-X", method, "-H", ACCEPT, path]
    if body is not None:
        argv += ["--input", "-"]
    result = ctx.run(argv, stdin=None if body is None else json.dumps(body))
    if result.returncode != 0:
        if missing_ok and NOT_FOUND in result.stderr:
            return None
        raise GithubError(
            f"gh api {method} {path.split('?', 1)[0]} failed: {(result.stderr or result.stdout).strip()[:300]}"
        )
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GithubError(f"gh api {method} {path.split('?', 1)[0]} returned malformed JSON") from exc


def _base(ctx: Context, environment: str | None = None) -> str:
    github = ctx.config.github
    return f"repos/{github.repository}/environments/{environment or github.environment}"


def get_environment(ctx: Context) -> dict[str, Any] | None:
    found = _api(ctx, "GET", _base(ctx), missing_ok=True)
    return found if isinstance(found, dict) else None


def has_custom_policies(environment: dict[str, Any]) -> bool:
    policy = environment.get("deployment_branch_policy")
    return isinstance(policy, dict) and policy.get("custom_branch_policies") is True


def ensure_environment(ctx: Context) -> None:
    """Create the environment, or make it use custom deployment branch policies. Existing reviewers and wait timers are kept."""
    _api(
        ctx,
        "PUT",
        _base(ctx),
        {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
    )


def branch_policies(ctx: Context) -> list[str]:
    found = _api(ctx, "GET", f"{_base(ctx)}/deployment-branch-policies?per_page=100", missing_ok=True)
    if not isinstance(found, dict):
        return []
    return [str(item["name"]) for item in found.get("branch_policies", []) if item.get("type", "branch") == "branch"]


def add_branch_policy(ctx: Context, name: str) -> None:
    _api(ctx, "POST", f"{_base(ctx)}/deployment-branch-policies", {"name": name, "type": "branch"})


def list_secrets(ctx: Context) -> dict[str, datetime]:
    """Secret names in the configured environment with their ``updated_at``. Values cannot be read back."""
    found = _api(ctx, "GET", f"{_base(ctx)}/secrets?per_page=100", missing_ok=True)
    if not isinstance(found, dict):
        return {}
    return {
        str(item["name"]): datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00"))
        for item in found.get("secrets", [])
    }


def set_secret(ctx: Context, name: str, value: Secret, environment: str | None = None) -> None:
    github = ctx.config.github
    result = ctx.run(
        ["gh", "secret", "set", name, "--env", environment or github.environment, "--repo", github.repository],
        stdin=value.reveal(),
    )
    if result.returncode != 0:
        raise GithubError(f"gh secret set {name} failed: {(result.stderr or result.stdout).strip()[:300]}")
