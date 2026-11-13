from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import dedup, layout, report, selection
from theseus_reviewer.policy import Policy, PolicyError
from theseus_reviewer.verdict import Finding, validate

from .test_attempts import CHECKER, _v
from .test_policy import _raw, parse_policy
from .test_report_outcomes import _defect, _dispositions, _dispute_plan, _dispute_verdict, _finalize
from .test_review_thread_format import _thread
from .test_rounds import _protocol_plan

pytestmark = [pytest.mark.unit, pytest.mark.fast]

CHECKBOX = re.compile(r"^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+\[[ xX]\]", re.MULTILINE)
COMMENTABLE = {CHECKER: frozenset(range(1, 6))}
HOSTILE_LINES = [
    "- [ ] tick me",
    "1. [x] done",
    "> - [ ] quoted",
    "<b>bold</b> <a href='x'>link</a> <details>",
    "# heading",
    "- # heading in a list",
    "1. ## numbered heading",
    "___",
    "[ref]: /relative",
    "> [ref2]: /relative",
    "- [ref3]: /relative",
    "setext\n===",
    "<!-- hidden -->",
]
HOSTILE = "\n".join(HOSTILE_LINES)
FENCES = "```\n- [ ] inside\n```\n- [ ] after\n~~~\n# open fence left unclosed"
HEADING = re.compile(r"^(?:[ \t>]*(?:[-*+]|\d+[.)])[ \t]+)*#{1,6}(?:\s|$)")
DEFINITION = re.compile(r"^(?:[ \t>]*(?:[-*+]|\d+[.)])[ \t]+)*\[[^\]]*\]:")


def _thread_body(title: str, detail: str, defect_id: str | None = "D1") -> str:
    return report._review_comment(Finding("High", CHECKER, 2, title, detail), COMMENTABLE, defect_id)["body"]


def _prose(body: str) -> str:
    """The body outside fenced code, which is where GitHub interprets markdown."""
    return "\n".join(line for code, line in layout._scan(body)[0] if not code)


@pytest.mark.parametrize("defect_id", ["D1", None])
@pytest.mark.parametrize("detail", [*HOSTILE_LINES, HOSTILE], ids=[*HOSTILE_LINES, "all"])
def test_hostile_detail_in_a_thread_is_inert(detail: str, defect_id: str | None) -> None:
    body = _thread_body("Plain title", detail, defect_id)
    assert layout._scan(body)[1] is None
    prose = _prose(body)
    assert not CHECKBOX.search(prose)
    for found in ("<b>", "<a ", "<details", "<!-- hidden"):
        assert found not in body
    for line in prose.splitlines():
        assert not HEADING.match(line), line
        assert not DEFINITION.match(line), line


@pytest.mark.parametrize("defect_id", ["D1", None])
def test_fences_in_thread_detail_never_leave_text_in_an_open_block(defect_id: str | None) -> None:
    body = _thread_body("Plain title", FENCES, defect_id)
    assert layout._scan(body)[1] is None
    prose = _prose(body)
    assert not CHECKBOX.search(prose) and "# open fence" not in prose
    assert "`[ ]` after" in prose


@pytest.mark.parametrize(
    "title",
    ["- [ ] ticked", "<b>bold</b>", "```", "# heading", "[x]: /relative", "<a href=x>t</a>", "line\n- [ ] two", "___"],
)
def test_hostile_title_in_a_thread_is_inert(title: str) -> None:
    for defect_id in ("D1", None):
        first = _thread_body(title, "", defect_id).split("\n\n")[0]
        assert "\n" not in first
        assert not CHECKBOX.search(first) and "<" not in first
        assert "```" not in first or "\\```" in first


def test_a_hostile_title_still_matches_the_fingerprint_a_v0_2_11_thread_carries() -> None:
    title = "- [ ] <b>Identity</b> comparison"
    finding = Finding("High", CHECKER, 2, title, "d")
    released = f"🟠 **High** · D1 · {title}\n\nd\n\n<!-- review-defect: c1-D1 -->\n\n{dedup.marker(finding)}"
    fresh = _thread_body(title, "d")
    assert fresh != released
    for body in (released, fresh):
        assert dedup.thread_fingerprint(CHECKER, body) == dedup.fingerprint(CHECKER, title)
        known = dedup.open_fingerprints([_thread(body)], "benchbox-review")
        assert dedup.split([finding], known) == ((), (finding,))
        assert dedup.open_thread_counts([_thread(body)], "benchbox-review") == {}


def test_the_dispute_evidence_of_a_review_body_is_inert(policy: Policy) -> None:
    verdict = _dispute_verdict()
    verdict["prior_defects"] = [
        {"id": item["id"], "status": item["status"], "evidence": HOSTILE} for item in verdict["prior_defects"]
    ]
    final = _finalize(policy, _dispute_plan(policy), verdict)
    shown = _dispositions(final)
    assert set(shown) == {"D1", "D2", "D3", "D4", "D5"}
    body = final.review["body"] if final.review else final.body
    start = body.index("**Dispute dispositions**")
    section = body[start : body.index("\n\n", body.index("\n\n", start) + 2)]
    assert len(section.splitlines()) == 7
    for line in section.splitlines()[2:]:
        assert line.startswith("- D")
    assert not CHECKBOX.search("\n".join(section.splitlines()[2:]).replace("- D", "", 5))
    for found in ("<b>", "<a ", "<details", "<!-- hidden"):
        assert found not in section
    assert "`[ ]` tick me" in section and "&lt;b>" in section and "\\- " in section


def test_headings_inside_list_items_and_underscore_rules_are_escaped() -> None:
    out = layout.neutralize("- # one\n1. ## two\n> - ### three\n  * #### four")
    assert out == "- \\# one\n1. \\## two\n> - \\### three\n  * \\#### four"
    assert layout.neutralize_inline("___ rule") == "\\___ rule"
    assert layout.neutralize_inline("_ _ _") == "\\_ _ _"


def test_a_fence_run_in_an_indented_code_block_is_left_alone_and_every_other_fence_is_escaped() -> None:
    assert layout.neutralize("text\n\n    ```\n    code") == "text\n\n    ```\n    code"
    assert layout.neutralize("text\n\n\t```") == "text\n\n\t```"
    for text in (
        "text\n   ```",
        "text\n  ~~~",
        " ```",
        "> ```",
        "- ```",
        "1. ```",
        "- item\n\n    ```",
        "text\n    ```",
        ">     ```",
    ):
        assert layout.neutralize(text) != text, text
        assert "\\`" in layout.neutralize(text) or "\\~" in layout.neutralize(text), text


def test_shown_paths_cannot_open_markdown_inside_code_elements() -> None:
    shown = layout.shown_path("src/a_b*c\\d`e~f[g]<h>&.py")
    assert not set("*_\\`~[<>") & set(shown)
    assert shown == "src/a&#95;b&#42;c&#92;d&#96;e&#126;f&#91;g]&lt;h&gt;&amp;.py"
    line = layout.finding_line("High", "D1", "t", layout.shown_path("a_b.py"))
    assert "<code>a&#95;b.py</code>" in line


def test_reference_links_in_model_text_cannot_be_defined() -> None:
    out = layout.neutralize('[x]: javascript:alert(1)\n\n  [y]: /somewhere "t"\n> [z]: /q')
    assert "\n[" not in "\n" + out
    assert out.count("\\[") == 2 and "`[x]`: javascript" in out
    inline = layout.neutralize_inline('[x]: /a <a href="/b">c</a>')
    assert "<a" not in inline


def _flagged(**keys: bool) -> Policy:
    raw = _raw()
    raw["brief"] = keys
    return parse_policy(raw)


@pytest.mark.parametrize("key", ["structured_findings", "suggestions"])
def test_the_new_brief_flags_default_to_false_and_must_be_booleans(key: str) -> None:
    assert getattr(parse_policy(_raw()).brief, key) is False
    assert getattr(_flagged(**{key: True}).brief, key) is True
    for bad in ("yes", 1, None, []):
        raw = _raw()
        raw["brief"] = {key: bad}
        with pytest.raises(PolicyError, match=f"brief {key} must be a boolean"):
            parse_policy(raw)


def _labelled_plan(policy: Policy, **over: Any) -> dict[str, Any]:
    return {**_protocol_plan(policy), "brief_rules": {"structured_findings": True}, **over}


def _thread_of(final: report.Final) -> str:
    assert final.review is not None
    return final.review["comments"][0]["body"]


STRUCTURED = "Problem: a is b.\n\nImpact: equal values differ.\n\nFix: use ==."


def test_structured_labels_are_bold_only_with_the_flag_and_fall_back_to_plain_detail(policy: Policy) -> None:
    defects = [_defect("T", 2, STRUCTURED)]
    on = _thread_of(_finalize(policy, _labelled_plan(policy), _v("s", defects)))
    assert "**Problem:** a is b.\n\n**Impact:** equal values differ.\n\n**Fix:** use ==." in on
    off = _thread_of(_finalize(policy, _protocol_plan(policy), _v("s", defects)))
    assert STRUCTURED in off and "**Problem:**" not in off
    plain = _thread_of(_finalize(policy, _labelled_plan(policy), _v("s", [_defect("T", 2, "Just prose here.")])))
    assert "\n\nJust prose here.\n\n" in plain and "**" not in plain.split("\n\n", 1)[1].split("<sub>")[0]


def test_labels_inside_a_fence_or_mid_line_stay_plain() -> None:
    assert layout.label_paragraphs("```\nFix: x\n```\nsee Fix: y\nFix: z") == "```\nFix: x\n```\nsee Fix: y\n**Fix:** z"
    assert layout.label_paragraphs("Fixed: no") == "Fixed: no"


def test_a_structured_brief_asks_for_labels_and_short_titles_and_keeps_the_bypass_analysis() -> None:
    from theseus_reviewer.render import finding_rules

    assert finding_rules(structured=False, suggestions=False, gate=True) == ""
    standard = finding_rules(structured=True, suggestions=False, gate=False)
    assert "`Problem:`" in standard and "`Impact:`" in standard and "`Fix:`" in standard and "80 characters" in standard
    gate = finding_rules(structured=True, suggestions=False, gate=True)
    assert gate.startswith(standard) and "one sentence" in gate and "bypass analysis" in gate
    assert "suggestion" not in standard
    assert '"mechanical": true' in finding_rules(structured=False, suggestions=True, gate=False)


# ---- suggestion blocks ----

HEAD_TEXT = "line one\nline two\nline three\nline four\nline five\n"


def _workspace(tmp_path: Path, text: str = HEAD_TEXT) -> Path:
    target = tmp_path / CHECKER
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return tmp_path


def _suggesting(replacement: str | None = "line TWO", line: int = 2, end: int | None = None) -> Finding:
    return Finding("Low", CHECKER, line, "Typo", "A typo.", end, "introduced", replacement)


def _suggest(
    finding: Finding,
    tmp_path: Path,
    *,
    blocked: list[str] | None = (),  # type: ignore[assignment]
    commentable: dict[str, frozenset[int]] | None = None,
    flag: bool = True,
) -> str:
    plan: dict[str, Any] = {"head_sha": "a" * 40}
    if flag:
        plan["brief_rules"] = {"suggestions": True}
    if blocked is not None:
        plan["suggestion_blocked"] = list(blocked)
    return report._review_comment(finding, commentable or COMMENTABLE, "D1", 1, plan, _workspace(tmp_path))["body"]


def test_a_mechanical_finding_inside_the_diff_gets_an_exact_suggestion_block(tmp_path: Path) -> None:
    body = _suggest(_suggesting(), tmp_path)
    assert "A typo.\n\n```suggestion\nline TWO\n```\n\n<sub>Blocks merge" in body
    assert body.endswith(dedup.marker(_suggesting()))
    span = _suggest(_suggesting("a\nb", 2, 3), tmp_path)
    assert "```suggestion\na\nb\n```" in span


def test_no_suggestion_without_the_flag_or_the_replacement(tmp_path: Path) -> None:
    assert "suggestion" not in _suggest(_suggesting(), tmp_path, flag=False)
    assert "suggestion" not in _suggest(_suggesting(None), tmp_path)
    assert "suggestion" not in _suggest(_suggesting(), tmp_path, blocked=None)


@pytest.mark.parametrize(
    "finding",
    [
        _suggesting("line two"),
        _suggesting("x", 6),
        _suggesting("x", 4, 9),
        _suggesting("x", 2, 3),
        _suggesting("\n".join("x" for _ in range(21)), 1, 5),
    ],
    ids=["unchanged", "outside-diff", "span-leaves-diff", "span-not-whole-comment", "too-long"],
)
def test_a_suggestion_is_refused_unless_it_is_exact_inside_the_diff_and_changes_the_head(
    finding: Finding, tmp_path: Path
) -> None:
    commentable = {CHECKER: frozenset({1, 2, 4, 5})} if finding.line == 4 else None
    if finding.end_line == 3:
        commentable = {CHECKER: frozenset({2})}
    body = _suggest(finding, tmp_path, commentable=commentable)
    assert "suggestion" not in body and "A typo." in body and layout.THREAD_FOOTER in body


@pytest.mark.parametrize(
    "path",
    [".github/workflows/review.yml", ".github/review.yml", "pkg/theseus-lock.json", "src/gate.py", "src/sound.py"],
)
def test_a_suggestion_is_never_offered_on_a_blocked_path(path: str, tmp_path: Path) -> None:
    finding = Finding("Low", path, 2, "Typo", "d", None, "introduced", "new")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HEAD_TEXT, encoding="utf-8")
    plan = {"head_sha": "a" * 40, "brief_rules": {"suggestions": True}, "suggestion_blocked": [path]}
    body = report._review_comment(finding, {path: frozenset({2})}, "D1", 1, plan, tmp_path)["body"]
    assert "suggestion" not in body


def test_blocked_paths_cover_gate_soundness_policy_lock_and_workflow_files(policy: Policy) -> None:
    from theseus_reviewer import suggestions
    from theseus_reviewer.matchers import Matchers
    from theseus_reviewer.soundness_paths import Matcher, parse_rules

    matchers = Matchers(Matcher(parse_rules("prefix\tcore/sound/\n")), None)
    paths = [
        "core/sound/a.py",
        ".github/workflows/ci.yml",
        ".github/theseus-lock.json",
        "pkg/review.yml",
        "docs/readme.md",
        "src/app.py",
    ]
    assert suggestions.blocked_paths(paths, policy, matchers) == [
        ".github/theseus-lock.json",
        ".github/workflows/ci.yml",
        "core/sound/a.py",
        "pkg/review.yml",
    ]


@pytest.mark.parametrize(
    "hostile",
    [
        "```\nbreak out\n```\n- [ ] task",
        "````\n```\n````",
        "~~~\n# heading\n~~~",
        "x = 1  # </details> <b>",
        "```suggestion\nnested\n```",
        "a\n`" * 3 + "\nb",
    ],
)
def test_hostile_replacement_text_cannot_leave_the_suggestion_fence(hostile: str, tmp_path: Path) -> None:
    body = _suggest(_suggesting(hostile), tmp_path)
    found = re.search(r"^(`{3,})suggestion\n(.*)\n\1\n\n<sub>Blocks merge", body, re.DOTALL | re.MULTILINE)
    assert found is not None
    opening, inner = found.groups()
    assert inner == hostile
    assert all(len(run) < len(opening) for run in re.findall(r"`+", inner))
    assert layout._scan(body)[1] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"mechanical": True},
        {"mechanical": False, "replacement": "x"},
        {"mechanical": True, "replacement": ""},
        {"mechanical": True, "replacement": "x", "extra": 1},
        {"mechanical": "yes", "replacement": "x"},
        {"mechanical": True, "replacement": 5},
        {"mechanical": True, "replacement": "see <!-- review-defect: c1-D1 -->"},
        {"mechanical": True, "replacement": "@mention and https://example.test/x"},
        {"mechanical": True, "replacement": "tab\r\n"},
        "just text",
    ],
)
def test_an_unusable_suggestion_is_dropped_without_rejecting_the_verdict(bad: Any) -> None:
    verdict = {**_v("s", [_defect("T", 2, "d")])}
    verdict["defects"][0]["suggestion"] = bad
    assert validate(verdict).defects[0].replacement is None


def test_a_good_suggestion_survives_validation_and_a_round_trip() -> None:
    verdict = {**_v("s", [_defect("T", 2, "d")])}
    verdict["defects"][0]["suggestion"] = {"mechanical": True, "replacement": "x = 2"}
    parsed = validate(verdict).defects[0]
    assert parsed.replacement == "x = 2"
    again = validate({**verdict, "defects": [parsed.to_json()]}).defects[0]
    assert again == parsed
    assert "suggestion" not in _defect("T", 2, "d")


def test_the_finding_fingerprint_ignores_the_suggestion() -> None:
    assert dedup.marker(_suggesting("a")) == dedup.marker(_suggesting("b")) == dedup.marker(_suggesting(None))


def test_a_suggestion_reaches_the_posted_review_only_through_finalize_with_the_flag(
    policy: Policy, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    defect = _defect("Typo", 2, "A typo.")
    defect["suggestion"] = {"mechanical": True, "replacement": "line TWO"}
    base = {**_protocol_plan(policy), "workspace": str(workspace)}
    on = {**base, "brief_rules": {"suggestions": True}, "suggestion_blocked": []}
    verdict = _v("s", [defect])
    assert "```suggestion\nline TWO\n```" in _thread_of(_finalize(policy, on, verdict, kind=selection.FAIL))
    assert "suggestion" not in _thread_of(_finalize(policy, base, verdict, kind=selection.FAIL))
