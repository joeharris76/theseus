# Consumer policy

The `theseus-reviewer` wheel carries no consumer identity. Everything that names an account, branch or path comes from a policy file the consumer controls and Theseus reads from a trusted checkout (the gated base, never the pull request). Missing configuration fails with a message naming the missing key; nothing falls back to a default.

## Policy file

The policy is the existing reviewer policy (`version: 2`, reviewers, buckets, classifier, retry, omp pin, evidence) plus these keys.

```yaml
bot_login: acme-oracle        # GitHub App slug that posts verdicts; required, top level
consumer:
  gated_base: main            # branch the gate and review apply to
  attesters:                  # logins allowed to stand in, adjudicate, reset a backoff, attest an omp bump
    - acme-owner
  soundness_manifest: .github/soundness-paths.txt
  omp_config: .github/oracle-omp-config.yml
  inert_manifest: .github/inert-paths.txt            # optional
  omp_fixture_prefix: tests/fixtures/omp_candidate   # optional
promotion:                    # optional; when present, repository and bootstrap_reviewers are required
  repository: acme/theseus
  bootstrap_reviewers: [acme-owner]
  bootstrap_baseline_commit: <40-hex>
  bootstrap_path_map: {}
  dependencies: {}
sqlglot_adoption:             # optional; all five keys are required when present
  pin_path: benchbox/_vendor/sqlglot.json
  fork_repository: acme/acme-sqlglot
  upstream_repository: tobymao/sqlglot
  distribution: acme-sqlglot
  namespace: acme._vendor.sqlglot
```

All `consumer` paths are repository-relative and are read relative to the trusted checkout root (`--root` for the gate, the working directory for `plan`, the trusted root for local review). Absolute paths, `..`, empty or backslash segments are refused.

Protocol identities are not consumer settings and do not change: the `oracle-verdict` status context, the `oracle-gate` required context, marker grammars, artifact prefixes, finding identifiers and the persisted member-review schema string.

## Identity in practice

- Verdict statuses and reviews count only when created by `<bot_login>[bot]` with account type `Bot`.
- Stand-in attestations, owner instructions, adjudications, backoff resets and omp-bump evidence count only from `consumer.attesters` and only when unedited.
- The gate posts only for pull requests whose base is `consumer.gated_base`; `plan`, `sweep` and dispatch use the same value.
- A policy whose `consumer` section is absent, or whose manifests cannot be read under the trusted root, makes the gate fail (exit 2) rather than guess.

## SQLGlot runtime adoption

A consumer that bundles a relocated SQLGlot wheel names it in `sqlglot_adoption`. The section is read from the trusted base policy; without it nothing below applies. A pull request counts as an adoption when it changes `pin_path` or a `.zip`/`.whl` file directly inside the pin's directory. Its fork source is the review subject, not the pin edit.

For such a pull request the trusted reviewer, never executing candidate code:

- reads the pin at the exact base and head commits and refuses unless both validate: schema 1, the policy distribution and namespace, the policy fork repository, a `<upstream>.<fork revision>` version, full lowercase 40-hex `fork_sha` and `upstream_sha`, a 64-hex `sha256`, and a wheel and archive name derived from the distribution and version (so the archive path cannot leave the pin's directory);
- reads the pin and archive only as regular Git file blobs, refuses touched archives other than the base or head pinned archive, and requires the exact head archive's SHA-256 to equal the pin;
- verifies that the pinned fork commit is reachable from the canonical fork's default branch and that the peeled upstream version tag resolves to the pinned upstream commit;
- requires a newer version on a descendant fork commit for a forward adoption, or a lower version on an ancestor fork commit for a reviewed rollback. Both directions require the pinned upstream commit to be an ancestor of the candidate fork commit and stage the full source delta. An unchanged fork commit must retain its version and upstream commit;
- fetches the full fork source trees at the base and head `fork_sha` (symlinks are recorded as `symlink -> target` text) and stages them with the actual unified delta under `.oracle-promotion/`, exactly as for a Theseus promotion;
- verifies the wheel against the head source with the package's own copy of the fork relocation, not the fork's `downstream/relocate.py`: the runtime file set and bytes must equal the relocated `sqlglot/**/*.py`, `py.typed`, `LICENSE` and the generated `_version.py`; the provenance file must agree with the pin, the staged `downstream/patches.json` and the payload hashes; dist-info must be the allowed set with matching metadata, license and `RECORD`; and no `.so`, `.pyd`, `.dll`, `.dylib`, `.pth`, `.pyc`, `entry_points.txt` or `.data` entry may exist. Missing, extra or differing files, an unknown source, a wrong digest, identity or provenance are nonapproval, never a fallback. A relocation the package does not recognise also refuses; it is updated through a reviewed Theseus change.
- requires UTF-8 Python source without a byte-order mark, so encoding cookies cannot make the interpreter execute bytes that the source review treats as comments, and requires each ZIP member to have the same bytes under the verifier and Python's ZIP importer.

The archive must use an ordinary, single-disk ZIP directory with no ZIP64 directory, archive comment, entry comments, or trailing data. This avoids differences between Python's archive verifier and ZIP importer when choosing the central directory.

Classification and the required reads come from the source delta. A pin or archive edit is always in review scope even when the consumer's exempt paths or soundness manifest would otherwise exempt it, runtime-relevant source (`sqlglot/`, `sqlglotc/`, `downstream/`, `.github/`, `LICENSE`, `pyproject.toml`, `setup.py`, `setup.cfg`, `MANIFEST.in`) cannot be exempted by `evidence.exempt_paths`, and an oversized file list stays `oversize` instead of a truncated review. Consumers may add the archive glob to `evidence.exempt_paths` because the wheel is verified mechanically; it is still never exempt from scope.

The relocation verifier is not a Python security analyzer. It checks exact relocation output and known namespace constructs; it cannot prove the safety of arbitrary computed imports or executable source. Independent review of the staged source remains required. Both local and hosted review apply the same adoption scope rules.

The shipped review records an ordinary promotion evidence record whose `lock_sha256` binds the base and head pins, `old_artifact_sha256` and `new_artifact_sha256` bind the base and head archive hashes, and `delta_sha256` binds the source delta. The gate refuses an adoption pull request unless the latest unedited oracle review for the exact head carries a matching record and the head archive still hashes to the pin, so a moved base or head needs a new review; a stand-in attestation cannot waive it. A pull request cannot adopt a SQLGlot runtime and promote Theseus together.

An incomplete PR file list does not by itself turn an unrelated change into an adoption: the gate checks both regular-file pins and the pinned head archive directly. If the pins are identical and the archive still matches their hash, ordinary review rules apply. A changed pin, invalid archive, or known unpinned archive remains a refusal; an owner attestation cannot waive adoption evidence.

The source loader honours `ORACLE_PROMOTION_SOURCES` as for Theseus promotions. Fork source is otherwise fetched with the CI token as a commit tarball of `fork_repository`. Pins and runtime archives are always read from exact regular Git blobs in the consumer repository.

## Commands

| Command | Purpose |
|---|---|
| `theseus gate --repo OWNER/NAME --pr N --policy PATH [--root DIR] [--head-sha SHA] [--no-post] [--pending-first]` | Evaluate and post `oracle-gate`. Needs `GITHUB_TOKEN`. |
| `theseus plan/select/review/finalize/guard/sweep/...` | The reviewer engine commands used by the hosted review job; `--policy` is required wherever a policy is read. |
| `theseus local-review --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--head REF]` | Local pre-submission review. An empty `--base-ref` means `origin/<gated_base>`; the repository comes from `--repo`, `ORACLE_LOCAL_REPOSITORY` or the origin remote. |
| `theseus promotion-check --policy PATH --lock FILE --source DIR` | Verify a lock's artifact bytes and dependency closure against the reviewed source tree. |
| `theseus bootstrap install|verify` | Install Theseus from a lock at a trusted consumer commit and print the executable; see [bootstrap.md](bootstrap.md). |
| `theseus matcher check|verify|digest` | Soundness-path predicate and vendored-copy drift check; see [matcher.md](matcher.md). |
| `theseus install-omp --policy PATH` | Install the OMP build pinned in the policy. |
| `theseus omp-bump`, `omp-watch`, `omp-candidate-check` | OMP maintenance; every policy, config and fixture path is a required flag. |

Tool-owned defaults are neutral: cache roots `~/.cache/theseus/omp` and `~/.cache/theseus/local-oracle`, with `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR`, `THESEUS_LOCAL_STATE` and `THESEUS_INSTALL_CACHE` overrides. A consumer that already holds an OMP cache or local findings and dispute state keeps them by pointing the variable at the existing directory (for example `THESEUS_LOCAL_STATE=~/.cache/<consumer>/local-oracle`); nothing is copied or deleted, and the persisted formats (`CACHE_SCHEMA`, the member-review schema string, finding identifiers, attempt and state files) are unchanged, so existing records keep working. A directory the consumer stops pointing at is simply no longer read.

The gate hands off to `theseus gate` in a subprocess (`THESEUS_GATE_COMMAND`, shlex-split; required, there is no fallback to the running interpreter) and surfaces a failed subprocess as `GitHubError`, so the hosted workflow can run the gate from a separately verified install.
