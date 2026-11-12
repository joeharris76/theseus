from __future__ import annotations

from typing import Any

import pytest

from theseus_reviewer import cli, dedup, layout, protocol, report
from theseus_reviewer.verdict import Finding

from .test_attempts import CHECKER

pytestmark = [pytest.mark.unit, pytest.mark.fast]

TITLE = "Identity comparison replaces equality"
DETAIL = "checker returns a is b, so equal values held in distinct objects compare unequal."
FINDING = Finding("High", CHECKER, 2, TITLE, DETAIL)
COMMENTABLE = {CHECKER: frozenset({1, 2})}


def _old_thread(defect_id: str = "D1") -> str:
    """A thread as released v0.2.8 posts it."""
    return f"**High**: {TITLE}\n\n{DETAIL}\n\n<!-- review-defect: c1-{defect_id} -->\n\n{dedup.marker(FINDING)}"


def _new_thread(defect_id: str = "D1") -> str:
    return report._review_comment(FINDING, COMMENTABLE, defect_id, 1)["body"]


def _thread(body: str, **over: Any) -> dict[str, Any]:
    return {
        "resolved": False,
        "edited": False,
        "author": "benchbox-review[bot]",
        "author_type": "Bot",
        "path": CHECKER,
        "body": body,
        "replies": [],
        **over,
    }


def test_a_new_thread_shows_the_mark_severity_id_and_dispute_footer_and_keeps_the_markers() -> None:
    assert _new_thread() == (
        f"🟠 **High** · D1 · {TITLE}\n\n{DETAIL}\n\n"
        "<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>\n\n"
        f"<!-- review-defect: c1-D1 -->\n\n{dedup.marker(FINDING)}"
    )


@pytest.mark.parametrize(("severity", "mark"), [("Critical", "🔴"), ("High", "🟠"), ("Medium", "🟡"), ("Low", "🔵")])
def test_every_severity_has_its_mark_and_always_its_word(severity: str, mark: str) -> None:
    body = report._review_comment(Finding(severity, CHECKER, 2, TITLE, ""), COMMENTABLE, "D7", 3)["body"]
    assert body.splitlines()[0] == f"{mark} **{severity}** · D7 · {TITLE}"
    assert "<!-- review-defect: c3-D7 -->" in body


def test_a_thread_without_a_recorded_id_keeps_the_plain_title_and_has_nothing_to_contest() -> None:
    body = report._review_comment(FINDING, COMMENTABLE)["body"]
    assert body == f"**High**: {TITLE}\n\n{DETAIL}\n\n{dedup.marker(FINDING)}"


def test_the_old_and_new_thread_formats_have_one_fingerprint_for_one_finding() -> None:
    expected = dedup.fingerprint(CHECKER, TITLE)
    for body in (_old_thread(), _new_thread()):
        assert dedup.thread_fingerprint(CHECKER, body) == expected
        assert dedup.parse_title(body) == ("High", TITLE)
        assert dedup.first_line_title(body) == TITLE
    legacy_without_marker = f"**High**: {TITLE}\n\n{DETAIL}"
    new_without_marker = _new_thread().split("\n\n<!-- review-defect")[0]
    assert dedup.thread_fingerprint(CHECKER, legacy_without_marker) == expected
    assert dedup.thread_fingerprint(CHECKER, new_without_marker) == expected


@pytest.mark.parametrize("body", [_old_thread(), _new_thread()], ids=["old", "new"])
def test_an_open_thread_in_either_format_suppresses_the_same_finding_posted_again(body: str) -> None:
    threads = [_thread(body)]
    known = dedup.open_fingerprints(threads, "benchbox-review")
    assert known == {dedup.fingerprint(CHECKER, TITLE): "High"}
    fresh, repeated = dedup.split([FINDING], known)
    assert (fresh, repeated) == ((), (FINDING,))


def test_both_formats_in_one_pull_request_keep_one_entry() -> None:
    threads = [_thread(_old_thread("D1")), _thread(_new_thread("D2"))]
    assert dedup.open_fingerprints(threads, "benchbox-review") == {dedup.fingerprint(CHECKER, TITLE): "High"}


@pytest.mark.parametrize("body", [_old_thread(), _new_thread()], ids=["old", "new"])
def test_a_disputed_thread_in_either_format_gives_the_same_id_and_the_bare_title(body: str) -> None:
    reply = {"author": "owner", "association": "OWNER", "author_type": "User", "body": "Dispute: see x.py:1"}
    [disputed] = cli._disputed_threads([_thread(body, replies=[reply])], "benchbox-review")
    assert (disputed["id"], disputed["cycle"], disputed["path"]) == ("D1", 1, CHECKER)
    assert disputed["title"] == TITLE


def test_the_dispute_footer_is_never_a_dispute_trigger() -> None:
    footer = layout.THREAD_FOOTER
    assert "Dispute:" in footer
    assert not protocol.is_dispute(footer)
    assert not protocol.is_dispute(_new_thread())
    assert not protocol.is_comment_dispute_trigger(footer)
    assert protocol.is_dispute("Dispute: counter-evidence at x.py:1")
    quoted_by_the_bot = _thread(
        _new_thread(), replies=[{"author": "benchbox-review[bot]", "author_type": "Bot", "body": footer}]
    )
    assert cli._disputed_threads([quoted_by_the_bot], "benchbox-review") == []
    echoed = {"author": "owner", "association": "OWNER", "author_type": "User", "body": _new_thread()}
    assert cli._disputed_threads([_thread(_new_thread(), replies=[echoed])], "benchbox-review") == []


def test_the_defect_id_marker_is_read_from_a_new_thread() -> None:
    assert protocol.thread_finding(_new_thread("D12")) == (1, "D12")
    assert protocol.thread_finding_id(_new_thread("D12")) == "D12"
