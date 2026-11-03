# Repository settings

Owner actions on `BenchBox-dev/theseus`. This document does not claim any of these are applied; the owner must apply and verify them.

| Setting | Required value |
| --- | --- |
| Repository | `BenchBox-dev/theseus` (owner creates or grants access) |
| Visibility | Public |
| License | MIT (`LICENSE`) |
| Default branch | `main` |
| Branch protection on `main` | Require pull request; at least 1 approving review; require review from Code Owners; dismiss stale approvals on new commits; require approval of the most recent push; require conversation resolution; require linear history; no force pushes; no deletions; apply to administrators |
| Required status checks | `test (py3.11)`, `test (py3.13)`, `build and installed-wheel smoke`; require branches up to date |
| Actions permissions | Allow-list of actions; default `GITHUB_TOKEN` read-only; approval required for all outside-collaborator workflow runs |
| Secrets/environments | No repository-level secrets; no secret-bearing environment reachable from fork or candidate workflows |
| Security | Private vulnerability reporting, Dependabot alerts and security updates enabled |
| Tags | Tag protection or ruleset restricting release tags to the owner |

The oracle gate's required check is added only by the later first-live-gate work, after a separately authenticated Theseus gate exists.
