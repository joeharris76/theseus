# Rotating review credentials

`theseus infra rotate --config .github/review.yml` replaces the credentials the `infra:` section describes. It
prints a plan and changes nothing unless you add `--apply`.

```
theseus infra rotate --config .github/review.yml [--apply] [--only NAME,...] [--resume] [--json]
```

`--only` takes secret names and selects the credential that supplies each (the two Tailscale secrets rotate
together). Names that cannot rotate, such as a literal or the gateway URL, are refused with exit code 2.

## What a rotation does

For each credential, in this order:

1. Create the new credential at the provider and record its id in the state file.
2. Store the value as `NAME.pending`.
3. Set it in every environment recorded for `NAME` (the configured environment when none is recorded yet).
4. Health-check it.
5. Promote it: store it as `NAME` and drop `NAME.pending`.
6. Revoke the old credential.

If steps 2 to 5 fail, the old value is set again from the store, the new credential is deleted at the provider and
the pending item is dropped; the old credential keeps working and the command exits 1. If the rollback itself
cannot finish, the record stays in the state file and `--resume` repeats it.

Before step 2 the old value is copied to `NAME.previous`, and it stays until the old credential is revoked. From
step 5 on, the rotation only rolls forward: the record moves to `promoting` before the new value reaches the store, and
`--resume` finishes a `promoting` or `promoted` record instead of undoing it, so an interrupt (Ctrl-C included) never
deletes the credential CI is using. A record still in `created` is rolled back from `NAME.previous`.

Each credential is health-checked directly before it is set in any environment, and again after.

Before anything is created, `--apply` reads the configured environment and every environment recorded for the
credentials from GitHub and refuses the whole run (making no change) if any allows deployments from every branch
or is outside the configured repository; each secret write repeats the check. Rotation needs POSIX file locking, so
`--apply` is refused on Windows while the rest of the CLI still loads.

`--apply` and `--resume` hold an exclusive lock on `<state file>.lock`; a second run is refused, not queued.

If step 6 fails, the new credential stays live, the old one stays recorded in the state file and the command exits
1. Fix the cause and run `rotate --resume`, which revokes the old credential and nothing else. A plain `rotate` is
refused while a record is unfinished. `--resume` never creates a credential.

## Credentials

| Credential | Create | Health check | Revoke old |
| --- | --- | --- | --- |
| Gateway token | the new token becomes a temporary client `<client>-next`; the host script's `gateway` step registers it | `/healthz` and `/v1/models` on 127.0.0.1 and `/v1/models` on the tailnet URL | the swap into the review client's `client.token` and a second `gateway` step stop the old token; the old token must then get a 401 from the gateway, or the revoke fails |
| Tailscale CI OAuth client | a new tagged client with the configured scopes | client-credentials token exchange | delete the old client |
| OpenAI service-account key | a further key on the recorded service account | `GET /v1/models` | delete the old key |
| Typesafe key | none: the owner creates it in the console | `GET /v1/models` | printed step: revoke the old key in the console |
| GitHub App private key | none: the owner generates it in the App settings | validated by signing a JWT and calling `GET /app` | printed steps: delete the old key in the App settings and the adopted `.pem` file |

Typesafe and GitHub offer no API to create or revoke these two, so they rotate by pickup.

- **Typesafe.** Only a `store` secret declared `rotator: typesafe` is treated this way (`rotator` is required on every `store`
  secret). A secret declared `rotator: owner` is
  owner-seeded: `rotate` reports its age and that it is re-seeded with `theseus infra seed NAME`, and never adopts a
  `.next` value for it or sends it to a provider, and reports any leftover `NAME.pending` or `NAME.previous` item.
  Each unfinished-rotation record names its secrets; a record left for other secrets, or for a rotation kind no secret
  uses any more, is reported as `stale` (exit 1) and never resumed. Seed the new key with `theseus infra seed TYPESAFE_API_KEY.next`, or put it in the Keychain item
  named by `mirror_keychain`. `rotate` adopts a value that differs from the stored one, sets it, checks it, stores it
  and removes `.next`. A key found in the Keychain item is adopted only when that item is newer than the stored key, and after an adoption the item is updated through stdin. With nothing to adopt it reports the key's age against `max_age_days` and the manual steps.
- **GitHub App key.** The age comes from the secret's `updated_at`. When the newest `<slug>.*.private-key.pem` in
  `pem_dir` is newer than the secret, it is validated before anything changes, then set and stored. Otherwise
  `rotate` reports the age and, past `max_age_days`, the manual step.

## Secrets stay out of the output

Values travel only on stdin or in HTTP bodies. Every message, the plan and the state file are scrubbed of every
value in the store and of every value created in the run; the state file holds provider ids and phases, never values.

## Exit codes

0 every credential rotated (a Tailscale or OpenAI rotation with no recorded old id is reported as `rotated-nothing-revoked`), was resumed or had nothing to do; 1 a credential failed, was rolled back or has an
unrevoked old credential; 2 the configuration, a read or a `--only` name is unusable.
