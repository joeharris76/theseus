from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from theseus_reviewer import legacy

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "replays" / "pr3942_coverage.json"


def _tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            f"{legacy.STAGED_PROMOTION_DIR}/new/src/theseus_reviewer/cli.py",
            ".review-promotion/new/src/theseus_reviewer/cli.py",
        ),
        (f"{legacy.STAGED_PROMOTION_DIR}/upstream.diff", ".review-promotion/upstream.diff"),
        (legacy.STAGED_DIFF_NAME, ".review-pull-request.diff"),
        ("tests/fixtures/oracle_golden/carry.json", "tests/fixtures/review_golden/carry.json"),
        ("_project/scripts/oracle_reviewers/cli.py", "_project/scripts/reviewers/cli.py"),
        ("tests/unit/test_oracle_review_check.py", "tests/unit/test_review_decision.py"),
        ("tests/unit/test_review_check.py", "tests/unit/test_review_check.py"),
        (".github/oracle-reviewers.yml", ".github/review.yml"),
        (".github/workflows/oracle-verdict.yml", ".github/workflows/review-verdict.yml"),
        (".github/workflows/oracle-review.yml", ".github/workflows/review-request.yml"),
        ("_project/scripts/oracle_review_check.py", "_project/scripts/review_check.py"),
    ],
)
def test_the_replay_rename_map_gives_the_current_names(old: str, new: str) -> None:
    tool = _tool("pr3942_replay")
    assert tool.renamed(old) == new
    assert tool.renamed(new) == new


def test_check_reproduces_the_committed_fixture_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool = _tool("pr3942_replay")
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert tool.rename_fixture(committed) == committed
    monkeypatch.setattr(tool, "build", lambda args: committed)
    arguments = ["--benchbox-repo", str(tmp_path), "--theseus-repo", str(tmp_path), "--capture", str(tmp_path)]
    assert tool.main([*arguments, "--output", str(FIXTURE), "--check"]) == 0
    stale = tmp_path / "stale.json"
    stale.write_text(FIXTURE.read_text(encoding="utf-8").replace('"schema": 1', '"schema": 2'), encoding="utf-8")
    assert tool.main([*arguments, "--output", str(stale), "--check"]) == 1


def test_the_tools_name_the_real_capture_artifact_through_the_legacy_module(capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("pr3942_replay", "pr3902_compaction"):
        with pytest.raises(SystemExit):
            _tool(name).main(["--help"])
        assert legacy.VERDICT_CAPTURE_ARTIFACT in " ".join(capsys.readouterr().out.split())


@pytest.mark.skipif(
    not (os.environ.get("PR3942_CAPTURE") and os.environ.get("BENCHBOX_REPO")),
    reason="needs the BenchBox clone and the verdict capture of the recorded run",
)
def test_the_committed_fixture_is_rebuilt_from_the_real_inputs() -> None:
    tool = _tool("pr3942_replay")
    arguments = [
        "--benchbox-repo",
        os.environ["BENCHBOX_REPO"],
        "--theseus-repo",
        str(ROOT),
        "--capture",
        os.environ["PR3942_CAPTURE"],
        "--check",
    ]
    assert tool.main(arguments) == 0
