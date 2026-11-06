from __future__ import annotations

import copy
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROBE_TIMEOUT_SECONDS = 15
UNAVAILABLE_PREFIX = "unavailable:"
DEGRADED_CAPACITY_PREFIX = "capacity-degraded:"
OWNER_REASON = "excluded by owner"
NO_LOGIN_REASON = "no logged-in broker account"
STALE_PIN_REASON = "stale pin"
NOT_SERVED_REASON = "granted but not served"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def strip_effort(target: str) -> str:
    provider, _, model = target.partition("/")
    return f"{provider}/{model.split(':', 1)[0]}" if model else target


def provider_of(target: str) -> str:
    return target.partition("/")[0]


def role_target(model: str, config: Mapping[str, Any]) -> str | None:
    if not model.startswith("@"):
        return model
    roles = config.get("modelRoles")
    target = roles.get(model[1:]) if isinstance(roles, Mapping) else None
    return target if isinstance(target, str) and target else None


def _chains(config: Mapping[str, Any]) -> Mapping[str, Any]:
    retry = config.get("retry")
    chains = retry.get("fallbackChains") if isinstance(retry, Mapping) else None
    return chains if isinstance(chains, Mapping) else {}


def _followers(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [item for item in value if isinstance(item, str)]
    return []


def candidates(model: str, config: Mapping[str, Any]) -> list[str]:
    target = role_target(model, config)
    if target is None:
        return []
    chains = _chains(config)
    followers = chains.get(target)
    if followers is None:
        followers = chains.get(strip_effort(target))
    return [target, *_followers(followers)]


def configured_models(config: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    roles = config.get("modelRoles")
    if isinstance(roles, Mapping):
        names.extend(target for target in roles.values() if isinstance(target, str) and target)
    for leader, followers in _chains(config).items():
        names.append(str(leader))
        names.extend(_followers(followers))
    return list(dict.fromkeys(names))


def gateway_models(config: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys(strip_effort(name) for name in configured_models(config)))


def routed(config: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(provider_of(model) for model in gateway_models(config)))


@dataclass(frozen=True)
class Served:
    models: frozenset[str]

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(provider_of(model) for model in self.models)

    def to_json(self) -> dict[str, Any]:
        return {"models": sorted(self.models), "providers": sorted(self.providers)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Served:
        return cls(frozenset(str(item) for item in data.get("models", ())))


class ProbeError(Exception):
    pass


class _Direct(urllib.request.BaseHandler):
    handler_order = 50

    def __init__(self, proxy: str) -> None:
        self.proxy = proxy.removeprefix("http://")

    def _route(self, request: urllib.request.Request) -> urllib.request.Request:
        request.set_proxy(self.proxy, request.type)
        return request

    http_request = https_request = _route


def _opener(proxy: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if proxy:
        handlers.append(_Direct(proxy))
    return urllib.request.build_opener(*handlers)


def _secure(url: str, allow_loopback_http: bool) -> bool:
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        return False
    return parsed.scheme == "https" or (
        allow_loopback_http and parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    )


def parse_models(raw: bytes) -> frozenset[str]:
    try:
        data = json.loads(raw)
        rows = data["data"]
        listed = frozenset(str(row["id"]) for row in rows if isinstance(row, Mapping) and row.get("id"))
    except (ValueError, KeyError, TypeError) as exc:
        raise ProbeError("gateway answered a malformed model list") from exc
    return listed


def probe(
    url: str,
    token: str,
    *,
    proxy: str = "",
    allow_loopback_http: bool = False,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Served:
    base = url.strip().rstrip("/")
    if not base or not _secure(base, allow_loopback_http):
        raise ProbeError("gateway URL is not an https:// URL")
    opener = _opener(proxy)
    try:
        with opener.open(urllib.request.Request(f"{base}/healthz", method="GET"), timeout=timeout):
            pass
    except urllib.error.HTTPError:
        pass
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError("gateway unreachable") from exc
    request = urllib.request.Request(
        f"{base}/v1/models", method="GET", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProbeError("client token rejected by the gateway") from exc
        raise ProbeError(f"gateway answered {exc.code} to the model listing") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError("gateway unreachable") from exc
    return Served(parse_models(body))


@dataclass(frozen=True)
class Resolution:
    target: str | None
    nominal: str
    reason: str = ""
    followers: tuple[str, ...] = ()
    degraded_capacity: bool = False
    rewrite: bool = False

    @property
    def substituted(self) -> bool:
        return self.target is not None and self.target != self.nominal

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target, "nominal": self.nominal}


def is_available(target: str, served: Served, excluded: Collection[str]) -> bool:
    return provider_of(target) not in excluded and strip_effort(target) in served.models


def _why(target: str, served: Served, excluded: Collection[str]) -> str:
    provider = provider_of(target)
    if provider in excluded:
        return f"{strip_effort(target)} was excluded after {provider} failed"
    return f"{strip_effort(target)} is not served"


def _window(target: str, windows: Mapping[str, int]) -> int | None:
    return windows.get(strip_effort(target))


def resolve(
    options: Sequence[str],
    served: Served | None,
    served_error: str,
    excluded: Collection[str],
    windows: Mapping[str, int],
    load: int,
) -> Resolution:
    nominal = options[0] if options else ""
    if not options:
        return Resolution(None, nominal, f"{UNAVAILABLE_PREFIX} no candidate model is configured")
    if served is None:
        return Resolution(None, nominal, f"{UNAVAILABLE_PREFIX} {served_error or 'the served models are unknown'}")
    live = [item for item in options if is_available(item, served, excluded)]
    if live and live[0] == options[0]:
        return Resolution(options[0], nominal, followers=tuple(live[1:]), rewrite=len(live) != len(options))
    fitting = [item for item in live if not load or ((window := _window(item, windows)) is not None and window >= load)]
    if fitting:
        chosen = fitting[0]
        return Resolution(chosen, nominal, followers=tuple(live[live.index(chosen) + 1 :]), rewrite=True)
    if live:
        return Resolution(
            None,
            nominal,
            f"{DEGRADED_CAPACITY_PREFIX} the change needs about {load} tokens of reading; no served candidate "
            f"({', '.join(strip_effort(item) for item in live)}) can hold it",
            degraded_capacity=True,
        )
    why = "; ".join(_why(item, served, excluded) for item in options)
    return Resolution(None, nominal, f"{UNAVAILABLE_PREFIX} {why}")


def effective_config(
    config: Mapping[str, Any], role: str, resolution: Resolution, options: Sequence[str]
) -> dict[str, Any]:
    held = copy.deepcopy(dict(config))
    if resolution.target is None:
        return held
    roles = dict(held.get("modelRoles") or {})
    roles[role] = resolution.target
    held["modelRoles"] = roles
    retry = dict(held.get("retry") or {})
    chains = dict(retry.get("fallbackChains") or {})
    for item in options:
        chains.pop(item, None)
        chains.pop(strip_effort(item), None)
    chains[strip_effort(resolution.target)] = list(resolution.followers)
    retry["fallbackChains"] = chains
    held["retry"] = retry
    return held


def degraded_providers(routed_providers: Iterable[str], served: Served | None, excluded: Collection[str]) -> list[str]:
    if served is None:
        return []
    return sorted(provider for provider in routed_providers if provider in excluded or provider not in served.providers)
