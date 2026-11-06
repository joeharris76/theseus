from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fcntl")

from theseus_reviewer import capacity, cli, omp_pin, preflight, promotion

from .test_local_review import (
    POLICY_RELATIVE,
    _git,
    _member_repo,
    home,  # noqa: F401
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

WINDOWS = {
    "anthropic/claude-sonnet-5-5": 872_000,
    "anthropic/claude-opus-5-5": 872_000,
    "anthropic/claude-fable-5-1": 872_000,
    "openai-codex/gpt-6-luna": 144_000,
    "openai-codex/gpt-6.1-sol": 372_000,
    "openai-codex/gpt-6-astra": 144_000,
}
SMALL_WINDOWS = {model: 50_000 for model in WINDOWS}


@pytest.fixture
def binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "omp"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setenv(omp_pin.BINARY_ENV, str(path))
    return path


def _args(repo: Path, **overrides: Any) -> argparse.Namespace:
    values = {
        "policy": POLICY_RELATIVE,
        "repo": "owner/repo",
        "base_ref": "develop",
        "trusted_ref": "",
        "head": "HEAD",
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _reader(windows: dict[str, int]) -> Any:
    return lambda binary, text: dict(windows)


def _run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], windows: Any, **overrides: Any
) -> tuple[int, dict[str, Any]]:
    monkeypatch.chdir(repo)
    code = preflight.command_preflight(_args(repo, **overrides), windows)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_small_change_is_reviewable_and_prints_the_schema(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader(WINDOWS))
    assert code == preflight.EXIT_REVIEWABLE
    assert set(report) == {"schema", "reviewable", "reasons", "advisories"}
    assert report["schema"] == 1 and report["reviewable"] is True and report["reasons"] == []


def test_advisories_name_the_reviewers_that_cannot_hold_the_change(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader({**WINDOWS, "openai-codex/gpt-6-luna": 50_000}))
    assert code == preflight.EXIT_REVIEWABLE
    assert any(capacity.SKIP_PREFIX in note for note in report["advisories"])


def test_a_change_that_carries_a_promotion_is_refused_with_the_split_instruction(
    home: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    (repo / promotion.LOCK_PATH).write_text("{}\n", encoding="utf-8")
    _git(repo, "add", promotion.LOCK_PATH)
    _git(repo, "commit", "--quiet", "-m", "bump the lock")
    code, report = _run(repo, monkeypatch, capsys, _reader(WINDOWS))
    assert code == preflight.EXIT_NOT_REVIEWABLE
    assert report["reviewable"] is False
    assert report["reasons"] == ["contains a Theseus promotion; land the lock change as its own pull request"]


def test_a_change_above_every_reviewer_capacity_is_refused_with_its_token_need(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader(SMALL_WINDOWS))
    assert code == preflight.EXIT_NOT_REVIEWABLE
    assert report["reviewable"] is False
    (reason,) = report["reasons"]
    assert reason.startswith("needs about ") and reason.endswith(
        "tokens of reading, above every reviewer's capacity; split"
    )


def test_a_catalog_listing_none_of_the_reviewer_models_is_undetermined(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader({"apple/on-device": 8_192}))
    assert code == preflight.EXIT_UNDETERMINED
    assert report["reviewable"] is False
    assert report["reasons"] and all(capacity.UNKNOWN_PREFIX in reason for reason in report["reasons"])


def test_a_reviewer_whose_model_is_not_listed_is_an_advisory_when_another_fits(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    listed = {model: window for model, window in WINDOWS.items() if model != "openai-codex/gpt-6-luna"}
    code, report = _run(repo, monkeypatch, capsys, _reader(listed))
    assert code == preflight.EXIT_REVIEWABLE
    assert any(capacity.UNKNOWN_PREFIX in note for note in report["advisories"])


def test_a_diff_that_cannot_be_built_is_undetermined(
    home: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader(WINDOWS), base_ref="no-such-ref")
    assert code == preflight.EXIT_UNDETERMINED
    assert report["reviewable"] is False and report["reasons"][0].startswith("the diff cannot be built")


def test_an_unreadable_catalog_is_undetermined_not_reviewable(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")

    def broken(path: Path, text: str) -> dict[str, int]:
        raise capacity.CatalogError("the model catalog could not be listed: exit code 1")

    code, report = _run(repo, monkeypatch, capsys, broken)
    assert code == preflight.EXIT_UNDETERMINED
    assert report["reviewable"] is False and report["reasons"][0].startswith(preflight.CATALOG_REASON)


def test_the_preflight_command_is_registered_and_prints_text_without_json(
    home: Path,  # noqa: F811
    binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parsed = cli._parser().parse_args(["preflight", "--policy", POLICY_RELATIVE, "--base-ref", "develop"])
    assert parsed.json is False and parsed.head == "HEAD" and parsed.handler is preflight.command_preflight
    repo = _member_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert preflight.command_preflight(_args(repo, json=False), _reader(WINDOWS)) == preflight.EXIT_REVIEWABLE
    assert capsys.readouterr().out.startswith("reviewable")


def test_a_change_with_no_commits_is_reviewable_without_a_plan(
    home: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _member_repo(tmp_path / "repo")
    code, report = _run(repo, monkeypatch, capsys, _reader(WINDOWS), head="develop")
    assert code == preflight.EXIT_REVIEWABLE and report["advisories"] == ["no committed changes against develop"]
