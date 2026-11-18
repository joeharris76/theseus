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


def _matches_every_branch(pattern: str) -> bool:
    return bool(pattern) and set(pattern) == {"*"}


def is_restricted(ctx: Context, environment: dict[str, Any] | None, name: str | None = None) -> bool:
    """Whether deployments to the environment are limited to chosen branches.

    Protected branches count. Custom policies count unless one matches every branch (``*``, ``**``).
    """
    policy = environment.get("deployment_branch_policy") if environment else None
    if not isinstance(policy, dict):
        return False
    if policy.get("custom_branch_policies"):
        return not any(_matches_every_branch(pattern) for pattern in branch_policies(ctx, name))
    return bool(policy.get("protected_branches"))


def kept_protections(environment: dict[str, Any]) -> dict[str, Any]:
    """The protections of an existing environment, in the shape a ``PUT`` takes, so a ``PUT`` does not drop them."""
    kept: dict[str, Any] = {}
    reviewers: list[dict[str, Any]] = []
    for rule in environment.get("protection_rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") == "wait_timer" and rule.get("wait_timer") is not None:
            kept["wait_timer"] = rule["wait_timer"]
        if rule.get("type") == "required_reviewers":
            kept["prevent_self_review"] = bool(rule.get("prevent_self_review"))
            for entry in rule.get("reviewers") or []:
                who = entry.get("reviewer") if isinstance(entry, dict) else None
                if isinstance(who, dict) and "id" in who and entry.get("type") in ("User", "Team"):
                    reviewers.append({"type": entry["type"], "id": who["id"]})
    if reviewers:
        kept["reviewers"] = reviewers
    if "can_admins_bypass" in environment:
        kept["can_admins_bypass"] = bool(environment["can_admins_bypass"])
    return kept


def describe_protections(environment: dict[str, Any] | None) -> str:
    if environment is None:
        return "no existing protections"
    kept = kept_protections(environment)
    parts = [f"{len(kept['reviewers'])} required reviewer(s)"] if "reviewers" in kept else []
    if "wait_timer" in kept:
        parts.append(f"wait timer {kept['wait_timer']} min")
    if kept.get("prevent_self_review"):
        parts.append("prevent self-review")
    if "can_admins_bypass" in kept:
        parts.append(f"admins can bypass: {kept['can_admins_bypass']}")
    return "kept as they are: " + ", ".join(parts) if parts else "it has no reviewers or wait timer to keep"


def ensure_environment(ctx: Context, environment: dict[str, Any] | None, custom_policies: bool) -> None:
    """Create the environment, or switch it to custom deployment branch policies, re-sending its existing protections.

    A ``PUT`` replaces the environment's protection settings, so the reviewers, wait timer, self-review and admin
    bypass settings read from ``environment`` go into the body.
    """
    body: dict[str, Any] = kept_protections(environment) if environment else {}
    if custom_policies:
        body["deployment_branch_policy"] = {"protected_branches": False, "custom_branch_policies": True}
    elif environment and isinstance(environment.get("deployment_branch_policy"), dict):
        body["deployment_branch_policy"] = environment["deployment_branch_policy"]
    _api(ctx, "PUT", _base(ctx), body)


def branch_policies(ctx: Context, environment: str | None = None) -> list[str]:
    found = _api(ctx, "GET", f"{_base(ctx, environment)}/deployment-branch-policies?per_page=100", missing_ok=True)
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
