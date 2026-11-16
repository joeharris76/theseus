"""Typesafe has no key-management API; the key is seeded by the owner and only checked here."""

from __future__ import annotations

from .store import Secret
from .transport import HttpTransport, bearer

MODELS_URL = "https://api.typesafe.ai/v1/models"


def check_key(http: HttpTransport, key: Secret) -> bool:
    return http.request("GET", MODELS_URL, headers=bearer(key.reveal())).status == 200
