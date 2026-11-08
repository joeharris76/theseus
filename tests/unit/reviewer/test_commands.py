from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from theseus_reviewer import omp_pin
from theseus_reviewer._vendor import yaml
from theseus_reviewer.commands import OMP_OVERLAY_TEXT, build
from theseus_reviewer.policy import Policy

pytestmark = [pytest.mark.unit, pytest.mark.fast]

WS = Path("/work/space")
PROMPT = "review this"


@pytest.fixture(autouse=True)
def pinned_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(omp_pin.BINARY_ENV, sys.executable)


def test_build_dispatches_omp_reviewer_with_selector_and_empty_effort(policy: Policy, tmp_path: Path) -> None:
    reviewer = policy.reviewers["small"]
    invocation = build(reviewer, WS, PROMPT, tmp_path)
    assert invocation.argv[0] == sys.executable
    assert invocation.argv[invocation.argv.index("--model") + 1] == reviewer.model
    assert "--thinking" not in invocation.argv
    assert PROMPT not in invocation.argv
    assert invocation.stdin_path is not None
    assert invocation.stdin_path.read_text(encoding="utf-8") == PROMPT


def test_build_passes_explicit_effort_through(policy: Policy, tmp_path: Path) -> None:
    reviewer = replace(policy.reviewers["small"], effort="low")
    invocation = build(reviewer, WS, PROMPT, tmp_path)
    assert invocation.argv[invocation.argv.index("--thinking") + 1] == "low"
    assert PROMPT not in invocation.argv
    assert invocation.stdin_path is not None
    assert invocation.stdin_path.read_text(encoding="utf-8") == PROMPT


def test_build_rejects_invalid_effort(policy: Policy, tmp_path: Path) -> None:
    reviewer = replace(policy.reviewers["small"], effort="turbo")
    with pytest.raises(ValueError, match="thinking level"):
        build(reviewer, WS, PROMPT, tmp_path)


def test_build_rejects_unknown_harness(policy: Policy, tmp_path: Path) -> None:
    reviewer = replace(policy.reviewers["small"], harness="codex")
    with pytest.raises(ValueError, match="unknown harness"):
        build(reviewer, WS, PROMPT, tmp_path)


def test_overlay_disables_compaction() -> None:
    compaction = yaml.safe_load(OMP_OVERLAY_TEXT)["compaction"]
    assert compaction == {"enabled": False, "midTurnEnabled": False}
