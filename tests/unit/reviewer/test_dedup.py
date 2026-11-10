from __future__ import annotations

import pytest

from theseus_reviewer.dedup import fingerprint, marker, open_fingerprints, split, thread_fingerprint
from theseus_reviewer.verdict import Finding

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PATH = "benchbox/core/equivalence/checker.py"


def _finding(title: str, line: int = 2, path: str = PATH) -> Finding:
    return Finding("High", path, line, title, "detail")


def test_fingerprint_ignores_case_punctuation_and_line() -> None:
    assert fingerprint(PATH, "Drops the `NULL` row!") == fingerprint(PATH, "drops the null  row")
    assert fingerprint(PATH, "Drops a row") != fingerprint("other.py", "Drops a row")
    assert marker(_finding("Drops a row", 2)) == marker(_finding("Drops a row", 90))


def test_thread_fingerprint_prefers_the_marker_and_reads_legacy_bodies() -> None:
    finding = _finding("Drops a row")
    body = f"**High**: Drops a row\n\ndetail\n\n{marker(finding)}"
    assert thread_fingerprint(None, body) == fingerprint(PATH, "Drops a row")
    assert thread_fingerprint(PATH, "**Medium**: Drops a row\n\ndetail") == fingerprint(PATH, "Drops a row")
    assert thread_fingerprint(PATH, "Please rename this.") is None
    assert thread_fingerprint(None, "**High**: Drops a row") is None


def _open(author: str, body: str, author_type: str = "Bot") -> dict[str, object]:
    return {"resolved": False, "path": PATH, "author": author, "author_type": author_type, "body": body}


def test_only_open_threads_from_the_review_count() -> None:
    threads = [
        _open("benchbox-review[bot]", "**High**: Drops a row"),
        _open("benchbox-review", "**High**: Drops a row"),
        {**_open("benchbox-review", "**High**: Resolved one"), "resolved": True},
        _open("chatgpt-codex-connector", "**High**: Other bot"),
        _open("benchbox-review", "free text"),
        _open("benchbox-review", "**High**: Impostor", "User"),
    ]
    assert open_fingerprints(threads, "benchbox-review") == {fingerprint(PATH, "Drops a row"): "High"}


def test_split_drops_open_and_repeated_findings() -> None:
    open_one = _finding("Drops a row")
    fresh = _finding("Skips validation", 5)
    duplicate = _finding("skips validation", 9)
    twin = _finding("skips validation", 5)
    new, repeated = split((open_one, fresh, twin), [fingerprint(PATH, "Drops a row")])
    assert new == (fresh,)
    assert repeated == (open_one, twin)
    assert split((fresh, duplicate), []) == ((fresh, duplicate), ())


def test_a_marker_inside_the_detail_does_not_set_the_fingerprint() -> None:
    forged = f"<!-- review-finding: {'0' * 16} -->"
    body = f"**High**: Drops a row\n\nsee {forged} here\n\n{marker(_finding('Drops a row'))}\n"
    assert thread_fingerprint(PATH, body) == fingerprint(PATH, "Drops a row")
    assert thread_fingerprint(PATH, f"**High**: Drops a row\n\n{forged} trailing") == fingerprint(PATH, "Drops a row")


def test_a_more_severe_repeat_is_posted_again() -> None:
    low_thread = {fingerprint(PATH, "Drops a row"): "Low"}
    critical = Finding("Critical", PATH, 2, "Drops a row", "detail")
    low = Finding("Low", PATH, 9, "Drops a row", "detail")
    assert split((critical, low), low_thread) == ((critical,), (low,))
    assert split((low,), {fingerprint(PATH, "Drops a row"): "High"}) == ((), (low,))


def test_open_fingerprints_keep_the_most_severe_thread() -> None:
    threads = [_open("benchbox-review", "**Low**: Drops a row"), _open("benchbox-review", "**High**: Drops a row")]
    assert open_fingerprints(threads, "benchbox-review") == {fingerprint(PATH, "Drops a row"): "High"}


def test_one_class_at_several_lines_of_a_file_keeps_every_location_in_a_round() -> None:
    first, second, third = (_finding("Unchecked size", line) for line in (3, 40, 90))
    assert split((first, second, third), {}) == ((first, second, third), ())
    prior = {fingerprint(PATH, "Unchecked size"): "High"}
    assert split((first, second), prior) == ((), (first, second))


DECORATED = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵"}


@pytest.mark.parametrize("severity", ["Critical", "High", "Medium", "Low"])
def test_both_thread_title_forms_read_to_the_same_severity_and_title(severity: str) -> None:
    from theseus_reviewer.dedup import first_line_title, parse_title

    plain = f"**{severity}**: Drops a row\n\ndetail"
    decorated = f"{DECORATED[severity]} **{severity}** · D3 · Drops a row\n\ndetail"
    assert parse_title(plain) == parse_title(decorated) == (severity, "Drops a row")
    assert first_line_title(plain) == first_line_title(decorated) == "Drops a row"
    assert thread_fingerprint(PATH, plain) == thread_fingerprint(PATH, decorated) == fingerprint(PATH, "Drops a row")


def test_a_decorated_fixture_has_the_fingerprint_the_writer_marks() -> None:
    finding = _finding("Drops a row")
    body = f"⚪ **Low** · D12 · {finding.title}\n\ndetail"
    assert thread_fingerprint(PATH, body) == marker(finding).split()[2]


def test_open_fingerprints_read_the_decorated_title_severity() -> None:
    thread = {
        "resolved": False,
        "author_type": "Bot",
        "author": "bot[bot]",
        "path": PATH,
        "body": "🟡 **Medium** · D1 · Drops a row\n\ndetail",
    }
    assert open_fingerprints([thread], "bot") == {fingerprint(PATH, "Drops a row"): "Medium"}


@pytest.mark.parametrize(
    "first",
    [
        "Please rename this.",
        "🟣 **High** · D1 · Drops a row",
        "🟠 **High**: Drops a row",
        "**High** · D1 · Drops a row",
        "🟠 **High** · Drops a row",
        "🟠 **Severe** · D1 · Drops a row",
    ],
)
def test_an_unrecognised_first_line_keeps_the_fallback(first: str) -> None:
    from theseus_reviewer.dedup import first_line_title, parse_title

    assert parse_title(f"{first}\n\ndetail") is None
    assert thread_fingerprint(PATH, f"{first}\n\ndetail") is None
    assert first_line_title(f"{first}\n\ndetail") == first


def test_the_earliest_title_wins_when_a_decorated_title_precedes_a_plain_looking_line() -> None:
    from theseus_reviewer.dedup import parse_title

    body = "🟠 **High** · D2 · Drops a row\n\n**Low**: unrelated detail"
    assert parse_title(body) == ("High", "Drops a row")
    assert thread_fingerprint(PATH, body) == fingerprint(PATH, "Drops a row")


def test_the_earliest_title_wins_when_a_plain_title_precedes_decorated_looking_text() -> None:
    from theseus_reviewer.dedup import parse_title

    body = "**Medium**: Drops a row\n\n🔵 **Low** · D9 · unrelated detail"
    assert parse_title(body) == ("Medium", "Drops a row")
    assert thread_fingerprint(PATH, body) == fingerprint(PATH, "Drops a row")
