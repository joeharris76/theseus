from __future__ import annotations

import copy
import json
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import adjudication, capacity, cli, derived, local, protocol, report, status_text
from theseus_reviewer._vendor import yaml
from theseus_reviewer.classifier import ChangedFile
from theseus_reviewer.classify import Classification
from theseus_reviewer.derived import DerivedEntry
from theseus_reviewer.matchers import load_matchers
from theseus_reviewer.policy import Policy, PolicyError, parse_policy
from theseus_reviewer.render import DERIVED_FILES_HEADER

from .conftest import POLICY_PATH
from .test_attempts import _v
from .test_protocol import DECIDED, _found, _marker_record, _round
from .test_report_outcomes import _comment_plan
from .test_rounds import _final, _protocol_plan, _record

pytestmark = [pytest.mark.unit, pytest.mark.fast]

DATA_DIR = "data/baseline"
SUMMARY = f"{DATA_DIR}/SUMMARY.md"
MANIFEST = f"{DATA_DIR}/manifest.json"
SHARD = f"{DATA_DIR}/shard-00.json"
ATTESTED = "Reproduce the baseline"
OLD = ("100644", "1" * 40)
NEW = ("100644", "2" * 40)
LARGE_ROWS = 40_000
OTHER_HEAD = "b" * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a derived-paths test reached the GitHub API")

    monkeypatch.setattr(cli.github, "_run_api", refuse)
    monkeypatch.setattr(cli.github, "issue_comments", lambda *_args: [])
    monkeypatch.setattr(cli.github, "review_threads", lambda *_args: [])


def _entry(**over: Any) -> dict[str, Any]:
    return {
        "name": "baseline",
        "files": [MANIFEST, f"{DATA_DIR}/shard-[0-9][0-9].json"],
        "summary": SUMMARY,
        "attested_by": ATTESTED,
        **over,
    }


def _raw(*entries: Any) -> dict[str, Any]:
    raw = copy.deepcopy(yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")))
    raw["evidence"]["derived_paths"] = list(entries)
    return raw


def _policy(*entries: Any) -> Policy:
    return parse_policy(_raw(*(entries or (_entry(),))))


def _plain_policy() -> Policy:
    return parse_policy(_raw())


def _recorded(paths: tuple[str, ...] = (MANIFEST, SHARD)) -> dict[str, Any]:
    (entry,) = _policy().evidence.derived_paths
    return {"entry": "baseline", "digest": entry.digest(), "paths": list(paths)}


DIGEST = _recorded()["digest"]


def test_a_policy_parses_a_derived_entry() -> None:
    (entry,) = _policy().evidence.derived_paths
    assert (entry.name, entry.summary, entry.attested_by) == ("baseline", SUMMARY, ATTESTED)
    assert entry.files == (MANIFEST, f"{DATA_DIR}/shard-[0-9][0-9].json")
    assert _plain_policy().evidence.derived_paths == ()


def test_the_digest_follows_every_field_but_not_the_order_of_files() -> None:
    (base,) = _policy().evidence.derived_paths
    (renamed,) = _policy(_entry(attested_by="Another check")).evidence.derived_paths
    (reordered,) = _policy(_entry(files=list(reversed(_entry()["files"])))).evidence.derived_paths
    assert len(base.digest()) == 64
    assert base.digest() != renamed.digest()
    assert base.digest() == reordered.digest()


def test_the_evidence_mapping_refuses_unknown_keys() -> None:
    raw = _raw()
    raw["evidence"]["derive_paths"] = []
    with pytest.raises(PolicyError, match="evidence has unknown key 'derive_paths'"):
        parse_policy(raw)


def test_an_entry_refuses_unknown_keys() -> None:
    with pytest.raises(PolicyError, match="unknown key 'checked_by'"):
        _policy(_entry(checked_by="x"))


@pytest.mark.parametrize("field", ["name", "files", "summary", "attested_by"])
def test_an_entry_requires_every_field(field: str) -> None:
    entry = _entry()
    entry.pop(field)
    with pytest.raises(PolicyError, match=f"missing '{field}'"):
        _policy(entry)


def test_derived_paths_must_be_a_list_of_mappings() -> None:
    raw = _raw()
    raw["evidence"]["derived_paths"] = {"name": "baseline"}
    with pytest.raises(PolicyError, match="must be a list"):
        parse_policy(raw)
    with pytest.raises(PolicyError, match="must be a mapping"):
        parse_policy(_raw("baseline"))


def test_names_must_be_unique() -> None:
    other = _entry(files=["data/other/manifest.json"], summary="data/other/SUMMARY.md")
    with pytest.raises(PolicyError, match="names must be unique"):
        _policy(_entry(), other)


def test_overlapping_entries_are_allowed_and_the_first_fit_wins() -> None:
    other = _entry(name="other", files=[f"{DATA_DIR}/shard-0*.json"], summary="data/other/SUMMARY.md")
    first, second = _policy(_entry(), other).evidence.derived_paths
    assert (first.name, second.name) == ("baseline", "other")


@pytest.mark.parametrize("name", ["Baseline", "base_line", "-x", "x-", ""])
def test_names_follow_the_slug_form(name: str) -> None:
    with pytest.raises(PolicyError, match="name must match"):
        _policy(_entry(name=name))


@pytest.mark.parametrize(
    "files",
    [
        [".github/data/manifest.json"],
        ["*/manifest.json"],
        [f"{DATA_DIR}/*.py"],
        [f"{DATA_DIR}/*"],
        [f"{DATA_DIR}/[!cx]*"],
        [f"{DATA_DIR}/run.p?"],
        [f"{DATA_DIR}/run.[ps]h"],
        [f"{DATA_DIR}/[!xc]*"],
        [f"{DATA_DIR}/[^c-x]*.*"],
        [f"{DATA_DIR}/[!a-z]*"],
        [f"{DATA_DIR}/tests-*"],
        [f"{DATA_DIR}/conftest.py"],
        [f"{DATA_DIR}/run.sh"],
        [f"{DATA_DIR}/settings.yaml"],
        [f"{DATA_DIR}/pyproject.toml"],
        ["_project/scripts/reviewers/data.json"],
        [f"{DATA_DIR}/**/manifest.json"],
        ["/abs/manifest.json"],
        [f"{DATA_DIR}/../manifest.json"],
        [],
    ],
)
def test_files_may_not_match_gate_github_or_code_paths(files: list[str]) -> None:
    with pytest.raises(PolicyError, match="evidence derived_paths"):
        _policy(_entry(files=files))


@pytest.mark.parametrize(
    "summary", [f"{DATA_DIR}/SUMMARY.txt", f"{DATA_DIR}/*.md", "../SUMMARY.md", ".github/SUMMARY.md"]
)
def test_the_summary_must_be_an_exact_safe_markdown_path(summary: str) -> None:
    with pytest.raises(PolicyError, match="evidence derived_paths"):
        _policy(_entry(summary=summary))


def test_the_summary_may_not_be_matched_by_files() -> None:
    with pytest.raises(PolicyError, match="not be matched by files"):
        _policy(_entry(files=[f"{DATA_DIR}/*.md"], summary=SUMMARY))


@pytest.mark.parametrize("attested", ["", " padded ", "two\nlines", "x" * 201, 5])
def test_attested_by_is_one_short_line(attested: Any) -> None:
    with pytest.raises(PolicyError, match="attested_by"):
        _policy(_entry(attested_by=attested))


def _file_diff(path: str, rows: int = 1, *, added: bool = False) -> str:
    header = f"diff --git a/{path} b/{path}\n"
    body = "".join(f"+row {n} {'x' * 200}\n" for n in range(rows))
    if added:
        return f"{header}new file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{rows} @@\n{body}"
    return f"{header}--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{rows + 1} @@\n old\n{body}"


class Scenario:
    def __init__(self, directory: str = DATA_DIR) -> None:
        self.files = [
            ChangedFile(f"{directory}/manifest.json", None, LARGE_ROWS, 0, status="modified"),
            ChangedFile(f"{directory}/shard-00.json", None, LARGE_ROWS, 0, status="modified"),
            ChangedFile(f"{directory}/SUMMARY.md", None, 3, 1, status="modified"),
        ]
        self.states: list[protocol.FileState] | None = [
            protocol.file_state(item.path, None, OLD, NEW) for item in self.files
        ]
        self.diff = "".join(
            _file_diff(item.path, 3 if item.path.endswith("SUMMARY.md") else LARGE_ROWS) for item in self.files
        )

    def add(self, item: ChangedFile, old: tuple[str, str] | None = OLD, new: tuple[str, str] | None = NEW) -> None:
        assert self.states is not None
        self.files.append(item)
        self.states.append(protocol.file_state(item.path, item.previous_path, old, new))
        self.diff += _file_diff(item.path, 2)

    def set_state(self, index: int, path: str, previous: str | None, old: Any, new: Any) -> None:
        assert self.states is not None
        self.states[index] = protocol.file_state(path, previous, old, new)

    def set_mode(self, path: str, **mode: str) -> None:
        assert self.states is not None
        self.states = [
            protocol.FileState(**{**state.__dict__, **mode}) if state.new_path == path else state
            for state in self.states
        ]


def _plan(
    scenario: Scenario,
    policy: Policy,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    found: protocol.History | None = None,
    since: Any = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    matchers = load_matchers(policy.consumer, POLICY_PATH.parents[1])
    plan = cli._base_plan(policy, "owner/repo", 7, "1")
    plan.update({"base_sha": "b" * 40, "base_ref": "develop", "head_sha": "a" * 40})
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    inputs = cli.RoundInputs(
        files=scenario.files,
        in_scope={item.path for item in scenario.files},
        full_diff=scenario.diff,
        states=scenario.states,
        found=found or protocol.History(records=[]),
        previous_strikes=0,
        pool_blocks={},
        manual=False,
        event_name="pull_request_target",
        base_changed_at=None,
        base_change_unreadable=False,
        since_diff=since,
    )
    assert cli.plan_decision(plan, policy, out_dir, inputs, matchers) == 0
    return plan


def _written(out_dir: Path) -> str:
    return (out_dir / cli.DIFF_FILE).read_text(encoding="utf-8")


def test_an_all_derived_change_with_a_summary_is_planned_as_summary_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch)
    assert plan["decision"] == "review"
    assert plan["evidence_files"] == [SUMMARY]
    assert plan["evidence_protected_files"] == [SUMMARY]
    assert plan["derived"] == {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}
    assert {item["path"] for item in plan["reviewed_files"]} == {MANIFEST, SHARD, SUMMARY}
    assert cli._plan_stats(plan) == [(SUMMARY, 3, 1, "modified", None)]


def test_data_hunks_are_in_no_reviewer_facing_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch)
    for text in (_written(tmp_path), plan["diff_inline"]):
        assert SUMMARY in text and "+row 0" in text
        assert MANIFEST not in text and SHARD not in text
    assert plan["brief_mode"] == "inline"
    assert set(plan["added_lines"]) == {SUMMARY}


def test_the_derived_plan_fits_where_the_full_plan_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = _plan(Scenario(), _plain_policy(), tmp_path / "plain", monkeypatch)
    derived = _plan(Scenario(), _policy(), tmp_path / "derived", monkeypatch)
    assert {MANIFEST, SHARD} <= set(plain["evidence_files"])
    assert plain[capacity.LOAD_KEY] > 1_000_000
    assert derived[capacity.LOAD_KEY] < 100_000


def test_the_brief_names_the_attestation_and_hides_the_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch)
    classification = Classification("small", "routine", "standard", 1.0, (), ())
    brief = cli.compose_brief(plan, tmp_path, classification, (), (), None, False, 400_000)
    assert f"`{ATTESTED}`" in brief and "do not open" in brief and "concrete inconsistency" in brief
    assert f"{MANIFEST} (+{LARGE_ROWS}/-0, modified)" in brief
    assert "+row 1000 " not in brief
    plan["brief_mode"], plan["diff_inline"] = "file-list", None
    listed = cli.compose_brief(plan, tmp_path, classification, (), (), None, False, 400_000)
    assert "Required reads: every changed hunk of each file listed here" in listed
    assert (
        listed.index(SUMMARY)
        < listed.index(DERIVED_FILES_HEADER)
        < listed.index(MANIFEST, listed.index(DERIVED_FILES_HEADER))
    )


def test_the_since_review_patch_drops_data_hunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    latest = _marker_record(decision="SHIP", coverage=1, patch_map={}, head_sha="c" * 40, derived=_recorded())
    since_text = _file_diff(SUMMARY, 2) + _file_diff(SHARD, 5)
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest), since=lambda previous: since_text)
    assert plan["since_review"]["head"] == "c" * 40
    staged = (tmp_path / plan["since_review"]["file"]).read_text(encoding="utf-8")
    assert SUMMARY in staged and SHARD not in staged


def _missing_summary(scenario: Scenario) -> None:
    scenario.files = [item for item in scenario.files if item.path != SUMMARY]
    assert scenario.states is not None
    scenario.states = [state for state in scenario.states if state.new_path != SUMMARY]
    scenario.diff = "".join(_file_diff(item.path, LARGE_ROWS) for item in scenario.files)


def _deleted_summary(scenario: Scenario) -> None:
    scenario.files[2] = ChangedFile(SUMMARY, None, 0, 3, status="removed")
    scenario.set_state(2, SUMMARY, None, OLD, None)


def _outside_file(scenario: Scenario) -> None:
    scenario.add(ChangedFile("data/other/notes.json", None, 2, 0, status="modified"))


def _gate_file(scenario: Scenario) -> None:
    scenario.add(ChangedFile(".github/review.yml", None, 2, 0, status="modified"))


def _nested_conftest(scenario: Scenario) -> None:
    scenario.add(ChangedFile(f"{DATA_DIR}/sub/conftest.py", None, 2, 0, status="added"), None, NEW)


def _nested_data(scenario: Scenario) -> None:
    scenario.add(ChangedFile(f"{DATA_DIR}/sub/shard-02.json", None, 2, 0, status="modified"))


def _rename_in(scenario: Scenario) -> None:
    scenario.files[1] = ChangedFile(SHARD, "tests/conftest.json", LARGE_ROWS, 0, status="renamed")
    scenario.set_state(1, SHARD, "tests/conftest.json", OLD, NEW)


def _rename_out(scenario: Scenario) -> None:
    scenario.files[1] = ChangedFile("elsewhere/shard.json", SHARD, LARGE_ROWS, 0, status="renamed")
    scenario.set_state(1, "elsewhere/shard.json", SHARD, OLD, NEW)


def _mode_change(scenario: Scenario) -> None:
    scenario.set_mode(SHARD, new_mode="100755")


def _old_mode(scenario: Scenario) -> None:
    scenario.set_mode(SHARD, old_mode="100755")


def _symlink(scenario: Scenario) -> None:
    scenario.set_mode(SHARD, new_mode="120000")


def _binary(scenario: Scenario) -> None:
    scenario.diff += f"diff --git a/{MANIFEST} b/{MANIFEST}\nBinary files a/{MANIFEST} and b/{MANIFEST} differ\n"


def _added_data(scenario: Scenario) -> None:
    scenario.files[1] = ChangedFile(SHARD, None, LARGE_ROWS, 0, status="added")
    scenario.set_state(1, SHARD, None, None, NEW)


def _removed_data(scenario: Scenario) -> None:
    scenario.files[1] = ChangedFile(SHARD, None, 0, LARGE_ROWS, status="removed")
    scenario.set_state(1, SHARD, None, OLD, None)


def _unknown_modes(scenario: Scenario) -> None:
    scenario.states = None


def _only_summary(scenario: Scenario) -> None:
    assert scenario.states is not None
    scenario.files, scenario.states = [scenario.files[2]], [scenario.states[2]]
    scenario.diff = _file_diff(SUMMARY, 3)


@pytest.mark.parametrize(
    "mutate",
    [
        _missing_summary,
        _deleted_summary,
        _outside_file,
        _gate_file,
        _nested_conftest,
        _nested_data,
        _rename_in,
        _rename_out,
        _mode_change,
        _old_mode,
        _symlink,
        _binary,
        _added_data,
        _removed_data,
        _unknown_modes,
        _only_summary,
    ],
)
def test_a_change_that_does_not_fit_the_entry_is_planned_as_today(
    mutate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario, twin = Scenario(), Scenario()
    mutate(scenario)
    mutate(twin)
    held = _plan(scenario, _policy(), tmp_path / "held", monkeypatch)
    today = _plan(twin, _plain_policy(), tmp_path / "plain", monkeypatch)
    assert "derived" not in held
    assert held["evidence_files"] == today["evidence_files"]
    assert _written(tmp_path / "held") == _written(tmp_path / "plain")
    assert held["diff_inline"] == today["diff_inline"]
    assert held[capacity.LOAD_KEY] == today[capacity.LOAD_KEY]


def test_an_added_summary_is_eligible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario()
    scenario.files[2] = ChangedFile(SUMMARY, None, 3, 0, status="added")
    scenario.set_state(2, SUMMARY, None, None, NEW)
    scenario.diff = scenario.diff.replace(_file_diff(SUMMARY, 3), _file_diff(SUMMARY, 3, added=True))
    plan = _plan(scenario, _policy(), tmp_path, monkeypatch)
    assert plan["derived"]["entry"] == "baseline" and plan["evidence_files"] == [SUMMARY]


def _derived_ship() -> protocol.History:
    return _found(_marker_record(decision="SHIP", coverage=1, patch_map={}, head_sha="c" * 40, derived=_recorded()))


def test_a_follow_up_after_a_derived_clearance_stays_summary_only_when_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _derived_ship())
    assert plan["protocol"]["kind"] == "follow-up"
    assert plan["evidence_files"] == [SUMMARY] and plan["scope"] == "full"


def test_a_non_eligible_round_after_a_derived_clearance_reads_the_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario()
    _outside_file(scenario)
    plan = _plan(scenario, _plain_policy(), tmp_path, monkeypatch, _derived_ship())
    assert plan["protocol"]["kind"] == "follow-up"
    assert {MANIFEST, SHARD} <= set(plan["protocol"]["changed"])
    assert {MANIFEST, SHARD} <= set(plan["evidence_files"])


@pytest.mark.parametrize("path", ["src/unchanged_module.py", "src/other.py"])
def test_a_finding_outside_the_entry_is_counted_and_anchored_on_the_summary(
    path: str, policy: Policy, tmp_path: Path
) -> None:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    verdict = _v("bad", [{"severity": "High", "file": path, "line": 7, "title": "Wrong", "detail": "Because."}])
    final = _final(policy, tmp_path, plan, verdict)
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    (defect,) = final.judgement.defects
    assert defect.file == SUMMARY and defect.severity == "High" and defect.title == f"Wrong [{path}:7]"
    assert f"{path}:7" in defect.detail and "Because." in defect.detail
    assert final.state == "failure"


def test_a_marker_round_trips_a_derived_record() -> None:
    record = _marker_record(derived=_recorded())
    assert protocol.decode_marker(protocol.encode_marker(record))["derived"] == _recorded()
    assert "derived" not in protocol.decode_marker(protocol.encode_marker(_marker_record()))


@pytest.mark.parametrize(
    "broken",
    [
        {"entry": "baseline", "digest": "x" * 64, "paths": [MANIFEST]},
        {"entry": "baseline", "digest": "a" * 64, "paths": []},
        {"entry": "Baseline", "digest": "a" * 64, "paths": [MANIFEST]},
        {"entry": "baseline", "digest": "a" * 64, "paths": [MANIFEST, MANIFEST]},
        {"entry": "baseline", "digest": "a" * 64, "paths": [MANIFEST], "extra": 1},
        {"entry": "baseline", "digest": "a" * 64},
        {"entry": "baseline", "digest": "a" * 64, "paths": [5]},
        "baseline",
    ],
)
def test_a_marker_with_a_malformed_derived_record_is_rejected(broken: Any) -> None:
    assert protocol.checked_record(_marker_record(derived=broken)) is None


def _bound(decision: str = "SHIP", **over: Any) -> dict[str, Any]:
    return _marker_record(**{"decision": decision, "coverage": 1, "patch_map": {}, "derived": _recorded(), **over})


def test_a_derived_clearance_carries_only_while_the_entry_digest_is_unchanged() -> None:
    kept = _round(_found(_bound()), head_sha=OTHER_HEAD, derived_digest=DIGEST)
    assert kept.kind == protocol.CARRY
    for digest in (None, "f" * 64):
        refused = _round(_found(_bound()), head_sha=OTHER_HEAD, derived_digest=digest, paths=["a.py", MANIFEST])
        assert refused.kind == protocol.FOLLOW_UP
        assert refused.changed == frozenset({"a.py", MANIFEST})


def test_a_derived_clearance_is_not_carried_on_the_same_head_after_a_base_change() -> None:
    held = _round(_found(_bound()), base_changed_at=DECIDED, derived_digest=DIGEST)
    assert held.kind == protocol.CARRY
    for digest in (None, "f" * 64):
        refused = _round(_found(_bound()), base_changed_at=DECIDED, derived_digest=digest)
        assert refused.kind == protocol.FIRST and refused.cycle == 2


def test_a_derived_do_not_ship_restarts_instead_of_following_up() -> None:
    step = _round(_found(_bound("DO_NOT_SHIP")), head_sha=OTHER_HEAD, derived_digest=None)
    assert step.kind == protocol.FIRST


def test_recorded_derived_paths_are_changed_reads_in_a_later_round() -> None:
    prints = {"a.py": "p1", MANIFEST: "p2"}
    paths = ["a.py", MANIFEST]
    old = protocol.patch_map(prints, paths)
    new = protocol.patch_map({**prints, "a.py": "p3"}, paths)
    arguments: dict[str, Any] = {"head_sha": OTHER_HEAD, "digest": "e" * 32, "patches": new, "paths": paths}
    ordinary = _round(_found(_marker_record(decision="SHIP", coverage=1, patch_map=old)), **arguments)
    bound = _round(_found(_bound(patch_map=old)), **arguments)
    attested = _round(_found(_bound(patch_map=old)), derived_digest=DIGEST, **arguments)
    assert ordinary.changed == frozenset({"a.py"})
    assert bound.changed == frozenset({"a.py", MANIFEST})
    assert attested.changed == frozenset({"a.py"})


def test_the_status_and_verdict_say_the_data_was_not_read(policy: Policy, tmp_path: Path) -> None:
    held = {**_recorded(), "summary": SUMMARY, "attested_by": "Nightly timings"}
    final = _final(policy, tmp_path, {**_protocol_plan(policy), "derived": held}, _v("fine"))
    expected = "summary only · data attested by Nightly timings, not read"
    assert final.description == f"SHIP · no open findings · {expected}"
    assert final.review is not None and f"\n{expected}\n" in final.review["body"]
    assert _record(final)["derived"] == _recorded()


NAME = "Reproduce the fast-tier duration baseline"


def test_the_ship_status_keeps_the_wording_and_clips_the_name_to_fit() -> None:
    text = status_text.with_derived("SHIP · no open findings", NAME, status_text.SUCCESS_LIMIT)
    assert text == "SHIP · summary only · data attested by Reproduce the fast-tier duration b…, not read"
    assert len(text) <= status_text.SUCCESS_LIMIT


def test_a_very_long_name_is_clipped_never_the_wording() -> None:
    text = status_text.with_derived("SHIP · no open findings", "n" * 200, status_text.SUCCESS_LIMIT)
    assert len(text) <= status_text.SUCCESS_LIMIT
    assert text.startswith("SHIP · summary only · data attested by nnnn") and text.endswith("…, not read")


def test_a_short_name_keeps_every_segment() -> None:
    text = status_text.with_derived("SHIP · no open findings", "Nightly", status_text.SUCCESS_LIMIT)
    assert text == "SHIP · no open findings · summary only · data attested by Nightly, not read"


def test_the_ruling_hint_outranks_the_check_name() -> None:
    ids = [f"D{n}" for n in range(1, 7)]
    base = status_text.with_findings("SHIP WITH FIXES", 6, [], ids, "", status_text.DESCRIPTION_LIMIT)
    assert "escalated D1,D2,D3,D4,D5,D6" in base
    text = status_text.with_derived(base, NAME, status_text.DESCRIPTION_LIMIT)
    assert text == (
        "SHIP WITH FIXES · 6 to fix · escalated D1,D2,D3,D4,D5,D6 · owner ruling needed"
        " · summary only · data attested, not read"
    )
    one = status_text.with_findings("SHIP WITH FIXES", 1, [], ["D1"], "", status_text.DESCRIPTION_LIMIT)
    clipped = status_text.with_derived(one, "n" * 200, status_text.DESCRIPTION_LIMIT)
    assert status_text.ESCALATED.search(clipped) and clipped.endswith(", not read") and "summary only" in clipped


def test_findings_keep_the_full_name_when_it_fits() -> None:
    text = status_text.with_derived("SHIP WITH FIXES · 2 to fix (1 High)", ATTESTED, status_text.DESCRIPTION_LIMIT)
    assert text == f"SHIP WITH FIXES · 2 to fix (1 High) · summary only · data attested by {ATTESTED}, not read"


def test_a_carried_decision_keeps_the_record_and_the_wording(policy: Policy) -> None:
    previous = _bound(open_defects=[], strikes_after=0, next_id=1, reviewer="small")
    plan = {
        **_comment_plan(policy, kind="carry", previous=previous),
        "reviewed_head": "a" * 40,
        "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED},
        "findings_delivery": "review",
    }
    final = report.carried(plan)
    assert "summary only · data attested" in final.description and final.description.endswith(", not read")
    assert protocol.decode_marker(final.body)["derived"] == _recorded()
    unattested = report.carried({**plan, "derived": None})
    assert "summary only" not in unattested.description
    assert protocol.decode_marker(unattested.body)["derived"] == _recorded()


def test_an_older_local_outcome_and_record_with_a_derived_key_still_read() -> None:
    outcome = local.Outcome(False, "SHIP", "small", "", ())
    assert local.Outcome.from_json({**outcome.to_json(), "derived": {"entry": "baseline"}}) == outcome
    assert local.recorded_outcome(_bound()).label == "SHIP"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr: str = "") -> local.LocalEnvironment:
    repo = tmp_path / "repo"
    (repo / DATA_DIR).mkdir(parents=True)
    _git(repo, "init", "--quiet", "--initial-branch=develop")
    for name in ("manifest.json", "shard-00.json", "SUMMARY.md"):
        (repo / DATA_DIR / name).write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    for name in ("manifest.json", "shard-00.json", "SUMMARY.md"):
        (repo / DATA_DIR / name).write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "refresh")
    policy_path = tmp_path / "policy.yml"
    policy_path.write_text(json.dumps(_raw(_entry())), encoding="utf-8")
    environ = {
        local.WORK_ENV: str(tmp_path / "work"),
        local.REPOSITORY_ENV: "o/r",
        local.ROOT_ENV: str(repo),
        local.TRUSTED_ENV: str(POLICY_PATH.parents[1]),
        local.BASE_ENV: "develop~1",
        local.HEAD_ENV: _git(repo, "rev-parse", "HEAD"),
        local.STATE_ENV: str(tmp_path / "state"),
        **({local.PR_ENV: pr} if pr else {}),
    }
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return local.LocalEnvironment.from_environ(environ, policy_path)


def _local_plan(env: local.LocalEnvironment) -> dict[str, Any]:
    assert env.plan() == 0
    return json.loads((env.layout.plan / cli.PLAN_FILE).read_text(encoding="utf-8"))


def test_a_local_review_of_a_data_and_summary_patch_is_planned_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _local_plan(_local_env(tmp_path, monkeypatch))
    assert plan["decision"] == "review" and "derived" not in plan
    assert {MANIFEST, SHARD} <= set(plan["evidence_files"])


def _hosted(env: local.LocalEnvironment, monkeypatch: pytest.MonkeyPatch, **over: Any) -> None:
    record = _marker_record(decision="SHIP", coverage=1, patch_map={}, head_sha=env.head, **over)
    monkeypatch.setattr(cli, "_plan_history", lambda *_args: _found(record))
    monkeypatch.setattr(cli, "_base_changed_at", lambda *_args: (None, False))


def test_a_local_review_never_reuses_a_hosted_derived_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _local_env(tmp_path, monkeypatch, pr="7")
    _hosted(env, monkeypatch, derived=_recorded())
    plan = _local_plan(env)
    assert plan["decision"] == "review" and "derived" not in plan
    assert {MANIFEST, SHARD} <= set(plan["evidence_files"])


def test_a_local_review_still_reuses_a_hosted_full_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _local_env(tmp_path, monkeypatch, pr="7")
    _hosted(env, monkeypatch)
    assert _local_plan(env)["decision"] != "review"


@pytest.mark.parametrize("decision", ["DO_NOT_SHIP", "REFUSED"])
def test_a_round_after_a_full_review_do_not_ship_or_a_refusal_is_not_derived(
    decision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = "refused" if decision == "REFUSED" else "first"
    latest = _marker_record(decision=decision, kind=kind, coverage=1, patch_map={}, head_sha="c" * 40)
    assert "derived" not in latest
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])


def test_soundness_data_is_eligible_and_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = "tests/fixtures/fast_tier_baseline"
    entry = _entry(
        files=[f"{directory}/manifest.json", f"{directory}/shard-[0-9][0-9].json"],
        summary=f"{directory}/SUMMARY.md",
    )
    matchers = load_matchers(_policy(entry).consumer, POLICY_PATH.parents[1])
    assert matchers.is_soundness_path(f"{directory}/shard-00.json")
    plain = _plan(Scenario(directory), _plain_policy(), tmp_path / "plain", monkeypatch)
    derived = _plan(Scenario(directory), _policy(entry), tmp_path / "derived", monkeypatch)
    assert plain[capacity.LOAD_KEY] > 1_000_000
    assert derived["evidence_files"] == [f"{directory}/SUMMARY.md"]
    assert derived["evidence_protected_files"] == [f"{directory}/SUMMARY.md"]
    assert derived[capacity.LOAD_KEY] < 100_000
    assert "shard-00" not in _written(tmp_path / "derived")


def _eligible(entry: DerivedEntry, files: list[ChangedFile], diff: str | None = None) -> Any:
    states = [protocol.file_state(item.path, item.previous_path, OLD, NEW) for item in files]
    text = diff if diff is not None else "".join(_file_diff(item.path, 2) for item in files)
    return derived.assess([entry], files, states, text, lambda _path: False)


def _bypass_entry(pattern: str) -> DerivedEntry:
    return DerivedEntry("baseline", (pattern,), SUMMARY, ATTESTED)


@pytest.mark.parametrize(
    "path",
    [
        f"{DATA_DIR}/evil.py",
        f"{DATA_DIR}/__init__.py",
        f"{DATA_DIR}/conftest.json",
        f"{DATA_DIR}/run.sh",
        f"{DATA_DIR}/run.py",
    ],
)
def test_a_code_path_is_never_eligible_whatever_the_pattern_matches(path: str) -> None:
    files = [ChangedFile(path, None, 2, 0, status="modified"), ChangedFile(SUMMARY, None, 2, 0, status="modified")]
    for pattern in ("[!cx]*", "*", "run.p?", "run.[ps]h", "*.p?", "[!xc]*"):
        assert _eligible(_bypass_entry(f"{DATA_DIR}/{pattern}"), files) is None


def test_a_github_path_is_never_eligible_whatever_the_pattern_matches() -> None:
    files = [
        ChangedFile(".github/data.json", None, 2, 0, status="modified"),
        ChangedFile(".github/SUMMARY.md", None, 2, 0, status="modified"),
    ]
    entry = DerivedEntry("baseline", (".github/*.json",), ".github/SUMMARY.md", ATTESTED)
    assert _eligible(entry, files) is None


def _many(count: int, width: int = 1) -> list[ChangedFile]:
    names = [f"{DATA_DIR}/p{'x' * width}{n}.json" for n in range(count)]
    return [
        *(ChangedFile(name, None, 1, 0, status="modified") for name in names),
        ChangedFile(SUMMARY, None, 2, 0, status="modified"),
    ]


def test_the_marker_path_limits_decide_eligibility() -> None:
    entry = DerivedEntry("baseline", (f"{DATA_DIR}/*.json",), SUMMARY, ATTESTED)
    assert _eligible(entry, _many(200)) is not None
    assert _eligible(entry, _many(201)) is None
    assert _eligible(entry, _many(2, width=500)) is None


@pytest.mark.parametrize("status", ["copied", "changed", "unchanged"])
def test_other_file_statuses_are_not_eligible(status: str) -> None:
    entry = DerivedEntry("baseline", (f"{DATA_DIR}/*.json",), SUMMARY, ATTESTED)
    files = [ChangedFile(SHARD, None, 2, 0, status=status), ChangedFile(SUMMARY, None, 2, 0, status="modified")]
    assert _eligible(entry, files) is None


def test_a_gitlink_is_not_eligible() -> None:
    entry = DerivedEntry("baseline", (f"{DATA_DIR}/*.json",), SUMMARY, ATTESTED)
    files = [ChangedFile(SHARD, None, 2, 0, status="modified"), ChangedFile(SUMMARY, None, 2, 0, status="modified")]
    states = [
        protocol.file_state(SHARD, None, ("160000", "1" * 40), ("160000", "2" * 40)),
        protocol.file_state(SUMMARY, None, OLD, NEW),
    ]
    diff = _file_diff(SHARD, 2) + _file_diff(SUMMARY, 2)
    assert derived.assess([entry], files, states, diff, lambda _path: False) is None


def test_a_quoted_unicode_data_header_is_stripped() -> None:
    entry = DerivedEntry("baseline", (f"{DATA_DIR}/*.json",), SUMMARY, ATTESTED)
    name = f"{DATA_DIR}/sh\u00e4rd.json"
    files = [ChangedFile(name, None, 1, 0, status="modified"), ChangedFile(SUMMARY, None, 2, 0, status="modified")]
    quoted = f"{DATA_DIR}/sh\\303\\244rd.json"
    data = f'diff --git "a/{quoted}" "b/{quoted}"\n--- "a/{quoted}"\n+++ "b/{quoted}"\n@@ -1,1 +1,2 @@\n old\n+new\n'
    found = _eligible(entry, files, data + _file_diff(SUMMARY, 2))
    assert found is not None and ".json" not in found.diff and SUMMARY in found.diff


def test_a_data_hunk_that_survives_stripping_is_not_eligible() -> None:
    entry = DerivedEntry("baseline", (f"{DATA_DIR}/*.json",), SUMMARY, ATTESTED)
    files = [ChangedFile(SHARD, None, 1, 0, status="modified"), ChangedFile(SUMMARY, None, 2, 0, status="modified")]
    garbled = f"diff --git garbled\n--- a/{SHARD}\n+++ b/{SHARD}\n@@ -1,1 +1,2 @@\n old\n+new\n"
    assert _eligible(entry, files, garbled + _file_diff(SUMMARY, 2)) is None


def test_the_since_review_filter_fails_closed() -> None:
    assert cli._without_files(None, [SHARD]) is None
    garbled = f"diff --git garbled\n--- a/{SHARD}\n+++ b/{SHARD}\n@@ -1,1 +1,2 @@\n old\n+new\n"
    assert cli._without_files(garbled, [SHARD]) is None
    assert cli._without_files(_file_diff(SHARD) + _file_diff(SUMMARY), [SHARD]) == _file_diff(SUMMARY)


def test_the_inline_brief_lists_data_files_as_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch)
    assert plan["brief_mode"] == "inline"
    classification = Classification("small", "routine", "standard", 1.0, (), ())
    brief = cli.compose_brief(plan, tmp_path, classification, (), (), None, False, 400_000)
    before, _, after = brief.partition(DERIVED_FILES_HEADER)
    assert after and MANIFEST in after and SHARD in after
    assert f"{MANIFEST} (" not in before and f"{SUMMARY} (" in before
    assert f"anchor every such finding on `{SUMMARY}`" in brief


def _ruled(policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(cli, "_carry_disagreement", lambda *_args: None)
    latest = _bound(
        "SHIP_WITH_FIXES",
        open_defects=[{"id": "D1", "severity": "High", "file": SUMMARY, "line": 1, "end_line": None, "title": "T"}],
        strikes_after=0,
        next_id=2,
        reviewer="small",
    )
    plan = cli._base_plan(policy, "owner/repo", 7, "1")
    plan.update({"base_sha": "b" * 40, "base_ref": "develop", "head_sha": "a" * 40})
    ruling = adjudication.Adjudication("WITHDRAW", "D1", "fine")
    assert cli.adjudicate_round(tmp_path, plan, policy, _found(latest), ruling, None) == 0
    return plan


def test_an_owner_ruling_after_the_entry_changed_cannot_clear_derived_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _ruled(_plain_policy(), tmp_path, monkeypatch)
    assert plan["decision"] == cli.HOLD and "policy entry has changed" in plan["decision_reason"]
    other = _policy(_entry(attested_by="Another check"))
    assert _ruled(other, tmp_path, monkeypatch)["decision"] == cli.HOLD


def test_an_owner_ruling_with_the_same_entry_carries_with_the_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _ruled(_policy(), tmp_path, monkeypatch)
    assert plan["decision"] == cli.CARRY and plan["derived"]["attested_by"] == ATTESTED
    final = report.adjudicated({**plan, "findings_delivery": "review"})
    assert final.description.endswith(", not read") and "summary only" in final.description
    assert protocol.decode_marker(final.body)["derived"] == _recorded()


@pytest.mark.parametrize("count", range(1, 41))
def test_escalated_ids_survive_the_derived_note_at_every_length(count: int) -> None:
    ids = [f"D{n}" for n in range(1, count + 1)]
    base = status_text.with_findings("SHIP WITH FIXES", count, [], ids, "", status_text.DESCRIPTION_LIMIT)
    text = status_text.with_derived(base, NAME, status_text.DESCRIPTION_LIMIT)
    assert len(text) <= status_text.DESCRIPTION_LIMIT
    assert "summary only · data attested" in text and text.endswith(", not read")
    kept = status_text.ESCALATED.search(text)
    original = status_text.ESCALATED.search(base)
    assert kept is not None and original is not None
    shown = kept["ids"].split(",")
    assert shown == ids[: len(shown)]
    assert len(shown) + int(kept["more"] or 0) == count
    assert ("owner ruling needed" in text) or len(shown) < count


@pytest.mark.parametrize("escalated", [[], ["D1", "D2", "D3"]])
def test_a_repost_near_the_limit_keeps_the_derived_note(escalated: list[str]) -> None:
    base = status_text.with_findings(
        "SHIP WITH FIXES", 12, [("High", 7), ("Medium", 5)], escalated, "", status_text.DESCRIPTION_LIMIT
    )
    held = status_text.with_derived(base, NAME, status_text.DESCRIPTION_LIMIT)
    posted = status_text.repost(held)
    assert len(posted) <= status_text.DESCRIPTION_LIMIT and posted.endswith(status_text.REPOSTED_MARK)
    assert "summary only · data attested" in posted and ", not read" in posted
    if escalated:
        assert status_text.ESCALATED.search(posted) and "owner ruling needed" in posted
    again = status_text.repost(posted)
    assert again == posted


def test_a_success_repost_keeps_the_derived_note() -> None:
    plan = {
        "repost": {
            "state": "success",
            "label": "SHIP",
            "description": status_text.with_derived("SHIP · no open findings", NAME, status_text.SUCCESS_LIMIT),
        },
        "head_sha": "a" * 40,
    }
    final = report.reposted(plan)
    assert "summary only · data attested" in final.description and ", not read" in final.description


def test_a_changed_entry_digest_is_planned_as_a_full_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = {**_recorded(), "digest": "f" * 64}
    latest = _marker_record(decision="SHIP", coverage=1, patch_map={}, head_sha="c" * 40, derived=stale)
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest))
    assert "derived" not in plan
    assert {MANIFEST, SHARD} <= set(plan["evidence_files"])
    assert {MANIFEST, SHARD} <= set(plan["protocol"]["changed"])
    assert MANIFEST in _written(tmp_path)


def test_same_title_findings_on_different_files_stay_distinct(policy: Policy, tmp_path: Path) -> None:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    findings = [
        {"severity": "High", "file": path, "line": 3, "title": "Same", "detail": f"Detail for {path}."}
        for path in ("src/a.py", "src/b.py")
    ]
    final = _final(policy, tmp_path, plan, _v("bad", findings))
    assert final.judgement is not None and len(final.judgement.defects) == 2
    assert {item.title for item in final.judgement.defects} == {"Same [src/a.py:3]", "Same [src/b.py:3]"}
    assert final.review is not None
    text = final.review["body"] + "".join(item["body"] for item in final.review["comments"])
    assert "Detail for src/a.py." in text and "Detail for src/b.py." in text


def test_a_manual_review_on_the_same_head_reads_derived_data_in_full() -> None:
    held = _round(_found(_bound()), manual=True, derived_digest=DIGEST)
    assert held.kind != protocol.FIRST
    for digest in (None, "f" * 64):
        step = _round(_found(_bound()), manual=True, derived_digest=digest)
        assert step.kind == protocol.FIRST and step.cycle == 2
    assert _round(_found(_bound()), manual=False, derived_digest=None).kind == protocol.SKIP


def test_a_near_limit_title_keeps_the_location_and_the_limit(policy: Policy, tmp_path: Path) -> None:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    long_path = "data/" + "d" * 380 + "/shard.json"
    findings = [
        {"severity": "High", "file": path, "line": 3, "title": "t" * 199, "detail": f"Detail {n}."}
        for n, path in enumerate(("src/a.py", "src/b.py", long_path))
    ]
    final = _final(policy, tmp_path, plan, _v("bad", findings))
    assert final.judgement is not None
    titles = [item.title for item in final.judgement.defects]
    assert len(titles) == 3 and len(set(titles)) == 3
    assert all(len(title) <= 200 for title in titles)
    assert titles[0].endswith("[src/a.py:3]") and titles[1].endswith("[src/b.py:3]")


def _open_on(path: str) -> dict[str, Any]:
    return {"id": "D1", "severity": "High", "file": path, "line": 3, "end_line": None, "title": "Bad value"}


def test_an_open_defect_on_a_data_file_keeps_the_round_a_full_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _marker_record(
        decision="SHIP_WITH_FIXES", coverage=1, patch_map={}, head_sha="c" * 40, open_defects=[_open_on(SHARD)]
    )
    plan = _plan(Scenario(), _policy(), tmp_path / "data", monkeypatch, _found(latest))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])
    elsewhere = _marker_record(
        decision="SHIP_WITH_FIXES",
        coverage=1,
        patch_map={},
        head_sha="c" * 40,
        open_defects=[_open_on(SUMMARY)],
        derived=_recorded(),
    )
    assert _plan(Scenario(), _policy(), tmp_path / "summary", monkeypatch, _found(elsewhere))["derived"]


@pytest.mark.parametrize("status", ["fixed", "withdrawn"])
def test_a_summary_only_round_cannot_clear_a_data_anchored_prior(status: str, policy: Policy, tmp_path: Path) -> None:
    plan = {
        **_protocol_plan(policy, kind="follow-up", round=2, prior=[_open_on(SHARD), {**_open_on(SUMMARY), "id": "D2"}]),
        "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED},
    }
    verdict = _v("fine")
    verdict["prior_defects"] = [
        {"id": "D1", "status": status, "evidence": f"{SHARD}:3 is right"},
        {"id": "D2", "status": "fixed", "evidence": f"{SUMMARY}:3 is right"},
    ]
    final = _final(policy, tmp_path, plan, verdict)
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    assert [item["id"] for item in final.judgement.carried] == ["D1"]


def test_every_segment_is_kept_before_the_name_is_dropped_for_the_bare_note() -> None:
    text = status_text.with_derived(
        "SHIP · no open findings · degraded: openai", "Reproduce the baseline", status_text.SUCCESS_LIMIT
    )
    assert text == "SHIP · degraded: openai · summary only · data attested, not read"
    carried = status_text.with_derived("SHIP · carried from 0123456789ab (same patch)", NAME, status_text.SUCCESS_LIMIT)
    assert carried == "SHIP · summary only · data attested by Reproduce the fast-tier duration b…, not read"
    kept = status_text.with_derived("SHIP · degraded: openai", NAME, status_text.SUCCESS_LIMIT)
    assert kept == "SHIP · degraded: openai · summary only · data attested, not read"


def _data_finding_round(policy: Policy, tmp_path: Path) -> report.Final:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    finding = {"severity": "High", "file": SHARD, "line": 3, "title": "Bad value", "detail": "Wrong baseline."}
    return _final(policy, tmp_path, plan, _v("bad", [finding]))


def test_a_finding_on_a_data_file_keeps_its_file_and_blocks(policy: Policy, tmp_path: Path) -> None:
    final = _data_finding_round(policy, tmp_path)
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    (defect,) = final.judgement.defects
    assert defect.file == SHARD and defect.title == "Bad value" and defect.line == 3
    assert final.state == "failure" and final.review is not None
    assert "Wrong baseline." in final.review["body"] + "".join(item["body"] for item in final.review["comments"])
    (entry,) = _record(final)["open_defects"]
    assert entry["file"] == SHARD and entry["title"] == "Bad value"


def test_the_trigger_sequence_for_a_data_finding_ends_in_a_full_review(
    policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {
        **_record(_data_finding_round(policy, tmp_path / "first")),
        "head_sha": "c" * 40,
        "patch_digest": "z" * 32,
    }
    plan = _plan(Scenario(), _policy(), tmp_path / "later", monkeypatch, _found(record))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])


def test_a_case_variant_of_a_data_path_maps_to_the_data_file(policy: Policy, tmp_path: Path) -> None:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    cited = f"{DATA_DIR}/Shard-00.json"
    finding = {"severity": "High", "file": cited, "line": 3, "title": "Bad value", "detail": "Wrong."}
    final = _final(policy, tmp_path, plan, _v("bad", [finding]))
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    (defect,) = final.judgement.defects
    assert defect.file == SHARD and defect.title == "Bad value" and defect.line == 3
    assert _record(final)["open_defects"][0]["file"] == SHARD


ANCHORS = [
    pytest.param(SHARD, "Bad value", id="exact"),
    pytest.param(f"{DATA_DIR.upper()}/SHARD-00.JSON", "Bad value", id="case"),
    pytest.param("…baseline/shard-00.json", "Bad value", id="clipped-path"),
    pytest.param("…", "Bad value", id="empty-clip"),
    pytest.param(SUMMARY, f"Bad value [{SHARD}:3]", id="legacy-title"),
    pytest.param(SUMMARY, f"Bad value [{SHARD.upper()}:3]", id="legacy-title-case"),
    pytest.param(SUMMARY, "Bad value […baseline/shard-00.json:3]", id="legacy-clipped-location"),
    pytest.param(SUMMARY, "Bad value that was cut sho…", id="legacy-clipped-title"),
]


@pytest.mark.parametrize(("file", "title"), ANCHORS)
def test_every_data_anchored_form_keeps_the_round_a_full_review(
    file: str, title: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = {"id": "D1", "severity": "High", "file": file, "line": 3, "end_line": None, "title": title}
    latest = _marker_record(
        decision="SHIP_WITH_FIXES", coverage=1, patch_map={}, head_sha="c" * 40, open_defects=[prior]
    )
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])


@pytest.mark.parametrize(("file", "title"), ANCHORS)
@pytest.mark.parametrize("status", ["fixed", "withdrawn"])
def test_every_data_anchored_form_stays_open_in_a_derived_plan(
    file: str, title: str, status: str, policy: Policy, tmp_path: Path
) -> None:
    prior = {"id": "D1", "severity": "High", "file": file, "line": 3, "end_line": None, "title": title}
    plan = {
        **_protocol_plan(policy, kind="follow-up", round=2, prior=[prior]),
        "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED},
    }
    verdict = _v("fine")
    verdict["prior_defects"] = [{"id": "D1", "status": status, "evidence": f"{SUMMARY}:3 says so"}]
    final = _final(policy, tmp_path, plan, verdict)
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    assert [item["id"] for item in final.judgement.carried] == ["D1"]


@pytest.mark.parametrize("title", ["Typo in the summary", "Typo [src/other.py:3]", "Typo […other.py:3]"])
def test_a_summary_prior_about_a_non_data_file_is_still_clearable(
    title: str, policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = {"id": "D1", "severity": "High", "file": SUMMARY, "line": 1, "end_line": None, "title": title}
    latest = _marker_record(
        decision="SHIP_WITH_FIXES",
        coverage=1,
        patch_map={},
        head_sha="c" * 40,
        open_defects=[prior],
        derived=_recorded(),
    )
    assert _plan(Scenario(), _policy(), tmp_path / "plan", monkeypatch, _found(latest))["derived"]
    plan = {
        **_protocol_plan(policy, kind="follow-up", round=2, prior=[prior]),
        "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED},
    }
    verdict = _v("fine")
    verdict["prior_defects"] = [{"id": "D1", "status": "fixed", "evidence": f"{SUMMARY}:1 is corrected"}]
    final = _final(policy, tmp_path / "judge", plan, verdict)
    assert final.judgement is not None and final.judgement.decision == "SHIP"


def test_a_clipped_summary_path_still_reaches_the_title_checks() -> None:
    clipped = "…" + SUMMARY[-31:]
    paths = [MANIFEST, SHARD]
    assert derived.data_anchored(clipped, f"Bad value [{SHARD}:3]"[:39] + "…", paths, SUMMARY)
    assert derived.data_anchored(clipped, f"Bad value [{SHARD}:3]", paths, SUMMARY)
    assert not derived.data_anchored(clipped, "Typo in the summary", paths, SUMMARY)
    assert not derived.data_anchored("…other/file.md", "Typo…", paths, SUMMARY)


def test_a_clipped_legacy_record_at_the_tightest_trim_stays_data_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = {
        "id": "D1",
        "severity": "High",
        "file": "…" + SUMMARY[-31:],
        "line": 1,
        "end_line": None,
        "title": f"Bad value [{SHARD}:3]"[:39] + "…",
    }
    latest = _marker_record(
        decision="SHIP_WITH_FIXES", coverage=1, patch_map={}, head_sha="c" * 40, open_defects=[prior]
    )
    assert "derived" not in _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest))


@pytest.mark.parametrize("variant", ["nfd", "space"])
def test_a_differently_written_data_path_maps_to_the_data_file(variant: str, policy: Policy, tmp_path: Path) -> None:
    data = f"{DATA_DIR}/caf\u00e9.json"
    cited = unicodedata.normalize("NFD", data) if variant == "nfd" else f"{data} "
    held = {"entry": "baseline", "digest": DIGEST, "paths": [data], "summary": SUMMARY, "attested_by": ATTESTED}
    plan = {**_protocol_plan(policy), "derived": held}
    finding = {"severity": "High", "file": cited, "line": 3, "title": "Bad value", "detail": "Wrong."}
    final = _final(policy, tmp_path, plan, _v("bad", [finding]))
    assert final.judgement is not None and final.judgement.decision != "SHIP"
    (defect,) = final.judgement.defects
    assert defect.file == data and defect.title == "Bad value"
    assert derived.data_anchored(cited, "Bad value", [data], SUMMARY)
    assert derived.data_anchored(SUMMARY, f"Bad value [{cited}:3]", [data], SUMMARY)


@pytest.mark.parametrize(
    "title",
    [
        f"Wrong p95 [ms] value [{SHARD}:3]",
        f"Wrong [p95] [ms] value [{SHARD}:3]",
        "Wrong p95 [ms] value […baseline/shard-00.json:3]",
        "Wrong [p95] [ms] value […baseline/shard-00.json:3]",
        f"Wrong p95 [ms] value [{SHARD.upper()}:12]",
    ],
)
def test_a_bracketed_title_does_not_hide_the_data_location(title: str) -> None:
    assert derived.data_anchored(SUMMARY, title, [MANIFEST, SHARD], SUMMARY)
    assert derived.data_anchored("…" + SUMMARY[-31:], title, [MANIFEST, SHARD], SUMMARY)


@pytest.mark.parametrize(
    "title",
    ["Wrong p95 [ms] value [src/other.py:3]", "Wrong [p95] [ms] [src/other.py:3]", "Wrong [ms] value […other.py:3]"],
)
def test_a_non_data_location_after_a_bracketed_title_is_not_data(title: str) -> None:
    assert not derived.data_anchored(SUMMARY, title, [MANIFEST, SHARD], SUMMARY)


def test_a_remapped_bracketed_title_is_recognised_as_data_anchored() -> None:
    from theseus_reviewer.verdict import Finding

    item = Finding("High", "src/x.py", 4, "Wrong [ms] value", "Because.")
    moved = protocol._on_summary(item, SUMMARY, [SHARD])
    assert moved.file == SUMMARY and moved.title == "Wrong [ms] value [src/x.py:4]"
    assert not derived.data_anchored(moved.file, moved.title, [SHARD], SUMMARY)


def _derived_plan(policy: Policy) -> dict[str, Any]:
    return {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}


def _later(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "head_sha": "c" * 40, "patch_digest": "z" * 32}


def _finding(path: str, n: int = 3) -> dict[str, Any]:
    return {"severity": "High", "file": path, "line": 3, "title": f"Bad value {n}", "detail": "Wrong."}


def test_a_derived_do_not_ship_is_followed_by_a_full_review(
    policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = _final(
        policy, tmp_path / "first", _derived_plan(policy), _v("bad", [_finding(SHARD)], decision="DO_NOT_SHIP")
    )
    record = _record(final)
    assert record["decision"] == "DO_NOT_SHIP" and record["open_defects"] == [] and "derived" in record
    plan = _plan(Scenario(), _policy(), tmp_path / "later", monkeypatch, _found(_later(record)))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])
    assert plan["protocol"]["kind"] == "first"


def test_an_over_limit_derived_do_not_ship_is_followed_by_a_full_review(
    policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = [_finding(SHARD, n) for n in range(policy.protocol.max_defects + 1)]
    record = _record(_final(policy, tmp_path / "first", _derived_plan(policy), _v("bad", many)))
    assert record["decision"] == "DO_NOT_SHIP" and "derived" in record
    plan = _plan(Scenario(), _policy(), tmp_path / "later", monkeypatch, _found(_later(record)))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])


def test_a_derived_ship_with_fixes_without_data_findings_allows_a_derived_round(
    policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(_final(policy, tmp_path / "first", _derived_plan(policy), _v("typo", [_finding(SUMMARY)])))
    assert record["decision"] == "SHIP_WITH_FIXES" and "derived" in record
    plan = _plan(Scenario(), _policy(), tmp_path / "later", monkeypatch, _found(_later(record)))
    assert plan["derived"]["entry"] == "baseline" and plan["evidence_files"] == [SUMMARY]


def test_a_same_head_review_and_a_ruling_after_a_derived_do_not_ship_read_in_full(
    policy: Policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(_final(policy, tmp_path / "first", _derived_plan(policy), _v("bad", [], decision="DO_NOT_SHIP")))
    step = _round(_found(record), manual=True, derived_digest=None)
    assert step.kind == protocol.FIRST
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    plan = cli._base_plan(_policy(), "owner/repo", 7, "1")
    plan.update({"base_sha": "b" * 40, "base_ref": "develop", "head_sha": "a" * 40})
    ruling = adjudication.Adjudication("WITHDRAW", "D1", "fine")
    assert cli.adjudicate_round(tmp_path / "ruling", plan, _policy(), _found(record), ruling, None) == 0
    assert plan["decision"] == cli.HOLD


def test_an_open_defect_from_a_full_review_keeps_the_next_round_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = {
        "id": "D1",
        "severity": "High",
        "file": SUMMARY,
        "line": 2,
        "end_line": None,
        "title": "Summary claims X but the shard shows Y",
    }
    latest = _marker_record(
        decision="SHIP_WITH_FIXES", coverage=1, patch_map={}, head_sha="c" * 40, open_defects=[prior]
    )
    assert "derived" not in latest
    plan = _plan(Scenario(), _policy(), tmp_path, monkeypatch, _found(latest))
    assert "derived" not in plan and {MANIFEST, SHARD} <= set(plan["evidence_files"])
