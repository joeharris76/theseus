from __future__ import annotations

import copy
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
    monkeypatch.setattr(cli.github, "review_threads", lambda *_args: [])
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


def test_findings_on_data_lines_are_not_counted(policy: Policy, tmp_path: Path) -> None:
    plan = {**_protocol_plan(policy), "derived": {**_recorded(), "summary": SUMMARY, "attested_by": ATTESTED}}
    verdict = _v("fine", [{"severity": "High", "file": SHARD, "line": 2, "title": "Data", "detail": ""}])
    final = _final(policy, tmp_path, plan, verdict)
    assert final.judgement is not None and final.judgement.defects == ()


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


def test_a_local_outcome_and_member_record_carry_the_reliance(monkeypatch: pytest.MonkeyPatch) -> None:
    reliance = {"entry": "baseline", "digest": DIGEST, "attested_by": ATTESTED}
    outcome = local.Outcome(False, "SHIP", "small", "", (), derived=reliance)
    restored = local.Outcome.from_json(outcome.to_json())
    assert restored is not None and restored.derived == reliance
    assert local.recorded_outcome(_bound()).derived == {"entry": "baseline", "digest": DIGEST}
    monkeypatch.setattr(local, "member_binding", lambda *_args: {"head_tree": "t", "patch_digest": "p"})
    member = local._member_record(Path("."), "b" * 40, "a" * 40, "c" * 40, "clear", "ok", reliance)
    assert member["derived"] == reliance
    assert "derived" not in local._member_record(Path("."), "b" * 40, "a" * 40, "c" * 40, "clear", "ok")


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
