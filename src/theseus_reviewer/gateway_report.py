from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import providers
from ._vendor import yaml

HEALTHY = "healthy"
DEGRADED = "degraded"
BROKEN = "broken"
EXIT_CODES = {HEALTHY: 0, DEGRADED: 3, BROKEN: 1}


def accounts(path: Path) -> dict[str, list[str]]:
    held: dict[str, list[str]] = {}
    for entry in json.loads(path.read_text(encoding="utf-8")).get("accounts", []):
        if entry.get("provider") and entry.get("identityKey"):
            held.setdefault(str(entry["provider"]), []).append(str(entry["identityKey"]))
    return held


def _pinned(path: Path) -> dict[str, list[str]] | None:
    for client in json.loads(path.read_text(encoding="utf-8")).values():
        if isinstance(client, Mapping) and isinstance(client.get("accounts"), Mapping):
            return {str(name): list(keys) for name, keys in client["accounts"].items()}
    return None


def _reason(row: Mapping[str, Any]) -> str:
    if row["excluded_by_owner"]:
        return providers.OWNER_REASON
    if row["logged_in"] is False:
        return providers.NO_LOGIN_REASON
    if row["stale_pin"]:
        return providers.STALE_PIN_REASON
    if row["granted"] and row["served"] is False:
        return providers.NOT_SERVED_REASON
    return ""


def build(
    config: Mapping[str, Any],
    accounts: Mapping[str, list[str]] | None,
    pinned: Mapping[str, list[str]] | None,
    owner: set[str],
    served: providers.Served | None,
    served_error: str | None,
) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for provider in providers.routed(config):
        keys = None if accounts is None else accounts.get(provider, [])
        logged_in = None if keys is None else bool(keys)
        stale = None if keys is None or pinned is None else bool(keys) and set(pinned.get(provider, ())) != set(keys)
        row: dict[str, Any] = {
            "routed": True,
            "logged_in": logged_in,
            "stale_pin": stale,
            "excluded_by_owner": provider in owner,
            "granted": provider not in owner and logged_in is not False,
            "served": None if served is None else provider in served.providers,
        }
        row["reason"] = _reason(row)
        rows[provider] = row
    roles: dict[str, Any] = {}
    for role, target in (config.get("modelRoles") or {}).items():
        options = providers.candidates(f"@{role}", config)
        usable = [
            item
            for item in options
            if rows.get(providers.provider_of(item), {}).get("granted")
            and rows[providers.provider_of(item)]["served"] is not False
        ]
        roles[str(role)] = {"primary": str(target), "first_served": usable[0] if usable else None}
    live = [row for row in rows.values() if row["granted"] and row["served"] is not False]
    if served_error or not live:
        state = BROKEN
    elif any(row["reason"] and not row["excluded_by_owner"] for row in rows.values()):
        state = DEGRADED
    else:
        state = HEALTHY
    return {"state": state, "served_error": served_error, "providers": rows, "roles": roles}


def command(args: argparse.Namespace) -> int:
    try:
        config = yaml.safe_load(Path(args.omp_config).read_text(encoding="utf-8"))
        held = accounts(Path(args.accounts)) if args.accounts else None
        pinned = _pinned(Path(args.clients)) if args.clients else None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"review-verdict: error: {exc}", file=sys.stderr)
        return EXIT_CODES[BROKEN]
    served: providers.Served | None = None
    served_error: str | None = None
    if args.served_url:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip() if args.token_file else ""
            served = providers.probe(args.served_url, token, allow_loopback_http=True)
        except (OSError, providers.ProbeError) as exc:
            served_error = str(exc)
    report = build(
        config if isinstance(config, Mapping) else {}, held, pinned, set(args.exclude_provider), served, served_error
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for provider, row in report["providers"].items():
            print(f"{provider}: {row['reason'] or 'ok'}")
        print(f"state: {report['state']}")
    return EXIT_CODES[report["state"]]
