# Review evals

The review-quality eval turns incidents into cases and scores a reviewer configuration against them. A template edit, a model-role or omp-pin change, or a consumer guidance change stays only if the score does not drop.

Scores are evidence for the owner's review. Nothing here merges, approves or waives anything.

## The corpus

`evals/cases/<id>/` holds one self-contained case:

| Path | Content |
|---|---|
| `case.json` | `id` (the directory name), `incident` (where the case came from), `message` (the commit message of the change), `expected`, optional `fake` |
| `base/` | Files of the tree the change applies to (optional) |
| `head/` | Files the change adds or replaces; at least one must differ from `base/` |

`expected` carries:

- `decisions`: the decisions that count as right (`SHIP`, `SHIP_WITH_FIXES`, `DO_NOT_SHIP`). A case that expects findings cannot accept `SHIP`.
- `findings`: the defects the reviewer must raise. Each has a slug `id`, a repository-relative `path` and `terms`, a list of groups of lower-case words. A finding matches when it is on `path` and its title and detail contain at least one word of every group. One finding satisfies at most one expectation.
- `acceptable`: findings that are valid but not required (same `path` and `terms`). They are neither matches nor noise.

`fake` holds the recorded replies the deterministic run feeds a fake omp: `good` (a correct review) and `regressed` (a review that misses the incident, or for the control, invents a defect). Each reply is `decision`, `summary`, `files_examined` and optional `defects`.

Case files are text, at most 64 KiB each and 256 KiB per case, with no symbolic links and nothing shaped like a credential. `python3 tools/review_eval.py validate` checks all of it, and CI runs it on every change.

### Seed cases

| Case | Incident |
|---|---|
| `v022-substitute-capacity-bypass` | v0.2.2 (#35): a substituted reviewer skipped the slot's own capacity check, found by the local review of BenchBox #3978 and fixed in #37 |
| `pr3962-test-weakenings` | BenchBox #3962: a test rewrite kept its names and lost what they assert (secret mappings checked by key only, edited-comment cases removed) |
| `legacy-transition-stdlib-patching` | `test_legacy_transition` and `test_github` before fddee19: tests faked `subprocess` through a module attribute instead of a seam the code owns |
| `pr41-forced-fixed-dispute` | #41 (BenchBox #3950, D1): a forced dispute re-review rewrote `fixed` to `not_fixed` against the reviewer's own evidence |
| `control-private-helper-rename` | Not an incident: a behaviour-preserving rename that anchors precision. A reviewer that flags everything scores lower |

To add a case from a new incident, write the smallest tree that reproduces the defect class (keep the real file and function names), state in `expected` what a reviewer must say, and record the two fake replies. Cases are in the soundness manifest and the gate paths, so a corpus change is reviewed like the reviewer itself.

## Scoring

Per run of a case:

- recall: expected findings matched / expected findings (1 when none are expected);
- precision: matched / (matched + noise), where noise is a finding that matched no expectation and no `acceptable` entry (1 when there are none);
- decision: 1 when the decision is in `decisions`, else 0.

The case score is `0.6 * recall + 0.2 * precision + 0.2 * decision`. A case that was not reviewed (unavailable, no outcome) scores 0 everywhere. The aggregate is the mean over cases. With `--repeat N` a case runs N times and its score is the mean of the runs.

The score is recomputed from the recorded observations and the case expectations every time it is compared; a score field in a result file is never trusted.

## Running it

```
python3 tools/review_eval.py validate
python3 tools/review_eval.py run --mode live [--case ID] [--repeat N] [--out FILE] [--compare]
python3 tools/review_eval.py score RESULT.json
```

`run` builds a throw-away git repository per case (the consumer skeleton, then `base/` as the gated branch, then `head/` as the change) and runs the checkout's own `theseus local-review --member` against it with the policy at the base commit. The skeleton is the consumer's `.github` files outside `workflows/` (policy, omp config, soundness manifest and the guidance file the policy names), so what is scored is the configuration in the checkout: templates, classifier, policy, omp pin, guidance. `--consumer DIR` scores another consumer's configuration with the same cases; such a run records a `foreign:` configuration digest and is never accepted as evidence.

Credentials are the local-review ones ([local-review.md](local-review.md)): the classifier key (`TYPESAFE_API_KEY`), and a gateway client, either `~/.omp/review-gateway/local-client.token` on the gateway host or `REVIEW_GATEWAY_URL` with `REVIEW_CLOUD_GATEWAY_TOKEN` from elsewhere. Live runs spend model tokens; a full run reviews each case once at the tier the policy picks for it.

`run` refuses to start when template, policy, guidance or corpus files have uncommitted changes (`--allow-dirty` records the run as dirty, and a dirty run is never accepted as evidence). It exits 1 when a case could not be reviewed, or with `--compare` when the aggregate is below the newest recorded baseline.

A result is `evals/results/<date>-<sha>.json`: observations, scores, configuration and corpus digests, source commit, clean-tree state and `workflow_run_id`. These fields alone prove nothing; acceptance requires authenticated execution evidence.

## Deterministic subset in CI

`tests/integration/test_review_eval_replay.py` runs every case through the real local-review pipeline against the fake omp, gateway and classifier of the integration harness, once with the recorded `good` replies (every case must score 1.0) and once with the `regressed` replies (every case must score lower). It proves that a correct review scores full marks, that a degraded one cannot, and that the scorer reads the engine's real outcome. It does not measure a model: the replies are recorded.

`tests/unit/test_review_eval.py` covers the matcher, the scoring, the corpus validation and the change rule below.

## The rule for reviewer-configuration changes

A pull request that changes any of

- every file under `src/theseus_reviewer/`, including gate code, nested judgment packages, templates and vendored dependencies;
- `.github/review.yml` (policy, models, omp pin), `.github/review-omp-config.yml`, and every other file the scored run carries in its consumer skeleton: anything under `.github/` except `workflows/`, `dependabot.yml` and `theseus-lock.json`, plus any file the policy names (such as the soundness manifest);
- the consumer guidance file the policy names (`consumer.review_guidance`) or any `.github/review-guidance*`;
- `evals/cases/`, `tools/review_eval.py`, `pyproject.toml`, `uv.lock`, and the CI and live-eval workflows;
- anything under `evals/results/`,

must attach authenticated live execution evidence for exactly the source, configuration and corpus it proposes, without lowering the baseline score. The same inputs participate in the configuration digest, so editing the scorer, gate or corpus invalidates old evidence.

CI enforces it in the `review eval corpus and score rule` job. On a pull request it runs the base branch's copy of `tools/review_eval.py` (so the change cannot loosen its own check) and `check-change`:

1. No relevant file changed: pass.
2. Authenticate baseline candidates against GitHub's HTTPS API. A result must identify a successful `schedule` execution of this repository's actual `review-eval.yml` workflow ID, on `main`, from this repository (not a fork). The execution's source SHA must equal the result's commit. Source objects must be available locally; CI fetches the full history.
3. Download that run's single non-expired `review-eval-result` artifact by its API-issued artifact ID. Verify its SHA-256 archive digest against the API response and require the artifact's `result.json` to equal the recorded result. A claimed workflow name, public configuration digest, owner assertion, local live run or forged observations cannot substitute for this artifact.
4. Verify the source commit's configuration and corpus against both the result digests and the proposed tree. That binds the actual executed source, including the scorer, workflows and every engine subpackage. Candidate code is never executed to verify evidence.
5. Existing results are append-only. Exactly one new, usable result is required: live, complete, clean, correctly dated/named, and authenticated as above. Both baseline and new observations are rescored with the base branch's scorer and case expectations, even when the corpus changes.
6. A pull request that changes no file under `evals/results/` passes with the explicit verdict `no trusted candidate executor: the score rule is inactive for configuration changes; owner whole-diff review applies`. The rule does not score such changes and is not a gate on them. A pull request that adds or changes results always goes through the authenticated path; without an authenticated baseline, only a results-only change for the unchanged base configuration and corpus can bootstrap one.
7. With a baseline, a new result's score must not be lower. Missing or expired artifacts, unavailable API access, unmatched source, and unauthenticated results block acceptance of that result.

To bootstrap, download the artifact from a successful scheduled run and add its unchanged `result.json` as `evals/results/<date>-<first 12 SHA characters>.json` in a results-only pull request. Required checks and whole-diff owner review still apply. Local runs are diagnostic, not authenticated evidence.

**Candidate execution prerequisite:** the scheduled workflow executes only reviewed `main`. It cannot authenticate a configuration-changing pull request. The score rule therefore stays inactive for such changes, including changes to this gate itself, and the owner's whole-diff review is the only control, until a separately isolated trusted candidate-evaluation executor and its authenticated verifier are implemented and reviewed. Repository policy forbids giving candidate code gateway or other secrets, so neither dispatching this workflow on a candidate nor owner claims are an acceptable replacement. There is no exemption for the patch introducing this rule.

## Weekly live run

`.github/workflows/review-eval.yml` runs every Monday at 05:41 UTC (`schedule` only, no manual or pull-request trigger; run the command above by hand for an ad-hoc run). It has `contents: read`, no repository write access and no App credentials. It checks out `main`, joins the tailnet, runs `tools/review_eval.py run --mode live --compare` and uploads the result as the `review-eval-result` artifact for 90 days. The job summary shows the per-case table. The job fails when a case cannot be reviewed or the aggregate is below the baseline, so the owner is notified through GitHub.

The workflow cannot commit. Its immutable artifact is the authenticated evidence used by the results-only bootstrap above; copying or editing JSON without the successful execution does not establish a baseline. Artifact retention is 90 days; an expired baseline is unusable, so record a fresh authenticated results-only run before expiry.

### Owner setup

1. Create the environment `review-eval` on the repository. Restrict deployment branches to `main`. It needs no reviewers because the workflow cannot be triggered from a pull request.
2. Add these environment secrets. They are the same names the review workflows use, minus the App credentials, which the eval never reads:

| Secret | Use |
|---|---|
| `REVIEW_GATEWAY_URL` | Address of the credential gateway |
| `REVIEW_GATEWAY_TOKEN` | A gateway client token. A dedicated client (an `eval-client.token` on the host, which `gateway-clients` turns into `review-eval`) keeps eval traffic apart from review traffic |
| `TS_OAUTH_CI_CLIENT_ID`, `TS_OAUTH_CI_SECRET` | Tailnet OAuth client allowed to join as `tag:theseus-ci` |
| `TYPESAFE_API_KEY` | The size classifier key. `local-review` refuses to run without it, and the classifier is part of what the eval scores |

3. Confirm the Actions allow-list permits the pinned actions the workflow uses (`actions/checkout`, `astral-sh/setup-uv`, `tailscale/github-action`, `actions/upload-artifact`).
4. After the first green weekly run, record its artifact through the enforced results-only bootstrap. CI needs `actions: read` to authenticate runs and artifacts; local `check-change` can use `GH_TOKEN` with read-only Actions access. No signing or gateway secret is exposed to pull-request code by this verifier.

The secrets are reachable only from the scheduled run on `main`. The reviewed cases are the repository's own corpus, so no pull request content ever reaches a model through this workflow; the reviewer itself runs with the same secret withholding and isolation as `local-review`.

## Limits

- Model output varies between runs. A change near the noise floor can fail or pass by chance; use `--repeat` and read the per-case table instead of re-running until green.
- Five cases are a smoke net, not a benchmark. Grow the corpus from incidents, and add a clean control whenever a case could reward flagging more.
- Keyword terms measure whether the defect class was named, not whether the explanation is right. The owner reads the stored detail.
