from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tools import seed_hash, upstream_ledger

pytestmark = [pytest.mark.unit, pytest.mark.fast]

WATCHED = "_project/scripts/oracle_gate.py"
PREFIXED = "_project/scripts/oracle_reviewers/brand_new.py"
UNWATCHED = "benchbox/core/other.py"
THESEUS_COMMIT = subprocess.run(
    ["git", "-C", str(upstream_ledger.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"change {path}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "benchbox"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=develop")
    _commit(root, "README.md", "base\n")
    return root


def _ledger(repo: Path, entries: list[dict] | None = None) -> dict:
    ledger = upstream_ledger.load_ledger(upstream_ledger.LEDGER_PATH)
    return {**ledger, "baseline": _git(repo, "rev-parse", "HEAD"), "entries": entries or []}


def test_the_recorded_ledger_names_the_seed_baseline_and_starts_without_entries() -> None:
    ledger = upstream_ledger.load_ledger(upstream_ledger.LEDGER_PATH)
    assert ledger["baseline"] == seed_hash.load_map()["baseline_commit"]
    assert ledger["entries"] == []


def test_a_range_without_inventory_changes_is_current(repo: Path) -> None:
    ledger = _ledger(repo)
    _commit(repo, UNWATCHED, "x = 1\n")

    report = upstream_ledger.evaluate(repo, ledger, "develop")

    assert report.ok and report.examined == 1 and report.commits == ()


@pytest.mark.parametrize("path", [WATCHED, PREFIXED, ".github/workflows/oracle-gate.yml", "scripts/pr_arm.py"])
def test_a_change_to_a_watched_path_is_pending_until_recorded(repo: Path, path: str) -> None:
    ledger = _ledger(repo)
    sha = _commit(repo, path, "changed\n")

    report = upstream_ledger.evaluate(repo, ledger, "develop")

    assert not report.ok
    assert [commit.sha for commit in report.pending] == [sha]
    assert report.commits[0].paths == (path,)


def test_a_noop_entry_with_a_rationale_clears_the_commit(repo: Path) -> None:
    ledger = _ledger(repo)
    sha = _commit(repo, WATCHED, "changed\n")
    ledger["entries"].append({"commit": sha, "disposition": "noop", "rationale": "comment-only change"})

    assert upstream_ledger.evaluate(repo, ledger, "develop").ok


def test_a_port_entry_needs_a_port_commit_in_this_repository_and_its_effects(repo: Path) -> None:
    ledger = _ledger(repo)
    sha = _commit(repo, WATCHED, "changed\n")
    ledger["entries"].append({"commit": sha, "disposition": "port", "port_commit": "f" * 40, "effects": "none"})
    assert any(
        "not in this repository" in problem for problem in upstream_ledger.evaluate(repo, ledger, "develop").problems
    )

    ledger["entries"][0] = {"commit": sha, "disposition": "port", "port_commit": THESEUS_COMMIT}
    assert any("effects" in problem for problem in upstream_ledger.evaluate(repo, ledger, "develop").problems)

    ledger["entries"][0] = {
        "commit": sha,
        "disposition": "port",
        "port_commit": THESEUS_COMMIT,
        "effects": "interface and cache unchanged",
    }
    assert upstream_ledger.evaluate(repo, ledger, "develop").ok


@pytest.mark.parametrize(
    "entry",
    [
        {"disposition": "noop"},
        {"disposition": "noop", "rationale": "  "},
        {"disposition": "pending"},
        {"disposition": "x"},
    ],
)
def test_an_incomplete_entry_never_clears_a_commit(repo: Path, entry: dict) -> None:
    ledger = _ledger(repo)
    sha = _commit(repo, WATCHED, "changed\n")
    ledger["entries"].append({"commit": sha, **entry})

    assert not upstream_ledger.evaluate(repo, ledger, "develop").ok


def test_an_entry_for_a_commit_outside_the_range_is_reported_as_stale(repo: Path) -> None:
    ledger = _ledger(repo, [{"commit": "a" * 40, "disposition": "noop", "rationale": "gone"}])

    report = upstream_ledger.evaluate(repo, ledger, "develop")

    assert not report.ok and any("does not match" in problem for problem in report.problems)


def test_a_ref_that_does_not_descend_from_the_baseline_is_an_error(repo: Path) -> None:
    ledger = _ledger(repo)
    _git(repo, "switch", "-q", "--orphan", "other")
    _commit(repo, "README.md", "unrelated\n")

    with pytest.raises(upstream_ledger.LedgerError, match="does not descend"):
        upstream_ledger.evaluate(repo, ledger, "other")


def test_refresh_records_pending_entries_and_the_check_then_names_them(repo: Path, tmp_path: Path) -> None:
    ledger = _ledger(repo)
    sha = _commit(repo, WATCHED, "changed\n")
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")

    assert (
        upstream_ledger.main(["refresh", "--benchbox-repo", str(repo), "--ref", "develop", "--ledger", str(path)]) == 1
    )

    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert [(entry["commit"], entry["disposition"]) for entry in entries] == [(sha, "pending")]
    assert entries[0]["paths"] == [WATCHED]


def test_the_check_exits_zero_only_when_current(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(_ledger(repo)), encoding="utf-8")
    arguments = ["check", "--benchbox-repo", str(repo), "--ref", "develop", "--ledger", str(path)]
    assert upstream_ledger.main(arguments) == 0
    _commit(repo, WATCHED, "changed\n")
    assert upstream_ledger.main(arguments) == 1
    assert "pending" in capsys.readouterr().out
