from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from conftest import CONSUMER_ALPHA
from theseus_reviewer import gate, review_check, status_text

POLICY_REL = ".github/review.yml"
CONFIG = gate.load_gate_config(POLICY_REL, CONSUMER_ALPHA)
BINDING = CONFIG.binding
POLICY_ARGS = ["--policy", POLICY_REL, "--root", str(CONSUMER_ALPHA)]

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
RUN = "1" * 32
REVIEW = "benchbox-review[bot]"
HEAD_DATE = "2026-10-01T12:00:00Z"
AFTER_HEAD = "2026-10-01T13:00:00Z"
RETARGETED = "2026-10-01T14:00:00Z"
AFTER_RETARGET = "2026-10-01T15:00:00Z"
BEFORE_HEAD = "2026-10-01T11:00:00Z"
SOUNDNESS = ["benchbox/core/equivalence/checker.py"]
API = "https://api.github.com"


def _body(run_uuid: str = RUN) -> str:
    from theseus_reviewer import protocol

    record = {
        "v": protocol.MARKER_VERSION,
        "cycle": 1,
        "round": 1,
        "kind": protocol.FIRST,
        "decision": protocol.SHIP,
        "head_sha": HEAD,
        "base_ref": "develop",
        "reviewer": "large",
        "tier": "large",
        "strikes_after": 0,
        "open_defects": [],
        "next_id": 1,
        "patch_digest": "d" * 32,
        "carried": False,
        "summary": "",
        "run_uuid": run_uuid,
    }
    return f"### review-verdict: success for `{HEAD}`\n\nDecision: **SHIP**.\n\n{protocol.encode_marker(record)}"


def _status(state: str = "success", created_at: str = AFTER_HEAD, **over: Any) -> dict[str, Any]:
    description = f"large: {state} [run {RUN}]" if state == "success" else f"large: {state}"
    status = {
        "id": 1,
        "context": "review-verdict",
        "state": state,
        "description": description,
        "login": REVIEW,
        "user_type": "Bot",
        "created_at": created_at,
    }
    return {**status, **over}


def _receipt(**over: Any) -> dict[str, Any]:
    review = {
        "id": 1,
        "login": REVIEW,
        "user_type": "Bot",
        "commit_id": HEAD,
        "state": "COMMENTED",
        "submitted_at": HEAD_DATE,
        "body": _body(),
        "last_edited_at": None,
        "includes_created_edit": False,
    }
    return {**review, **over}


def _standin(created_at: str = AFTER_HEAD) -> dict[str, Any]:
    return {
        "login": "joeharris76",
        "user_type": "User",
        "body": f"Stand-in review: APPROVE {HEAD}",
        "created_at": created_at,
        "updated_at": created_at,
    }


def _decide(**kwargs: Any) -> tuple[int, str]:
    return review_check.decide(
        HEAD,
        kwargs.pop("files", SOUNDNESS),
        kwargs.pop("statuses", [_status()]),
        review_check.path_matcher(
            2, 2, CONFIG.matchers, CONFIG.policy.classifier.exempt_paths, CONFIG.policy.classifier.gate_paths
        ),
        kwargs.pop("base_date", None),
        kwargs.pop("comments", []),
        binding=BINDING,
        reviews=kwargs.pop("reviews", [_receipt()]),
    )


def test_a_receipt_posted_exactly_at_the_retarget_does_not_count() -> None:
    assert (
        _decide(
            statuses=[_status(created_at=AFTER_RETARGET)],
            reviews=[_receipt(submitted_at=RETARGETED)],
            base_date=RETARGETED,
        )[0]
        == review_check.WAITING
    )
    assert (
        _decide(
            statuses=[_status(created_at=AFTER_RETARGET)],
            reviews=[_receipt(submitted_at=AFTER_RETARGET)],
            base_date=RETARGETED,
        )[0]
        == review_check.PASS
    )


def test_a_verdict_posted_exactly_at_the_retarget_does_not_count() -> None:
    receipt = _receipt(submitted_at=AFTER_RETARGET)
    assert (
        _decide(statuses=[_status(created_at=RETARGETED)], reviews=[receipt], base_date=RETARGETED)[0]
        == review_check.OUTSTANDING
    )
    assert (
        _decide(statuses=[_status(created_at=AFTER_RETARGET)], reviews=[receipt], base_date=RETARGETED)[0]
        == review_check.PASS
    )


def test_a_standin_posted_exactly_at_the_last_decisive_verdict_does_not_count() -> None:
    failing = _status("failure", created_at=RETARGETED)
    assert _decide(statuses=[failing], comments=[_standin(RETARGETED)])[0] == review_check.WAITING
    assert _decide(statuses=[failing], comments=[_standin(AFTER_RETARGET)])[0] == review_check.PASS


def test_a_standin_posted_exactly_at_the_retarget_does_not_count() -> None:
    assert _decide(statuses=[], comments=[_standin(RETARGETED)], base_date=RETARGETED)[0] == review_check.OUTSTANDING
    assert _decide(statuses=[], comments=[_standin(AFTER_RETARGET)], base_date=RETARGETED)[0] == review_check.PASS


@pytest.mark.parametrize("state", ["PENDING", "DISMISSED"])
def test_a_pending_or_dismissed_review_is_not_a_receipt(state: str) -> None:
    assert _decide(reviews=[_receipt(state=state)])[0] == review_check.WAITING
    assert _decide(reviews=[_receipt(state="APPROVED")])[0] == review_check.PASS


def test_the_receipt_with_the_higher_id_wins_a_same_second_tie() -> None:
    edited = _receipt(id=2, last_edited_at=AFTER_HEAD, includes_created_edit=True)
    assert _decide(reviews=[_receipt(id=1), edited])[0] == review_check.WAITING
    assert _decide(reviews=[edited, _receipt(id=1)])[0] == review_check.WAITING
    assert _decide(reviews=[_receipt(id=2), edited | {"id": 1}])[0] == review_check.PASS


def test_a_receipt_with_an_id_beats_one_without_in_a_tie() -> None:
    edited = _receipt(id=1, last_edited_at=AFTER_HEAD, includes_created_edit=True)
    unnumbered = _receipt(id=None)
    assert _decide(reviews=[unnumbered, edited])[0] == review_check.WAITING


def test_the_verdict_with_the_higher_id_wins_a_same_second_tie() -> None:
    assert _decide(statuses=[_status("failure", id=1), _status("success", id=None)])[0] == review_check.WAITING


@pytest.mark.parametrize("missing", ["last_edited_at", "includes_created_edit"])
def test_a_receipt_missing_either_edit_field_does_not_count(missing: str) -> None:
    partial = {key: value for key, value in _receipt().items() if key != missing}
    assert _decide(reviews=[partial])[0] == review_check.WAITING


def test_a_receipt_edited_after_posting_does_not_count() -> None:
    assert (
        _decide(reviews=[_receipt(last_edited_at=AFTER_HEAD, includes_created_edit=False)])[0] == review_check.WAITING
    )


def test_the_newest_retarget_wins_across_timezone_offsets() -> None:
    events = [
        {"event": "base_ref_changed", "created_at": "2026-10-01T14:00:00Z"},
        {"event": "base_ref_changed", "created_at": "2026-10-01T09:00:00-07:00"},
    ]
    assert review_check.latest_base_change(events) == "2026-10-01T09:00:00-07:00"


def test_a_gate_path_that_is_also_inert_needs_review_and_an_exempt_pattern_skips() -> None:
    assert review_check.path_matcher(1, 1, CONFIG.matchers, (), ("gate/*",))(["gate/policy.md"]) is True
    assert review_check.path_matcher(1, 1, CONFIG.matchers, ("zz_exempt/*",), ())(["zz_exempt/a.txt"]) is False
    assert review_check.path_matcher(1, 1, CONFIG.matchers, ("zz_exempt/*",), ())(["zz_other/a.txt"]) is True


def test_annotate_receipt_ignores_reviews_before_the_retarget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        review_check, "fetch_review_edit", lambda token, node_id: calls.append(node_id) or (None, False)
    )
    reviews = [_receipt(node_id="node_1")]
    review_check.annotate_receipt("t", reviews, HEAD, BINDING.bot_login, RETARGETED)
    assert calls == []
    review_check.annotate_receipt("t", reviews, HEAD, BINDING.bot_login, BEFORE_HEAD)
    assert calls == ["node_1"]


def test_annotate_receipt_records_what_the_edit_check_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    def edit(token: str, node_id: str) -> tuple[str | None, bool]:
        seen.append((token, node_id))
        return AFTER_HEAD, False

    monkeypatch.setattr(review_check, "fetch_review_edit", edit)
    reviews = [_receipt(node_id="node_1")]
    review_check.annotate_receipt("tok", reviews, HEAD, BINDING.bot_login)
    assert seen == [("tok", "node_1")]
    assert reviews[0]["last_edited_at"] == AFTER_HEAD and reviews[0]["includes_created_edit"] is False
    assert _decide(reviews=reviews)[0] == review_check.WAITING

    monkeypatch.setattr(review_check, "fetch_review_edit", lambda token, node_id: (None, True))
    reviews = [_receipt(node_id="node_1")]
    review_check.annotate_receipt("tok", reviews, HEAD, BINDING.bot_login)
    assert reviews[0]["last_edited_at"] is None and reviews[0]["includes_created_edit"] is True


@pytest.mark.parametrize("node_id", [None, ""])
def test_a_receipt_that_cannot_be_checked_is_recorded_as_edited(node_id: str | None) -> None:
    reviews = [_receipt(node_id=node_id)]
    review_check.annotate_receipt("t", reviews, HEAD, BINDING.bot_login)
    assert reviews[0]["last_edited_at"] is not None
    assert reviews[0]["includes_created_edit"] is True


def test_a_failed_edit_check_raises_instead_of_recording_an_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(token: str, node_id: str) -> tuple[str | None, bool]:
        raise review_check.CheckError("down")

    monkeypatch.setattr(review_check, "fetch_review_edit", broken)
    reviews = [_receipt(node_id="node_1")]
    with pytest.raises(review_check.CheckError, match="down"):
        review_check.annotate_receipt("t", reviews, HEAD, BINDING.bot_login)
    assert reviews[0]["includes_created_edit"] is False


class Network:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.rest: dict[str, Any] = {}
        self.graphql: Any = lambda body: {}
        self.error: Exception | None = None

    def request(self, token: str, url: str, body: dict[str, Any] | None = None) -> Any:
        self.requests.append({"url": url, "body": body, "token": token})
        if self.error is not None:
            raise self.error
        assert url.startswith(API), url
        route = url[len(API) :]
        if route == "/graphql":
            return self.graphql(body)
        path, _, query = route.partition("?")
        assert path in self.rest, route
        payload = self.rest[path]
        if isinstance(payload, dict) and "pages" in payload:
            payload = payload["pages"].get(int(query.split("&page=")[1]), [])
        return payload

    def paths(self) -> list[str]:
        return [request["url"][len(API) :] for request in self.requests]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Network:
    fake = Network()
    monkeypatch.setattr(review_check, "_request", fake.request)
    return fake


def _authorized(fake: Network, token: str = "tok") -> None:
    assert fake.requests
    for request in fake.requests:
        assert request["token"] == token


class _Loopback:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.payload: bytes = b"{}"
        self.root = ""
        self.stop: Any = lambda: None


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = _Loopback()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            state.requests.append(
                {
                    "path": self.path,
                    "method": self.command,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": json.loads(raw.decode("utf-8")) if raw else None,
                }
            )
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(state.payload)))
            self.end_headers()
            self.wfile.write(state.payload)

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.root = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(review_check, "API_ROOT", state.root)
    stopped: list[bool] = []

    def stop() -> None:
        if not stopped:
            stopped.append(True)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    state.stop = stop
    try:
        yield state
    finally:
        stop()


def test_requests_carry_the_token_and_the_github_api_headers(loopback: Any) -> None:
    loopback.payload = b'{"ok": true}'
    assert review_check._request("tok", f"{review_check.API_ROOT}/x") == {"ok": True}
    request = loopback.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/x"
    assert request["headers"]["authorization"] == "Bearer tok"
    assert request["headers"]["accept"] == "application/vnd.github+json"
    assert request["headers"]["x-github-api-version"] == "2022-11-28"
    assert request["headers"]["user-agent"] == "theseus-review-check"
    assert "content-type" not in request["headers"]
    assert request["body"] is None


def test_a_posted_body_is_sent_as_utf8_json(loopback: Any) -> None:
    loopback.payload = json.dumps({"echo": "é"}).encode("utf-8")
    assert review_check._request("tok", f"{review_check.API_ROOT}/graphql", {"q": "é"}) == {"echo": "é"}
    request = loopback.requests[0]
    assert request["method"] == "POST"
    assert request["headers"]["content-type"] == "application/json"
    assert request["body"] == {"q": "é"}
    assert request["headers"]["authorization"] == "Bearer tok"


def test_an_http_error_status_becomes_a_check_error_naming_the_status(loopback: Any) -> None:
    loopback.status = 502
    with pytest.raises(review_check.CheckError, match="HTTP 502"):
        review_check._request("tok", f"{review_check.API_ROOT}/x")


def test_an_unreachable_server_becomes_a_check_error(loopback: Any) -> None:
    loopback.stop()
    with pytest.raises(review_check.CheckError, match="failed"):
        review_check._request("tok", f"{loopback.root}/x")


def test_a_non_json_response_becomes_a_check_error(loopback: Any) -> None:
    loopback.payload = b"not json"
    with pytest.raises(review_check.CheckError, match="failed"):
        review_check._request("tok", f"{review_check.API_ROOT}/x")


def test_pagination_follows_full_pages_and_stops_on_the_first_short_one(net: Network) -> None:
    full = [{"n": i} for i in range(100)]
    net.rest["/items"] = {"pages": {1: full, 2: full, 3: [{"n": 1}]}}
    assert len(review_check._paginate("tok", "/items")) == 201
    assert net.paths() == [
        "/items?per_page=100&page=1",
        "/items?per_page=100&page=2",
        "/items?per_page=100&page=3",
    ]
    _authorized(net)


def test_pagination_stops_after_a_page_of_exactly_one_hundred_when_the_next_is_empty(net: Network) -> None:
    net.rest["/items"] = {"pages": {1: [{"n": i} for i in range(100)]}}
    assert len(review_check._paginate("tok", "/items")) == 100
    assert net.paths() == ["/items?per_page=100&page=1", "/items?per_page=100&page=2"]


def test_pagination_returns_a_single_short_page_in_one_request(net: Network) -> None:
    net.rest["/items"] = {"pages": {1: [{"n": 1}, {"n": 2}]}}
    assert review_check._paginate("tok", "/items") == [{"n": 1}, {"n": 2}]
    assert net.paths() == ["/items?per_page=100&page=1"]


def test_fetch_pull_and_files_read_this_pull_request(net: Network) -> None:
    net.rest["/repos/o/r/pulls/7"] = {"head": {"sha": HEAD}}
    net.rest["/repos/o/r/pulls/7/files"] = {"pages": {1: [{"filename": "a.py"}]}}
    assert review_check.fetch_pull("tok", "o/r", 7) == {"head": {"sha": HEAD}}
    assert review_check.fetch_files("tok", "o/r", 7) == [{"filename": "a.py"}]
    _authorized(net)


def test_fetch_statuses_comments_and_timeline_read_this_pull_request(net: Network) -> None:
    net.rest[f"/repos/o/r/commits/{HEAD}/statuses"] = {"pages": {1: []}}
    net.rest["/repos/o/r/issues/7/comments"] = {"pages": {1: [{"user": None, "body": "x", "created_at": AFTER_HEAD}]}}
    net.rest["/repos/o/r/issues/7/timeline"] = {"pages": {1: [{"event": "base_ref_changed", "created_at": RETARGETED}]}}
    assert review_check.fetch_statuses("tok", "o/r", HEAD) == []
    assert review_check.fetch_comments("tok", "o/r", 7) == [
        {"login": None, "user_type": None, "body": "x", "created_at": AFTER_HEAD, "updated_at": None}
    ]
    assert review_check.fetch_base_change_date("tok", "o/r", 7) == RETARGETED
    _authorized(net)


def test_fetch_reviews_keeps_every_field_the_gate_decides_on(net: Network) -> None:
    net.rest["/repos/o/r/pulls/7/reviews"] = {
        "pages": {
            1: [
                {
                    "id": 11,
                    "node_id": "N1",
                    "user": {"login": REVIEW, "type": "Bot"},
                    "commit_id": HEAD,
                    "state": "COMMENTED",
                    "submitted_at": HEAD_DATE,
                    "body": "b",
                    "html_url": "ignored",
                },
                {"id": 12, "user": None},
            ]
        }
    }
    assert review_check.fetch_reviews("tok", "o/r", 7) == [
        {
            "login": REVIEW,
            "user_type": "Bot",
            "commit_id": HEAD,
            "state": "COMMENTED",
            "submitted_at": HEAD_DATE,
            "body": "b",
            "node_id": "N1",
            "id": 11,
        },
        {
            "login": None,
            "user_type": None,
            "commit_id": None,
            "state": None,
            "submitted_at": None,
            "body": None,
            "node_id": None,
            "id": 12,
        },
    ]
    _authorized(net)


def test_a_review_edit_lookup_sends_the_node_and_returns_both_flags(net: Network) -> None:
    net.graphql = lambda body: {"data": {"node": {"lastEditedAt": AFTER_HEAD, "includesCreatedEdit": True}}}
    assert review_check.fetch_review_edit("tok", "N9") == (AFTER_HEAD, True)
    request = net.requests[0]
    assert request["url"] == f"{API}/graphql"
    assert request["body"]["variables"] == {"id": "N9"}
    assert "PullRequestReview" in request["body"]["query"]
    _authorized(net)


def test_a_review_edit_lookup_with_graphql_errors_fails_even_when_data_is_present(net: Network) -> None:
    net.graphql = lambda body: {
        "errors": [{"message": "x"}],
        "data": {"node": {"lastEditedAt": None, "includesCreatedEdit": False}},
    }
    with pytest.raises(review_check.CheckError):
        review_check.fetch_review_edit("tok", "N9")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"node": {"lastEditedAt": None}}},
        {"data": {"node": {"includesCreatedEdit": False}}},
    ],
    ids=["no-data", "no-node", "no-includes", "no-last-edited"],
)
def test_a_review_edit_lookup_missing_any_field_fails_closed(net: Network, payload: dict[str, Any]) -> None:
    net.graphql = lambda body: payload
    with pytest.raises(review_check.CheckError):
        review_check.fetch_review_edit("tok", "N9")


def test_the_head_commit_is_read_from_graphql(net: Network) -> None:
    net.graphql = lambda body: {"data": {"repository": {"pullRequest": {"headRefOid": HEAD}}}}
    assert review_check.fetch_head_oid("tok", "o/r", 7) == HEAD
    assert net.requests[0]["body"]["variables"] == {"owner": "o", "name": "r", "number": 7}
    net.graphql = lambda body: {
        "errors": [{"message": "x"}],
        "data": {"repository": {"pullRequest": {"headRefOid": HEAD}}},
    }
    with pytest.raises(review_check.CheckError):
        review_check.fetch_head_oid("tok", "o/r", 7)


def _wire(
    net: Network, *, files: list[str], statuses: list[dict[str, Any]], reviews: list[dict[str, Any]], **extra: Any
) -> None:
    net.rest["/repos/o/r/pulls/7"] = {"head": {"sha": HEAD}, "base": {"ref": "develop"}, "changed_files": len(files)}
    net.rest["/repos/o/r/pulls/7/files"] = {"pages": {1: [{"filename": name} for name in files]}}
    net.rest[f"/repos/o/r/commits/{HEAD}/statuses"] = {
        "pages": {
            1: [
                {
                    "id": item["id"],
                    "context": item["context"],
                    "state": item["state"],
                    "description": item["description"],
                    "creator": {"login": item["login"], "type": item["user_type"]},
                    "created_at": item["created_at"],
                }
                for item in statuses
            ]
        }
    }
    net.rest["/repos/o/r/pulls/7/reviews"] = {
        "pages": {
            1: [
                {
                    "id": item["id"],
                    "node_id": item.get("node_id", "N1"),
                    "user": {"login": item["login"], "type": item["user_type"]},
                    "commit_id": item["commit_id"],
                    "state": item["state"],
                    "submitted_at": item["submitted_at"],
                    "body": item["body"],
                }
                for item in reviews
            ]
        }
    }
    net.rest["/repos/o/r/issues/7/comments"] = {"pages": {1: extra.get("comments", [])}}
    net.rest["/repos/o/r/issues/7/timeline"] = {"pages": {1: extra.get("timeline", [])}}

    net.graphql = lambda body: {"data": {"node": {"lastEditedAt": None, "includesCreatedEdit": False}}}


def _run(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(gate, "PULL_READ_BACKOFF_SECONDS", 0)
    return gate.main(["--repo", "o/r", "--pr", "7", *POLICY_ARGS, "--no-post"])


def test_a_reviewed_pull_request_passes_and_every_call_uses_the_token_and_target(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _wire(net, files=SOUNDNESS, statuses=[_status()], reviews=[_receipt()])
    assert _run(monkeypatch) == review_check.PASS
    assert capsys.readouterr().out == f"{status_text.GATE_PASS_VERDICT}\n"
    _authorized(net)
    assert set(net.paths()) == {
        "/repos/o/r/pulls/7",
        "/repos/o/r/pulls/7/files?per_page=100&page=1",
        f"/repos/o/r/commits/{HEAD}/statuses?per_page=100&page=1",
        "/graphql",
        "/repos/o/r/issues/7/timeline?per_page=100&page=1",
        "/repos/o/r/issues/7/comments?per_page=100&page=1",
        "/repos/o/r/pulls/7/reviews?per_page=100&page=1",
    }
    assert len(net.requests) == 7
    bodies = [request["body"] for request in net.requests if request["body"] is not None]
    assert not any("reviewThreads" in body["query"] for body in bodies)
    assert [body["variables"] for body in bodies] == [{"id": "N1"}]


def test_only_exempt_paths_pass_without_a_verdict(net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    _wire(net, files=["NOTES.md"], statuses=[], reviews=[])
    assert _run(monkeypatch) == review_check.PASS
    assert capsys.readouterr().out == f"{status_text.GATE_PASS_EXEMPT}\n"


def test_the_reviewer_policy_gate_paths_keep_an_otherwise_exempt_change_under_review(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _wire(net, files=["_project/scripts/reviewers/templates/gate.md"], statuses=[], reviews=[])
    assert _run(monkeypatch) == review_check.OUTSTANDING
    assert capsys.readouterr().out == f"{status_text.gate_waiting(HEAD)}\n"


def test_a_retarget_voids_the_earlier_verdict_and_receipt_in_the_main_flow(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    timeline = [{"event": "base_ref_changed", "created_at": RETARGETED}]
    _wire(net, files=SOUNDNESS, statuses=[_status()], reviews=[_receipt()], timeline=timeline)
    assert _run(monkeypatch) == review_check.OUTSTANDING
    assert capsys.readouterr().out == f"{status_text.gate_waiting(HEAD)}\n"
    assert [
        request["body"] for request in net.requests if request["body"] and "id" in request["body"].get("variables", {})
    ] == []


def test_a_retarget_after_the_receipt_keeps_a_later_verdict_and_receipt(
    net: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeline = [{"event": "base_ref_changed", "created_at": RETARGETED}]
    _wire(
        net,
        files=SOUNDNESS,
        statuses=[_status(created_at=AFTER_RETARGET)],
        reviews=[_receipt(submitted_at=AFTER_RETARGET)],
        timeline=timeline,
    )
    assert _run(monkeypatch) == review_check.PASS


def test_a_standin_attestation_is_read_from_the_pull_request_comments(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    comment = {
        "user": {"login": "joeharris76", "type": "User"},
        "body": f"Stand-in review: APPROVE {HEAD}",
        "created_at": AFTER_HEAD,
        "updated_at": AFTER_HEAD,
    }
    _wire(net, files=SOUNDNESS, statuses=[], reviews=[], comments=[comment])
    assert _run(monkeypatch) == review_check.PASS
    assert capsys.readouterr().out == f"{status_text.gate_pass_standin('joeharris76')}\n"
    _authorized(net)


def test_a_github_failure_is_reported_on_stderr_as_an_error(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    net.error = review_check.CheckError("GitHub returned HTTP 500 for /x")
    assert _run(monkeypatch) == review_check.ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("review-gate: error: ")
    assert "500" in captured.err


def test_the_policy_exempt_paths_are_applied_to_the_pull_request_files(
    net: Network, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    policy = replace(CONFIG.policy, classifier=replace(CONFIG.policy.classifier, exempt_paths=("zz_exempt/*",)))
    monkeypatch.setattr(gate, "load_gate_config", lambda policy_path, root: replace(CONFIG, policy=policy))
    _wire(net, files=["zz_exempt/a.txt"], statuses=[], reviews=[])
    assert _run(monkeypatch) == review_check.PASS
    assert capsys.readouterr().out == f"{status_text.GATE_PASS_EXEMPT}\n"


def _open_findings_status(description: str = "large: SHIP WITH FIXES, 2 defect(s) to fix") -> dict[str, Any]:
    return _status("failure", created_at=RETARGETED, description=description)


def test_a_standin_is_rejected_on_a_head_whose_latest_decisive_verdict_lists_open_findings() -> None:
    code, message = _decide(statuses=[_open_findings_status()], comments=[_standin(AFTER_RETARGET)])
    assert code == review_check.WAITING
    assert "cannot waive" in message and "stand-in attestation" not in message


def test_a_carried_ship_with_fixes_verdict_also_rejects_a_standin() -> None:
    carried = _open_findings_status("carried from aaaaaaaaaaaa: SHIP WITH FIXES")
    assert _decide(statuses=[carried], comments=[_standin(AFTER_RETARGET)])[0] == review_check.WAITING


@pytest.mark.parametrize(
    "description",
    [
        "large: DO NOT SHIP",
        "pending: too large for the review",
        "refused after 3 DO NOT SHIP decisions: open a new one",
    ],
)
def test_a_standin_still_covers_a_head_without_open_findings(description: str) -> None:
    status = _status("failure", created_at=RETARGETED, description=description)
    assert _decide(statuses=[status], comments=[_standin(AFTER_RETARGET)])[0] == review_check.PASS


def test_a_standin_still_covers_absence() -> None:
    assert _decide(statuses=[], comments=[_standin(AFTER_RETARGET)])[0] == review_check.PASS


def test_a_standin_still_covers_an_edit_hold_even_with_open_findings() -> None:
    hold = _status("pending", created_at=AFTER_RETARGET, description="a review was edited")
    standin = _standin("2026-10-01T16:00:00Z")
    assert _decide(statuses=[_open_findings_status(), hold], comments=[standin])[0] == review_check.PASS


def test_a_later_non_hold_pending_status_does_not_lift_the_open_findings_rejection() -> None:
    later = _status("pending", created_at=AFTER_RETARGET, description="pending: no reviewer available")
    standin = _standin("2026-10-01T16:00:00Z")
    assert _decide(statuses=[_open_findings_status(), later], comments=[standin])[0] == review_check.WAITING


def test_an_escalated_finding_names_the_exact_owner_action() -> None:
    escalated = _open_findings_status("large: SHIP WITH FIXES, 2 defect(s) to fix; escalated D1,D3")
    code, message = _decide(statuses=[escalated], comments=[_standin(AFTER_RETARGET)])
    assert code == review_check.WAITING
    assert "D1,D3" in message and "Review adjudication: WITHDRAW|MAINTAIN D<n> - <reason>" in message


def test_a_reposted_status_does_not_invalidate_a_standin_posted_before_it() -> None:
    failing = _status("failure", created_at=RETARGETED, description="large: DO NOT SHIP")
    reposted = _status("failure", created_at="2026-10-01T16:00:00Z", description="large: DO NOT SHIP (reposted)")
    standin = _standin(AFTER_RETARGET)
    assert _decide(statuses=[failing, reposted], comments=[standin])[0] == review_check.PASS


def test_a_genuinely_new_decisive_status_still_invalidates_an_earlier_standin() -> None:
    failing = _status("failure", created_at=RETARGETED, description="large: DO NOT SHIP")
    newer = _status("failure", created_at="2026-10-01T16:00:00Z", description="large: DO NOT SHIP")
    assert _decide(statuses=[failing, newer], comments=[_standin(AFTER_RETARGET)])[0] == review_check.WAITING


def test_only_a_head_with_no_verdict_status_and_no_standin_is_outstanding() -> None:
    code, message = _decide(statuses=[])
    assert code == review_check.OUTSTANDING
    assert message == status_text.gate_waiting(HEAD)


@pytest.mark.parametrize(
    "statuses",
    [
        pytest.param([_status("pending", description="pending: no reviewer available")], id="pending-verdict"),
        pytest.param([_status("pending", description="a review was edited")], id="edit-hold"),
        pytest.param([_status("pending", description="pending: sweep retry queued")], id="sweep-pending-verdict"),
        pytest.param([_status("failure", description="large: DO NOT SHIP")], id="do-not-ship"),
        pytest.param(
            [_status("failure", description="large: SHIP WITH FIXES, 2 defect(s) to fix")], id="open-findings"
        ),
        pytest.param(
            [_status("failure", description="large: SHIP WITH FIXES, 2 defect(s) to fix; escalated D1")], id="escalated"
        ),
        pytest.param(
            [_status("failure", description="refused after 3 DO NOT SHIP decisions: open a new one")], id="refused"
        ),
        pytest.param([_status("error", description="large: error")], id="error"),
        pytest.param([_status("success", description="only exempt paths changed")], id="stale-exempt"),
        pytest.param([_status("success")], id="unmatched-success"),
    ],
)
def test_every_other_waiting_branch_stays_waiting(statuses: list[dict[str, Any]]) -> None:
    kwargs: dict[str, Any] = {"statuses": statuses, "reviews": []}
    assert _decide(**kwargs)[0] == review_check.WAITING


def _decide_with(**over: Any) -> tuple[int, str]:
    return review_check.decide(
        HEAD,
        SOUNDNESS,
        [],
        review_check.path_matcher(
            2, 2, CONFIG.matchers, CONFIG.policy.classifier.exempt_paths, CONFIG.policy.classifier.gate_paths
        ),
        None,
        [],
        binding=BINDING,
        reviews=[],
        **over,
    )


def test_a_refused_omp_pin_bump_stays_waiting_even_without_a_verdict() -> None:
    assert _decide_with(bump_refusals=["pin moved"])[0] == review_check.WAITING


def test_a_refused_promotion_stays_waiting_even_without_a_verdict() -> None:
    assert _decide_with(promotion_refusals=["lock changed"])[0] == review_check.WAITING
