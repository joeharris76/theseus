# Pinned local review

`theseus local-review` is the pre-submission review a contributor runs before opening a pull request. It reviews the local diff against the consumer's gated base with the same engine, policy and templates CI uses, posts nothing to GitHub, and refuses to proceed on a Critical or High finding. It runs from the installed, hash-verified Theseus artifact; nothing is imported from a checkout, a previous export directory or the ambient environment.

## Running it

Run it through the consumer-owned first stage, so the bytes that execute are the ones the consumer's lock names:

```
python3 -I -S .github/theseus-bootstrap.py run --repo . --commit "$(git rev-parse origin/main)" -- \
  local-review --policy .github/oracle-reviewers.yml [--repo OWNER/NAME] [--base-ref REF] [--head REF] \
  [--rebuttals FILE | --adjudicate TEXT]
```

- The first stage is `bootstrap.py` (standard library only), carried by the consumer at a reviewed, pinned hash. `--commit` must be a full commit SHA of the consumer's gated branch; the installer reads the lock from that commit with `git show` and never from the working tree. See [bootstrap.md](bootstrap.md).
- Any failure to resolve the commit, read or parse the lock, fetch bytes or match a hash refuses before anything executes. There is no fallback to an older install, an export directory or a `theseus` on `PATH`. A warm install is re-verified on every call and is reused only when it matches the lock's artifact, dependencies, interpreter and platform.
- An already verified install can be called directly with the executable that `bootstrap install` printed: `<executable> local-review --policy ...`.

The working directory must be the consumer checkout (a git repository). `--policy` is a path inside it; the policy and every manifest it names are read from the base ref, not from the working tree, so a branch cannot change its own review rules. `--base-ref` defaults to `origin/<consumer.gated_base>`; `--repo` falls back to `ORACLE_LOCAL_REPOSITORY`, then the origin remote.

## Exit status

| Code | Meaning |
|---|---|
| 0 | No Critical or High finding; the pull request may be opened. |
| 1 | Refused: a blocking finding, an unanswered earlier finding, or an invalid option or rebuttal. |
| 2 | Unavailable: no classifier key, unreachable gateway, unreadable trusted inputs. The review never reports success when it could not run. |

`--member` with `--result-json FILE` records a member review for batch consumers (both flags together, full commit SHAs only). The persisted member record schema string is `benchbox_member_review_v1`, unchanged.

## Findings, disputes and the record

Findings carry the identifiers and fingerprints CI assigns to the same patch. A refused review lists them (`[High] c1-D1 path:line title`). To dispute one, write the rebuttals file (a JSON object from finding id to a line starting `Dispute:` that cites `path:line`) and rerun with `--rebuttals FILE`; the same reviewer rejudges it under the evidence rules. A second maintained finding escalates, and only then may the owner pass `--adjudicate`. A withdrawal with a cited disposition clears the finding and is recorded. Set `ORACLE_LOCAL_RECORD_OUT=FILE` to have a passing run write its record for the pull request body; CI ingests it as claims, never as a verdict.

State lives under `~/.cache/theseus/local-oracle` (`THESEUS_LOCAL_STATE` overrides it): `records/`, `captures/`, `cache/`, `member/` and the pool-block file. A consumer that already holds local findings and dispute state keeps them by pointing `THESEUS_LOCAL_STATE` at the existing directory (for example `~/.cache/<consumer>/local-oracle`); formats and finding identifiers are unchanged and nothing is copied or deleted. The review cache key covers the plan, the head tree and a digest of the trusted policy, manifests, omp config and the installed package bytes, so a different package or policy never reuses a result.

## Credentials

- Classifier key: `TYPESAFE_API_KEY` from the environment, or on macOS the keychain item of the same name (`security add-generic-password -s TYPESAFE_API_KEY -a "$USER" -w`).
- Gateway client: `~/.omp/oracle-gateway/local-client.token` against `http://127.0.0.1:4000`, or in a cloud session (`THESEUS_EPHEMERAL_CLONE=1` or a set `ORACLE_CLOUD_GATEWAY_TOKEN`) `ORACLE_GATEWAY_URL` plus `ORACLE_CLOUD_GATEWAY_TOKEN`, reaching a tailnet-only gateway through the loopback proxy recorded in the status file.
- GitHub access: `GH_TOKEN`, `GITHUB_TOKEN` or `GH_ENTERPRISE_TOKEN`, used only for the pull-request lookup.

Every secret above is withheld from the reviewer: at start the process hands them over a pipe to a re-executed copy of itself with the secrets removed from its environment, gives each phase only the grants it needs, scans the work area and redacts captures. Captures keep a redacted excerpt only.

## What changed from the BenchBox launcher

`scripts/oracle_local_review.py` exported `origin/develop` into a cache directory and ran `python -m _project.scripts.oracle_reviewers.cli` from it. The installed command replaces that:

| BenchBox launcher | Installed |
|---|---|
| `--reviewer-ref` (export source) | the lock at `--commit` names the exact artifact |
| `--record-out FILE` | `ORACLE_LOCAL_RECORD_OUT=FILE` |
| implicit `origin/develop` base | `origin/<consumer.gated_base>` from the policy |
| `~/.cache/benchbox/local-oracle` | `~/.cache/theseus/local-oracle`, or the old directory through `THESEUS_LOCAL_STATE` |
| `BENCHBOX_EPHEMERAL_CLONE=1` (cloud session marker) | `THESEUS_EPHEMERAL_CLONE=1` |
| `.git/benchbox-cloud-gateway.status` (cloud gateway proxy record) | `.git/theseus-cloud-gateway.status`, same `gateway_proxy=` line |
| `BENCHBOX_OMP_CACHE`, `BENCHBOX_OMP_DOWNLOAD_MIRROR` | `THESEUS_OMP_CACHE`, `THESEUS_OMP_DOWNLOAD_MIRROR` |
| export directories under `~/.cache/benchbox/local-oracle/export` | inert; never read or executed, and nothing deletes the findings beside them |

The consumer's launcher change, which switches `make` targets and `pr_arm` to the installed command, belongs to the consumer cutover, not to this repository.

## Verification

`tools/smoke_wheel.py` builds the wheel, installs it into a clean virtual environment outside the checkout and runs the local-review scenarios there against fake OMP, gateway and GitHub servers: clean review and capture redaction, blocking finding, rebuttal, withdrawal, escalation, adjudication, forged record, relative rebuttal path, concurrency lock and the cloud reachability cases.
