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
promotion:                    # optional; when present, repository is required
  repository: acme/theseus
  bootstrap_reviewers: [acme-owner]   # optional; omit every bootstrap_* key when no first-promotion bootstrap is configured
  bootstrap_baseline_commit: <40-hex> # optional
  bootstrap_path_map: {}              # optional
  dependencies: {}
```

Top-level keys outside the reviewer policy schema are refused, naming the key. `brief_max_bytes` is no longer a policy key: the brief size bound is fixed at 100000 bytes inside the package, and a policy that still sets it is refused.

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
| `theseus preflight --policy PATH [--base-ref REF] [--head REF] [--json]` | Plan a local change without reviewers and report whether it can be reviewed; exit 0, 3 or 2. See [Review capacity](#review-capacity). |
| `theseus audit-coverage --capture FILE --policy PATH [--repo-root DIR] [--base-ref REF] [--head REF] [--json]` | Apply the hunk-coverage rule to a recorded review capture. See [Review depth](#review-depth). |
| `theseus promotion-check --policy PATH --lock FILE --source DIR` | Verify a lock's artifact bytes and dependency closure against the reviewed source tree. |
| `theseus bootstrap install|verify` | Install Theseus from a lock at a trusted consumer commit and print the executable; see [bootstrap.md](bootstrap.md). |
| `theseus matcher check|verify|digest` | Soundness-path predicate and vendored-copy drift check; see [matcher.md](matcher.md). |
| `theseus install-omp --policy PATH` | Install the OMP build pinned in the policy. |
| `theseus omp-bump`, `omp-watch`, `omp-candidate-check` | OMP maintenance; every policy, config and fixture path is a required flag. |

Tool-owned defaults are neutral: cache roots `~/.cache/theseus/omp` and `~/.cache/theseus/local-oracle`, with `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR`, `THESEUS_LOCAL_STATE` and `THESEUS_INSTALL_CACHE` overrides. A consumer that already holds an OMP cache or local findings and dispute state keeps them by pointing the variable at the existing directory (for example `THESEUS_LOCAL_STATE=~/.cache/<consumer>/local-oracle`); nothing is copied or deleted, and the persisted formats (`CACHE_SCHEMA`, the member-review schema string, finding identifiers, attempt and state files) are unchanged, so existing records keep working. A directory the consumer stops pointing at is simply no longer read.

The gate hands off to `theseus gate` in a subprocess (`THESEUS_GATE_COMMAND`, shlex-split; required, there is no fallback to the running interpreter) and surfaces a failed subprocess as `GitHubError`, so the hosted workflow can run the gate from a separately verified install.

## Review capacity

A review is planned with an estimated load: a fixed overhead of 150000 tokens, plus the tokens of the staged diff blocks of the required files (a whole removed promotion file counts in full), plus the tokens of the brief; bytes convert at 2.27 per token. In inline mode the diff is part of the brief, so it is counted once. The load is recorded in the plan as `review_load_tokens`.

A reviewer's capacity is the smallest `contextWindow - maxTokens` over its model and the models its fallback chain can move to, read from the pinned omp's catalog (`omp models --json`, run in the harness phase with the reviewer home and the review overlay) and written to the plan as `capacity`. Theseus keeps no model table, and no policy key sets a capacity. A model the catalog does not list is ignored; a reviewer none of whose models is listed, or a run whose catalog cannot be read, is assumed to hold 200000 tokens, so only a small change can still be reviewed.

A reviewer whose capacity is below the load is skipped (`skipped: the change needs about N tokens of reading; this reviewer's capacity is M`). When no enabled reviewer fits, the result is pending with a split instruction, no attempt is spent, and the head is not retried until a new push. A reviewer that fails with a context overflow is not retried; the chain moves on.

`theseus preflight --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the local change without running a reviewer and prints `{"schema": 1, "reviewable": bool, "reasons": [...], "advisories": [...]}`. Exit `0` means reviewable, `3` not reviewable (a Theseus promotion in the change, or a load above every reviewer's capacity), `2` the answer could not be determined (the diff cannot be built, the omp pin cannot be installed, the model catalog cannot be read). Advisories name reviewers that cannot hold the change when others can.

## Review depth

A verdict that clears a change, SHIP or SHIP WITH FIXES, is accepted only when the attempt saw every changed hunk of every required file.

- A hunk is seen when its whole block was returned by a read of the staged diff (`.oracle-pull-request.diff`, or `.oracle-promotion/upstream.diff` for upstream files), or, for a hunk that only adds lines, when the lines it adds were returned by a read of the file itself. Only the lines a read actually returned count: omp returns only the first 300 lines to a read with no line range (and about 300 to an open-ended one), so a longer file or diff takes ranges. Greps, reads of several paths in one call, URL and archive reads, and summarized reads count for nothing.
- Lines are the units git and omp both count: a line ends at `\n` only. A lone `\r` does not start a new line in omp's numbering (a read of a file with lone `\r` and `\r\n` separators reports the `\n`-delimited lines), so staged diffs are read as bytes and split on `\n` alone, and a `\r` planted in a diff cannot shift a later hunk's span. The `read-lines` canary pins this against the omp pin.
- omp cuts a line longer than 768 columns in a read and reports the cut lines in `details.meta.limits.columnTruncated.lines` (`first` and `last`, absolute line numbers); those lines, and the span between them, are not counted as returned, and a read whose cut lines are not reported as numbers counts for nothing. A `:raw` read is not cut and counts in full, so a hunk with a very long line is seen by reading the diff block with `:raw`. The canary pins both behaviors.
- A text file that git reports only as `Binary files differ` needs its whole file read; a binary file needs one successful read; a byte-identical rename or a mode change has no hunk and needs none.
- Inline briefs hold every changed hunk already; the plan asserts that each required file appears in the diff it inlines and falls back to a file-list brief otherwise.
- A retry names the diff line spans of the files the earlier attempt left unseen.
- An attempt whose omp stream shows a compaction (`auto_compaction_start`) is refused as incomplete, and the review overlay turns `compaction.enabled` and `compaction.midTurnEnabled` off.
- Records of an accepted clearing round carry `coverage: 1`. A SHIP or SHIP WITH FIXES recorded without it (made before this rule) is never carried to another head or the same patch; the next round is a full first round, and a follow-up after it takes full scope.
- Withdrawals and disputed fixes must cite a line inside the lines a read returned.
- The pull request diff is always built from git in the trusted prepare step (`git fetch --filter=blob:none` of the merge base and head, then `git diff -M`), and its path set must equal the pull request's file list; a diff that cannot be built holds the plan. The GitHub diff API is not used.
- A Theseus promotion's required reads are limited to what the consumer executes: the package source the wheel carries, `pyproject.toml`, and the reusable workflows the consumer's callers reference (followed through nested `uses:`). Renames are detected, and a byte-identical move needs no read. The lock's `delta_sha256` is unchanged by this.

Captures are schema 2: they add a `reads` ledger (path, returned line spans, truncated, elided) with no content. `theseus audit-coverage --capture FILE --policy PATH [--repo-root DIR] [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the change locally, stages its diffs, and applies the rule to a capture's reads (exact for schema 2; reconstructed favorably from the requested selectors for schema 1, whose results are withheld). A promotion's plan inputs resolve through `ORACLE_PROMOTION_SOURCES`, as for a local review. Exit `0` means every hunk was seen and nothing compacted, `3` not covered, `2` undetermined.

Attempt artifacts are kept 30 days, like captures.
