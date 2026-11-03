from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fake_github import APP_TOKEN
from .harness import CHECKER, POLICY, Harness
from .pins import (
    GATE_VERSION,
    HOLD_DESCRIPTION,
    LOCK_PATH,
    PASS_DESCRIPTION,
    REVIEWER_VERSION,
    PinWorld,
)
from .runs import ReviewRun

pytestmark = [
    pytest.mark.integration,
    pytest.mark.medium,
    pytest.mark.skipif(
        sys.platform != "linux" and sys.platform != "darwin", reason="the installer launches POSIX shebangs"
    ),
]

FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[PinWorld]:
    built = PinWorld(tmp_path / "pins")
    built.workdir.mkdir()
    try:
        yield built
    finally:
        built.close()


def _review(harness: Harness, world: PinWorld, tip_lock: str) -> tuple[ReviewRun, str]:
    harness.write({LOCK_PATH: world.reviewer_lock}, "pin the reviewer")
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(41, "feat/checker")
    harness.script_omp([{"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}])

    def advance_the_gated_branch() -> None:
        harness.write({LOCK_PATH: tip_lock}, "bump the gate pin")

    run = harness.review(41, pins=world.pins, before_gate=advance_the_gated_branch)
    return run, head


def test_the_gate_decision_follows_the_tip_pin_b_while_the_reviewer_ran_pin_a(
    harness: Harness, world: PinWorld
) -> None:
    run, head = _review(harness, world, world.gate_lock("hold"))

    assert run.steps["reviewer_pin"].code == 0, run.steps["reviewer_pin"].stderr
    assert run.steps["gate_pin"].code == 0, run.steps["gate_pin"].stderr
    assert [item["state"] for item in harness.github.oracle_statuses(head)] == ["success"]
    gate = harness.github.gate_statuses(head)
    assert [(item["state"], item["description"]) for item in gate] == [("failure", HOLD_DESCRIPTION)]
    assert gate[0]["creator"]["login"] == "benchbox-oracle[bot]"
    assert run.steps["publish"].code == 0, run.steps["publish"].stderr
    assert "gate_failed" not in run.steps["publish"].outputs
    assert "gate_failure" not in run.steps


def test_the_reviewer_and_the_gate_are_separate_verified_installs_with_their_own_pins(
    harness: Harness, world: PinWorld
) -> None:
    run, _ = _review(harness, world, world.gate_lock("pass"))

    reviewer_cache = run.temp / "theseus-reviewer-install"
    gate_cache = run.temp / "theseus-gate-install"
    gate_command = Path(run.steps["gate_pin"].outputs["command"])
    reviewer_launcher = next(reviewer_cache.glob("installs/*/tree/bin/theseus"))
    assert gate_command.is_file() and gate_cache in gate_command.parents
    assert reviewer_launcher != gate_command and reviewer_cache not in gate_command.parents
    assert [path.name for path in reviewer_cache.glob("installs/*/tree/site/theseus_reviewer-*.dist-info")] == [
        f"theseus_reviewer-{REVIEWER_VERSION}.dist-info"
    ]
    assert [path.name for path in gate_cache.glob("installs/*/tree/site/theseus_reviewer-*.dist-info")] == [
        f"theseus_reviewer-{GATE_VERSION}.dist-info"
    ]
    record = world.recorded()
    assert record is not None
    assert record["pin"] == "B"
    assert record["isolated"] == 1 and record["no_site"] == 1
    assert record["modules"] == ["theseus_reviewer", "theseus_reviewer.main"]
    assert Path(str(record["module_file"])).is_relative_to(gate_cache)
    assert record["yaml_importable"] is False


def test_the_gate_subprocess_gets_the_current_policy_root_the_head_and_only_the_app_token(
    harness: Harness, world: PinWorld
) -> None:
    run, head = _review(harness, world, world.gate_lock("pass"))

    record = world.recorded()
    assert record is not None
    assert record["argv"] == [
        "gate",
        "--repo",
        "BenchBox-dev/BenchBox",
        "--pr",
        "41",
        "--head-sha",
        head,
        "--policy",
        POLICY,
        "--root",
        str(run.temp / "gate"),
    ]
    assert record["token"] == APP_TOKEN
    assert [(item["state"], item["description"]) for item in harness.github.gate_statuses(head)] == [
        ("success", PASS_DESCRIPTION)
    ]


def test_a_failing_gate_subprocess_records_the_failure_posts_no_gate_and_retries_through_the_dispatch(
    harness: Harness, world: PinWorld
) -> None:
    run, head = _review(harness, world, world.gate_lock("crash"))

    publish = run.steps["publish"]
    assert publish.code == 0
    assert "::warning::oracle-gate was not posted from this run: could not post oracle-gate for #41" in publish.stderr
    assert "exit 1: gate pin B crashed" in publish.stderr
    assert publish.outputs["gate_failed"] == "true"
    assert publish.outputs["state_recorded"] == "true"
    assert [item["state"] for item in harness.github.oracle_statuses(head)] == ["success"]
    assert harness.github.gate_statuses(head) == []
    assert run.steps["gate_failure"].code == 1
    assert harness.github.dispatches == [
        {"workflow": "oracle-gate.yml", "ref": "develop", "inputs": {"pr": "41", "head_sha": head}}
    ]
    assert any(item["name"] == "oracle-verdict-state-41" for item in harness.github.artifacts)


def test_an_unresolvable_gate_pin_fails_closed_instead_of_falling_back_to_the_reviewer_interpreter(
    harness: Harness, world: PinWorld
) -> None:
    run, head = _review(harness, world, '{"schema": 1}')

    assert run.steps["gate_pin"].code != 0
    assert "refused" in run.steps["gate_pin"].stderr
    publish = run.steps["publish"]
    assert publish.code == 0
    assert "THESEUS_GATE_COMMAND is set but empty" in publish.stderr
    assert publish.outputs["gate_failed"] == "true"
    assert [item["state"] for item in harness.github.oracle_statuses(head)] == ["success"]
    assert harness.github.gate_statuses(head) == []
    assert world.recorded() is None
    assert run.steps["gate_failure"].code == 1
