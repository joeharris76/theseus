"""Tailscale: the policy file (tagOwners, a grant and a test) and OAuth clients, with a root OAuth client's token.

The policy file is edited as HuJSON text, so the owner's comments and layout survive. The edit is validated by
Tailscale before it is saved, and saved only if the ETag still matches what was read.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .config import TailnetConfig
from .plan import Context
from .store import Secret
from .transport import HttpResponse, HttpTransport, bearer

API = "https://api.tailscale.com/api/v2"
ROOT_ID_NAME = "TAILSCALE_ROOT_CLIENT_ID"
ROOT_SECRET_NAME = "TAILSCALE_ROOT_CLIENT_SECRET"
HUJSON = "application/hujson"


class TailscaleError(RuntimeError):
    pass


def _form(**fields: str) -> bytes:
    return urllib.parse.urlencode(fields).encode()


def exchange_token(http: HttpTransport, client_id: str, client_secret: Secret) -> Secret | None:
    """A client-credentials exchange. ``None`` when Tailscale refuses the client."""
    response = http.request(
        "POST",
        f"{API}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=_form(grant_type="client_credentials", client_id=client_id, client_secret=client_secret.reveal()),
    )
    if not response.ok:
        return None
    token = response.json().get("access_token")
    return Secret(token) if isinstance(token, str) and token else None


def check_client(http: HttpTransport, client_id: str, secret: Secret) -> bool:
    return exchange_token(http, client_id, secret) is not None


class TailscaleClient:
    def __init__(self, ctx: Context, tailnet: str, token: Secret) -> None:
        self._ctx = ctx
        self._base = f"{API}/tailnet/{urllib.parse.quote(tailnet, safe='')}"
        self._token = token
        ctx.redactor.add(token.reveal())

    @classmethod
    def from_root(cls, ctx: Context) -> TailscaleClient:
        tailnet = ctx.config.tailnet
        if tailnet is None:
            raise TailscaleError("infra.tailnet is not configured")
        client_id, client_secret = ctx.secret(ROOT_ID_NAME), ctx.secret(ROOT_SECRET_NAME)
        if client_id is None or client_secret is None:
            raise TailscaleError(
                f"{ROOT_ID_NAME} and {ROOT_SECRET_NAME} are not both in the store; see 'seed' in docs/infra.md"
            )
        token = exchange_token(ctx.http, client_id.reveal(), client_secret)
        if token is None:
            raise TailscaleError("Tailscale refused the root OAuth client")
        return cls(ctx, tailnet.name, token)

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        accept: str = "application/json",
        extra: dict[str, str] | None = None,
    ) -> HttpResponse:
        headers = bearer(self._token.reveal(), Accept=accept, **{"Content-Type": content_type})
        return self._ctx.http.request(method, f"{self._base}{path}", headers={**headers, **(extra or {})}, body=body)

    def get_policy(self) -> tuple[str, str]:
        response = self._call("GET", "/acl", accept=HUJSON)
        etag = response.headers.get("etag")
        if not response.ok or not etag:
            raise TailscaleError(f"cannot read the policy file (HTTP {response.status})")
        return response.text(), etag

    def validate_policy(self, text: str) -> None:
        response = self._call("POST", "/acl/validate", body=text.encode(), content_type=HUJSON)
        problem = None
        if response.body.strip():
            try:
                data = response.json()
            except RuntimeError:
                data = {}
            problem = (
                data.get("message") if isinstance(data, dict) and (data.get("message") or data.get("data")) else None
            )
        if not response.ok or problem:
            raise TailscaleError(f"Tailscale rejects the edited policy: {problem or f'HTTP {response.status}'}")

    def save_policy(self, text: str, etag: str) -> None:
        response = self._call("POST", "/acl", body=text.encode(), content_type=HUJSON, extra={"If-Match": etag})
        if response.status == 412:
            raise TailscaleError("the policy file changed since it was read; run setup again")
        if not response.ok:
            raise TailscaleError(f"Tailscale refused the policy (HTTP {response.status})")

    def create_oauth_client(
        self, description: str, scopes: tuple[str, ...], tags: tuple[str, ...]
    ) -> tuple[str, Secret]:
        response = self._call(
            "POST",
            "/keys",
            body=json.dumps(
                {"keyType": "client", "description": description, "scopes": list(scopes), "tags": list(tags)}
            ).encode(),
        )
        if not response.ok:
            raise TailscaleError(f"cannot create the OAuth client (HTTP {response.status})")
        data = response.json()
        client_id, secret = data.get("id"), data.get("key")
        if not isinstance(client_id, str) or not isinstance(secret, str) or not secret:
            raise TailscaleError("Tailscale answered without a client id and secret")
        self._ctx.redactor.add(secret)
        return client_id, Secret(secret)

    def get_key(self, key_id: str) -> dict[str, Any] | None:
        response = self._call("GET", f"/keys/{urllib.parse.quote(key_id, safe='')}")
        if response.status == 404:
            return None
        if not response.ok:
            raise TailscaleError(f"cannot read key {key_id} (HTTP {response.status})")
        data = response.json()
        return data if isinstance(data, dict) else None

    def delete_key(self, key_id: str) -> None:
        response = self._call("DELETE", f"/keys/{urllib.parse.quote(key_id, safe='')}")
        if not response.ok and response.status != 404:
            raise TailscaleError(f"cannot delete key {key_id} (HTTP {response.status})")


# --- HuJSON -----------------------------------------------------------------------------------------------------


def _scan(text: str) -> dict[str, Any]:
    """Locate the top-level members of a HuJSON object without parsing values.

    Returns ``{"keys": {key: (open, close, last_significant_char, last_significant_index)}, "root": (close, char,
    index)}`` where indices point into ``text``.
    """
    keys: dict[str, tuple[int, int, str, int]] = {}
    stack: list[tuple[int, str | None]] = []
    last_char, last_index = "", -1
    pending_key: str | None = None
    root: tuple[int, str, int] | None = None
    root_open = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise TailscaleError("the policy file has an unterminated comment")
            i = end + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise TailscaleError("the policy file has an unterminated string")
            if len(stack) == 1 and last_char in "{,":
                pending_key = json.loads(text[i : j + 1])
            last_char, last_index = '"', j
            i = j + 1
        else:
            if c in "{[":
                if not stack and not root_open:
                    root_open = c
                stack.append((i, pending_key if len(stack) == 1 else None))
                pending_key = None
            elif c in "}]":
                if not stack:
                    raise TailscaleError("the policy file has unbalanced brackets")
                opener, key = stack.pop()
                if len(stack) == 1 and key is not None:
                    keys[key] = (opener, i, last_char, last_index)
                if not stack:
                    root = (i, last_char, last_index)
            last_char, last_index = c, i
            i += 1
    if root is None or root_open != "{":
        raise TailscaleError("the policy file is not a JSON object")
    return {"keys": keys, "root": root}


def standardize(text: str) -> Any:
    """Parse HuJSON: drop comments and trailing commas, then load as JSON."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            if c in "}]":
                while out and out[-1].isspace():
                    out.pop()
                if out and out[-1] == ",":
                    out.pop()
            out.append(c)
            i += 1
    try:
        policy = json.loads("".join(out))
    except ValueError as exc:
        raise TailscaleError("the policy file is not valid HuJSON") from exc
    if not isinstance(policy, dict):
        raise TailscaleError("the policy file is not a JSON object")
    return policy


def _insert(text: str, at: int, last_char: str, member: str, indent: str) -> str:
    lead = "" if last_char in "[{," else ","
    tail = "," if last_char == "," else ""
    return text[: at + 1] + f"{lead}\n{indent}{member}{tail}" + text[at + 1 :]


def _entry(value: Any) -> str:
    return json.dumps(value)


def expected(tailnet: TailnetConfig) -> dict[str, Any]:
    port, deny = tailnet.gateway_port, tailnet.deny_port
    return {
        "tagOwners": list(tailnet.tag_owners),
        "grant": {"src": [tailnet.tag], "dst": list(tailnet.gateway_dst), "ip": [f"tcp:{port}"]},
        "test": {
            "src": tailnet.tag,
            "accept": [f"{dst}:{port}" for dst in tailnet.gateway_dst],
            "deny": [f"{dst}:{deny}" for dst in tailnet.gateway_dst],
        },
    }


def missing(policy: Any, tailnet: TailnetConfig) -> list[str]:
    """Which of tagOwners, grant and test the policy still lacks, as ``tagOwners``, ``grants`` and ``tests``."""
    want = expected(tailnet)
    lacking: list[str] = []
    if tailnet.tag not in (policy.get("tagOwners") or {}):
        lacking.append("tagOwners")
    if not any(
        isinstance(grant, dict)
        and tailnet.tag in _as_list(grant.get("src"))
        and set(want["grant"]["dst"]) <= set(_as_list(grant.get("dst")))
        and want["grant"]["ip"][0] in _as_list(grant.get("ip"))
        for grant in policy.get("grants") or []
    ):
        lacking.append("grants")
    if not any(
        isinstance(test, dict)
        and test.get("src") == tailnet.tag
        and set(want["test"]["accept"]) <= set(_as_list(test.get("accept")))
        and set(want["test"]["deny"]) <= set(_as_list(test.get("deny")))
        for test in policy.get("tests") or []
    ):
        lacking.append("tests")
    return lacking


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


def edit_policy(text: str, tailnet: TailnetConfig) -> str:
    """Add what ``missing`` reports to the HuJSON text, leaving everything else byte-for-byte."""
    lacking = missing(standardize(text), tailnet)
    want = expected(tailnet)
    additions = {
        "tagOwners": (f"{_entry(tailnet.tag)}: {_entry(want['tagOwners'])}", "{", "}"),
        "grants": (_entry(want["grant"]), "[", "]"),
        "tests": (_entry(want["test"]), "[", "]"),
    }
    for key in lacking:
        member, opener, closer = additions[key]
        found = _scan(text)
        if key in found["keys"]:
            _, close, last_char, last_index = found["keys"][key]
            if text[found["keys"][key][0]] != opener:
                raise TailscaleError(f"the policy file's {key} is not a {'object' if opener == '{' else 'list'}")
            text = _insert(text, last_index, last_char, member, "    ")
        else:
            _, last_char, last_index = found["root"]
            text = _insert(text, last_index, last_char, f'"{key}": {opener}\n    {member}\n  {closer}', "  ")
    return text
