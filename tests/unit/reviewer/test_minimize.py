from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import dedup, edits, github, minimize, promotion, protocol, review_check
from theseus_reviewer.driver import GitHubSink

from .conftest import POLICY_PATH
from .test_cli_plan import TWO_FILE_DIFF, _record
from .test_edited_reviews import BOT, _app_review

pytestmark = [pytest.mark.unit, pytest.mark.fast, pytest.mark.usefixtures("consumer_cwd")]

HEAD = "a" * 40
OLD = "b" * 40
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "minimize_probe"
STATES = ("before", "after_review_minimize", "after_review_unminimize", "after_inline_minimize")


def _body(decision: str = "SHIP") -> str:
    record = _record(TWO_FILE_DIFF, decision)
    return "Body.\n\n" + protocol.encode_marker({k: v for k, v in record.items() if k != "patch_map"})


def _review(number: int, body: str | None = None, *, head: str = HEAD, **over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": number,
        "node_id": f"PRR_{number}",
        "commit_id": head,
        "submitted_at": f"2026-11-10T10:0{number}:00Z",
        **over,
    }
    return _app_review(_body() if body is None else body, **fields)


def test_every_earlier_record_bearing_review_is_superseded_and_the_newest_never() -> None:
    reviews = [_review(3), _review(1, head=OLD), _review(2, head=OLD)]
    assert minimize.superseded(reviews, BOT, HEAD) == ["PRR_1", "PRR_2"]


def test_the_newest_review_is_ordered_by_submission_time_then_id() -> None:
    older, newer = _review(1), _review(2, submitted_at="2026-11-10T10:01:00Z")
    tie = _review(3, submitted_at="2026-11-10T10:01:00Z")
    assert minimize.superseded([tie, newer, older], BOT, HEAD) == ["PRR_1", "PRR_2"]


def test_nothing_is_superseded_by_a_lone_review() -> None:
    assert minimize.superseded([_review(1)], BOT, HEAD) == []
    assert minimize.superseded([], BOT, HEAD) == []


def test_a_not_reviewed_post_is_neither_a_candidate_nor_the_review_that_supersedes() -> None:
    not_reviewed = _review(3, "### review-verdict: pending for `a`\n\nReviewers not available.\n")
    assert minimize.superseded([_review(1, head=OLD), _review(2, head=OLD), not_reviewed], BOT, HEAD) == []
    assert minimize.superseded([_review(1, head=OLD), _review(2), not_reviewed], BOT, HEAD) == ["PRR_1"]


def test_a_new_not_reviewed_post_alone_minimises_nothing() -> None:
    assert minimize.superseded([_review(1, head=OLD), _review(2, "Reviewers not available.")], BOT, HEAD) == []


def test_nothing_is_minimised_unless_the_newest_record_bearing_review_is_on_the_current_head() -> None:
    assert minimize.superseded([_review(1, head=OLD), _review(2, head=OLD)], BOT, HEAD) == []


@pytest.mark.parametrize(
    "outsider",
    [
        {"login": "joe", "user_type": "User"},
        {"login": "other-app[bot]", "user_type": "Bot"},
        {"state": "DISMISSED"},
    ],
    ids=["human", "another-app", "dismissed"],
)
def test_only_the_apps_own_live_reviews_are_candidates(outsider: dict[str, Any]) -> None:
    reviews = [_review(1, head=OLD, **outsider), _review(2, head=OLD), _review(3)]
    assert minimize.superseded(reviews, BOT, HEAD) == ["PRR_2"]


def test_a_review_with_an_unreadable_marker_or_no_node_id_is_skipped_without_failing() -> None:
    broken = _review(1, "x\n<!-- review-protocol: v1 !!! -->")
    nameless = _review(2, node_id=None)
    assert minimize.superseded([broken, nameless, _review(3, head=OLD), _review(4)], BOT, HEAD) == ["PRR_3"]


def test_a_review_without_a_protocol_record_is_never_a_candidate() -> None:
    plain = _review(1, "Decision.\n", head=OLD)
    assert minimize.superseded([plain, _review(2, head=OLD), _review(3)], BOT, HEAD) == ["PRR_2"]


# --- the GraphQL calls, against a fake `gh` ---------------------------------------------------------------------


class FakeGh:
    def __init__(self, *, minimized: tuple[str, ...] = (), refuse: tuple[str, ...] = (), query_fails: bool = False):
        self.minimized = set(minimized)
        self.refuse = set(refuse)
        self.query_fails = query_fails
        self.calls: list[tuple[str, dict[str, list[str]], str]] = []

    def __call__(self, argv: list[str], *, token: str, timeout: float) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["gh", "api", "graphql"]
        fields: dict[str, list[str]] = {}
        pairs = [item for flag, item in zip(argv[3:], argv[4:], strict=False) if flag == "-f"]
        for pair in pairs:
            name, _, value = pair.partition("=")
            fields.setdefault(name, []).append(value)
        query = fields.pop("query")[0]
        token = token
        kind = "mutation" if query.lstrip().startswith("mutation") else "query"
        self.calls.append((kind, fields, token))
        if kind == "query":
            if self.query_fails:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403: Resource not accessible by integration")
            nodes = [{"id": node, "isMinimized": node in self.minimized} for node in fields["ids[]"]]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"nodes": nodes}}), "")
        node = fields["id"][0]
        if node in self.refuse:
            body = {
                "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}],
                "data": None,
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        probe = json.loads((FIXTURES / "minimize_review_response.json").read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0, json.dumps(probe), "")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(github, "run_gh", fake)
    return fake


def test_the_mutation_is_the_probed_one_and_only_names_the_given_reviews(gh: FakeGh) -> None:
    assert github.minimize_reviews(["PRR_1", "PRR_2", "PRR_1"], "app-token") == []
    mutations = [(fields, token) for kind, fields, token in gh.calls if kind == "mutation"]
    assert mutations == [({"id": ["PRR_1"]}, "app-token"), ({"id": ["PRR_2"]}, "app-token")]
    assert "classifier: OUTDATED" in github.MINIMIZE_MUTATION and "subjectId: $id" in github.MINIMIZE_MUTATION


def test_an_already_minimised_review_is_not_minimised_again() -> None:
    fake = FakeGh(minimized=("PRR_1",))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(github, "run_gh", fake)
        assert github.minimize_reviews(["PRR_1", "PRR_2"], "t") == []
    assert [fields for kind, fields, _ in fake.calls if kind == "mutation"] == [{"id": ["PRR_2"]}]


def test_no_review_means_no_request(gh: FakeGh) -> None:
    assert github.minimize_reviews([], "t") == []
    assert gh.calls == []


def test_a_refused_mutation_is_reported_and_the_others_still_run() -> None:
    fake = FakeGh(refuse=("PRR_1",))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(github, "run_gh", fake)
        failures = github.minimize_reviews(["PRR_1", "PRR_2"], "t")
    assert len(failures) == 1 and failures[0].startswith("PRR_1:") and "not accessible" in failures[0]
    assert [fields for kind, fields, _ in fake.calls if kind == "mutation"] == [{"id": ["PRR_1"]}, {"id": ["PRR_2"]}]


def test_a_failed_state_query_raises_before_any_mutation() -> None:
    fake = FakeGh(query_fails=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(github, "run_gh", fake)
        with pytest.raises(github.GitHubError, match="not accessible"):
            github.minimize_reviews(["PRR_1"], "t")
    assert all(kind == "query" for kind, _, _ in fake.calls)


def test_minimising_through_the_pull_request_touches_only_superseded_reviews(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh
) -> None:
    human = _review(4, login="joe", user_type="User", head=OLD)
    reviews = [_review(1, head=OLD), _review(2, "Not reviewed.", head=OLD), human, _review(3)]
    monkeypatch.setattr(github, "reviews", lambda repo, pr: reviews)
    monkeypatch.setattr(github, "pull_head_sha", lambda repo, pr, token: HEAD)
    monkeypatch.setattr(github, "review_threads", lambda repo, pr: [])
    sink = GitHubSink("app-token", "job-token", "develop", POLICY_PATH)
    assert sink.minimize_superseded("o/r", 7, HEAD, BOT) == []
    assert [fields for kind, fields, _ in gh.calls if kind == "mutation"] == [{"id": ["PRR_1"]}]


# --- minimised content is still read by every detector, and a minimise is never an edit --------------------------


def _fixture(state: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads((FIXTURES / state / "graphql.json").read_text(encoding="utf-8"))
    rest = json.loads((FIXTURES / state / "rest_reviews.json").read_text(encoding="utf-8"))
    return data["data"]["repository"]["pullRequest"], rest


def _probe_review(state: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The probe's first review as Theseus reads it: the recorded edit state, with a decodable protocol record.

    The probe posted as a user, so the identity is the bot's and the opaque `abc` record is a real one.
    """
    pull, rest = _fixture(state)
    node = pull["reviews"]["nodes"][0]
    posted = next(item for item in rest if item["id"] == node["databaseId"])
    body = node["body"].split("<!-- review-protocol")[0] + protocol.encode_marker(
        {k: v for k, v in _record(TWO_FILE_DIFF, "SHIP").items() if k != "patch_map"}
    )
    review = {
        "id": node["databaseId"],
        "node_id": node["id"],
        "login": f"{BOT}[bot]",
        "user_type": "Bot",
        "state": posted["state"],
        "submitted_at": posted["submitted_at"],
        "commit_id": posted["commit_id"],
        "body": body,
        "last_edited_at": node["lastEditedAt"],
        "includes_created_edit": node["includesCreatedEdit"],
    }
    return pull, node, review


@pytest.mark.parametrize("state", STATES)
def test_the_probes_recorded_minimised_objects_are_not_edits(state: str) -> None:
    pull, node, review = _probe_review(state)
    assert node["lastEditedAt"] is None and node["includesCreatedEdit"] is False
    assert node["userContentEdits"]["totalCount"] == 0
    assert edits.node_edited(node) is False
    assert [edits.node_edited(comment) for comment in node["comments"]["nodes"]] == [False, False]
    assert edits.parse_review_edits({"data": {"nodes": [node]}}, [node["id"]]) == {node["id"]: (None, False)}
    assert [(t["viewerCanReply"], t["viewerCanResolve"], t["isResolved"]) for t in pull["reviewThreads"]["nodes"]] == [
        (True, True, False)
    ] * 2


def test_the_probe_recorded_the_minimise_and_its_reversal() -> None:
    minimized = {state: _fixture(state)[0]["reviews"]["nodes"][0]["isMinimized"] for state in STATES}
    assert minimized == {
        "before": False,
        "after_review_minimize": True,
        "after_review_unminimize": False,
        "after_inline_minimize": False,
    }
    assert _fixture("after_review_minimize")[0]["reviews"]["nodes"][0]["minimizedReason"] == "outdated"


@pytest.mark.parametrize("state", STATES)
def test_protocol_history_reads_a_minimised_review_and_does_not_hold(state: str) -> None:
    _, _, review = _probe_review(state)
    found = protocol.history([review], BOT)
    assert found.error is None
    assert [record["decision"] for record in found.records] == ["SHIP"]
    assert found.ids == [review["id"]]


def test_a_minimised_review_yields_the_same_history_as_the_unminimised_one() -> None:
    before = protocol.history([_probe_review("before")[2]], BOT)
    after = protocol.history([_probe_review("after_review_minimize")[2]], BOT)
    assert (after.records, after.error, after.ids) == (before.records, before.error, before.ids)


@pytest.mark.parametrize("state", STATES)
def test_the_receipt_check_and_promotion_reader_accept_a_minimised_review(state: str) -> None:
    _, _, review = _probe_review(state)
    head = review["commit_id"]
    uuid = protocol.decode_marker(review["body"]).get("run_uuid") or "4242"
    review["body"] = f"x\n<!-- review-verdict: v2 success {head} -->\n" + protocol.encode_marker(
        {**{k: v for k, v in _record(TWO_FILE_DIFF, "SHIP").items() if k != "patch_map"}, "run_uuid": uuid}
    )
    assert review_check._receipt_candidates([review], head, None, BOT) == [review]
    assert review_check._receipt_agrees(review, head, uuid) is True
    records, reason = promotion._head_app_records(
        [review], head_sha=head, review_login=BOT, base_time=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert reason is None and [record["decision"] for record in records] == ["SHIP"]


@pytest.mark.parametrize("state", STATES)
def test_the_finding_fingerprint_of_a_minimised_threads_review_is_still_read(state: str) -> None:
    _, node, _ = _probe_review(state)
    inline = node["comments"]["nodes"][0]
    body = f"**High**: First finding\n\n<!-- review-defect: c1-D1 -->\n\n{dedup.MARKER_OPEN} " + (
        f"{dedup.fingerprint('f.txt', 'First finding')} -->"
    )
    thread = {
        "resolved": False,
        "edited": edits.node_edited(inline),
        "author_type": "Bot",
        "author": f"{BOT}[bot]",
        "body": body,
        "path": "f.txt",
    }
    assert dedup.open_fingerprints([thread], BOT) == {dedup.fingerprint("f.txt", "First finding"): "High"}


def test_minimising_does_not_move_the_edit_state_the_detectors_read() -> None:
    _, before, _ = _probe_review("before")
    _, after, _ = _probe_review("after_review_minimize")
    keep = ("body", "lastEditedAt", "includesCreatedEdit", "userContentEdits")
    assert {key: after[key] for key in keep} == {key: before[key] for key in keep}
    assert after["isMinimized"] is True and before["isMinimized"] is False


def test_a_real_edit_is_still_detected_next_to_minimised_reviews() -> None:
    _, _, minimised = _probe_review("after_review_minimize")
    edited = copy.deepcopy(minimised) | {"id": 9, "last_edited_at": "2026-11-10T16:00:00Z"}
    assert protocol.history([minimised, edited], BOT).error == "a review was edited"


# --- findings that exist only in a review body keep that review expanded ------------------------------------------


def _defect(number: int) -> dict[str, Any]:
    return {"id": f"D{number}", "severity": "High", "file": "f.txt", "line": 1, "end_line": None, "title": f"t{number}"}


def _listing(number: int, defects: list[int], *, head: str = HEAD, cycle: int = 1) -> dict[str, Any]:
    record = _record(TWO_FILE_DIFF, "SHIP_WITH_FIXES", open_defects=[_defect(n) for n in defects], cycle=cycle)
    body = "Body.\n\n" + protocol.encode_marker({k: v for k, v in record.items() if k != "patch_map"})
    return _review(number, body, head=head)


def test_an_earlier_review_listing_a_still_open_threadless_defect_stays_expanded() -> None:
    reviews = [_listing(1, [1, 2], head=OLD), _listing(2, [3], head=OLD), _listing(3, [1, 2])]
    assert minimize.superseded(reviews, BOT, HEAD, threaded={"c1-D2"}) == ["PRR_2"]
    assert minimize.superseded(reviews, BOT, HEAD, threaded={"c1-D1", "c1-D2"}) == ["PRR_1", "PRR_2"]
    assert minimize.superseded(reviews, BOT, HEAD) == ["PRR_2"]


def test_a_threadless_defect_that_is_fixed_in_the_newest_record_no_longer_protects_its_review() -> None:
    reviews = [_listing(1, [1], head=OLD), _listing(2, [2])]
    assert minimize.superseded(reviews, BOT, HEAD) == ["PRR_1"]


def test_a_defect_id_from_another_cycle_does_not_protect_a_review() -> None:
    reviews = [_listing(1, [1], head=OLD, cycle=1), _listing(2, [1], cycle=2)]
    assert minimize.superseded(reviews, BOT, HEAD) == ["PRR_1"]


def test_the_sink_reads_thread_markers_to_decide_which_reviews_stay_expanded(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh
) -> None:
    reviews = [_listing(1, [1, 2], head=OLD), _listing(2, [3], head=OLD), _listing(3, [1, 2])]
    bot = {"author": f"{BOT}[bot]", "author_type": "Bot", "body": "x\n<!-- review-defect: c1-D1 -->", "resolved": True}
    monkeypatch.setattr(github, "reviews", lambda repo, pr: reviews)
    monkeypatch.setattr(github, "pull_head_sha", lambda repo, pr, token: HEAD)
    monkeypatch.setattr(github, "review_threads", lambda repo, pr: [bot])
    sink = GitHubSink("app-token", "job-token", "develop", POLICY_PATH)
    assert sink.minimize_superseded("o/r", 7, HEAD, BOT) == []
    # D2 has no thread, so review 1 stays; review 2 lists only D3, which the newest record no longer holds open.
    assert [fields for kind, fields, _ in gh.calls if kind == "mutation"] == [{"id": ["PRR_2"]}]


# --- the step is bounded ------------------------------------------------------------------------------------------


def test_every_graphql_call_has_a_timeout_and_a_timeout_is_a_reported_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def hang(argv: list[str], *, token: str, timeout: float) -> Any:
        seen.append(timeout)
        raise github.GitHubError("gh api graphql timed out after 20 seconds")

    monkeypatch.setattr(github, "run_gh", hang)
    with pytest.raises(github.GitHubError, match="timed out"):
        github.minimize_reviews(["PRR_1"], "t")
    assert seen == [github.MINIMIZE_CALL_SECONDS]


def test_the_total_time_cap_skips_the_remaining_reviews(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGh()
    monkeypatch.setattr(github, "run_gh", fake)
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(github, "monotonic", lambda: next(clock))
    failures = github.minimize_reviews(["PRR_1", "PRR_2"], "t")
    assert [fields for kind, fields, _ in fake.calls if kind == "mutation"] == [{"id": ["PRR_1"]}]
    assert len(failures) == 1 and failures[0].startswith("PRR_2:") and "limit" in failures[0]


def test_a_stale_run_minimises_nothing_when_the_pull_request_has_moved_on(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGh
) -> None:
    reviews = [_review(1, head=OLD), _review(2, head=OLD), _review(3)]
    monkeypatch.setattr(github, "reviews", lambda repo, pr: reviews)
    monkeypatch.setattr(github, "review_threads", lambda repo, pr: [])
    monkeypatch.setattr(github, "pull_head_sha", lambda repo, pr, token: "c" * 40)
    sink = GitHubSink("app-token", "job-token", "develop", POLICY_PATH)
    assert sink.minimize_superseded("o/r", 7, HEAD, BOT) == []
    assert gh.calls == []


def test_a_failed_live_head_read_minimises_nothing(monkeypatch: pytest.MonkeyPatch, gh: FakeGh) -> None:
    def broken(repo: str, pr: int, token: str) -> str:
        raise github.GitHubError("HTTP 502")

    monkeypatch.setattr(github, "pull_head_sha", broken)
    sink = GitHubSink("app-token", "job-token", "develop", POLICY_PATH)
    with pytest.raises(github.GitHubError):
        sink.minimize_superseded("o/r", 7, HEAD, BOT)
    assert gh.calls == []


def test_run_gh_turns_a_timeout_into_a_github_error() -> None:
    import sys

    with pytest.raises(github.GitHubError, match="timed out"):
        github.run_gh([sys.executable, "-c", "import time; time.sleep(5)"], token="t", timeout=0.2)
