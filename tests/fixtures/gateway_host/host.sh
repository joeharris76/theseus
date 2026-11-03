#!/bin/bash
set -euo pipefail
umask 077

: "${THESEUS:?the verified installed theseus launcher}"
: "${CONSUMER_REPO:?}"
: "${TRUSTED_COMMIT:?}"
: "${GATEWAY_DIR:?}"
: "${ACCOUNTS_FILE:?}"

trusted=$(mktemp -d)
trap 'rm -rf "$trusted"' EXIT
git -C "$CONSUMER_REPO" archive "$TRUSTED_COMMIT" .github | tar -x -C "$trusted"

omp=$("$THESEUS" install-omp --policy "$trusted/.github/oracle-reviewers.yml")
[ -n "$omp" ] && [ -x "$omp" ] || { echo "the pinned omp install gave no executable" >&2; exit 1; }
echo "omp: $omp"

named=()
for file in "$GATEWAY_DIR"/*-client.token; do
  [ -e "$file" ] || continue
  named+=(--client "oracle-$(basename "$file" -client.token)=$file")
done
"$THESEUS" gateway-clients \
  --token-file "$GATEWAY_DIR/client.token" ${named[@]+"${named[@]}"} \
  --accounts "$ACCOUNTS_FILE" \
  --omp-config "$trusted/.github/oracle-omp-config.yml" \
  --out "$GATEWAY_DIR/clients.json"
