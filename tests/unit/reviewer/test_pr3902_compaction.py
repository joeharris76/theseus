from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import absence, runner
from theseus_reviewer.commands import Invocation
from theseus_reviewer.diff import STAGED_DIFF_NAME
from theseus_reviewer.policy import Policy

pytestmark = [pytest.mark.unit, pytest.mark.medium]

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "replays" / "pr3902_compaction.json"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
COMPACTION_START = "auto_compaction_start"


def _spec() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _event(message: dict[str, Any]) -> str:
    return json.dumps({"type": "message_end", "message": message}) + "\n"


def _stream(spec: dict[str, Any], workspace: Path, *, compacted: bool) -> str:
    lines: list[str] = []
    examined: list[str] = []
    pending = [item for item in spec["compaction"] if compacted]
    for number, (name, reference, *selector) in enumerate(spec["calls"]):
        relative = spec["paths"][reference]
        path = str(workspace / relative) if relative else str(workspace)
        if name == "read" and relative and relative not in examined:
            examined.append(relative)
        arguments: dict[str, Any] = {"path": path + (f":{selector[0]}" if selector else "")}
        if name == "grep":
            arguments["pattern"] = "x"
        call = {"type": "toolCall", "id": f"call-{number}", "name": name, "arguments": arguments}
        lines.append(_event({"role": "assistant", "content": [call]}))
        lines.append(
            _event(
                {
                    "role": "toolResult",
                    "toolCallId": f"call-{number}",
                    "toolName": name,
                    "isError": False,
                    "content": [{"type": "text", "text": "x"}],
                    "details": {},
                }
            )
        )
        while pending and pending[0]["after_calls"] == number + 1 and number + 1 < len(spec["calls"]):
            lines.append(json.dumps(pending.pop(0)) + "\n")
    answer = {
        "status": spec["answer"]["status"],
        "incomplete_reason": "",
        "decision": spec["answer"]["decision"],
        "summary": "reviewed",
        "files_examined": examined,
        "defects": [],
        "prior_defects": [],
    }
    lines.append(_event({"role": "assistant", "content": [{"type": "text", "text": json.dumps(answer)}]}))
    lines.extend(json.dumps(item) + "\n" for item in pending)
    return "".join(lines)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    for arguments in (("init", "-q"), ("config", "user.name", "t"), ("config", "user.email", "t@example.com")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "head"], check=True)
    return root


def _judge(policy: Policy, workspace: Path, stdout: str) -> runner.ReviewOutcome:
    plan = {
        "brief_mode": "file-list",
        "coverage": 1,
        "evidence_files": [],
        "evidence_exempt_paths": [],
        "binary_files": [],
        "diff_inline": None,
        "max_defects": 10,
        "protocol": {},
    }
    return runner._judge(
        replace(policy.reviewers["huge"], harness="omp"),
        Invocation(argv=(), cwd=workspace, output_file=None),
        runner.RunResult(0, stdout, "", False),
        workspace=workspace,
        staged_before=runner._digest(workspace / STAGED_DIFF_NAME),
        now=NOW,
        plan=plan,
    )


def test_the_recorded_run_compacted_its_context_before_its_accepted_ship() -> None:
    spec = _spec()
    assert [item["type"] for item in spec["compaction"]][0] == COMPACTION_START
    assert spec["compaction"][0]["action"] == "remote"
    assert spec["answer"]["decision"] == "SHIP"


def test_pr3902_compacted_ship_is_refused(policy: Policy, workspace: Path) -> None:
    stdout = _stream(_spec(), workspace, compacted=True)
    assert COMPACTION_START in stdout
    outcome = _judge(policy, workspace, stdout)
    assert outcome.verdict is None
    assert outcome.missing is not None
    assert outcome.missing.kind == absence.INCOMPLETE
    assert outcome.missing.detail == "the reviewer's context was compacted"


def test_the_same_ship_without_the_compaction_event_is_accepted(policy: Policy, workspace: Path) -> None:
    stdout = _stream(_spec(), workspace, compacted=False)
    assert COMPACTION_START not in stdout
    outcome = _judge(policy, workspace, stdout)
    assert outcome.missing is None
    assert outcome.verdict is not None
    assert outcome.verdict["decision"] == "SHIP"
