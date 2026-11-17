"""The gateway token and URL, obtained from the consumer's host script.

The gateway host stays consumer-owned: its script creates the client token, runs the gateway and publishes it on
the tailnet. Theseus calls that script and reads the token file it leaves; it does not reimplement any of it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import GatewayConfig, InfraConfig
from .plan import Context
from .store import Secret

HOST_TIMEOUT_SECONDS = 600.0
CI_CLIENT = "review-ci"
_PUBLISHED = re.compile(r"^gateway: published at (https://\S+)\s*$", re.MULTILINE)


class GatewayError(RuntimeError):
    pass


def script_path(config: InfraConfig) -> Path:
    gateway = config.gateway
    if gateway is None:
        raise GatewayError("infra.gateway is not configured")
    root = config.repo_root.resolve()
    path = (root / gateway.host_script).resolve()
    if root not in path.parents:
        raise GatewayError(f"infra.gateway.host_script must stay inside {root}")
    return path


def token_path(gateway: GatewayConfig, client: str) -> Path:
    """``review-ci`` reads ``client.token``; ``review-<name>`` reads ``<name>-client.token``, as the host script does."""
    if client == CI_CLIENT:
        return gateway.directory / "client.token"
    return gateway.directory / f"{client.removeprefix('review-')}-client.token"


def read_token(gateway: GatewayConfig, client: str) -> Secret:
    path = token_path(gateway, client)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GatewayError(f"cannot read the token file of {client}: {exc.strerror}") from exc
    if not value:
        raise GatewayError(f"the token file of {client} is empty")
    return Secret(value)


def run_host_step(ctx: Context, step: str, *args: str) -> str:
    """Run one step of the host script and return its stdout. Its stderr is shown only on failure."""
    script = script_path(ctx.config)
    result = ctx.run([str(script), step, *args], timeout=HOST_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise GatewayError(f"host script step '{step}' failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def published_url(output: str) -> str:
    found = _PUBLISHED.findall(output)
    if not found:
        raise GatewayError("the host script did not report a published gateway URL")
    return found[-1]


def obtain(ctx: Context) -> tuple[Secret, str]:
    """Run the host script's ``gateway`` step (idempotent), then read the client token and the published URL."""
    gateway = ctx.config.gateway
    if gateway is None:
        raise GatewayError("infra.gateway is not configured")
    url = published_url(run_host_step(ctx, "gateway"))
    token = read_token(gateway, gateway.client)
    ctx.redactor.add(token.reveal())
    return token, url
