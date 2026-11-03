# Provenance

Theseus is extracted from BenchBox. This file records the exact source baseline, the path map, the retained notices and the normalized seed hash so the extraction can be reviewed independently.

## Baseline

- Source repository: BenchBox (`BenchBox-dev/BenchBox`)
- Baseline commit: `95c423c35efd3fb4962145bdc628006d5aaaad26` (`origin/develop` after PR #3929)
- Decision records: `_project/decisions/theseus-oracle-extraction-2026-11-02.md` and its inventory, in the baseline commit.
- Seed commit: `4b76a4a3ddf11b531a637f10a2a58cff4fee563f` (`chore: seed reviewer, gate and matcher source from BenchBox 95c423c`), the first commit after the repository governance commit. It holds every mapped file byte-identical to the baseline blob at its mapped path, plus `tools/seed_map.json`, `tools/seed_hash.py` and this file. Later commits adapt the code; `git diff <seed commit> HEAD -- src` is the reviewable adaptation delta. The extraction pull request is merged with a merge commit so these commit identities stay on `main`.

## Notices

- `LICENSE` is MIT. It carries the repository copyright and the BenchBox copyright notice of the extracted code (`Copyright (c) 2026 Joe Harris / BenchBox Project`), which the MIT license requires to be retained. The seeded files carry no per-file copyright or third-party license headers (checked with a case-insensitive search for `copyright` and `licen[sc]e` over `src/`).
- The wheel has no runtime dependencies. Its YAML parser is vendored pure Python: PyYAML 6.0.3, `lib/yaml/*.py` from the upstream sdist `https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz` (sha256 `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`, matching PyPI's published digest). The files sit byte-identical under `src/theseus_reviewer/_vendor/yaml/` except that `cyaml.py` (the libyaml binding) is omitted, so no C extension or ambient `yaml` can be loaded. Its MIT license (`Copyright (c) 2017-2021 Ingy döt Net`, `Copyright (c) 2006-2016 Kirill Simonov`) is retained as `src/theseus_reviewer/_vendor/yaml/LICENSE`. `tests/unit/test_vendored_yaml.py` pins the vendored file digest, and ruff excludes `_vendor`.
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

All four must agree pairwise with the values above. `--verify HEX` fails on mismatch. The machine-readable map is `tools/seed_map.json`. `--tree` and the `--kind test/fixture` hashes describe the seed commit only: later commits adapt, move and delete files, so verify later heads with `--git-rev <seed commit>`.

## Scope

Moved: the reviewer engine and gate (`_project/scripts/oracle_reviewers/`, `oracle_gate.py`, `oracle_review_check.py`), the soundness/inert path matchers, templates, and the matching unit and integration tests and captured fixtures.

Not moved (BenchBox-owned consumer code): policy and manifests, read-index scanning, PR lifecycle, trigger wrappers, `oracle_gateway_host.sh`, `soundness_merge_digest.py`, `soundness_drain_report.py`, and `test_oracle_local_review.py`. The consumer policy and manifest files under `tests/fixtures/consumer/` are BenchBox's baseline copies, kept as test fixtures only; the package ships none of them.

## Adaptation after the seed

Reviewable as `git diff 4b76a4a HEAD -- src` (source) and by commit:

1. Imports became relative package imports (`oracle_reviewers.*`, `oracle_gate`, `oracle_review_check`, `_project.scripts.*` to `theseus_reviewer.*`); no behavior change.
2. Consumer identity and paths moved out of the code: the policy now requires a `consumer` section (gated base, attesters, soundness, inert and OMP config manifests) beside `bot_login`; `PromotionRules` lost its BenchBox repository and reviewer defaults; every removed constant (`ORACLE_LOGIN`, `STANDIN_ATTESTERS`, `ATTESTERS`, `GATED_BASE`, `DEVELOP`, `TRUSTED_BRANCH`, `POLICY_PATH`, `REPO_ROOT`, default cache, default repository, default policy) became a parameter; the gate evaluates with the configured `bot_login`. See [docs/consumer-policy.md](docs/consumer-policy.md).
3. `soundness_paths.py` became the standalone, import-pure matcher module with no default manifest and union semantics; `inert_paths.py` kept only matching; `matchers.py`, `matcher_cli.py` and `theseus matcher verify` add loading and vendored-drift detection. See [docs/matcher.md](docs/matcher.md).
4. The `theseus` CLI (`main.py`, `__main__.py`) wraps the seeded argparse dispatcher and adds `gate`, `promotion-check`, `bootstrap`, `matcher` and the OMP maintenance commands. The in-process gate import became a `theseus gate` subprocess.
5. `bootstrap.py` is the consumer installer. See [docs/bootstrap.md](docs/bootstrap.md).
6. YAML is vendored (`_vendor/yaml`, see Notices) and every `import yaml` became `from ._vendor import yaml`; `PyYAML` left the dependencies, so the lock's dependency closure is normally empty.
7. Local review phases no longer run `python -P -m theseus_reviewer` (which cannot import the package under the bootstrap launcher's `-IBS` and would import an ambient copy under `PYTHONPATH`): `selfexec.package_command()` re-executes the running interpreter with `-I -B -S` and only the verified package's parent directory on the import path, and every `PYTHON*` variable is removed from the phase environment. `github.gate_command()` has no fallback: `THESEUS_GATE_COMMAND` must name the separately verified gate. The default local base branch is read from the policy at `--trusted-ref`, never from the working tree. `promotion-check` also verifies `source_sha256`.
8. Seeded tests were ported; BenchBox-owned tests were removed (see below) and new tests cover identity isolation, the installer refusal matrix and the isolated matcher.

## Retained identities and wording

These stay because they are protocol, persisted-format or service contracts, not consumer bindings: the `oracle-verdict` and `oracle-gate` contexts, marker grammars, `oracle-verdict-*` artifact prefixes, the persisted member-review schema string `benchbox_member_review_v1` (preserves existing local findings), the three prompt templates (their domain wording is the reviewed protocol text), the gateway and provider secret names (`ORACLE_GATEWAY_URL`, `OMP_GATEWAY_TOKEN`, `TYPESAFE_API_KEY`, `TS_*`), the jev API default and the OMP build host. The hosted-workflow work reconciles the secret and workflow wiring.

## Not carried

BenchBox keeps: `soundness_merge_digest.py`, `soundness_drain_report.py`, `oracle_gateway_host.sh`, `oracle_local_review.py`, `pr_arm.py`, `pr_landing.py`, remote closeout and review wrappers, the inert-path read-index scanner and symlink guard, selectors and CI workflow YAML. Removed tests: `test_oracle_local_review.py`, `test_soundness_merge_digest*.py`, `test_gateway_host.py` (the shell-script test; `tests/integration/test_gateway_host.py` now covers the installed `install-omp` and `gateway-clients` call sites), `test_pr_arm_scenario.py`, `test_pr_landing_soundness_scenario.py`, the five BenchBox bump-workflow shell scenarios in `test_omp_pin_scenarios.py`, and the scanner, symlink, reader-index, selector and workflow-parity tests from `test_inert_paths.py`, `test_soundness_paths.py` and `test_scan_scratch.py`.

Test fixtures added after the seed: `tests/fixtures/consumer/alpha/` (BenchBox-shaped consumer data: the baseline policy and manifests plus a rewritten `oracle-verdict.yml` for the scenario runner) and `tests/fixtures/consumer/beta/` (a synthetic second consumer with its own App, attesters and base branch). Nineteen golden plans gained the `omp_config` plan field.

## Upstream port ledger

[UPSTREAM.md](UPSTREAM.md) and `tools/upstream_ledger.json` map every BenchBox commit since the baseline that touches the extraction inventory to a port commit, a reasoned no-op or a pending entry. Checked with `python3 tools/upstream_ledger.py check --benchbox-repo <BenchBox checkout> --ref origin/develop` against `origin/develop` at `ef52509983e1e3e345ec0b76b8c0f79c7d863867`: two inventory-touching commits, both recorded (`ee619c3` ported in `c6f2fed`, the batch member-result store removal; `ef52509` ported in `7d01388`, the first-promotion path-bounded source loader and leading-dot fix, its BenchBox policy section a no-op), no pending entries, exit 0.

## Parity and frozen source

`tools/parity.py` builds the wheel, installs it into a clean virtual environment outside the checkout, extracts the baseline scripts from `95c423c` with `git archive`, and runs the same fixture scenarios through both with the offline fake OMP, GitHub and classifier servers: the full `tests/integration` scenario suite (review classification across every size bucket, hosted review rounds, follow-ups, carry, disputes, quota and absence, sweeps, redaction, promotion refusals, local review findings, disputes and secret withholding) plus `tests/integration/test_parity_cases.py` (gate decisions, soundness-matcher results, gateway-client files, persisted local-review state). Golden and parity captures are normalized (paths, SHAs, timestamps, run IDs) and compared leaf by leaf; scenario assertions must pass on both sides.

```
uv run python tools/parity.py --benchbox-repo <BenchBox checkout> --report parity-report.json
uv run python tools/parity.py --source-hash
```

Run at Theseus commit `3179f65ea2f591a2559bd29aae28eacca450cbf5` (shipped source clean) against baseline `95c423c35efd3fb4962145bdc628006d5aaaad26`, whose extracted source hashes to the recorded seed value above:

```
frozen source hash   715a8f22818e9d1448a78f63c04a2a5118c77e9d49f75c84d31058bf54603e1f
wheel                theseus_reviewer-0.1.0-py3-none-any.whl  sha256 ee5fe097b5f6ba0efe9dfb81957936ed794f2c33cc27680b3c563f7847d37024
scenario tests       223 on each side, all passing or identically skipped, except the one listed below
captures compared    44: 18 identical, 26 differing only as listed
unexplained differences  0
parity PASS
```

The frozen source hash is the sha256 over the sorted lines `<path>\t<sha256 of file bytes>\n` for every tracked file under `src/theseus_reviewer/`. The wheel hash is recorded for this build; the release publishes the artifact built from the merged head, and a different hash there does not invalidate parity as long as the frozen source hash is unchanged.

Differences, each intentional and listed in `tools/parity.py`:

- Output: the `plan` capture carries `omp_config`, the consumer-configured omp config path the baseline hard-coded (26 captures).
- Outcome: `test_a_cloud_review_without_the_recorded_proxy_refuses_and_says_how_to_start_it` passes on Theseus and fails on the baseline, whose remedy text names the consumer command `make cloud-gateway`; Theseus says `start the cloud gateway`.
- Configuration: the policy `consumer` section, the `--policy`/`--root`, `--repo`, `--omp-config` and `--manifest` flags, the neutral local-state and OMP-cache roots, the `THESEUS_*` environment names and `theseus-cloud-gateway.status`, and cache file names keyed by the installed package digest. The baseline side maps each to its own name or flag.
- Ported after the baseline: the path-bounded first-promotion source loader and leading-dot archive fix, and the removal of the member-result store (both in BenchBox `develop` at `ef52509`, ledgered in UPSTREAM.md), are not in the `95c423c` baseline the parity run compares against; they change no captured scenario output and are covered by `tests/unit/reviewer/test_promotion.py` and `tests/unit/reviewer/test_local_review.py`.
- Runtime: the installed side runs from a wheel with no dependencies in a clean venv without PyYAML (its YAML parser is the vendored copy); the baseline side runs in its own venv with PyYAML 6.0.3 because the baseline imports it. Review and gate outputs are unchanged by the vendoring.
- Process model: local-review phase children re-exec the running interpreter with `-I -B -S` and the verified package directory (the baseline ran `python -m` with `PYTHONPATH` set to its export), and `THESEUS_GATE_COMMAND` is required with no fallback. Neither changes a captured output; `tests/integration/test_launcher_local_review.py` covers the launcher path.
- Excluded for having no baseline counterpart: `test_two_consumers`, `test_hosted_pins`, `test_gateway_host`, `test_launcher_local_review`, and the in-process CI-versus-local adapter comparison in `test_local_driver_scenarios`.

`tools/smoke_wheel.py` against the same wheel, from a scratch directory outside the checkout with the package imported from the installed venv: 72 scenario tests passed (hosted review, two consumers, promotion, local review, findings and disputes, concurrency, cloud reachability, redaction and the gateway-host call sites).

The claim is valid only for this source hash. Any change under `src/theseus_reviewer/`, or an un-ported BenchBox change found by the ledger check, invalidates it: port the change, rerun parity and replace the values above.

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
