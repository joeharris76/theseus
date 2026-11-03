# Theseus

Theseus reviews pull requests with AI models and blocks merging until a review of
the exact commit being merged has passed.

The name comes from the Ship of Theseus, the ship that was rebuilt one plank at a
time until none of its original wood remained. A long-lived codebase is that ship:
every pull request swaps out a plank. Theseus decides which replacement planks are
sound enough to go in, so the ship can keep changing without losing what it is.

Distribution `theseus-reviewer`, import package `theseus_reviewer`, console script `theseus`. Requires Python 3.11+.

The package carries no consumer identity: GitHub App login, attesters, gated branch and policy paths come from trusted consumer policy, and missing configuration fails with an error rather than a default.

## Development

```
uv sync --locked
uv run -- ruff check .
uv run -- ruff format --check .
uv run -- pytest
uv build
```

## Using Theseus

- [docs/consumer-policy.md](docs/consumer-policy.md): consumer identity, policy keys and commands
- [docs/bootstrap.md](docs/bootstrap.md): hash-verified lock and installer
- [docs/matcher.md](docs/matcher.md): soundness-path matcher and vendored-copy drift check
- [docs/local-review.md](docs/local-review.md): pinned pre-submission review from the installed artifact
- [docs/gateway-host.md](docs/gateway-host.md): installed `install-omp` and `gateway-clients` for the gateway host
- [docs/hosted-workflows.md](docs/hosted-workflows.md): reusable review and gate workflows, pinned ref, environment secrets
- [examples/consumer/.github/workflows/](examples/consumer/.github/workflows/): consumer caller workflows

## Maintaining the extraction

- [UPSTREAM.md](UPSTREAM.md): ledger and check for BenchBox changes not yet ported
- `tools/parity.py`: installed-artifact versus BenchBox baseline parity run; the recorded result is in [PROVENANCE.md](PROVENANCE.md)
- `tools/smoke_wheel.py`: install the built wheel outside the checkout and run the scenario suites

## Project documents

- [CONTRIBUTING.md](CONTRIBUTING.md): merge policy, fork/candidate boundaries, development
- [SECURITY.md](SECURITY.md): reporting and trust model
- [RELEASING.md](RELEASING.md): release process
- [docs/repository-settings.md](docs/repository-settings.md): owner-applied GitHub settings
- [PROVENANCE.md](PROVENANCE.md): BenchBox source baseline and path map

## Status

Bootstrap. Until a separately authenticated Theseus oracle gate is live, merges require owner full-diff review.

## License

MIT; see [LICENSE](LICENSE).
