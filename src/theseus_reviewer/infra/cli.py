"""``theseus infra``: setup, status and seed. Dry run is the default; ``--apply`` is the only way anything changes.

Exit codes: 0 done (or nothing to do); 1 an action failed, or ``status`` found drift; 2 the configuration, a read
or an input is unusable; 3 owner action is still needed (a blocked item).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import github_env, rotate_command, setup
from .config import ConfigError
from .plan import OK, Context, Plan, open_context
from .store import Secret, StoreError
from .transport import HttpTransport, Redactor, Runner

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNUSABLE = 2
EXIT_BLOCKED = 3
MAX_SEED_BYTES = 65536

build_context = open_context


@dataclass
class Dependencies:
    """The seams ``theseus infra`` runs through. Tests replace these; nothing else reaches the network or a prompt."""

    http: HttpTransport | None = None
    run: Runner | None = None
    now: Callable[[], datetime] | None = None
    prompt: Callable[[str], str] = getpass.getpass
    stdin: TextIO | None = None
    seen: list[Context] = field(default_factory=list)


DEPENDENCIES = Dependencies()


def _open(path: str) -> Context:
    ctx = open_context(Path(path), DEPENDENCIES.http, DEPENDENCIES.run, DEPENDENCIES.now)
    DEPENDENCIES.seen.append(ctx)
    return ctx


def _fail(message: str, redactor: Redactor | None = None) -> int:
    print(f"error: {redactor.scrub(message) if redactor else message}", file=sys.stderr)
    return EXIT_UNUSABLE


def _only(args: argparse.Namespace) -> set[str] | None:
    return {name for name in args.only.split(",") if name} if args.only else None


def _plan(ctx: Context, args: argparse.Namespace) -> Plan:
    return setup.build_setup(ctx, _only(args))


def command_setup(args: argparse.Namespace) -> int:
    code = EXIT_OK
    for path in args.config:
        ctx: Context | None = None
        try:
            ctx = _open(path)
            plan = _plan(ctx, args)
            outcomes = plan.execute(ctx.redactor) if args.apply and plan.actions else None
        except (ConfigError, StoreError, RuntimeError, OSError, ValueError) as exc:
            return _fail(str(exc), ctx.redactor if ctx else None)
        print(plan.render(ctx.redactor, args.json, outcomes, {"config": path, "applied_changes": bool(outcomes)}))
        if not args.apply and plan.actions and not args.json:
            print("dry run: nothing was changed; rerun with --apply to carry out the actions above")
        if outcomes and any(item.status != OK for item in outcomes):
            code = max(code, EXIT_FAILED)
        elif plan.blocked:
            code = max(code, EXIT_BLOCKED)
    return code


def command_status(args: argparse.Namespace) -> int:
    code = EXIT_OK
    for path in args.config:
        ctx: Context | None = None
        try:
            ctx = _open(path)
            plan = _plan(ctx, args)
            rows = setup.report(ctx)
        except (ConfigError, StoreError, RuntimeError, OSError, ValueError) as exc:
            return _fail(str(exc), ctx.redactor if ctx else None)
        restricted = github_env.is_restricted(ctx, github_env.get_environment(ctx))
        drift = bool(
            not restricted or plan.actions or plan.blocked or any(note.startswith("aged:") for note in plan.notes)
        )
        if args.json:
            data = {
                "config": path,
                "environment_restricted": restricted,
                "secrets": rows,
                "drift": drift,
                **plan.to_json(),
            }
            print(ctx.redactor.scrub(json.dumps(data, indent=2, sort_keys=True)))
        else:
            lines = [f"{path}: {ctx.config.github.repository} environment {ctx.config.github.environment}"]
            if not restricted:
                lines.append(
                    "  environment has no branch restriction: deployments from every branch can read its secrets"
                )
            for row in rows:
                stored = row["stored_at"] or "not in the store"
                delivered = "set" if row["in_environment"] else "not set"
                current = "current" if row["delivered_version"] >= max(row["version"], 1) else "stale"
                lines.append(f"  {row['name']} ({row['source']}): stored {stored}; environment {delivered}, {current}")
            print(ctx.redactor.scrub("\n".join(lines)))
            print(plan.render(ctx.redactor))
        if drift:
            code = max(code, EXIT_FAILED)
    return code


def _read_value(args: argparse.Namespace) -> str:
    if args.from_file:
        text = Path(args.from_file).read_text(encoding="utf-8")
    else:
        stdin = DEPENDENCIES.stdin or sys.stdin
        text = stdin.read() if not stdin.isatty() else DEPENDENCIES.prompt(f"Value for {args.name} (input is hidden): ")
    if len(text.encode()) > MAX_SEED_BYTES:
        raise ValueError("the value is too large")
    return text if "\n" in text.rstrip("\r\n") else text.rstrip("\r\n")


def command_seed(args: argparse.Namespace) -> int:
    ctx: Context | None = None
    try:
        ctx = _open(args.config)
        value = Secret(_read_value(args))
        setup.seed(ctx, args.name, value)
    except (ConfigError, StoreError, RuntimeError, OSError, ValueError) as exc:
        return _fail(str(exc), ctx.redactor if ctx else None)
    print(f"seeded {args.name}")
    return EXIT_OK


def _plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", nargs="+", required=True, metavar="PATH", help="policy file(s) with an infra: section"
    )
    parser.add_argument("--only", default="", help="comma-separated secret names to limit the plan to")
    parser.add_argument("--json", action="store_true")


def _register_setup(sub: Any) -> None:
    parser = sub.add_parser("setup", help="plan, and with --apply create, the environment, tailnet policy and secrets")
    _plan_options(parser)
    parser.add_argument("--apply", action="store_true", help="carry out the plan; without it nothing changes")
    parser.set_defaults(handler=command_setup)


def _register_status(sub: Any) -> None:
    parser = sub.add_parser("status", help="show what is stored, delivered and due, changing nothing")
    _plan_options(parser)
    parser.set_defaults(handler=command_status)


def _register_seed(sub: Any) -> None:
    parser = sub.add_parser("seed", help="store a value the owner supplies; read from a prompt or stdin, never argv")
    parser.add_argument("name")
    parser.add_argument("--config", required=True, metavar="PATH")
    parser.add_argument("--from-file", default="", metavar="PATH")
    parser.set_defaults(handler=command_seed)


SUBCOMMANDS: list[Callable[[Any], None]] = [_register_setup, _register_status, _register_seed, rotate_command.register]


def register(commands: Any) -> None:
    infra = commands.add_parser("infra", help="set up and check a consumer's review infrastructure")
    sub = infra.add_subparsers(dest="infra_command", required=True)
    for register_command in SUBCOMMANDS:
        register_command(sub)
