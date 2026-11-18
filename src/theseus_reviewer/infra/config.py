"""Parse and validate the ``infra:`` section of a consumer policy file.

The policy parser accepts the key and ignores it, so only ``theseus infra`` reads this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._vendor import yaml

DEFAULT_STORE_DIR = "~/.config/theseus/infra"
DEFAULT_STATE_DIR = "~/.config/theseus/infra-state"
DEFAULT_GATEWAY_DIR = "~/.omp/review-gateway"
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,99}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
TAG = re.compile(r"tag:[A-Za-z][A-Za-z0-9-]*")
CLIENT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DEFAULT_APP_ID_SECRET = "REVIEW_APP_ID"

LITERAL = "literal"
APP_KEY = "app_key"
GATEWAY_TOKEN = "gateway_token"
GATEWAY_URL = "gateway_url"
TAILSCALE_OAUTH = "tailscale_oauth"
OPENAI_SERVICE_ACCOUNT = "openai_service_account"
STORE = "store"
OWNER = "owner"
TYPESAFE_ROTATOR = "typesafe"

_SOURCE_KEYS: dict[str, frozenset[str]] = {
    LITERAL: frozenset({"source", "value"}),
    APP_KEY: frozenset({"source", "app_slug", "pem_dir", "max_age_days", "app_id_secret"}),
    GATEWAY_TOKEN: frozenset({"source"}),
    GATEWAY_URL: frozenset({"source"}),
    TAILSCALE_OAUTH: frozenset({"source", "field"}),
    OPENAI_SERVICE_ACCOUNT: frozenset({"source", "project", "name"}),
    STORE: frozenset({"source", "rotator", "max_age_days", "mirror_keychain"}),
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StoreConfig:
    backend: str
    directory: Path | None


@dataclass(frozen=True)
class GithubConfig:
    repository: str
    environment: str
    branch_policies: tuple[str, ...]


@dataclass(frozen=True)
class OAuthClientConfig:
    scopes: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class TailnetConfig:
    name: str
    tag: str
    tag_owners: tuple[str, ...]
    gateway_port: int
    deny_port: int
    gateway_dst: tuple[str, ...]
    oauth_client: OAuthClientConfig


@dataclass(frozen=True)
class GatewayConfig:
    host_script: str
    client: str
    directory: Path


@dataclass(frozen=True)
class SecretSpec:
    name: str
    source: str
    value: str | None = None
    field: str | None = None
    app_slug: str | None = None
    pem_dir: Path | None = None
    app_id_secret: str | None = None
    max_age_days: int | None = None
    project: str | None = None
    account_name: str | None = None
    rotator: str | None = None
    mirror_keychain: str | None = None


@dataclass(frozen=True)
class InfraConfig:
    path: Path
    repo_root: Path
    store: StoreConfig
    github: GithubConfig
    tailnet: TailnetConfig | None
    gateway: GatewayConfig | None
    secrets: tuple[SecretSpec, ...]
    state_path: Path

    def secret(self, name: str) -> SecretSpec:
        for spec in self.secrets:
            if spec.name == name:
                return spec
        raise ConfigError(f"no secret named {name} in {self.path}")

    @property
    def environment_key(self) -> str:
        return f"{self.github.repository}:{self.github.environment}"


def _mapping(
    value: Any, where: str, allowed: frozenset[str] | None = None, required: Sequence[str] = ()
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    data = {str(key): item for key, item in value.items()}
    if allowed is not None:
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigError(f"{where} has unknown key {unknown[0]!r}; allowed keys are {sorted(allowed)}")
    for key in required:
        if key not in data:
            raise ConfigError(f"{where} needs {key}")
    return data


def _text(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int) or not str(value).strip():
        raise ConfigError(f"{where} must be a non-empty string")
    text = str(value).strip()
    if pattern is not None and pattern.fullmatch(text) is None:
        raise ConfigError(f"{where} is not valid: {text!r}")
    return text


def _texts(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list")
    items = tuple(_text(item, f"{where} item", pattern) for item in value)
    if len(set(items)) != len(items):
        raise ConfigError(f"{where} has a duplicate")
    return items


def _port(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"{where} must be a port number")
    return value


def _days(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} must be a positive number of days")
    return value


def _path(value: Any, where: str) -> Path:
    return Path(_text(value, where)).expanduser()


def _store(data: Any) -> StoreConfig:
    store = _mapping(data, "infra.store", frozenset({"backend", "dir"}), ("backend",))
    backend = _text(store["backend"], "infra.store.backend")
    if backend not in ("keychain", "file"):
        raise ConfigError("infra.store.backend must be keychain or file")
    if backend == "keychain":
        if "dir" in store:
            raise ConfigError("infra.store.dir applies only to the file backend")
        return StoreConfig(backend, None)
    return StoreConfig(backend, _path(store.get("dir", DEFAULT_STORE_DIR), "infra.store.dir"))


def _github(data: Any) -> GithubConfig:
    github = _mapping(
        data, "infra.github", frozenset({"repository", "environment", "branch_policies"}), ("repository", "environment")
    )
    policies = _texts(github["branch_policies"], "infra.github.branch_policies") if "branch_policies" in github else ()
    return GithubConfig(
        _text(github["repository"], "infra.github.repository", REPOSITORY),
        _text(github["environment"], "infra.github.environment", re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")),
        policies,
    )


def _tailnet(data: Any) -> TailnetConfig:
    keys = frozenset({"name", "tag", "tag_owners", "gateway_port", "deny_port", "gateway_dst", "oauth_client"})
    tailnet = _mapping(
        data, "infra.tailnet", keys, ("tag", "tag_owners", "gateway_port", "deny_port", "gateway_dst", "oauth_client")
    )
    client = _mapping(
        tailnet["oauth_client"],
        "infra.tailnet.oauth_client",
        frozenset({"scopes", "description"}),
        ("scopes", "description"),
    )
    gateway_port = _port(tailnet["gateway_port"], "infra.tailnet.gateway_port")
    deny_port = _port(tailnet["deny_port"], "infra.tailnet.deny_port")
    if gateway_port == deny_port:
        raise ConfigError("infra.tailnet.deny_port must differ from gateway_port")
    return TailnetConfig(
        _text(tailnet.get("name", "-"), "infra.tailnet.name"),
        _text(tailnet["tag"], "infra.tailnet.tag", TAG),
        _texts(tailnet["tag_owners"], "infra.tailnet.tag_owners"),
        gateway_port,
        deny_port,
        _texts(tailnet["gateway_dst"], "infra.tailnet.gateway_dst"),
        OAuthClientConfig(
            _texts(client["scopes"], "infra.tailnet.oauth_client.scopes"),
            _text(client["description"], "infra.tailnet.oauth_client.description"),
        ),
    )


def _gateway(data: Any) -> GatewayConfig:
    gateway = _mapping(data, "infra.gateway", frozenset({"host_script", "client", "dir"}), ("host_script", "client"))
    return GatewayConfig(
        _text(gateway["host_script"], "infra.gateway.host_script"),
        _text(gateway["client"], "infra.gateway.client", CLIENT_NAME),
        _path(gateway.get("dir", DEFAULT_GATEWAY_DIR), "infra.gateway.dir"),
    )


def _secret(name: str, data: Any) -> SecretSpec:
    where = f"infra.secrets.{name}"
    if SECRET_NAME.fullmatch(name) is None:
        raise ConfigError(f"{where}: {name!r} is not a valid secret name")
    base = _mapping(data, where)
    source = _text(base.get("source"), f"{where}.source")
    if source not in _SOURCE_KEYS:
        raise ConfigError(f"{where}.source must be one of {sorted(_SOURCE_KEYS)}")
    item = _mapping(data, where, _SOURCE_KEYS[source])
    if source == LITERAL:
        return SecretSpec(name, source, value=_text(item.get("value"), f"{where}.value"))
    if source == APP_KEY:
        for key in ("app_slug", "pem_dir"):
            if key not in item:
                raise ConfigError(f"{where} needs {key}")
        return SecretSpec(
            name,
            source,
            app_slug=_text(item["app_slug"], f"{where}.app_slug", re.compile(r"[a-z0-9][a-z0-9-]*")),
            pem_dir=_path(item["pem_dir"], f"{where}.pem_dir"),
            app_id_secret=_text(
                item.get("app_id_secret", DEFAULT_APP_ID_SECRET), f"{where}.app_id_secret", SECRET_NAME
            ),
            max_age_days=_days(item["max_age_days"], f"{where}.max_age_days") if "max_age_days" in item else None,
        )
    if source == TAILSCALE_OAUTH:
        field = _text(item.get("field"), f"{where}.field")
        if field not in ("id", "secret"):
            raise ConfigError(f"{where}.field must be id or secret")
        return SecretSpec(name, source, field=field)
    if source == OPENAI_SERVICE_ACCOUNT:
        for key in ("project", "name"):
            if key not in item:
                raise ConfigError(f"{where} needs {key}")
        return SecretSpec(
            name,
            source,
            project=_text(item["project"], f"{where}.project", re.compile(r"[A-Za-z0-9_-]+")),
            account_name=_text(item["name"], f"{where}.name"),
        )
    if source == STORE:
        rotator = _text(item.get("rotator", OWNER), f"{where}.rotator")
        if rotator == "manual":
            raise ConfigError(
                f"{where}.rotator: manual no longer names a rotator; use rotator: {TYPESAFE_ROTATOR} for a Typesafe "
                f"key, or omit it (or write {OWNER}) for a secret only the owner seeds"
            )
        if rotator not in (OWNER, TYPESAFE_ROTATOR):
            raise ConfigError(f"{where}.rotator must be {OWNER} or {TYPESAFE_ROTATOR}")
        if rotator != TYPESAFE_ROTATOR and "mirror_keychain" in item:
            raise ConfigError(f"{where}.mirror_keychain applies only to rotator: {TYPESAFE_ROTATOR}")
        return SecretSpec(
            name,
            source,
            rotator=rotator,
            max_age_days=_days(item["max_age_days"], f"{where}.max_age_days") if "max_age_days" in item else None,
            mirror_keychain=_text(item["mirror_keychain"], f"{where}.mirror_keychain")
            if "mirror_keychain" in item
            else None,
        )
    return SecretSpec(name, source)


def _state_path(data: Mapping[str, Any], github: GithubConfig) -> Path:
    if "state" in data:
        return _path(data["state"], "infra.state")
    slug = f"{github.repository.replace('/', '__')}__{github.environment}.json"
    return Path(DEFAULT_STATE_DIR).expanduser() / slug


def parse_infra(data: Any, path: Path) -> InfraConfig:
    keys = frozenset({"store", "github", "tailnet", "gateway", "secrets", "state"})
    infra = _mapping(data, "infra", keys, ("store", "github", "secrets"))
    github = _github(infra["github"])
    secrets_data = _mapping(infra["secrets"], "infra.secrets")
    if not secrets_data:
        raise ConfigError("infra.secrets must name at least one secret")
    secrets = tuple(_secret(str(name), value) for name, value in secrets_data.items())
    tailnet = _tailnet(infra["tailnet"]) if "tailnet" in infra else None
    gateway = _gateway(infra["gateway"]) if "gateway" in infra else None
    by_name = {spec.name: spec for spec in secrets}
    for spec in secrets:
        if spec.source == TAILSCALE_OAUTH and tailnet is None:
            raise ConfigError(f"infra.secrets.{spec.name} reads a tailscale_oauth client, so infra.tailnet is required")
        if spec.source in (GATEWAY_TOKEN, GATEWAY_URL) and gateway is None:
            raise ConfigError(f"infra.secrets.{spec.name} reads the gateway, so infra.gateway is required")
        if spec.source == APP_KEY:
            target = by_name.get(spec.app_id_secret or "")
            if target is None or target.source != LITERAL or not (target.value or "").isdigit():
                raise ConfigError(
                    f"infra.secrets.{spec.name} checks the key against the App id, which must be a numeric literal "
                    f"secret named {spec.app_id_secret}"
                )
    for source in (GATEWAY_TOKEN, GATEWAY_URL):
        if sum(spec.source == source for spec in secrets) > 1:
            raise ConfigError(f"infra.secrets names the {source} source more than once")
    for field in ("id", "secret"):
        if sum(spec.source == TAILSCALE_OAUTH and spec.field == field for spec in secrets) > 1:
            raise ConfigError(f"infra.secrets names the tailscale_oauth {field} more than once")
    return InfraConfig(
        path=path,
        repo_root=_repo_root(path),
        store=_store(infra["store"]),
        github=github,
        tailnet=tailnet,
        gateway=gateway,
        secrets=secrets,
        state_path=_state_path(infra, github),
    )


def _repo_root(path: Path) -> Path:
    resolved = path.resolve()
    return resolved.parent.parent if resolved.parent.name == ".github" else resolved.parent


def load_infra(path: Path) -> InfraConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, Mapping) or "infra" not in data:
        raise ConfigError(f"{path} has no infra: section")
    return parse_infra(data["infra"], path)


__all__ = [
    "ConfigError",
    "InfraConfig",
    "SecretSpec",
    "load_infra",
    "parse_infra",
]
