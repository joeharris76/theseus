from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import commands, credentials, promotion
from ._vendor import yaml
from .diff import Hunk, hunk_index
from .providers import candidates, configured_models, routed, strip_effort

OVERHEAD_TOKENS = {"inline": 80_000, "file-list": 170_000, "promotion": 170_000}
BYTES_PER_TOKEN = 2.27
USAGE_LIMIT_FALLBACK = "usage-limit"
CATALOG_TIMEOUT_SECONDS = 60
CATALOG_GATEWAY_URL = "https://catalog.invalid"
CATALOG_TOKEN = json.dumps({"oauthAccessToken": "catalog", "apiKey": "catalog"})
CAPACITY_KEY = "capacity"
ERROR_KEY = "capacity_error"
LOAD_KEY = "review_load_tokens"
SPLIT_INSTRUCTION = "split the change into smaller pull requests"
SKIP_PREFIX = "skipped: the change needs about"
UNKNOWN_PREFIX = "skipped: this reviewer's capacity cannot be determined:"
UNLISTED_CAUSE = "its primary model is not in the model catalog"

Catalog = Callable[[], Mapping[str, int]]


class CatalogError(Exception):
    pass


def tokens(byte_count: int) -> int:
    return math.ceil(max(byte_count, 0) / BYTES_PER_TOKEN)


def review_load(required_bytes: int, brief_bytes: int, mode: str) -> int:
    return OVERHEAD_TOKENS[mode] + tokens(required_bytes) + tokens(brief_bytes)


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


def _fallback_on(config: Mapping[str, Any]) -> object:
    retry = config.get("retry")
    return retry.get("fallbackOn") if isinstance(retry, Mapping) else None


def _window(model: str, catalog: Mapping[str, int]) -> int | None:
    if model in catalog:
        return catalog[model]
    return catalog.get(strip_effort(model))


def unlisted_models(config: Mapping[str, Any], catalog: Mapping[str, int]) -> list[str]:
    return list(
        dict.fromkeys(strip_effort(name) for name in configured_models(config) if _window(name, catalog) is None)
    )


def model_windows(config: Mapping[str, Any], catalog: Mapping[str, int]) -> dict[str, int]:
    return {
        strip_effort(name): window
        for name in configured_models(config)
        if (window := _window(name, catalog)) is not None
    }


def reviewer_capacity(model: str, config: Mapping[str, Any], catalog: Mapping[str, int]) -> int | None:
    chain = candidates(model, config)
    if not chain or (primary := _window(chain[0], catalog)) is None:
        return None
    if _fallback_on(config) == USAGE_LIMIT_FALLBACK:
        return primary
    return min([primary, *(window for item in chain[1:] if (window := _window(item, catalog)) is not None)])


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
    try:
        held = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"the model configuration is not readable: {exc}") from exc
    providers = routed(held if isinstance(held, Mapping) else {})
    with tempfile.TemporaryDirectory(prefix="review-catalog-") as scratch:
        home = Path(scratch) / "home"
        agent = home / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "config.yml").write_text(config_text, encoding="utf-8")
        (agent / "models.yml").write_text(
            credentials.gateway_models_text(CATALOG_GATEWAY_URL, providers), encoding="utf-8"
        )
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


def skip_text(load: int, capacity: int) -> str:
    return f"{SKIP_PREFIX} {load} tokens of reading; this reviewer's capacity is {capacity}"


def unknown_text(cause: str) -> str:
    return f"{UNKNOWN_PREFIX} {cause or UNLISTED_CAUSE}"
