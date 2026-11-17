"""A fake provider world for rotation tests: a tailnet, an OpenAI project, a GitHub environment, a gateway host and
the stores, all behind the seams. Nothing here touches a network, a keychain or a real gh."""

from __future__ import annotations

import json
import os
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from theseus_reviewer.infra import rotators
from theseus_reviewer.infra.config import InfraConfig
from theseus_reviewer.infra.plan import Context
from theseus_reviewer.infra.state import State
from theseus_reviewer.infra.store import FileStore, Secret, StoreError
from theseus_reviewer.infra.transport import Redactor

from .example import example
from .fakes import Call, FakeHttp, FakeRunner, FakeSecurity, RunCall, completed, json_response

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
ENV_KEY = "Example-Org/example:review"
GATEWAY_URL = "https://gw.example.ts.net:4000"
APP_ID = "123456"
OLD = {
    "REVIEW_GATEWAY_TOKEN": "OLD-gateway-token-aaaaaaaa",
    "TS_OAUTH_CI_CLIENT_ID": "OLD-ts-client-id-bbbbbbbb",
    "TS_OAUTH_CI_SECRET": "OLD-ts-client-secret-cccccccc",
    "OPENAI_API_KEY": "OLD-openai-key-dddddddd",
    "TYPESAFE_API_KEY": "OLD-typesafe-key-eeeeeeee",
    "REVIEW_APP_PRIVATE_KEY": "-----BEGIN OLD PEM-----\nOLD-pem-body-ffffffff\n-----END OLD PEM-----\n",
}
ROOT = {
    "TAILSCALE_ROOT_CLIENT_ID": "ROOT-ts-id-gggggggg",
    "TAILSCALE_ROOT_CLIENT_SECRET": "ROOT-ts-secret-hhhhhhhh",
    "OPENAI_ADMIN_KEY": "ROOT-openai-admin-iiiiiiii",
}
NEW_PEM = "-----BEGIN NEW PEM-----\nNEW-pem-body-jjjjjjjj\n-----END NEW PEM-----\n"
NEW_TYPESAFE = "NEW-typesafe-key-kkkkkkkk"
NEW_GATEWAY = "NEW-gateway-token-llllllll"
TS = "https://api.tailscale.com/api/v2"
OAI = "https://api.openai.com/v1"
PROJECT = f"{OAI}/organization/projects/proj_abc"


class FlakyStore(FileStore):
    """A file store that refuses to write chosen names, to fail the store step of a rotation."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.fail_put: set[str] = set()
        self.fail_delete: set[str] = set()
        self.hook: Callable[[], None] = lambda: None

    def put(self, name: str, value: Secret) -> None:
        self.hook()
        if name in self.fail_put:
            raise StoreError(f"the store refused {name}")
        super().put(name, value)
        self.hook()

    def delete(self, name: str) -> None:
        self.hook()
        if name in self.fail_delete:
            raise StoreError(f"the store refused to delete {name}")
        super().delete(name)
        self.hook()


@dataclass
class World:
    tmp: Path
    config: InfraConfig
    store: FlakyStore
    state: State
    http: FakeHttp
    runner: FakeRunner
    ctx: Context
    env: dict[str, str] = field(default_factory=dict)  # secret name -> value in the GitHub environment
    secret_updated: dict[str, str] = field(default_factory=dict)
    ts_clients: dict[str, str] = field(default_factory=dict)  # client id -> secret
    openai_keys: dict[str, str] = field(default_factory=dict)  # key id -> value
    typesafe_keys: set[str] = field(default_factory=set)
    gateway_rejects: set[str] = field(default_factory=set)
    # failure switches
    fail_set: set[str] = field(default_factory=set)  # secret names whose next `gh secret set` fails once
    fail_delete: set[str] = field(default_factory=set)  # provider ids whose delete fails
    fail_gateway_step_call: int | None = None
    app_id_answer: str = APP_ID
    gateway_calls: int = 0
    reject_new: bool = False
    gateway_keeps: set[str] = field(default_factory=set)  # tokens the gateway keeps accepting
    sleeps: list[float] = field(default_factory=list)
    security: FakeSecurity = field(default_factory=FakeSecurity)
    environments: dict[str, dict] = field(
        default_factory=lambda: {
            "review": {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        }
    )
    environment_reads: int = 0
    open_after_reads: int | None = None  # after this many environment reads, the environment opens to every branch
    trip_at: int | None = None
    ticks: int = 0

    def arm(self, at: int | None) -> None:
        """Raise ``KeyboardInterrupt`` at the ``at``-th boundary (before and after each write) from now on."""
        self.trip_at, self.ticks = at, 0

    def tick(self) -> None:
        if self.trip_at is None:
            return
        self.ticks += 1
        if self.ticks == self.trip_at:
            self.trip_at = None
            raise KeyboardInterrupt

    def reload(self) -> None:
        """Read the state the command left on disk, as the next process would."""
        self.state = self.ctx.state

    counter: count = field(default_factory=lambda: count(1))
    minted: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=lambda: [OLD["TS_OAUTH_CI_CLIENT_ID"]])

    # -- provider facts the assertions read ---------------------------------------------------------------------

    def accepted_gateway_tokens(self) -> set[str]:
        directory = self.config.gateway.directory  # type: ignore[union-attr]
        tokens: set[str] = set()
        if directory.is_dir():
            for path in directory.iterdir():
                if path.name == "client.token" or path.name.endswith("-client.token"):
                    tokens.add(path.read_text(encoding="utf-8").strip())
        return tokens

    def all_text(self) -> str:
        """Everything a leak could hide in: the state file, the file store's files, calls' URLs and argv."""
        parts = [self.config.state_path.read_text(encoding="utf-8")] if self.config.state_path.exists() else []
        parts.extend(call.url for call in self.http.calls)
        parts.extend(" ".join(call.argv) for call in self.runner.calls)
        return "\n".join(parts)

    def every_value(self) -> list[str]:
        """Credential values. Provider ids (``self.identifiers``) may appear in URLs and the state file."""
        return [
            value
            for value in (*OLD.values(), *ROOT.values(), NEW_PEM, NEW_TYPESAFE, NEW_GATEWAY, *self.minted)
            if value not in self.identifiers
        ]

    def values_in_argv_or_urls(self, values: Sequence[str]) -> list[str]:
        haystack = "\n".join([*(call.url for call in self.http.calls), *(" ".join(c.argv) for c in self.runner.calls)])
        return [value for value in values if value in haystack]


def build_world(tmp_path: Path, monkeypatch) -> World:  # noqa: ANN001 - pytest fixture
    config = example(tmp_path)
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    store = FlakyStore(tmp_path / "store")
    for name, value in {**OLD, **ROOT}.items():
        store.put(name, Secret(value))
    store.put("REVIEW_GATEWAY_URL", Secret(GATEWAY_URL))
    state = State(config.state_path)
    for name in OLD:
        state.bump(name)
        state.mark_delivered(name, ENV_KEY)
    state.set_provider("openai", service_account_id="sa_1", key_id="key_old")
    state.set_provider("tailscale", client_id=OLD["TS_OAUTH_CI_CLIENT_ID"])
    state.save()
    gateway_dir = config.gateway.directory  # type: ignore[union-attr]
    gateway_dir.mkdir(mode=0o700, parents=True)
    (gateway_dir / "client.token").write_text(OLD["REVIEW_GATEWAY_TOKEN"], encoding="utf-8")
    pem_dir = tmp_path / "pems"
    pem_dir.mkdir()
    http, runner = FakeHttp(), FakeRunner()
    redactor = Redactor([*OLD.values(), *ROOT.values(), NEW_PEM, NEW_TYPESAFE, NEW_GATEWAY])
    ctx = Context(config, store, state, http, runner, redactor, lambda: NOW)
    world = World(tmp_path, config, store, state, http, runner, ctx)
    world.env = {name: value for name, value in OLD.items()}
    world.env["REVIEW_GATEWAY_URL"] = GATEWAY_URL
    world.secret_updated = {"REVIEW_APP_PRIVATE_KEY": "2026-01-01T00:00:00Z"}
    world.ts_clients = {
        OLD["TS_OAUTH_CI_CLIENT_ID"]: OLD["TS_OAUTH_CI_SECRET"],
        ROOT["TAILSCALE_ROOT_CLIENT_ID"]: ROOT["TAILSCALE_ROOT_CLIENT_SECRET"],
    }
    world.openai_keys = {"key_old": OLD["OPENAI_API_KEY"]}
    world.typesafe_keys = {OLD["TYPESAFE_API_KEY"], NEW_TYPESAFE}
    world.store.hook = world.tick
    original_save = State.save
    original_request = http.request

    def traced_save(self: State, forbidden=()) -> None:  # noqa: ANN001
        world.tick()
        original_save(self, forbidden)
        world.tick()

    def traced_request(method: str, url: str, **kwargs):  # noqa: ANN003, ANN202
        if method != "GET":
            world.tick()
        response = original_request(method, url, **kwargs)
        if method != "GET":
            world.tick()
        return response

    monkeypatch.setattr(State, "save", traced_save)
    monkeypatch.setattr(http, "request", traced_request)
    _routes(world, monkeypatch)
    return world


def _routes(world: World, monkeypatch) -> None:  # noqa: ANN001
    http, runner = world.http, world.runner

    def token(call: Call):
        fields = urllib.parse.parse_qs((call.body or b"").decode())
        client, secret = fields["client_id"][0], fields["client_secret"][0]
        if world.ts_clients.get(client) == secret and not (world.reject_new and client.startswith("NEW-")):
            return json_response(200, {"access_token": "SESSION-" + client})
        return json_response(401, {"message": "invalid client"})

    def create_client(call: Call):
        body = call.json()
        assert body["keyType"] == "client" and body["tags"] == ["tag:review-ci"]
        n = next(world.counter)
        client_id, secret = f"NEW-ts-client-id-{n:08d}", f"NEW-ts-client-secret-{n:08d}"
        world.ts_clients[client_id] = secret
        world.identifiers.append(client_id)
        world.minted.append(secret)
        return json_response(200, {"id": client_id, "key": secret})

    def delete_client(call: Call):
        client_id = call.url.rsplit("/", 1)[1]
        if client_id in world.fail_delete:
            return json_response(500, {"message": "boom"})
        world.ts_clients.pop(client_id, None)
        return json_response(200, {})

    http.on("POST", rf"{TS}/oauth/token", token)
    http.on("POST", rf"{TS}/tailnet/-/keys", create_client)
    http.on("DELETE", rf"{TS}/tailnet/-/keys/.+", delete_client)

    def mint(call: Call):
        n = next(world.counter)
        key_id, value = f"key_new_{n}", f"NEW-openai-key-{n:08d}"
        world.openai_keys[key_id] = value
        world.minted.append(value)
        return json_response(200, {"id": key_id, "value": value})

    def delete_openai(call: Call):
        key_id = call.url.rsplit("/", 1)[1]
        if key_id in world.fail_delete:
            return json_response(500, {"message": "boom"})
        world.openai_keys.pop(key_id, None)
        return json_response(200, {})

    def models(call: Call):
        bearer = call.headers.get("Authorization", "").removeprefix("Bearer ")
        ok = bearer in world.openai_keys.values() and not (world.reject_new and bearer.startswith("NEW-"))
        return json_response(200 if ok else 401, {})

    http.on("POST", rf"{PROJECT}/service_accounts/sa_1/api_keys", mint)
    http.on("DELETE", rf"{PROJECT}/api_keys/.+", delete_openai)
    http.on("GET", rf"{OAI}/models", models)

    def typesafe_models(call: Call):
        bearer = call.headers.get("Authorization", "").removeprefix("Bearer ")
        return json_response(200 if bearer in world.typesafe_keys else 401, {})

    http.on("GET", r"https://api\.typesafe\.ai/v1/models", typesafe_models)
    http.on(
        "GET",
        r"https://api\.github\.com/app",
        lambda call: json_response(200, {"id": int(world.app_id_answer), "slug": "example-app"}),
    )

    def gateway_models(call: Call):
        bearer = call.headers.get("Authorization", "").removeprefix("Bearer ")
        ok = bearer in world.accepted_gateway_tokens() | world.gateway_keeps and bearer not in world.gateway_rejects
        return json_response(200 if ok else 401, {})

    http.on("GET", rf"{GATEWAY_URL}/v1/models", gateway_models)

    def gh_set(call: RunCall):
        name = call.argv[3]
        assert call.argv[4:] == ("--env", "review", "--repo", "Example-Org/example"), call.argv
        world.tick()
        if name in world.fail_set and call.stdin not in OLD.values():
            world.fail_set.discard(name)
            return completed(returncode=1, stderr="gh: secret set failed")
        assert call.stdin is not None
        world.env[name] = call.stdin
        world.tick()
        return completed()

    runner.on(lambda argv: tuple(argv[:3]) == ("gh", "secret", "set"), gh_set)

    def gh_api(call: RunCall):
        target = call.argv[-1]
        if "/environments/" in target and "/secrets" not in target:
            world.environment_reads += 1
            if world.open_after_reads is not None and world.environment_reads > world.open_after_reads:
                world.environments["review"] = {"deployment_branch_policy": None}
            found = world.environments.get(target.rsplit("/", 1)[1])
            if found is None:
                return completed(returncode=1, stderr="gh: Not Found (HTTP 404)")
            return completed(json.dumps(found))
        assert "/secrets?per_page" in call.argv[-1], call.argv
        body = {"secrets": [{"name": n, "updated_at": t} for n, t in world.secret_updated.items()]}
        return completed(json.dumps(body))

    runner.on(lambda argv: argv[:2] == ("gh", "api") or list(argv[:2]) == ["gh", "api"], gh_api)

    def host(call: RunCall):
        step = call.argv[1]
        if step == "gateway":
            world.gateway_calls += 1
            if world.fail_gateway_step_call == world.gateway_calls:
                return completed(returncode=1, stderr="error: gateway step failed")
            return completed(f"gateway: granted a\ngateway: published at {GATEWAY_URL}\n")
        if step == "revoke":
            name = call.argv[2].removeprefix("review-") + "-client.token"
            (world.config.gateway.directory / name).unlink(missing_ok=True)  # type: ignore[union-attr]
            return completed("revoked\n")
        raise AssertionError(step)

    runner.on(lambda argv: str(argv[0]).endswith("scripts/gateway.sh"), host)

    def curl(call: RunCall):
        assert call.stdin is not None and call.stdin.startswith("Authorization: Bearer ")
        bearer = call.stdin.removeprefix("Authorization: Bearer ").strip()
        ok = bearer in world.accepted_gateway_tokens() | world.gateway_keeps and bearer not in world.gateway_rejects
        return completed("200" if ok else "401")

    runner.on(lambda argv: argv[0] == "curl", curl)

    def openssl(call: RunCall):
        out = call.argv[list(call.argv).index("-out") + 1]
        Path(out).write_bytes(b"signature")
        return completed()

    runner.on(lambda argv: argv[0] == "openssl", openssl)
    runner.on(lambda argv: argv[0] == "security", lambda call: world.security(call.argv, stdin=call.stdin))
    monkeypatch.setattr(
        rotators, "SEAMS", rotators.Seams(token=lambda: NEW_GATEWAY, user=lambda: "tester", sleep=world.sleeps.append)
    )


def add_new_pem(world: World) -> Path:
    pem = world.tmp / "pems" / "example-app.2026-11-10.private-key.pem"
    pem.write_text(NEW_PEM, encoding="utf-8")
    stamp = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    os.utime(pem, (stamp, stamp))
    return pem


def write_policy_file(world: World) -> Path:
    """The example policy with its paths under the world's tmp dir, as a file the command can load."""
    from copy import deepcopy

    from .example import EXAMPLE, write_policy

    data = deepcopy(EXAMPLE)
    data["store"]["dir"] = str(world.tmp / "store")
    data["state"] = str(world.config.state_path)
    data["gateway"]["dir"] = str(world.tmp / "gateway")
    data["secrets"]["REVIEW_APP_PRIVATE_KEY"]["pem_dir"] = str(world.tmp / "pems")
    return write_policy(world.tmp / ".github" / "review.yml", data)
