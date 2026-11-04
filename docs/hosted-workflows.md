# Hosted workflows

Theseus ships two reusable workflows, `.github/workflows/review.yml` and `.github/workflows/gate.yml`. A consumer keeps thin caller workflows that own the triggers, the concurrency groups and the check names, and calls these at a pinned commit. Copy-ready callers are in [`examples/consumer/.github/workflows/`](../examples/consumer/.github/workflows).

| Caller file (consumer) | Workflow name | Calls | Check it produces |
|---|---|---|---|
| `oracle-verdict.yml` | `oracle-verdict` | `review.yml` | `oracle-verdict` status, reviews and comments from the App |
| `oracle-gate.yml` | `oracle-gate` | `gate.yml` | `oracle-gate` status from the App |
| `oracle-review.yml` | `oracle-review` | none (a no-op job) | its completion is the bridge trigger |

The `oracle-verdict` and `oracle-gate` contexts, the caller workflow names and file names, and the `oracle-verdict-*` artifact names are protocol identities. They stay as they are so an existing branch protection that requires `oracle-gate` keeps matching. The reviewer finds retry state by the caller workflow path `.github/workflows/oracle-verdict.yml` and the gate dispatch targets `oracle-gate.yml`, so the caller files keep those names.

## Caller context

A called workflow sees the caller's `github` context: `github.event_name`, `github.event.*`, `github.sha`, `github.ref`, `github.repository`, `github.workflow_ref` (the caller's), `github.token`. `workflow_dispatch` inputs are read as `github.event.inputs.*`; `inputs.*` inside a callee is only its own `workflow_call` inputs. Consequences:

- Triggers, `run-name`, `concurrency` and the job-level `permissions` ceiling belong to the caller. The callees declare no workflow-level `concurrency` and no `run-name`, so a callee can never share a group with its caller. The example callers use groups prefixed `oracle-verdict-` and `oracle-gate-`; the two never collide with each other or with CI groups.
- `github.sha` is the tip of the branch the event ran against. The review runs only when `github.ref` is `refs/heads/<gated_base>`; a pull request against any other base skips the jobs. A `workflow_dispatch` from any other ref is refused by the guard job.
- The guard job (`contents: read` only, no secrets, no environment) refuses before any secret job starts: a `theseus_ref` that is not a full 40-hex commit, an empty `gated_base`, a caller whose `github.workflow_ref` is not `<repository>/.github/workflows/<caller_workflow>@...`, and a `theseus_ref` that is not the pin recorded in the trusted consumer lock (below).

## Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `environment` | yes | | Name of the caller environment whose secrets the secret-bearing jobs read |
| `theseus_ref` | yes | | Full commit SHA of the Theseus repository that holds the callee and its bootstrap installer; must equal the SHA on the caller's `uses:` line |
| `theseus_repository` | no | `joeharris76/theseus` | Repository checked out at `theseus_ref` |
| `gated_base` | yes | | Branch the review and gate apply to; its tip supplies the trusted policy and lock |
| `policy_path` | no | `.github/oracle-reviewers.yml` | Consumer policy, read from the trusted checkout |
| `trusted_paths` | no | `.github` | Sparse-checkout patterns for the trusted checkout; add the directories of any `consumer` manifest outside `.github`. The example gate caller reruns on every push to the gated branch (no `paths:` filter), so a manifest change outside `.github` re-evaluates standing gate statuses immediately rather than at the next hourly sweep |
| `caller_workflow` | no | `oracle-verdict.yml` / `oracle-gate.yml` | File name of the caller; the guard compares it with `github.workflow_ref`, and the bridge dispatches it |
| `review_workflow_name` | no (`gate.yml`) | `oracle-review` | `name:` of the workflow whose completion bridges into the gate |

## Pinned workflow ref

Pin both the `uses:` line and `theseus_ref` to the same full commit SHA:

```yaml
uses: joeharris76/theseus/.github/workflows/review.yml@<40-hex commit>
with:
  theseus_ref: <the same 40-hex commit>
```

A branch or tag would let the workflow change under the consumer, so every reference is a commit SHA. The callee cannot read its own ref from the ambient context: `github.workflow_sha` and `github.workflow_ref` describe the caller, and `job_workflow_sha` is a claim inside the OIDC token, not a context value. The callee therefore takes the pin as an explicit input and checks out only `src/theseus_reviewer/bootstrap.py` from that commit. A caller that passes a different `theseus_ref` from its `uses:` ref would run a bootstrap installer from one commit under a workflow from another. A consumer that wants that comparison enforced adds a separate no-secret job to its caller that requests an OIDC token, reads the `job_workflow_sha` claim and compares it with the input; the review job never gets `id-token: write` and the isolation phase refuses a job that holds the OIDC request token.

The organisation Actions allow-list must permit `joeharris76/theseus/.github/workflows/*@<sha>` and the four pinned actions the callees use (`actions/checkout`, `actions/create-github-app-token`, `actions/upload-artifact`, `tailscale/github-action`).

## Trust chain: two pins, two processes

Neither workflow installs `latest` or reads a lock from a pull request head.

### The pin must equal the trusted lock

The caller's `uses: joeharris76/theseus/.github/workflows/<callee>@<sha>` and its `theseus_ref` input are chosen by the caller workflow file, which a pull request can edit. So the guard job binds them to the reviewed lock. It sparse-checks-out `.github/theseus-lock.json` from the trusted ref (`review.yml`: `github.sha`, the base tip for a pull request event; `gate.yml`: the current tip of `gated_base`), reads it from that commit's object, and refuses with exit 2 unless `theseus_ref == workflow_sha == source_commit`. A missing, unparsable or incomplete lock is refused too. A lock on the pull request head is never read, so a pull request that repoints the pin to an unreviewed Theseus commit without a reviewed lock change cannot run. Every secret-bearing job (`review`, `sweep`, `gate`, `bridge`) has `needs: guard`, so none starts when the check fails. The lock location is fixed at `.github/theseus-lock.json`.

1. **Reviewer pin A.** The review checks out the trusted copy of the gated branch at `github.sha` (sparse, `persist-credentials: false`) and runs the bootstrap installer from `theseus_ref` against it: `bootstrap.py install --repo trusted --commit "$github.sha"`. The installer reads `.github/theseus-lock.json` from that commit's object, verifies every wheel hash, refuses anything it cannot verify, installs into an isolated tree and prints the launcher. The launcher directory goes on `PATH`, so every later step runs `theseus drive --policy "$POLICY_PATH" <phase>` from that install. The pull request head is checked out later, into a separate directory, for reading only.
2. **Gate pin B.** After the reviewer has exited and the result is finalised, the workflow checks out the *current* gated branch into `gate/`, resolves its tip with `git rev-parse HEAD` and installs that commit's lock into a separate cache. Its launcher path is passed to the publish step only, as `THESEUS_GATE_COMMAND`, with `THESEUS_GATE_ROOT` naming the `gate/` checkout. The reviewer posts the verdict, then runs `<pin B launcher> gate --repo ... --pr ... --head-sha ... --policy ... --root gate/` as a subprocess. The gate decision is therefore computed by pin B's code against pin B's policy and manifests. Pin A never imports pin B and pin B never imports pin A; they share no interpreter.

If the gate pin cannot be resolved (no lock at the tip, a hash mismatch, a blocked source) the step is allowed to fail so the verdict is still published, `THESEUS_GATE_COMMAND` is then empty, and `theseus` refuses an empty value instead of falling back to its own interpreter.

The `gate.yml` workflow has a single pin: the gate runs from the lock at the tip of `gated_base`, read at the time the job starts. It never checks out candidate code.

## Failure and retry

`theseus_reviewer.github.post_gate` runs the gate subprocess and raises `GitHubError` for a spawn failure, any exit other than 0 (posted) or 3 (posted, waiting), and a `THESEUS_GATE_COMMAND` that is set but empty. `drive publish` then:

- keeps the verdict it already posted and the retry-state artifact (`state_recorded`),
- prints `::warning::oracle-gate was not posted from this run: <reason>` and sets the step output `gate_failed=true`,
- lets the later steps run: the state and attempt uploads, and the dispatch of the consumer's `oracle-gate.yml` for the reviewed head, which resolves a fresh pin B and retries (the hourly gate sweep is the backstop),
- ends the run with a failing step, so a failed gate post is never a green run.

A failing subprocess posts no status of its own, so no success is published on its behalf.

## Permission ceilings

A callee job can hold at most what the caller's calling job grants. The example callers grant the union of what the callee jobs ask for; each callee job then narrows it.

| Job | Ceiling the caller must grant | Job permissions |
|---|---|---|
| `guard` | `contents: read` | `contents: read`; it checks out only `.github/theseus-lock.json` from the trusted ref |
| `review` (`review.yml`) | `actions: write`, `contents: read`, `pull-requests: read` | the same; `actions: write` lets the retry sweep and the dispatch step run |
| `sweep` (`review.yml`) | same | same |
| `gate` (`gate.yml`) | `contents: read`, `pull-requests: read` | the same |
| `bridge` | `actions: write`, `contents: read`, `pull-requests: read` | the same; it only lists pulls and dispatches the gate |
| `sweep` (`gate.yml`) | `contents: read`, `pull-requests: read` | the same |

Both workflow files declare `permissions: {}` at the top. No job requests `id-token`, `statuses`, or `pull-requests: write` through `github.token`; statuses, reviews and comments are posted with the App token minted inside the job.

## Environment secrets

The secret-bearing jobs (`review`, `sweep`, `gate`) name the caller's environment through the `environment` input. Secrets are read from that environment, not from repository secrets and not through `secrets: inherit`, so a caller needs no `secrets:` block and the callee needs no declared secrets. The environment must hold:

| Secret | Used by |
|---|---|
| `TYPESAFE_API_KEY` | `prepare`, `recover`, `finalize`, the scans |
| `ORACLE_GATEWAY_TOKEN`, `ORACLE_GATEWAY_URL` | the attempt steps and `finalize` only |
| `TS_OAUTH_CI_CLIENT_ID`, `TS_OAUTH_CI_SECRET` | the tailnet join, the scans, `finalize` |
| `ORACLE_APP_ID`, `ORACLE_APP_PRIVATE_KEY` | the token-mint steps only |

Each step names exactly the secrets it needs in its own `env:`; the job-level `env:` holds only booleans derived from them and the policy path. The App private key never reaches an environment variable. The App token is minted only after `finalize` has run and the reviewer has exited, and is passed only to `publish` and `dispatch`.

Candidate code never meets a credential: the pull request head is checked out without credentials into `workspace/` and read, never built or executed, and the reviewer's process isolation and disk scans run before and after it. Fork pull requests reach the same path through `pull_request_target`, which executes only the trusted base copy. Environment protection on `environment` must not make the secrets reachable to any workflow that builds or runs candidate code.

## The `workflow_run` bridge

The caller `oracle-gate.yml` triggers on `workflow_run: workflows: [oracle-review]`. GitHub matches that list against the triggering workflow's `name:`, not its file name. The consumer's `oracle-review.yml` therefore must keep `name: oracle-review`. `gate.yml` repeats the check on the event itself: its `bridge` job runs only when `github.event.workflow_run.name` equals `review_workflow_name` and the run belongs to the same repository, then dispatches `caller_workflow` on `gated_base` for the open pull request whose head matches. The bridge runs no candidate code and holds no secrets.

## Local pre-submission review

Hosted workflows do not change the local flow: `theseus local-review --policy PATH` (see [consumer-policy.md](consumer-policy.md)) still runs from the author's trusted checkout and keeps its own cache and state.

## What the offline suite proves

`tests/integration/runs.py` executes `jobs.review.steps` of the shipped `review.yml` against the fake GitHub, fake OMP and fake gateway: every phase step runs the command text from the workflow, with exactly the environment the step names. `tests/integration/test_hosted_pins.py` installs two synthetic locked artifacts (reviewer pin A built from this source, gate pin B with a different gate) through the shipped installer and the shipped install steps, advances the gated branch between the review start and the publish step, and asserts that the posted gate decision comes from pin B in a separate process, that a failing pin B records `gate_failed` and posts no gate status, and that an unresolvable pin B does the same. `tests/unit/test_hosted_workflows.py` pins the structure: SHA-pinned actions, permissions, secret scoping, the guard conditions and the bridge name match.

Not provable offline: environment-secret resolution inside a called workflow, the organisation allow-list, `GITHUB_TOKEN` ceiling enforcement by GitHub, the `workflow_run` delivery itself, the tailnet and gateway, and the App token mint. They are verified at the first live run.
