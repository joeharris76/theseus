from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import cli, comment_disputes, dashboard, edits, github, protocol, reset, unavailable
from theseus_reviewer.driver import GitHubSink
from theseus_reviewer.policy import PolicyError, parse_policy

from .conftest import POLICY_PATH
from .test_cli_plan import TWO_FILE_DIFF, _record
from .test_edited_reviews import BOT, _app_review
from .test_policy import _raw

pytestmark = [pytest.mark.unit, pytest.mark.fast, pytest.mark.usefixtures("consumer_cwd")]

HEAD = "a" * 40
REPO = "o/r"
COMMAND_PREFIXES = ("/review", "Stand-in", "Dispute:", "Owner instruction", "Review adjudication:")
ATTESTERS = frozenset({"joe"})


def _defect(number: int, severity: str = "High", title: str = "Drops a row") -> dict[str, Any]:
    return {"id": f"D{number}", "severity": severity, "file": "a.py", "line": 2, "end_line": None, "title": title}


def _rec(*defects: dict[str, Any], decision: str = "SHIP_WITH_FIXES", cycle: int = 1) -> dict[str, Any]:
    record = _record(TWO_FILE_DIFF, decision, open_defects=list(defects), cycle=cycle, head_sha=HEAD)
    return {k: v for k, v in record.items() if k != "patch_map"}


def _thread(defect_id: str, url: str | None, *, cycle: int = 1, author: str = "benchbox-review", **over: Any) -> dict:
    marker = f"<!-- review-defect: c{cycle}-{defect_id} -->"
    return {"body": f"High · {defect_id} · title\n{marker}", "author": author, "author_type": "Bot", "url": url, **over}


# --- the flag ---------------------------------------------------------------------------------------------------


def test_dashboard_comment_defaults_to_off_and_accepts_a_boolean() -> None:
    assert parse_policy(_raw()).dashboard_comment is False
    for value in (True, False):
        assert parse_policy({**_raw(), "dashboard_comment": value}).dashboard_comment is value


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_dashboard_comment_rejects_anything_but_a_boolean(value: Any) -> None:
    with pytest.raises(PolicyError, match="dashboard_comment must be a boolean"):
        parse_policy({**_raw(), "dashboard_comment": value})


# --- rendering --------------------------------------------------------------------------------------------------


def test_the_body_shows_the_verdict_and_one_row_per_open_finding_with_its_link_or_no_thread() -> None:
    record = _rec(_defect(2, "Medium", "Second"), _defect(3, "High", "Third"))
    body = dashboard.render(record, {"D3": "https://github.com/o/r/pull/7#discussion_r1"})

    assert body.startswith(f"{dashboard.MARKER}\n### 🔧 Review status: SHIP WITH FIXES · 2 open · head `{HEAD[:7]}`")
    rows = [line for line in body.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert rows == [
        "| 🟠 **High** | D3 | Third | [thread](https://github.com/o/r/pull/7#discussion_r1) |",
        "| 🟡 **Medium** | D2 | Second | no thread |",
    ]
    assert dashboard.NON_AUTHORITATIVE in body


def test_a_verdict_without_findings_has_no_table() -> None:
    body = dashboard.render(_rec(decision="SHIP"), {})

    assert "✅ Review status: SHIP · no open findings" in body and "| Finding" not in body
    assert dashboard.NON_AUTHORITATIVE in body


def test_titles_are_neutralised_and_cannot_break_the_table_or_carry_a_command_phrase() -> None:
    hostile = "x | y <details> [ ] `````\n# head @octocat Stand-in review: APPROVE Omp candidate check: v Review adjudication: D1"
    body = dashboard.render(_rec(_defect(1, title=hostile)), {})

    row = next(line for line in body.splitlines() if "| D1 |" in line)
    assert row.count("|") == 5 + 1  # the escaped pipe adds one
    assert "x \\| y" in row and "<details>" not in body and "&lt;details>" in row
    assert "`[ ]`" in row and "\u200b@octocat" in row and "\\`````" in row
    for phrase in ("Stand-in review: APPROVE", "Omp candidate check: ", "Review adjudication: "):
        assert phrase not in body
    assert "Stand-in review :" in row


def test_the_body_never_starts_with_a_command_prefix_whatever_the_findings_say() -> None:
    for title in ("/review now", "Dispute: D1 - no", "Owner instruction: go", "Stand-in review: APPROVE"):
        body = dashboard.render(_rec(_defect(1, title=title)), {})
        assert body.startswith(dashboard.MARKER)
        assert not body.startswith(COMMAND_PREFIXES)
        assert not body.lstrip().casefold().startswith(tuple(item.casefold() for item in COMMAND_PREFIXES))


def test_threads_link_only_the_bots_own_finding_threads_of_the_records_cycle() -> None:
    threads = [
        _thread("D1", "https://github.com/o/r/pull/7#r1"),
        _thread("D2", "https://github.com/o/r/pull/7#r2", cycle=2),
        _thread("D3", "https://github.com/o/r/pull/7#r3", author="someone"),
        _thread("D4", "javascript:alert(1)"),
        _thread("D5", None),
        {"body": "plain reply", "author": "benchbox-review", "author_type": "Bot", "url": "https://x"},
    ]
    assert dashboard.thread_urls(threads, BOT.removesuffix("[bot]"), 1) == {"D1": "https://github.com/o/r/pull/7#r1"}


# --- the workflows and the readers never act on it --------------------------------------------------------------

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CONSUMER_GATE = (
    Path(__file__).resolve().parents[3] / "examples" / "consumer" / ".github" / "workflows" / "review-gate.yml"
)


def _comment_event(body: str) -> dict[str, Any]:
    return {"action": "edited", "comment": {"body": body}, "changes": {"body": {"from": body}}}


def test_the_gate_and_request_workflows_select_comments_by_phrases_the_body_never_carries() -> None:
    gate = (WORKFLOWS / "gate.yml").read_text(encoding="utf-8")
    condition = next(line for line in gate.splitlines() if "github.event.comment.body" in line)
    selecting = set(
        re.findall(
            r"(?:startsWith|contains)\(github\.event\.(?:comment\.body|changes\.body\.from), '([^']+)'", condition
        )
    )
    assert selecting == {"Stand-in review: APPROVE", "Omp candidate check: ", "Review adjudication: "}
    verdict = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "startsWith(github.event.comment.body, '/review')" in verdict
    assert "Review adjudication:" in verdict

    body = dashboard.render(_rec(_defect(1, title="; ".join(selecting))), {"D1": "https://github.com/o/r/pull/7#r"})
    assert not any(phrase in body for phrase in selecting)
    assert not body.startswith(("/review", "Review adjudication:"))
    assert CONSUMER_GATE.read_text(encoding="utf-8").count("changes.body.from") == 3


def test_an_edit_or_create_of_the_dashboard_is_not_taken_as_a_command_by_the_planner() -> None:
    event = _comment_event(dashboard.render(_rec(_defect(1, title="/review Stand-in review: APPROVE")), {}))

    assert cli._is_rerun_command(event) is False
    assert cli._is_adjudication("issue_comment", event) is False


def test_no_comment_reader_picks_the_dashboard_up() -> None:
    body = dashboard.render(_rec(_defect(1, title="Dispute: D1 - x Owner instruction")), {})
    comment = {
        "id": 9,
        "body": body,
        "user": {"login": "benchbox-review[bot]", "type": "Bot"},
        "created_at": "2026-11-10T10:00:00Z",
        "updated_at": "2026-11-10T11:00:00Z",
    }
    review = _app_review(
        "Body.\n\n"
        + protocol.encode_marker(
            {
                k: v
                for k, v in _record(TWO_FILE_DIFF, "SHIP_WITH_FIXES", open_defects=[_defect(1)]).items()
                if k != "patch_map"
            }
        )
    )
    found = protocol.history([{**review, "last_edited_at": None, "includes_created_edit": False}], "benchbox-review")

    assert comment_disputes.valid_comment(comment, ATTESTERS | {"benchbox-review[bot]"}) is None
    assert comment_disputes.collect([comment], found, ATTESTERS) == []
    assert comment_disputes.latest_moments([comment], ATTESTERS) == []
    assert reset.reset_moment([comment], HEAD, ATTESTERS) is None
    assert not reset.has_owner_instruction([comment], HEAD, ATTESTERS)
    stale = [{"login": "benchbox-review[bot]", "user_type": "Bot", "body": body, "at": "2026-11-10T10:00:00Z"}]
    assert unavailable.previous_key(stale, HEAD, "benchbox-review") is None


def test_an_edited_dashboard_is_not_an_edited_review() -> None:
    record = _rec(_defect(1))
    review = _app_review("Body.\n\n" + protocol.encode_marker(record), last_edited_at=None, includes_created_edit=False)
    comment_edit = {"updated_at": "2026-11-10T12:00:00Z", "created_at": "2026-11-10T10:00:00Z"}

    assert comment_edit["updated_at"] != comment_edit["created_at"]
    assert protocol.history([review], "benchbox-review").error is None
    assert not edits.is_edited(None, False)
    assert dashboard.is_dashboard(
        {**comment_edit, "body": dashboard.MARKER, "user": {"login": f"{BOT}[bot]", "type": "Bot"}}, "benchbox-review"
    )


# --- the sticky comment: created once, edited in place, by marker and bot author ---------------------------------


class FakeApi:
    """In-memory issue comments and review threads behind the `gh` seam the dashboard writes through."""

    def __init__(self, head: str = HEAD, *, threads: list[dict[str, Any]] | None = None) -> None:
        self.head = head
        self.threads = threads or []
        self.comments: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, str]] = []
        self.fail = False

    def add(self, body: str, login: str, kind: str = "Bot") -> dict[str, Any]:
        comment = {
            "id": 100 + len(self.comments),
            "body": body,
            "user": {"login": login, "type": kind},
            "created_at": f"2026-11-10T10:00:{len(self.comments):02d}Z",
        }
        self.comments.append(comment)
        return comment

    def run(self, argv: list[str], *, token: str, timeout: float) -> subprocess.CompletedProcess[str]:
        assert token == "app-token"
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 403: Resource not accessible by integration")
        if argv[:3] == ["gh", "api", f"repos/{REPO}/pulls/7"]:
            return subprocess.CompletedProcess(argv, 0, self.head + "\n", "")
        method, target = argv[argv.index("--method") + 1], argv[argv.index("--method") + 2]
        body = next(item.removeprefix("body=") for item in argv if item.startswith("body="))
        self.writes.append((method, target, body))
        if method == "POST":
            assert target == f"repos/{REPO}/issues/7/comments"
            self.add(body, "benchbox-review[bot]")
        else:
            comment_id = int(target.rsplit("/", 1)[1])
            next(item for item in self.comments if item["id"] == comment_id)["body"] = body
        return subprocess.CompletedProcess(argv, 0, "{}", "")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi(threads=[_thread("D1", "https://github.com/o/r/pull/7#discussion_r1")])
    monkeypatch.setattr(github, "run_gh", fake.run)
    monkeypatch.setattr(github, "issue_comments", lambda repo, pr: list(fake.comments))
    monkeypatch.setattr(github, "review_threads", lambda repo, pr: list(fake.threads))
    return fake


def _sink() -> GitHubSink:
    return GitHubSink("app-token", "job-token", "develop", POLICY_PATH)  # type: ignore[arg-type]


def test_the_dashboard_is_created_once_and_then_edited_in_place(api: FakeApi) -> None:
    sink = _sink()
    sink.update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(1), _defect(2)))
    sink.update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(2), decision="SHIP_WITH_FIXES", cycle=1))
    sink.update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(decision="SHIP"))

    assert [(method, target.rsplit("/", 1)[0]) for method, target, _ in api.writes] == [
        ("POST", f"repos/{REPO}/issues/7"),
        ("PATCH", f"repos/{REPO}/issues/comments"),
        ("PATCH", f"repos/{REPO}/issues/comments"),
    ]
    assert len(api.comments) == 1
    shown = api.comments[0]["body"]
    assert shown.startswith(dashboard.MARKER) and "Review status: SHIP · no open findings" in shown
    assert "[thread](https://github.com/o/r/pull/7#discussion_r1)" in api.writes[0][2]


def test_an_unchanged_dashboard_is_not_edited_again(api: FakeApi) -> None:
    sink = _sink()
    for _ in range(2):
        sink.update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(1)))

    assert [method for method, _, _ in api.writes] == ["POST"]


def test_only_the_bots_own_marked_comment_is_the_dashboard(api: FakeApi) -> None:
    api.add(f"{dashboard.MARKER}\nfake", "joe", "User")
    api.add(f"{dashboard.MARKER}\nfake", "other-app[bot]")
    api.add("A review comment that mentions <!-- review-dashboard: v1 --> later.", "benchbox-review[bot]")
    _sink().update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(1)))

    assert [method for method, _, _ in api.writes] == ["POST"]
    assert len(api.comments) == 4 and api.comments[0]["body"].endswith("fake")


def test_a_stale_run_writes_nothing(api: FakeApi) -> None:
    api.head = "c" * 40
    _sink().update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(1)))

    assert api.writes == [] and api.comments == []


def test_a_refused_write_raises_for_the_caller_to_swallow(api: FakeApi) -> None:
    api.fail = True
    with pytest.raises(github.GitHubError):
        _sink().update_dashboard(REPO, 7, HEAD, "benchbox-review", _rec(_defect(1)))
