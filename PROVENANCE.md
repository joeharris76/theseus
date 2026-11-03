# Provenance

Theseus is extracted from BenchBox. This file records the exact source baseline, the path map, the retained notices and the normalized seed hash so the extraction can be reviewed independently.

## Baseline

- Source repository: BenchBox (`BenchBox-dev/BenchBox`)
- Baseline commit: `95c423c35efd3fb4962145bdc628006d5aaaad26` (`origin/develop` after PR #3929)
- Decision records: `_project/decisions/theseus-oracle-extraction-2026-11-02.md` and its inventory, in the baseline commit.
- Seed commit: the first commit on `feat/initial-extraction` after `main`. It holds every mapped file byte-identical to the baseline blob at its mapped path, plus `tools/seed_map.json`, `tools/seed_hash.py` and this file. Later commits adapt the code; `git diff <seed commit> HEAD -- src` is the reviewable adaptation delta.

## Notices

- `LICENSE` is the BenchBox MIT license, copied unchanged (`Copyright (c) 2026 Joe Harris / BenchBox Project`). The seeded files carry no per-file copyright or third-party license headers (checked with a case-insensitive search for `copyright` and `licen[sc]e` over `src/`).
- Runtime dependencies are declared in `pyproject.toml` and locked in `uv.lock`; their licenses stay with their distributions.
- OMP is not bundled or redistributed. It is installed from consumer-pinned, hash-verified sources (`install-omp`).

## Normalized seed source hash

Definition: sha256 over the sorted lines `<theseus path>\t<sha256 of file bytes>\n` for every map entry of kind `source`. Paths are normalized to the Theseus layout; bytes are not altered.

```
source  cdbde7d3d47f753c10f75694faee552085fb5b012d44b9714ea0099b5cf805d3
tests+fixtures  02e42048c4fa54149bccb4109c3643830307e3f384f3f4afc04af97b970f6436
```

Reproduce from BenchBox, and check the seed commit of this repository against it:

```
python3 tools/seed_hash.py --benchbox-repo <BenchBox checkout>
python3 tools/seed_hash.py --git-rev <seed commit>
python3 tools/seed_hash.py --benchbox-repo <BenchBox checkout> --kind test --kind fixture
python3 tools/seed_hash.py --git-rev <seed commit> --kind test --kind fixture
```

All four must agree pairwise with the values above. `--verify HEX` fails on mismatch. The machine-readable map is `tools/seed_map.json`.

## Scope

Moved: the reviewer engine and gate (`_project/scripts/oracle_reviewers/`, `oracle_gate.py`, `oracle_review_check.py`), the soundness/inert path matchers, templates, and the matching unit and integration tests and captured fixtures.

Not moved (BenchBox-owned consumer code): policy and manifests, read-index scanning, PR lifecycle, trigger wrappers, `oracle_gateway_host.sh`, `soundness_merge_digest.py`, `soundness_drain_report.py`, and `test_oracle_local_review.py`. The consumer policy and manifest files under `tests/fixtures/consumer/` are BenchBox's baseline copies, kept as test fixtures only; the package ships none of them.

## Path map

| BenchBox path | Theseus path | Kind |
|---|---|---|
| `_project/scripts/oracle_reviewers/__init__.py` | `src/theseus_reviewer/__init__.py` | source |
| `_project/scripts/oracle_reviewers/absence.py` | `src/theseus_reviewer/absence.py` | source |
| `_project/scripts/oracle_reviewers/adjudication.py` | `src/theseus_reviewer/adjudication.py` | source |
| `_project/scripts/oracle_reviewers/attempts.py` | `src/theseus_reviewer/attempts.py` | source |
| `_project/scripts/oracle_reviewers/base_change.py` | `src/theseus_reviewer/base_change.py` | source |
| `_project/scripts/oracle_reviewers/brief.py` | `src/theseus_reviewer/brief.py` | source |
| `_project/scripts/oracle_reviewers/candidate_canaries.py` | `src/theseus_reviewer/candidate_canaries.py` | source |
| `_project/scripts/oracle_reviewers/candidate_chain.py` | `src/theseus_reviewer/candidate_chain.py` | source |
| `_project/scripts/oracle_reviewers/candidate_checks.py` | `src/theseus_reviewer/candidate_checks.py` | source |
| `_project/scripts/oracle_reviewers/candidate_env.py` | `src/theseus_reviewer/candidate_env.py` | source |
| `_project/scripts/oracle_reviewers/candidate_stub.py` | `src/theseus_reviewer/candidate_stub.py` | source |
| `_project/scripts/oracle_reviewers/classifier.py` | `src/theseus_reviewer/classifier.py` | source |
| `_project/scripts/oracle_reviewers/classify.py` | `src/theseus_reviewer/classify.py` | source |
| `_project/scripts/oracle_reviewers/cli.py` | `src/theseus_reviewer/cli.py` | source |
| `_project/scripts/oracle_reviewers/commands.py` | `src/theseus_reviewer/commands.py` | source |
| `_project/scripts/oracle_reviewers/credentials.py` | `src/theseus_reviewer/credentials.py` | source |
| `_project/scripts/oracle_reviewers/dedup.py` | `src/theseus_reviewer/dedup.py` | source |
| `_project/scripts/oracle_reviewers/diff.py` | `src/theseus_reviewer/diff.py` | source |
| `_project/scripts/oracle_reviewers/disk_scan.py` | `src/theseus_reviewer/disk_scan.py` | source |
| `_project/scripts/oracle_reviewers/driver.py` | `src/theseus_reviewer/driver.py` | source |
| `_project/scripts/oracle_reviewers/edits.py` | `src/theseus_reviewer/edits.py` | source |
| `_project/scripts/oracle_reviewers/evidence.py` | `src/theseus_reviewer/evidence.py` | source |
| `_project/scripts/oracle_reviewers/findings.py` | `src/theseus_reviewer/findings.py` | source |
| `_project/scripts/oracle_gate.py` | `src/theseus_reviewer/gate.py` | source |
| `_project/scripts/oracle_reviewers/github.py` | `src/theseus_reviewer/github.py` | source |
| `_project/scripts/inert_paths.py` | `src/theseus_reviewer/inert_paths.py` | source |
| `_project/scripts/oracle_reviewers/isolation.py` | `src/theseus_reviewer/isolation.py` | source |
| `_project/scripts/oracle_reviewers/jev.py` | `src/theseus_reviewer/jev.py` | source |
| `_project/scripts/oracle_reviewers/local.py` | `src/theseus_reviewer/local.py` | source |
| `_project/scripts/oracle_reviewers/omp_bump.py` | `src/theseus_reviewer/omp_bump.py` | source |
| `_project/scripts/oracle_reviewers/omp_bump_gate.py` | `src/theseus_reviewer/omp_bump_gate.py` | source |
| `_project/scripts/oracle_reviewers/omp_candidate.py` | `src/theseus_reviewer/omp_candidate.py` | source |
| `_project/scripts/oracle_reviewers/omp_pin.py` | `src/theseus_reviewer/omp_pin.py` | source |
| `_project/scripts/oracle_reviewers/omp_watch.py` | `src/theseus_reviewer/omp_watch.py` | source |
| `_project/scripts/oracle_reviewers/policy.py` | `src/theseus_reviewer/policy.py` | source |
| `_project/scripts/oracle_reviewers/processes.py` | `src/theseus_reviewer/processes.py` | source |
| `_project/scripts/oracle_reviewers/promotion.py` | `src/theseus_reviewer/promotion.py` | source |
| `_project/scripts/oracle_reviewers/protocol.py` | `src/theseus_reviewer/protocol.py` | source |
| `_project/scripts/oracle_reviewers/recovery.py` | `src/theseus_reviewer/recovery.py` | source |
| `_project/scripts/oracle_reviewers/redaction_check.py` | `src/theseus_reviewer/redaction_check.py` | source |
| `_project/scripts/oracle_reviewers/render.py` | `src/theseus_reviewer/render.py` | source |
| `_project/scripts/oracle_reviewers/report.py` | `src/theseus_reviewer/report.py` | source |
| `_project/scripts/oracle_reviewers/reset.py` | `src/theseus_reviewer/reset.py` | source |
| `_project/scripts/oracle_reviewers/retry.py` | `src/theseus_reviewer/retry.py` | source |
| `_project/scripts/oracle_review_check.py` | `src/theseus_reviewer/review_check.py` | source |
| `_project/scripts/oracle_reviewers/runner.py` | `src/theseus_reviewer/runner.py` | source |
| `_project/scripts/oracle_reviewers/selection.py` | `src/theseus_reviewer/selection.py` | source |
| `_project/scripts/soundness_paths.py` | `src/theseus_reviewer/soundness_paths.py` | source |
| `_project/scripts/oracle_reviewers/sweep.py` | `src/theseus_reviewer/sweep.py` | source |
| `_project/scripts/oracle_reviewers/templates/README.md` | `src/theseus_reviewer/templates/README.md` | source |
| `_project/scripts/oracle_reviewers/templates/deep.md` | `src/theseus_reviewer/templates/deep.md` | source |
| `_project/scripts/oracle_reviewers/templates/gate.md` | `src/theseus_reviewer/templates/gate.md` | source |
| `_project/scripts/oracle_reviewers/templates/standard.md` | `src/theseus_reviewer/templates/standard.md` | source |
| `_project/scripts/oracle_reviewers/verdict.py` | `src/theseus_reviewer/verdict.py` | source |
| `.github/inert-paths.txt` | `tests/fixtures/consumer/inert-paths.txt` | fixture |
| `.github/oracle-omp-config.yml` | `tests/fixtures/consumer/oracle-omp-config.yml` | fixture |
| `.github/oracle-reviewers.yml` | `tests/fixtures/consumer/oracle-reviewers.yml` | fixture |
| `.github/soundness-paths.txt` | `tests/fixtures/consumer/soundness-paths.txt` | fixture |
| `tests/fixtures/oracle_golden/app_missing.json` | `tests/fixtures/oracle_golden/app_missing.json` | fixture |
| `tests/fixtures/oracle_golden/carry.json` | `tests/fixtures/oracle_golden/carry.json` | fixture |
| `tests/fixtures/oracle_golden/carry_disagrees_with_status_holds.json` | `tests/fixtures/oracle_golden/carry_disagrees_with_status_holds.json` | fixture |
| `tests/fixtures/oracle_golden/dispute_round.json` | `tests/fixtures/oracle_golden/dispute_round.json` | fixture |
| `tests/fixtures/oracle_golden/draft.json` | `tests/fixtures/oracle_golden/draft.json` | fixture |
| `tests/fixtures/oracle_golden/edited_prior_review_holds.json` | `tests/fixtures/oracle_golden/edited_prior_review_holds.json` | fixture |
| `tests/fixtures/oracle_golden/every_attempt_absent.json` | `tests/fixtures/oracle_golden/every_attempt_absent.json` | fixture |
| `tests/fixtures/oracle_golden/first_review_ships.json` | `tests/fixtures/oracle_golden/first_review_ships.json` | fixture |
| `tests/fixtures/oracle_golden/first_review_with_a_defect.json` | `tests/fixtures/oracle_golden/first_review_with_a_defect.json` | fixture |
| `tests/fixtures/oracle_golden/follow_up_after_a_fix.json` | `tests/fixtures/oracle_golden/follow_up_after_a_fix.json` | fixture |
| `tests/fixtures/oracle_golden/fork.json` | `tests/fixtures/oracle_golden/fork.json` | fixture |
| `tests/fixtures/oracle_golden/guard_gives_up.json` | `tests/fixtures/oracle_golden/guard_gives_up.json` | fixture |
| `tests/fixtures/oracle_golden/integrity_error.json` | `tests/fixtures/oracle_golden/integrity_error.json` | fixture |
| `tests/fixtures/oracle_golden/omp_install_failure.json` | `tests/fixtures/oracle_golden/omp_install_failure.json` | fixture |
| `tests/fixtures/oracle_golden/oversize_file_list.json` | `tests/fixtures/oracle_golden/oversize_file_list.json` | fixture |
| `tests/fixtures/oracle_golden/recovery_escalate.json` | `tests/fixtures/oracle_golden/recovery_escalate.json` | fixture |
| `tests/fixtures/oracle_golden/recovery_fallback.json` | `tests/fixtures/oracle_golden/recovery_fallback.json` | fixture |
| `tests/fixtures/oracle_golden/recovery_retry.json` | `tests/fixtures/oracle_golden/recovery_retry.json` | fixture |
| `tests/fixtures/oracle_golden/recovery_stop.json` | `tests/fixtures/oracle_golden/recovery_stop.json` | fixture |
| `tests/fixtures/oracle_golden/stale_same_head_state.json` | `tests/fixtures/oracle_golden/stale_same_head_state.json` | fixture |
| `tests/fixtures/oracle_golden/unreadable_edit_state_posts_nothing.json` | `tests/fixtures/oracle_golden/unreadable_edit_state_posts_nothing.json` | fixture |
| `tests/fixtures/oracle_reviewers/captures/ci_attempt.json` | `tests/fixtures/oracle_reviewers/captures/ci_attempt.json` | fixture |
| `tests/fixtures/oracle_reviewers/captures/local_review.json` | `tests/fixtures/oracle_reviewers/captures/local_review.json` | fixture |
| `tests/fixtures/oracle_reviewers/check_runs_full_ci_in_progress.json` | `tests/fixtures/oracle_reviewers/check_runs_full_ci_in_progress.json` | fixture |
| `tests/fixtures/oracle_reviewers/dispute_3407.json` | `tests/fixtures/oracle_reviewers/dispute_3407.json` | fixture |
| `tests/fixtures/oracle_reviewers/dispute_3465.json` | `tests/fixtures/oracle_reviewers/dispute_3465.json` | fixture |
| `tests/fixtures/oracle_reviewers/jev_recovery_answers.json` | `tests/fixtures/oracle_reviewers/jev_recovery_answers.json` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/build_api_latest.json` | `tests/fixtures/oracle_reviewers/omp_candidate/build_api_latest.json` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/chain_review.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/chain_review.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/elided_read.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/elided_read.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/gateway_rejection.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/gateway_rejection.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/manifest.json` | `tests/fixtures/oracle_reviewers/omp_candidate/manifest.json` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/quota_terminal.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/quota_terminal.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/quota_walk.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/quota_walk.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/roster_breach.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/roster_breach.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/static_outputs.json` | `tests/fixtures/oracle_reviewers/omp_candidate/static_outputs.json` | fixture |
| `tests/fixtures/oracle_reviewers/omp_candidate/upstream_auth.jsonl` | `tests/fixtures/oracle_reviewers/omp_candidate/upstream_auth.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_multi_turn_review.jsonl` | `tests/fixtures/oracle_reviewers/omp_multi_turn_review.jsonl` | fixture |
| `tests/fixtures/oracle_reviewers/omp_watch/attempts.json` | `tests/fixtures/oracle_reviewers/omp_watch/attempts.json` | fixture |
| `tests/fixtures/oracle_reviewers/pr2769_history.json` | `tests/fixtures/oracle_reviewers/pr2769_history.json` | fixture |
| `tests/fixtures/oracle_reviewers/pr3222_replay.json` | `tests/fixtures/oracle_reviewers/pr3222_replay.json` | fixture |
| `tests/fixtures/oracle_reviewers/pr3237_missing_text_fields.json` | `tests/fixtures/oracle_reviewers/pr3237_missing_text_fields.json` | fixture |
| `tests/fixtures/oracle_reviewers/pr3585_attempt-1.json` | `tests/fixtures/oracle_reviewers/pr3585_attempt-1.json` | fixture |
| `tests/fixtures/oracle_reviewers/pr3585_attempt-2.json` | `tests/fixtures/oracle_reviewers/pr3585_attempt-2.json` | fixture |
| `tests/fixtures/oracle_reviewers/provenance.json` | `tests/fixtures/oracle_reviewers/provenance.json` | fixture |
| `tests/integration/oracle/__init__.py` | `tests/integration/__init__.py` | test |
| `tests/integration/oracle/conftest.py` | `tests/integration/conftest.py` | test |
| `tests/integration/oracle/fake_github.py` | `tests/integration/fake_github.py` | test |
| `tests/integration/oracle/fake_jev.py` | `tests/integration/fake_jev.py` | test |
| `tests/integration/oracle/fake_omp.py` | `tests/integration/fake_omp.py` | test |
| `tests/integration/oracle/golden.py` | `tests/integration/golden.py` | test |
| `tests/integration/oracle/harness.py` | `tests/integration/harness.py` | test |
| `tests/integration/oracle/runs.py` | `tests/integration/runs.py` | test |
| `tests/integration/oracle/test_ancestor_environment_leak.py` | `tests/integration/test_ancestor_environment_leak.py` | test |
| `tests/integration/oracle/test_carry_scenarios.py` | `tests/integration/test_carry_scenarios.py` | test |
| `tests/integration/oracle/test_cloud_review_reachability_scenario.py` | `tests/integration/test_cloud_review_reachability_scenario.py` | test |
| `tests/integration/oracle/test_curated_capture_fixtures.py` | `tests/integration/test_curated_capture_fixtures.py` | test |
| `tests/integration/oracle/test_evidence_scenarios.py` | `tests/integration/test_evidence_scenarios.py` | test |
| `tests/integration/oracle/test_followup_scenarios.py` | `tests/integration/test_followup_scenarios.py` | test |
| `tests/integration/oracle/test_golden_outputs.py` | `tests/integration/test_golden_outputs.py` | test |
| `tests/integration/oracle/test_invalid_recovery_scenarios.py` | `tests/integration/test_invalid_recovery_scenarios.py` | test |
| `tests/integration/oracle/test_local_concurrency_scenarios.py` | `tests/integration/test_local_concurrency_scenarios.py` | test |
| `tests/integration/oracle/test_local_driver_scenarios.py` | `tests/integration/test_local_driver_scenarios.py` | test |
| `tests/integration/oracle/test_local_findings_scenarios.py` | `tests/integration/test_local_findings_scenarios.py` | test |
| `tests/integration/oracle/test_local_review_scenario.py` | `tests/integration/test_local_review_scenario.py` | test |
| `tests/integration/oracle/test_omp_pin_scenarios.py` | `tests/integration/test_omp_pin_scenarios.py` | test |
| `tests/integration/oracle/test_pr_arm_scenario.py` | `tests/integration/test_pr_arm_scenario.py` | test |
| `tests/integration/oracle/test_pr_landing_soundness_scenario.py` | `tests/integration/test_pr_landing_soundness_scenario.py` | test |
| `tests/integration/oracle/test_quota_scenarios.py` | `tests/integration/test_quota_scenarios.py` | test |
| `tests/integration/oracle/test_redaction_scenarios.py` | `tests/integration/test_redaction_scenarios.py` | test |
| `tests/integration/oracle/test_review_scenarios.py` | `tests/integration/test_review_scenarios.py` | test |
| `tests/integration/oracle/test_runner_isolation_scenarios.py` | `tests/integration/test_runner_isolation_scenarios.py` | test |
| `tests/integration/oracle/test_sweep_listing_scenarios.py` | `tests/integration/test_sweep_listing_scenarios.py` | test |
| `tests/integration/oracle/test_sweep_liveness_scenarios.py` | `tests/integration/test_sweep_liveness_scenarios.py` | test |
| `tests/integration/oracle/test_sweep_scenarios.py` | `tests/integration/test_sweep_scenarios.py` | test |
| `tests/integration/oracle/test_theseus_promotion_scenario.py` | `tests/integration/test_theseus_promotion_scenario.py` | test |
| `tests/integration/oracle/test_trust_scenarios.py` | `tests/integration/test_trust_scenarios.py` | test |
| `tests/unit/scripts/oracle_reviewers/__init__.py` | `tests/unit/reviewer/__init__.py` | test |
| `tests/unit/scripts/oracle_reviewers/conftest.py` | `tests/unit/reviewer/conftest.py` | test |
| `tests/unit/scripts/oracle_reviewers/loop_process.py` | `tests/unit/reviewer/loop_process.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_absence.py` | `tests/unit/reviewer/test_absence.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_absence_reset_redaction.py` | `tests/unit/reviewer/test_absence_reset_redaction.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_absence_retry.py` | `tests/unit/reviewer/test_absence_retry.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_attempt_loop.py` | `tests/unit/reviewer/test_attempt_loop.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_attempt_tempfiles.py` | `tests/unit/reviewer/test_attempt_tempfiles.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_attempts.py` | `tests/unit/reviewer/test_attempts.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_brief.py` | `tests/unit/reviewer/test_brief.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_carry_identity.py` | `tests/unit/reviewer/test_carry_identity.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_citation_paths.py` | `tests/unit/reviewer/test_citation_paths.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_classifier.py` | `tests/unit/reviewer/test_classifier.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_classify.py` | `tests/unit/reviewer/test_classify.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_cli_plan.py` | `tests/unit/reviewer/test_cli_plan.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_commands.py` | `tests/unit/reviewer/test_commands.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_credentials.py` | `tests/unit/reviewer/test_credentials.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_dedup.py` | `tests/unit/reviewer/test_dedup.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_dispute_escalation.py` | `tests/unit/reviewer/test_dispute_escalation.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_dispute_keyed_evidence.py` | `tests/unit/reviewer/test_dispute_keyed_evidence.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_driver.py` | `tests/unit/reviewer/test_driver.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_edited_reviews.py` | `tests/unit/reviewer/test_edited_reviews.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_evidence.py` | `tests/unit/reviewer/test_evidence.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_evidence_retry.py` | `tests/unit/reviewer/test_evidence_retry.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_findings_record.py` | `tests/unit/reviewer/test_findings_record.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_gateway_host.py` | `tests/unit/reviewer/test_gateway_host.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_github.py` | `tests/unit/reviewer/test_github.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_invalid_recovery.py` | `tests/unit/reviewer/test_invalid_recovery.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_isolation.py` | `tests/unit/reviewer/test_isolation.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_jev.py` | `tests/unit/reviewer/test_jev.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_local_review.py` | `tests/unit/reviewer/test_local_review.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_local_windows_support.py` | `tests/unit/reviewer/test_local_windows_support.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_omp_bump.py` | `tests/unit/reviewer/test_omp_bump.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_omp_candidate.py` | `tests/unit/reviewer/test_omp_candidate.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_omp_harness.py` | `tests/unit/reviewer/test_omp_harness.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_omp_pin.py` | `tests/unit/reviewer/test_omp_pin.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_omp_watch.py` | `tests/unit/reviewer/test_omp_watch.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_policy.py` | `tests/unit/reviewer/test_policy.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_promotion.py` | `tests/unit/reviewer/test_promotion.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_protocol.py` | `tests/unit/reviewer/test_protocol.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_redaction_check.py` | `tests/unit/reviewer/test_redaction_check.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_render.py` | `tests/unit/reviewer/test_render.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_report_body_cap.py` | `tests/unit/reviewer/test_report_body_cap.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_report_outcomes.py` | `tests/unit/reviewer/test_report_outcomes.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_reset.py` | `tests/unit/reviewer/test_reset.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_response_ingress.py` | `tests/unit/reviewer/test_response_ingress.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_retry.py` | `tests/unit/reviewer/test_retry.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_reviewer_home.py` | `tests/unit/reviewer/test_reviewer_home.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_rounds.py` | `tests/unit/reviewer/test_rounds.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_runner.py` | `tests/unit/reviewer/test_runner.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_same_head_rerun.py` | `tests/unit/reviewer/test_same_head_rerun.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_scan_scratch.py` | `tests/unit/reviewer/test_scan_scratch.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_selection.py` | `tests/unit/reviewer/test_selection.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_sweep.py` | `tests/unit/reviewer/test_sweep.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_sweep_liveness.py` | `tests/unit/reviewer/test_sweep_liveness.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_template_test_quality_rule.py` | `tests/unit/reviewer/test_template_test_quality_rule.py` | test |
| `tests/unit/scripts/oracle_reviewers/test_verdict.py` | `tests/unit/reviewer/test_verdict.py` | test |
| `tests/unit/scripts/test_inert_paths.py` | `tests/unit/test_inert_paths.py` | test |
| `tests/unit/scripts/test_oracle_gate.py` | `tests/unit/test_oracle_gate.py` | test |
| `tests/unit/scripts/test_oracle_review_check.py` | `tests/unit/test_oracle_review_check.py` | test |
| `tests/unit/scripts/test_oracle_review_check_decisions.py` | `tests/unit/test_oracle_review_check_decisions.py` | test |
| `tests/unit/scripts/test_oracle_review_check_omp_bump.py` | `tests/unit/test_oracle_review_check_omp_bump.py` | test |
| `tests/unit/scripts/test_oracle_review_check_promotion.py` | `tests/unit/test_oracle_review_check_promotion.py` | test |
| `tests/unit/scripts/test_soundness_paths.py` | `tests/unit/test_soundness_paths.py` | test |
