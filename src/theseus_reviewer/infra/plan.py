"""The plan a command builds from read-only calls, shows, and (with ``--apply``) executes.

An action's text is written when the plan is built, from names and ids only. Its ``run`` closure fetches any value
from the store at execution time, so no value exists in the plan object or in its rendering.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import InfraConfig, load_infra
from .state import State
from .store import ScopedStore, Secret, Store, make_store
from .transport import HttpTransport, Redactor, Runner, UrllibTransport, run_command

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Context:
    """Everything a command touches, so every outside effect goes through a seam a test can fake."""

    config: InfraConfig
    store: Store
    state: State
    http: HttpTransport
    run: Runner
    redactor: Redactor
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def watch(self, value: str) -> None:
        """Register a value, and each line of a multi-line one, so no part of it can be shown."""
        self.redactor.add(value, *value.splitlines())

    def secret(self, name: str) -> Secret | None:
        """A stored value, registered for redaction as soon as it is read."""
        value = self.store.get(name)
        if value is not None:
            self.watch(value.reveal())
        return value

    def keep(self, name: str, value: Secret) -> None:
        """Put a new value in the store and mark it as needing delivery."""
        self.watch(value.reveal())
        self.store.put(name, value)
        self.state.bump(name)
        self.save_state()

    def save_state(self) -> None:
        self.state.save(self.redactor.values)


def open_context(
    config_path: Path,
    http: HttpTransport | None = None,
    run: Runner | None = None,
    now: Callable[[], datetime] | None = None,
) -> Context:
    """Load a policy file's ``infra:`` section, its store and state, and register every stored value for redaction."""
    config = load_infra(config_path)
    runner = run or run_command
    base = make_store(config.store.backend, config.store.directory, runner)
    store = ScopedStore(base, config.github.repository, config.github.environment)
    store.check_unscoped([spec.name for spec in config.secrets])
    ctx = Context(
        config=config,
        store=store,
        state=State.load(config.state_path),
        http=http or UrllibTransport(),
        run=runner,
        redactor=Redactor(),
        **({"now": now} if now else {}),
    )
    for name in store.names():
        ctx.secret(name)
    return ctx


@dataclass
class Action:
    id: str
    summary: str
    run: Callable[[], None]
    requires: tuple[str, ...] = ()
    gated: bool = False  # runs only after prerequisites; with none named it is refused, never run


@dataclass(frozen=True)
class Blocked:
    id: str
    reason: str


@dataclass(frozen=True)
class Outcome:
    id: str
    status: str
    detail: str = ""


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(
        self, id: str, summary: str, run: Callable[[], None], requires: tuple[str, ...] = (), gated: bool = False
    ) -> str:
        self.actions.append(Action(id, summary, run, requires, gated))
        return id

    def block(self, id: str, reason: str) -> None:
        self.blocked.append(Blocked(id, reason))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def has(self, id: str) -> bool:
        return any(action.id == id for action in self.actions)

    def to_json(self) -> dict[str, Any]:
        return {
            "actions": [{"id": action.id, "summary": action.summary} for action in self.actions],
            "blocked": [{"id": item.id, "reason": item.reason} for item in self.blocked],
            "notes": list(self.notes),
        }

    def render(
        self,
        redactor: Redactor,
        as_json: bool = False,
        applied: list[Outcome] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        if as_json:
            data = {**(extra or {}), **self.to_json()}
            if applied is not None:
                data["applied"] = [{"id": item.id, "status": item.status, "detail": item.detail} for item in applied]
            return redactor.scrub(json.dumps(data, indent=2, sort_keys=True))
        lines: list[str] = []
        results = {item.id: item for item in applied or []}
        for action in self.actions:
            outcome = results.get(action.id)
            mark = {None: "+", OK: "done", FAILED: "FAILED", SKIPPED: "skipped"}[outcome.status if outcome else None]
            lines.append(f"{mark} {action.id}: {action.summary}")
            if outcome and outcome.detail:
                lines.append(f"    {outcome.detail}")
        lines.extend(f"! {item.id}: {item.reason}" for item in self.blocked)
        lines.extend(f"note: {text}" for text in self.notes)
        if not self.actions and not self.blocked:
            lines.append("nothing to do")
        return redactor.scrub("\n".join(lines))

    def execute(self, redactor: Redactor) -> list[Outcome]:
        """Run the actions in order. An action whose prerequisite did not succeed is skipped, not attempted."""
        outcomes: dict[str, Outcome] = {}
        for action in self.actions:
            if action.gated and not action.requires:
                outcomes[action.id] = Outcome(action.id, SKIPPED, "its prerequisites were not planned")
                continue
            unmet = [need for need in action.requires if outcomes.get(need, Outcome(need, SKIPPED)).status != OK]
            if unmet:
                outcomes[action.id] = Outcome(action.id, SKIPPED, f"needs {', '.join(unmet)}")
                continue
            try:
                action.run()
            except (RuntimeError, OSError) as exc:
                outcomes[action.id] = Outcome(action.id, FAILED, redactor.scrub(str(exc)))
            else:
                outcomes[action.id] = Outcome(action.id, OK)
        return list(outcomes.values())
