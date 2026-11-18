"""Build the setup plan: what is missing between the configuration, the store, the providers and GitHub.

Reads are free in a dry run; every mutation is an action that only runs with ``--apply``. A second run after a
successful apply finds nothing missing and plans nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import app_key, gateway, github_env, openai_admin, tailscale, typesafe
from .config import (
    APP_KEY,
    GATEWAY_TOKEN,
    GATEWAY_URL,
    LITERAL,
    OPENAI_SERVICE_ACCOUNT,
    STORE,
    TAILSCALE_OAUTH,
    TYPESAFE_ROTATOR,
    SecretSpec,
)
from .plan import Context, Plan
from .store import Secret, check_name
from .transport import HttpTransport

ENV_ACTION = "github.environment"
POLICY_ACTION = "tailscale.policy"
GATEWAY_ACTION = "gateway.obtain"
OAUTH_ACTION = "tailscale.oauth-client"
REVOKE_ACTION = "tailscale.revoke-previous"
STATE_RETIRED = "tailscale_retired"
STATE_OPENAI = "openai"
STATE_TAILSCALE = "tailscale"
STATE_APP_KEY = "app_key"

Verifier = Callable[[HttpTransport, Secret], bool]
DAY_SECONDS = 86400.0


def seed_command(name: str) -> str:
    return f"theseus infra seed {name} --config <policy file>"


def _keep_if_changed(ctx: Context, name: str, value: Secret) -> None:
    current = ctx.secret(name)
    if current is None or current != value:
        ctx.keep(name, value)


def _only_error(ctx: Context, only: set[str]) -> None:
    unknown = sorted(only - {spec.name for spec in ctx.config.secrets})
    if unknown:
        raise ValueError(f"--only names {unknown[0]}, which is not in infra.secrets")


def build_setup(ctx: Context, only: set[str] | None = None) -> Plan:
    if only is not None:
        _only_error(ctx, only)
    config = ctx.config
    plan = Plan()
    selected = [spec for spec in config.secrets if only is None or spec.name in only]
    oauth_names = {spec.name for spec in config.secrets if spec.source == TAILSCALE_OAUTH}
    if only is not None and only & oauth_names and not oauth_names <= only:
        raise ValueError(
            f"--only must name every tailscale_oauth secret together ({', '.join(sorted(oauth_names))}): "
            "they are one client"
        )
    environment = github_env.get_environment(ctx)
    remote = github_env.list_secrets(ctx) if environment is not None else {}

    env_action: tuple[str, ...] = ()
    env = config.github.environment
    if environment is None:
        plan.add(
            ENV_ACTION,
            f"create environment {env} in {config.github.repository} with custom deployment branch policies "
            "(an environment is never created open to every branch)",
            lambda: github_env.ensure_environment(ctx, None, True),
        )
        env_action = (ENV_ACTION,)
    elif only is None and not github_env.has_custom_policies(environment):
        before = environment.get("deployment_branch_policy")
        was = (
            "protected branches only"
            if isinstance(before, dict) and before.get("protected_branches")
            else "all branches"
        )
        plan.add(
            ENV_ACTION,
            f"change environment {env} in {config.github.repository} from {was} to custom deployment branch "
            f"policies; its protections are {github_env.describe_protections(environment)}",
            lambda: github_env.ensure_environment(ctx, environment, True),
        )
        env_action = (ENV_ACTION,)
    if only is None or environment is None:
        have = set(github_env.branch_policies(ctx)) if environment is not None else set()
        for branch in config.github.branch_policies:
            if branch not in have:
                plan.add(
                    f"github.branch-policy:{branch}",
                    f"allow deployments from branch {branch}",
                    lambda branch=branch: github_env.add_branch_policy(ctx, branch),
                    env_action,
                )

    # A full run can switch an environment that has no custom policies; a custom list that is a wildcard or
    # unreadable is never changed automatically, so it stops the run before any credential is created.
    open_now = environment is not None and not github_env.is_restricted(ctx, environment)
    if open_now and (only is not None or github_env.has_custom_policies(environment or {})):
        plan.block(
            "github.environment",
            f"environment {env} allows deployments from every branch (a wildcard branch policy, or a policy list that "
            "cannot be read), so no credential is created and no secret is delivered to it; "
            + (
                "remove the wildcard policy by hand in the environment's settings, then run setup again"
                if only is None
                else "run setup without --only to restrict it"
            ),
        )
        # Nothing is created that could not be delivered: no key, client or revocation is planned.
        return plan
    producers: dict[str, str] = {}
    _tailnet(ctx, plan, selected, only is None, producers)
    for spec in selected:
        if spec.source == LITERAL:
            _literal(ctx, plan, spec, producers)
        elif spec.source == OPENAI_SERVICE_ACCOUNT:
            _openai(ctx, plan, spec, producers)
        elif spec.source == APP_KEY:
            _app_key(ctx, plan, spec, producers)
        elif spec.source == STORE and ctx.secret(spec.name) is None:
            plan.block(f"seed:{spec.name}", f"not in the store; run: {seed_command(spec.name)}")
    _gateway(ctx, plan, selected, producers)

    for spec in selected:
        _deliver(ctx, plan, spec, producers, env_action, environment is not None, remote)
    for action in [action for action in plan.actions if action.id == REVOKE_ACTION]:
        plan.actions.remove(action)
        needs: list[str] = []
        ready = True
        for name in sorted(oauth_names):
            if plan.has(f"deliver:{name}"):
                needs.append(f"deliver:{name}")
            elif not (
                name in remote
                and ctx.secret(name) is not None
                and ctx.state.delivered(name, config.environment_key) >= max(ctx.state.version(name), 1)
            ):
                ready = False
                break
        if ready:  # revoke only once both OAuth secrets are verified delivered, or their deliveries are planned
            action.requires = tuple(needs)
            action.gated = bool(needs)
            plan.actions.append(action)
    for line in aged(ctx):
        plan.note(line)
    return plan


def _tailnet(
    ctx: Context, plan: Plan, selected: list[SecretSpec], with_policy: bool, producers: dict[str, str]
) -> None:
    config = ctx.config
    tailnet = config.tailnet
    oauth = [spec for spec in selected if spec.source == TAILSCALE_OAUTH]
    if tailnet is None or not (oauth or with_policy):
        return
    try:
        client: tailscale.TailscaleClient | None = tailscale.TailscaleClient.from_root(ctx)
    except tailscale.TailscaleError as exc:
        client = None
        reason = f"needs the Tailscale root OAuth client: {exc}"
        plan.block(POLICY_ACTION, reason)
        if oauth:
            plan.block(OAUTH_ACTION, reason)
    policy_planned = False
    if client is not None:
        text, _ = client.get_policy()
        lacking = tailscale.missing(tailscale.standardize(text), tailnet)
        if lacking:
            policy_planned = True
            plan.add(
                POLICY_ACTION,
                f"add {', '.join(lacking)} for {tailnet.tag} to the tailnet policy file (validated before saving)",
                lambda: _save_policy(ctx),
            )
    if not oauth or client is None:
        return
    stored = {spec.name: ctx.secret(spec.name) for spec in oauth}
    stale = any(value is None for value in stored.values())
    if not stale:
        for spec in oauth:
            if spec.field == "id" and client.get_key(stored[spec.name].reveal()) is None:  # type: ignore[union-attr]
                stale = True
    if stale:
        plan.add(
            OAUTH_ACTION,
            f"create the OAuth client {tailnet.oauth_client.description} (scopes {', '.join(tailnet.oauth_client.scopes)}, "
            f"tag {tailnet.tag}) and keep its id and secret in the store",
            lambda: _create_oauth(ctx, oauth),
            (POLICY_ACTION,) if policy_planned else (),
        )
        for spec in oauth:
            producers[spec.name] = OAUTH_ACTION
    retired = _retired(ctx)
    if retired or (stale and ctx.state.provider(STATE_TAILSCALE).get("client_id")):
        plan.add(
            REVOKE_ACTION,
            "revoke the previous OAuth client once every OAuth secret is delivered",
            lambda: _revoke_retired(ctx),
            gated=True,
        )


def _save_policy(ctx: Context) -> None:
    tailnet = ctx.config.tailnet
    assert tailnet is not None
    client = tailscale.TailscaleClient.from_root(ctx)
    text, etag = client.get_policy()
    edited = tailscale.edit_policy(text, tailnet)
    if edited == text:
        return
    client.validate_policy(edited)
    client.save_policy(edited, etag)


def _create_oauth(ctx: Context, specs: list[SecretSpec]) -> None:
    tailnet = ctx.config.tailnet
    assert tailnet is not None
    client = tailscale.TailscaleClient.from_root(ctx)
    client_id, secret = client.create_oauth_client(
        tailnet.oauth_client.description, tailnet.oauth_client.scopes, (tailnet.tag,)
    )
    previous = ctx.state.provider(STATE_TAILSCALE).get("client_id")
    try:
        for spec in specs:
            ctx.keep(spec.name, Secret(client_id) if spec.field == "id" else secret)
        ctx.state.set_provider(STATE_TAILSCALE, client_id=client_id)
        ctx.save_state()
    except (RuntimeError, OSError):
        client.delete_key(client_id)
        raise
    if previous and previous != client_id:
        ctx.state.set_provider(STATE_RETIRED, **{str(i): cid for i, cid in enumerate([*_retired(ctx), previous])})
        ctx.save_state()


def _retired(ctx: Context) -> list[str]:
    record = ctx.state.provider(STATE_RETIRED)
    return [record[key] for key in sorted(record, key=int)]


def _revoke_retired(ctx: Context) -> None:
    """Revoke every retired client, recording each as it goes so a failure part-way loses none."""
    client = tailscale.TailscaleClient.from_root(ctx)
    remaining = _retired(ctx)
    while remaining:
        client.delete_key(remaining[0])
        remaining.pop(0)
        ctx.state.clear_provider(STATE_RETIRED)
        if remaining:
            ctx.state.set_provider(STATE_RETIRED, **{str(i): cid for i, cid in enumerate(remaining)})
        ctx.save_state()


def _literal(ctx: Context, plan: Plan, spec: SecretSpec, producers: dict[str, str]) -> None:
    current = ctx.secret(spec.name)
    if current is not None and current.reveal() == spec.value:
        return
    value = spec.value or ""
    producers[spec.name] = plan.add(
        f"literal:{spec.name}",
        f"keep the configured value of {spec.name} in the store",
        lambda: ctx.keep(spec.name, Secret(value)),
    )


def _openai(ctx: Context, plan: Plan, spec: SecretSpec, producers: dict[str, str]) -> None:
    if ctx.secret(spec.name) is not None:
        return
    if ctx.secret(openai_admin.ADMIN_KEY_NAME) is None:
        plan.block(
            f"openai:{spec.name}",
            f"needs the Admin API key; run: {seed_command(openai_admin.ADMIN_KEY_NAME)}",
        )
        return
    producers[spec.name] = plan.add(
        f"openai:{spec.name}",
        f"create a key for service account {spec.account_name} in project {spec.project} and keep it in the store",
        lambda: _create_openai(ctx, spec),
    )


def _create_openai(ctx: Context, spec: SecretSpec) -> None:
    assert spec.project is not None and spec.account_name is not None
    admin = openai_admin.OpenAIAdmin(ctx)
    known = ctx.state.provider(STATE_OPENAI).get("service_account_id")
    created = (
        admin.mint_key(spec.project, known, spec.account_name)
        if known
        else admin.create_service_account(spec.project, spec.account_name)
    )
    try:
        if not openai_admin.check_key(ctx.http, created.secret):
            raise openai_admin.OpenAIError("OpenAI does not accept the new key")
        ctx.keep(spec.name, created.secret)
        ctx.state.set_provider(STATE_OPENAI, service_account_id=created.service_account_id or "", key_id=created.key_id)
        ctx.save_state()
    except (RuntimeError, OSError):
        admin.delete_key(spec.project, created.key_id)
        raise


def _app_key(ctx: Context, plan: Plan, spec: SecretSpec, producers: dict[str, str]) -> None:
    pem = app_key.newest_pem(spec)
    recorded = ctx.state.provider(STATE_APP_KEY).get("pem")
    stored = ctx.secret(spec.name) is not None
    if pem is None:
        if not stored:
            plan.block(
                f"app-key:{spec.name}",
                f"no key yet: generate a private key in the App's settings and save it as {spec.app_slug}.<date>."
                f"private-key.pem in {spec.pem_dir}, or run: {seed_command(spec.name)} --from-file PATH",
            )
        return
    if stored and recorded == app_key.fingerprint(pem):
        return
    producers[spec.name] = plan.add(
        f"app-key:{spec.name}",
        f"pick up {pem.name}, check it against App {spec.app_slug} and keep it in the store; "
        f"afterwards delete {pem} (it stays readable in {pem.parent})",
        lambda: _pick_up_app_key(ctx, spec, pem),
    )


def _pick_up_app_key(ctx: Context, spec: SecretSpec, pem: Any) -> None:
    app_id = ctx.config.secret(spec.app_id_secret or "").value or ""
    value = Secret(pem.read_text(encoding="utf-8"))
    ctx.redactor.add(value.reveal(), *value.reveal().splitlines())
    app_key.validate(ctx, value, app_id, spec.app_slug or "")
    ctx.keep(spec.name, value)
    ctx.state.set_provider(STATE_APP_KEY, pem=app_key.fingerprint(pem))
    ctx.save_state()


def _gateway(ctx: Context, plan: Plan, selected: list[SecretSpec], producers: dict[str, str]) -> None:
    specs = [spec for spec in selected if spec.source in (GATEWAY_TOKEN, GATEWAY_URL)]
    if not specs or all(ctx.secret(spec.name) is not None for spec in specs):
        return
    try:
        script = gateway.script_path(ctx.config)
    except gateway.GatewayError as exc:
        plan.block(GATEWAY_ACTION, str(exc))
        return
    if not script.is_file():
        plan.block(GATEWAY_ACTION, f"the gateway host script {script} does not exist")
        return
    plan.add(
        GATEWAY_ACTION,
        f"run '{script.name} gateway' on this host and keep the client token and published URL in the store",
        lambda: _obtain_gateway(ctx, specs),
    )
    for spec in specs:
        producers[spec.name] = GATEWAY_ACTION


def _obtain_gateway(ctx: Context, specs: list[SecretSpec]) -> None:
    token, url = gateway.obtain(ctx)
    for spec in specs:
        _keep_if_changed(ctx, spec.name, token if spec.source == GATEWAY_TOKEN else Secret(url))


def _deliver(
    ctx: Context,
    plan: Plan,
    spec: SecretSpec,
    producers: dict[str, str],
    env_action: tuple[str, ...],
    environment_exists: bool,
    remote: dict[str, Any],
) -> None:
    name = spec.name
    producer = producers.get(name)
    if producer is None:
        if ctx.secret(name) is None:
            return
        current = max(ctx.state.version(name), 1)
        up_to_date = (
            environment_exists and name in remote and ctx.state.delivered(name, ctx.config.environment_key) >= current
        )
        if up_to_date:
            return
    env = ctx.config.github.environment
    plan.add(
        f"deliver:{name}",
        f"set {name} in environment {env} of {ctx.config.github.repository}",
        lambda: _set_in_environment(ctx, spec),
        (*env_action, *((producer,) if producer else ())),
    )


def _verifier(spec: SecretSpec) -> Verifier | None:
    """The provider check for a secret, chosen by what it is declared to be, never by its name."""
    if spec.source == OPENAI_SERVICE_ACCOUNT:
        return openai_admin.check_key
    if spec.source == STORE and spec.rotator == TYPESAFE_ROTATOR:
        return typesafe.check_key
    return None


def _set_in_environment(ctx: Context, spec: SecretSpec) -> None:
    value = ctx.secret(spec.name)
    if value is None:
        raise RuntimeError(f"{spec.name} is not in the store")
    if not github_env.is_restricted(ctx, github_env.get_environment(ctx)):
        raise RuntimeError(f"environment {ctx.config.github.environment} allows every branch; {spec.name} was not set")
    verify: Verifier | None = _verifier(spec)
    if verify is not None and not verify(ctx.http, value):
        raise RuntimeError(f"the provider rejects the stored {spec.name}; nothing was set")
    github_env.set_secret(ctx, spec.name, value)
    ctx.state.mark_delivered(spec.name, ctx.config.environment_key)
    ctx.save_state()


def aged(ctx: Context) -> list[str]:
    lines: list[str] = []
    for spec in ctx.config.secrets:
        if spec.max_age_days is None:
            continue
        modified = ctx.store.modified_at(spec.name)
        if modified is None:
            continue
        days = (ctx.now() - modified).total_seconds() / DAY_SECONDS
        if days > spec.max_age_days:
            if spec.source == APP_KEY:
                how = "generate a new private key in the App's settings and save it in the key directory"
            elif spec.source == STORE and spec.rotator == TYPESAFE_ROTATOR:
                how = f"create a new key in the Typesafe console and run: {seed_command(spec.name)}"
            elif spec.source == STORE:
                how = f"re-seed it with: {seed_command(spec.name)}"
            else:
                how = f"create a new key in the provider's console and run: {seed_command(spec.name)}"
            lines.append(f"aged: {spec.name} is {int(days)} days old (limit {spec.max_age_days}); {how}")
    return lines


def report(ctx: Context) -> list[dict[str, Any]]:
    """One row per configured secret, from the store, the state file and GitHub. No values."""
    environment = github_env.get_environment(ctx)
    remote = github_env.list_secrets(ctx) if environment is not None else {}
    rows: list[dict[str, Any]] = []
    for spec in ctx.config.secrets:
        modified = ctx.store.modified_at(spec.name)
        updated = remote.get(spec.name)
        rows.append(
            {
                "name": spec.name,
                "source": spec.source,
                "in_store": modified is not None,
                "stored_at": modified.isoformat() if modified else None,
                "version": ctx.state.version(spec.name),
                "delivered_version": ctx.state.delivered(spec.name, ctx.config.environment_key),
                "in_environment": spec.name in remote,
                "environment_updated_at": updated.isoformat() if updated else None,
            }
        )
    return rows


def seed(ctx: Context, name: str, value: Secret) -> None:
    """Store an owner-supplied value. A configured secret becomes due for delivery; a ``.next`` pickup is held."""
    check_name(name)
    if not value.reveal().strip():
        raise ValueError("the value is empty")
    base = name.removesuffix(".next")
    names = {spec.name for spec in ctx.config.secrets}
    root_names = {tailscale.ROOT_ID_NAME, tailscale.ROOT_SECRET_NAME, openai_admin.ADMIN_KEY_NAME}
    if base not in names | root_names:
        raise ValueError(f"{base} is neither in infra.secrets nor a root credential")
    if name in names:
        ctx.keep(name, value)
    else:
        ctx.redactor.add(value.reveal())
        ctx.store.put(name, value)
