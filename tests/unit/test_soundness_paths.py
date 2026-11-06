from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ALPHA_POLICY, CONSUMER_ALPHA
from theseus_reviewer import soundness_paths
from theseus_reviewer.soundness_paths import Matcher, Rule, load_rules, parse_rules

MANIFEST = CONSUMER_ALPHA / ".github" / "soundness-paths.txt"
OVERRIDE_FILES_GLOB = "results-data/bundles/**/*.override.json"
soundness = Matcher(load_rules(MANIFEST))

pytestmark = [pytest.mark.unit, pytest.mark.fast]


@pytest.mark.parametrize(
    "path",
    [
        "benchbox/core/tpchavoc/validation.py",
        "benchbox/core/validation/engines.py",
        "benchbox/utils/data_validation.py",
        "benchbox/core/tpcdi/query_validation.py",
        "benchbox/core/tpchavoc/dataframe_equivalence.py",
        "benchbox/core/tpchavoc/equivalence.py",
        "benchbox/core/results/result_digest.py",
        "_project/scripts/regenerate_correctness_gate_digests.py",
        "benchbox/core/dataframe/query_validation.py",
        "benchbox/core/datavault/validation_specs.yaml",
        "benchbox/core/validation/cross_platform.py",
        "benchbox/core/validation/query_validation.py",
        "benchbox/core/validation/data.py",
        "benchbox/platforms/base/validation.py",
        "benchbox/core/tpc_validation.py",
        "_project/scripts/build_joinorder_data.py",
        "_project/joinorder/reference_cardinalities.json",
        "_project/joinorder/tiny_reference_cardinalities.json",
        "_sources/tpc-h/dbgen/answers/q1.out",
        "_sources/tpc-ds/answer_sets/1.ans",
        "benchbox/core/results/validation.py",
        "benchbox/core/equivalence/cross_surface.py",
        "benchbox/core/equivalence/nested/module.py",
        "benchbox/core/query_plans/parsers/spark.py",
        r"benchbox\core\query_plans\parsers\spark.py",
        "benchbox/core/expected_results/loader.py",
        "benchbox/core/expected_results/registry.py",
        "benchbox/core/expected_results/reference_digests/tpch_value_digests_sf1.json",
        "benchbox/platforms/base/result_capture.py",
        "benchbox/platforms/base/tuning_trust.py",
        "benchbox/core/tuning/introspection.py",
        "benchbox/core/tuning/applied_ledger.py",
        "benchbox/core/tuning/capability_registry.py",
        "benchbox/core/tuning/metadata.py",
        "benchbox/core/tuning/reconciliation.py",
        "benchbox/platforms/duckdb_introspection.py",
        "benchbox/platforms/snowflake_introspection.py",
        "benchbox/platforms/clickhouse/introspection.py",
        "benchbox/sql_compat/resolver.py",
        "benchbox/sql_compat/decision.py",
        "benchbox/sql_compat/rules/_registration.py",
        "_project/scripts/review_check.py",
        ".github/workflows/review-request.yml",
        ".github/workflows/review-verdict.yml",
        "benchbox/core/results/anonymization.py",
        "benchbox/core/results/anonymization_specs.yaml",
        "benchbox/core/results/provenance.py",
        "benchbox/core/results/status.py",
        "benchbox/core/results/query_status.py",
        "benchbox/validation/bundle.py",
        "benchbox/core/publishing/admission.py",
        "benchbox/core/publishing/bundle_publisher.py",
        "_project/scripts/explorer_pipeline/models.py",
        "_project/scripts/explorer_pipeline/pipeline.py",
        "_project/scripts/explorer_pipeline/ranking.py",
        "_project/scripts/explorer_pipeline/transformer.py",
        "_project/scripts/explorer_pipeline/duckdb_builder.py",
        "_project/scripts/explorer_pipeline/compare_math.py",
        "_project/scripts/explorer_pipeline/browser_fixtures.py",
        "_project/scripts/results_explorer_snapshot_invariants.py",
        "benchbox/core/results/canonical_json.py",
        "benchbox/core/results/schema_policy.py",
        "_project/scripts/explorer_publish.py",
        "scripts/generate_corpus_inventory.py",
        "scripts/validate_submission.py",
        "scripts/site_inputs.py",
        "scripts/docs_history_filter.py",
        "scripts/check_docs_ownership.py",
        "quality/docs-ownership.json",
        "make/site-inputs.mk",
        "_project/scripts/soundness_merge_digest.py",
        ".github/workflows/soundness-merge-digest.yml",
        "results-data/bundles/tpch/duckdb/sf1.override.json",
        "results-data/bundles/sf1.override.json",
        ".github/workflows/validate-submission.yml",
        ".github/workflows/sync-results-data-to-published.yml",
        "_project/scripts/soundness_paths.py",
        ".github/workflows/release.yml",
        ".github/workflows/pr.yml",
        ".github/workflows/release-canary.yml",
        ".github/workflows/trunk.yml",
        ".github/workflows/nightly-v2.yml",
        ".github/workflows/new-example.yml",
        "scripts/pr_arm.py",
        "scripts/pr_arm_omp_bump.py",
        "scripts/pr_landing.py",
        "scripts/remote_closeout.py",
        "_project/decisions/single-repo-migration.md",
    ],
)
def test_soundness_predicate_matches_review_required_paths(path: str) -> None:
    assert soundness.is_soundness_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "benchbox/core/tpchavoc/benchmark.py",
        "benchbox/core/query_plans/comparison.py",
        "_project/decisions/independent-publication-a0-freeze-2026-08-31.md",
        "docs/internal/adr/adr-independent-publication-authorities.md",
        "docs/internal/adr/adr-duckdb-datasketches-vendoring.md",
        "docs/internal/adr/adr-published-results-slim-corpus-branch.md",
        "docs/internal/independent-publication-threat-model.md",
        "docs/internal/independent-publication-contract.md",
        "docs/internal/results-phase-2-runbook.md",
        "docs/internal/results-phase-3-runbook.md",
        "docs/internal/site-deploy.md",
        "docs/reference/threat-model.md",
        "tests/unit/scripts/test_soundness_paths.py",
        "AGENTS.md",
        ".github/workflowsx/new-example.yml",
        "scripts/pr_arm.py.bak",
        "_sources/tpc-h/dbgen/answersx/q1.out",
        "_sources/tpc-ds/answer_sets_backup/1.ans",
        "",
        "benchbox/sql_compat/rules/clickhouse_rewrites.py",
        "benchbox/sql_compat/registry.py",
        "benchbox/sql_compat/actions.py",
        "benchbox/platforms/base/result_capture_helpers.py",
        "benchbox/platforms/base/adapter.py",
        "benchbox/platforms/duckdb.py",
        "benchbox/platforms/base/tuning_trust_helpers.py",
        "benchbox/core/tuning/coverage.py",
        "benchbox/core/tuning/modes.py",
        "benchbox/core/tuning/introspection.py.bak",
        "benchbox/core/tuning/metadata_helpers.py",
        "benchbox/platforms/duckdb_introspection_notes.md",
        "benchbox/platforms/clickhouse/adapter.py",
        "benchbox/core/results/exporter.py",
        "benchbox/core/results/anonymization.py.bak",
        "benchbox/core/results/anonymization_specs.yaml.example",
        "results-data/bundles/tpch/duckdb/sf1.json",
        "results-data/bundles/tpch/duckdb/sf1.plans.json",
        "results-data/corpus-inventory.json",
        "publicationx",
        "publicationx/notes.md",
        "scripts/publicationx/check.py",
        "benchbox/core/equivalencex",
        "benchbox/core/equivalencex/helpers.py",
        "benchbox/core/query_plans/parsersx/spark.py",
        "benchbox/core/expected_resultsx/loader.py",
    ],
)
def test_soundness_predicate_ignores_fast_default_paths(path: str) -> None:
    assert soundness.is_soundness_path(path) is False


def test_every_prefix_rule_ends_with_a_directory_separator() -> None:
    prefixes = [rule.pattern for rule in soundness.rules if rule.kind == "prefix"]
    assert prefixes
    assert all(prefix.endswith("/") for prefix in prefixes)


def test_soundness_files_glob_duality_stays_pinned() -> None:
    assert Rule("glob", OVERRIDE_FILES_GLOB) in soundness.rules
    assert soundness.is_soundness_path("results-data/bundles/tpch/duckdb/sf1.override.json") is True
    assert soundness.is_soundness_path("results-data/bundles/x.override.json.bak") is False


TUNING_TRUST_FILES = (
    "benchbox/platforms/base/tuning_trust.py",
    "benchbox/core/tuning/introspection.py",
    "benchbox/core/tuning/applied_ledger.py",
    "benchbox/core/tuning/capability_registry.py",
    "benchbox/core/tuning/metadata.py",
    "benchbox/core/tuning/reconciliation.py",
    "benchbox/platforms/clickhouse/introspection.py",
    "benchbox/platforms/dataframe/tuning_trust.py",
)
TUNING_TRUST_GLOB = "benchbox/platforms/*_introspection.py"


def test_tuning_trust_paths_are_pinned_in_manifest() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8").splitlines()
    for path in TUNING_TRUST_FILES:
        assert f"file\t{path}" in manifest
        assert soundness.is_soundness_path(path) is True
    assert f"glob\t{TUNING_TRUST_GLOB}" in manifest
    for module in ("benchbox/platforms/duckdb_introspection.py", "benchbox/platforms/snowflake_introspection.py"):
        assert fnmatch.fnmatchcase(module, TUNING_TRUST_GLOB) is True
        assert soundness.is_soundness_path(module) is True


def test_tuning_trust_glob_covers_nested_introspection_modules() -> None:
    nested = "benchbox/platforms/clickhouse/custom_introspection.py"
    assert fnmatch.fnmatchcase(nested, TUNING_TRUST_GLOB) is True
    assert soundness.is_soundness_path(nested) is True


def test_any_soundness_path_recognises_tuning_trust_paths() -> None:
    assert soundness.any_soundness_path(["docs/internal/nightly-t3.md", *TUNING_TRUST_FILES[:1]]) is True
    assert soundness.any_soundness_path(["benchbox/platforms/duckdb_introspection.py"]) is True
    assert soundness.any_soundness_path(["benchbox/platforms/base/adapter.py", "benchbox/platforms/duckdb.py"]) is False


SELECTION_LOGIC_FILES = (
    "scripts/coverage_test_selection.py",
    "scripts/coverage_map.ini",
    "_project/scripts/preflight_targets.py",
    "scripts/heavy_tier_needed.py",
    ".github/path-filters.yml",
    "scripts/path_filter_decision.py",
    "scripts/ci_units.py",
    ".github/ci-units.yml",
)


def test_selection_logic_paths_are_pinned_in_manifest() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8").splitlines()
    for path in SELECTION_LOGIC_FILES:
        assert f"file\t{path}" in manifest
        assert soundness.is_soundness_path(path) is True


TRANSLATION_FILES = (
    "benchbox/utils/dialect_utils.py",
    "scripts/sqlglot_runtime_baseline.json",
    "scripts/sync_sqlglot_runtime.py",
    "scripts/check_sqlglot_repro_retirement.py",
    "Makefile",
    "scripts/sqlglot_fork_diff.py",
    "tests/unit/scripts/test_sql_execution_gate.py",
    "tests/unit/scripts/test_sync_sqlglot_runtime.py",
    "tests/unit/scripts/test_check_sqlglot_repro_retirement.py",
    "tests/unit/scripts/test_sqlglot_fork_diff.py",
    "tests/unit/utils/test_dialect_utils.py",
    "tests/unit/utils/test_apply_rewrite.py",
    "tests/unit/utils/test_apply_rewrite_engines.py",
    "tests/unit/utils/test_translate_ddl.py",
)
TRANSLATION_PREFIXES = (
    "benchbox/_vendor/",
    "make/",
    "scripts/sql_execution_gate/",
    "tests/unit/sql_compat/",
)
TRANSLATION_DATA_FILES = (
    "scripts/sql_execution_gate/expected_differences.json",
    "tests/unit/sql_compat/golden_corpus/recorded/area_ddl.json",
    "tests/unit/sql_compat/golden_corpus/frozen/area_ddl.json",
    "tests/unit/sql_compat/test_fork_golden_corpus.py",
    "tests/unit/sql_compat/test_wrapper_matches_fork.py",
    "tests/unit/sql_compat/test_vendored_sqlglot_bypass.py",
    "tests/unit/sql_compat/test_translate_sql_delegation.py",
    "tests/unit/sql_compat/test_datafusion_fork_rewrites_reference.py",
    "benchbox/_vendor/sqlglot.json",
)


def test_translation_paths_are_pinned_in_manifest() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8").splitlines()
    for path in TRANSLATION_FILES:
        assert f"file\t{path}" in manifest
        assert soundness.is_soundness_path(path) is True
    for prefix in TRANSLATION_PREFIXES:
        assert f"prefix\t{prefix}" in manifest
    for path in TRANSLATION_DATA_FILES:
        assert soundness.is_soundness_path(path) is True


def test_translation_prefixes_cover_every_file_beneath_them() -> None:
    covered = [
        "benchbox/_vendor/sqlglot.json",
        "benchbox/_vendor/sqlglot-runtime.whl.zip",
        "make/site-inputs.mk",
        "scripts/sql_execution_gate/gate.py",
        "scripts/sql_execution_gate/nested/expected_differences.json",
        "tests/unit/sql_compat/golden_corpus/area_ddl.py",
        "tests/unit/sql_compat/golden_corpus/recorded/area_ddl.json",
        "tests/unit/sql_compat/golden_corpus/frozen/area_ddl.json",
    ]
    for path in covered:
        assert soundness.is_soundness_path(path) is True, path


@pytest.mark.parametrize(
    "path",
    [
        "benchbox/_vendorx/sqlglot.json",
        "benchbox/utils/dialect_utils.py.bak",
        "benchbox/utils/dialect_helpers.py",
        "scripts/sqlglot_runtime_baseline.json.bak",
        "scripts/sql_execution_gatex/gate.py",
        "scripts/sync_sqlglot_runtime.py.bak",
        "tests/unit/sql_compatx/golden_corpus/area_ddl.py",
        "tests/unit/sql_compat.py",
    ],
)
def test_translation_paths_do_not_overmatch_neighbours(path: str) -> None:
    assert soundness.is_soundness_path(path) is False


def test_importing_the_matcher_reads_no_files() -> None:
    source = Path(soundness_paths.__file__).read_text(encoding="utf-8")
    assert "read_text" not in source.split("def load_rules", 1)[0]
    assert "BenchBox" not in source


def test_parse_rules_reports_malformed_manifests() -> None:
    with pytest.raises(ValueError, match="m:1: expected kind<TAB>pattern"):
        parse_rules("file no-tab", "m")
    with pytest.raises(ValueError, match="m:1: unknown rule kind 'bogus'"):
        parse_rules("bogus\tx", "m")
    with pytest.raises(ValueError, match="prefix must end with '/'"):
        parse_rules("prefix\tdocs", "m")
    with pytest.raises(ValueError, match="manifest is empty"):
        parse_rules("# only a comment\n\n", "m")
    with pytest.raises(Exception):
        parse_rules("regex\t(", "m")


def test_parse_rules_skips_comments_and_blank_lines() -> None:
    rules = parse_rules("# c\n\nfile\ta.py\nprefix\tdocs/\n", "m")
    assert rules == (Rule("file", "a.py"), Rule("prefix", "docs/"))


def test_matcher_has_no_default_rules_and_union_requires_any_manifest() -> None:
    base = Matcher(parse_rules("file\tbase.py\n"))
    head = Matcher(parse_rules("file\thead.py\n"))
    assert soundness_paths.union_matches("base.py", [base, head]) is True
    assert soundness_paths.union_matches("head.py", [base, head]) is True
    assert soundness_paths.union_matches("other.py", [base, head]) is False
    assert soundness_paths.union_matches("base.py", []) is False


def test_matcher_normalizes_backslashes_and_whitespace() -> None:
    matcher = Matcher(parse_rules("prefix\tdocs/\n"))
    assert matcher.is_soundness_path(" docs\\a.md \n") is True
    assert matcher.is_soundness_path("   ") is False


def test_load_rules_reads_a_custom_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "paths.txt"
    manifest.write_text("file\tonly.py\n", encoding="utf-8")
    matcher = Matcher(load_rules(manifest))
    assert matcher.is_soundness_path("only.py") is True
    assert matcher.is_soundness_path("benchbox/core/validation/data.py") is False


def run_cli(*args: str, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "theseus_reviewer.soundness_paths", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_requires_a_manifest() -> None:
    result = run_cli("--stdin", stdin="a.py\n")
    assert result.returncode != 0
    assert "--manifest is required" in result.stderr


def test_cli_reports_a_missing_manifest(tmp_path: Path) -> None:
    result = run_cli("--manifest", str(tmp_path / "absent.txt"), "--stdin", stdin="a.py\n")
    assert result.returncode != 0
    assert "soundness_paths:" in result.stderr


def test_cli_unions_repeated_manifests(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    head = tmp_path / "head.txt"
    base.write_text("file\tbase.py\n", encoding="utf-8")
    head.write_text("file\thead.py\n", encoding="utf-8")
    manifests = ["--manifest", str(base), "--manifest", str(head)]
    assert run_cli(*manifests, "--stdin", stdin="docs/x.md\nhead.py\n").stdout.strip() == "true"
    assert run_cli(*manifests, "--stdin", stdin="base.py\n").stdout.strip() == "true"
    assert run_cli(*manifests, "--stdin", stdin="docs/x.md\n").stdout.strip() == "false"
    only_base = run_cli("--manifest", str(base), "--stdin", "--format", "github-output", stdin="head.py\n")
    assert only_base.stdout.strip() == "soundness_path=false"


def test_cli_matches_against_the_alpha_manifest() -> None:
    manifest = str(MANIFEST)
    assert run_cli("--manifest", manifest, "--stdin", "--format", "shell", stdin="scripts/pr_arm.py\n").stdout == (
        "SOUNDNESS_PATH=true\n"
    )
    assert run_cli("--manifest", manifest, "--stdin", stdin="docs/site-deploy.md\n").stdout.strip() == "false"


def test_cli_reads_paths_from_a_file(tmp_path: Path) -> None:
    paths = tmp_path / "paths.txt"
    paths.write_text("benchbox/core/validation/data.py\n", encoding="utf-8")
    result = run_cli("--manifest", str(MANIFEST), "--paths-file", str(paths), stdin="")
    assert result.stdout.strip() == "true"


def test_alpha_policy_names_the_alpha_manifest() -> None:
    assert ALPHA_POLICY.parent.parent == CONSUMER_ALPHA
    assert (CONSUMER_ALPHA / ".github" / "soundness-paths.txt") == MANIFEST


def test_theseus_matcher_check_runs_the_same_predicate() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "matcher", "check", "--manifest", str(MANIFEST), "--stdin"],
        input="scripts/pr_arm.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "true"
