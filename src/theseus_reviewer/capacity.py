from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import commands, credentials, promotion
from ._vendor import yaml
from .diff import Hunk, hunk_index

OVERHEAD_TOKENS = 150_000
BYTES_PER_TOKEN = 2.27
UNKNOWN_CAPACITY_TOKENS = 200_000
CATALOG_TIMEOUT_SECONDS = 60
CATALOG_GATEWAY_URL = "https://catalog.invalid"
CATALOG_TOKEN = json.dumps({"oauthAccessToken": "catalog", "apiKey": "catalog"})
CAPACITY_KEY = "capacity"
LOAD_KEY = "review_load_tokens"
SPLIT_INSTRUCTION = "split the change into smaller pull requests"
SKIP_PREFIX = "skipped: the change needs about"

Catalog = Callable[[], Mapping[str, int]]


class CatalogError(Exception):
    pass


def tokens(byte_count: int) -> int:
    return math.ceil(max(byte_count, 0) / BYTES_PER_TOKEN)


def review_load(required_bytes: int, brief_bytes: int) -> int:
    return OVERHEAD_TOKENS + tokens(required_bytes) + tokens(brief_bytes)


def _block_bytes(blocks: Iterable[Hunk]) -> int:
    return sum(block.size for block in blocks)


def required_bytes(
    evidence_files: Iterable[str],
    diff_text: str | None,
    plan_dir: Path | None = None,
    whole: Collection[str] = (),
) -> int:
    index = hunk_index(diff_text or "")
    staged = promotion.STAGED_PROMOTION_DIR
    root = None if plan_dir is None else plan_dir / promotion.PROMOTION_PLAN_DIR
    upstream: dict[str, list[Hunk]] = {}
    if root is not None and (root / promotion.UPSTREAM_DIFF_NAME).is_file():
        text = (root / promotion.UPSTREAM_DIFF_NAME).read_bytes().decode("utf-8", "replace")
        upstream = hunk_index(text)
    total = 0
    for path in evidence_files:
        if path.startswith(f"{staged}/old/"):
            relative = path.removeprefix(f"{staged}/old/")
            held = None if root is None else root / "old" / relative
            if held is not None and held.is_file():
                total += held.stat().st_size
            else:
                total += _block_bytes(upstream.get(relative, ()))
        elif path.startswith(f"{staged}/new/"):
            relative = path.removeprefix(f"{staged}/new/")
            held = None if root is None or path not in whole else root / "new" / relative
            if held is not None and held.is_file():
                total += held.stat().st_size
            else:
                total += _block_bytes(upstream.get(relative, ()))
        else:
            total += _block_bytes(index.get(path, ()))
    return total


def _strip_effort(target: str) -> str:
    provider, _, model = target.partition("/")
    return f"{provider}/{model.split(':', 1)[0]}" if model else target


def _role_target(model: str, config: Mapping[str, Any]) -> str | None:
    if not model.startswith("@"):
        return model
    roles = config.get("modelRoles")
    target = roles.get(model[1:]) if isinstance(roles, Mapping) else None
    return target if isinstance(target, str) and target else None


def reviewer_models(model: str, config: Mapping[str, Any]) -> list[str]:
    target = _role_target(model, config)
    if target is None:
        return []
    models = [target]
    retry = config.get("retry")
    chains = retry.get("fallbackChains") if isinstance(retry, Mapping) else None
    if isinstance(chains, Mapping):
        followers = chains.get(target)
        if followers is None:
            followers = chains.get(_strip_effort(target))
        if isinstance(followers, Sequence) and not isinstance(followers, str):
            models.extend(item for item in followers if isinstance(item, str))
    return models


def _window(model: str, catalog: Mapping[str, int]) -> int | None:
    if model in catalog:
        return catalog[model]
    return catalog.get(_strip_effort(model))


def reviewer_capacity(model: str, config: Mapping[str, Any], catalog: Mapping[str, int]) -> int | None:
    known = [window for item in reviewer_models(model, config) if (window := _window(item, catalog)) is not None]
    return min(known) if known else None


def reviewer_capacities(
    chain: Iterable[Mapping[str, Any]], config: Mapping[str, Any], catalog: Mapping[str, int]
) -> dict[str, int]:
    held: dict[str, int] = {}
    for reviewer in chain:
        capacity = reviewer_capacity(str(reviewer["model"]), config, catalog)
        if capacity is not None:
            held[str(reviewer["name"])] = capacity
    return held


def parse_catalog(text: str) -> dict[str, int]:
    try:
        data = json.loads(text)
        listed = data["models"]
        entries = {
            str(item["selector"]): int(item["contextWindow"]) - int(item["maxTokens"])
            for item in listed
            if isinstance(item, Mapping)
            and isinstance(item.get("contextWindow"), int)
            and isinstance(item.get("maxTokens"), int)
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogError(f"the model catalog is not readable: {exc}") from exc
    if not entries:
        raise CatalogError("the model catalog lists no models")
    return entries


def read_catalog(
    binary: Path,
    config_text: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="oracle-catalog-") as scratch:
        home = Path(scratch) / "home"
        agent = home / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "config.yml").write_text(config_text, encoding="utf-8")
        (agent / "models.yml").write_text(credentials.gateway_models_text(CATALOG_GATEWAY_URL), encoding="utf-8")
        overlay = commands.write_overlay(Path(scratch))
        env = {**commands.omp_env(), "HOME": str(home), "TMPDIR": scratch, credentials.GATEWAY_ENV[0]: CATALOG_TOKEN}
        try:
            result = run(
                [str(binary), "models", "--json", "--no-extensions", "--config", str(overlay)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=CATALOG_TIMEOUT_SECONDS,
                env=env,
                cwd=scratch,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CatalogError(f"the model catalog could not be listed: {exc}") from exc
    if result.returncode != 0:
        raise CatalogError(f"the model catalog could not be listed: exit code {result.returncode}")
    return parse_catalog(result.stdout)


def measure(plan: Mapping[str, Any], config_text: str, catalog: Catalog) -> dict[str, int]:
    try:
        held = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"the model configuration is not readable: {exc}") from exc
    return reviewer_capacities(plan["chain"], held if isinstance(held, Mapping) else {}, catalog())


def effective(capacities: Mapping[str, int], reviewer: str) -> tuple[int, bool]:
    known = capacities.get(reviewer)
    return (UNKNOWN_CAPACITY_TOKENS, False) if known is None else (known, True)


def skip_text(load: int, capacity: int, known: bool) -> str:
    if known:
        return f"{SKIP_PREFIX} {load} tokens of reading; this reviewer's capacity is {capacity}"
    return (
        f"{SKIP_PREFIX} {load} tokens of reading; this reviewer's capacity is unknown and at most {capacity} is assumed"
    )
