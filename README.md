# Theseus

Theseus reviews pull requests with AI models and blocks merging until a review of
the exact commit being merged has passed.

The name comes from the Ship of Theseus, the ship that was rebuilt one plank at a
time until none of its original wood remained. A long-lived codebase is that ship:
every pull request swaps out a plank. Theseus decides which replacement planks are
sound enough to go in, so the ship can keep changing without losing what it is.

Distribution `theseus-reviewer`, import package `theseus_reviewer`, console script `theseus`. Requires Python 3.11+.

The package carries no consumer identity: GitHub App login, attesters, gated branch and policy paths come from trusted consumer policy, and missing configuration fails with an error rather than a default.

## How Theseus uses OMP

Theseus does not call model APIs itself. Every review runs inside OMP, a coding-agent
CLI, in a read-only session. OMP gives Theseus one interface for working consistently
and reliably with many models and providers, and for falling back across them in a
structured way.

- **Pinned build.** The consumer policy pins the OMP version and a SHA-256 for each
  platform build (`omp:`). `theseus install-omp` installs exactly that build and refuses
  any other. Theseus never uses an `omp` already on `PATH`.
- **Roles, not model names.** Each reviewer in the policy names an OMP role such as
  `@smol`, `@default`, `@review` or `@plan`. The consumer's OMP config
  (`consumer.omp_config`) maps each role to a provider, model and effort level. Changing
  which model reviews a pull request is a config change, not a code change.
- **Provider logins stay on the gateway.** Reviewers hold no provider credentials. They
  reach a consumer-run OMP gateway with a scoped client token. The gateway host keeps
  the provider logins (for example Anthropic and OpenAI Codex accounts), and
  `theseus gateway-clients` writes its client list.
- **Two layers of fallback.**
  - When a model hits a usage limit, OMP moves to the next model in that model's
    fallback chain (`retry.fallbackChains` in the OMP config).
  - If a whole pool of reviewers still reports a quota error, Theseus pauses that pool
    for up to an hour and retries later.
  - A review that could not run never counts as a pass.

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
- [docs/review-evals.md](docs/review-evals.md): incident corpus, reviewer scoring, the no-lower-score rule and the weekly live run
- [docs/review-comment-format-proposal.md](docs/review-comment-format-proposal.md): format audit and redesign proposal for reviewer comments, statuses and check output
- [examples/consumer/.github/workflows/](examples/consumer/.github/workflows/): consumer caller workflows

## Maintaining the extraction

- `tools/smoke_wheel.py`: install the built wheel outside the checkout and run the scenario suites

## Project documents

- [CONTRIBUTING.md](CONTRIBUTING.md): merge policy, fork/candidate boundaries, development
- [SECURITY.md](SECURITY.md): reporting and trust model
- [RELEASING.md](RELEASING.md): release process
- [docs/repository-settings.md](docs/repository-settings.md): owner-applied GitHub settings
- [PROVENANCE.md](PROVENANCE.md): BenchBox source baseline and frozen source hashes through v0.1.6

## Status

Bootstrap. Until a separately authenticated Theseus review gate is live, merges require owner full-diff review.

## License

MIT; see [LICENSE](LICENSE).
