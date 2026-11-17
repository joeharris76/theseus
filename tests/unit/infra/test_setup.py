from __future__ import annotations

import io
import json
import logging
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer.cli import main
from theseus_reviewer.infra import cli as infra_cli
from theseus_reviewer.infra.state import State
from theseus_reviewer.infra.store import FileStore
from theseus_reviewer.infra.tailscale import standardize

from .fakes import SENTINELS
from .world import NOW, PUBLISHED, World, make_world

pytestmark = [pytest.mark.unit, pytest.mark.fast]

SECRET_NAMES = [
    "REVIEW_APP_ID",
    "REVIEW_APP_PRIVATE_KEY",
    "REVIEW_GATEWAY_TOKEN",
    "REVIEW_GATEWAY_URL",
    "TS_OAUTH_CI_CLIENT_ID",
    "TS_OAUTH_CI_SECRET",
    "OPENAI_API_KEY",
    "TYPESAFE_API_KEY",
]


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = make_world(tmp_path)
    ctx = w.context()
    w.seed_roots(ctx)
    w.script(ctx.config)
    w.put_pem(ctx.config)
    return w


def cli(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    stdin: str | None = None,
    prompt: str | None = None,
) -> tuple[int, str, str]:
    deps = infra_cli.Dependencies(
        http=world.http,
        run=world.run,
        now=lambda: NOW,
        stdin=io.StringIO(stdin or ""),
        prompt=lambda _: prompt or "",
    )
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", deps)
    world.context()
    capsys.readouterr()
    code = main(["infra", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def setup(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, str, str]:
    return cli(world, monkeypatch, capsys, "setup", "--config", str(world.policy_path), *extra)


def all_secret_values(world: World) -> list[str]:
    return [*SENTINELS.values(), *world.clients.values(), *world.openai_keys.values()]


def mutating_gh(world: World) -> list[Any]:
    return [
        call
        for call in world.run.calls
        if call.argv[:2] == ("gh", "secret") or "-X" in call.argv and call.argv[3] != "GET"
    ]


def test_a_dry_run_changes_nothing_and_shows_the_plan(world: World, monkeypatch, capsys) -> None:
    code, out, _ = setup(world, monkeypatch, capsys)
    assert code == 0
    assert "+ github.environment:" in out and "+ tailscale.policy:" in out and "+ gateway.obtain:" in out
    assert "dry run: nothing was changed" in out
    assert world.http.mutating() == [call for call in world.http.calls if call.url.endswith("/oauth/token")]
    assert mutating_gh(world) == []
    assert world.environment is None and world.secrets == {} and world.policies == []
    assert not world.policy_path.parent.parent.joinpath("state").exists()
    assert not Path(world.infra["state"]).exists()
    assert FileStore(Path(world.infra["store"]["dir"])).names() == [
        "OPENAI_ADMIN_KEY",
        "TAILSCALE_ROOT_CLIENT_ID",
        "TAILSCALE_ROOT_CLIENT_SECRET",
        "TYPESAFE_API_KEY",
    ]


def test_apply_builds_everything_and_a_second_run_plans_nothing(world: World, monkeypatch, capsys) -> None:
    code, out, err = setup(world, monkeypatch, capsys, "--apply")
    assert (code, err) == (0, ""), out
    assert world.environment == {
        "name": "review",
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }
    assert world.policies == ["develop", "release/*"]
    assert {name: value for name, (value, _) in world.secrets.items()} == {
        "REVIEW_APP_ID": "123456",
        "REVIEW_APP_PRIVATE_KEY": world.jwt_signed_with[0],
        "REVIEW_GATEWAY_TOKEN": SENTINELS["gateway"],
        "REVIEW_GATEWAY_URL": PUBLISHED,
        "TS_OAUTH_CI_CLIENT_ID": "kclient1",
        "TS_OAUTH_CI_SECRET": world.clients["kclient1"],
        "OPENAI_API_KEY": world.openai_keys["key_1"],
        "TYPESAFE_API_KEY": SENTINELS["typesafe"],
    }
    assert world.secrets["REVIEW_APP_PRIVATE_KEY"][0].startswith("-----BEGIN RSA PRIVATE KEY-----")
    state = json.loads(Path(world.infra["state"]).read_text())
    assert state["providers"]["tailscale"] == {"client_id": "kclient1"}
    assert state["providers"]["openai"] == {"service_account_id": "svc_1", "key_id": "key_1"}
    assert sorted(state["secrets"]) == sorted(SECRET_NAMES)
    assert all(record["environments"] == {"Example-Org/example:review": 1} for record in state["secrets"].values())

    before = (len(world.http.calls), len(world.run.calls))
    code, out, _ = setup(world, monkeypatch, capsys, "--apply", "--json")
    assert code == 0
    assert json.loads(out)["actions"] == []
    assert json.loads(out)["blocked"] == []
    assert mutating_gh(world) == [c for c in mutating_gh(world) if c in world.run.calls[: before[1]]]
    assert not [c for c in world.http.calls[before[0] :] if c.method not in ("GET",) and "oauth/token" not in c.url]


def test_the_tailnet_policy_keeps_the_owners_comments_and_gains_tag_grant_and_test(
    world: World, monkeypatch, capsys
) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    text = world.policy_text
    assert "// Example tailnet policy" in text and "// keep this comment" in text and "// tests below" in text
    policy = standardize(text)
    assert policy["tagOwners"] == {"tag:other": ["autogroup:admin"], "tag:review-ci": ["autogroup:admin"]}
    assert policy["grants"][0] == {"src": ["*"], "dst": ["*"], "ip": ["*"]}
    assert policy["grants"][1] == {"src": ["tag:review-ci"], "dst": ["tag:review-gateway"], "ip": ["tcp:4000"]}
    assert policy["tests"] == [
        {"src": "tag:review-ci", "accept": ["tag:review-gateway:4000"], "deny": ["tag:review-gateway:22"]}
    ]
    saves = [c for c in world.http.calls if c.method == "POST" and c.url.endswith("/acl")]
    validations = [c for c in world.http.calls if c.url.endswith("/acl/validate")]
    assert len(saves) == len(validations) == 1
    assert saves[0].headers["If-Match"] == '"etag-1"'
    assert world.http.calls.index(validations[0]) < world.http.calls.index(saves[0])


def test_a_rejected_policy_is_never_saved_and_what_needs_it_is_skipped(world: World, monkeypatch, capsys) -> None:
    world.reject_policy = "test failed: tag:review-ci can reach :22"
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1
    assert "Tailscale rejects the edited policy: test failed" in out
    assert world.policy_text.startswith("// Example") and world.etag == 1
    assert world.clients == {}
    assert "skipped tailscale.oauth-client" in out
    assert "TS_OAUTH_CI_SECRET" in out and "set" in out
    assert "REVIEW_GATEWAY_TOKEN" in world.secrets


def test_a_policy_that_changed_since_it_was_read_is_refused(world: World, monkeypatch, capsys) -> None:
    original = world._acl_get

    def racing(call: Any) -> Any:
        response = original(call)
        world.etag += 1
        return response

    world.http.routes = [
        (m, p, racing if p.pattern.endswith("/acl") and m == "GET" else h) for m, p, h in world.http.routes
    ]
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "changed since it was read" in out
    assert world.policy_text.startswith("// Example")


def test_a_failed_secret_set_is_not_recorded_and_the_next_run_does_only_that(world: World, monkeypatch, capsys) -> None:
    world.gh_fail_secret = "REVIEW_GATEWAY_URL"
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "FAILED deliver:REVIEW_GATEWAY_URL" in out
    assert "REVIEW_GATEWAY_URL" not in world.secrets
    world.gh_fail_secret = ""
    code, out, _ = setup(world, monkeypatch, capsys, "--json")
    assert [a["id"] for a in json.loads(out)["actions"]] == ["deliver:REVIEW_GATEWAY_URL"]
    code, _, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 0 and world.secrets["REVIEW_GATEWAY_URL"][0] == PUBLISHED


def test_an_openai_key_the_provider_rejects_is_revoked_and_not_stored(world: World, monkeypatch, capsys) -> None:
    world.http.routes = [
        r for r in world.http.routes if not r[1].pattern.endswith("/models") or "openai" not in r[1].pattern
    ]
    world.http.on(
        "GET",
        r"https://api\.openai\.com/v1/models",
        lambda call: __import__("tests.unit.infra.fakes", fromlist=["x"]).json_response(401, {}),
    )
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "OpenAI does not accept the new key" in out
    assert world.openai_keys == {}
    assert "OPENAI_API_KEY" not in FileStore(Path(world.infra["store"]["dir"])).names()
    assert "OPENAI_API_KEY" not in world.secrets


def test_a_missing_manual_secret_blocks_only_itself_and_exits_3(world: World, monkeypatch, capsys) -> None:
    FileStore(Path(world.infra["store"]["dir"])).delete("TYPESAFE_API_KEY")
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 3
    assert "! seed:TYPESAFE_API_KEY: not in the store; run: theseus infra seed TYPESAFE_API_KEY" in out
    assert "TYPESAFE_API_KEY" not in world.secrets and len(world.secrets) == 7
    code, out, _ = setup(world, monkeypatch, capsys, "--json")
    assert json.loads(out)["actions"] == [] and len(json.loads(out)["blocked"]) == 1


def test_a_typesafe_key_the_provider_rejects_is_not_delivered(world: World, monkeypatch, capsys) -> None:
    world.typesafe_ok = False
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "the provider rejects the stored TYPESAFE_API_KEY" in out
    assert "TYPESAFE_API_KEY" not in world.secrets


def test_without_the_root_credentials_everything_that_needs_them_is_blocked(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    world = make_world(tmp_path)
    ctx = world.context()
    world.script(ctx.config)
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 3
    assert "! tailscale.policy: needs the Tailscale root OAuth client" in out
    assert "! tailscale.oauth-client:" in out
    assert "! openai:OPENAI_API_KEY: needs the Admin API key; run: theseus infra seed OPENAI_ADMIN_KEY" in out
    assert world.http.calls == []


def test_the_app_key_is_checked_against_the_app_before_it_is_stored(world: World, monkeypatch, capsys) -> None:
    world.app_id = 999
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "the key belongs to App 999, not 123456" in out
    assert "REVIEW_APP_PRIVATE_KEY" not in FileStore(Path(world.infra["store"]["dir"])).names()
    assert "REVIEW_APP_PRIVATE_KEY" not in world.secrets


def test_the_app_key_is_signed_through_stdin_and_never_argv(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    signings = [c for c in world.run.calls if c.argv[0] == "openssl"]
    assert len(signings) == 1 and signings[0].stdin == world.jwt_signed_with[0]
    assert "/dev/stdin" in signings[0].argv
    assert not any(SENTINELS["pem_marker"] in part for part in signings[0].argv)


def test_a_new_app_key_file_is_picked_up_and_old_ones_are_not_repeated(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    pem = world.put_pem(world.context().config)
    newer = pem.with_name("example-app.2026-11-15.private-key.pem")
    newer.write_text(pem.read_text().replace("secondline", "newer-line!"))
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 0 and "app-key:REVIEW_APP_PRIVATE_KEY" in out and "deliver:REVIEW_APP_PRIVATE_KEY" in out
    assert "newer-line!" in world.secrets["REVIEW_APP_PRIVATE_KEY"][0]
    code, out, _ = setup(world, monkeypatch, capsys, "--json")
    assert json.loads(out)["actions"] == []


def test_no_app_key_anywhere_is_a_blocked_owner_step(tmp_path: Path, monkeypatch, capsys) -> None:
    world = make_world(tmp_path)
    ctx = world.context()
    world.seed_roots(ctx)
    world.script(ctx.config)
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 3 and "! app-key:REVIEW_APP_PRIVATE_KEY: no key yet: generate a private key" in out


def test_a_failing_host_script_is_reported_and_leaves_the_gateway_secrets_unset(
    world: World, monkeypatch, capsys
) -> None:
    world.host_fail = True
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "host script step 'gateway' failed: error: the broker is not running" in out
    assert "REVIEW_GATEWAY_TOKEN" not in world.secrets and "skipped deliver:REVIEW_GATEWAY_URL" in out


def test_only_limits_the_plan_to_the_named_secrets(world: World, monkeypatch, capsys) -> None:
    code, out, _ = setup(world, monkeypatch, capsys, "--apply", "--only", "TYPESAFE_API_KEY")
    assert code == 0
    assert list(world.secrets) == ["TYPESAFE_API_KEY"] and world.policies == []
    assert world.policy_text.startswith("// Example")
    code, _, err = setup(world, monkeypatch, capsys, "--only", "NOPE")
    assert code == 2 and "NOPE" in err


def test_an_existing_environment_with_custom_policies_is_left_alone(world: World, monkeypatch, capsys) -> None:
    world.environment = {
        "name": "review",
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        "reviewers": ["x"],
    }
    world.policies = ["develop"]
    setup(world, monkeypatch, capsys, "--apply")
    assert world.environment["reviewers"] == ["x"]
    assert world.policies == ["develop", "release/*"]


def test_an_oauth_client_that_no_longer_exists_is_recreated(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    world.clients.clear()
    code, out, _ = setup(world, monkeypatch, capsys, "--apply")
    assert code == 0 and "tailscale.oauth-client" in out
    assert list(world.clients) == ["kclient1"] and world.secrets["TS_OAUTH_CI_CLIENT_ID"][0] == "kclient1"


# --- the security properties ---------------------------------------------------------------------------------


def _everything_shown(world: World, out: str, err: str, log: str) -> str:
    state = Path(world.infra["state"]).read_text()
    return "\n".join([out, err, log, state])


@pytest.mark.parametrize("argv", [["--apply"], ["--apply", "--json"], ["--json"], []])
def test_no_secret_value_appears_in_any_output_state_log_or_argv(
    world: World, monkeypatch, capsys, caplog, argv: list[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    code, out, err = setup(world, monkeypatch, capsys, *argv)
    if "--apply" not in argv:
        Path(world.infra["state"]).parent.mkdir(parents=True, exist_ok=True)
        Path(world.infra["state"]).write_text("{}")
    shown = _everything_shown(world, out, err, caplog.text)
    for value in all_secret_values(world) + ["secondlineofkeybody", "BEGIN RSA PRIVATE KEY"]:
        assert value not in shown, value
    for call in world.run.calls:
        assert not any(value in part for value in all_secret_values(world) for part in call.argv)
    for http in world.http.calls:
        assert not any(value in http.url for value in all_secret_values(world))


def test_values_travel_only_on_stdin_and_in_provider_requests(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    sets = [c for c in world.run.calls if c.argv[:3] == ("gh", "secret", "set")]
    assert sorted(c.argv[3] for c in sets) == sorted(SECRET_NAMES)
    assert all(c.stdin for c in sets)
    for call in world.http.calls:
        for sentinel in (SENTINELS["typesafe"], SENTINELS["gateway"], SENTINELS["openai"]):
            assert sentinel not in call.url
    host = [c for c in world.run.calls if len(c.argv) == 2 and c.argv[1] == "gateway"]
    assert host and all(c.stdin is None for c in host)


def test_a_provider_error_that_echoes_a_secret_is_scrubbed(world: World, monkeypatch, capsys) -> None:
    world.gh_fail_secret = "TYPESAFE_API_KEY"
    original = world._gh_secret

    def leaking(call: Any) -> Any:
        from .fakes import completed

        if call.argv[3] == "TYPESAFE_API_KEY":
            return completed(returncode=1, stderr=f"bad value {call.stdin} for secret")
        return original(call)

    world.run.routes.insert(0, (lambda argv: argv[:3] == ["gh", "secret", "set"], leaking))
    code, out, err = setup(world, monkeypatch, capsys, "--apply")
    assert code == 1
    assert SENTINELS["typesafe"] not in out + err and "bad value [redacted] for secret" in out


def test_the_state_and_store_files_are_private(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    state = Path(world.infra["state"])
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    store = Path(world.infra["store"]["dir"])
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in store.iterdir())
    assert not any(SENTINELS["typesafe"] in text for text in [state.read_text()])
    assert State.load(state).version("TYPESAFE_API_KEY") == 1


# --- seed and status --------------------------------------------------------------------------------------------


def test_seed_reads_stdin_and_stores_without_echo_or_argv(world: World, monkeypatch, capsys) -> None:
    value = "SENTINEL-seeded-value-0010"
    code, out, err = cli(
        world, monkeypatch, capsys, "seed", "TYPESAFE_API_KEY", "--config", str(world.policy_path), stdin=value + "\n"
    )
    assert (code, out, err) == (0, "seeded TYPESAFE_API_KEY\n", "")
    assert FileStore(Path(world.infra["store"]["dir"])).get("TYPESAFE_API_KEY").reveal() == value
    assert State.load(Path(world.infra["state"])).version("TYPESAFE_API_KEY") == 1


def test_seed_prompts_when_stdin_is_a_terminal(world: World, monkeypatch, capsys) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    deps = infra_cli.Dependencies(stdin=Tty(), prompt=lambda text: "SENTINEL-prompted-0011")
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", deps)
    assert main(["infra", "seed", "OPENAI_ADMIN_KEY", "--config", str(world.policy_path)]) == 0
    assert FileStore(Path(world.infra["store"]["dir"])).get("OPENAI_ADMIN_KEY").reveal() == "SENTINEL-prompted-0011"
    assert "SENTINEL" not in capsys.readouterr().out


def test_seed_from_a_file_keeps_a_multiline_pem_intact(world: World, monkeypatch, capsys, tmp_path: Path) -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----\n"
    path = tmp_path / "k.pem"
    path.write_text(pem)
    code, _, _ = cli(
        world,
        monkeypatch,
        capsys,
        "seed",
        "REVIEW_APP_PRIVATE_KEY",
        "--config",
        str(world.policy_path),
        "--from-file",
        str(path),
    )
    assert code == 0
    assert FileStore(Path(world.infra["store"]["dir"])).get("REVIEW_APP_PRIVATE_KEY").reveal() == pem


def test_seed_accepts_a_pickup_name_and_the_roots_but_nothing_else(world: World, monkeypatch, capsys) -> None:
    for name, ok in [
        ("TYPESAFE_API_KEY.next", True),
        ("TAILSCALE_ROOT_CLIENT_SECRET", True),
        ("RANDOM_NAME", False),
        ("bad name", False),
    ]:
        code, _, err = cli(
            world, monkeypatch, capsys, "seed", name, "--config", str(world.policy_path), stdin="SENTINEL-v-0012"
        )
        assert (code == 0) == ok, (name, err)
        assert "SENTINEL" not in err
    assert State.load(Path(world.infra["state"])).version("TYPESAFE_API_KEY.next") == 0


def test_seed_refuses_an_empty_value(world: World, monkeypatch, capsys) -> None:
    code, _, err = cli(
        world, monkeypatch, capsys, "seed", "TYPESAFE_API_KEY", "--config", str(world.policy_path), stdin="\n"
    )
    assert code == 2 and "empty" in err


def test_status_is_clean_after_setup_and_shows_drift_after_a_reseed(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    code, out, _ = cli(world, monkeypatch, capsys, "status", "--config", str(world.policy_path), "--json")
    data = json.loads(out)
    assert code == 0 and data["drift"] is False and len(data["secrets"]) == 8
    assert all(row["in_store"] and row["in_environment"] for row in data["secrets"])
    cli(
        world,
        monkeypatch,
        capsys,
        "seed",
        "TYPESAFE_API_KEY",
        "--config",
        str(world.policy_path),
        stdin="SENTINEL-new-typesafe-0013",
    )
    code, out, _ = cli(world, monkeypatch, capsys, "status", "--config", str(world.policy_path))
    assert code == 1 and "TYPESAFE_API_KEY" in out and "stale" in out and "SENTINEL" not in out


def test_status_reports_a_manual_key_past_its_age_limit(world: World, monkeypatch, capsys) -> None:
    setup(world, monkeypatch, capsys, "--apply")
    import os

    old = NOW.timestamp() - 91 * 86400
    path = Path(world.infra["store"]["dir"]) / "TYPESAFE_API_KEY"
    os.utime(path, (old, old))
    code, out, _ = cli(world, monkeypatch, capsys, "status", "--config", str(world.policy_path))
    assert code == 1 and re.search(r"aged: TYPESAFE_API_KEY is 91 days old \(limit 90\)", out)


def test_a_config_without_an_infra_section_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "review.yml"
    path.write_text("version: 2\n")
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", infra_cli.Dependencies())
    assert main(["infra", "setup", "--config", str(path)]) == 2
    assert "no infra: section" in capsys.readouterr().err
