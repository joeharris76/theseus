# Gateway host

The OMP gateway host (broker and gateway agents, client tokens, `clients.json`, the run file and the tailnet publication) stays consumer-owned. Theseus supplies the two commands the host script calls, and the host script obtains them only from a verified install.

## Commands the host calls

| Host step | Installed command |
|---|---|
| Install the omp build pinned by policy | `theseus install-omp --policy POLICY` prints the verified executable path |
| Write the scoped client list | `theseus gateway-clients --token-file client.token [--client NAME=TOKEN_FILE ...] [--accounts FILE] --omp-config OMP_CONFIG --out clients.json` |

Both read their inputs from a trusted checkout of the consumer's gated branch: the policy (`.github/review.yml`) and the omp config (`.github/review-omp-config.yml`). The host exports only `.github` from the fetched trusted ref into a scratch directory, never the embedded `_project/scripts` engine:

```
trusted=$(mktemp -d)
git -C "$REPO" archive "$TRUSTED_COMMIT" .github | tar -x -C "$trusted"
omp=$("$THESEUS" install-omp --policy "$trusted/.github/review.yml")
"$THESEUS" gateway-clients --token-file "$GATEWAY_DIR/client.token" --client "review-local=$GATEWAY_DIR/local-client.token" \
  --accounts "$ACCOUNTS_FILE" --omp-config "$trusted/.github/review-omp-config.yml" --out "$GATEWAY_DIR/clients.json"
```

`tests/fixtures/gateway_host/host.sh` is the exercised reference for these call sites (`tests/integration/test_gateway_host.py`).

## First stage

`$THESEUS` is the executable printed by the consumer's first stage:

```
THESEUS=$(python3 -I -S .github/theseus-bootstrap.py install --repo "$REPO" --commit "$TRUSTED_COMMIT")
```

The first stage is consumer-owned and uses the trusted lock, as in [bootstrap.md](bootstrap.md). The host fetches the trusted ref itself (`git fetch origin +refs/heads/<gated_base>:refs/remotes/origin/<gated_base>`), resolves it to a full commit and passes that commit. If the fetch, lock, hash or install verification fails the host stops: there is no fallback to an older install, an export directory, an ambient `theseus` or the embedded engine, and the running agents are not touched.

## What stays stable

- OMP cache layout is `<cache>/<omp version>/<platform>/omp`. The default root is now `~/.cache/theseus/omp`; a host that already caches builds keeps them by exporting `THESEUS_OMP_CACHE="$HOME/.cache/benchbox/omp"`, and the pinned sha256 is checked against the cached binary either way.
- Client identity: `review-ci` from `client.token` plus `review-<name>` for each `<name>-client.token`, the `tokenSha256` of each token, the model list from the omp config and the pinned accounts. The same inputs produce byte-identical `clients.json`. Client names changed in 0.2.0 (see "Migrating from 0.1.x" in [hosted-workflows.md](hosted-workflows.md)); register the new names with the gateway.
- The gateway secret names are `REVIEW_GATEWAY_URL` and `REVIEW_GATEWAY_TOKEN` (renamed in 0.2.0; see "Migrating from 0.1.x" in [hosted-workflows.md](hosted-workflows.md)).

## Not done here

Nothing in this repository starts or restarts a host, regenerates the shared `clients.json`, restarts an agent or contacts a live gateway. The host script's own switch to the installed commands is part of the consumer cutover; until then the BenchBox script keeps its embedded call sites.
