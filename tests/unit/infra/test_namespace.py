from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from theseus_reviewer.cli import main
from theseus_reviewer.infra import cli as infra_cli
from theseus_reviewer.infra import github_env
from theseus_reviewer.infra.plan import open_context
from theseus_reviewer.infra.store import FileStore, KeychainStore, ScopedStore, Secret, StoreError

from .fakes import FakeSecurity
from .world import NOW, World, make_world, scoped_store

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ROOTS = ["TAILSCALE_ROOT_CLIENT_ID", "TAILSCALE_ROOT_CLIENT_SECRET", "OPENAI_ADMIN_KEY"]


@pytest.fixture(params=["file", "keychain"])
def base(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    return FileStore(tmp_path / "shared") if request.param == "file" else KeychainStore(FakeSecurity())


def test_two_repositories_sharing_one_store_never_see_each_others_values(base) -> None:  # type: ignore[no-untyped-def]
    one = ScopedStore(base, "Theseus-Org/theseus", "theseus")
    two = ScopedStore(base, "BenchBox-dev/BenchBox", "review")
    names = [
        "REVIEW_APP_ID",
        "REVIEW_APP_PRIVATE_KEY",
        "TS_OAUTH_CI_SECRET",
        "TYPESAFE_API_KEY",
        "TYPESAFE_API_KEY.next",
    ]
    for name in names:
        one.put(name, Secret(f"one-{name}-value"))
    assert [two.get(name) for name in names] == [None] * len(names)
    for name in names:
        two.put(name, Secret(f"two-{name}-value"))
    for name in names:
        assert one.get(name) == Secret(f"one-{name}-value")
        assert two.get(name) == Secret(f"two-{name}-value")
    one.put("REVIEW_APP_PRIVATE_KEY", Secret("x" * 9000))  # parts must be scoped too
    assert two.get("REVIEW_APP_PRIVATE_KEY") == Secret("two-REVIEW_APP_PRIVATE_KEY-value")
    one.delete("TS_OAUTH_CI_SECRET")
    assert two.get("TS_OAUTH_CI_SECRET") == Secret("two-TS_OAUTH_CI_SECRET-value")
    assert sorted(two.names()) == sorted(names)
    assert sorted(one.names()) == sorted(n for n in names if n != "TS_OAUTH_CI_SECRET")


def test_root_credentials_are_shared_and_unprefixed(base) -> None:  # type: ignore[no-untyped-def]
    one = ScopedStore(base, "a/one", "env")
    two = ScopedStore(base, "b/two", "env")
    for name in ROOTS:
        one.put(name, Secret(f"root-{name}"))
        assert two.get(name) == Secret(f"root-{name}")
        assert base.get(name) == Secret(f"root-{name}")
    assert sorted(two.names()) == sorted(ROOTS)


def test_the_same_repository_in_two_environments_is_separate(base) -> None:  # type: ignore[no-untyped-def]
    ScopedStore(base, "a/one", "review").put("REVIEW_APP_ID", Secret("review-value"))
    assert ScopedStore(base, "a/one", "staging").get("REVIEW_APP_ID") is None


def test_an_unprefixed_leftover_is_refused_by_name_and_never_read_as_scoped(base) -> None:  # type: ignore[no-untyped-def]
    base.put("REVIEW_APP_ID", Secret("old-unprefixed"))
    scoped = ScopedStore(base, "a/one", "env")
    assert scoped.get("REVIEW_APP_ID") is None
    with pytest.raises(StoreError, match="unprefixed item REVIEW_APP_ID"):
        scoped.check_unscoped(["REVIEW_APP_ID"])
    base.delete("REVIEW_APP_ID")
    base.put("TYPESAFE_API_KEY.next", Secret("old-next"))
    with pytest.raises(StoreError, match="TYPESAFE_API_KEY.next"):
        scoped.check_unscoped(["TYPESAFE_API_KEY"])
    scoped.check_unscoped(["SOMETHING_ELSE"])
    base.put("OPENAI_ADMIN_KEY", Secret("root-value"))
    base.delete("TYPESAFE_API_KEY.next")
    scoped.check_unscoped(["OPENAI_ADMIN_KEY"])


def test_opening_a_context_refuses_an_unprefixed_leftover(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.context()
    FileStore(Path(world.infra["store"]["dir"])).put("REVIEW_GATEWAY_TOKEN", Secret("old-unprefixed"))
    with pytest.raises(StoreError, match="unprefixed item REVIEW_GATEWAY_TOKEN"):
        open_context(world.policy_path, world.http, world.run, lambda: NOW)


def test_two_configs_over_one_directory_keep_separate_values(tmp_path: Path) -> None:
    first = make_world(tmp_path / "a")
    second = make_world(tmp_path / "b")
    shared = str(tmp_path / "shared-store")
    for world, repo in ((first, "Example-Org/example"), (second, "Other-Org/other")):
        world.infra["store"]["dir"] = shared
        world.infra["github"]["repository"] = repo
    a, b = first.context(), second.context()
    a.keep("REVIEW_APP_ID", Secret("123456"))
    a.keep("TYPESAFE_API_KEY", Secret("SENTINEL-typesafe-A"))
    assert b.secret("TYPESAFE_API_KEY") is None and b.secret("REVIEW_APP_ID") is None
    b.keep("TYPESAFE_API_KEY", Secret("SENTINEL-typesafe-B"))
    assert a.secret("TYPESAFE_API_KEY") == Secret("SENTINEL-typesafe-A")
    assert b.secret("TYPESAFE_API_KEY") == Secret("SENTINEL-typesafe-B")


def _seed(world: World, monkeypatch, capsys, name: str, value: str) -> int:  # type: ignore[no-untyped-def]
    deps = infra_cli.Dependencies(http=world.http, run=world.run, now=lambda: NOW, stdin=io.StringIO(value))
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", deps)
    world.context()
    code = main(["infra", "seed", name, "--config", str(world.policy_path)])
    capsys.readouterr()
    return code


def test_seed_writes_the_configs_account_and_roots_to_the_shared_one(world_dir, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    world = world_dir
    store_dir = Path(world.infra["store"]["dir"])
    assert _seed(world, monkeypatch, capsys, "TYPESAFE_API_KEY", "SENTINEL-seed-0001") == 0
    assert _seed(world, monkeypatch, capsys, "OPENAI_ADMIN_KEY", "SENTINEL-seed-0002") == 0
    assert _seed(world, monkeypatch, capsys, "TYPESAFE_API_KEY.next", "SENTINEL-seed-0003") == 0
    files = sorted(entry.name for entry in store_dir.iterdir())
    assert files == [
        "Example-Org%2Fexample%3Areview%3ATYPESAFE_API_KEY",
        "Example-Org%2Fexample%3Areview%3ATYPESAFE_API_KEY.next",
        "OPENAI_ADMIN_KEY",
    ]
    assert scoped_store(world).get("TYPESAFE_API_KEY") == Secret("SENTINEL-seed-0001")


@pytest.fixture
def world_dir(tmp_path: Path) -> World:
    return make_world(tmp_path)


# --- a policy that matches every branch is not a restriction ---------------------------------------------------


def _env(world: World, policies: list[str]) -> dict:  # type: ignore[type-arg]
    world.environment = {
        "name": "review",
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }
    world.policies = policies
    return world.environment


@pytest.mark.parametrize(
    ("policies", "restricted"),
    [
        (["develop"], True),
        (["develop", "release/*"], True),
        ([], True),
        (["*"], False),
        (["**"], False),
        (["develop", "*"], False),
    ],
)
def test_a_wildcard_branch_policy_is_unrestricted(world_dir: World, policies: list[str], restricted: bool) -> None:
    ctx = world_dir.context()
    environment = _env(world_dir, policies)
    assert github_env.is_restricted(ctx, environment) is restricted


def test_protected_branches_stay_restricted_and_no_policy_is_open(world_dir: World) -> None:
    ctx = world_dir.context()
    assert github_env.is_restricted(ctx, {"deployment_branch_policy": {"protected_branches": True}}) is True
    assert github_env.is_restricted(ctx, {"deployment_branch_policy": None}) is False
    assert github_env.is_restricted(ctx, None) is False


def test_status_and_delivery_treat_a_wildcard_policy_as_open(world_dir: World, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    world = world_dir
    ctx = world.context()
    world.seed_roots(ctx)
    world.script(ctx.config)
    _env(world, ["*"])
    deps = infra_cli.Dependencies(http=world.http, run=world.run, now=lambda: NOW, stdin=io.StringIO(""))
    monkeypatch.setattr(infra_cli, "DEPENDENCIES", deps)
    code = main(["infra", "status", "--config", str(world.policy_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["environment_restricted"] is False
    code = main(["infra", "setup", "--apply", "--only", "TYPESAFE_API_KEY", "--config", str(world.policy_path)])
    assert code == 3 and world.secrets == {}
