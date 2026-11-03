# Releasing

## Rules

- A release tag points at a head of `main` that has been merged under the governance in [CONTRIBUTING.md](CONTRIBUTING.md); never at an unreviewed or PR-only commit.
- Release assets are the wheel and sdist built from that exact commit. Their SHA-256 hashes are published with the release and recorded in the consumer lock; hashing a release label alone is not verification.
- Release builds run without App, tailnet or gateway credentials. Any publisher credential is available only to a publish step on the tagged `main` commit, never to fork PRs or candidate builds.
- The first live Theseus gate depends on an owner-reviewed bootstrap source and committed lock, not on the release whose publication it protects.

## Procedure

1. Confirm `main` CI is green at the intended head and the owner has reviewed every diff since the previous tag.
2. Update the version in `pyproject.toml` through a reviewed PR.
3. From a clean checkout of that head: `uv sync --locked && uv run -- pytest && uv build`.
4. Compute hashes: `shasum -a 256 dist/*`.
5. Create an annotated, signed tag on the reviewed head and publish a GitHub Release attaching `dist/*` and the hash list.
6. Verify by downloading the assets and re-checking the hashes before updating any consumer lock.

## Owner actions on GitHub

These settings are recorded intent. They are applied only by the owner; nothing in this repository applies or proves them. See [docs/repository-settings.md](docs/repository-settings.md).
