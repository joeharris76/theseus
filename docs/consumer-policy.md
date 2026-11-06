# Consumer policy

The `theseus-reviewer` wheel carries no consumer identity. Everything that names an account, branch or path comes from a policy file the consumer controls and Theseus reads from a trusted checkout (the gated base, never the pull request). Missing configuration fails with a message naming the missing key; nothing falls back to a default.

## Policy file

The policy is the existing reviewer policy (`version: 2`, reviewers, buckets, classifier, retry, omp pin, evidence) plus these keys.

```yaml
bot_login: acme-review        # GitHub App slug that posts verdicts; required, top level
consumer:
  gated_base: main            # branch the gate and review apply to
  attesters:                  # logins allowed to stand in, adjudicate, reset a backoff, attest an omp bump
    - acme-owner
  soundness_manifest: .github/soundness-paths.txt
  omp_config: .github/review-omp-config.yml
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

Protocol identities are not consumer settings and do not change: the `review-verdict` status context, the `review-gate` required context, marker grammars, artifact prefixes, finding identifiers and the persisted member-review schema string.

## Identity in practice

- Verdict statuses and reviews count only when created by `<bot_login>[bot]` with account type `Bot`.
- Stand-in attestations, owner instructions, adjudications, backoff resets and omp-bump evidence count only from `consumer.attesters` and only when unedited.
- The gate posts only for pull requests whose base is `consumer.gated_base`; `plan`, `sweep` and dispatch use the same value.
- A policy whose `consumer` section is absent, or whose manifests cannot be read under the trusted root, makes the gate fail (exit 2) rather than guess.

## Commands

| Command | Purpose |
|---|---|
| `theseus gate --repo OWNER/NAME --pr N --policy PATH [--root DIR] [--head-sha SHA] [--no-post] [--pending-first]` | Evaluate and post `review-gate`. Needs `GITHUB_TOKEN`. |
| `theseus plan/select/review/finalize/guard/sweep/...` | The reviewer engine commands used by the hosted review job; `--policy` is required wherever a policy is read. |
| `theseus local-review --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--head REF]` | Local pre-submission review. An empty `--base-ref` means `origin/<gated_base>`; the repository comes from `--repo`, `REVIEW_LOCAL_REPOSITORY` or the origin remote. |
| `theseus preflight --policy PATH [--base-ref REF] [--head REF] [--json]` | Plan a local change without reviewers and report whether it can be reviewed; exit 0, 3 or 2. See [Review capacity](#review-capacity). |
| `theseus audit-coverage --capture FILE --policy PATH [--repo-root DIR] [--base-ref REF] [--head REF] [--json]` | Apply the hunk-coverage rule to a recorded review capture. See [Review depth](#review-depth). |
| `theseus promotion-check --policy PATH --lock FILE --source DIR` | Verify a lock's artifact bytes and dependency closure against the reviewed source tree. |
| `theseus bootstrap install|verify` | Install Theseus from a lock at a trusted consumer commit and print the executable; see [bootstrap.md](bootstrap.md). |
| `theseus matcher check|verify|digest` | Soundness-path predicate and vendored-copy drift check; see [matcher.md](matcher.md). |
| `theseus install-omp --policy PATH` | Install the OMP build pinned in the policy. |
| `theseus omp-bump`, `omp-watch`, `omp-candidate-check` | OMP maintenance; every policy, config and fixture path is a required flag. |

Tool-owned defaults are neutral: cache roots `~/.cache/theseus/omp` and `~/.cache/theseus/local-review`, with `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR`, `THESEUS_LOCAL_STATE` and `THESEUS_INSTALL_CACHE` overrides. A consumer that already holds an OMP cache or local findings and dispute state keeps them by pointing the variable at the existing directory (for example `THESEUS_LOCAL_STATE=~/.cache/<consumer>/local-review`); nothing is copied or deleted, and the persisted formats (`CACHE_SCHEMA`, the member-review schema string, finding identifiers, attempt and state files) are unchanged, so existing records keep working. A directory the consumer stops pointing at is simply no longer read.

The gate hands off to `theseus gate` in a subprocess (`THESEUS_GATE_COMMAND`, shlex-split; required, there is no fallback to the running interpreter) and surfaces a failed subprocess as `GitHubError`, so the hosted workflow can run the gate from a separately verified install.

## Review capacity

A review is planned with an estimated load: a fixed overhead that depends on the brief mode, plus the tokens of the staged diff blocks of the required files (a whole removed promotion file counts in full), plus the tokens of the brief; bytes convert at 2.27 per token. In inline mode the diff is part of the brief, so it is counted once and there are no separate required bytes. The overhead is 80000 tokens for an inline brief, 170000 for a file-list brief and 170000 for a promotion (`capacity.OVERHEAD_TOKENS`). The load is recorded in the plan as `review_load_tokens`.

A reviewer's capacity is `contextWindow - maxTokens` of its primary model, read from the pinned omp's catalog (`omp models --json`, run in the harness phase for a hosted and a local review alike) and written to the plan as `capacity`. When the reviewer config's `retry.fallbackOn` is anything other than `usage-limit`, the capacity is the smallest such value over the primary and the models its fallback chain can move to. With `fallbackOn: usage-limit` the fallbacks are left out: a fallback only takes over when the primary reports a usage limit, which is rare, and if a fallback's window is smaller than the context already built up the attempt ends as a context overflow, which is a non-retried absence that moves the chain on and never clears a change. Counting every fallback would instead cap every reviewer at the smallest fallback window and make a change between that and the primary's window unreviewable. The listing runs in an isolated temporary home that holds only the trusted reviewer config and a provider sheet naming the gateway's providers with a placeholder credential, so the catalog lists those providers' models whatever the caller's `~/.omp` holds, and no real gateway token reaches the listing. Theseus keeps no model table, and no policy key sets a capacity.

Capacity is never assumed. A reviewer whose primary model the catalog does not list has an unknown capacity, and so does every reviewer when the catalog cannot be read; the plan records the reason as `capacity_error` and, for a sized change, such a reviewer is skipped with `skipped: this reviewer's capacity cannot be determined: <reason>`. When no enabled reviewer can be selected for that reason the result is a plain pending with those reasons (hosted and local alike): no attempt is spent, and unlike a too-large change it is retried by the sweep once the retry backoff has elapsed (the pending cause is `all-absent`), because a later catalog read can succeed. The omp candidate check asserts, against the candidate build, that the same listing names every model the reviewer config and its fallback chains use (contract row 13), so a build that stops listing a configured model fails the check before it is pinned.

### Overhead calibration

The overhead is calibrated from the archived review captures (1142 attempts with usage data, from the hosted runs and the local reviews). For each attempt the peak context is the largest `input + cacheRead + cacheWrite + output` over its assistant turns. The brief is the first user message; its mode is inline when it carries the `Unified diff of these files from base to head` block, promotion when it names `.review-promotion`, and file-list otherwise. The observed overhead of an attempt is the peak minus the brief's tokens minus the required reading. For inline briefs the required reading is zero, because the load does not count it either, so everything the reviewer reads or writes beyond the brief is overhead. For file-list briefs it is estimated from the changed-line counts in the brief's file listing at 38.5 bytes per changed line plus 1074 bytes per file (a least-squares fit of the inline attempts' diff sizes against their listings), and for promotions it is the tool output of the reads under `.review-promotion` and of the staged diff. Tool output and the brief are converted at the same 2.27 bytes per token the load uses, so the figures are consistent with the plan.

| mode | n | p50 | p90 | p95 | margin | overhead |
| --- | --- | --- | --- | --- | --- | --- |
| inline | 893 | 26680 | 55792 | 70892 | 10% of p95, rounded up to 5000 | 80000 |
| file-list | 226 | 63920 | 136488 | 152760 | same | 170000 |
| promotion | 23 | -76735 | 49677 | 53017 | same, and not below the file-list overhead because the sample is small | 170000 |

The overhead is the p95 plus the margin. A promotion's p95 alone would be 60000, but 23 attempts is too few to rely on, so it takes the file-list value. A negative p50 means the reads were over-counted by the byte-to-token ratio, which only makes the load more cautious.

With the pinned omp's catalog (`@smol` 144000, `@default`, `@review`, `@plan` and `@slow` 872000) BenchBox's policy can select these reviewers. A typical inline pull request (median brief 8078 tokens) loads about 88000 tokens, and a typical file-list pull request (median brief 5511 and about 43000 tokens of required diff) about 219000.

| reviewer | model | capacity | typical inline (about 88000) | typical file-list (about 219000) | largest inline brief | largest file-list required reading plus brief |
| --- | --- | --- | --- | --- | --- | --- |
| trivial | `@smol` | 144000 | selectable | not selectable | 64000 | none |
| small | `@smol` | 144000 | selectable | not selectable | 64000 | none |
| medium | `@default` | 872000 | selectable | selectable | 792000 | 702000 |
| large | `@review` | 872000 | selectable | selectable | 792000 | 702000 |
| huge | `@plan` | 872000 | selectable | selectable | 792000 | 702000 |
| max | `@slow` | 872000 | selectable | selectable | 792000 | 702000 |

A reviewer whose capacity is below the load is skipped (`skipped: the change needs about N tokens of reading; this reviewer's capacity is M`). When no enabled reviewer fits, the result is pending with a split instruction, no attempt is spent, and the head is not retried until a new push. A reviewer that fails with a context overflow is not retried; the chain moves on.

`theseus preflight --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the local change without running a reviewer and prints `{"schema": 1, "reviewable": bool, "reasons": [...], "advisories": [...]}`. Exit `0` means reviewable, `3` not reviewable (a Theseus promotion in the change, or a load above every reviewer's capacity), `2` the answer could not be determined (the diff cannot be built, the omp pin cannot be installed, the model catalog cannot be read, or no reviewer can be selected because the capacity of every reviewer that is not too large is unknown). Advisories name reviewers that cannot hold the change, or whose capacity is unknown, when others can.

## Review depth

A verdict that clears a change, SHIP or SHIP WITH FIXES, is accepted only when the attempt saw every changed hunk of every required file.

- A hunk is seen when its whole block was returned by a read of the staged diff (`.review-pull-request.diff`, or `.review-promotion/upstream.diff` for upstream files), or, for a hunk that only adds lines, when the lines it adds were returned by a read of the file itself. Only the lines a read actually returned count: omp returns only the first 300 lines to a read with no line range (and about 300 to an open-ended one), so a longer file or diff takes ranges. Greps, reads of several paths in one call, URL and archive reads, and summarized reads count for nothing.
- Lines are the units git and omp both count: a line ends at `\n` only. A lone `\r` does not start a new line in omp's numbering (a read of a file with lone `\r` and `\r\n` separators reports the `\n`-delimited lines), so staged diffs are read as bytes and split on `\n` alone, and a `\r` planted in a diff cannot shift a later hunk's span. The `read-lines` canary pins this against the omp pin.
- omp cuts a line longer than 768 columns in a read and reports the cut lines in `details.meta.limits.columnTruncated.lines` (`first` and `last`, absolute line numbers); those lines, and the span between them, are not counted as returned, and a read whose cut lines are not reported as numbers counts for nothing. A `:raw` read is not cut and counts in full, so a hunk with a very long line is seen by reading the diff block with `:raw`. omp counts columns in UTF-16 code units (an astral character is two), and a diff line is counted with its `+`, `-` or space prefix. Because a plain read drops every line from the first cut line to the last, the retry hint marks a hunk `:raw` when its block holds a line over 768 columns or lies between two such lines of the same diff, and gives the span to paste, for example `(read with :raw as .review-pull-request.diff:raw:120-180)`; hunks outside that stretch keep their plain `lines a-b` spans. The `read-lines` canary pins the raw read's line numbers.
- A text file that git reports only as `Binary files differ` needs its whole file read; a binary file needs one successful read; a byte-identical rename or a mode change has no hunk and needs none.
- Inline briefs hold every changed hunk already; the plan asserts that each required file appears in the diff it inlines and falls back to a file-list brief otherwise.
- A retry names the diff line spans of the files the earlier attempt left unseen.
- An attempt whose omp stream shows a compaction (`auto_compaction_start`) is refused as incomplete, and the review overlay turns `compaction.enabled` and `compaction.midTurnEnabled` off.
- Records of an accepted clearing round carry `coverage: 1`. A SHIP or SHIP WITH FIXES recorded without it (made before this rule) is never carried to another head or the same patch; the next round is a full first round, and a follow-up after it takes full scope.
- Withdrawals and disputed fixes must cite a line inside the lines a read returned.
- The pull request diff is always built from git in the trusted prepare step (`git fetch --filter=blob:none` of the merge base and head, then `git diff -M`), and its path set must equal the pull request's file list; a diff that cannot be built holds the plan. The GitHub diff API is not used.
- A Theseus promotion's required reads are limited to what the consumer executes: the package source the wheel carries, `pyproject.toml`, and the reusable workflows the consumer's callers reference (followed through nested job-level `uses:`). Theseus supports only job-level reusable workflows plus its Python package. The new surface is every reference at the base and the head, so a change can only widen it; the old surface comes from the base workflows and the old tree alone. Every workflow in the new surface and not the old one is read whole from the staged new tree, changed or not, so a Theseus workflow the head newly references is never read by its hunks. A job-level reference to the Theseus repository holds the plan unless it is pinned to the full 40-hex commit the lock promotes (the base lock's commit for base workflows; a branch, tag or `refs/...` runs a ref the promoted tree does not prove), names a workflow file that exists in the tree, and takes a recognised form; owner and repository match case-insensitively, a `.git` suffix on the repository name is the same repository, and a reference that mentions the repository but does not parse holds. A step-level `uses:` of any action in the Theseus repository holds the plan whatever its kind (the action's files, the commands it runs and what they reach cannot be bounded), as does a step-level `./` reference inside a Theseus workflow (it resolves against `$GITHUB_WORKSPACE` at run time, not against the promoted Theseus commit). Job-level `./.github/workflows/x.yml` references inside Theseus workflows are followed. Actions from other repositories and `docker://` images are ignored. Renames are detected, and a byte-identical move needs no read. The lock's `delta_sha256` is unchanged by this.

Captures are schema 2: they add a `reads` ledger (path, returned line spans, truncated, elided) with no content. `theseus audit-coverage --capture FILE --policy PATH [--repo-root DIR] [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the change locally, stages its diffs, and applies the rule to a capture's reads (exact for schema 2; reconstructed favorably from the requested selectors for schema 1, whose results are withheld). A promotion's plan inputs resolve through `REVIEW_PROMOTION_SOURCES`, as for a local review. Exit `0` means every hunk was seen and nothing compacted, `3` not covered, `2` undetermined.

Attempt artifacts are kept 30 days, like captures.
