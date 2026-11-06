from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import absence, coverage, evidence, promotion, runner
from theseus_reviewer.commands import Invocation
from theseus_reviewer.diff import STAGED_DIFF_NAME, hunk_index
from theseus_reviewer.policy import Policy
from theseus_reviewer.verdict import Verdict

pytestmark = [pytest.mark.unit, pytest.mark.medium]

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "replays" / "pr3942_coverage.json"
PROMOTION_DIFF = f"{promotion.STAGED_PROMOTION_DIR}/{promotion.UPSTREAM_DIFF_NAME}"
DEFAULT_READ_LINES = 300
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CHUNK = re.compile(r"(?:(?P<start>\d+)(?:(?P<dash>-)(?P<end>\d+)?|\+(?P<count>\d+))?|-(?P<tail>\d+))")


def _spec() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _windows(selector: str | None, total: int) -> list[tuple[int, int, bool]]:
    chunks = [part for part in re.split(r"[:,]", selector or "") if part and part != "raw"]
    if not chunks:
        return [(1, DEFAULT_READ_LINES, True)]
    windows: list[tuple[int, int, bool]] = []
    for chunk in chunks:
        match = CHUNK.fullmatch(chunk)
        assert match is not None, chunk
        if match["tail"]:
            windows.append((max(total - int(match["tail"]) + 1, 1), total, False))
            continue
        start = int(match["start"])
        if match["count"]:
            windows.append((start, start + int(match["count"]) - 1, False))
        elif match["end"]:
            windows.append((start, int(match["end"]), False))
        else:
            windows.append((start, start + DEFAULT_READ_LINES - 1, True))
    return windows


def _placeholder(count: int) -> str:
    return "x\n" * count


def _render_diff(spec: dict[str, Any], names: dict[int, str]) -> str:
    lines: list[str] = []
    for ordinal, section in enumerate(spec["sections"]):
        start = len(lines) + 1
        if isinstance(section, int):
            lines.append(f"diff --git a/unlisted/{ordinal} b/unlisted/{ordinal}")
            lines.extend(f"unlisted {ordinal}" for _ in range(section - 1))
            continue
        index, total, hunks = section
        name = names[index]
        lines.append(f"diff --git a/{name} b/{name}")
        if hunks:
            lines.extend(f"index {number}" for number in range(hunks[0][0] - start - 3))
            lines.extend((f"--- a/{name}", f"+++ b/{name}"))
        for block_start, block_end, new_start, new_count, additive in hunks:
            assert len(lines) + 1 == block_start
            body = block_end - block_start
            if additive:
                lines.append(f"@@ -0,0 +{new_start},{new_count} @@")
                lines.extend("+x" for _ in range(new_count))
                lines.extend("\\ No newline at end of file" for _ in range(body - new_count))
            else:
                removed = body - new_count
                lines.append(f"@@ -1,{removed} +{new_start},{new_count} @@")
                lines.extend("-x" for _ in range(removed))
                lines.extend("+x" for _ in range(new_count))
        lines.extend(f"unlisted {ordinal}" for _ in range(start + total - 1 - len(lines)))
    assert len(lines) == spec["lines"]
    return "\n".join(lines) + "\n"


def _staged_names(required: list[list[Any]]) -> tuple[dict[int, str], dict[int, str]]:
    request: dict[int, str] = {}
    upstream: dict[int, str] = {}
    for ordinal, (path, _) in enumerate(required):
        match = re.fullmatch(rf"{re.escape(promotion.STAGED_PROMOTION_DIR)}/(?:new|old)/(.+)", path)
        if match:
            upstream[ordinal] = match[1]
        else:
            request[ordinal] = path
    return request, upstream


class Replay:
    def __init__(self, workspace: Path, spec: dict[str, Any]) -> None:
        self.workspace = workspace
        self.spec = spec
        self.required = [path for path, _ in spec["required"]]
        self.lines = {path: count for path, count in spec["required"]}
        self.lines[STAGED_DIFF_NAME] = spec["pull_request_diff"]["lines"]
        self.lines[PROMOTION_DIFF] = spec["promotion_diff"]["lines"]
        self.plan: dict[str, Any] = {
            "brief_mode": "file-list",
            "coverage": 1,
            "evidence_files": self.required,
            "evidence_exempt_paths": [],
            "binary_files": [],
            "diff_inline": None,
            "max_defects": 10,
            "protocol": {},
        }

    def path(self, name: str) -> str:
        return str(self.workspace / name)

    def _result(self, name: str, selector: str | None) -> dict[str, Any]:
        total = self.lines.get(name)
        if total is None:
            return {"isError": False, "content": [{"type": "text", "text": "x"}], "details": {}}
        numbers: set[int] = set()
        truncated = False
        for start, end, defaulted in _windows(selector, total):
            if start > total:
                return {"isError": True, "content": [{"type": "text", "text": "beyond end of file"}], "details": {}}
            numbers.update(range(start, min(end, total) + 1))
            truncated = truncated or (defaulted and end < total)
        shown = sorted(numbers)
        details: dict[str, Any] = {
            "totalLines": total,
            "rowPrefixes": False,
            "displayContent": {"text": "x", "startLine": shown[0], "lineNumbers": shown},
        }
        if truncated:
            details["truncation"] = {
                "truncated": True,
                "truncatedBy": "lines",
                "totalLines": total,
                "outputLines": len(shown),
                "lastLinePartial": False,
                "firstLineExceedsLimit": False,
            }
        return {"isError": False, "content": [{"type": "text", "text": "x"}], "details": details}

    def stdout(self, calls: list[tuple[str, str, str | None]], answer: dict[str, Any]) -> str:
        events: list[dict[str, Any]] = []
        for number, (name, relative, selector) in enumerate(calls):
            identifier = f"call-{number}"
            target = self.path(relative) if relative else str(self.workspace)
            arguments: dict[str, Any] = {"path": target + (f":{selector}" if selector else "")}
            if name == "grep":
                arguments["pattern"] = "x"
            events.append(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "id": identifier, "name": name, "arguments": arguments}],
                    },
                }
            )
            outcome = (
                self._result(relative, selector)
                if name == "read"
                else {"isError": False, "content": [{"type": "text", "text": "x"}], "details": {}}
            )
            events.append(
                {
                    "type": "message_end",
                    "message": {"role": "toolResult", "toolCallId": identifier, "toolName": name, **outcome},
                }
            )
        final = {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(answer)}]},
        }
        events.append(final)
        return "".join(json.dumps(event) + "\n" for event in events)

    def recorded_calls(self) -> list[tuple[str, str, str | None]]:
        calls = []
        for item in self.spec["calls"]:
            name, reference, *rest = item
            path = self.required[reference] if isinstance(reference, int) else reference
            calls.append((name, path, rest[0] if rest else None))
        return calls

    def shipping_answer(self) -> dict[str, Any]:
        return {
            "status": "complete",
            "incomplete_reason": "",
            "decision": "SHIP",
            "summary": "every changed file was read",
            "files_examined": self.required,
            "defects": [],
            "prior_defects": [],
        }

    def diff_block_reads(self) -> list[tuple[str, str, str | None]]:
        covering: dict[str, list[str]] = {}
        diffs = (
            (self.spec["pull_request_diff"], STAGED_DIFF_NAME),
            (self.spec["promotion_diff"], PROMOTION_DIFF),
        )
        calls: list[tuple[str, str, str | None]] = []
        hunked: set[int] = set()
        for diff, name in diffs:
            for section in diff["sections"]:
                if isinstance(section, int):
                    continue
                for block_start, block_end, *_ in section[2]:
                    covering.setdefault(name, []).append(f"{block_start}-{block_end}")
                    hunked.add(section[0])
        for name, selectors in covering.items():
            calls.extend(("read", name, selector) for selector in selectors)
        calls.extend(("read", path, None) for ordinal, path in enumerate(self.required) if ordinal not in hunked)
        return calls

    def judge(self, policy: Policy, stdout: str) -> runner.ReviewOutcome:
        reviewer = replace(policy.reviewers["small"], harness="omp")
        return runner._judge(
            reviewer,
            Invocation(argv=(), cwd=self.workspace, output_file=None),
            runner.RunResult(0, stdout, "", False),
            workspace=self.workspace,
            staged_before=runner._digest(self.workspace / STAGED_DIFF_NAME),
            promotion_before=promotion.staged_tree_digest(self.workspace),
            now=NOW,
            plan=self.plan,
        )

    def verdict(self, examined: list[str] | None = None) -> Verdict:
        from theseus_reviewer.verdict import validate

        answer = self.shipping_answer()
        if examined is not None:
            answer["files_examined"] = examined
        return validate(answer)

    def gap(self, stdout: str) -> evidence.Gap | None:
        run = evidence.trace("omp", stdout)
        return evidence.check(
            self.verdict(),
            clears=True,
            harness="omp",
            brief_mode="file-list",
            workspace=self.workspace,
            required=self.required,
            run=run,
            targets=coverage.targets(self.plan, self.workspace),
            is_protected=lambda _: False,
        )


@pytest.fixture(scope="module")
def replay(tmp_path_factory: pytest.TempPathFactory) -> Replay:
    spec = _spec()
    root = tmp_path_factory.mktemp("pr3942").resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    request_names, upstream_names = _staged_names(spec["required"])
    for path, count in spec["required"]:
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_placeholder(count), encoding="utf-8")
    (workspace / STAGED_DIFF_NAME).write_text(_render_diff(spec["pull_request_diff"], request_names), encoding="utf-8")
    (workspace / PROMOTION_DIFF).write_text(_render_diff(spec["promotion_diff"], upstream_names), encoding="utf-8")
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@example.com"),
    ):
        subprocess.run(["git", "-C", str(workspace), *arguments], check=True)
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"/{STAGED_DIFF_NAME}\n/{promotion.STAGED_PROMOTION_DIR}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "head"], check=True)
    return Replay(workspace, spec)


def test_the_recorded_run_has_the_expected_shape() -> None:
    spec = _spec()
    assert len(spec["required"]) == 304
    assert len(spec["calls"]) == 405
    assert spec["source"]["promotion_entries"] == 213
    assert spec["promotion_diff"]["lines"] == 32947


def test_synthesized_diffs_reproduce_the_recorded_hunk_spans(replay: Replay) -> None:
    for diff, name in (
        (replay.spec["pull_request_diff"], STAGED_DIFF_NAME),
        (replay.spec["promotion_diff"], PROMOTION_DIFF),
    ):
        index = hunk_index((replay.workspace / name).read_text(encoding="utf-8"))
        for section in diff["sections"]:
            if isinstance(section, int):
                continue
            ordinal, _, hunks = section
            path = replay.required[ordinal].removeprefix(f"{promotion.STAGED_PROMOTION_DIR}/new/")
            path = path.removeprefix(f"{promotion.STAGED_PROMOTION_DIR}/old/")
            found = [
                [h.block_start, h.block_end, h.new_start, h.new_count, int(h.additive)] for h in index.get(path, [])
            ]
            assert found == hunks
        assert len((replay.workspace / name).read_text(encoding="utf-8").splitlines()) == diff["lines"]


def test_pr3942_ship_is_refused_for_uncovered_hunks(replay: Replay, policy: Policy) -> None:
    stdout = replay.stdout(replay.recorded_calls(), replay.shipping_answer())
    gap = replay.gap(stdout)
    assert gap is not None
    assert len(gap.files) >= 150
    outcome = replay.judge(policy, stdout)
    assert outcome.verdict is None
    assert outcome.missing is not None
    assert outcome.missing.kind == absence.INCOMPLETE
    assert len(outcome.missing.unread) >= 150
    assert set(outcome.missing.unread) <= set(replay.required)


def test_a_bare_read_of_the_recorded_run_covers_only_its_first_300_lines(replay: Replay) -> None:
    long_file = max(replay.required, key=lambda path: replay.lines[path])
    assert replay.lines[long_file] > DEFAULT_READ_LINES
    run = evidence.trace("omp", replay.stdout([("read", long_file, None)], replay.shipping_answer()))
    ranges = evidence.returned(run, replay.workspace)[long_file]
    assert ranges == ((1, DEFAULT_READ_LINES),)


def test_pr3942_ship_is_accepted_when_every_hunk_is_read_through_the_staged_diffs(
    replay: Replay, policy: Policy
) -> None:
    stdout = replay.stdout(replay.diff_block_reads(), replay.shipping_answer())
    assert replay.gap(stdout) is None
    outcome = replay.judge(policy, stdout)
    assert outcome.missing is None
    assert outcome.verdict is not None
    assert outcome.verdict["decision"] == "SHIP"


def test_reading_every_diff_block_but_one_leaves_that_file_uncovered(replay: Replay) -> None:
    reads = replay.diff_block_reads()
    dropped = next(item for item in reads if item[1] == PROMOTION_DIFF)
    kept = [item for item in reads if item is not dropped]
    gap = replay.gap(replay.stdout(kept, replay.shipping_answer()))
    assert gap is not None
    assert len(gap.files) == 1
