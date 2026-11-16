from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from theseus_reviewer.infra import app_key, gateway, openai_admin, tailscale
from theseus_reviewer.infra.store import Secret

from .example import example
from .fakes import SENTINELS, FakeHttp, json_response
from .world import NOW, POLICY, make_world

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def tailnet(tmp_path: Path):
    config = example(tmp_path)
    assert config.tailnet is not None
    return config.tailnet


def test_editing_an_empty_policy_adds_every_section(tmp_path: Path) -> None:
    edited = tailscale.edit_policy("{}", tailnet(tmp_path))
    policy = tailscale.standardize(edited)
    assert tailscale.missing(policy, tailnet(tmp_path)) == []
    assert list(policy) == ["tagOwners", "grants", "tests"]


@pytest.mark.parametrize(
    "text",
    [
        POLICY,
        '{"tagOwners": {}, "grants": [], "tests": []}',
        '{"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}]}',
        '{\n  "acls": [], /* block */\n  "grants": [ // c\n  ],\n}\n',
        '{"tests": [{"src": "a", "accept": ["b:1"]}], "tagOwners": {"tag:x": ["a"]}}',
    ],
)
def test_a_policy_edit_is_idempotent_and_keeps_every_existing_byte(tmp_path: Path, text: str) -> None:
    once = tailscale.edit_policy(text, tailnet(tmp_path))
    assert tailscale.edit_policy(once, tailnet(tmp_path)) == once
    before = tailscale.standardize(text)
    after = tailscale.standardize(once)
    for key, value in before.items():
        if isinstance(value, list):
            assert after[key][: len(value)] == value
        else:
            assert all(after[key][k] == v for k, v in value.items())
    assert tailscale.missing(after, tailnet(tmp_path)) == []


def test_comments_and_strings_that_look_like_structure_are_not_parsed_as_structure(tmp_path: Path) -> None:
    text = '{\n  // "grants": [ not here\n  "note": "has } and ] and // inside",\n  "grants": []\n}'
    edited = tailscale.edit_policy(text, tailnet(tmp_path))
    assert '"note": "has } and ] and // inside"' in edited and '// "grants": [ not here' in edited
    assert tailscale.standardize(edited)["grants"][0]["src"] == ["tag:review-ci"]


@pytest.mark.parametrize("text", ["[]", '{"grants": {}}', '{"tagOwners": []}', '{"x": /* open', "{"])
def test_a_policy_it_cannot_edit_safely_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(tailscale.TailscaleError):
        tailscale.edit_policy(text, tailnet(tmp_path))


def test_an_existing_matching_grant_and_test_need_no_edit(tmp_path: Path) -> None:
    config = tailnet(tmp_path)
    policy = {
        "tagOwners": {"tag:review-ci": ["autogroup:admin"]},
        "grants": [{"src": ["tag:review-ci", "x"], "dst": ["tag:review-gateway", "y"], "ip": ["tcp:4000", "tcp:1"]}],
        "tests": [
            {"src": "tag:review-ci", "accept": ["tag:review-gateway:4000"], "deny": ["tag:review-gateway:22", "z:1"]}
        ],
    }
    assert tailscale.missing(policy, config) == []
    policy["grants"][0]["ip"] = ["tcp:1"]
    assert tailscale.missing(policy, config) == ["grants"]


def test_the_token_exchange_sends_the_client_in_the_body_only() -> None:
    http = FakeHttp()
    http.on("POST", r".*/oauth/token", json_response(200, {"access_token": "tok-123456789"}))
    token = tailscale.exchange_token(http, "cid", Secret(SENTINELS["root_secret"]))
    assert token is not None and token.reveal() == "tok-123456789"
    call = http.calls[0]
    assert SENTINELS["root_secret"] not in call.url and SENTINELS["root_secret"] not in str(call.headers)
    assert SENTINELS["root_secret"].encode() in (call.body or b"")
    http = FakeHttp()
    http.on("POST", r".*/oauth/token", json_response(401, {}))
    assert tailscale.check_client(http, "cid", Secret("secret-value")) is False


def test_the_host_script_must_stay_inside_the_repository(tmp_path: Path) -> None:
    config = example(tmp_path)
    assert gateway.script_path(config) == (config.repo_root / "scripts" / "gateway.sh").resolve()
    escaped = example(tmp_path, gateway={"host_script": "../outside.sh", "client": "review-ci", "dir": str(tmp_path)})
    with pytest.raises(gateway.GatewayError, match="inside"):
        gateway.script_path(escaped)


def test_token_files_follow_the_host_script_convention(tmp_path: Path) -> None:
    config = example(tmp_path)
    assert config.gateway is not None
    assert gateway.token_path(config.gateway, "review-ci") == config.gateway.directory / "client.token"
    assert gateway.token_path(config.gateway, "review-local") == config.gateway.directory / "local-client.token"
    with pytest.raises(gateway.GatewayError, match="cannot read"):
        gateway.read_token(config.gateway, "review-ci")
    config.gateway.directory.mkdir(parents=True)
    (config.gateway.directory / "client.token").write_text("\n")
    with pytest.raises(gateway.GatewayError, match="empty"):
        gateway.read_token(config.gateway, "review-ci")
    (config.gateway.directory / "client.token").write_text("tok-value-1234\n")
    assert gateway.read_token(config.gateway, "review-ci").reveal() == "tok-value-1234"


def test_the_published_url_is_read_from_the_script_output() -> None:
    assert gateway.published_url("a\ngateway: published at https://h.ts.net:4000\n") == "https://h.ts.net:4000"
    with pytest.raises(gateway.GatewayError):
        gateway.published_url("gateway: published at http://insecure:4000\n")


def test_openai_reads_both_response_shapes_and_rolls_a_service_account_forward(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    world = make_world(tmp_path)
    ctx = world.context()
    world.seed_roots(ctx)
    world.run.routes.clear()
    created = openai_admin.OpenAIAdmin(ctx).create_service_account("proj_abc", "n")
    minted = openai_admin.OpenAIAdmin(ctx).mint_key("proj_abc", created.service_account_id or "", "n")
    assert (created.service_account_id, created.key_id) == ("svc_1", "key_1") and minted.key_id == "key_2"
    assert created.secret != minted.secret
    assert SENTINELS["openai"] in "".join(ctx.redactor.values)
    openai_admin.OpenAIAdmin(ctx).delete_key("proj_abc", "key_1")
    assert "key_1" not in world.openai_keys


def test_openai_without_a_usable_key_in_the_answer_fails_without_showing_the_body(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    ctx = world.context()
    world.seed_roots(ctx)
    world.http.routes.clear()
    world.http.on(
        "POST",
        r".*service_accounts",
        json_response(200, {"id": "svc", "api_key": {"id": "k"}, "note": "SENTINEL-body-0099"}),
    )
    with pytest.raises(openai_admin.OpenAIError) as caught:
        openai_admin.OpenAIAdmin(ctx).create_service_account("proj_abc", "n")
    assert "SENTINEL" not in str(caught.value)
    world.http.routes.clear()
    world.http.on("POST", r".*service_accounts", json_response(500, {}))
    with pytest.raises(openai_admin.OpenAIError, match="HTTP 500"):
        openai_admin.OpenAIAdmin(ctx).create_service_account("proj_abc", "n")


def test_openai_needs_the_admin_key_in_the_store(tmp_path: Path) -> None:
    ctx = make_world(tmp_path).context()
    with pytest.raises(openai_admin.OpenAIError, match="OPENAI_ADMIN_KEY"):
        openai_admin.OpenAIAdmin(ctx)


def test_the_newest_key_file_for_the_app_wins_and_others_are_ignored(tmp_path: Path) -> None:
    import os

    spec = example(tmp_path).secret("REVIEW_APP_PRIVATE_KEY")
    assert app_key.newest_pem(spec) is None
    assert spec.pem_dir is not None
    spec.pem_dir.mkdir()
    old = spec.pem_dir / "example-app.2026-01-01.private-key.pem"
    new = spec.pem_dir / "example-app.2026-02-01.private-key.pem"
    other = spec.pem_dir / "other-app.2026-03-01.private-key.pem"
    for index, path in enumerate((old, new, other)):
        path.write_text("x")
        os.utime(path, (1_000 + index, 1_000 + index))
    assert app_key.newest_pem(spec) == new
    assert app_key.fingerprint(new) == "example-app.2026-02-01.private-key.pem@1001"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_a_jwt_signed_through_stdin_verifies_with_the_public_key(tmp_path: Path) -> None:
    import base64

    from .fakes import FakeRunner

    key = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, text=True, check=True).stdout
    public = subprocess.run(["openssl", "rsa", "-pubout"], input=key, capture_output=True, text=True, check=True).stdout
    (tmp_path / "pub.pem").write_text(public)
    from theseus_reviewer.infra.transport import run_command

    calls: list[tuple[str, ...]] = []

    def run(argv, *, stdin=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return run_command(argv, stdin=stdin)

    ctx = make_world(tmp_path).context()
    ctx.run = run
    jwt = app_key.sign_jwt(ctx, Secret(key), "123456")
    header, claims, signature = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(pad(signature)))
    (tmp_path / "msg").write_text(f"{header}.{claims}")
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(tmp_path / "pub.pem"),
            "-signature",
            str(tmp_path / "sig"),
            str(tmp_path / "msg"),
        ],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert not any(key.splitlines()[1] in part for argv in calls for part in argv)
    assert base64.urlsafe_b64decode(pad(claims)).startswith(b'{"iat":')
    del FakeRunner


def test_a_key_openssl_cannot_read_is_a_clear_error(tmp_path: Path) -> None:
    ctx = make_world(tmp_path).context()
    from .fakes import FakeRunner, completed

    runner = FakeRunner()
    runner.on(
        lambda argv: argv[0] == "openssl", completed(returncode=1, stderr="unable to load key SENTINEL-leak-0001")
    )
    ctx.run = runner
    with pytest.raises(app_key.AppKeyError) as caught:
        app_key.sign_jwt(ctx, Secret("not a key"), "1")
    assert "SENTINEL" not in str(caught.value)
    assert NOW.year == 2026
