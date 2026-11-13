# Structured findings and suggestion blocks: one-off eval comparison (2026-11-13)

Two live runs of the review eval corpus, one case each, against source commit `a619f4f456e7b83eaa076fef47c4dd88dbb71a9d` (clean tree), through `tools/review_eval.py run --mode live`:

- **baseline**: the checkout's own policy, no `brief` flags (`docs/eval-runs/2026-11-13-baseline.json`, configuration digest `3804db8d92ef`).
- **structured**: the same policy with `brief: {structured_findings: true, suggestions: true}` added, passed as `--consumer` (`docs/eval-runs/2026-11-13-structured-findings.json`, recorded by the tool as `foreign:` configuration digest `5a7f5818bb69`).

Same models and settings for both: the policy picks the reviewer tier per case, and both runs used the gateway's `review-local` client.

These are diagnostic local runs. They are not authenticated evidence and are deliberately not stored under `evals/results/`: a pull request that adds a result there must attach exactly one authenticated, digest-matched scheduled run, so a local file would fail the score rule and a recorded result would activate it for every configuration pull request (see [review-evals.md](../review-evals.md)).

## Scores

| case | baseline decision | baseline score | structured decision | structured score |
|---|---|---|---|---|
| control-private-helper-rename | SHIP | 1.0 | SHIP | 1.0 |
| legacy-transition-stdlib-patching | SHIP_WITH_FIXES | 0.9 | SHIP_WITH_FIXES | 0.9 |
| pr3962-test-weakenings | SHIP_WITH_FIXES | 1.0 | SHIP_WITH_FIXES | 1.0 |
| pr41-forced-fixed-dispute | SHIP | 0.2 | SHIP | 0.2 |
| v022-substitute-capacity-bypass | SHIP_WITH_FIXES | 0.866667 | SHIP_WITH_FIXES | 0.9 |
| **aggregate** | | **0.793333** (recall 0.8, precision 0.766667, decision 0.8) | | **0.8** (recall 0.8, precision 0.8, decision 0.8) |

## Expected-finding hits

Recall per case is identical in both runs, so the structured run finds every expected finding the baseline finds:

| case | expected findings hit (baseline) | expected findings hit (structured) |
|---|---|---|
| control-private-helper-rename | none expected | none expected |
| legacy-transition-stdlib-patching | 1 of 1 | 1 of 1 |
| pr3962-test-weakenings | 2 of 2 | 2 of 2 |
| pr41-forced-fixed-dispute | 0 of 1 | 0 of 1 |
| v022-substitute-capacity-bypass | 1 of 1 | 1 of 1 |

`pr41-forced-fixed-dispute` was missed by both runs (the reviewer decided SHIP). The only score difference is precision on `v022-substitute-capacity-bypass`: three findings in the baseline, two in the structured run, one hit in each.

## What the brief change did

- All 6 findings of the structured run carried `Problem:`, `Impact:` and `Fix:` paragraphs; 2 of the 7 findings of the baseline carried a `Fix:` label by chance and none the other two.
- Titles: the structured run's longest is 73 characters (all at most 80); the baseline's longest is 115.
- Detail length did not shrink: the structured findings average about 720 characters (566 to 1091) against about 560 (405 to 800) for the baseline. The labelled layout makes them scannable but not shorter.
- Severity differed on two findings of `pr3962-test-weakenings` (High in the baseline, Medium in the structured run); their expected-finding matches are unaffected.
- The run observations do not record the optional `suggestion` key, so this comparison does not show whether the reviewer offered any suggestion; no suggestion block can appear in a result score.

One sample per case; model output varies between runs, so differences of one finding are within the noise floor.

## Cost

The harness records no tokens or dollars, and the reviewers run on subscription pools through the local gateway, so cost is reported as the gateway's own request count for the `review-local` client over each run (each request is one model turn), plus wall time:

| run | gateway requests (review-local) | wall time |
|---|---|---|
| baseline | 18 (13 claude-opus-5-5, 4 claude-sonnet-5-5, 1 gpt-6-luna) | about 2 min 40 s |
| structured | 21 (15 claude-opus-5-5, 5 claude-sonnet-5-5, 1 gpt-6-luna) | about 4 min 40 s |

Requests of other gateway clients in the same window (`review-theseus`) are not counted.
