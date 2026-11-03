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
```

All `consumer` paths are repository-relative and are read relative to the trusted checkout root (`--root` for the gate, the working directory for `plan`, the trusted root for local review). Absolute paths, `..`, empty or backslash segments are refused.

Protocol identities are not consumer settings and do not change: the `oracle-verdict` status context, the `oracle-gate` required context, marker grammars, artifact prefixes, finding identifiers and the persisted member-review schema string.

## Identity in practice

- Verdict statuses and reviews count only when created by `<bot_login>[bot]` with account type `Bot`.
- Stand-in attestations, owner instructions, adjudications, backoff resets and omp-bump evidence count only from `consumer.attesters` and only when unedited.
- The gate posts only for pull requests whose base is `consumer.gated_base`; `plan`, `sweep` and dispatch use the same value.
- A policy whose `consumer` section is absent, or whose manifests cannot be read under the trusted root, makes the gate fail (exit 2) rather than guess.

## Commands

| Command | Purpose |
|---|---|
| `theseus gate --repo OWNER/NAME --pr N --policy PATH [--root DIR] [--head-sha SHA] [--no-post] [--pending-first]` | Evaluate and post `oracle-gate`. Needs `GITHUB_TOKEN`. |
| `theseus plan/select/review/finalize/guard/sweep/...` | The reviewer engine commands used by the hosted review job; `--policy` is required wherever a policy is read. |
| `theseus local-review --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--head REF]` | Local pre-submission review. An empty `--base-ref` means `origin/<gated_base>`; the repository comes from `--repo`, `ORACLE_LOCAL_REPOSITORY` or the origin remote. |
| `theseus promotion-check --policy PATH --lock FILE --source DIR` | Verify a lock's artifact bytes and dependency closure against the reviewed source tree. |
| `theseus bootstrap install|verify|run` | Install Theseus from a lock at a trusted consumer commit; see [bootstrap.md](bootstrap.md). |
| `theseus matcher check|verify|digest` | Soundness-path predicate and vendored-copy drift check; see [matcher.md](matcher.md). |
| `theseus install-omp --policy PATH` | Install the OMP build pinned in the policy. |
| `theseus omp-bump`, `omp-watch`, `omp-candidate-check` | OMP maintenance; every policy, config and fixture path is a required flag. |

Tool-owned defaults are neutral: cache roots `~/.cache/theseus/omp` and `~/.cache/theseus/local-oracle`, with `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR`, `THESEUS_LOCAL_STATE` and `THESEUS_INSTALL_CACHE` overrides. A consumer that already holds an OMP cache or local findings and dispute state keeps them by pointing the variable at the existing directory (for example `THESEUS_LOCAL_STATE=~/.cache/<consumer>/local-oracle`); nothing is copied or deleted, and the persisted formats (`CACHE_SCHEMA`, the member-review schema string, finding identifiers, attempt and state files) are unchanged, so existing records keep working. A directory the consumer stops pointing at is simply no longer read.

The gate hands off to `theseus gate` in a subprocess (`THESEUS_GATE_COMMAND`, shlex-split; required, there is no fallback to the running interpreter) and surfaces a failed subprocess as `GitHubError`, so the hosted workflow can run the gate from a separately verified install.
