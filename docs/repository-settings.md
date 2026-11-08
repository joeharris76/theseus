# Repository settings

Owner actions on `joeharris76/theseus`. Repository, visibility, license and default branch are already in place; the remaining rows count as applied only once the owner applies and verifies them.

| Setting | Required value |
| --- | --- |
| Repository | `joeharris76/theseus` |
| Visibility | Public |
| License | MIT (`LICENSE`) |
| Default branch | `main` |
| Branch protection on `main` | Require pull request; require conversation resolution; no force pushes; no deletions; apply to administrators. Approving reviews are not required while the owner is the sole maintainer: the owner's full-diff review is recorded by the owner performing the merge. Linear history is not required so the extraction pull request can merge with a merge commit and keep its seed commit identity. |
| Required status checks | `test (py3.11)`, `test (py3.13)`, `test (windows, py3.12)`, `build and installed-wheel smoke`, `review eval corpus and score rule`; require branches up to date |
| Actions permissions | Allow-list of actions; default `GITHUB_TOKEN` read-only; approval required for all outside-collaborator workflow runs |
| Secrets/environments | No repository-level secrets; no secret-bearing environment reachable from fork or candidate workflows. The `review-eval` environment (deployment branches: `main`) holds the gateway client, tailnet client and classifier key the weekly eval reads; see [review-evals.md](review-evals.md#owner-setup) |
| Security | Private vulnerability reporting, Dependabot alerts and security updates enabled |
| Tags | Tag protection or ruleset restricting release tags to the owner |

The review gate's required check is added only by the later first-live-gate work, after a separately authenticated Theseus gate exists. When added, the `review-gate` status context MUST be restricted to the review App as its expected source (ruleset `integration_id`); see [hosted-workflows.md](hosted-workflows.md#what-to-require-and-what-the-job-exit-means).
