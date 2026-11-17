# Review infrastructure: `theseus infra`

`theseus infra` creates and checks the infrastructure a consumer's hosted review needs: a GitHub environment with its deployment branch policies and secrets, the tailnet policy and OAuth client the runner joins with, the gateway token and URL, the OpenAI service account key and the GitHub App key. It is a command for the owner's machine, never for CI.

The gate, verdict, plan and policy trust do not read any of it. The policy parser accepts the top-level `infra:` key and ignores it, so a policy with or without the section reviews identically.

## Safety rules

- **Dry run is the default.** `setup` and `status` read and print a plan; only `setup --apply` changes anything.
- **Values never reach argv, a log or a file other than the store.** A value travels on a child's stdin (`gh secret set`, `openssl`, `security -i`), in an HTTP body or in an authorization header. Output, the plan, the state file and errors are scrubbed against every value in the store.
- **GitHub secrets cannot be read back.** Every value comes from the store, the gateway host or a provider.

## Configuration

Add an `infra:` section to the consumer's policy file and pass the file with `--config`:

```yaml
infra:
  store: {backend: keychain}            # or {backend: file, dir: ~/.config/theseus/infra}
  state: ~/.config/theseus/infra-state/example.json   # optional; this is derived from repository and environment by default
  github:
    repository: Example-Org/example
    environment: review
    branch_policies: [develop, "release/*"]     # deployment branch policies; omit for none
  tailnet:
    name: "-"                                     # optional; "-" is the default tailnet
    tag: tag:review-ci
    tag_owners: [autogroup:admin]
    gateway_port: 4000
    deny_port: 22
    gateway_dst: [tag:review-gateway]             # who the tag may reach on gateway_port; required
    oauth_client: {scopes: [auth_keys], description: example-review-ci}
  gateway:
    host_script: scripts/gateway.sh               # relative to the repository root; must stay inside it
    client: review-ci                             # review-ci reads client.token; review-<n> reads <n>-client.token
    dir: ~/.omp/review-gateway                    # optional; where the script keeps token files
  secrets:
    REVIEW_APP_ID: {source: literal, value: "123456"}
    REVIEW_APP_PRIVATE_KEY: {source: app_key, app_slug: example-app, pem_dir: ~/Downloads, max_age_days: 180}
    REVIEW_GATEWAY_TOKEN: {source: gateway_token}
    REVIEW_GATEWAY_URL: {source: gateway_url}
    TS_OAUTH_CI_CLIENT_ID: {source: tailscale_oauth, field: id}
    TS_OAUTH_CI_SECRET: {source: tailscale_oauth, field: secret}
    OPENAI_API_KEY: {source: openai_service_account, project: proj_abc, name: review-classifier}
    TYPESAFE_API_KEY: {source: store, rotator: manual, max_age_days: 90, mirror_keychain: TYPESAFE_API_KEY}
```

Unknown keys are refused. Sources:

| Source | What `setup` does |
|---|---|
| `literal` | Keeps the configured value in the store and delivers it. |
| `app_key` | Picks up the newest `<app_slug>.*.private-key.pem` in `pem_dir`, signs a JWT with it and requires `GET /app` to return the App with the id in the numeric literal secret named by `app_id_secret` (default `REVIEW_APP_ID`) before storing it. A symlink is never picked up, and a key file already picked up (same name and modification time) is not picked up again. The plan reminds you to delete the `.pem` afterwards; it stays readable in `pem_dir`. |
| `gateway_token`, `gateway_url` | Runs `<host_script> gateway` (the consumer's script, unchanged), reads the token file it leaves and the `gateway: published at https://…` line it prints. |
| `tailscale_oauth` | Creates one OAuth client with the configured scopes and tag, and keeps its id and secret. The client is created only after the policy has the tag. |
| `openai_service_account` | Creates the service account in the project (or mints another key if one was recorded), checks the key with `GET /v1/models`, and keeps it. A key OpenAI rejects is revoked and not stored. |
| `store` | The owner supplies the value with `seed`. `max_age_days` makes `status` report its age. `rotator: manual` is the only rotator. |

`mirror_keychain` names the legacy Keychain service the same key is also kept under; setup parses it and does not read that item.

### Stores

- `keychain`: service `theseus.infra`, one account per name, written by feeding `security -i` on stdin and read back. A value that is not plain printable text is stored base64-encoded behind `b64:`. `security -i` reads at most 4096 bytes of a line, so a value over 2000 characters is stored in numbered parts (`NAME#<id>#<n>`) written first and a short header under `NAME` written last: a failed write leaves the old value intact.
- `file`: a `0700` directory with one `0600` file per name, written atomically. A wider mode is refused, not repaired.

The state file holds ids and versions only (which OAuth client and service account exist, which version of each secret each environment received), never a value. Losing it is safe: the next `setup --apply` sets every secret again.

## Commands

```
theseus infra setup  --config PATH [PATH…] [--apply] [--only NAME,…] [--json]
theseus infra status --config PATH [PATH…] [--only NAME,…] [--json]
theseus infra seed   NAME --config PATH [--from-file PATH]
```

`setup` builds the plan from read-only calls: the environment and its branch policies, the tailnet policy file, which secrets are in the store and which the environment already has at the current version. With `--apply` it runs the actions in order. An action whose prerequisite failed is skipped, not attempted. **A second `setup` after a clean apply plans no actions.** `--only` limits the plan to the named secrets (and what they need).

The tailnet policy is edited as text, so comments and layout survive. Setup adds only what is missing: the tag in `tagOwners`, a grant from the tag to `gateway_dst` on `gateway_port`, and a test that the tag can reach `gateway_port` and cannot reach `deny_port`. Tailscale validates the edit (which runs the tests) before it is saved, and the save carries `If-Match` with the ETag that was read; a policy changed in between is refused.

An existing environment that already uses custom deployment branch policies is left alone. One that does not is switched with a `PUT`, which GitHub treats as a replacement of the environment's protection settings, so setup reads the environment first and sends back its required reviewers, wait timer, prevent-self-review and admin-bypass settings. The plan line says what the switch changes (for example from protected branches only to custom policies) and which protections are kept. Check the environment in GitHub afterwards.

`--only` never switches an existing environment. An environment is never left open to every branch: a missing one is always created with custom deployment branch policies and the configured branches (even under `--only`), delivery re-checks that the environment is restricted and refuses otherwise, and `status` reports one with no branch restriction. The two `tailscale_oauth` secrets are one client: `--only` must name both or neither. A replaced OAuth client is revoked only after both secrets are delivered, so a failed delivery leaves the old client working.

Exit codes: `0` done or nothing to do; `1` an action failed, or `status` found drift (something due, blocked or aged); `2` unusable configuration, read or input; `3` owner action is still needed.

### Seeding

`seed` reads the value from a hidden prompt, from stdin when stdin is not a terminal, or from `--from-file`. It never takes a value in argv. A value on one line loses its trailing newline; a multi-line value (a PEM) is kept exactly.

Seed the root credentials once. Each prompts for the value:

```sh
theseus infra seed TAILSCALE_ROOT_CLIENT_ID     --config .github/review.yml
theseus infra seed TAILSCALE_ROOT_CLIENT_SECRET --config .github/review.yml
theseus infra seed OPENAI_ADMIN_KEY             --config .github/review.yml
security find-generic-password -s TYPESAFE_API_KEY -a "$USER" -w | theseus infra seed TYPESAFE_API_KEY --config .github/review.yml
theseus infra seed REVIEW_APP_PRIVATE_KEY --config .github/review.yml --from-file ~/Downloads/example-app.2026-11-14.private-key.pem
```

With the Keychain store the root credentials can also be added without Theseus; leave `-w` last so `security` prompts for the value:

```sh
security add-generic-password -U -s theseus.infra -a TAILSCALE_ROOT_CLIENT_ID -w
```

`seed NAME.next` stores a value that is held for pickup (a manual rotation) and does not make `NAME` due for delivery. Only names in `infra.secrets`, their `.next` forms and the three root credentials can be seeded.

The Tailscale root is an OAuth client created in the admin console with the scopes `oauth_keys`, `policy_file`, `devices:core:read` and `devices:posture_attributes`; its access tokens last one hour. The OpenAI root is an organization Admin API key.

## Owner actions that have no API

| Action | Why | Where |
|---|---|---|
| Create the first Tailscale root OAuth client | Nothing exists to authorise creating it | Tailscale admin console |
| Create the first OpenAI Admin API key | Same | OpenAI console |
| Create and revoke Typesafe keys | The Typesafe API has no key management; setup checks a key with `GET /v1/models` | Typesafe console |
| Generate and delete the GitHub App private key | GitHub has no REST endpoint for App private keys | App settings → Private keys |
| Rename the App, change its permissions | UI only | App settings |

An App holds up to 25 keys, so a new key can be generated and picked up before the old one is deleted.

## Recovery

- **`setup` fails part-way.** Run it again. Each action records only what it finished; the failed one is planned again, and what it needed from an earlier step is already in the store.
- **A secret was changed in GitHub by hand.** `setup` cannot see values. Reseed or re-run the source, which marks the secret due, then `setup --apply`.
- **A created credential was not stored.** An OAuth client or OpenAI key is revoked when the store or state write after creating it fails; a stale OAuth client is deleted once its replacement is stored. If a process dies between creating and storing, delete the orphan in the provider's console (the Tailscale description and the OpenAI service account name identify it).
- **The state file is lost.** Run `setup --apply`; every secret is set again with the stored value.
- **A Keychain item is damaged.** `theseus infra seed NAME` writes it again.
- **Policy edit refused.** The message is Tailscale's validation text, usually a failing policy test (for example an existing grant already lets the tag reach `deny_port`). Fix the policy by hand; setup never removes a rule.

Rotation is a separate command.
