# Releasing

## Rules

- A release tag points at a head of `main` that has been merged under the governance in [CONTRIBUTING.md](CONTRIBUTING.md); never at an unreviewed or PR-only commit.
- Release assets are the wheel and sdist built from that exact commit. Their SHA-256 hashes are published with the release and recorded in the consumer lock; hashing a release label alone is not verification.
- The consumer lock needs two distinct https URLs for the identical wheel bytes (the installer refuses a lock with fewer): the primary release asset and a retained copy published under a second asset name. A wrong hash from either refuses; a blocked primary falls back only to the retained copy.
- Release builds run without App, tailnet or gateway credentials. Any publisher credential is available only to a publish step on the tagged `main` commit, never to fork PRs or candidate builds.
- The first live Theseus gate depends on an owner-reviewed bootstrap source and committed lock, not on the release whose publication it protects.

## Procedure

1. Confirm `main` CI is green at the intended head and the owner has reviewed every diff since the previous tag.
2. Confirm `version` in `pyproject.toml` equals the tag (`0.1.0` for `v0.1.0`); change it only through a reviewed PR. Record the frozen source hash of the release in [PROVENANCE.md](PROVENANCE.md): the sha256 over the sorted lines `<path>\t<sha256 of file bytes>\n` for every tracked file under `src/theseus_reviewer/` at the tag.
3. From a clean checkout of the merge commit: `uv sync --locked && uv run -- pytest && uv build`.
4. Compute hashes: `cd dist && shasum -a 256 *`. The wheel is deterministic, so rebuilding the same source reproduces its hash.
5. Create an annotated tag `v<version>` on the merge commit (signed when a key is configured) and publish a GitHub Release attaching the wheel, the sdist and the hash list. Attach the wheel a second time under a different asset name (for example `theseus_reviewer-<version>-py3-none-any.retained.whl`) as the retained copy; the file bytes must be identical.
6. Verify by downloading both wheel URLs and re-checking that each hashes to the recorded value before updating any consumer lock.
7. Record in the consumer lock (`.github/theseus-lock.json`) the wheel sha256 and both URLs, `source_commit` and `workflow_sha` (both the tagged merge commit), and the source and delta hashes, through that consumer's reviewed promotion pull request.

## Owner actions on GitHub

These settings are recorded intent. They are applied only by the owner; nothing in this repository applies or proves them. See [docs/repository-settings.md](docs/repository-settings.md).
