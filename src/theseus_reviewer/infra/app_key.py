"""The GitHub App's private key: picked up from a download directory and checked before it is stored.

GitHub offers no API to create or delete an App private key, so the owner generates it in the App's settings.
The key is checked by signing a JWT with ``openssl`` (the key on stdin, never in argv) and asking GitHub for the
App; the id must match.
"""

from __future__ import annotations

import base64
import json
import tempfile
from datetime import datetime
from pathlib import Path

from .config import SecretSpec
from .plan import Context
from .store import Secret
from .transport import bearer

APP_URL = "https://api.github.com/app"
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540


class AppKeyError(RuntimeError):
    pass


def newest_pem(spec: SecretSpec) -> Path | None:
    """The most recently written ``<slug>.*.private-key.pem`` in the key directory, if any."""
    if spec.pem_dir is None or spec.app_slug is None or not spec.pem_dir.is_dir():
        return None
    found = [path for path in spec.pem_dir.glob(f"{spec.app_slug}.*.private-key.pem") if path.is_file()]
    return max(found, key=lambda path: (path.stat().st_mtime, path.name), default=None)


def fingerprint(path: Path) -> str:
    """A name and modification time that changes when a new key file appears; never the content."""
    return f"{path.name}@{int(path.stat().st_mtime)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(ctx: Context, pem: Secret, app_id: str) -> str:
    now = int(ctx.now().timestamp())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(
        json.dumps(
            {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": app_id}, separators=(",", ":")
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    with tempfile.TemporaryDirectory(prefix="theseus-jwt-") as scratch:
        message, signature = Path(scratch) / "message", Path(scratch) / "signature"
        message.write_text(signing_input, encoding="ascii")
        result = ctx.run(
            ["openssl", "dgst", "-sha256", "-sign", "/dev/stdin", "-out", str(signature), str(message)],
            stdin=pem.reveal(),
        )
        if result.returncode != 0 or not signature.exists():
            raise AppKeyError("openssl could not sign with this key; is it an RSA private key in PEM form?")
        return f"{signing_input}.{_b64(signature.read_bytes())}"


def validate(ctx: Context, pem: Secret, app_id: str, slug: str) -> None:
    """Sign a JWT and require ``GET /app`` to name the expected App."""
    token = sign_jwt(ctx, pem, app_id)
    ctx.redactor.add(token)
    response = ctx.http.request("GET", APP_URL, headers=bearer(token, Accept="application/vnd.github+json"))
    if not response.ok:
        raise AppKeyError(f"GitHub rejected the key's JWT (HTTP {response.status})")
    app = response.json()
    if not isinstance(app, dict) or str(app.get("id")) != app_id:
        raise AppKeyError(f"the key belongs to App {app.get('id') if isinstance(app, dict) else '?'}, not {app_id}")
    if app.get("slug") not in (None, slug):
        raise AppKeyError(f"the key belongs to App {app.get('slug')}, not {slug}")


def age_days(ctx: Context, name: str) -> float | None:
    modified: datetime | None = ctx.store.modified_at(name)
    return None if modified is None else (ctx.now() - modified).total_seconds() / 86400
