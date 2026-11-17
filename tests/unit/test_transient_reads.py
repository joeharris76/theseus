from __future__ import annotations

import email.message
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from theseus_reviewer import github, review_check, status_text, transient_reads

pytestmark = [pytest.mark.unit, pytest.mark.fast]

TIMED_OUT = urllib.error.URLError(TimeoutError("timed out"))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(transient_reads, "sleep", slept.append)
    return slept


class FakeGh:
    def __init__(self, results: list[tuple[int, str]]) -> None:
        self.results: Iterator[tuple[int, str]] = iter(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        code, text = next(self.results)
        return subprocess.CompletedProcess(argv, code, text.encode() if code == 0 else b"", text.encode())


def _gh(monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, str]]) -> FakeGh:
    fake = FakeGh(results)
    monkeypatch.setattr(github, "_run_api", fake)
    return fake


def test_a_gh_read_survives_one_503(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    fake = _gh(monkeypatch, [(1, "gh: HTTP 503"), (0, '{"number": 7}')])

    assert github.get_json("repos/o/r/pulls/7") == {"number": 7}
    assert len(fake.calls) == 2
    assert sleeps == [transient_reads.RETRY_DELAYS_SECONDS[0]]


def test_a_gh_read_that_stays_unavailable_fails_naming_the_read(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    attempts = len(transient_reads.RETRY_DELAYS_SECONDS) + 1
    fake = _gh(monkeypatch, [(1, "gh: HTTP 503")] * attempts)

    with pytest.raises(github.GitHubError, match=r"repos/o/r/pulls/7 failed: gh: HTTP 503"):
        github.get_json("repos/o/r/pulls/7")
    assert len(fake.calls) == attempts
    assert sleeps == list(transient_reads.RETRY_DELAYS_SECONDS)


@pytest.mark.parametrize("stderr", ["gh: HTTP 404", "gh: HTTP 403: Resource not accessible by integration"])
def test_a_permanent_gh_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], stderr: str
) -> None:
    fake = _gh(monkeypatch, [(1, stderr)])

    with pytest.raises(github.GitHubError):
        github.get_json("repos/o/r/pulls/7")
    assert len(fake.calls) == 1
    assert sleeps == []


def test_a_gh_write_is_never_repeated(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    fake = _gh(monkeypatch, [(1, "gh: HTTP 503")])

    with pytest.raises(github.GitHubError):
        github._gh("--method", "POST", "repos/o/r/issues/7/comments", "-f", "body=x")
    assert len(fake.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("args", "read"),
    [
        (("repos/o/r/pulls/7",), True),
        (("--paginate", "--slurp", "repos/o/r/pulls/7/reviews"), True),
        (("graphql", "-f", "query=query { viewer { login } }"), True),
        (("graphql", "-f", "query= mutation { minimizeComment }"), False),
        (("graphql", "-f", "query=# minimize\nmutation { minimizeComment }"), False),
        (("graphql", "--input", "-"), False),
        (("--method=POST", "repos/o/r/issues/7/comments"), False),
        (("-XPATCH", "repos/o/r/check-runs/1"), False),
        (("repos/o/r/statuses/abc", "--field=state=success"), False),
        (("repos/o/r/statuses/abc", "-f", "state=success"), False),
        (("repos/o/r/check-runs", "--input", "-"), False),
        (("-X", "PATCH", "repos/o/r/check-runs/1"), False),
        (("graphql", "-fquery=mutation { minimizeComment }"), False),
        (("graphql", "--field=query=mutation { x }"), False),
        (("graphql", "--raw-field=query=query { viewer { login } }"), True),
        (("graphql", "-F", "query=@mutation.graphql"), False),
        (("graphql", "-f", "owner=o"), False),
        (("graphql", "--input=-"), False),
        (("graphql", "-f", "query=query { a }", "-f", "query=mutation { b }"), False),
    ],
)
def test_only_gh_reads_are_retryable(args: tuple[str, ...], read: bool) -> None:
    assert transient_reads.gh_call_is_read(args) is read


class FakeOpen:
    def __init__(self, outcomes: list[Exception | bytes]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0

    def __call__(self, request: urllib.request.Request) -> bytes:
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "error", email.message.Message(), None)


def _urlopen(monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | bytes]) -> FakeOpen:
    fake = FakeOpen(outcomes)
    monkeypatch.setattr(transient_reads, "_read_once", fake)
    return fake


def test_a_gate_read_survives_one_timeout(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    fake = _urlopen(monkeypatch, [TIMED_OUT, b'{"number": 7}'])

    assert review_check.fetch_pull("token", "o/r", 7) == {"number": 7}
    assert fake.calls == 2
    assert sleeps == [transient_reads.RETRY_DELAYS_SECONDS[0]]


def test_a_gate_read_that_keeps_timing_out_fails_with_a_status_naming_the_endpoint(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = _urlopen(monkeypatch, [TIMED_OUT] * (len(transient_reads.RETRY_DELAYS_SECONDS) + 1))

    with pytest.raises(review_check.CheckError) as raised:
        review_check.fetch_pull("token", "o/r", 7)
    description = status_text.gate_error(raised.value)

    assert fake.calls == len(transient_reads.RETRY_DELAYS_SECONDS) + 1
    assert "GitHub request to /repos/o/r/pulls/7 failed" in description
    assert "to failed" not in description
    assert len(description) <= status_text.DESCRIPTION_LIMIT


def test_a_gate_read_retries_5xx_but_not_4xx(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    fake = _urlopen(monkeypatch, [_http(502), b"policy"])
    assert review_check.fetch_text("token", "o/r", ".github/review.yml", "develop") == "policy"
    assert fake.calls == 2

    missing = _urlopen(monkeypatch, [_http(404)])
    assert review_check.fetch_optional_bytes("token", "o/r", ".github/review.yml", "develop") is None
    assert missing.calls == 1


def test_a_graphql_mutation_through_the_gate_is_never_repeated(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = _urlopen(monkeypatch, [_http(503)])

    with pytest.raises(review_check.CheckError):
        review_check._request("token", "https://api.github.com/graphql", {"query": "mutation { x }"})
    assert fake.calls == 1
    assert sleeps == []
