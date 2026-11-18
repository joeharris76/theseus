"""A stateful fake of everything setup talks to: gh, Tailscale, OpenAI, Typesafe, the App API and the host script.

Nothing here opens a socket or runs a real tool. Values are sentinels so tests can search every output for them.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from theseus_reviewer.infra.config import InfraConfig
from theseus_reviewer.infra.plan import Context, open_context
from theseus_reviewer.infra.store import FileStore, ScopedStore, Secret
from theseus_reviewer.infra.tailscale import standardize

from .example import EXAMPLE, write_policy
from .fakes import SENTINELS, Call, FakeHttp, FakeRunner, RunCall, completed, json_response

POLICY = """// Example tailnet policy
{
  "tagOwners": {
    "tag:other": ["autogroup:admin"], // keep this comment
  },
  "grants": [
    {"src": ["*"], "dst": ["*"], "ip": ["*"]},
  ],
  // tests below
  "tests": [],
}
"""
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
TOKEN = "SENTINEL-ts-access-token-0009"
PUBLISHED = "https://gateway-host.example.ts.net:4000"
FAKE_JWT_SIGNATURE = b"fake-signature-bytes"


@dataclass
class World:
    tmp: Path
    infra: dict[str, Any]
    http: FakeHttp = field(default_factory=FakeHttp)
    run: FakeRunner = field(default_factory=FakeRunner)
    environment: dict[str, Any] | None = None
    policies: list[str] = field(default_factory=list)
    secrets: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    policy_text: str = POLICY
    etag: int = 1
    reject_policy: str = ""
    clients: dict[str, str] = field(default_factory=dict)
    openai_keys: dict[str, str] = field(default_factory=dict)
    openai_accounts: int = 0
    app_slug: str = "example-app"
    app_id: int = 123456
    typesafe_ok: bool = True
    gh_fail_secret: str = ""
    host_fail: bool = False
    jwt_signed_with: list[str] = field(default_factory=list)
    open_env_after_gets: int | None = None
    env_gets: int = 0

    def __post_init__(self) -> None:
        self._routes_gh()
        self._routes_http()

    # -- seeding ------------------------------------------------------------------------------------------------

    @property
    def policy_path(self) -> Path:
        return self.tmp / "repo" / ".github" / "review.yml"

    def context(self) -> Context:
        write_policy(self.policy_path, self.infra)
        return open_context(self.policy_path, self.http, self.run, lambda: NOW)

    def seed_roots(self, ctx: Context) -> None:
        ctx.store.put("TAILSCALE_ROOT_CLIENT_ID", Secret(SENTINELS["root_id"]))
        ctx.store.put("TAILSCALE_ROOT_CLIENT_SECRET", Secret(SENTINELS["root_secret"]))
        ctx.store.put("OPENAI_ADMIN_KEY", Secret(SENTINELS["admin"]))
        ctx.store.put("TYPESAFE_API_KEY", Secret(SENTINELS["typesafe"]))

    def put_pem(self, config: InfraConfig, text: str | None = None) -> Path:
        directory = config.secret("REVIEW_APP_PRIVATE_KEY").pem_dir
        assert directory is not None
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "example-app.2026-11-14.private-key.pem"
        path.write_text(
            text
            or f"-----BEGIN RSA PRIVATE KEY-----\n{SENTINELS['pem_marker']}\nsecondlineofkeybody\n-----END RSA PRIVATE KEY-----\n"
        )
        return path

    def script(self, config: InfraConfig) -> Path:
        path = config.repo_root / "scripts" / "gateway.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    # -- gh -----------------------------------------------------------------------------------------------------

    def _routes_gh(self) -> None:
        self.run.on(lambda argv: argv[:2] == ["gh", "api"], self._gh_api)
        self.run.on(lambda argv: argv[:3] == ["gh", "secret", "set"], self._gh_secret)
        self.run.on(lambda argv: argv[0] == "openssl", self._openssl)
        self.run.on(lambda argv: len(argv) == 2 and argv[1] == "gateway", self._host_gateway)

    def _gh_api(self, call: RunCall) -> Any:
        argv = list(call.argv)
        method, path = argv[3], argv[-3] if "--input" in argv else argv[-1]
        body = json.loads(call.stdin) if call.stdin else None
        base = re.match(r"repos/Example-Org/example/environments/review(.*)", path)
        assert base, path
        rest = base.group(1)
        missing = completed(returncode=1, stderr="gh: Not Found (HTTP 404)")
        if rest == "":
            if method == "GET":
                self.env_gets += 1
                if (
                    self.open_env_after_gets is not None
                    and self.env_gets > self.open_env_after_gets
                    and self.environment
                ):
                    self.environment["deployment_branch_policy"] = None
                return missing if self.environment is None else completed(json.dumps(self.environment))
            self.environment = {"name": "review", **body}
            return completed(json.dumps(self.environment))
        if self.environment is None:
            return missing
        if rest.startswith("/deployment-branch-policies"):
            if method == "POST":
                self.policies.append(body["name"])
                return completed("{}")
            items = [{"id": i, "name": name, "type": "branch"} for i, name in enumerate(self.policies)]
            return completed(json.dumps({"total_count": len(items), "branch_policies": items}))
        if rest.startswith("/secrets"):
            items = [
                {"name": name, "updated_at": at.isoformat().replace("+00:00", "Z")}
                for name, (_, at) in self.secrets.items()
            ]
            return completed(json.dumps({"total_count": len(items), "secrets": items}))
        raise AssertionError(path)

    def _gh_secret(self, call: RunCall) -> Any:
        name = call.argv[3]
        if name == self.gh_fail_secret:
            return completed(returncode=1, stderr="gh: boom")
        assert call.argv[4:] == ("--env", "review", "--repo", "Example-Org/example")
        assert call.stdin is not None
        self.secrets[name] = (call.stdin, NOW)
        return completed()

    def _openssl(self, call: RunCall) -> Any:
        argv = list(call.argv)
        assert call.stdin is not None and "/dev/stdin" in argv
        self.jwt_signed_with.append(call.stdin)
        Path(argv[argv.index("-out") + 1]).write_bytes(FAKE_JWT_SIGNATURE)
        return completed()

    def _host_gateway(self, call: RunCall) -> Any:
        if self.host_fail:
            return completed(returncode=1, stderr="error: the broker is not running")
        gateway_dir = Path(self.infra["gateway"]["dir"])
        gateway_dir.mkdir(parents=True, exist_ok=True)
        (gateway_dir / "client.token").write_text(SENTINELS["gateway"])
        return completed(f"gateway: granted review-ci\ngateway: published at {PUBLISHED}\n")

    # -- http ---------------------------------------------------------------------------------------------------

    def _routes_http(self) -> None:
        ts = r"https://api\.tailscale\.com/api/v2"
        self.http.on("POST", ts + "/oauth/token", self._oauth_token)
        self.http.on("GET", ts + r"/tailnet/-/acl", self._acl_get)
        self.http.on("POST", ts + r"/tailnet/-/acl/validate", self._acl_validate)
        self.http.on("POST", ts + r"/tailnet/-/acl", self._acl_post)
        self.http.on("POST", ts + r"/tailnet/-/keys", self._key_create)
        self.http.on("GET", ts + r"/tailnet/-/keys/[^/]+", self._key_get)
        self.http.on("DELETE", ts + r"/tailnet/-/keys/[^/]+", self._key_delete)
        oa = r"https://api\.openai\.com/v1"
        self.http.on("POST", oa + r"/organization/projects/proj_abc/service_accounts", self._sa_create)
        self.http.on("POST", oa + r"/organization/projects/proj_abc/service_accounts/[^/]+/api_keys", self._sa_mint)
        self.http.on("DELETE", oa + r"/organization/projects/proj_abc/api_keys/[^/]+", self._sa_delete)
        self.http.on("GET", oa + "/models", self._openai_models)
        self.http.on("GET", r"https://api\.typesafe\.ai/v1/models", self._typesafe_models)
        self.http.on("GET", r"https://api\.github\.com/app", self._app)

    def _oauth_token(self, call: Call) -> Any:
        form = urllib.parse.parse_qs((call.body or b"").decode())
        pair = (form["client_id"][0], form["client_secret"][0])
        known = {(SENTINELS["root_id"], SENTINELS["root_secret"])} | {
            (cid, secret) for cid, secret in self.clients.items()
        }
        if pair in known:
            return json_response(200, {"access_token": TOKEN, "token_type": "Bearer"})
        return json_response(401, {"error": "invalid_client"})

    def _authed(self, call: Call) -> bool:
        return call.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _acl_get(self, call: Call) -> Any:
        assert self._authed(call) and call.headers["Accept"] == "application/hujson"
        return _text_response(self.policy_text, f'"etag-{self.etag}"')

    def _acl_validate(self, call: Call) -> Any:
        assert self._authed(call)
        standardize((call.body or b"").decode())
        if self.reject_policy:
            return json_response(200, {"message": self.reject_policy, "data": [{"user": "x"}]})
        return json_response(200, {})

    def _acl_post(self, call: Call) -> Any:
        assert self._authed(call)
        if call.headers.get("If-Match") != f'"etag-{self.etag}"':
            return json_response(412, {"message": "precondition failed"})
        self.policy_text = (call.body or b"").decode()
        self.etag += 1
        return json_response(200, {})

    def _key_create(self, call: Call) -> Any:
        assert self._authed(call)
        body = call.json()
        assert body["keyType"] == "client" and body["tags"] and body["scopes"]
        cid = f"kclient{len(self.clients) + 1}"
        secret = SENTINELS["ts_secret"] + f"-{len(self.clients) + 1}"
        self.clients[cid] = secret
        return json_response(200, {"id": cid, "key": secret, "keyType": "client"})

    def _key_get(self, call: Call) -> Any:
        cid = call.url.rsplit("/", 1)[1]
        return json_response(200, {"id": cid}) if cid in self.clients else json_response(404, {"message": "not found"})

    def _key_delete(self, call: Call) -> Any:
        self.clients.pop(call.url.rsplit("/", 1)[1], None)
        return json_response(200, {})

    def _sa_create(self, call: Call) -> Any:
        assert call.headers["Authorization"] == f"Bearer {SENTINELS['admin']}"
        self.openai_accounts += 1
        key_id = f"key_{len(self.openai_keys) + 1}"
        self.openai_keys[key_id] = SENTINELS["openai"] + f"-{len(self.openai_keys) + 1}"
        return json_response(
            200,
            {
                "id": f"svc_{self.openai_accounts}",
                "name": call.json()["name"],
                "api_key": {"value": self.openai_keys[key_id], "id": key_id},
            },
        )

    def _sa_mint(self, call: Call) -> Any:
        key_id = f"key_{len(self.openai_keys) + 1}"
        self.openai_keys[key_id] = SENTINELS["openai"] + f"-{len(self.openai_keys) + 1}"
        return json_response(200, {"id": key_id, "value": self.openai_keys[key_id]})

    def _sa_delete(self, call: Call) -> Any:
        self.openai_keys.pop(call.url.rsplit("/", 1)[1], None)
        return json_response(200, {"deleted": True})

    def _openai_models(self, call: Call) -> Any:
        token = call.headers["Authorization"].removeprefix("Bearer ")
        return json_response(200 if token in self.openai_keys.values() else 401, {})

    def _typesafe_models(self, call: Call) -> Any:
        return json_response(200 if self.typesafe_ok else 401, {})

    def _app(self, call: Call) -> Any:
        assert call.headers["Authorization"].startswith("Bearer ") and call.headers["Authorization"].count(".") == 2
        return json_response(200, {"id": self.app_id, "slug": self.app_slug})


def _text_response(text: str, etag: str) -> Any:
    from theseus_reviewer.infra.transport import HttpResponse

    return HttpResponse(200, {"etag": etag}, text.encode())


def scoped_store(world: World) -> ScopedStore:
    """The example repository's view of the world's file store."""
    return ScopedStore(FileStore(Path(world.infra["store"]["dir"])), "Example-Org/example", "review")


def seed_store_directly(config: InfraConfig, **values: str) -> None:
    assert config.store.directory is not None
    store = ScopedStore(FileStore(config.store.directory), config.github.repository, config.github.environment)
    for name, value in values.items():
        store.put(name, Secret(value))


def make_world(tmp_path: Path) -> World:
    infra = deepcopy(EXAMPLE)
    infra["store"]["dir"] = str(tmp_path / "store")
    infra["state"] = str(tmp_path / "state" / "state.json")
    infra["gateway"]["dir"] = str(tmp_path / "gateway")
    infra["secrets"]["REVIEW_APP_PRIVATE_KEY"]["pem_dir"] = str(tmp_path / "pems")
    return World(tmp_path, infra)
