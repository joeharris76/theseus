# Contributing

## Merge policy

Every pull request needs a current-head review-verdict `SHIP` from `theseus-review[bot]` and green required checks before it merges. A failing, absent or stale gate is never permission to self-approve.

Changes to gate, receipt, dedup or posting code, workflows, policy or the lock additionally need an independent review: a fresh-context agent session that did not author the change, of the exact final head SHA, with the reviewer, head and verdict recorded.

The repository owner (`@joeharris76`) alone performs stand-ins, adjudications, owner instructions and backoff resets.

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
- Changes to `evals/results/` must be unmodified authenticated artifacts of a successful scheduled run, appended one at a time; see [docs/review-evals.md](docs/review-evals.md). While no result has been recorded, the score rule is inactive for engine, scorer, corpus, workflow, dependency, policy, omp config and guidance changes and the owner's whole-diff review applies to them. Committing the first authenticated baseline activates the rule for every such change, which then needs digest-matched authenticated evidence that does not score lower; do not record a baseline until a trusted candidate executor exists.

## Reporting vulnerabilities

See [SECURITY.md](SECURITY.md); do not open public issues for vulnerabilities.
