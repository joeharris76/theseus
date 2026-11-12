# Consumer policy

The `theseus-reviewer` wheel carries no consumer identity. Everything that names an account, branch or path comes from a policy file the consumer controls and Theseus reads from a trusted checkout (the gated base, never the pull request). Missing configuration fails with a message naming the missing key; nothing falls back to a default.

## Policy file

The policy is the existing reviewer policy (`version: 2`, reviewers, buckets, classifier, retry, omp pin, evidence) plus these keys.

```yaml
bot_login: acme-review        # GitHub App slug that posts verdicts; required, top level
consumer:
  gated_base: main            # branch, or list of branches and prefix patterns, the gate and review apply to
  attesters:                  # logins allowed to stand in, adjudicate, reset a backoff, attest an omp bump
    - acme-owner
  soundness_manifest: .github/soundness-paths.txt
  omp_config: .github/review-omp-config.yml
  inert_manifest: .github/inert-paths.txt            # optional
  omp_fixture_prefix: tests/fixtures/omp_candidate   # optional
  review_guidance: .github/review-guidance.md        # optional; see Reviewer guidance
classifier:
  unavailable_bucket: large   # optional; size bucket used when the classifier is unreachable
  fallback: decisions         # optional; needs unavailable_bucket; OpenAI Decisions answers when TypeSafe is down
  fallback_min_bucket: medium # optional; needs fallback; floor for Decisions answers only
  size_floor:                 # optional; applies to every classifier backend
    min_lines: 1500           # changed lines outside data_paths and exclude that force at least `large`
    exclude: []               # extra globs not counted, beyond data_paths (lockfiles, generated files)
promotion:                    # optional; when present, repository is required
  repository: acme/theseus
  bootstrap_reviewers: [acme-owner]   # optional; omit every bootstrap_* key when no first-promotion bootstrap is configured
  bootstrap_baseline_commit: <40-hex> # optional
  bootstrap_path_map: {}              # optional
  dependencies: {}
```

`classifier.unavailable_bucket` is one of the size buckets (`trivial`, `small`, `medium`, `large`, `huge`, `max`). Without it a classifier outage leaves the change pending, as before. With it, a classifier outage (the judgment service unreachable, answering 429 or a 5xx, answering a 200 whose answers are unusable, or a classification that outlives its 300-second total; a missing `TYPESAFE_API_KEY` or any other 4xx is configuration and still refuses) classifies the change from that bucket instead, or from the size bucket the service had already answered when that is larger (only when the change-level answers were usable and the failure came later, while scoring files; an unusable change-level answer carries no bucket): the capability is `deep`, the template is `deep`, and when the change touches `classifier.gate_paths` the bucket is raised to at least `large` and the template is `gate`. The classification is recorded with `backend: unavailable`, and the review body says `Classification unavailable (<cause>): reviewed from the <bucket> bucket with the <template> template.` A hosted review and a local review behave the same way.

`classifier.fallback: decisions` adds a second backend between TypeSafe and `unavailable_bucket`. When TypeSafe is in outage (unreachable, 429, 5xx, a classification that outlives its 300-second total, or a 200 whose answers are unusable) the same questions go to the OpenAI Decisions API (`POST /v1/decisions`, model `gpt-6-luna`; at most two requests, 30 seconds each and 60 seconds in total, no retries) with `OPENAI_API_KEY`. Decisions answers go through the same rules as TypeSafe's: a size answer below 0.60 probability steps down one bucket (never below `small`), and gate paths force the `gate` template and at least `large`. `fallback_min_bucket` then raises a Decisions size to at least that bucket. A refusal, a malformed answer, an HTTP error, a timeout or a missing `OPENAI_API_KEY` ends at `unavailable_bucket`; a missing key also adds a warning to the step summary on every run, never a refusal. A TypeSafe 4xx (other than 429) or a missing `TYPESAFE_API_KEY` never calls Decisions. When TypeSafe answered the change but failed while scoring files, its change-level answer is kept and Decisions supplies the per-file rows (none if it fails too). Invalid-attempt recovery never uses Decisions. The classification records `backend` (`jev`, `decisions` or `unavailable`), `model`, the validated `answers`, the applied `floors`, `degraded_from` and Decisions `usage`, and the review body names the backend and model.

`classifier.size_floor` raises any backend's size to at least `large` when the changed lines in files outside `classifier.data_paths` and `size_floor.exclude` reach `min_lines`. `size_floor.exclude` globs use fnmatch, where `*` crosses `/`, so keep them suffix-anchored (for example `uv.lock`, `*baseline*.json`). The `classifier` section refuses unknown keys. The rendered per-file score is the service's 0-based level plus one (1 to 5).

The `protocol` section's `max_defects` and `max_do_not_ship` are required. Three further keys are optional and strictly validated; leaving them out keeps today's behaviour:

```yaml
protocol:
  max_finding_rounds: 4     # integer >= 1; see Round discipline
  pre_existing: known       # block (default) or known; see Pre-existing findings
brief:
  include_intent: true      # boolean, default false; see Author statement
```

Top-level keys outside the reviewer policy schema are refused, naming the key. `brief_max_bytes` is no longer a policy key: the brief size bound is fixed at 400000 bytes inside the package (an inline brief is further limited to 100000 bytes), and a policy that still sets it is refused. The brief reaches omp on standard input, so no operating-system argument limit applies; the review capacity below, not the bound, decides which reviewers can hold it.

All `consumer` paths are repository-relative and are read relative to the trusted checkout root (`--root` for the gate, the working directory for `plan`, the trusted root for local review). Absolute paths, `..`, empty or backslash segments are refused.

Protocol identities are not consumer settings and do not change: the `review-verdict` status context, the `review-gate` required context, marker grammars, artifact prefixes, finding identifiers and the persisted member-review schema string.

## Identity in practice

- Verdict statuses and reviews count only when created by `<bot_login>[bot]` with account type `Bot`.
- Stand-in attestations, owner instructions, adjudications, backoff resets and omp-bump evidence count only from `consumer.attesters` and only when unedited.
- The gate and the review apply only to pull requests whose base matches `consumer.gated_base`, and only when the policy was read from that same base: `plan`, `sweep` and dispatch use the same value, and a pull request into any other base is refused (`UngatedBase`, exit 0 with nothing posted).
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

`omp-candidate-check` exercises the candidate executable against isolated loopback broker, gateway and scripted upstream processes, never paid model completions. The contract inventory is `candidate_checks.CONTRACT_ROWS`: row 14 preserves stdin prompt transport; rows 15–17 check the served set without credentials (C1), a credentialed provider hidden by `disabledProviders` with a credentialed control (C2), and the upstream request model after rewriting a reviewer role (C3). Missing observations or a control that cannot serve the provider fail closed.

Tool-owned defaults are neutral: cache roots `~/.cache/theseus/omp` and `~/.cache/theseus/local-review`, with `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR`, `THESEUS_LOCAL_STATE` and `THESEUS_INSTALL_CACHE` overrides. A consumer that already holds an OMP cache or local findings and dispute state keeps them by pointing the variable at the existing directory (for example `THESEUS_LOCAL_STATE=~/.cache/<consumer>/local-review`); nothing is copied or deleted, and the persisted formats (`CACHE_SCHEMA`, the member-review schema string, finding identifiers, attempt and state files) are unchanged, so existing records keep working. A directory the consumer stops pointing at is simply no longer read.

The gate hands off to `theseus gate` in a subprocess (`THESEUS_GATE_COMMAND`, shlex-split; required, there is no fallback to the running interpreter) and surfaces a failed subprocess as `GitHubError`, so the hosted workflow can run the gate from a separately verified install.

## Provider loss

Losing one model provider never stops reviews. Four words describe what the review flow knows about providers, and none of them is a hard-coded list:

- **Routed providers** are the providers named by the trusted omp config, in `modelRoles` and `retry.fallbackChains`. They are derived from the config. The reviewer's omp is told to reach exactly these providers, only through the gateway. Routing is an allow-list, not a requirement.
- **Granted providers** are the routed providers that have at least one broker account, minus any the host owner excludes. `theseus gateway-clients` writes `clients.json` with the models and accounts of exactly these.
- **Served providers** are the providers of the models the running gateway lists on `/v1/models` for the client token.
- **No provider is required.** The floor is at least one served model that can review the change.

The candidates of a reviewer are its role's model followed by the `retry.fallbackChains` entries of that model, in order. The chain is the owner-curated list of qualified substitutes, and the config is read from the trusted base only: a pull request's own omp config never selects or excludes a reviewer.

At the start of the attempt phase (hosted and local) the review probes `GET /healthz` and `GET /v1/models` with the client token and records the served models in the plan, or `served_error` when the gateway is unreachable, answers an error, rejects the token or lists malformed data. The probe is repeated until the first attempt has run and then fixed for the run. Then, per reviewer:

0. First, whether or not any provider is served, the slot's own capacity (its primary's, as computed for the healthy case) is checked: a load above it is skipped as oversize (and the next tier is tried) and an unknown capacity fails closed. A substitute never widens what a slot accepts.
1. When its first candidate is served and has not failed in this run, it runs as today.
2. Otherwise the first later candidate that is served, not excluded and whose own window (`contextWindow - maxTokens` from the pinned omp's catalog, recorded for every routed model) holds the load runs instead. The attempt is written with a rewritten reviewer config: the role points at the chosen model, and the chain keeps only the remaining served candidates.
3. Otherwise the reviewer is skipped with `unavailable: <each candidate and why>`, or with `capacity-degraded: ...` when served candidates exist but none holds a load that the primary would hold. Escalation stays upward only; a reviewer is never replaced by a smaller tier, and no model outside the trusted candidate lists runs.

A provider that is listed but fails when called is excluded for the rest of the run. The structured terminal error of the omp stream (`provider` and `errorStatus` of the final assistant message, never model text) attributes a 401, 404 or 5xx (including 529) to a provider. The same reviewer then tries its next candidate, and an attempt that failed on the provider the reviewer was running does not count against the two attempts per reviewer. Quota errors keep their pool block; which provider a pool leads with is derived from the config (`pool_providers` in the plan).

When no reviewer can run because of availability alone (the served set is empty or unknown, or no served candidate holds the load) and no attempt ran, the result is pending with cause `unavailable`: no attempt and no retry from the budget is spent, the status reads `pending: no reviewer available (...)`, the gate stays pending, and the sweep re-dispatches it after `retry.backoff_start_minutes` with no doubling. Nothing is merged on an unavailable review; only an owner stand-in does.

Every review-verdict status and body reports degradation: the status description ends with `· degraded: <providers>` (the routed providers that are not served or were excluded), and the body has a `Provider availability` section naming unserved and excluded providers and each substitution (`large: anthropic/claude-opus-5-5:medium → openai-codex/gpt-6.1-sol:high`). The reviewer identity stays the tier; the body shows the model that ran.

The reusable `review.yml` has a `health` job that runs only on the caller's `schedule` event, with `contents: read`. It joins the tailnet and runs `theseus drive health`, which exits 1 when the gateway is unreachable, rejects the client token, or does not serve every provider the trusted config names, so the scheduled run goes red and its summary names the providers.

## Brief mode

The brief mode is chosen at plan time from the rendered brief, not from byte counts of its parts. The plan renders the inline candidate and the file-list candidate with every template (the largest result counts), the worst-case retry note that marks every required file, the prior defects and the disputes, and adds the largest the per-file priority slot can be. The mode is `inline` when the diff is complete and has no opaque file and that rendering fits 100000 bytes, else `file-list` when that fits the 400000-byte bound, else `oversize` (pending with a split instruction, no attempt spent). Because the classification is not known at plan time, the measured size is an upper bound on the brief the review job renders. The recorded load counts the brief of the chosen mode without the retry marks.

omp receives the brief on standard input from a private file in the scratch directory that is removed when the run ends; the command line carries no prompt. The omp candidate check (contract row 14) proves, against the candidate build, that a token placed only on standard input reaches the model request, that the command line does not carry it, and that a prompt as large as the brief bound (400000 bytes), which no single argument could hold, arrives as one unchanged request body.

## Review capacity

A review is planned with an estimated load: a fixed overhead that depends on the brief mode, plus the tokens of the staged diff blocks of the required files (a whole removed promotion file counts in full), plus the tokens of the brief; bytes convert at 2.27 per token. In inline mode the diff is part of the brief, so it is counted once and there are no separate required bytes. The overhead is 80000 tokens for an inline brief, 165000 for a file-list brief and 165000 for a promotion (`capacity.OVERHEAD_TOKENS`). The load is recorded in the plan as `review_load_tokens`.

A reviewer's capacity is `contextWindow - maxTokens` of its primary model, read from the pinned omp's catalog (`omp models --json`, run in the harness phase for a hosted and a local review alike) and written to the plan as `capacity`. When the reviewer config's `retry.fallbackOn` is anything other than `usage-limit`, the capacity is the smallest such value over the primary and the models its fallback chain can move to. With `fallbackOn: usage-limit` the fallbacks are left out: a fallback only takes over when the primary reports a usage limit, which is rare, and if a fallback's window is smaller than the context already built up the attempt ends as a context overflow, which is a non-retried absence that moves the chain on and never clears a change. Counting every fallback would instead cap every reviewer at the smallest fallback window and make a change between that and the primary's window unreviewable. The listing runs in an isolated temporary home that holds only the trusted reviewer config and a provider sheet naming the gateway's providers with a placeholder credential, so the catalog lists those providers' models whatever the caller's `~/.omp` holds, and no real gateway token reaches the listing. Theseus keeps no model table, and no policy key sets a capacity.

Capacity is never assumed. A reviewer whose primary model the catalog does not list has an unknown capacity, and so does every reviewer when the catalog cannot be read; the plan records the reason as `capacity_error` and, for a sized change, such a reviewer is skipped with `skipped: this reviewer's capacity cannot be determined: <reason>`. When no enabled reviewer can be selected for that reason the result is a plain pending with those reasons (hosted and local alike): no attempt is spent, and unlike a too-large change it is retried by the sweep once the retry backoff has elapsed (the pending cause is `all-absent`), because a later catalog read can succeed. The omp candidate check asserts, against the candidate build, that the same listing names every model the reviewer config and its fallback chains use (contract row 13), so a build that stops listing a configured model fails the check before it is pinned.

### Overhead calibration

The overhead is calibrated from the archived review captures (881 attempts with usage data, from the hosted runs still inside the artifact retention window and the local reviews). For each attempt the peak context is the largest `input + cacheRead + cacheWrite + output` over its assistant turns. The brief is the first user message; its mode is inline when it carries the `Unified diff of these files from base to head` block, promotion when it names `.review-promotion`, and file-list otherwise. The observed overhead of an attempt is the peak minus the brief's tokens minus the required reading. For inline briefs the required reading is zero, because the load does not count it either, so everything the reviewer reads or writes beyond the brief is overhead. For file-list briefs it is estimated from the changed-line counts in the brief's file listing at 38.5 bytes per changed line plus 1074 bytes per file (a least-squares fit of the inline attempts' diff sizes against their listings), and for promotions it is the tool output of the reads under `.review-promotion` and of the staged diff. The returned read spans that newer captures record are not used for file-list briefs: they total every read of the staged diff, re-reads included, while the load counts the required diff blocks once, so subtracting them would understate the overhead (the file-list p95 would fall to about 130000). Tool output and the brief are converted at the same 2.27 bytes per token the load uses, so the figures are consistent with the plan.

| mode | n | p50 | p90 | p95 | margin | overhead |
| --- | --- | --- | --- | --- | --- | --- |
| inline | 641 | 29152 | 57871 | 71358 | 10% of p95, rounded up to 5000 | 80000 |
| file-list | 234 | 69128 | 131529 | 146228 | same | 165000 |
| promotion | 6 | -30907 | 10378 | 12981 | same, and not below the file-list overhead because the sample is small | 165000 |

The overhead is the p95 plus the margin. A promotion's p95 alone would be 15000, but 6 attempts is too few to rely on, so it takes the file-list value. A negative p50 means the reads were over-counted by the byte-to-token ratio, which only makes the load more cautious.

With the pinned omp's catalog (`@smol` 144000, `@default`, `@review`, `@plan` and `@slow` 872000) BenchBox's policy can select these reviewers. A typical inline pull request (median brief 9605 tokens) loads about 90000 tokens, and a typical file-list pull request (median brief 5648 and about 43000 tokens of required diff) about 214000.

| reviewer | model | capacity | typical inline (about 90000) | typical file-list (about 214000) | largest inline brief | largest file-list required reading plus brief |
| --- | --- | --- | --- | --- | --- | --- |
| trivial | `@smol` | 144000 | selectable | not selectable | 64000 | none |
| small | `@smol` | 144000 | selectable | not selectable | 64000 | none |
| medium | `@default` | 872000 | selectable | selectable | 792000 | 707000 |
| large | `@review` | 872000 | selectable | selectable | 792000 | 707000 |
| huge | `@plan` | 872000 | selectable | selectable | 792000 | 707000 |
| max | `@slow` | 872000 | selectable | selectable | 792000 | 707000 |

A reviewer whose capacity is below the load is skipped (`skipped: the change needs about N tokens of reading; this reviewer's capacity is M`). When no enabled reviewer fits, the result is pending with a split instruction, no attempt is spent, and the head is not retried until a new push. A reviewer that fails with a context overflow is not retried; the chain moves on.

`theseus preflight --policy PATH [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the local change without running a reviewer and prints `{"schema": 1, "reviewable": bool, "reasons": [...], "advisories": [...]}`. Exit `0` means reviewable, `3` not reviewable (a Theseus promotion in the change, or a load above every reviewer's capacity), `2` the answer could not be determined (the diff cannot be built, the omp pin cannot be installed, the model catalog cannot be read, or no reviewer can be selected because the capacity of every reviewer that is not too large is unknown). Advisories name reviewers that cannot hold the change, or whose capacity is unknown, when others can.

## Review depth

A verdict that clears a change, SHIP or SHIP WITH FIXES, is accepted only when the attempt saw every changed hunk of every required file.

- A hunk is seen when its whole block was returned by a read of the staged diff (`.review-pull-request.diff`, or `.review-promotion/upstream.diff` for upstream files), or, for a hunk that only adds lines, when the lines it adds were returned by a read of the file itself. Only the lines a read actually returned count: omp returns only the first 300 lines to a read with no line range (and about 300 to an open-ended one), so a longer file or diff takes ranges. Greps, reads of several paths in one call, URL and archive reads, and summarized reads count for nothing.
- Lines are the units git and omp both count: a line ends at `\n` only. A lone `\r` does not start a new line in omp's numbering (a read of a file with lone `\r` and `\r\n` separators reports the `\n`-delimited lines), so staged diffs are read as bytes and split on `\n` alone, and a `\r` planted in a diff cannot shift a later hunk's span. The `read-lines` canary pins this against the omp pin.
- omp cuts a line longer than 768 columns in a read and reports the cut lines in `details.meta.limits.columnTruncated.lines` (`first` and `last`, absolute line numbers); those lines, and the span between them, are not counted as returned, and a read whose cut lines are not reported as numbers counts for nothing. A `:raw` read is not cut and counts in full, so a hunk with a very long line is seen by reading the diff block with `:raw`. omp counts columns in UTF-16 code units (an astral character is two), and a diff line is counted with its `+`, `-` or space prefix. Because a plain read drops every line from the first cut line to the last, the retry hint marks a hunk `:raw` when its block holds a line over 768 columns or lies between two such lines of the same diff, and gives the span to paste, for example `(read with :raw as .review-pull-request.diff:raw:120-180)`; hunks outside that stretch keep their plain `lines a-b` spans. The `read-lines` canary pins the raw read's line numbers.
- A text file that git reports only as `Binary files differ` needs its whole file read; a binary file needs one successful read; a byte-identical rename or a mode change has no hunk and needs none.
- Inline briefs hold every changed hunk already; the plan asserts that each required file appears in the diff it inlines and falls back to a file-list brief otherwise.
- A retry names the diff line spans of the hunks the earlier attempt left unseen, not of every hunk of the files it missed: the attempt artifact records the unseen spans (`absence.unseen`, up to 200 `[path, source, start, end]` entries), and the retry brief marks each unread required file with `(unseen: <diff> lines a-b, c)`, with `:raw` ranges where a line over 768 columns needs them, `read the whole file` for a whole-file read, and `and N more` past six spans. This is a reading aid only: the rule above still counts every changed hunk.
- An attempt whose omp stream shows a compaction (`auto_compaction_start`) is refused as incomplete, and the review overlay turns `compaction.enabled` and `compaction.midTurnEnabled` off.
- Records of an accepted clearing round carry `coverage: 1`. A SHIP or SHIP WITH FIXES recorded without it (made before this rule) is never carried to another head or the same patch; the next round is a full first round, and a follow-up after it takes full scope.
- Withdrawals and disputed fixes must cite a line inside the lines a read returned.
- The pull request diff is always built from git in the trusted prepare step (`git fetch --filter=blob:none` of the merge base and head, then `git diff -M`), and its path set must equal the pull request's file list; a diff that cannot be built holds the plan. The GitHub diff API is not used.
- A Theseus promotion's required reads are limited to what the consumer executes: the package source the wheel carries, `pyproject.toml`, and the reusable workflows the consumer's callers reference (followed through nested job-level `uses:`). Theseus supports only job-level reusable workflows plus its Python package. The new surface is every reference at the base and the head, so a change can only widen it; the old surface comes from the base workflows and the old tree alone. Every workflow in the new surface and not the old one is read whole from the staged new tree, changed or not, so a Theseus workflow the head newly references is never read by its hunks. A job-level reference to the Theseus repository holds the plan unless it is pinned to the full 40-hex commit the lock promotes (the base lock's commit for base workflows; a branch, tag or `refs/...` runs a ref the promoted tree does not prove), names a workflow file that exists in the tree, and takes a recognised form; owner and repository match case-insensitively, a `.git` suffix on the repository name is the same repository, and a reference that mentions the repository but does not parse holds. A step-level `uses:` of any action in the Theseus repository holds the plan whatever its kind (the action's files, the commands it runs and what they reach cannot be bounded), as does a step-level `./` reference inside a Theseus workflow (it resolves against `$GITHUB_WORKSPACE` at run time, not against the promoted Theseus commit). Job-level `./.github/workflows/x.yml` references inside Theseus workflows are followed. Actions from other repositories and `docker://` images are ignored. Renames are detected, and a byte-identical move needs no read. The lock's `delta_sha256` is unchanged by this.

Captures are schema 2: they add a `reads` ledger (path, returned line spans, truncated, elided) with no content. `theseus audit-coverage --capture FILE --policy PATH [--repo-root DIR] [--repo OWNER/NAME] [--base-ref REF] [--trusted-ref REF] [--head REF] [--json]` plans the change locally, stages its diffs, and applies the rule to a capture's reads (exact for schema 2; reconstructed favorably from the requested selectors for schema 1, whose results are withheld). A promotion's plan inputs resolve through `REVIEW_PROMOTION_SOURCES`, as for a local review. Exit `0` means every hunk was seen and nothing compacted, `3` not covered, `2` undetermined.

Attempt artifacts are kept 30 days, like captures.

## Round discipline

Without `protocol.max_finding_rounds` a pull request can take any number of follow-up rounds. With it, the round count is derived from the app's own unedited review records: a round with findings is a `first` or `follow-up` record of the current cycle with open defects, forced dispute re-reviews included, counted back from the latest record. A record with no open defects (or a new cycle) resets the count.

- **Step-back.** When the count reaches the cap and the patch changes again, the next round is one full-scope re-review. It keeps every finding id, carries, disputes and the coverage rule exactly as any follow-up does, so no open defect can be dropped. The brief says the pull request has had N rounds with findings, lists the earlier defects (data), and asks the reviewer to look for every remaining instance of each class and report each location as its own defect. The verdict body says `Step back: ...` and the record carries `step_back: 1`. The step-back happens once per run of finding rounds; later rounds are ordinary again.
- **Delta first.** An ordinary follow-up stages `.review-since-last-review.diff`, the diff from the previously reviewed head to this head (built in the trusted plan step by `git diff -M`, never from pull request content; skipped when it cannot be built, is empty or over the brief bound). The brief tells the reviewer to read it first. It is a reading instruction only: required reads, hunk coverage and defect scoping count exactly what they counted before.
- **Class-level findings.** The templates (v3) ask for one defect entry per location of a recurring class (each title is unique, naming the class and the specific symbol or location; details may cross-reference the others), and never the same location again under a new title. Every location is therefore a separate finding in the record, the earlier-defects listing and the defect limit.
- **Dispute dispositions.** A reviewer that answers a dispute re-review of an unchanged patch with a verified `fixed` makes the attempt invalid, regardless of its decision or how many attempts remain. It must withdraw the finding while conceding the rebuttal or consistently maintain it. Exhaustion does not rewrite its evidence into a maintained disposition or count the invalid answer toward escalation.

## Minimising superseded reviews

```yaml
minimize_superseded: true   # boolean, default false; any other value is refused
```

With the key off (the default) nothing changes. With it on, after Theseus posts a verdict review that carries a protocol record, it collapses the App's earlier record-bearing reviews on that pull request as outdated (GraphQL `minimizeComment`, classifier `OUTDATED`), so a long pull request shows one open verdict and one-line stubs. A reader can expand any of them, and GitHub can reverse the collapse.

- Only the App's own reviews are touched, and only those that carry a protocol record and were followed by a newer record-bearing review on the current head. "Current" is checked twice: the newest record-bearing review must be on the head this run reviewed and on the pull request's live head, which is read just before minimising. A stale run (a newer push has landed) and a failed head read minimise nothing. These stay expanded: the newest verdict; "not reviewed" posts (they carry no record); human reviews; attester comments; every inline thread, so open findings and disputes stay visible; and any earlier review that lists a defect still open in the newest record that has no review thread, because that review body is the only place with its detail and the dispute instruction.
- It is presentation only. A minimised review is not an edit: GitHub sets neither `lastEditedAt` nor `includesCreatedEdit`, the review keeps its body and protocol record, and history, finding deduplication, the receipt check and the edit detectors read it exactly as before. It never causes a "review was edited" hold.
- It fails open. It runs last in the publish step, after the verdict, the status, the gate and the retry-state output are done, and is bounded in time (20 seconds per request, 90 seconds for the whole step; a stalled request is abandoned). A refused or failed request (including a permission error for the App installation token) is logged as a workflow warning and changes nothing else: the verdict, statuses, gate result, record and exit code are identical to a run with the key off.
- It needs no new App permission in the code path: the request uses the same App installation token that posts the review. That GitHub accepts a review `minimizeComment` under that token is not stated in GitHub's documentation (the REST permission tables do not cover GraphQL mutations), so enable the key for a consumer only after the first real run there has been checked: the call succeeds, the minimised review still shows `lastEditedAt: null` and `includesCreatedEdit: false`, and no unwanted workflow run follows. GitHub's webhook documentation lists no event for minimising (`pull_request_review` has `submitted`, `edited` and `dismissed`; `issue_comment` has `created`, `edited` and `deleted`), so the gate and request workflows' triggers should not fire. If the token lacks permission, the request fails open and the key does nothing until the consumer's owner decides whether to grant it.

## Disputing a finding

Every finding on a line inside the diff gets a review thread carrying the finding marker, including a finding carried into a round from an author-supplied local record, which previously had none. A finding that cannot be anchored (its line is outside the diff) has no thread, and the review body says so. Two disputes reach the same re-judgement and escalation:

- a reply starting with `Dispute:` on the finding's thread (from an owner, member or collaborator), as before;
- a pull request comment `Dispute: D<n> - <reason>` from a login in `consumer.attesters`. The prefix is matched case-insensitively; the rest of the grammar is exact (`D<n>`, one space, `- `, a non-empty reason). The comment must be unedited, from a user account, posted after the review that first listed that finding in the current cycle, and name a finding that is still open. The reason is data, shown to the reviewer like a thread reply, which must restate it in `claim` and answer it.

A re-judgement runs on `/review` or when the sweep finds an unanswered dispute. Two dispute re-reviews of one patch that leave the finding open escalate it, after which only an attester's `Review adjudication: WITHDRAW|MAINTAIN D<n> - <reason>` settles it.

## Pre-existing findings

The findings schema has an optional `origin` (`introduced`, the default, or `pre-existing`). The reviewer sets it; Theseus cannot prove from the diff that a defect existed at the base commit, because a new caller, input or setting can activate unchanged code. With `protocol.pre_existing: block` (the default) every finding blocks and counts toward the defect limit, whatever its origin.

With `protocol.pre_existing: known` a finding is logged as a known defect and does not block when all of these hold:

- the reviewer flagged it `pre-existing` and did not rate it Critical (a Critical finding always blocks);
- its title or detail cites at least one location in plain text (`path:12`, `path#L12`, `path lines 12-14`, `path:12, 40`, an existing repository path, a path suffix or bare file name of a changed file, or a bare `line 12`, `L12`, `#L12` read against the finding's own file); an uncited finding blocks;
- the change touches none of the cited files, the finding's own file included, and the finding's file is a regular file in the head tree that the change does not add, rename, copy or alter as binary;
- no file the change touches can reach the cited code. Theseus scans the full post-change content of every other changed file (at most 300 files of 256 KiB each; more or larger blocks) for each cited module name, dotted module path, cited file name, function or class the cited files define (every definition; a cited file over 256 KiB blocks), and each symbol the finding names in backticks or as `name()`; a match anywhere in the file is a trigger, and so is a changed file whose module name or file name appears in a cited file (the cited code may depend on it), so a new call through an existing import blocks. A changed file whose extension is not a source or documentation extension (data, fixtures, binaries, dependency manifests, config) anywhere in the repository, any change under `.github/`, a build or test config file (Makefile, pyproject.toml, conftest.py and similar), or a changed path missing from the head tree is treated as a possible trigger. Cited and changed paths are compared after resolving symlinks;
- the review listed every finding it found, is not DO NOT SHIP, is not a promotion review, and at most 5 findings qualify (more than 5 and all of them block);
- the finding contains no URL or redaction, and any markdown link is reduced to its text before the check, so citations must be plain text.

The trigger scan is a bounded text search, not a call graph: it follows callers and dependencies through up to two rounds of modules, seeded with the cited files' module names only (the definitions they contain are matched in the changed files directly), found in the head tree (a changed file counts when the cited code, a caller of it, or anything they reach within two rounds refers to it by module name) (at most 20000 source files of 4 MiB each in the head tree, symlinks and special files skipped; more or larger blocks), only the last segment of a dotted module name is followed, and the walks follow import statements only (`import`, `from`, `use`, `require(`, `#include` and similar lines), so a reference built from strings at run time (plugin registries, entry points, configuration naming a module) is not seen, while `importlib.import_module(`, `__import__(` and `mod x;` lines count as imports; package `__init__` names are not followed, and the scan can still over-block on an uncommon name shared with unrelated code. Base-branch reproduction of the defect is not implemented. Known defects are not counted toward the defect limit, get no review thread, and appear under "Pre-existing, not blocking" in the review body and in the `advisory` list of the review record. They are decided again whenever a review covers their file, and Theseus does not carry them from one review record to the next.

`theseus finalize` also writes `known-defects.json` next to `status.json` when a review logged known defects: one entry per defect with a stable `kd-` id (a hash of the file and normalised title), repository, pull request, head, severity, location, title and detail. A trusted job on the gated branch can feed that file, or the review record, to a durable known-defect store; Theseus itself writes no store. `advisory` is no longer a valid `pre_existing` value and is a policy error.

## Reviewer guidance

`consumer.review_guidance` names a repository-relative text file of reviewer guidance: the domain checks this repository wants on top of the generic template rules. Theseus reads it from the trusted checkout of the gated base branch (a local review reads it from the base ref), never from the pull request head, so a change that edits the file does not alter its own review; the edit takes effect once it merges. The file is trusted owner text and appears in the brief under `CONSUMER GUIDANCE`, outside the fenced data sections. It cannot override the template rules, the decision rules or the response contract.

The file must be a non-empty UTF-8 regular file (not a symlink) of at most 16384 bytes. A missing, empty, oversized or non-UTF-8 file makes the plan fail (exit 2) rather than review without it, in a local review too, which also refuses a base-branch symlink; the gate check fails the same way, so a bad file that merged to the base blocks every pull request until it is fixed. Keep the file in a directory the trusted checkout includes (`trusted_paths` defaults to `.github`), and list it in the classifier `gate_paths` so a change to it is reviewed as review machinery. Omit the key for no guidance; the brief then shows `none`.

## Author statement

With `brief.include_intent: true` the brief opens its fenced change data with the pull request title, the first 2000 characters of the description (review and local-record markers and HTML comments removed) and up to three same-repository issues the description closes, fixes, resolves or refs (title and 1000 characters of text each). A local review uses the branch's commit messages instead. All of it is untrusted data: sanitized (links, mentions and credentials removed), inside the existing fences, and never part of a template slot; the template only says to judge whether the change does what the statement says.

## Reading review output

Tooling that reads Theseus reviews or threads must use the markers, not the prose; the one verdict line still read from prose is the `### review-verdict:` heading until the v2 marker is written. The grammar and the reading rules are in [review-markers.md](review-markers.md).
