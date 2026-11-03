from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CONSUMER_ALPHA
from theseus_reviewer.classifier import ChangedFile as ReviewFile
from theseus_reviewer.classifier import ClassifierRules, soundness_scope
from theseus_reviewer.inert_paths import INERT_KINDS, InertMatcher
from theseus_reviewer.matchers import load_matchers
from theseus_reviewer.policy import PolicyError, load_policy
from theseus_reviewer.soundness_paths import Matcher, parse_rules

pytestmark = [pytest.mark.unit, pytest.mark.fast]

MANIFEST = CONSUMER_ALPHA / ".github" / "inert-paths.txt"
POLICY = CONSUMER_ALPHA / ".github" / "oracle-reviewers.yml"
EMPTY_RULES = ClassifierRules(gate_paths=(), logic_paths=(), data_paths=(), exempt_paths=())
MACHINE_READ_SAMPLE = [
    "AGENTS.md",
    "README.md",
    "docs/README.md",
    "docs/agent/batch-feature-delivery.md",
    "docs/internal/repo-admin-settings.md",
    "docs/internal/dev-loop-worktrees.md",
    "docs/publish-allowlist.txt",
    "docs/publish-exclusions.txt",
    "tests/uat/CHARTER.md",
    ".claude/settings.json",
    ".claude/commands/blind-spot.md",
    ".claude/skills/code/SKILL.md",
    "_project/scripts/prompts/pr_review_followup.md",
    "benchbox/utils/VERSION_MANAGEMENT.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
]
inert = InertMatcher.from_file(MANIFEST)
matchers = load_matchers(load_policy(POLICY).consumer, CONSUMER_ALPHA)


def _review_file(path: str) -> ReviewFile:
    return ReviewFile(path, None, 5, 0)


def test_seed_exceptions_are_machine_read_but_not_inert() -> None:
    for path in MACHINE_READ_SAMPLE:
        assert inert.is_machine_read_doc(path), path
        assert not inert.is_inert_path(path), path


def test_plain_prose_stays_inert() -> None:
    assert inert.is_inert_path("docs/internal/branch-rename-runbook.md")
    assert inert.is_inert_path("docs/some-guide.md")
    assert inert.is_inert_path("_project/todo-db-export/events.jsonl")
    assert not inert.is_inert_path("benchbox/core/engine.py")
    assert not inert.is_inert_path("")


def test_inert_predicate_normalises_windows_separators() -> None:
    assert inert.is_inert_path("docs\\internal\\branch-rename-runbook.md")


def test_soundness_paths_still_need_review() -> None:
    assert matchers.is_soundness_path(".github/PULL_REQUEST_TEMPLATE.md")


def test_manifest_uses_only_known_kinds() -> None:
    kinds = {line.split("\t", 1)[0] for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert kinds <= INERT_KINDS
    assert "except" in kinds


def test_guard_flags_reference_missing_its_exception() -> None:
    matcher = InertMatcher.from_text("glob\tdocs/**\n")
    assert matcher.matches_exemption("docs/secret.md")
    assert not matcher.matches_exception("docs/secret.md")
    assert matcher.is_inert_path("docs/secret.md")
    excepted = InertMatcher.from_text("glob\tdocs/**\nexcept\tdocs/secret.md\n")
    assert excepted.matches_exemption("docs/secret.md") and excepted.matches_exception("docs/secret.md")
    assert not excepted.is_inert_path("docs/secret.md")
    assert excepted.is_machine_read_doc("docs/secret.md")


def test_from_file_reads_a_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "inert-paths.txt"
    manifest.write_text("glob\tdocs/**\n", encoding="utf-8")
    assert InertMatcher.from_file(manifest).is_inert_path("docs/guide.md")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "inert path manifest is empty"),
        ("bogus\tx\n", "unknown rule kind 'bogus'"),
        ("prefix\tdocs\n", "prefix must end with '/'"),
        ("docs/**\n", "expected kind<TAB>pattern"),
    ],
)
def test_malformed_inert_manifests_are_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        InertMatcher.from_text(text)


def test_any_inert_is_true_when_any_path_is_inert() -> None:
    assert inert.any_inert(["benchbox/core/engine.py", "docs/some-guide.md"]) is True
    assert inert.any_inert(["benchbox/core/engine.py", "AGENTS.md"]) is False


def test_inert_matcher_has_no_default_manifest() -> None:
    with pytest.raises(TypeError):
        InertMatcher()  # type: ignore[call-arg]


def test_matchers_expose_inert_and_protected_classes() -> None:
    assert matchers.is_inert_path("docs/internal/branch-rename-runbook.md")
    assert not matchers.is_inert_path("AGENTS.md")
    assert matchers.is_machine_read_doc("AGENTS.md")
    assert matchers.is_protected("AGENTS.md")
    assert matchers.is_protected("scripts/pr_arm.py")
    assert not matchers.is_protected("docs/internal/branch-rename-runbook.md")


def test_matchers_without_an_inert_manifest_treat_nothing_as_inert() -> None:
    from theseus_reviewer.matchers import Matchers

    bare = Matchers(Matcher(parse_rules("file\ta.py\n")), None)
    assert bare.is_inert_path("docs/guide.md") is False
    assert bare.is_machine_read_doc("AGENTS.md") is False
    assert bare.is_protected("a.py") is True


def test_load_matchers_refuses_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_matchers(load_policy(POLICY).consumer, tmp_path)


def test_inert_only_change_is_out_of_scope() -> None:
    scope = soundness_scope([_review_file("docs/internal/branch-rename-runbook.md")], EMPTY_RULES, matchers)
    assert scope.soundness is False


def test_machine_read_change_is_in_scope_without_policy_exemptions() -> None:
    scope = soundness_scope([_review_file("AGENTS.md")], EMPTY_RULES, matchers)
    assert scope.soundness is True
    assert scope.paths == ("AGENTS.md",)


def test_gate_template_still_wins_over_the_manifest_exception() -> None:
    gate = "_project/scripts/oracle_reviewers/templates/gate.md"
    scope = soundness_scope(
        [_review_file(gate)],
        ClassifierRules(
            gate_paths=("_project/scripts/oracle_reviewers/*",), logic_paths=(), data_paths=(), exempt_paths=()
        ),
        matchers,
    )
    assert scope.soundness is True
    assert scope.gate_paths == (gate,)
