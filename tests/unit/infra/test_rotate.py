from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from theseus_reviewer.infra import rotate
from theseus_reviewer.infra.rotate_command import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Report, run
from theseus_reviewer.infra.store import PENDING_SUFFIX, Secret

from .fakes import completed
from .rotation_world import (
    ENV_KEY,
    NEW_GATEWAY,
    NEW_PEM,
    NEW_TYPESAFE,
    OLD,
    World,
    add_new_pem,
    build_world,
)

TS_NAMES = ("TS_OAUTH_CI_CLIENT_ID", "TS_OAUTH_CI_SECRET")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch)


def rotate_now(world: World, *only: str, resume: bool = False, as_json: bool = False) -> Report:
    return run(world.ctx, only=list(only), apply=True, resume=resume, as_json=as_json)


def stored(world: World, name: str) -> str | None:
    value = world.store.get(name)
    return None if value is None else value.reveal()


def old_everywhere(world: World, names: tuple[str, ...]) -> None:
    for name in names:
        assert world.env[name] == OLD[name], name
        assert stored(world, name) == OLD[name], name
        assert stored(world, name + PENDING_SUFFIX) is None, name
    assert not [key for key in world.state.providers if key.startswith(rotate.STATE_KEY_PREFIX)]


def new_everywhere(world: World, names: tuple[str, ...]) -> None:
    for name in names:
        assert world.env[name] != OLD[name], name
        assert stored(world, name) == world.env[name], name
        assert stored(world, name + PENDING_SUFFIX) is None, name
        assert world.state.delivered(name, ENV_KEY) == world.state.version(name)
    assert not [key for key in world.state.providers if key.startswith(rotate.STATE_KEY_PREFIX)]


def assert_no_values(world: World, report: Report) -> None:
    values = world.every_value()
    assert not [value for value in values if value in report.text]
    assert not world.values_in_argv_or_urls(values)
    state_text = world.config.state_path.read_text(encoding="utf-8")
    assert not [value for value in values if value in state_text]


# -- credential-specific facts ----------------------------------------------------------------------------------


def ts_old_works(world: World) -> bool:
    return world.ts_clients.get(OLD["TS_OAUTH_CI_CLIENT_ID"]) == OLD["TS_OAUTH_CI_SECRET"]


def ts_only_old(world: World) -> bool:
    return not [key for key in world.ts_clients if key.startswith("NEW-")]


def openai_old_works(world: World) -> bool:
    return world.openai_keys.get("key_old") == OLD["OPENAI_API_KEY"] and len(world.openai_keys) == 1


def gateway_old_works(world: World) -> bool:
    return world.accepted_gateway_tokens() == {OLD["REVIEW_GATEWAY_TOKEN"]}


# -- success ---------------------------------------------------------------------------------------------------


def test_tailscale_rotation_replaces_the_client(world: World) -> None:
    report = rotate_now(world, *TS_NAMES)
    assert report.code == EXIT_OK, report.text
    new_everywhere(world, TS_NAMES)
    assert world.env["TS_OAUTH_CI_CLIENT_ID"] in world.ts_clients
    assert OLD["TS_OAUTH_CI_CLIENT_ID"] not in world.ts_clients
    assert world.ts_clients[world.env["TS_OAUTH_CI_CLIENT_ID"]] == world.env["TS_OAUTH_CI_SECRET"]
    assert world.state.provider("tailscale") == {"client_id": world.env["TS_OAUTH_CI_CLIENT_ID"]}
    assert_no_values(world, report)


def test_openai_rotation_replaces_the_key(world: World) -> None:
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_OK, report.text
    new_everywhere(world, ("OPENAI_API_KEY",))
    assert "key_old" not in world.openai_keys
    assert list(world.openai_keys.values()) == [world.env["OPENAI_API_KEY"]]
    record = world.state.provider("openai")
    assert record["service_account_id"] == "sa_1" and record["key_id"] in world.openai_keys
    assert_no_values(world, report)


def test_gateway_rotation_swaps_the_token_and_drops_the_temporary_client(world: World) -> None:
    report = rotate_now(world, "REVIEW_GATEWAY_TOKEN")
    assert report.code == EXIT_OK, report.text
    new_everywhere(world, ("REVIEW_GATEWAY_TOKEN",))
    assert world.env["REVIEW_GATEWAY_TOKEN"] == NEW_GATEWAY
    assert world.accepted_gateway_tokens() == {NEW_GATEWAY}
    assert not (world.config.gateway.directory / "ci-next-client.token").exists()  # type: ignore[union-attr]
    steps = [call.argv[1] for call in world.runner.calls if call.argv[0].endswith("gateway.sh")]
    assert steps == ["gateway", "gateway"]
    assert_no_values(world, report)


def test_gateway_new_token_is_checked_locally_and_over_the_tailnet_before_the_swap(world: World) -> None:
    rotate_now(world, "REVIEW_GATEWAY_TOKEN")
    curls = [call for call in world.runner.calls if call.argv[0] == "curl"]
    assert [c.argv[-1].rsplit(":4000", 1)[1] for c in curls][:2] == ["/healthz", "/v1/models"]
    tailnet = [c for c in world.http.calls if c.url.endswith(":4000/v1/models")]
    assert tailnet and tailnet[0].headers["Authorization"] == f"Bearer {NEW_GATEWAY}"
    assert all(NEW_GATEWAY not in " ".join(c.argv) for c in curls)


def test_typesafe_picks_up_a_newer_key_from_the_store(world: World) -> None:
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK, report.text
    assert world.env["TYPESAFE_API_KEY"] == NEW_TYPESAFE
    assert stored(world, "TYPESAFE_API_KEY") == NEW_TYPESAFE
    assert stored(world, "TYPESAFE_API_KEY.next") is None
    assert "revoke the previous Typesafe key" in report.text
    assert_no_values(world, report)


def test_typesafe_picks_up_a_newer_key_from_the_configured_keychain_item(world: World) -> None:
    world.runner.routes.insert(
        0,
        (
            lambda argv: argv[0] == "security" and "TYPESAFE_API_KEY" in argv and "tester" in argv,
            lambda call: completed(NEW_TYPESAFE + "\n"),
        ),
    )
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK, report.text
    assert world.env["TYPESAFE_API_KEY"] == NEW_TYPESAFE
    assert not world.values_in_argv_or_urls([NEW_TYPESAFE])


def test_typesafe_without_a_newer_key_reports_age_and_the_manual_step(world: World) -> None:
    world.store.put("TYPESAFE_API_KEY", Secret(OLD["TYPESAFE_API_KEY"]))  # modified now (2026), so 0 days old
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK
    assert "TYPESAFE_API_KEY: " in report.text and "limit" in report.text
    assert "theseus infra seed TYPESAFE_API_KEY.next" in report.text
    assert world.runner.calls  # only the keychain pickup probe ran
    assert not [c for c in world.runner.calls if c.argv[0] == "gh"]
    assert world.http.mutating() == []


def test_typesafe_reports_a_key_past_its_limit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    world.ctx.now = lambda: datetime(2027, 6, 1, tzinfo=UTC)
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert "past the 90-day limit" in report.text


def test_app_key_adopts_a_newer_pem_and_prints_the_delete_step(world: World) -> None:
    add_new_pem(world)
    report = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert report.code == EXIT_OK, report.text
    assert world.env["REVIEW_APP_PRIVATE_KEY"] == NEW_PEM
    assert stored(world, "REVIEW_APP_PRIVATE_KEY") == NEW_PEM
    assert "delete the old private key in the example-app GitHub App settings" in report.text
    assert_no_values(world, report)
    assert all(call.argv[0] != "openssl" or NEW_PEM not in " ".join(call.argv) for call in world.runner.calls)
    signed = [call for call in world.runner.calls if call.argv[0] == "openssl"]
    assert signed and signed[0].stdin == NEW_PEM


def test_app_key_without_a_newer_pem_reports_age_and_the_manual_step(world: World) -> None:
    report = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert report.code == EXIT_OK
    assert "days old, past the 180-day limit" in report.text
    assert "generate a private key in the settings of the example-app GitHub App" in report.text
    assert world.env["REVIEW_APP_PRIVATE_KEY"] == OLD["REVIEW_APP_PRIVATE_KEY"]


def test_app_key_within_its_limit_prints_only_the_age(world: World) -> None:
    world.secret_updated["REVIEW_APP_PRIVATE_KEY"] = "2026-11-01T00:00:00Z"
    report = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert "within the 180-day limit" in report.text
    assert "generate a private key" not in report.text


def test_app_key_ignores_a_pem_older_than_the_secret(world: World) -> None:
    add_new_pem(world)
    world.secret_updated["REVIEW_APP_PRIVATE_KEY"] = "2026-11-12T00:00:00Z"
    report = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert world.env["REVIEW_APP_PRIVATE_KEY"] == OLD["REVIEW_APP_PRIVATE_KEY"]
    assert report.code == EXIT_OK


# -- rollback: one test per failure point per rotator ------------------------------------------------------------


@dataclass
class Scenario:
    id: str
    only: tuple[str, ...]
    names: tuple[str, ...]
    arrange: Callable[[World], None]
    old_works: Callable[[World], bool]
    new_absent: Callable[[World], bool]


def _store_fail(*names: str) -> Callable[[World], None]:
    return lambda world: world.store.fail_put.update(name + PENDING_SUFFIX for name in names)


def _set_fail(name: str) -> Callable[[World], None]:
    return lambda world: world.fail_set.add(name)


def _typesafe_next(value: str) -> Callable[[World], None]:
    return lambda world: world.store.put("TYPESAFE_API_KEY.next", Secret(value))


def _app_pem(world: World) -> None:
    add_new_pem(world)


def _compose(*steps: Callable[[World], None]) -> Callable[[World], None]:
    def arrange(world: World) -> None:
        for step in steps:
            step(world)

    return arrange


def _reject_new(world: World) -> None:
    world.reject_new = True


SCENARIOS = [
    Scenario("tailscale-store", TS_NAMES, TS_NAMES, _store_fail("TS_OAUTH_CI_SECRET"), ts_old_works, ts_only_old),
    Scenario("tailscale-set", TS_NAMES, TS_NAMES, _set_fail("TS_OAUTH_CI_CLIENT_ID"), ts_old_works, ts_only_old),
    Scenario("tailscale-health", TS_NAMES, TS_NAMES, _reject_new, ts_old_works, ts_only_old),
    Scenario(
        "openai-store",
        ("OPENAI_API_KEY",),
        ("OPENAI_API_KEY",),
        _store_fail("OPENAI_API_KEY"),
        openai_old_works,
        lambda w: len(w.openai_keys) == 1,
    ),
    Scenario(
        "openai-set",
        ("OPENAI_API_KEY",),
        ("OPENAI_API_KEY",),
        _set_fail("OPENAI_API_KEY"),
        openai_old_works,
        lambda w: len(w.openai_keys) == 1,
    ),
    Scenario(
        "openai-health",
        ("OPENAI_API_KEY",),
        ("OPENAI_API_KEY",),
        _reject_new,
        openai_old_works,
        lambda w: len(w.openai_keys) == 1,
    ),
    Scenario(
        "gateway-store",
        ("REVIEW_GATEWAY_TOKEN",),
        ("REVIEW_GATEWAY_TOKEN",),
        _store_fail("REVIEW_GATEWAY_TOKEN"),
        gateway_old_works,
        lambda w: NEW_GATEWAY not in w.accepted_gateway_tokens(),
    ),
    Scenario(
        "gateway-set",
        ("REVIEW_GATEWAY_TOKEN",),
        ("REVIEW_GATEWAY_TOKEN",),
        _set_fail("REVIEW_GATEWAY_TOKEN"),
        gateway_old_works,
        lambda w: NEW_GATEWAY not in w.accepted_gateway_tokens(),
    ),
    Scenario(
        "gateway-health",
        ("REVIEW_GATEWAY_TOKEN",),
        ("REVIEW_GATEWAY_TOKEN",),
        lambda w: w.gateway_rejects.add(NEW_GATEWAY),
        gateway_old_works,
        lambda w: NEW_GATEWAY not in w.accepted_gateway_tokens(),
    ),
    Scenario(
        "gateway-swap",
        ("REVIEW_GATEWAY_TOKEN",),
        ("REVIEW_GATEWAY_TOKEN",),
        lambda w: setattr(w, "fail_gateway_step_call", 2),
        gateway_old_works,
        lambda w: NEW_GATEWAY not in w.accepted_gateway_tokens(),
    ),
    Scenario(
        "typesafe-store",
        ("TYPESAFE_API_KEY",),
        ("TYPESAFE_API_KEY",),
        _compose(_typesafe_next(NEW_TYPESAFE), _store_fail("TYPESAFE_API_KEY")),
        lambda w: True,
        lambda w: True,
    ),
    Scenario(
        "typesafe-set",
        ("TYPESAFE_API_KEY",),
        ("TYPESAFE_API_KEY",),
        _compose(_typesafe_next(NEW_TYPESAFE), _set_fail("TYPESAFE_API_KEY")),
        lambda w: True,
        lambda w: True,
    ),
    Scenario(
        "typesafe-health",
        ("TYPESAFE_API_KEY",),
        ("TYPESAFE_API_KEY",),
        _typesafe_next("BAD-typesafe-key-zzzzzzzz"),
        lambda w: True,
        lambda w: True,
    ),
    Scenario(
        "app-key-store",
        ("REVIEW_APP_PRIVATE_KEY",),
        ("REVIEW_APP_PRIVATE_KEY",),
        _compose(_app_pem, _store_fail("REVIEW_APP_PRIVATE_KEY")),
        lambda w: True,
        lambda w: True,
    ),
    Scenario(
        "app-key-set",
        ("REVIEW_APP_PRIVATE_KEY",),
        ("REVIEW_APP_PRIVATE_KEY",),
        _compose(_app_pem, _set_fail("REVIEW_APP_PRIVATE_KEY")),
        lambda w: True,
        lambda w: True,
    ),
    Scenario(
        "app-key-validate",
        ("REVIEW_APP_PRIVATE_KEY",),
        ("REVIEW_APP_PRIVATE_KEY",),
        _compose(_app_pem, lambda w: setattr(w, "app_id_answer", "999")),
        lambda w: True,
        lambda w: True,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_a_failed_rotation_leaves_the_old_credential_set_and_working(world: World, scenario: Scenario) -> None:
    scenario.arrange(world)
    report = rotate_now(world, *scenario.only)
    assert report.code == EXIT_FAILED, report.text
    old_everywhere(world, scenario.names)
    assert scenario.old_works(world)
    assert scenario.new_absent(world)
    assert_no_values(world, report)


def test_a_failed_rotation_names_its_status_in_json(world: World) -> None:
    world.fail_set.add("OPENAI_API_KEY")
    report = rotate_now(world, "OPENAI_API_KEY", as_json=True)
    data = json.loads(report.text)
    assert data["applied"] is True
    (result,) = data["results"]
    assert result["credential"] == "openai" and result["status"] == rotate.ROLLED_BACK
    assert "the old credential is set again" in result["detail"]


def test_a_rollback_that_cannot_finish_keeps_its_record_for_resume(world: World) -> None:
    world.fail_set.add("OPENAI_API_KEY")
    world.fail_delete.add("key_new_1")
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED
    assert "rollback incomplete" in report.text
    assert world.state.provider("rotate:openai")["phase"] == rotate.PHASE_CREATED
    assert world.state.provider("rotate:openai")["new.key_id"] == "key_new_1"
    world.fail_delete.clear()
    resumed = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert openai_old_works(world)
    old_everywhere(world, ("OPENAI_API_KEY",))


# -- a failed revoke keeps the new credential live; --resume finishes ----------------------------------------------


def test_tailscale_failed_revoke_keeps_the_new_client_and_resume_finishes(world: World) -> None:
    world.fail_delete.add(OLD["TS_OAUTH_CI_CLIENT_ID"])
    report = rotate_now(world, *TS_NAMES)
    assert report.code == EXIT_FAILED
    assert "the new credential is live" in report.text and "rotate --resume" in report.text
    new_everywhere_pending = world.env["TS_OAUTH_CI_CLIENT_ID"]
    assert new_everywhere_pending.startswith("NEW-") and new_everywhere_pending in world.ts_clients
    assert ts_old_works(world)
    record = world.state.provider("rotate:tailscale")
    assert record["phase"] == rotate.PHASE_PROMOTED and record["old.client_id"] == OLD["TS_OAUTH_CI_CLIENT_ID"]
    world.fail_delete.clear()
    resumed = rotate_now(world, *TS_NAMES, resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert OLD["TS_OAUTH_CI_CLIENT_ID"] not in world.ts_clients
    assert new_everywhere_pending in world.ts_clients
    assert world.state.provider("rotate:tailscale") == {}
    assert_no_values(world, resumed)


def test_openai_failed_revoke_keeps_the_new_key_and_resume_finishes(world: World) -> None:
    world.fail_delete.add("key_old")
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED
    assert world.openai_keys["key_old"] == OLD["OPENAI_API_KEY"]
    assert world.env["OPENAI_API_KEY"] in world.openai_keys.values()
    assert stored(world, "OPENAI_API_KEY") == world.env["OPENAI_API_KEY"]
    world.fail_delete.clear()
    resumed = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert list(world.openai_keys.values()) == [world.env["OPENAI_API_KEY"]]
    assert world.state.provider("rotate:openai") == {}
    assert world.state.provider("openai")["key_id"] in world.openai_keys


def test_a_new_rotation_is_refused_while_one_is_unfinished(world: World) -> None:
    world.fail_delete.add("key_old")
    rotate_now(world, "OPENAI_API_KEY")
    calls = len(world.http.calls)
    again = rotate_now(world, "OPENAI_API_KEY")
    assert again.code == EXIT_FAILED
    assert "an earlier rotation did not finish" in again.text and "--resume" in again.text
    assert len(world.http.calls) == calls


def test_resume_with_nothing_unfinished_changes_nothing(world: World) -> None:
    report = rotate_now(world, *TS_NAMES, "OPENAI_API_KEY", resume=True)
    assert report.code == EXIT_OK
    assert "nothing to resume" in report.text
    assert world.http.calls == [] and world.runner.calls == []


def test_resume_rolls_back_a_rotation_interrupted_before_promotion(world: World) -> None:
    world.state.set_provider(
        "rotate:openai", phase=rotate.PHASE_CREATED, **{"new.key_id": "key_new_7", "old.key_id": "key_old"}
    )
    world.openai_keys["key_new_7"] = "NEW-openai-key-orphan0007"
    world.store.put("OPENAI_API_KEY.pending", Secret("NEW-openai-key-orphan0007"))
    world.state.save()
    report = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert report.code != EXIT_USAGE
    assert "key_new_7" not in world.openai_keys
    assert stored(world, "OPENAI_API_KEY.pending") is None
    assert (
        world.env["OPENAI_API_KEY"] == OLD["OPENAI_API_KEY"]
        and stored(world, "OPENAI_API_KEY") == OLD["OPENAI_API_KEY"]
    )


# -- selection, dry run, redaction ------------------------------------------------------------------------------


def test_only_rejects_a_secret_that_cannot_rotate(world: World) -> None:
    report = rotate_now(world, "REVIEW_APP_ID")
    assert report.code == EXIT_USAGE
    assert "REVIEW_APP_ID cannot be rotated" in report.text
    assert world.http.calls == []


def test_without_only_every_rotatable_credential_is_rotated(world: World) -> None:
    add_new_pem(world)
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    report = rotate_now(world)
    assert report.code == EXIT_OK, report.text
    for kind in ("tailscale", "gateway", "openai", "typesafe", "app_key"):
        assert kind in report.text
    assert_no_values(world, report)


def test_dry_run_plans_and_changes_nothing(world: World) -> None:
    add_new_pem(world)
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    before = {name: stored(world, name) for name in world.store.names()}
    report = run(world.ctx, only=[], apply=False, resume=False, as_json=False)
    assert report.code == EXIT_OK
    assert "dry run" in report.text and "add --apply" in report.text
    assert "temporary client review-ci-next" in report.text
    assert world.http.mutating() == []
    assert not [c for c in world.runner.calls if c.argv[0] in ("curl", "openssl") or c.argv[1:3] == ("secret", "set")]
    assert not [c for c in world.runner.calls if c.argv[0].endswith("gateway.sh")]
    assert before == {name: stored(world, name) for name in world.store.names()}
    assert_no_values(world, report)
    as_json = run(world.ctx, only=[], apply=False, resume=False, as_json=True)
    data = json.loads(as_json.text)
    assert data["applied"] is False and {item["credential"] for item in data["plan"]} >= {"tailscale", "openai"}


def test_dry_run_reports_an_unfinished_rotation(world: World) -> None:
    world.fail_delete.add("key_old")
    rotate_now(world, "OPENAI_API_KEY")
    report = run(world.ctx, only=["OPENAI_API_KEY"], apply=False, resume=False, as_json=False)
    assert "did not finish (phase promoted); with --resume: revoke the old credential" in report.text


def test_secrets_reach_children_only_on_stdin_and_http_bodies(world: World) -> None:
    add_new_pem(world)
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    rotate_now(world)
    assert not world.values_in_argv_or_urls(world.every_value())
    sets = [c for c in world.runner.calls if c.argv[:3] == ("gh", "secret", "set")]
    assert {c.argv[3] for c in sets} >= {"OPENAI_API_KEY", "REVIEW_GATEWAY_TOKEN", "TS_OAUTH_CI_SECRET"}
    assert all(c.stdin for c in sets)
    for call in world.http.calls:
        assert call.url.startswith("https://")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_no_failure_text_carries_a_value(world: World, scenario: Scenario) -> None:
    scenario.arrange(world)
    report = rotate_now(world, *scenario.only)
    for value in world.every_value():
        assert value not in report.text


def test_a_provider_error_that_echoes_a_secret_is_scrubbed(world: World) -> None:
    def leaky(call):  # noqa: ANN001
        raise rotate.RotationError("provider said: bad key " + world.env["OPENAI_API_KEY"] + " for " + NEW_TYPESAFE)

    world.http.routes.insert(0, ("GET", re.compile(r"https://api\.openai\.com/v1/models"), leaky))
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED
    assert OLD["OPENAI_API_KEY"] not in report.text and NEW_TYPESAFE not in report.text
    assert "[redacted]" in report.text
    assert openai_old_works(world)
