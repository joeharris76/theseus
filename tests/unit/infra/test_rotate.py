from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from theseus_reviewer.cli import main
from theseus_reviewer.infra import cli as infra_cli
from theseus_reviewer.infra import rotate, rotate_command, rotators
from theseus_reviewer.infra.rotate_command import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Report, run
from theseus_reviewer.infra.state import State
from theseus_reviewer.infra.store import PENDING_SUFFIX, Secret

from .fakes import json_response
from .rotation_world import (
    ENV_KEY,
    NEW_GATEWAY,
    NEW_PEM,
    NEW_TYPESAFE,
    NOW,
    OLD,
    PROJECT,
    TS,
    World,
    add_new_pem,
    build_world,
    write_policy_file,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

TS_NAMES = ("TS_OAUTH_CI_CLIENT_ID", "TS_OAUTH_CI_SECRET")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch)


def rotate_now(world: World, *only: str, resume: bool = False, as_json: bool = False) -> Report:
    report = run(world.ctx, only=list(only), apply=True, resume=resume, as_json=as_json)
    world.reload()
    return report


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


MIRROR = ("TYPESAFE_API_KEY", "tester")
LATER, EARLIER = datetime(2099, 1, 1, tzinfo=UTC), datetime(2000, 1, 1, tzinfo=UTC)


def put_mirror(world: World, value: str, written: datetime) -> None:
    world.security.items[MIRROR] = (value, written)


def test_typesafe_picks_up_a_newer_key_from_a_newer_keychain_item(world: World) -> None:
    put_mirror(world, NEW_TYPESAFE, LATER)
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK, report.text
    assert world.env["TYPESAFE_API_KEY"] == NEW_TYPESAFE
    assert not world.values_in_argv_or_urls([NEW_TYPESAFE])
    assert world.security.items[MIRROR][0] == NEW_TYPESAFE


def test_typesafe_ignores_a_keychain_item_older_than_the_stored_key(world: World) -> None:
    put_mirror(world, "STALE-typesafe-key-mmmmmmmm", EARLIER)
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK, report.text
    assert "unchanged" in report.text
    assert world.env["TYPESAFE_API_KEY"] == OLD["TYPESAFE_API_KEY"]
    assert world.security.items[MIRROR][0] == "STALE-typesafe-key-mmmmmmmm"


def test_adopting_from_next_updates_the_keychain_mirror_through_stdin(world: World) -> None:
    put_mirror(world, OLD["TYPESAFE_API_KEY"], EARLIER)
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    assert rotate_now(world, "TYPESAFE_API_KEY").code == EXIT_OK
    assert world.security.items[MIRROR][0] == NEW_TYPESAFE
    assert not world.values_in_argv_or_urls([NEW_TYPESAFE])
    # The next run does not adopt the old value back.
    again = rotate_now(world, "TYPESAFE_API_KEY")
    assert again.code == EXIT_OK and world.env["TYPESAFE_API_KEY"] == NEW_TYPESAFE


def test_typesafe_without_a_newer_key_reports_age_and_the_manual_step(world: World) -> None:
    world.store.put("TYPESAFE_API_KEY", Secret(OLD["TYPESAFE_API_KEY"]))  # modified now (2026), so 0 days old
    report = rotate_now(world, "TYPESAFE_API_KEY")
    assert report.code == EXIT_OK
    assert "TYPESAFE_API_KEY: " in report.text and "limit" in report.text
    assert "theseus infra seed TYPESAFE_API_KEY.next" in report.text
    assert world.runner.calls  # only the keychain pickup probe ran
    assert not [c for c in world.runner.calls if c.argv[:3] == ("gh", "secret", "set")]
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


def test_a_record_whose_secrets_were_reordered_in_the_policy_still_resumes(world: World) -> None:
    world.fail_delete.add(OLD["TS_OAUTH_CI_CLIENT_ID"])
    assert rotate_now(world, *TS_NAMES).code == EXIT_FAILED
    record = world.state.provider("rotate:tailscale")
    record["names"] = ",".join(reversed(record["names"].split(",")))
    world.state.set_provider("rotate:tailscale", **record)
    world.state.save()
    world.fail_delete.clear()
    resumed = rotate_now(world, *TS_NAMES, resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert OLD["TS_OAUTH_CI_CLIENT_ID"] not in world.ts_clients
    assert world.state.provider("rotate:tailscale") == {}


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


# -- the command line -----------------------------------------------------------------------------------------


def invoke(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    deps = infra_cli.Dependencies(http=world.http, run=world.runner, now=lambda: NOW)
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", deps)
    capsys.readouterr()
    code = main(["infra", "rotate", "--config", str(write_policy_file(world)), *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_the_command_is_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["infra", "rotate", "--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--apply", "--only", "--resume", "--json", "--config"):
        assert flag in text


def test_cli_dry_run_is_the_default_and_changes_nothing(world: World, monkeypatch, capsys) -> None:
    code, out, err = invoke(world, monkeypatch, capsys)
    assert code == 0, err
    assert "dry run" in out
    assert world.http.mutating() == []
    assert world.env["OPENAI_API_KEY"] == OLD["OPENAI_API_KEY"]


def test_cli_apply_rotates_and_prints_no_value(world: World, monkeypatch, capsys) -> None:
    add_new_pem(world)
    world.store.put("TYPESAFE_API_KEY.next", Secret(NEW_TYPESAFE))
    code, out, err = invoke(world, monkeypatch, capsys, "--apply")
    assert code == 0, out + err
    for value in world.every_value():
        assert value not in out and value not in err
    assert world.env["OPENAI_API_KEY"] != OLD["OPENAI_API_KEY"]
    assert world.env["REVIEW_APP_PRIVATE_KEY"] == NEW_PEM
    state = State.load(world.config.state_path)
    assert not [key for key in state.providers if key.startswith("rotate:")]
    assert not world.values_in_argv_or_urls(world.every_value())
    text = world.config.state_path.read_text(encoding="utf-8")
    assert not [value for value in world.every_value() if value in text]


def test_cli_exits_non_zero_after_a_rollback_and_after_a_failed_revoke_until_resume(
    world: World, monkeypatch, capsys
) -> None:
    world.fail_set.add("OPENAI_API_KEY")
    code, out, _ = invoke(world, monkeypatch, capsys, "--apply", "--only", "OPENAI_API_KEY")
    assert code == EXIT_FAILED and "rolled-back" in out
    world.fail_delete.add("key_old")
    code, out, _ = invoke(world, monkeypatch, capsys, "--apply", "--only", "OPENAI_API_KEY")
    assert code == EXIT_FAILED and "rotate --resume" in out
    world.fail_delete.clear()
    code, out, _ = invoke(world, monkeypatch, capsys, "--apply", "--only", "OPENAI_API_KEY", "--resume")
    assert code == 0 and "resumed" in out
    assert list(world.openai_keys.values()) == [world.env["OPENAI_API_KEY"]]


def test_cli_rejects_a_name_that_cannot_rotate_and_prints_json(world: World, monkeypatch, capsys) -> None:
    code, _, _ = invoke(world, monkeypatch, capsys, "--only", "REVIEW_GATEWAY_URL")
    assert code == EXIT_USAGE
    code, out, _ = invoke(world, monkeypatch, capsys, "--apply", "--only", "OPENAI_API_KEY", "--json")
    assert code == 0 and json.loads(out)["results"][0]["status"] == "rotated"


# -- the promotion is rolled forward, never back --------------------------------------------------------------


def in_use_works(world: World, names: tuple[str, ...], *, store: bool = True) -> bool:
    """Whether what the environment (and the store) hold is accepted by the fake provider right now."""
    for name in names:
        for value in (world.env[name], stored(world, name)) if store else (world.env[name],):
            if name == "OPENAI_API_KEY" and value not in world.openai_keys.values():
                return False
            if name == "REVIEW_GATEWAY_TOKEN" and value not in world.accepted_gateway_tokens():
                return False
            if name == "TS_OAUTH_CI_SECRET" and value not in world.ts_clients.values():
                return False
            if name == "TS_OAUTH_CI_CLIENT_ID" and value not in world.ts_clients:
                return False
    return True


@pytest.mark.parametrize(
    "names",
    [("OPENAI_API_KEY",), TS_NAMES, ("REVIEW_GATEWAY_TOKEN",)],
    ids=["openai", "tailscale", "gateway"],
)
def test_an_interrupt_at_every_step_boundary_never_deletes_the_credential_in_use(
    tmp_path: Path, names: tuple[str, ...]
) -> None:
    tested = 0
    for point in range(1, 500):
        with pytest.MonkeyPatch.context() as patch:
            world = build_world(tmp_path / f"point{point}", patch)
            world.arm(point)
            try:
                first = rotate_now(world, *names)
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
            world.arm(None)
            world.reload()
            # Mid-swap the gateway already refuses the stored old token; CI, which holds the environment value, must work.
            assert in_use_works(world, names, store=False), f"after an interrupt at boundary {point}"
            if not interrupted:
                assert first.code == EXIT_OK, first.text
                break
            tested += 1
            resumed = rotate_now(world, *names, resume=True)
            assert resumed.code == EXIT_OK, f"boundary {point}: {resumed.text}"
            assert in_use_works(world, names), f"after resuming from boundary {point}"
            for name in names:
                assert stored(world, name) == world.env[name], (point, name)
                assert stored(world, name + PENDING_SUFFIX) is None, (point, name)
                assert stored(world, name + rotate.PREVIOUS_SUFFIX) is None, (point, name)
            assert not [key for key in world.state.providers if key.startswith(rotate.STATE_KEY_PREFIX)], point
    else:
        pytest.fail("the rotation never finished")
    assert tested > 10


def test_an_interrupt_right_after_the_store_put_resumes_forward(world: World) -> None:
    original = world.store.put

    def put_then_stop(name: str, value: Secret) -> None:
        original(name, value)
        if name == "OPENAI_API_KEY":
            raise KeyboardInterrupt

    world.store.put = put_then_stop  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        rotate_now(world, "OPENAI_API_KEY")
    world.store.put = original  # type: ignore[method-assign]
    world.reload()
    new_value = world.env["OPENAI_API_KEY"]
    assert new_value != OLD["OPENAI_API_KEY"] and stored(world, "OPENAI_API_KEY") == new_value
    assert world.state.provider("rotate:openai")["phase"] == rotate.PHASE_PROMOTING
    resumed = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert list(world.openai_keys.values()) == [new_value]
    assert stored(world, "OPENAI_API_KEY") == new_value == world.env["OPENAI_API_KEY"]


def test_a_failing_pending_delete_keeps_the_new_credential_and_resume_finishes(world: World) -> None:
    world.store.fail_delete.add("OPENAI_API_KEY.pending")
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED and "rotate --resume" in report.text
    new_value = world.env["OPENAI_API_KEY"]
    assert new_value in world.openai_keys.values() and stored(world, "OPENAI_API_KEY") == new_value
    assert openai_old_works(world) is False or "key_old" in world.openai_keys
    assert world.state.provider("rotate:openai")["phase"] == rotate.PHASE_PROMOTED
    world.store.fail_delete.clear()
    resumed = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert list(world.openai_keys.values()) == [new_value]
    assert stored(world, "OPENAI_API_KEY.pending") is None


def test_a_failing_promotion_store_put_is_finished_by_resume_not_undone(world: World) -> None:
    world.store.fail_put.add("OPENAI_API_KEY")
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED and "rotate --resume" in report.text
    new_value = world.env["OPENAI_API_KEY"]
    assert new_value in world.openai_keys.values() and "key_old" in world.openai_keys
    assert world.state.provider("rotate:openai")["phase"] == rotate.PHASE_PROMOTING
    world.store.fail_put.clear()
    resumed = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert list(world.openai_keys.values()) == [new_value] and stored(world, "OPENAI_API_KEY") == new_value


def test_the_old_value_is_kept_as_previous_until_the_revoke_succeeds(world: World) -> None:
    world.fail_delete.add("key_old")
    rotate_now(world, "OPENAI_API_KEY")
    assert stored(world, "OPENAI_API_KEY.previous") == OLD["OPENAI_API_KEY"]
    world.fail_delete.clear()
    rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert stored(world, "OPENAI_API_KEY.previous") is None


# -- one run at a time --------------------------------------------------------------------------------------------


def hold_lock(world: World) -> int:
    lock = world.config.state_path.with_name(world.config.state_path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_second_run_is_refused_while_another_holds_the_lock(world: World) -> None:
    fd = hold_lock(world)
    try:
        refused = rotate_now(world, "OPENAI_API_KEY")
        refused_resume = rotate_now(world, "OPENAI_API_KEY", resume=True)
        calls = len(world.http.calls) + len(world.runner.calls)
        planned = run(world.ctx, only=[], apply=False, resume=False, as_json=False)
    finally:
        os.close(fd)
    for report in (refused, refused_resume):
        assert report.code == EXIT_FAILED and "another rotation is running" in report.text
    assert calls == 0
    assert planned.code == EXIT_OK  # a dry run changes nothing, so it needs no lock
    assert rotate_now(world, "OPENAI_API_KEY").code == EXIT_OK


def test_a_run_reads_the_state_again_once_it_holds_the_lock(world: World) -> None:
    other = State.load(world.config.state_path)
    other.set_provider("rotate:openai", phase=rotate.PHASE_PROMOTED, **{"old.key_id": "key_old"})
    other.save()
    report = rotate_now(world, "OPENAI_API_KEY", resume=True)
    assert report.code == EXIT_OK, report.text
    assert "key_old" not in world.openai_keys


# -- the gateway's old token must really be refused ---------------------------------------------------------------


def test_gateway_revoke_fails_loudly_while_the_gateway_still_accepts_the_old_token(world: World) -> None:
    world.gateway_keeps.add(OLD["REVIEW_GATEWAY_TOKEN"])
    report = rotate_now(world, "REVIEW_GATEWAY_TOKEN")
    assert report.code == EXIT_FAILED, report.text
    assert "still accepts the old token" in report.text and "rotate --resume" in report.text
    assert world.env["REVIEW_GATEWAY_TOKEN"] == NEW_GATEWAY and NEW_GATEWAY in world.accepted_gateway_tokens()
    assert world.sleeps == [rotators.REVOKE_WAIT_SECONDS] * (rotators.REVOKE_CHECKS - 1)
    world.gateway_keeps.clear()
    resumed = rotate_now(world, "REVIEW_GATEWAY_TOKEN", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert stored(world, "REVIEW_GATEWAY_TOKEN.previous") is None


def test_gateway_revoke_waits_for_the_gateway_to_drop_the_old_token(world: World) -> None:
    calls = {"count": 0}
    original = world.accepted_gateway_tokens

    def lagging() -> set[str]:
        calls["count"] += 1
        return original() | ({OLD["REVIEW_GATEWAY_TOKEN"]} if world.sleeps == [] else set())

    world.accepted_gateway_tokens = lagging  # type: ignore[method-assign]
    report = rotate_now(world, "REVIEW_GATEWAY_TOKEN")
    assert report.code == EXIT_OK, report.text
    assert world.sleeps == [rotators.REVOKE_WAIT_SECONDS]


# -- creation that fails after the provider made something -------------------------------------------------------


def test_an_unparseable_openai_reply_deletes_the_key_it_may_have_made(world: World) -> None:
    def keyless(call):  # noqa: ANN001
        world.openai_keys["key_orphan"] = "NEVER-SEEN-orphan-0000"
        return json_response(200, {"id": "key_orphan"})

    world.http.routes.insert(0, ("POST", re.compile(rf"{PROJECT}/service_accounts/sa_1/api_keys"), keyless))
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED, report.text
    assert "key_orphan" not in world.openai_keys and openai_old_works(world)
    assert world.state.provider("rotate:openai") == {}
    assert not [c for c in world.runner.calls if c.argv[:3] == ("gh", "secret", "set")]


def test_an_unparseable_tailscale_reply_deletes_the_client_it_may_have_made(world: World) -> None:
    def keyless(call):  # noqa: ANN001
        world.ts_clients["NEW-orphan-client"] = "NEVER-SEEN-orphan-0001"
        return json_response(200, {"id": "NEW-orphan-client"})

    world.http.routes.insert(0, ("POST", re.compile(rf"{TS}/tailnet/-/keys"), keyless))
    report = rotate_now(world, *TS_NAMES)
    assert report.code == EXIT_FAILED, report.text
    assert "NEW-orphan-client" not in world.ts_clients and ts_old_works(world)
    assert world.state.provider("rotate:tailscale") == {}


def test_an_orphan_that_cannot_be_deleted_is_recorded_for_resume(world: World) -> None:
    def keyless(call):  # noqa: ANN001
        world.openai_keys["key_orphan"] = "NEVER-SEEN-orphan-0002"
        return json_response(200, {"id": "key_orphan"})

    world.http.routes.insert(0, ("POST", re.compile(rf"{PROJECT}/service_accounts/sa_1/api_keys"), keyless))
    world.fail_delete.add("key_orphan")
    assert rotate_now(world, "OPENAI_API_KEY").code == EXIT_FAILED
    assert world.state.provider("rotate:openai")["new.key_id"] == "key_orphan"
    world.fail_delete.clear()
    assert rotate_now(world, "OPENAI_API_KEY", resume=True).code == EXIT_OK
    assert "key_orphan" not in world.openai_keys


def test_an_orphan_recorded_without_secret_names_is_still_deleted_on_resume(world: World) -> None:
    def keyless(call):  # noqa: ANN001
        world.openai_keys["key_orphan"] = "NEVER-SEEN-orphan-0003"
        return json_response(200, {"id": "key_orphan"})

    world.http.routes.insert(0, ("POST", re.compile(rf"{PROJECT}/service_accounts/sa_1/api_keys"), keyless))
    world.fail_delete.add("key_orphan")
    assert rotate_now(world, "OPENAI_API_KEY").code == EXIT_FAILED
    record = world.state.provider("rotate:openai")
    record.pop("names")
    world.state.set_provider("rotate:openai", **record)
    world.state.save()
    world.fail_delete.clear()
    assert rotate_now(world, "OPENAI_API_KEY", resume=True).code == EXIT_OK
    assert "key_orphan" not in world.openai_keys


# -- warnings, manual steps and checking before delivery ----------------------------------------------------------


def test_nothing_is_reported_as_revoked_when_no_old_id_was_recorded(world: World) -> None:
    world.store.delete("TS_OAUTH_CI_CLIENT_ID")
    world.state.clear_provider("tailscale")
    world.state.save()
    report = rotate_now(world, *TS_NAMES)
    assert report.code == EXIT_OK, report.text
    assert rotate.UNREVOKED in report.text and "nothing was revoked" in report.text
    assert OLD["TS_OAUTH_CI_CLIENT_ID"] in world.ts_clients


def test_the_adopted_pem_file_is_named_for_deletion(world: World) -> None:
    pem = add_new_pem(world)
    report = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert f"delete the adopted key file {pem}" in report.text


@pytest.mark.parametrize("which", ["openai", "gateway", "tailscale"])
def test_a_credential_that_fails_its_direct_check_never_reaches_an_environment(world: World, which: str) -> None:
    if which == "gateway":
        world.gateway_rejects.add(NEW_GATEWAY)
        names: tuple[str, ...] = ("REVIEW_GATEWAY_TOKEN",)
    else:
        world.reject_new = True
        names = ("OPENAI_API_KEY",) if which == "openai" else TS_NAMES
    report = rotate_now(world, *names)
    assert report.code == EXIT_FAILED
    sets = [c for c in world.runner.calls if c.argv[:3] == ("gh", "secret", "set")]
    assert [c for c in sets if c.stdin not in OLD.values()] == []


def test_a_resumed_app_key_rotation_still_names_the_pem_for_deletion(world: World) -> None:
    pem = add_new_pem(world)
    world.store.fail_delete.add("REVIEW_APP_PRIVATE_KEY.pending")
    first = rotate_now(world, "REVIEW_APP_PRIVATE_KEY")
    assert first.code == EXIT_FAILED
    assert world.state.provider("rotate:app_key")["new.pem"] == str(pem)
    world.store.fail_delete.clear()
    resumed = rotate_now(world, "REVIEW_APP_PRIVATE_KEY", resume=True)
    assert resumed.code == EXIT_OK, resumed.text
    assert f"delete the adopted key file {pem}" in resumed.text
    assert "delete the old private key in the example-app GitHub App settings" in resumed.text


# -- hosts without fcntl, dry-run wording, and unrestricted environments ---------------------------------------


def test_the_cli_imports_without_fcntl_and_apply_is_refused_there(world: World) -> None:
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        "import theseus_reviewer.cli, theseus_reviewer.infra.rotate_command as r\n"
        "from pathlib import Path\n"
        "try:\n"
        "    with r._locked(Path(sys.argv[1]) / 's.json'): pass\n"
        "except r.RotateUnavailableError as e: print('refused', e)\n"
    )
    done = subprocess.run([sys.executable, "-c", code, str(world.tmp)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "refused rotate --apply needs POSIX file locking" in done.stdout


def test_a_dry_run_says_a_promoting_record_resumes_forward(world: World) -> None:
    world.state.set_provider(
        "rotate:openai", phase=rotate.PHASE_PROMOTING, names="OPENAI_API_KEY", **{"new.key_id": "key_new_1"}
    )
    world.state.save()
    text = run(world.ctx, only=["OPENAI_API_KEY"], apply=False, resume=False, as_json=False).text
    assert "phase promoting" in text and "finish the promotion forward" in text
    assert "roll the interrupted rotation back" not in text


def mutating_calls(world: World) -> list[object]:
    return [
        *world.http.mutating(),
        *[
            c
            for c in world.runner.calls
            if c.argv[:3] == ("gh", "secret", "set") or c.argv[:4] == ("gh", "api", "-X", "PUT")
        ],
    ]


def test_apply_refuses_an_unrestricted_environment_before_any_call(world: World) -> None:
    world.environments["review"] = {"deployment_branch_policy": None}
    report = rotate_now(world)
    assert report.code == EXIT_FAILED and "allows deployments from every branch" in report.text
    assert mutating_calls(world) == []
    assert not [c for c in world.runner.calls if c.argv[0] in ("curl", "openssl")]
    assert world.openai_keys == {"key_old": OLD["OPENAI_API_KEY"]}


def test_apply_refuses_a_recorded_environment_that_is_unrestricted_or_foreign(world: World) -> None:
    world.state.mark_delivered("OPENAI_API_KEY", "Example-Org/example:staging")
    world.state.save()
    world.environments["staging"] = {"deployment_branch_policy": None}
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED and "Example-Org/example:staging" in report.text
    assert mutating_calls(world) == []
    world.environments["staging"] = world.environments["review"]
    world.state.mark_delivered("OPENAI_API_KEY", "Other-Org/other:review")
    world.state.save()
    foreign = rotate_now(world, "OPENAI_API_KEY")
    assert foreign.code == EXIT_FAILED and "not an environment of Example-Org/example" in foreign.text
    assert mutating_calls(world) == []


def test_the_setter_backstop_refuses_if_the_environment_opens_mid_run(world: World) -> None:
    world.open_after_reads = 1  # the pre-check passes, then the environment is opened to every branch
    report = rotate_now(world, "OPENAI_API_KEY")
    assert report.code == EXIT_FAILED
    assert "allows deployments from every branch" in report.text
    assert [
        c for c in world.runner.calls if c.argv[:3] == ("gh", "secret", "set") and c.stdin not in OLD.values()
    ] == []
    assert world.env["OPENAI_API_KEY"] == OLD["OPENAI_API_KEY"]
    assert openai_old_works(world)


# -- a plain store secret is owner-seeded and never leaves the host ------------------------------------------------


def with_plain_store_secret(world: World, name: str, value: str) -> None:
    import dataclasses

    from theseus_reviewer.infra.config import STORE, SecretSpec

    spec = SecretSpec(name, STORE, rotator="owner", max_age_days=30)
    world.ctx.config = dataclasses.replace(world.ctx.config, secrets=(*world.ctx.config.secrets, spec))
    world.store.put(name, Secret(value))


def test_a_plain_store_secret_is_never_adopted_checked_or_sent(world: World) -> None:
    secret = "PLAIN-gateway-url-secret-nnnnnnnn"
    with_plain_store_secret(world, "PLAIN_TOKEN", secret)
    world.store.put("PLAIN_TOKEN.next", Secret("PLAIN-next-value-oooooooo"))
    report = rotate_now(world, "PLAIN_TOKEN")
    assert report.code == EXIT_OK, report.text
    assert "unchanged" in report.text and "owner-seeded" in report.text
    assert "theseus infra seed PLAIN_TOKEN" in report.text and "days old" in report.text
    assert "Typesafe" not in report.text
    assert world.http.calls == []
    assert [c for c in world.runner.calls if c.argv[0] != "gh"] == []
    assert stored(world, "PLAIN_TOKEN") == secret and stored(world, "PLAIN_TOKEN.next") == "PLAIN-next-value-oooooooo"
    assert "PLAIN_TOKEN" not in world.env
    assert [r.kind for r in rotate_command.select(world.ctx, ["PLAIN_TOKEN"])] == ["owner-seeded"]
    planned = run(world.ctx, only=["PLAIN_TOKEN"], apply=False, resume=False, as_json=False)
    assert "owner-seeded" in planned.text and "Typesafe" not in planned.text


def test_without_only_a_plain_store_secret_sends_nothing_to_typesafe(world: World) -> None:
    secret = "PLAIN-gateway-url-secret-nnnnnnnn"
    with_plain_store_secret(world, "PLAIN_TOKEN", secret)
    world.store.put("PLAIN_TOKEN.next", Secret("PLAIN-next-value-oooooooo"))
    rotate_now(world)
    assert not [c for c in world.http.calls if secret in str(c.headers) or secret in str(c.body)]
    assert not [c for c in world.http.calls if "PLAIN-next-value-oooooooo" in str(c.headers)]
    assert stored(world, "PLAIN_TOKEN") == secret


def test_an_owner_secret_reports_leftover_rotation_items(world: World) -> None:
    with_plain_store_secret(world, "PLAIN_TOKEN", "PLAIN-gateway-url-secret-nnnnnnnn")
    world.store.put("PLAIN_TOKEN.pending", Secret("PLAIN-pending-value-pppppppp"))
    world.store.put("PLAIN_TOKEN.previous", Secret("PLAIN-previous-value-qqqqqqqq"))
    report = rotate_now(world, "PLAIN_TOKEN")
    assert "leftover item PLAIN_TOKEN.pending" in report.text and "leftover item PLAIN_TOKEN.previous" in report.text
    assert "PLAIN-pending-value-pppppppp" not in report.text


def _v0214_record(world: World, phase: str) -> None:
    """A state file as v0.2.14 wrote it: keyed by kind, with no secret names."""
    world.state.set_provider("rotate:typesafe", phase=phase)
    world.state.save()


def test_a_v0214_typesafe_record_for_a_now_owner_secret_is_stale_and_never_resumed(world: World) -> None:
    import dataclasses

    from theseus_reviewer.infra.config import STORE, SecretSpec

    specs = tuple(s for s in world.ctx.config.secrets if s.name != "TYPESAFE_API_KEY")
    owner = SecretSpec("TYPESAFE_API_KEY", STORE, rotator="owner", max_age_days=90)
    world.ctx.config = dataclasses.replace(world.ctx.config, secrets=(*specs, owner))
    world.store.put("TYPESAFE_API_KEY.pending", Secret("PENDING-typesafe-key-rrrrrrrr"))
    _v0214_record(world, rotate.PHASE_PROMOTING)
    for resume in (False, True):
        report = rotate_now(world, resume=resume)
        assert report.code == EXIT_FAILED, report.text
        assert "stale" in report.text and "unfinished typesafe rotation" in report.text
        assert "leftover item TYPESAFE_API_KEY.pending" in report.text
    assert world.state.provider("rotate:typesafe") == {"phase": rotate.PHASE_PROMOTING}
    assert world.env["TYPESAFE_API_KEY"] == OLD["TYPESAFE_API_KEY"]
    assert [c for c in world.http.calls if "typesafe" in c.url] == []


def test_a_record_for_another_secret_is_not_taken_over_by_a_typesafe_rotator(world: World) -> None:
    world.state.set_provider("rotate:typesafe", phase=rotate.PHASE_PROMOTING, names="SOME_OTHER_KEY")
    world.state.save()
    report = rotate_now(world, "TYPESAFE_API_KEY", resume=True)
    assert report.code == EXIT_FAILED and "SOME_OTHER_KEY" in report.text and "stale" in report.text
    assert world.state.provider("rotate:typesafe")["names"] == "SOME_OTHER_KEY"
    assert world.env["TYPESAFE_API_KEY"] == OLD["TYPESAFE_API_KEY"]


def test_a_v0214_typesafe_record_with_leftover_items_still_resumes_for_a_typesafe_secret(world: World) -> None:
    world.store.put("TYPESAFE_API_KEY.pending", Secret(NEW_TYPESAFE))
    _v0214_record(world, rotate.PHASE_PROMOTING)
    report = rotate_now(world, "TYPESAFE_API_KEY", resume=True)
    assert report.code == EXIT_OK, report.text
    assert stored(world, "TYPESAFE_API_KEY") == NEW_TYPESAFE


def test_the_loader_refuses_a_store_secret_without_a_rotator_and_builds_the_right_rotators(tmp_path: Path) -> None:
    import copy

    from theseus_reviewer.infra.config import ConfigError, parse_infra

    from .example import EXAMPLE

    data = copy.deepcopy(EXAMPLE)
    data["secrets"]["GATEWAY_URL_COPY"] = {"source": "store", "rotator": "owner"}
    config = parse_infra(data, tmp_path / ".github" / "review.yml")
    from theseus_reviewer.infra.plan import Context
    from theseus_reviewer.infra.rotators import OwnerSeededRotator, TypesafeRotator, build_rotators

    ctx = Context(config, None, None, None, None, None)  # type: ignore[arg-type]
    built = {name: type(r) for r in build_rotators(ctx, config) for name in r.names}
    assert built["GATEWAY_URL_COPY"] is OwnerSeededRotator and built["TYPESAFE_API_KEY"] is TypesafeRotator
    del data["secrets"]["GATEWAY_URL_COPY"]["rotator"]
    with pytest.raises(ConfigError, match="needs rotator: typesafe"):
        parse_infra(data, tmp_path / ".github" / "review.yml")


def test_rotate_reads_the_environment_through_the_shared_helper_and_refuses_when_unreadable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from theseus_reviewer.infra import github_env

    asked: list[str | None] = []

    def unreadable(ctx: object, environment: str | None = None) -> None:
        asked.append(environment)
        raise github_env.GithubError("boom")

    monkeypatch.setattr(github_env, "get_environment", unreadable)
    report = rotate_now(world)
    assert report.code == EXIT_FAILED and "cannot read environment" in report.text
    assert asked and mutating_calls(world) == []


def test_an_empty_environment_body_is_unreadable_not_open(world: World) -> None:
    world.environments["review"] = {}
    report = rotate_now(world)
    assert report.code == EXIT_FAILED and "cannot read environment" in report.text
    assert "every branch" not in report.text and mutating_calls(world) == []
