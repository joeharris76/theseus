from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from zipfile import ZipFile

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.medium]

ROOT = Path(__file__).resolve().parents[2]
SEEDS = (
    "v022-substitute-capacity-bypass",
    "pr3962-test-weakenings",
    "legacy-transition-stdlib-patching",
    "pr41-forced-fixed-dispute",
)
CONTROL = "control-private-helper-rename"
POLICY = "version: 2\nconsumer:\n  gated_base: main\n  review_guidance: .github/review-guidance.md\n"
TEMPLATE = "src/theseus_reviewer/templates/standard.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_eval", ROOT / "tools" / "review_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load()
CASES = TOOL.load_cases(ROOT)
AUTHENTICATED_RESULT = TOOL.authenticated_result
TRUSTED_RESULTS: dict[str, list[dict[str, Any]]] = {}


def _finding(path: str, title: str, detail: str = "") -> dict[str, Any]:
    return {"severity": "Medium", "file": path, "line": 1, "title": title, "detail": detail}


def _observation(decision: str, *findings: dict[str, Any]) -> dict[str, Any]:
    return {"status": TOOL.REVIEWED, "decision": decision, "reviewer": "r", "summary": "", "findings": list(findings)}


def _perfect(case: Any) -> dict[str, Any]:
    reply = case.fake["good"][0]
    return _observation(reply["decision"], *reply.get("defects", []))


def test_the_corpus_holds_every_seeded_incident_and_a_clean_control() -> None:
    assert set(SEEDS) | {CONTROL} == set(CASES)
    for case_id in SEEDS:
        assert CASES[case_id].findings and "SHIP" not in CASES[case_id].decisions
    assert CASES[CONTROL].findings == () and CASES[CONTROL].decisions == ("SHIP",)


def test_every_case_carries_a_recorded_good_and_regressed_reply() -> None:
    for case in CASES.values():
        assert set(case.fake) == {"good", "regressed"}
        assert TOOL.score_observation(case, _perfect(case))["score"] == 1.0


def test_the_weights_sum_to_one() -> None:
    assert abs(sum(TOOL.WEIGHTS.values()) - 1.0) < 1e-12


def test_a_matcher_needs_the_path_and_one_word_from_every_group() -> None:
    matcher = TOOL.Matcher("a.py", (("fixed",), ("evidence", "rationale")))

    assert matcher.matches(_finding("a.py", "Marked FIXED", "against its own Rationale"))
    assert matcher.matches(_finding("./a.py", "fixed", "evidence"))
    assert not matcher.matches(_finding("b.py", "fixed", "evidence"))
    assert not matcher.matches(_finding("a.py", "fixed", "nothing else"))


def test_one_finding_cannot_satisfy_two_expectations() -> None:
    case = CASES["pr3962-test-weakenings"]
    secret = case.findings[0].matcher.path
    combined = _finding(secret, "secret mapping checked by key only", "edited cases dropped")

    score = TOOL.score_observation(case, _observation("SHIP_WITH_FIXES", combined))

    assert score["recall"] == 0.5 and len(score["missed"]) == 1


def test_a_shared_finding_is_reassigned_so_both_expectations_are_met_when_possible() -> None:
    expected = (
        TOOL.Expected("one", TOOL.Matcher("a.py", (("x", "y"),))),
        TOOL.Expected("two", TOOL.Matcher("a.py", (("x",),))),
    )
    found = [_finding("a.py", "x and y"), _finding("a.py", "x only")]

    assert len(TOOL._assign(expected, found)) == 2


def test_findings_outside_the_expectations_are_noise_unless_acceptable() -> None:
    case = CASES["pr41-forced-fixed-dispute"]
    good = case.fake["good"][0]["defects"][0]
    acceptable = _finding(
        "tests/unit/reviewer/test_protocol.py", "pins the fixed to maintained flip", "asserts maintained"
    )
    stray = _finding("src/theseus_reviewer/protocol.py", "Missing type hint", "unrelated")

    quiet = TOOL.score_observation(case, _observation("SHIP_WITH_FIXES", good, acceptable))
    noisy = TOOL.score_observation(case, _observation("SHIP_WITH_FIXES", good, stray))

    assert quiet["noise"] == 0 and quiet["score"] == 1.0
    assert noisy["noise"] == 1 and noisy["precision"] == 0.5 and noisy["score"] < 1.0


def test_the_decision_label_is_normalised_and_a_wrong_decision_costs_its_weight() -> None:
    case = CASES["v022-substitute-capacity-bypass"]
    defect = case.fake["good"][0]["defects"][0]

    assert TOOL.score_observation(case, _observation("SHIP WITH FIXES", defect))["decision"] == 1.0
    wrong = TOOL.score_observation(case, _observation("SHIP", defect))
    assert wrong["decision"] == 0.0 and wrong["score"] == pytest.approx(0.8)


def test_a_case_that_was_not_reviewed_scores_zero_everywhere() -> None:
    case = CASES[CONTROL]

    score = TOOL.score_observation(case, TOOL.unreviewed(TOOL.UNAVAILABLE, "gateway unreachable"))

    assert (score["recall"], score["precision"], score["decision"], score["score"]) == (0.0, 0.0, 0.0, 0.0)


def test_repeats_are_averaged_per_case_and_across_cases() -> None:
    case = CASES[CONTROL]
    runs = {
        case_id: [_perfect(item)]
        if case_id != CONTROL
        else [_perfect(case), _observation("SHIP_WITH_FIXES", _finding("x", "y"))]
        for case_id, item in CASES.items()
    }

    result = TOOL.build_result(CASES, runs, mode="live", meta={"date": "d", "git_sha": "g", "dirty": False})

    assert result["cases"][CONTROL]["score"]["score"] == pytest.approx(0.8)
    assert result["aggregate"]["score"] == pytest.approx((4 + 0.8) / 5)


def _write_case(root: Path, name: str, **overrides: Any) -> Path:
    directory = root / name
    (directory / "head").mkdir(parents=True)
    (directory / "head" / "a.py").write_text("x = 1\n", encoding="utf-8")
    data: dict[str, Any] = {
        "id": name,
        "incident": "an incident",
        "message": "change",
        "expected": {"decisions": ["SHIP"], "findings": [], "acceptable": []},
        **overrides,
    }
    (directory / "case.json").write_text(json.dumps(data), encoding="utf-8")
    return directory


def test_a_well_formed_case_loads(tmp_path: Path) -> None:
    _write_case(tmp_path, "valid-case")

    assert TOOL.load_case(tmp_path / "valid-case").id == "valid-case"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "other-name"},
        {"surprise": 1},
        {"expected": {"decisions": ["SHIP"], "findings": [], "acceptable": [], "x": 1}},
        {"expected": {"decisions": ["MAYBE"], "findings": [], "acceptable": []}},
        {
            "expected": {
                "decisions": ["SHIP"],
                "findings": [{"id": "f-one", "path": "a.py", "terms": [["x"]]}],
                "acceptable": [],
            }
        },
        {
            "expected": {
                "decisions": ["SHIP_WITH_FIXES"],
                "findings": [{"id": "f-one", "path": "a.py", "terms": [["Upper"]]}],
                "acceptable": [],
            }
        },
        {"fake": {"good": [{"decision": "SHIP", "summary": "s"}]}},
    ],
)
def test_a_malformed_case_is_refused(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _write_case(tmp_path, "valid-case", **overrides)

    with pytest.raises(TOOL.EvalError):
        TOOL.load_case(tmp_path / "valid-case")


def test_a_case_with_a_secret_shape_or_without_a_change_is_refused(tmp_path: Path) -> None:
    leaking = _write_case(tmp_path, "leaking-case")
    (leaking / "head" / "a.py").write_text("token = 'ghp_" + "a" * 30 + "'\n", encoding="utf-8")
    unchanged = _write_case(tmp_path, "unchanged-case")
    (unchanged / "base").mkdir()
    (unchanged / "base" / "a.py").write_text("x = 1\n", encoding="utf-8")

    for name in ("leaking-case", "unchanged-case"):
        with pytest.raises(TOOL.EvalError):
            TOOL.load_case(tmp_path / name)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _meta(repository: Path, *, dirty: bool = False) -> dict[str, Any]:
    tree = TOOL.committed_tree(repository, "HEAD")
    return {
        "date": "2026-11-08",
        "git_sha": _git(repository, "rev-parse", "HEAD"),
        "dirty": dirty,
        "config_digest": TOOL.tree_digest(TOOL.config_entries(tree)),
        "corpus_digest": TOOL.tree_digest(TOOL.corpus_entries(tree)),
    }


def _result(repository: Path, variant: str, *, mode: str = "live", dirty: bool = False) -> dict[str, Any]:
    runs = {}
    for case_id, case in CASES.items():
        reply = case.fake[variant][0]
        runs[case_id] = [_observation(reply["decision"], *reply.get("defects", []))]
    return TOOL.build_result(CASES, runs, mode=mode, meta=_meta(repository, dirty=dirty))


def _attach(repository: Path, label: str, result: dict[str, Any], *, named: str = "", trusted: bool = True) -> str:
    result["git_sha"] = (label.encode().hex() + "0" * 12)[:40]
    name = named or f"{result['date']}-{result['git_sha'][:12]}.json"
    target = repository / TOOL.RESULTS_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result), encoding="utf-8")
    if trusted:
        TRUSTED_RESULTS.setdefault(str(repository), []).append(json.loads(json.dumps(result)))
    return name


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(
        TOOL,
        "authenticated_result",
        lambda root, tree, result: result in TRUSTED_RESULTS.get(str(root), []),
    )
    _git(root, "init", "--quiet", "--initial-branch=main")
    shutil.copytree(ROOT / "evals" / "cases", root / "evals" / "cases")
    for relative, text in {
        ".github/review.yml": POLICY,
        ".github/review-guidance.md": "Look for broken invariants.\n",
        TEMPLATE: "template one\n",
        "src/theseus_reviewer/gate.py": "x = 1\n",
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    _commit(root, "base")
    return root


def _change(repository: Path, relative: str, text: str) -> str:
    _git(repository, "switch", "--quiet", "-c", "change")
    (repository / relative).write_text(text, encoding="utf-8")
    return _commit(repository, "change")


def _check(repository: Path, base: str) -> Any:
    return TOOL.check_change(repository, base, "HEAD", "2026-12-01")


def test_a_diff_without_reviewer_configuration_needs_no_score(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _change(repository, "unrelated.txt", "x = 2\n")

    verdict = _check(repository, base)

    assert verdict.ok and "nothing to score" in verdict.lines[0]


@pytest.mark.parametrize(
    "relative", [TEMPLATE, ".github/review.yml", ".github/review-guidance.md", "src/theseus_reviewer/render.py"]
)
def test_a_configuration_only_change_is_inactive_and_passes_explicitly(repository: Path, relative: str) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _change(repository, relative, POLICY + "# changed\n" if relative.endswith("review.yml") else "changed\n")

    verdict = _check(repository, base)

    assert verdict.ok
    assert "no trusted candidate executor: the score rule is inactive" in verdict.lines[-1]
    assert "owner whole-diff review applies" in verdict.lines[-1]


def test_a_results_change_without_an_authenticated_baseline_is_blocked(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _change(repository, TEMPLATE, "changed\n")
    _attach(repository, "head", _result(repository, "good"), trusted=False)
    _commit(repository, "attach")

    verdict = _check(repository, base)

    assert not verdict.ok and "no authenticated baseline" in verdict.lines[-1]


def _with_baseline(repository: Path) -> str:
    baseline = _result(repository, "good")
    baseline["date"] = "2026-11-01"
    _attach(repository, "baseline", baseline)
    return _commit(repository, "baseline")


@pytest.mark.parametrize("relative", [TEMPLATE, ".github/review.yml", ".github/review-guidance.md"])
def test_a_configuration_change_with_an_unauthenticated_result_is_blocked(repository: Path, relative: str) -> None:
    base = _with_baseline(repository)
    _change(repository, relative, POLICY + "# changed\n" if relative.endswith("review.yml") else "changed\n")
    _attach(repository, "head", _result(repository, "good"), trusted=False)
    _commit(repository, "attach")

    verdict = _check(repository, base)

    assert not verdict.ok and "exactly one new result" in " ".join(verdict.lines)


def test_an_attached_run_that_does_not_lower_the_score_passes(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    _attach(repository, "head", _result(repository, "good"))
    _commit(repository, "attach")

    verdict = _check(repository, base)

    assert verdict.ok, verdict.lines


def test_an_attached_run_that_lowers_the_score_fails(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    _attach(repository, "head", _result(repository, "regressed"))
    _commit(repository, "attach")

    verdict = _check(repository, base)

    assert not verdict.ok and "lowers the eval score" in verdict.lines[-1]


def test_a_stored_score_cannot_hide_a_lower_run(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    forged = _result(repository, "regressed")
    forged["aggregate"]["score"] = 1.0
    for entry in forged["cases"].values():
        entry["score"]["score"] = 1.0
    _attach(repository, "head", forged)
    _commit(repository, "attach")

    assert not _check(repository, base).ok


def test_a_run_for_other_configuration_or_of_another_kind_is_not_accepted(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    stale = _result(repository, "good")
    stale["config_digest"] = "0" * 64
    _attach(repository, "stale", stale)
    _attach(repository, "fake", _result(repository, "good", mode="fake"))
    _attach(repository, "dirty", _result(repository, "good", dirty=True))
    _commit(repository, "attach")

    verdict = _check(repository, base)

    assert not verdict.ok and "exactly one new result" in " ".join(verdict.lines)


def test_an_incomplete_run_is_not_accepted(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    runs = {case_id: [TOOL.unreviewed(TOOL.UNAVAILABLE, "down")] for case_id in CASES}
    incomplete = TOOL.build_result(CASES, runs, mode="live", meta=_meta(repository))
    _attach(repository, "head", incomplete)
    _commit(repository, "attach")

    assert not _check(repository, base).ok


def test_the_baseline_is_scored_with_the_base_expectations(repository: Path) -> None:
    base = _with_baseline(repository)
    _git(repository, "switch", "--quiet", "-c", "change")
    expected = repository / "evals" / "cases" / CONTROL / "case.json"
    data = json.loads(expected.read_text(encoding="utf-8"))
    data["expected"]["decisions"] = ["SHIP", "SHIP_WITH_FIXES", "DO_NOT_SHIP"]
    expected.write_text(json.dumps(data), encoding="utf-8")
    _commit(repository, "loosen only the corpus expectations")
    _attach(repository, "head", _result(repository, "regressed"))
    _commit(repository, "attach")

    assert not _check(repository, base).ok


def test_the_run_state_records_uncommitted_reviewer_configuration(repository: Path) -> None:
    assert TOOL.git_state(repository)["dirty"] is False
    (repository / TEMPLATE).write_text("uncommitted\n", encoding="utf-8")

    assert TOOL.git_state(repository)["dirty"] is True


def test_the_case_repository_has_the_base_below_the_head_on_the_gated_branch(tmp_path: Path) -> None:
    skeleton = {".github/review.yml": POLICY.encode(), ".github/review-guidance.md": b"guidance\n"}
    case = CASES["pr41-forced-fixed-dispute"]

    repository, base, head = TOOL.build_repository(case, skeleton, tmp_path / "work")

    assert _git(repository, "merge-base", base, head) == base
    assert _git(repository, "show", f"{base}:.github/review-guidance.md") == "guidance"
    assert _git(repository, "branch", "--show-current") == f"eval/{case.id}"
    assert "main" in _git(repository, "branch", "--list", "main")
    changed = set(_git(repository, "diff", "--name-only", base, head).splitlines())
    assert changed == {path for path, data in case.head.items() if case.base.get(path) != data}


def test_the_skeleton_is_the_policy_manifests_and_named_guidance_without_workflows(tmp_path: Path) -> None:
    root = tmp_path / "consumer"
    for relative, text in {
        ".github/review.yml": POLICY + "  soundness_manifest: .github/soundness-paths.txt\n",
        ".github/soundness-paths.txt": "prefix\tsrc/\n",
        ".github/workflows/ci.yml": "name: ci\n",
        ".github/theseus-lock.json": "{}\n",
        ".github/review-guidance.md": "guidance\n",
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")

    assert set(TOOL.skeleton_files(root)) == {
        ".github/review.yml",
        ".github/soundness-paths.txt",
        ".github/review-guidance.md",
    }


def test_the_review_command_runs_the_member_mode_against_exact_commits() -> None:
    command = TOOL.review_command("b" * 40, "h" * 40, Path("/tmp/member.json"))

    assert command[1:4] == ["-m", "theseus_reviewer", "local-review"]
    assert command[command.index("--base-ref") + 1] == "b" * 40
    assert command[command.index("--trusted-ref") + 1] == "b" * 40
    assert command[command.index("--head") + 1] == "h" * 40
    assert "--member" in command


def test_the_outcome_a_review_cached_becomes_an_observation(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    outcome = {
        "blocked": False,
        "label": "SHIP WITH FIXES",
        "reviewer": "large",
        "summary": "s",
        "findings": [{"severity": "Medium", "file": "./a.py", "line": 3, "title": "t", "detail": "d"}],
        "record": None,
    }
    (cache / "k.json").write_text(json.dumps(outcome), encoding="utf-8")

    observation = TOOL.observation_from_outcome(TOOL.parse_outcome(tmp_path))

    assert observation["decision"] == "SHIP_WITH_FIXES"
    assert observation["findings"] == [{"severity": "Medium", "file": "a.py", "line": 3, "title": "t", "detail": "d"}]
    assert TOOL.parse_outcome(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("relative", "triggers"),
    [
        ("src/theseus_reviewer/templates/deep.md", True),
        ("src/theseus_reviewer/protocol.py", True),
        ("src/theseus_reviewer/cli.py", True),
        ("src/theseus_reviewer/new_module.py", True),
        ("src/theseus_reviewer/commands.py", True),
        ("src/theseus_reviewer/matchers.py", True),
        ("src/theseus_reviewer/soundness_paths.py", True),
        ("src/theseus_reviewer/gate.py", True),
        ("src/theseus_reviewer/bootstrap.py", True),
        ("src/theseus_reviewer/sweep.py", True),
        ("src/theseus_reviewer/github.py", True),
        ("src/theseus_reviewer/_vendor/yaml/__init__.py", True),
        ("src/theseus_reviewer/protocol_helpers/judgment.py", True),
        ("tools/review_eval.py", True),
        ("evals/cases/control/case.json", True),
        (".github/workflows/review-eval.yml", True),
        (".github/workflows/ci.yml", True),
        ("tests/unit/test_gate.py", False),
        (".github/review-omp-config.yml", True),
    ],
)
def test_every_engine_subpackage_and_eval_input_triggers(relative: str, triggers: bool) -> None:
    assert TOOL.is_reviewer_config(relative, set()) is triggers


def test_the_baseline_is_the_newest_by_date_not_by_path() -> None:
    def usable(date: str, sha: str) -> dict[str, Any]:
        return {"schema": 1, "mode": "live", "complete": True, "dirty": False, "date": date, "git_sha": sha}

    results = {
        "evals/results/2026-11-01-aaaaaaaaaaaa.json": usable("2026-11-01", "a" * 12),
        "evals/results/2026-11-05-bbbbbbbbbbbb.json": usable("2026-11-05", "b" * 12),
        "evals/results/2026-11-03-zzzzzzzzzzzz.json": usable("2026-11-03", "z" * 12),
    }

    path, _ = TOOL.latest_baseline(results, "2026-12-01")

    assert path.endswith("2026-11-05-bbbbbbbbbbbb.json")


def test_a_result_with_the_wrong_name_or_a_future_date_is_never_a_baseline() -> None:
    good = {"schema": 1, "mode": "live", "complete": True, "dirty": False, "date": "2026-11-05", "git_sha": "b" * 40}

    assert TOOL.usable_result(good, "evals/results/2026-11-05-bbbbbbbbbbbb.json", "2026-12-01") == []
    assert TOOL.usable_result(good, "evals/results/2026-11-05-other.json", "2026-12-01")
    assert TOOL.usable_result(good, "evals/results/2026-11-05-bbbbbbbbbbbb.json", "2026-11-04")
    assert TOOL.usable_result({**good, "date": "soon"}, "evals/results/soon-bbbbbbbbbbbb.json", "2026-12-01")
    assert TOOL.latest_baseline({"evals/results/2026-11-05-other.json": good}, "2026-12-01") is None


def test_a_misnamed_future_dated_result_cannot_steer_the_baseline(repository: Path) -> None:
    base = _with_baseline(repository)
    steering = _result(repository, "regressed")
    steering["date"] = "2099-01-01"
    _git(repository, "switch", "--quiet", "-c", "change")
    _attach(repository, "steer", steering)
    _attach(repository, "steer2", _result(repository, "regressed"), named="2026-11-30-chosen-name.json")
    _commit(repository, "add results")

    verdict = _check(repository, base)

    assert not verdict.ok and "exactly one new result" in " ".join(verdict.lines)


def test_a_results_only_change_requires_authenticated_matching_evidence(repository: Path) -> None:
    base = _with_baseline(repository)
    _git(repository, "switch", "--quiet", "-c", "change")
    stale = _result(repository, "good")
    stale["config_digest"] = "0" * 64
    _attach(repository, "stale", stale)
    _commit(repository, "add a result")

    verdict = _check(repository, base)

    assert not verdict.ok and "authenticated live run" in " ".join(verdict.lines)


def test_a_results_only_change_with_matching_evidence_and_no_lower_score_passes(repository: Path) -> None:
    base = _with_baseline(repository)
    _git(repository, "switch", "--quiet", "-c", "change")
    _attach(repository, "next", _result(repository, "good"))
    _commit(repository, "add a result")

    assert _check(repository, base).ok


def test_a_recorded_result_cannot_be_edited_or_removed(repository: Path) -> None:
    base = _with_baseline(repository)
    _git(repository, "switch", "--quiet", "-c", "change")
    recorded = next((repository / TOOL.RESULTS_DIR).glob("*.json"))
    recorded.write_text(
        recorded.read_text(encoding="utf-8").replace('"complete": true', '"complete": true '), encoding="utf-8"
    )
    _commit(repository, "edit a result")

    verdict = _check(repository, base)

    assert not verdict.ok and "append-only" in " ".join(verdict.lines)


def test_extra_results_beside_the_matching_one_are_refused(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "template two\n")
    _attach(repository, "head", _result(repository, "good"))
    _attach(repository, "extra", _result(repository, "regressed"))
    _commit(repository, "attach two")

    verdict = _check(repository, base)

    assert not verdict.ok and "exactly one new result" in " ".join(verdict.lines)


def test_renaming_a_recorded_result_out_of_the_results_directory_is_refused(repository: Path) -> None:
    base = _with_baseline(repository)
    _git(repository, "switch", "--quiet", "-c", "change")
    recorded = next((repository / TOOL.RESULTS_DIR).glob("*.json"))
    _git(repository, "mv", str(recorded), str(repository / "moved.json"))
    _commit(repository, "move a result")

    verdict = _check(repository, base)

    assert not verdict.ok and "append-only" in " ".join(verdict.lines)


def test_every_skeleton_file_is_reviewer_configuration() -> None:
    files = {
        ".github/review.yml": POLICY + "  soundness_manifest: docs/manifest.txt\n",
        ".github/soundness-paths.txt": "prefix\tsrc/\n",
        ".github/workflows/ci.yml": "name: ci\n",
        ".github/theseus-lock.json": "{}\n",
        "docs/manifest.txt": "x\n",
        "docs/other.md": "x\n",
    }
    tree = TOOL.Tree(tuple(sorted(files)), lambda path: files[path].encode())
    guidance = TOOL.guidance_paths(tree)

    assert TOOL.is_reviewer_config(".github/soundness-paths.txt", guidance)
    assert TOOL.is_reviewer_config("docs/manifest.txt", guidance)
    assert TOOL.is_reviewer_config(".github/workflows/ci.yml", guidance)
    assert not TOOL.is_reviewer_config(".github/theseus-lock.json", guidance)
    assert not TOOL.is_reviewer_config("docs/other.md", guidance)


def test_a_forged_live_result_with_matching_digests_is_rejected(repository: Path) -> None:
    base = _with_baseline(repository)
    _change(repository, TEMPLATE, "changed\n")
    _attach(repository, "forged", _result(repository, "good"), trusted=False)
    _commit(repository, "forged evidence")

    assert not _check(repository, base).ok


def test_trusted_results_only_bootstrap_is_enforced(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _attach(repository, "bootstrap", _result(repository, "good"))
    _commit(repository, "bootstrap")

    verdict = _check(repository, base)

    assert verdict.ok and "bootstrapped" in verdict.lines[-1]


@pytest.mark.parametrize(
    "relative",
    ["src/theseus_reviewer/gate.py", "tools/review_eval.py", f"evals/cases/{CONTROL}/case.json"],
)
def test_gate_scorer_and_corpus_changes_alter_the_configuration_digest(repository: Path, relative: str) -> None:
    before = TOOL.git_state(repository)["config_digest"]
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")

    assert TOOL.git_state(repository)["config_digest"] != before
    assert TOOL.git_state(repository)["dirty"]


@pytest.fixture
def authenticated_execution(repository: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    result = {**_result(repository, "good"), "workflow_run_id": "123"}
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("result.json", json.dumps(result))
    data = output.getvalue()
    run = {
        "workflow_id": 42,
        "event": "schedule",
        "head_branch": "main",
        "head_sha": result["git_sha"],
        "conclusion": "success",
        "repository": {"full_name": TOOL.EVAL_REPOSITORY},
        "head_repository": {"full_name": TOOL.EVAL_REPOSITORY},
    }
    artifact = {
        "id": 12,
        "name": TOOL.EVAL_ARTIFACT,
        "expired": False,
        "digest": f"sha256:{TOOL.hashlib.sha256(data).hexdigest()}",
    }
    responses = {
        f"actions/workflows/{TOOL.EVAL_WORKFLOW}": {"id": 42},
        "actions/runs/123": run,
        "actions/runs/123/artifacts?per_page=100": {"total_count": 1, "artifacts": [artifact]},
    }
    monkeypatch.setattr(TOOL, "github_json", responses.__getitem__)
    monkeypatch.setattr(TOOL, "github_bytes", lambda path: data)
    return result, run


def test_authenticated_scheduled_artifact_is_accepted(
    repository: Path, authenticated_execution: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    result, _ = authenticated_execution

    assert AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_id", 99),
        ("event", "pull_request"),
        ("head_branch", "candidate"),
        ("head_sha", "0" * 40),
        ("conclusion", "failure"),
        ("head_repository", {"full_name": "attacker/theseus"}),
    ],
)
def test_untrusted_execution_cannot_borrow_a_workflow_name(
    repository: Path,
    authenticated_execution: tuple[dict[str, Any], dict[str, Any]],
    field: str,
    value: Any,
) -> None:
    result, run = authenticated_execution
    run[field] = value

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


def test_forged_observations_cannot_borrow_a_trusted_run(
    repository: Path, authenticated_execution: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    result, _ = authenticated_execution
    result["cases"][CONTROL]["runs"][0]["observation"]["summary"] = "forged"

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


def test_main_execution_does_not_authenticate_candidate_source(
    repository: Path, authenticated_execution: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    result, _ = authenticated_execution
    _change(repository, TEMPLATE, "candidate\n")
    result.update(
        config_digest=TOOL.git_state(repository)["config_digest"],
        corpus_digest=TOOL.git_state(repository)["corpus_digest"],
    )

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


def test_matching_public_digests_are_not_execution_authentication(repository: Path) -> None:
    result = _result(repository, "good")

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


def test_a_forged_result_cannot_bootstrap(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _attach(repository, "forged", _result(repository, "good"), trusted=False)
    _commit(repository, "forged baseline")

    assert not _check(repository, base).ok


def test_corrupted_artifact_download_is_rejected(
    repository: Path,
    authenticated_execution: tuple[dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = authenticated_execution
    monkeypatch.setattr(TOOL, "github_bytes", lambda path: b"forged archive")

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


def test_artifact_api_failure_is_closed(
    repository: Path,
    authenticated_execution: tuple[dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = authenticated_execution

    def unavailable(path: str) -> Any:
        raise TOOL.URLError("offline")

    monkeypatch.setattr(TOOL, "github_json", unavailable)

    assert not AUTHENTICATED_RESULT(repository, TOOL.committed_tree(repository, "HEAD"), result)


@pytest.mark.parametrize("relative", [TEMPLATE, "unrelated.txt", ".github/workflows/ci.yml", "tools/review_eval.py"])
def test_bootstrap_rejects_any_non_result_change(repository: Path, relative: str) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    _commit(repository, "change before bootstrap")
    _attach(repository, "bootstrap", _result(repository, "good"))
    _commit(repository, "attach trusted result")

    verdict = _check(repository, base)

    assert not verdict.ok and "results-only" in verdict.lines[-1]
