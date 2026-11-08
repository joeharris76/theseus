# Contributing

## Merge policy

Until a separately authenticated Theseus review gate is live, only merges whose full diff has been reviewed by the repository owner (`@joeharris76`) are allowed. A failing or absent gate is never permission to self-approve. Required status checks are necessary, not sufficient.

Pull requests that change CI, workflows, release tooling, lock or installer logic, or anything that reads credentials need explicit owner review of the whole diff, including generated and vendored files.

## Fork and candidate boundaries

Pull requests from forks, and any build of candidate (unmerged) code, never receive App credentials, tailnet credentials, trusted-publisher credentials, gateway credentials or a secret-bearing environment. Workflows triggered by candidate code use `pull_request` with `permissions: contents: read`; `pull_request_target` and `workflow_run` bridges that check out or execute candidate code are not accepted.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync --locked
uv run -- ruff check .
uv run -- ruff format --check .
uv run -- pytest
uv build
```

- Ruff, 120 columns, 4-space indent.
- No explanatory comments or docstrings; names and tests carry the intent.
- Tests are offline and fixture-driven; no test may need a live App, gateway or network secret.
- Pin GitHub Actions to full commit SHAs with the tag in a trailing comment.
- Commit messages describe the durable reason for the change.
- A change to any engine module under `src/theseus_reviewer/` other than the listed plumbing, to the review templates, the policy, the omp config, the consumer guidance or `evals/results/` must attach a live eval run that does not score lower than the recorded baseline; see [docs/review-evals.md](docs/review-evals.md).

## Reporting vulnerabilities

See [SECURITY.md](SECURITY.md); do not open public issues for vulnerabilities.
