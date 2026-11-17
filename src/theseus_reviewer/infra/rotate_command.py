"""``theseus infra rotate``: plan or run the rotation of each credential."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import github_env
from .config import ConfigError
from .plan import Context
from .rotate import Engine, Outcome, Rotator, SetSecret
from .rotators import build_rotators
from .state import State
from .store import Secret, StoreError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class RotateUsageError(ValueError):
    pass


@dataclass(frozen=True)
class Report:
    code: int
    text: str


def select(ctx: Context, only: Sequence[str]) -> list[Rotator]:
    rotators: list[Rotator] = build_rotators(ctx, ctx.config)
    if not only:
        return rotators
    available = {name for rotator in rotators for name in rotator.names}
    unknown = [name for name in only if name not in available]
    if unknown:
        raise RotateUsageError(
            f"{', '.join(unknown)} cannot be rotated; rotatable secrets are {', '.join(sorted(available)) or 'none'}"
        )
    return [rotator for rotator in rotators if set(rotator.names) & set(only)]


def _environment_problem(ctx: Context, key: str) -> str | None:
    """Why ``key`` (``owner/repo:environment``) must not receive credentials, from a fresh read; ``None`` if it may."""
    repository = ctx.config.github.repository
    repo, _, environment = key.partition(":")
    if repo != repository or not environment:
        return f"{key} is not an environment of {repository}"
    result = ctx.run(["gh", "api", "-X", "GET", "-H", github_env.ACCEPT, f"repos/{repo}/environments/{environment}"])
    try:
        found = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return f"cannot read environment {key}, so no credential is created or delivered"
    if not github_env.is_restricted(found):
        return f"environment {key} allows deployments from every branch, so no credential is created or delivered"
    return None


def _setter(ctx: Context) -> SetSecret:
    def set_in(environment_key: str, name: str, value: Secret) -> None:
        problem = _environment_problem(ctx, environment_key)
        if problem:
            raise github_env.GithubError(f"cannot set {name}: {problem}")
        github_env.set_secret(ctx, name, value, environment_key.partition(":")[2])

    return set_in


def _title(rotator: Rotator) -> str:
    return f"{rotator.kind} ({', '.join(rotator.names)})"


def _render_text(lines: Sequence[tuple[str, Sequence[str]]]) -> str:
    out: list[str] = []
    for title, body in lines:
        out.append(title)
        out.extend(f"    {line}" for line in body)
    return "\n".join(out)


class RotateLockedError(RuntimeError):
    pass


class RotateUnavailableError(RuntimeError):
    pass


@contextmanager
def _locked(state_path: Path) -> Iterator[None]:
    """An exclusive lock beside the state file for a whole apply or resume; a second run is refused, not queued."""
    try:
        import fcntl  # POSIX only; imported here so the rest of the CLI still loads where it is missing
    except ImportError as exc:
        raise RotateUnavailableError(
            "rotate --apply needs POSIX file locking and runs only on macOS or Linux hosts"
        ) from exc
    lock = state_path.with_name(state_path.name + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RotateLockedError(
                f"another rotation is running (it holds {lock}); wait for it to finish, then run rotate again"
            ) from exc
        yield
    finally:
        os.close(fd)


def run(ctx: Context, *, only: Sequence[str], apply: bool, resume: bool, as_json: bool) -> Report:
    try:
        rotators = select(ctx, only)
    except RotateUsageError as exc:
        return Report(EXIT_USAGE, ctx.redactor.scrub(str(exc)))
    if not apply:
        return _plan(ctx, rotators, resume, as_json)
    try:
        with _locked(ctx.config.state_path):
            # Another run may have finished while this one waited to start, so read the state again under the lock.
            ctx.state = State.load(ctx.config.state_path)
            return _apply(ctx, rotators, resume, as_json)
    except RotateUnavailableError as exc:
        return Report(EXIT_USAGE, str(exc))
    except RotateLockedError as exc:
        return Report(EXIT_FAILED, str(exc))


def _targets(ctx: Context, rotators: Sequence[Rotator]) -> list[str]:
    """The configured environment and every environment recorded for the credentials being rotated."""
    keys = {ctx.config.environment_key}
    for rotator in rotators:
        for name in rotator.names:
            keys.update(ctx.state.environments(name))
    return sorted(keys)


def _apply(ctx: Context, rotators: Sequence[Rotator], resume: bool, as_json: bool) -> Report:
    if not resume:
        for key in _targets(ctx, rotators):
            problem = _environment_problem(ctx, key)
            if problem:
                body = json.dumps({"applied": False, "refused": problem}) if as_json else f"refused: {problem}"
                return Report(EXIT_FAILED, ctx.redactor.scrub(body))
    engine = Engine(ctx.store, ctx.state, ctx.redactor, _setter(ctx), ctx.config.environment_key)
    outcomes: list[Outcome] = []
    for rotator in rotators:
        if resume and not engine.pending_record(rotator):
            outcomes.append(Outcome(rotator.kind, rotator.names, "unchanged", "nothing to resume"))
            continue
        outcomes.append(engine.rotate(rotator, resume=resume))
    code = EXIT_OK if all(outcome.ok for outcome in outcomes) else EXIT_FAILED
    if as_json:
        body = json.dumps({"applied": True, "results": [item.to_json() for item in outcomes]}, indent=2, sort_keys=True)
        return Report(code, ctx.redactor.scrub(body))
    lines = [
        (f"{item.status}: {_title_of(item)}", [*([item.detail] if item.detail else []), *item.notes])
        for item in outcomes
    ]
    return Report(code, ctx.redactor.scrub(_render_text(lines)))


def _title_of(outcome: Outcome) -> str:
    return f"{outcome.kind} ({', '.join(outcome.names)})"


def _plan(ctx: Context, rotators: Sequence[Rotator], resume: bool, as_json: bool) -> Report:
    engine = Engine(ctx.store, ctx.state, ctx.redactor, _setter(ctx), ctx.config.environment_key)
    entries: list[tuple[Rotator, list[str]]] = []
    for rotator in rotators:
        record = engine.pending_record(rotator)
        if record:
            phase = record.get("phase", "?")
            step = {
                "promoted": "revoke the old credential",
                "promoting": "finish the promotion forward (the new credential is live and is never undone)",
            }.get(phase, "roll the interrupted rotation back")
            entries.append((rotator, [f"an earlier rotation did not finish (phase {phase}); with --resume: {step}"]))
        elif resume:
            entries.append((rotator, ["nothing to resume"]))
        else:
            entries.append((rotator, list(rotator.describe())))
    if as_json:
        body = json.dumps(
            {
                "applied": False,
                "plan": [
                    {"credential": item.kind, "secrets": list(item.names), "steps": steps} for item, steps in entries
                ],
            },
            indent=2,
            sort_keys=True,
        )
        return Report(EXIT_OK, ctx.redactor.scrub(body))
    text = _render_text([(_title(item), steps) for item, steps in entries])
    return Report(EXIT_OK, ctx.redactor.scrub(text + "\n(dry run: nothing changed; add --apply to rotate)"))


def command_rotate(args: argparse.Namespace) -> int:
    from . import cli

    code = EXIT_OK
    only = [name for name in args.only.split(",") if name]
    for path in args.config:
        ctx: Context | None = None
        try:
            ctx = cli._open(path)
            report = run(ctx, only=only, apply=args.apply, resume=args.resume, as_json=args.json)
        except (ConfigError, StoreError, RuntimeError, OSError, ValueError) as exc:
            return cli._fail(str(exc), ctx.redactor if ctx else None)
        print(report.text)
        code = max(code, report.code)
    return code


def register(sub: Any) -> None:
    parser = sub.add_parser("rotate", help="rotate each credential: create, check, promote, then revoke the old one")
    parser.add_argument(
        "--config", nargs="+", required=True, metavar="PATH", help="policy file(s) with an infra: section"
    )
    parser.add_argument("--only", default="", help="comma-separated secret names to rotate")
    parser.add_argument("--apply", action="store_true", help="rotate; without it nothing changes")
    parser.add_argument("--resume", action="store_true", help="finish rotations an earlier run left unfinished")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=command_rotate)
