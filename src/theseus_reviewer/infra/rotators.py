"""One rotator per credential kind: gateway token, Tailscale OAuth client, OpenAI service-account key, Typesafe key
and the GitHub App private key. Each plugs into ``rotate.Engine``; none of them puts a value in an error message.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import app_key, gateway, github_env, openai_admin, tailscale, typesafe
from .config import (
    APP_KEY,
    GATEWAY_TOKEN,
    OPENAI_SERVICE_ACCOUNT,
    STORE,
    TAILSCALE_OAUTH,
    InfraConfig,
    SecretSpec,
)
from .plan import Context
from .rotate import PREVIOUS_SUFFIX, Created, RotationError, Rotator
from .store import Secret, StoreError
from .transport import HTTP_TIMEOUT_SECONDS, HttpResponse, HttpTransport, TransportError

NEXT_SUFFIX = ".next"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/healthz"
LOCAL_GATEWAY_PORT = 4000
SECONDS_PER_DAY = 86400
REVOKE_CHECKS = 6
REVOKE_WAIT_SECONDS = 2.0
_MDAT = re.compile(r'"mdat"<timedate>=(?:0x[0-9A-Fa-f]+\s+)?"(\d{14})')
_PLAIN = re.compile(r"[A-Za-z0-9._~+/=@:-]+")


@dataclass(frozen=True)
class Seams:
    """What the rotators take from the process: a token generator, the user name and a sleep. Tests replace these."""

    token: Callable[[], str] = lambda: secrets.token_urlsafe(36)
    user: Callable[[], str] = getpass.getuser
    sleep: Callable[[float], None] = time.sleep


SEAMS = Seams()


def _quote(value: str) -> str:
    """A word for ``security -i``: as is when plain, else double-quoted with backslash escapes."""
    if _PLAIN.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Recorder:
    """Wraps the HTTP transport to keep POST replies, so a credential made by a reply we cannot parse can be found."""

    def __init__(self, inner: HttpTransport) -> None:
        self.inner = inner
        self.replies: list[HttpResponse] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        response = self.inner.request(method, url, headers=headers, body=body, timeout=timeout)
        if method == "POST":
            self.replies.append(response)
        return response

    def partial(self, label: str) -> dict[str, str]:
        """The id of the last created credential in any reply, as ``{label: id}``; empty when none is visible."""
        for response in reversed(self.replies):
            if not response.ok:
                continue
            try:
                data = response.json()
            except TransportError:
                continue
            nested = data.get("api_key") if isinstance(data, dict) else None
            for candidate in (nested, data):
                if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                    return {label: candidate["id"]}
        return {}


@contextmanager
def _recording(ctx: Context) -> Iterator[_Recorder]:
    original = ctx.http
    recorder = _Recorder(original)
    ctx.http = recorder
    try:
        yield recorder
    finally:
        ctx.http = original


def _days(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else (now - then).total_seconds() / SECONDS_PER_DAY


def _age_line(name: str, age: float | None, limit: int | None) -> str:
    if age is None:
        return f"{name}: age unknown"
    if limit is None:
        return f"{name}: {age:.0f} days old"
    if age > limit:
        return f"{name}: {age:.0f} days old, past the {limit}-day limit"
    return f"{name}: {age:.0f} days old, within the {limit}-day limit"


def _write_private(path: Path, text: str) -> None:
    """Atomically write a 0600 file next to ``path``."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".rotate-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


class GatewayRotator:
    """Add the new token as a temporary named client, check it locally and over the tailnet, then swap it in."""

    kind = "gateway"

    def __init__(self, ctx: Context, spec: SecretSpec) -> None:
        gateway_config = ctx.config.gateway
        if gateway_config is None:
            raise RotationError("infra.gateway is not configured")
        self.ctx = ctx
        self.name = spec.name
        self.names = (spec.name,)
        self._config = gateway_config
        self._temp = f"{gateway_config.client}-next"
        tailnet = ctx.config.tailnet
        self._port = tailnet.gateway_port if tailnet else LOCAL_GATEWAY_PORT
        self._url: str | None = None

    def old_ids(self) -> dict[str, str]:
        return {"client": self._config.client}

    def describe(self) -> Sequence[str]:
        return (
            f"add the new token as the temporary client {self._temp} and run the host script's gateway step",
            f"check {MODELS_PATH} on 127.0.0.1:{self._port} and over the tailnet URL with the new token",
            f"set {self.name} in the recorded environments, then swap the token into {self._config.client}",
        )

    def _local(self, token: Secret, path: str) -> int:
        result = self.ctx.run(
            [
                "curl",
                "-s",
                "--max-time",
                "10",
                "-H",
                "@-",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                f"http://127.0.0.1:{self._port}{path}",
            ],
            stdin=f"Authorization: Bearer {token.reveal()}\n",
        )
        text = result.stdout.strip()
        return int(text) if result.returncode == 0 and text.isdigit() else 0

    def _tailnet(self, token: Secret, path: str) -> int:
        if self._url is None:
            stored = self.ctx.secret(self._url_name())
            if stored is None:
                raise RotationError("no tailnet URL to check: the host script reported none and none is stored")
            self._url = stored.reveal()
        response = self.ctx.http.request(
            "GET", self._url.rstrip("/") + path, headers={"Authorization": f"Bearer {token.reveal()}"}
        )
        return response.status

    def _url_name(self) -> str:
        for spec in self.ctx.config.secrets:
            if spec.source == "gateway_url":
                return spec.name
        raise RotationError("the gateway URL is not configured")

    def _check(self, token: Secret, *, tailnet: bool) -> None:
        for path in (HEALTH_PATH, MODELS_PATH):
            if self._local(token, path) != 200:
                raise RotationError(f"the gateway did not accept the new token locally on {path}")
        if tailnet and self._tailnet(token, MODELS_PATH) != 200:
            raise RotationError("the gateway did not accept the new token over the tailnet")

    def create(self) -> Created | None:
        token = Secret(SEAMS.token())
        self.ctx.watch(token.reveal())
        temp_file = gateway.token_path(self._config, self._temp)
        try:
            _write_private(temp_file, token.reveal())
            self._url = gateway.published_url(gateway.run_host_step(self.ctx, "gateway"))
        except Exception:
            self._discard_temp()
            raise
        return Created({self.name: token}, {"client": self._temp})

    def _discard_temp(self) -> None:
        gateway.token_path(self._config, self._temp).unlink(missing_ok=True)
        try:
            gateway.run_host_step(self.ctx, "revoke", self._temp)
        except gateway.GatewayError:
            pass

    def health(self, created: Created) -> None:
        self._check(created.values[self.name], tailnet=True)

    def promote(self, created: Created) -> None:
        token = created.values[self.name]
        _write_private(gateway.token_path(self._config, self._config.client), token.reveal())
        gateway.token_path(self._config, self._temp).unlink(missing_ok=True)
        gateway.run_host_step(self.ctx, "gateway")
        self._check(token, tailnet=False)

    def delete_new(self, ids: Mapping[str, str]) -> None:
        """Drop the temporary client and put the old token back as the review client's, then regenerate clients."""
        gateway.token_path(self._config, self._temp).unlink(missing_ok=True)
        current = gateway.token_path(self._config, self._config.client)
        old = self.ctx.secret(self.name)
        if old is not None:
            try:
                differs = current.read_text(encoding="utf-8").strip() != old.reveal()
            except OSError:
                differs = True
            if differs:
                _write_private(current, old.reveal())
        gateway.run_host_step(self.ctx, "gateway")

    def committed(self, created: Created) -> None:
        """Nothing to record locally."""

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        """The swap stopped the gateway accepting the old token; check the gateway really refuses it now."""
        old = self.ctx.store.get(self.name + PREVIOUS_SUFFIX)
        if old is None:
            return
        for attempt in range(REVOKE_CHECKS):
            if self._local(old, MODELS_PATH) in (401, 403):
                return
            if attempt + 1 < REVOKE_CHECKS:
                SEAMS.sleep(REVOKE_WAIT_SECONDS)
        raise RotationError("the gateway still accepts the old token after the swap")

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        return ()


class TailscaleRotator:
    """Create a tagged OAuth client, prove it by a token exchange, then delete the old one."""

    kind = "tailscale"
    revocable = True
    _partial: dict[str, str] = {}

    def __init__(self, ctx: Context, specs: Sequence[SecretSpec]) -> None:
        self.ctx = ctx
        self.by_field = {spec.field: spec.name for spec in specs}
        self.names = tuple(spec.name for spec in specs)
        self._new: tuple[str, Secret] = ("", Secret(""))

    def old_ids(self) -> dict[str, str]:
        stored_id = self.by_field.get("id")
        value = self.ctx.secret(stored_id) if stored_id else None
        client = value.reveal() if value else self.ctx.state.provider("tailscale").get("client_id", "")
        return {"client_id": client} if client else {}

    def describe(self) -> Sequence[str]:
        tailnet = self.ctx.config.tailnet
        assert tailnet is not None
        return (
            f"create an OAuth client {tailnet.oauth_client.description!r} for {tailnet.tag} with scopes "
            f"{', '.join(tailnet.oauth_client.scopes)}",
            f"check it by a client-credentials token exchange, set {', '.join(self.names)}, then delete the old client",
        )

    def create(self) -> Created | None:
        tailnet = self.ctx.config.tailnet
        assert tailnet is not None
        client = tailscale.TailscaleClient.from_root(self.ctx)
        with _recording(self.ctx) as recorder:
            try:
                client_id, client_secret = client.create_oauth_client(
                    tailnet.oauth_client.description, tailnet.oauth_client.scopes, (tailnet.tag,)
                )
            except Exception:
                self._partial = recorder.partial("client_id")
                raise
        values: dict[str, Secret] = {}
        if "id" in self.by_field:
            values[self.by_field["id"]] = Secret(client_id)
        if "secret" in self.by_field:
            values[self.by_field["secret"]] = client_secret
        self._new = (client_id, client_secret)
        return Created(values, {"client_id": client_id})

    def partial_ids(self) -> dict[str, str]:
        return self._partial

    def health(self, created: Created) -> None:
        client_id, client_secret = self._new
        if not tailscale.check_client(self.ctx.http, client_id, client_secret):
            raise RotationError("Tailscale refused a token exchange for the new OAuth client")

    def promote(self, created: Created) -> None:
        """The new client is live as soon as it exists; nothing provider-side is left."""

    def committed(self, created: Created) -> None:
        self.ctx.state.set_provider("tailscale", client_id=created.ids["client_id"])

    def _root(self) -> tailscale.TailscaleClient:
        return tailscale.TailscaleClient.from_root(self.ctx)

    def delete_new(self, ids: Mapping[str, str]) -> None:
        if ids.get("client_id"):
            self._root().delete_key(ids["client_id"])

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        if ids.get("client_id"):
            self._root().delete_key(ids["client_id"])

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        return ()


class OpenAIRotator:
    """Mint a second key on the service account, check it with GET /v1/models, then delete the old key."""

    kind = "openai"
    revocable = True
    _partial: dict[str, str] = {}

    def __init__(self, ctx: Context, spec: SecretSpec) -> None:
        assert spec.project is not None and spec.account_name is not None
        self.ctx = ctx
        self.spec = spec
        self.names = (spec.name,)
        self._project: str = spec.project

    def old_ids(self) -> dict[str, str]:
        record = self.ctx.state.provider("openai")
        return {key: record[key] for key in ("service_account_id", "key_id") if record.get(key)}

    def describe(self) -> Sequence[str]:
        return (
            f"mint a new key on the {self.spec.account_name!r} service account in {self._project}",
            f"check it with GET /v1/models, set {self.spec.name}, then delete the old key",
        )

    def create(self) -> Created | None:
        account = self.ctx.state.provider("openai").get("service_account_id")
        if not account:
            raise RotationError("no OpenAI service account is recorded; run 'theseus infra setup' first")
        assert self.spec.account_name is not None
        with _recording(self.ctx) as recorder:
            try:
                new = openai_admin.OpenAIAdmin(self.ctx).mint_key(self._project, account, self.spec.account_name)
            except Exception:
                self._partial = recorder.partial("key_id")
                raise
        return Created({self.spec.name: new.secret}, {"key_id": new.key_id, "service_account_id": account})

    def partial_ids(self) -> dict[str, str]:
        return self._partial

    def health(self, created: Created) -> None:
        if not openai_admin.check_key(self.ctx.http, created.values[self.spec.name]):
            raise RotationError("OpenAI refused the new key on GET /v1/models")

    def promote(self, created: Created) -> None:
        """A minted key is live at once."""

    def committed(self, created: Created) -> None:
        self.ctx.state.set_provider(
            "openai", service_account_id=created.ids["service_account_id"], key_id=created.ids["key_id"]
        )

    def delete_new(self, ids: Mapping[str, str]) -> None:
        if ids.get("key_id"):
            openai_admin.OpenAIAdmin(self.ctx).delete_key(self._project, ids["key_id"])

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        if ids.get("key_id"):
            openai_admin.OpenAIAdmin(self.ctx).delete_key(self._project, ids["key_id"])

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        return ()


class TypesafeRotator:
    """Pick up a key the owner made in the Typesafe console; Typesafe has no key-management API."""

    kind = "typesafe"

    def __init__(self, ctx: Context, spec: SecretSpec) -> None:
        self.ctx = ctx
        self.spec = spec
        self.names = (spec.name,)

    @property
    def _next_name(self) -> str:
        return self.spec.name + NEXT_SUFFIX

    def old_ids(self) -> dict[str, str]:
        return {}

    def _mirror(self) -> Secret | None:
        """The ``mirror_keychain`` item, only when it was written after the stored key (the owner put it there)."""
        mirror = self.spec.mirror_keychain
        if mirror is None:
            return None
        user = SEAMS.user()
        result = self.ctx.run(["security", "find-generic-password", "-s", mirror, "-a", user, "-w"])
        text = result.stdout.removesuffix("\n") if result.returncode == 0 else ""
        if not text.strip():
            return None
        attributes = self.ctx.run(["security", "find-generic-password", "-s", mirror, "-a", user])
        found = _MDAT.search(attributes.stdout) if attributes.returncode == 0 else None
        if found is None:
            return None
        written = datetime.strptime(found.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        stored = self.ctx.store.modified_at(self.spec.name)
        return None if stored is not None and written <= stored else Secret(text)

    def _candidate(self) -> Secret | None:
        value = self.ctx.secret(self._next_name)
        if value is None:
            value = self._mirror()
            if value is not None:
                self.ctx.watch(value.reveal())
        current = self.ctx.secret(self.spec.name)
        return None if value is None or value == current else value

    def _age(self) -> float | None:
        try:
            return _days(self.ctx.now(), self.ctx.store.modified_at(self.spec.name))
        except StoreError:
            return None

    def _steps(self) -> tuple[str, ...]:
        return (
            f"create a new key in the Typesafe console, then run: theseus infra seed {self._next_name}",
            "run 'theseus infra rotate --only " + self.spec.name + " --apply' to adopt it",
        )

    def describe(self) -> Sequence[str]:
        lines = [_age_line(self.spec.name, self._age(), self.spec.max_age_days)]
        if self._candidate() is None:
            lines.extend(self._steps())
        else:
            lines.append(
                f"adopt the newer key found in {self._next_name}: check it with GET /v1/models and set {self.spec.name}"
            )
        return lines

    def create(self) -> Created | None:
        candidate = self._candidate()
        return None if candidate is None else Created({self.spec.name: candidate})

    def health(self, created: Created) -> None:
        if not typesafe.check_key(self.ctx.http, created.values[self.spec.name]):
            raise RotationError("Typesafe refused the new key on GET /v1/models")

    def promote(self, created: Created) -> None:
        """Nothing provider-side."""

    def committed(self, created: Created) -> None:
        self.ctx.store.delete(self._next_name)

    def delete_new(self, ids: Mapping[str, str]) -> None:
        """The owner's key stays in the console and in ``NAME.next``; only the pending item is dropped."""

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        """Typesafe has no API to revoke the old key. Bring the local-review Keychain copy up to date instead."""
        mirror = self.spec.mirror_keychain
        current = self.ctx.store.get(self.spec.name)
        if mirror is None or current is None:
            return
        user = SEAMS.user()
        line = " ".join(
            ["add-generic-password", "-U", "-s", _quote(mirror), "-a", _quote(user), "-w", _quote(current.reveal())]
        )
        self.ctx.run(["security", "-i"], stdin=line + "\n")
        check = self.ctx.run(["security", "find-generic-password", "-s", mirror, "-a", user, "-w"])
        if check.returncode != 0 or check.stdout.removesuffix("\n") != current.reveal():
            raise RotationError(f"the Keychain item {mirror} was not updated with the new key")

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        if created is None:
            return (_age_line(self.spec.name, self._age(), self.spec.max_age_days), *self._steps())
        return ("revoke the previous Typesafe key in the Typesafe console; its API cannot do it",)


class AppKeyRotator:
    """Pick up a private key the owner generated in the App's settings; GitHub has no API to create or delete one."""

    kind = "app_key"

    def __init__(self, ctx: Context, spec: SecretSpec) -> None:
        self.ctx = ctx
        self.spec = spec
        self.names = (spec.name,)
        self._adopted: Path | None = None

    def _app_id(self) -> str:
        assert self.spec.app_id_secret is not None
        value = self.ctx.config.secret(self.spec.app_id_secret).value
        assert value is not None
        return value

    def _updated_at(self) -> datetime | None:
        try:
            return github_env.list_secrets(self.ctx).get(self.spec.name)
        except github_env.GithubError:
            return None

    def _age(self) -> float | None:
        return _days(self.ctx.now(), self._updated_at())

    def old_ids(self) -> dict[str, str]:
        return {}

    def _newer_pem(self) -> Path | None:
        pem = app_key.newest_pem(self.spec)
        if pem is None:
            return None
        updated = self._updated_at()
        if updated is not None and datetime.fromtimestamp(pem.stat().st_mtime, updated.tzinfo) <= updated:
            return None
        return pem

    def _steps(self) -> tuple[str, ...]:
        slug = self.spec.app_slug
        return (
            f"generate a private key in the settings of the {slug} GitHub App and save it in {self.spec.pem_dir}",
            f"run 'theseus infra rotate --only {self.spec.name} --apply' to adopt it",
        )

    def describe(self) -> Sequence[str]:
        lines = [_age_line(self.spec.name, self._age(), self.spec.max_age_days)]
        pem = self._newer_pem()
        if pem is None:
            if self._past_limit():
                lines.extend(self._steps())
        else:
            lines.append(f"validate {pem.name} against GitHub, set {self.spec.name}, store it")
        return lines

    def _past_limit(self) -> bool:
        age, limit = self._age(), self.spec.max_age_days
        return age is not None and limit is not None and age > limit

    def create(self) -> Created | None:
        pem = self._newer_pem()
        if pem is None:
            return None
        try:
            value = Secret(pem.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RotationError(f"cannot read {pem.name}: {exc.strerror}") from exc
        self.ctx.watch(value.reveal())
        assert self.spec.app_slug is not None
        try:
            app_key.validate(self.ctx, value, self._app_id(), self.spec.app_slug)
        except app_key.AppKeyError as exc:
            raise RotationError(f"{pem.name} did not validate: {exc}") from exc
        self._adopted = pem
        return Created({self.spec.name: value}, {"pem": str(pem)})

    def health(self, created: Created) -> None:
        """The key was validated before anything changed."""

    def promote(self, created: Created) -> None:
        """Nothing provider-side."""

    def delete_new(self, ids: Mapping[str, str]) -> None:
        """The owner's key stays in App settings."""

    def committed(self, created: Created) -> None:
        """Nothing to record locally."""

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        """GitHub has no API to delete a private key; the owner does it from the printed step."""

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        if created is not None:
            lines = [f"delete the old private key in the {self.spec.app_slug} GitHub App settings (Private keys)"]
            adopted = Path(created.ids["pem"]) if created.ids.get("pem") else self._adopted
            if adopted is not None:
                lines.append(f"delete the adopted key file {adopted} from {adopted.parent}")
            return tuple(lines)
        lines = [_age_line(self.spec.name, self._age(), self.spec.max_age_days)]
        if self._past_limit():
            lines.extend(self._steps())
        return tuple(lines)


def build_rotators(ctx: Context, config: InfraConfig) -> list[Rotator]:
    """One rotator per rotatable credential, in the order the secrets appear in the policy file."""
    found: list[Rotator] = []
    tailscale_specs = [spec for spec in config.secrets if spec.source == TAILSCALE_OAUTH]
    if tailscale_specs:
        found.append(TailscaleRotator(ctx, tailscale_specs))
    for spec in config.secrets:
        if spec.source == GATEWAY_TOKEN:
            found.append(GatewayRotator(ctx, spec))
        elif spec.source == OPENAI_SERVICE_ACCOUNT:
            found.append(OpenAIRotator(ctx, spec))
        elif spec.source == STORE:
            found.append(TypesafeRotator(ctx, spec))
        elif spec.source == APP_KEY:
            found.append(AppKeyRotator(ctx, spec))
    return found
