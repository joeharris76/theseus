from __future__ import annotations

import pytest

from theseus_reviewer.dedup import fingerprint, marker, open_fingerprints, open_thread_counts, split, thread_fingerprint
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
    counts = {fingerprint(PATH, "Unchecked size"): 1}
    assert split((first, second), prior, counts) == ((second,), (first,))
    assert split((first, second, third), prior, {fingerprint(PATH, "Unchecked size"): 2}) == ((third,), (first, second))


def test_open_thread_counts_count_every_open_thread_of_a_fingerprint() -> None:
    threads = [
        _open("benchbox-review", "**High**: Unchecked size"),
        _open("benchbox-review", "**High**: Unchecked size"),
        {**_open("benchbox-review", "**High**: Unchecked size"), "resolved": True},
        _open("benchbox-review", "**High**: Other"),
        _open("benchbox-review", "**High**: Unchecked size\n\n<!-- review-defect: c1-D1 -->"),
    ]
    assert open_thread_counts(threads, "benchbox-review") == {
        fingerprint(PATH, "Unchecked size"): 2,
        fingerprint(PATH, "Other"): 1,
    }


def test_a_new_location_gets_its_own_thread_beside_an_open_thread_of_a_marked_defect() -> None:
    marked = _open(
        "benchbox-review",
        f"**High**: Unchecked size\n\n<!-- review-defect: c1-D1 -->\n{marker(_finding('Unchecked size'))}",
    )
    key = fingerprint(PATH, "Unchecked size")
    assert open_fingerprints([marked], "benchbox-review") == {key: "High"}
    counts = open_thread_counts([marked], "benchbox-review")
    assert counts == {}
    new = _finding("Unchecked size", 40)
    assert split((new,), {key: "High"}, counts) == ((new,), ())
