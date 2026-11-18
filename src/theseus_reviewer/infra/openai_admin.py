"""OpenAI project service accounts, created and revoked with the organization Admin API key."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .plan import Context
from .store import Secret
from .transport import HttpTransport, bearer

API = "https://api.openai.com/v1"
ADMIN_KEY_NAME = "OPENAI_ADMIN_KEY"


class OpenAIError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewKey:
    service_account_id: str | None
    key_id: str
    secret: Secret


def check_key(http: HttpTransport, key: Secret) -> bool:
    return http.request("GET", f"{API}/models", headers=bearer(key.reveal())).status == 200


def _quote(part: str) -> str:
    return urllib.parse.quote(part, safe="")


class OpenAIAdmin:
    def __init__(self, ctx: Context) -> None:
        admin = ctx.secret(ADMIN_KEY_NAME)
        if admin is None:
            raise OpenAIError(
                f"{ADMIN_KEY_NAME} is not in the store; seed it with 'theseus infra seed {ADMIN_KEY_NAME} --config PATH'"
            )
        self._ctx = ctx
        self._admin = admin

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._ctx.http.request(
            method,
            f"{API}{path}",
            headers=bearer(self._admin.reveal(), **{"Content-Type": "application/json"}),
            body=None if body is None else json.dumps(body).encode(),
        )
        if not response.ok:
            raise OpenAIError(f"OpenAI {method} {path} answered HTTP {response.status}")
        data = response.json() if response.body.strip() else {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _key(data: dict[str, Any], service_account_id: str | None) -> NewKey:
        """The create response nests the key under ``api_key``; a later mint returns it at the top level."""
        key = data.get("api_key") if isinstance(data.get("api_key"), dict) else data
        value, key_id = key.get("value"), key.get("id")
        if not isinstance(value, str) or not value or not isinstance(key_id, str):
            raise OpenAIError("OpenAI answered without a usable API key")
        return NewKey(service_account_id, key_id, Secret(value))

    def create_service_account(self, project: str, name: str) -> NewKey:
        data = self._call("POST", f"/organization/projects/{_quote(project)}/service_accounts", {"name": name})
        account = data.get("id")
        created = self._key(data, str(account) if account else None)
        self._ctx.redactor.add(created.secret.reveal())
        if created.service_account_id is None:
            raise OpenAIError("OpenAI answered without a service account id")
        return created

    def mint_key(self, project: str, service_account_id: str, name: str) -> NewKey:
        data = self._call(
            "POST",
            f"/organization/projects/{_quote(project)}/service_accounts/{_quote(service_account_id)}/api_keys",
            {"name": name},
        )
        minted = self._key(data, service_account_id)
        self._ctx.redactor.add(minted.secret.reveal())
        return minted

    def delete_key(self, project: str, key_id: str) -> None:
        self._call("DELETE", f"/organization/projects/{_quote(project)}/api_keys/{_quote(key_id)}")
