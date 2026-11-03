from __future__ import annotations

import sys
from pathlib import Path

import pytest

from theseus_reviewer import bootstrap

from . import harness as harness_module
from .harness import CHECKER, Harness
from .pins import PinWorld, support

pytestmark = [
    pytest.mark.integration,
    pytest.mark.medium,
    pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="the installer launches POSIX shebangs"),
]

CLEAN = "def checker(a, b):\n    return a == b and a is not None\n"


@pytest.fixture
def world(tmp_path: Path):
    built = PinWorld(tmp_path / "pins")
    built.workdir.mkdir()
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def launcher(world: PinWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "consumer"
    commit = support.commit_lock(repo, world.reviewer_lock)
    monkeypatch.setenv("ORACLE_PROMOTION_MIRROR", world.mirror.origin)
    lock = bootstrap.read_lock(repo, commit)
    assert lock.dependencies == ()
    return bootstrap.install(lock, tmp_path / "install-cache", bootstrap.default_fetch()).executable


def _ship() -> list[dict]:
    return [{"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}]


def test_local_review_runs_through_a_real_bootstrap_launcher(
    harness: Harness, launcher: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness_module, "PACKAGE", [str(launcher)])
    harness.branch("feat/local", {CHECKER: CLEAN}, "reject None")
    harness.script_omp(_ship(), "OMP_GATEWAY_TOKEN")

    review = harness.local_review("feat/local")

    assert review.code == 0, review.stdout + review.stderr
    assert "Local oracle review: no Critical or High findings; pr-open may proceed." in review.stdout
    assert "No module named" not in review.stderr


def test_an_ambient_theseus_reviewer_is_never_imported_by_a_phase(
    harness: Harness, launcher: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness_module, "PACKAGE", [str(launcher)])
    marker = tmp_path / "ambient-imported"
    ambient = tmp_path / "ambient"
    (ambient / "theseus_reviewer").mkdir(parents=True)
    (ambient / "theseus_reviewer" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8"
    )
    (ambient / "theseus_reviewer" / "main.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(99)\n", encoding="utf-8"
    )
    harness.branch("feat/local", {CHECKER: CLEAN}, "reject None")
    harness.script_omp(_ship(), "OMP_GATEWAY_TOKEN")

    review = harness.local_review("feat/local", env={"PYTHONPATH": str(ambient), "PYTHONSTARTUP": str(ambient)})

    assert review.code == 0, review.stdout + review.stderr
    assert not marker.exists()


def test_the_phase_command_pins_the_running_package_and_drops_python_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from theseus_reviewer import selfexec

    command = selfexec.package_command()
    assert command[1:5] == ["-I", "-B", "-S", "-c"]
    assert command[-1] == str(Path(selfexec.__file__).resolve().parent.parent)
    kept = selfexec.scrubbed_environment({"PYTHONPATH": "/x", "pythonhome": "/y", "PATH": "/bin", "HOME": "/h"})
    assert kept == {"PATH": "/bin", "HOME": "/h"}
