"""``theseus infra rotate``: plan or run the rotation of each credential."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from . import github_env
from .plan import Context
from .rotate import Engine, Outcome, Rotator, SetSecret
from .rotators import build_rotators
from .store import Secret

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


def _setter(ctx: Context) -> SetSecret:
    repository = ctx.config.github.repository

    def set_in(environment_key: str, name: str, value: Secret) -> None:
        repo, _, environment = environment_key.partition(":")
        if repo != repository or not environment:
            raise github_env.GithubError(f"cannot set {name} in {environment_key}: it is not in {repository}")
        github_env.set_secret(ctx, name, value, environment)

    return set_in


def _title(rotator: Rotator) -> str:
    return f"{rotator.kind} ({', '.join(rotator.names)})"


def _render_text(lines: Sequence[tuple[str, Sequence[str]]]) -> str:
    out: list[str] = []
    for title, body in lines:
        out.append(title)
        out.extend(f"    {line}" for line in body)
    return "\n".join(out)


def run(ctx: Context, *, only: Sequence[str], apply: bool, resume: bool, as_json: bool) -> Report:
    try:
        rotators = select(ctx, only)
    except RotateUsageError as exc:
        return Report(EXIT_USAGE, ctx.redactor.scrub(str(exc)))
    if not apply:
        return _plan(ctx, rotators, resume, as_json)
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
            step = "revoke the old credential" if phase == "promoted" else "roll the interrupted rotation back"
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
