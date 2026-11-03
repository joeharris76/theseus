# Upstream port tracking

BenchBox stays the implementation source of truth for the reviewer, gate and matcher until it cuts over to the installed Theseus artifact. This ledger records every BenchBox commit since the extraction baseline that touches the extraction inventory, and how Theseus answered it. No source-freeze or parity claim is valid while the ledger has pending entries.

- Baseline: `95c423c35efd3fb4962145bdc628006d5aaaad26` (the commit [PROVENANCE.md](PROVENANCE.md) maps the seed from).
- Ledger: [tools/upstream_ledger.json](tools/upstream_ledger.json), reviewed like source.
- Tool: [tools/upstream_ledger.py](tools/upstream_ledger.py), standard library only.

## What is watched

A BenchBox commit counts when it touches any of:

- every BenchBox path in [tools/seed_map.json](tools/seed_map.json) (source, tests, fixtures);
- every path under the prefixes in the ledger's `watch.prefixes` (the reviewer package, the oracle test and fixture trees, the oracle and OMP-pin workflow tests), so a newly added upstream file is caught without a map entry;
- the consumer-owned files in `watch.files`: the policy and manifests, the oracle and OMP-pin workflows, the gate and review-check scripts, the gateway host, local-review and `pr_arm` launchers, the soundness digest and drain scripts, and the inert-path reader index. They are not ported, but a change to them can alter an interface, a cache or a policy the package serves.

The promotion path (`promotion.py`, the gate's promotion checks, the oracle-gate workflow) is inside this set.

## Check

```
python3 tools/upstream_ledger.py check --benchbox-repo <BenchBox checkout> --ref origin/develop
```

Fetch the checkout first (`git -C <checkout> fetch origin develop`); the tool never touches the network. It prints the commits since the baseline that touch the inventory, with their paths, and exits 0 only when every one has a ledger entry that is `port` or `noop`. It exits 1 for a pending commit, a malformed entry, a `port` entry whose Theseus commit is not in this repository, or an entry for a commit that is no longer in the range; it exits 2 when the ref is unreadable or does not descend from the baseline.

`list` prints the same report and always exits 0. `refresh` appends a `pending` entry for every uncovered commit, then runs the check, so the next step is to resolve each one.

## Entries

```json
{"commit": "<40-hex BenchBox commit>", "disposition": "port", "port_commit": "<40-hex Theseus commit>",
 "effects": "interface, policy and cache effects, or none", "evidence": "tests or parity run that covers it"}
{"commit": "<40-hex BenchBox commit>", "disposition": "noop", "rationale": "why Theseus is unaffected"}
{"commit": "<40-hex BenchBox commit>", "disposition": "pending", "subject": "...", "paths": ["..."]}
```

- `port` needs the Theseus commit that carries the change and its `effects`: interface, policy and cache impact, including persisted-format compatibility.
- `noop` needs a rationale; an empty one does not count.
- `pending` never passes.

## Refresh and invalidation

1. Run the check against a freshly fetched `origin/develop`.
2. Resolve each pending entry with a port commit or a reasoned no-op; commit the ledger with the port.
3. Run [tools/parity.py](tools/parity.py) again: the frozen-source claim in PROVENANCE.md holds only for the source hash it names.

An urgent BenchBox fix after a freeze lands in BenchBox, is ported here, and invalidates the recorded parity run and frozen hash: both are regenerated at the new head. A check run against any later `origin/develop` that finds an un-ported commit invalidates the claim until the ledger is current again.

## Status

Checked against BenchBox branch `feat/theseus-promotion-policy` at `8cebb190e7f0cbb2f3a6de653b1db0d0ecd3fce9` (`python3 tools/upstream_ledger.py check --benchbox-repo <BenchBox checkout> --ref feat/theseus-promotion-policy`): five commits since the baseline, two touching the inventory, both recorded, none pending, exit 0.

- `b78c2f2` (BenchBox's own promotion policy section): no-op, consumer-owned data; the slug change in its promotion scenario is already in Theseus's fixtures.
- `8cebb19` (read only the mapped baseline paths; keep leading dots in archive paths): ported ahead in Theseus commit `7d01388`.

Both commits are on a BenchBox branch that is not on `develop` yet, so the same check against `origin/develop` (`c1c42a15940855e62b64dc2be0382179eb302160`) reports these two entries as not matching a commit in range. When the BenchBox pull request merges its SHAs change: rewrite both entries to the `develop` commits (the Theseus port stands) and rerun the check against `origin/develop` before relying on the claim.
