# Hosted workflows

Theseus ships two reusable workflows, `.github/workflows/review.yml` and `.github/workflows/gate.yml`. A consumer keeps thin caller workflows that own the triggers, the concurrency groups and the check names, and calls these at a pinned commit. Copy-ready callers are in [`examples/consumer/.github/workflows/`](../examples/consumer/.github/workflows).

| Caller file (consumer) | Workflow name | Calls | Check it produces |
|---|---|---|---|
| `review-verdict.yml` | `review-verdict` | `review.yml` | `review-verdict` status, reviews and comments from the App |
| `review-gate.yml` | `review-gate` | `gate.yml` | `review-gate` status from the App |
| `review-request.yml` | `review-request` | none (a no-op job) | its completion is the bridge trigger |

The `review-verdict` and `review-gate` contexts, the caller workflow names and file names, and the `review-verdict-*` artifact names are protocol identities. The reviewer finds retry state by the caller workflow path `.github/workflows/review-verdict.yml` and the gate dispatch targets `review-gate.yml`, so the caller files keep those names. This source tree posts only these contexts; the compatibility contexts used through 0.2.6 are never written again, so a ruleset that still requires one must be changed before installing this tree (see "Migrating from 0.1.x").

## Caller context

A called workflow sees the caller's `github` context: `github.event_name`, `github.event.*`, `github.sha`, `github.ref`, `github.repository`, `github.workflow_ref` (the caller's), `github.token`. `workflow_dispatch` inputs are read as `github.event.inputs.*`; `inputs.*` inside a callee is only its own `workflow_call` inputs. Consequences:

- Triggers, `run-name`, `concurrency` and the job-level `permissions` ceiling belong to the caller. The callees declare no workflow-level `concurrency` and no `run-name`, so a callee can never share a group with its caller. The example callers use groups prefixed `review-verdict-` and `review-gate-`; the two never collide with each other or with CI groups.
- `github.sha` is the tip of the branch the event ran against. The guard resolves the ref the workflow runs from (`github.ref`: the pull request's base for `pull_request_target`, the dispatched branch for `workflow_dispatch`, the default branch for `schedule`, `push` and `issue_comment`) against `gated_base`, and every later job runs only when it is gated; a pull request against any other base skips the jobs. A `workflow_dispatch` from a ref that is not gated is refused by the guard job.
- The guard job (`contents: read` only, no secrets, no environment) refuses before any secret job starts: a `theseus_ref` that is not a full 40-hex commit, an empty or unsafe `gated_base`, a caller whose `github.workflow_ref` is not `<repository>/.github/workflows/<caller_workflow>@...`, and a `theseus_ref` that is not the pin recorded in the trusted consumer lock (below).

## Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `environment` | yes | | Name of the caller environment whose secrets the secret-bearing jobs read |
| `theseus_ref` | yes | | Full commit SHA of the Theseus repository that holds the callee and its bootstrap installer; must equal the SHA on the caller's `uses:` line |
| `theseus_repository` | no | `joeharris76/theseus` | Repository checked out at `theseus_ref` |
| `gated_base` | yes | | Branches the review and gate apply to: a newline- or comma-separated list of branch names and prefix patterns (see "Several gated bases"). The tip of the pull request's own base supplies the trusted policy and lock |
| `policy_path` | no | `.github/review.yml` | Consumer policy, read from the trusted checkout |
| `trusted_paths` | no | `.github` | Sparse-checkout patterns for the trusted checkout; add the directories of any `consumer` manifest outside `.github`. The example gate caller reruns on every push to the gated branch (no `paths:` filter), so a manifest change outside `.github` re-evaluates standing gate statuses immediately rather than at the next hourly sweep |
| `caller_workflow` | no | `review-verdict.yml` / `review-gate.yml` | File name of the caller; the guard compares it with `github.workflow_ref`, and the bridge dispatches it |
| `tailscale_tag` | yes (`review.yml`) | | Tailscale tag the review job's runner joins the tailnet under to reach the credential gateway; the guard refuses a value that does not match `^tag:[a-z0-9-]+$` |
| `review_workflow_name` | no (`gate.yml`) | `review-request` | `name:` of the workflow whose completion bridges into the gate |

## Pinned workflow ref

Pin both the `uses:` line and `theseus_ref` to the same full commit SHA:

```yaml
uses: joeharris76/theseus/.github/workflows/review.yml@<40-hex commit>
with:
  theseus_ref: <the same 40-hex commit>
```

A branch or tag would let the workflow change under the consumer, so every reference is a commit SHA. The callee cannot read its own ref from the ambient context: `github.workflow_sha` and `github.workflow_ref` describe the caller, and `job_workflow_sha` is a claim inside the OIDC token, not a context value. The callee therefore takes the pin as an explicit input and checks out only `src/theseus_reviewer/bootstrap.py` from that commit. A caller that passes a different `theseus_ref` from its `uses:` ref would run a bootstrap installer from one commit under a workflow from another. A consumer that wants that comparison enforced adds a separate no-secret job to its caller that requests an OIDC token, reads the `job_workflow_sha` claim and compares it with the input; the review job never gets `id-token: write` and the isolation phase refuses a job that holds the OIDC request token.

The organisation Actions allow-list must permit `joeharris76/theseus/.github/workflows/*@<sha>` and the four pinned actions the callees use (`actions/checkout`, `actions/create-github-app-token`, `actions/upload-artifact`, `tailscale/github-action`).

The `prepare` step builds the pull request diff with git and needs git 2.40 or newer on the runner. On an older or unparsable git the plan holds with "the pull request diff could not be built" followed by the required version.

## Several gated bases

`gated_base` (the workflow input, and `consumer.gated_base` in the policy, which may be a single string or a list) names every base the review and gate cover, for example `develop` plus `release/*` for maintenance branches. Rules:

- Entries are literal branch names or a literal prefix followed by a final `*` (one path segment) or `**` (any depth), such as `release/*`. A bare `*`, a wildcard anywhere but the last segment, a duplicate or an empty list is refused. The first entry must be a literal branch name: it is the branch local review and the omp watcher default to.
- The trusted policy, lock and gate pin come from the tip of the pull request's own base. A run on `release/0.5.x` therefore reads `release/0.5.x`'s `review.yml` and `theseus-lock.json`; `consumer`, `attesters` and the soundness manifest are never taken from another gated branch. `REVIEW_TRUSTED_BASE` carries that base into every Python phase, and `theseus gate` and `theseus drive plan` refuse a pull request whose base differs from it.
- A pull request into an unlisted base is refused: the jobs are skipped by the guard, and `theseus gate` reports `UngatedBase` without posting.
- Events that GitHub runs only from the default branch (`schedule`, `push`, `workflow_run`, `issue_comment`) cover the other bases by dispatching `caller_workflow` on the pull request's own base branch: the `bridge` job and the hourly `sweep` of `gate.yml` do this for every open gated pull request, and `theseus drive sweep` does it for retries. Each dispatched run then uses that branch's own caller workflow, pin and lock, so a maintenance branch keeps working on its own pin until it is promoted.
- `review.yml` comment commands (`/review`, review adjudications) are evaluated by the workflow of the default branch against the default branch's policy and so apply only to its own pull requests; a review of a pull request into another gated base is started by dispatching the caller from that branch. Gate-relevant comments (stand-in attestations, adjudications, omp checks, and their edits and deletions) on a pull request into another gated base are handed to that base: the `gate` job dispatches `caller_workflow` on the pull request's own base, so a revoked attestation is re-evaluated at once.
- `review-gate` is one commit status per head. Every gate, including a single-base consumer, checks for open pull requests sharing that head into any different base and posts an error instead of letting a pass earned under one base's policy count for another. The other base need not appear in the local policy; its policy and pin can differ. Close one of the pull requests or use distinct heads to clear the collision. Lookup failures also block. Deploy this guard to every branch that can produce the shared status before enabling multi-base gating: an older producer can still overwrite a newer producer's error, and a newer release cannot repair an already-deployed older binary.
- Maintenance branches carry their own copy of the callers: list the branches in the caller's `pull_request_target` and `push` triggers if their workflow files should react, and allow those branches to deploy to the environment named by `environment`. This does widen who can reach the App secrets. `pull_request_target` and `workflow_dispatch` run the caller from the base branch, so whoever can write a branch that matches a pattern in `gated_base` (and the environment's deployment rule) controls a workflow that can read `REVIEW_APP_ID` and `REVIEW_APP_PRIVATE_KEY` and mint a `statuses: write` token, which can post `review-gate` success on any head, including heads of pull requests into other bases. List a pattern only when creating, pushing to and deleting every branch it can match is limited to the owner (a branch ruleset on the pattern that restricts creation and requires review), and give the environment the same deployment rule as `gated_base` and no wider; the matcher accepts only a literal prefix and a final wildcard so a pattern cannot match unrelated branches, but it cannot protect the branches it does match.

## Trust chain: two pins, two processes

Neither workflow installs `latest` or reads a lock from a pull request head.

### The pin must equal the trusted lock

A pull request can edit the caller files, so the pin has two halves and the callee can defend only one.

**What the guard enforces at runtime.** The guard job binds `theseus_ref` to the reviewed lock. It sparse-checks-out `.github/theseus-lock.json` from the trusted ref (`review.yml`: `github.sha`, the base tip for a pull request event; `gate.yml`: the current tip of the ref the workflow runs from, which is the pull request's own base), reads it from that commit's object into a temporary file, and refuses with exit 2 unless `theseus_ref == workflow_sha == source_commit`. A missing, unparsable or incomplete lock is refused too. A lock on the pull request head is never read. The guard outputs the commit it read as `trusted_commit`, and the `gate` and `sweep` jobs of `gate.yml` check out exactly that commit. When the ref is not a gated base the guard still succeeds, reads no lock and outputs `gated=false`, so no other job starts; `gate.yml` and `review.yml` behave the same. Every other job has `needs: guard`, so none starts when the check fails. The lock location is fixed at `.github/theseus-lock.json`.

**What the guard cannot enforce.** A workflow cannot see or verify the `uses: ...@<sha>` that called it, and `github.workflow_sha` describes the caller, not the callee. A pull request that changes `uses:@<sha>` to an unreviewed Theseus commit while leaving `theseus_ref` and the lock alone passes the guard, and GitHub runs the unreviewed workflow code. The consumer must close that gap itself:

1. Run a required CI check on the pull request head that asserts `uses:@sha == theseus_ref == lock.workflow_sha` in every caller file. For example, as a test:

   ```python
   import json, re
   from pathlib import Path


   def test_theseus_pin_is_one_commit():
       lock = json.loads(Path(".github/theseus-lock.json").read_text())["workflow_sha"]
       for name in ("review-verdict.yml", "review-gate.yml"):
           text = Path(".github/workflows", name).read_text()
           uses = re.search(r"joeharris76/theseus/\.github/workflows/\S+@([0-9a-f]{40})", text).group(1)
           ref = re.search(r"theseus_ref:\s*([0-9a-f]{40})", text).group(1)
           assert uses == ref == lock
   ```

2. List the caller files (`.github/workflows/review-verdict.yml`, `.github/workflows/review-gate.yml`, `.github/workflows/review-request.yml`), `.github/theseus-lock.json` and the file that holds the check in the consumer's soundness manifest (`consumer.soundness_manifest`), so changing any of them requires the full review and gate.

The runtime guard and the CI check together give the binding. Neither alone does.


1. **Reviewer pin A.** The review checks out the trusted copy of the gated branch at `github.sha` (sparse, `persist-credentials: false`) and runs the bootstrap installer from `theseus_ref` against it: `bootstrap.py install --repo trusted --commit "$github.sha"`. The installer reads `.github/theseus-lock.json` from that commit's object, verifies every wheel hash, refuses anything it cannot verify, installs into an isolated tree and prints the launcher. The launcher directory goes on `PATH`, so every later step runs `theseus drive --policy "$POLICY_PATH" <phase>` from that install. The pull request head is checked out later, into a separate directory, for reading only.
2. **Gate pin B.** After the reviewer has exited and the result is finalised, the workflow checks out the *current* gated branch into `gate/`, resolves its tip with `git rev-parse HEAD` and installs that commit's lock into a separate cache. Its launcher path is passed to the publish step only, as `THESEUS_GATE_COMMAND`, with `THESEUS_GATE_ROOT` naming the `gate/` checkout. The reviewer posts the verdict, then runs `<pin B launcher> gate --repo ... --pr ... --head-sha ... --policy ... --root gate/` as a subprocess. The gate decision is therefore computed by pin B's code against pin B's policy and manifests. Pin A never imports pin B and pin B never imports pin A; they share no interpreter.

If the gate pin cannot be resolved (no lock at the tip, a hash mismatch, a blocked source) the step is allowed to fail so the verdict is still published, `THESEUS_GATE_COMMAND` is then empty, and `theseus` refuses an empty value instead of falling back to its own interpreter.

The `gate.yml` workflow has a single pin: the gate runs from the lock at the tip of the pull request's own base, read at the time the job starts. It never checks out candidate code.

## What to require, and what the job exit means

Branch protection and rulesets MUST require the App-posted commit status context (`review-gate`), never the `gate / gate` check-run. The check-run is the workflow job's own result: it reports whether the job ran without error, not whether the review passed, and a refresh run can leave a stale check-run on a different SHA. The App-posted status is the only authoritative verdict; it stays `failure` while a review is pending and turns `success` only on a pass.

Status descriptions are at most 140 characters, lead with the decision or state and name no tier. Programs that read them match only these tokens, defined once in `theseus_reviewer.status_text`: a trailing ` [run <id>]`, ` (reposted)` before it, `SHIP WITH FIXES`, `escalated D1,D2`, `only exempt paths changed`, the prefixes `refused after`, `a review was edited` and `review-verdict failed twice without a result at: `, ` · degraded: <providers>`, and for a `review-gate` that is waiting, the prefix `Waiting for review-verdict` (the head appears only inside the quoted stand-in line `Stand-in review: APPROVE <40-hex sha>`). Anything else is prose, and a reader that cannot classify a description treats it as not passing.

`theseus gate` exits with:

| Exit | Meaning | `gate / gate` job |
|---|---|---|
| 0 | Posted `success`. | Green. |
| 3 | Posted, waiting: the status was posted as not-yet-passing (`failure`) because the review is pending or blocking. | Green; the step summary says `posted, waiting`. A green job here is not a pass. |
| 2 | Error: the gate could not decide or could not post. | Red. |
| any other non-zero | Crash or spawn failure. | Red. |

The required `review-gate` context MUST be restricted to the review App as its source (ruleset `integration_id` / "expected source"). Without that, any workflow holding `statuses: write` can post a same-named `success` and satisfy the requirement.

The job treats exit 3 as waiting only when the gate's result file (`REVIEW_GATE_RESULT_FILE`) also records 3, exactly as the sweep does; an unreported 3, or a failed summary write, fails the job.

The status the App posts does not depend on the job result: a waiting gate still posts `failure` on the required context. The hourly sweep applies the same rule (exit 0 and 3 are accepted).

Concurrency stays with the caller. The example `review-gate.yml` still cancels an in-progress gate when a newer event arrives for the same pull request, because that guarantees the newest head's gate is the one that runs and posts; a cancelled run is grey, not red, and never posts a status.

## Failure and retry

`theseus_reviewer.github.post_gate` runs the gate subprocess and raises `GitHubError` for a spawn failure, any exit other than 0 (posted) or 3 (posted, waiting), and a `THESEUS_GATE_COMMAND` that is set but empty. `drive publish` then:

- keeps the verdict it already posted and the retry-state artifact (`state_recorded`),
- prints `::warning::review-gate was not posted from this run: <reason>` and sets the step output `gate_failed=true`,
- lets the later steps run: the state and attempt uploads, and the dispatch of the consumer's `review-gate.yml` for the reviewed head, which resolves a fresh pin B and retries (the hourly gate sweep is the backstop),
- ends the run with a failing step, so a failed gate post is never a green run.

A failing subprocess posts no status of its own, so no success is published on its behalf.

`theseus reset-backoff --pr N --head SHA --reason TEXT` records an attester's exact-head reset after an owner instruction. It permits one review through the existing backoff, not a reroll of a decisive verdict. A successful guard-only comment run or a dispatch refused by prepare does not consume it. Trusted attempt/state artifacts or a started attempt-loop step consume it even if the review later fails, times out or is cancelled. Missing or unreadable evidence holds the reset rather than granting another attempt.

When no artifact exists, the Jobs API must prove the trusted `review` job was skipped or all five attempt-loop steps were skipped. Renaming that job, `Run the attempt loop`, or `Resume the attempt loop after attempt N` requires updating `github.run_review_started`; unrecognized or incomplete evidence fails closed.

## Provider loss and the health job

The attempt phase probes the gateway's served models before choosing a reviewer, so a provider that is gone changes the reviewer's model, never the outcome of the run; see [Provider loss](consumer-policy.md#provider-loss) for the selection rules.

- A review that ran on a substitute, or while a routed provider is unserved or excluded, ends its status description with `· degraded: <providers>` and lists the provider availability in the body's collapsed `Run details` block.
- A review that cannot run for availability alone ends pending with cause `unavailable`: no attempt or daily retry is spent, the sweep retries it after the flat `retry.backoff_start_minutes` (at most about 24 a day), and `review-gate` stays pending. Those dispatches are exempt from the sweep's daily dispatch cap, and the state records each one so a later `all-absent` retry still has its full budget after the outage.
- The `health` job runs on `schedule` only (the caller's existing hourly cron) and needs no permission beyond `contents: read`. It exits 1, and the scheduled run goes red, when the gateway is unreachable, the client token is rejected, or a provider named in the trusted omp config is not served. Push, pull request and dispatch events skip it. It reads the same `REVIEW_GATEWAY_URL` and `REVIEW_GATEWAY_TOKEN` secrets as the attempt steps. The CI health job cannot see the host's owner denylist, so a provider the owner removed on purpose still shows the scheduled run red until the trusted config no longer names it; that is the expected CI-side behaviour for a deliberate removal, and the host monitor is what reports it as a notice rather than an alert.

## The not-reviewed notice

When no reviewer produces a verdict, the pending review (heading `### review-verdict: pending for <sha>`) lists each absence under the bold title `**Reviewers not available**` as a short reason: provider quota, gateway unreachable, provider error, timeout, an answer that was not a valid review, a reviewer that did not read every changed hunk, a change too large to read, or run artifacts that failed validation. Provider text never reaches GitHub; it stays in the run log and the uploaded attempt artifacts. The same reasons fill the `Reviewers not available` and `Provider availability` sections of verdict bodies. A `Next step:` line follows by cause: transient causes say the hourly sweep retries and a maintainer can comment `/review`; a change too large says to split the pull request or ask an owner for a stand-in review; withheld artifacts and a reviewer that never reported say to push a new head.

The notice ends with a hidden marker, `<!-- review-unavailable: v1 <40-hex head sha> <cause-key> -->`. The cause key is the sorted set of cause keys joined by `+` (`quota`, `provider-auth`, `timeout`, `invalid-answer`, `empty-answer`, `incomplete-read`, `provider-error`, `run-failed`, `too-large`, `gateway-unreachable`, `gateway-rejected`, `gateway-error`, `not-served`, `capacity-unknown`, `blocked`, `no-report`, `artifacts-invalid`, `reviewer-disabled`, `attempt-limit`, `no-reviewer`). `drive publish` skips the post when the latest post by the review App for the same head carries the same marker; only markers are compared, never prose. A missing, unreadable or differently keyed previous marker, or a listing that cannot be read, posts again. The status and the retry state are written either way.

## Permission ceilings

A callee job can hold at most what the caller's calling job grants. The example callers grant the union of what the callee jobs ask for; each callee job then narrows it.

| Job | Ceiling the caller must grant | Job permissions |
|---|---|---|
| `guard` | `contents: read` | `contents: read`; it checks out only `.github/theseus-lock.json` from the trusted ref |
| `review` (`review.yml`) | `actions: write`, `contents: read`, `pull-requests: read` | the same; `actions: write` lets the retry sweep and the dispatch step run |
| `sweep` (`review.yml`) | same | same |
| `health` (`review.yml`) | `contents: read` | the same; it runs only on `schedule`, probes the gateway and writes nothing |
| `gate` (`gate.yml`) | `contents: read`, `pull-requests: read` | the same |
| `bridge` | `actions: write`, `contents: read`, `pull-requests: read` | the same; it only lists pulls and dispatches the gate |
| `sweep` (`gate.yml`) | `contents: read`, `pull-requests: read` | the same |

Both workflow files declare `permissions: {}` at the top. No job requests `id-token`, `statuses`, or `pull-requests: write` through `github.token`; statuses, reviews and comments are posted with the App token minted inside the job.

## Environment secrets

The secret-bearing jobs (`review`, `sweep`, `gate`) name the caller's environment through the `environment` input and read their secrets from it. Each callee declares the secrets it uses under `on.workflow_call.secrets`, all `required: true`, and every example caller passes each one by name: `secrets: { NAME: ${{ secrets.NAME }} }`. That works even when the secret exists only in the caller's environment (GitHub documentation, "Reuse workflows", updated 2026-10-07).

Do not use `secrets: inherit`. It does not carry secrets to a callee owned by a different account than the caller's organization or enterprise, and it would also expose every repository and organization secret of the caller to the callee. A probe with an organization caller, a user-owned callee and a secret that exists only in the caller's environment showed: passing nothing, or `inherit` to a callee that declares no secrets, leaves the secret empty and the job green; `inherit` to a callee that declares the secret `required: true` fails at startup with `Secret NAME is required, but not provided while calling`; passing the secret by name to a declared callee delivers it. Because the callees declare their secrets as required, both `inherit` and a caller that omits a mapping fail when the workflow starts, and a caller that passes an empty value makes the jobs fail with an `::error::` annotation instead of passing quietly. A test requires every `secrets.NAME` the callees reference to be declared, and every declared secret to be referenced, so an undeclared reference (which would resolve empty) cannot be added.

An environment's branch policy is checked against the default branch for `pull_request_target` runs, so it does not keep pull request runs away from the secrets. What does is the structure above: the callee runs only the trusted base copy, and candidate code is never executed. `review.yml` needs the seven secrets below; `gate.yml` needs only `REVIEW_APP_ID` and `REVIEW_APP_PRIVATE_KEY`.

| Secret | Used by |
|---|---|
| `TYPESAFE_API_KEY` | `prepare`, `recover`, `finalize`, the scans |
| `OPENAI_API_KEY` (optional) | `prepare`, `finalize`, the scans; the classifier fallback only |
| `REVIEW_GATEWAY_TOKEN`, `REVIEW_GATEWAY_URL` | the attempt steps and `finalize` only |
| `TS_OAUTH_CI_CLIENT_ID`, `TS_OAUTH_CI_SECRET` | the tailnet join, the scans, `finalize` |
| `REVIEW_APP_ID`, `REVIEW_APP_PRIVATE_KEY` | the token-mint steps only |

Each step names exactly the secrets it needs in its own `env:`; the job-level `env:` holds only booleans derived from them and the policy path. The App private key never reaches an environment variable. The App token is minted only after `finalize` has run and the reviewer has exited, and is passed only to `publish` and `dispatch`.

Candidate code never meets a credential: the pull request head is checked out without credentials into `workspace/` and read, never built or executed, and the reviewer's process isolation and disk scans run before and after it. Fork pull requests reach the same path through `pull_request_target`, which executes only the trusted base copy. Environment protection on `environment` must not make the secrets reachable to any workflow that builds or runs candidate code.

## The `workflow_run` bridge

The caller `review-gate.yml` triggers on `workflow_run: workflows: [review-request]`. GitHub matches that list against the triggering workflow's `name:`, not its file name. The consumer's `review-request.yml` therefore must keep `name: review-request`. `gate.yml` repeats the check on the event itself: its `bridge` job runs only when `github.event.workflow_run.name` equals `review_workflow_name` and the run belongs to the same repository, then dispatches `caller_workflow` for every open pull request whose head matches and whose base is gated, on that pull request's own base branch. The bridge checks out only the pinned `bases.py` matcher at `theseus_ref`, runs no candidate code and holds no secrets.

## Reading review output

Review bodies and threads carry HTML-comment markers (`review-protocol`, `review-defect`, `review-finding`, and the `review-verdict: v2` marker, which every review body written after 0.2.8 carries in place of the old `### review-verdict:` heading). They are the supported machine interface; the surrounding prose is presentation and may change. The grammar is in [review-markers.md](review-markers.md).

## Local pre-submission review

Hosted workflows do not change the local flow: `theseus local-review --policy PATH` (see [consumer-policy.md](consumer-policy.md)) still runs from the author's trusted checkout and keeps its own cache and state.

## What the offline suite proves

`tests/integration/runs.py` executes `jobs.review.steps` of the shipped `review.yml` against the fake GitHub, fake OMP and fake gateway: every phase step runs the command text from the workflow, with exactly the environment the step names. `tests/integration/test_hosted_pins.py` installs two synthetic locked artifacts (reviewer pin A built from this source, gate pin B with a different gate) through the shipped installer and the shipped install steps, advances the gated branch between the review start and the publish step, and asserts that the posted gate decision comes from pin B in a separate process, that a failing pin B records `gate_failed` and posts no gate status, and that an unresolvable pin B does the same. `tests/unit/test_hosted_workflows.py` pins the structure: SHA-pinned actions, permissions, secret scoping, the guard conditions and the bridge name match.

Not provable offline: environment-secret resolution inside a called workflow, the organisation allow-list, `GITHUB_TOKEN` ceiling enforcement by GitHub, the `workflow_run` delivery itself, the tailnet and gateway, and the App token mint. The offline suite pins the by-name secret mapping (each example caller's `secrets:` equals the callee's declared names, and `inherit` is forbidden) and covers the empty-App refusal step (`test_hosted_workflows.py` runs its script, and `runs.py` executes it as the first step of the review job); the rest is verified at the first live run of each consumer.

## Migrating from 0.1.x

0.2.0 renamed every `oracle` name to `review` and collapsed doubled words. Released versions 0.2.0 through 0.2.6 were transitional: each status was also posted under its 0.1.x context and the 0.1.x forms of everything persisted or typed by humans were still read. This source tree removes both compatibility paths; that removal is not part of the already released 0.2.5 or 0.2.6. Theseus writes only the new names. It still reads the old persisted record forms (the `oracle-protocol`, `oracle-defect`, `oracle-finding` and `oracle-local-record` markers and `Oracle backoff reset:`) so a pull request last reviewed before the rename keeps its open defects and strikes; an old status context, artifact, command or stand-in form has no effect. Before upgrading from any release through 0.2.6 to a release containing this removal, change every ruleset or branch protection that requires `oracle-gate` or `oracle-verdict` to require `review-gate`: the old contexts keep their last state on open heads and are never updated again, so a ruleset still requiring one is satisfied by a stale success. This is the only place the old names appear.

| 0.1.x | After compatibility removal |
|---|---|
| status context `oracle-verdict` | `review-verdict` (`oracle-verdict` is no longer posted) |
| policy `status_context: oracle-verdict` | consumers must set `status_context: review-verdict`; a policy left at `oracle-verdict` posts under that name, and the gate, which reads only `review-verdict`, never sees it |
| status context `oracle-gate` | `review-gate` (`oracle-gate` is no longer posted) |
| callee secret `ORACLE_APP_ID` | `REVIEW_APP_ID` |
| callee secret `ORACLE_APP_PRIVATE_KEY` | `REVIEW_APP_PRIVATE_KEY` |
| callee secret `ORACLE_GATEWAY_TOKEN` | `REVIEW_GATEWAY_TOKEN` |
| callee secret `ORACLE_GATEWAY_URL` | `REVIEW_GATEWAY_URL` |
| input `tailscale_tag` default `tag:oracle-ci` | no default; the caller must pass it |
| input `policy_path` default `.github/oracle-reviewers.yml` | `.github/review.yml` |
| omp config `.github/oracle-omp-config.yml` | `.github/review-omp-config.yml` |
| input `caller_workflow` default `oracle-verdict.yml` / `oracle-gate.yml` | `review-verdict.yml` / `review-gate.yml` |
| input `review_workflow_name` default `oracle-review` | `review-request` (caller file `review-request.yml`) |
| every `ORACLE_*` environment variable (`ORACLE_LOCAL_ROOT`, `ORACLE_OMP_BINARY`, `ORACLE_PROMOTION_MIRROR`, ...) | the same name with `REVIEW_` in place of `ORACLE_`; the old names are not read |
| the local state root override `THESEUS_LOCAL_STATE` | unchanged; neither version reads a `THESEUS_LOCAL_*_DIR` variable |
| local cache `~/.cache/theseus/local-oracle`, gateway directory `~/.omp/oracle-gateway` | `~/.cache/theseus/local-review`, `~/.omp/review-gateway` |
| gateway clients `oracle-ci`, `oracle-<name>` | `review-ci`, `review-<name>` |
| artifacts `oracle-verdict-state-*`, `oracle-verdict-attempt-*`, `oracle-attempts`, `oracle-verdict-capture` | `review-verdict-state-*`, `review-verdict-attempt-*`, `review-attempts`, `review-verdict-capture` (state under the old names is not read) |
| markers `oracle-protocol`, `oracle-defect`, `oracle-finding`, `oracle-local-record`; heading `### oracle-verdict:` | `review-protocol`, `review-defect`, `review-finding`, `review-local-record`; `### review-verdict:` (the four markers are still read, never written, so records and findings from earlier reviews keep counting; the old heading is not read) |
| `Stand-in oracle review: APPROVE <sha>` | `Stand-in review: APPROVE <sha>` (the old form is not read) |
| `Oracle adjudication: WITHDRAW\|MAINTAIN D<n> - <reason>` | `Review adjudication: ...` (the old form is not read) |
| `Oracle backoff reset: <sha>` | `Review backoff reset: <sha>` (the old form is still read, never written) |
| owner command `/oracle-review` | `/review` (the old command does nothing) |

The caller's secrets block must map the new names onto the secrets it already holds, by name:

```yaml
secrets:
  REVIEW_APP_ID: ${{ secrets.ORACLE_APP_ID }}
  REVIEW_APP_PRIVATE_KEY: ${{ secrets.ORACLE_APP_PRIVATE_KEY }}
  REVIEW_GATEWAY_TOKEN: ${{ secrets.ORACLE_GATEWAY_TOKEN }}
  REVIEW_GATEWAY_URL: ${{ secrets.ORACLE_GATEWAY_URL }}
```

The callee `if:` conditions and the example callers' concurrency groups match owner commands and attester comments by exact, case-insensitive prefix (`/review`, `Review adjudication:`, `Stand-in review: APPROVE`), so only a real command shares the `pr-<n>` group and a quoted or mid-body mention cannot cancel a pending run. The reviewer then parses the exact forms and refuses any other comment.
