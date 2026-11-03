# Theseus

Independent oracle reviewer and merge gate, extracted from BenchBox.

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
