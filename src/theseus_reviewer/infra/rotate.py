"""The rotation state machine, shared by every rotator.

For one credential: create the new one at the provider, record its id in the state file, store it as
``NAME.pending``, set it in every recorded environment, health-check it, promote it to ``NAME`` and revoke the old
one. A failure before the promotion is rolled back: the old value is set again from the store, the new credential
is deleted at the provider and the pending item is dropped. A failure to revoke the old credential leaves the new
one live and the old one recorded in the state file, so ``--resume`` revokes it later.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .state import State
from .store import PENDING_SUFFIX, Secret, Store
from .transport import Redactor

PHASE_CREATED = "created"
PHASE_PROMOTING = "promoting"
PHASE_PROMOTED = "promoted"
STATE_KEY_PREFIX = "rotate:"
PREVIOUS_SUFFIX = ".previous"

log = logging.getLogger("theseus.infra")

# Outcomes of one credential.
ROTATED = "rotated"
UNREVOKED = "rotated-nothing-revoked"
UNCHANGED = "unchanged"
ROLLED_BACK = "rolled-back"
REVOKE_PENDING = "revoke-pending"
RESUMED = "resumed"
FAILED = "failed"
REPORTED = "reported"


class RotationError(RuntimeError):
    """A step failed. The message is safe to show: rotators never put a value in one."""


@dataclass
class Created:
    """A new credential: the value of each secret it supplies and the provider ids needed to delete it later."""

    values: dict[str, Secret]
    ids: dict[str, str] = field(default_factory=dict)


class Rotator(Protocol):
    """One rotatable credential. ``names`` are the secrets it supplies; Tailscale supplies two."""

    kind: str
    names: tuple[str, ...]

    def create(self) -> Created | None:
        """Make the new credential, or return ``None`` when there is nothing newer to adopt (manual sources)."""

    def old_ids(self) -> dict[str, str]:
        """Provider ids of the credential being replaced, read before ``create``."""

    def health(self, created: Created) -> None:
        """Raise ``RotationError`` unless the new credential works."""

    def promote(self, created: Created) -> None:
        """Provider-side work that makes the new credential the current one, after the health check."""

    def delete_new(self, ids: Mapping[str, str]) -> None:
        """Delete the new credential at the provider."""

    def revoke_old(self, ids: Mapping[str, str]) -> None:
        """Revoke the replaced credential."""

    def after_revoke(self, created: Created | None) -> Sequence[str]:
        """Lines for the owner: manual steps still to do (for example deleting the old key in a console)."""

    def committed(self, created: Created) -> None:
        """Record the new credential as the live one, after it is stored. Local bookkeeping only."""

    def describe(self) -> Sequence[str]:
        """What a rotation would do, from read-only calls: for the dry run."""


SetSecret = Callable[[str, str, Secret], None]
"""Sets one secret in one recorded environment: ``(environment_key, name, value)``."""


@dataclass
class Outcome:
    kind: str
    names: tuple[str, ...]
    status: str
    detail: str = ""
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (ROTATED, UNREVOKED, UNCHANGED, RESUMED, REPORTED)

    def to_json(self) -> dict[str, object]:
        return {
            "credential": self.kind,
            "secrets": list(self.names),
            "status": self.status,
            "detail": self.detail,
            "notes": list(self.notes),
        }


@dataclass
class Engine:
    store: Store
    state: State
    redactor: Redactor
    set_secret: SetSecret
    default_environment: str

    def _save(self) -> None:
        self.state.save(self.redactor.values)

    def _record_key(self, rotator: Rotator) -> str:
        return STATE_KEY_PREFIX + rotator.kind

    def _scrub(self, error: BaseException) -> str:
        return self.redactor.scrub(f"{type(error).__name__}: {error}" if not str(error) else str(error))

    def pending_record(self, rotator: Rotator) -> dict[str, str]:
        return self.state.provider(self._record_key(rotator))

    def _environments_of(self, name: str) -> list[str]:
        return self.state.environments(name) or [self.default_environment]

    def _environments(self, names: Sequence[str]) -> dict[str, list[str]]:
        return {name: self._environments_of(name) for name in names}

    def _old_values(self, names: Sequence[str]) -> dict[str, Secret | None]:
        return {name: self.store.get(name) for name in names}

    def _outcome(self, rotator: Rotator, status: str, detail: str = "", notes: tuple[str, ...] = ()) -> Outcome:
        return Outcome(rotator.kind, rotator.names, status, detail, notes)

    def rotate(self, rotator: Rotator, *, resume: bool) -> Outcome:
        record = self.pending_record(rotator)
        if record:
            return self._leftover(rotator, record, resume)
        old = self._old_values(rotator.names)
        try:
            old_ids = rotator.old_ids()
            created = rotator.create()
        except Exception as exc:
            return self._failed_create(rotator, exc)
        if created is None:
            return self._outcome(rotator, UNCHANGED, "no newer credential to adopt", self._manual(rotator, None))
        for value in created.values.values():
            self.redactor.add(value.reveal(), *value.reveal().splitlines())
        return self._run(rotator, created, old, old_ids)

    def _failed_create(self, rotator: Rotator, exc: Exception) -> Outcome:
        """Creation failed. If the provider may have made a credential anyway, delete it by the id it answered with."""
        detail = f"creating the new credential: {self._scrub(exc)}"
        partial = getattr(rotator, "partial_ids", None)
        ids = dict(partial()) if callable(partial) else {}
        if not ids:
            return self._outcome(rotator, FAILED, detail)
        log.warning("%s: the provider may have created a credential with id %s", rotator.kind, ids)
        self.state.set_provider(
            self._record_key(rotator), phase=PHASE_CREATED, **{f"new.{key}": value for key, value in ids.items()}
        )
        try:
            self._save()
        except Exception as save_error:
            return self._outcome(
                rotator, FAILED, f"{detail}; the credential {ids} may exist: {self._scrub(save_error)}"
            )
        outcome = self._rollback(rotator, Created({}, ids), detail, restore=False)
        outcome.status = FAILED
        return outcome

    def _run(
        self, rotator: Rotator, created: Created, old: Mapping[str, Secret | None], old_ids: Mapping[str, str]
    ) -> Outcome:
        key = self._record_key(rotator)
        record = {"phase": PHASE_CREATED, **{f"new.{k}": v for k, v in created.ids.items()}}
        record.update({f"old.{k}": v for k, v in old_ids.items()})
        self.state.set_provider(key, **record)
        try:
            self._save()
        except Exception as exc:
            return self._rollback(rotator, created, f"recording the new credential: {self._scrub(exc)}")
        try:
            for name, value in old.items():
                if value is not None:
                    self.store.put(name + PREVIOUS_SUFFIX, value)
            for name, value in created.values.items():
                self.store.put(name + PENDING_SUFFIX, value)
            rotator.health(created)
            environments = self._environments(created.values)
            for name, value in created.values.items():
                for environment in environments[name]:
                    self.set_secret(environment, name, value)
            rotator.health(created)
            rotator.promote(created)
        except BaseException as exc:
            outcome = self._rollback(rotator, created, self._scrub(exc))
            if not isinstance(exc, Exception):
                raise
            return outcome
        # From here the new credential is the live one: only roll forward. An interrupt leaves the record behind,
        # and ``--resume`` finishes from either phase without ever deleting the credential in use.
        record["phase"] = PHASE_PROMOTING
        self.state.set_provider(key, **record)
        try:
            self._save()
        except Exception as exc:
            record["phase"] = PHASE_CREATED
            self.state.set_provider(key, **record)
            return self._rollback(rotator, created, f"recording the promotion: {self._scrub(exc)}")
        return self._forward(rotator, created, record, ROTATED)

    def _forward(self, rotator: Rotator, created: Created, record: dict[str, str], status: str) -> Outcome:
        """Finish a promotion: store the new values, record them, then revoke. Safe to repeat."""
        key = self._record_key(rotator)
        try:
            for name, value in created.values.items():
                if self.store.get(name + PENDING_SUFFIX) is not None:
                    self.store.put(name, value)
            rotator.committed(created)
            for name in created.values:
                self.state.bump(name)
                for environment in self._environments_of(name):
                    self.state.mark_delivered(name, environment)
            self.state.set_provider(key, **{**record, "phase": PHASE_PROMOTED})
            self._save()
        except Exception as exc:
            return self._outcome(
                rotator,
                REVOKE_PENDING,
                f"the new credential is live but recording it failed: {self._scrub(exc)}; run rotate --resume",
            )
        return self._revoke(rotator, created, record, status)

    def _rollback(self, rotator: Rotator, created: Created, reason: str, *, restore: bool = True) -> Outcome:
        problems: list[str] = []

        def attempt(label: str, action: Callable[[], None]) -> None:
            try:
                action()
            except Exception as exc:
                problems.append(f"{label}: {self._scrub(exc)}")

        for name in rotator.names if restore else ():
            value = self.store.get(name + PREVIOUS_SUFFIX) or self.store.get(name)
            if value is None:
                continue
            for environment in self._environments_of(name):
                attempt(
                    f"restoring {name} in {environment}",
                    lambda n=name, e=environment, v=value: self.set_secret(e, n, v),
                )
            attempt(f"restoring {name} in the store", lambda n=name, v=value: self.store.put(n, v))
        attempt("deleting the new credential", lambda: rotator.delete_new(created.ids))
        if not problems:
            for name in rotator.names:
                for suffix in (PENDING_SUFFIX, PREVIOUS_SUFFIX):
                    attempt(f"dropping {name}{suffix}", lambda n=name, s=suffix: self.store.delete(n + s))
        if problems:
            # Keep the record: the new credential may still exist and --resume rolls it back again.
            return self._outcome(rotator, FAILED, f"{reason}; rollback incomplete ({'; '.join(problems)})")
        self.state.clear_provider(self._record_key(rotator))
        try:
            self._save()
        except Exception as exc:
            return self._outcome(rotator, FAILED, f"{reason}; rolled back; saving the state file: {self._scrub(exc)}")
        return self._outcome(rotator, ROLLED_BACK, f"{reason}; the old credential is set again")

    def _revoke(self, rotator: Rotator, created: Created | None, record: Mapping[str, str], status: str) -> Outcome:
        old_ids = {key.removeprefix("old."): value for key, value in record.items() if key.startswith("old.")}
        try:
            for name in rotator.names:
                self.store.delete(name + PENDING_SUFFIX)
            rotator.revoke_old(old_ids)
            for name in rotator.names:
                self.store.delete(name + PREVIOUS_SUFFIX)
        except Exception as exc:
            return self._outcome(
                rotator,
                REVOKE_PENDING,
                f"the new credential is live; revoking the old one failed: {self._scrub(exc)}; run rotate --resume",
            )
        self.state.clear_provider(self._record_key(rotator))
        try:
            self._save()
        except Exception as exc:
            return self._outcome(
                rotator, REVOKE_PENDING, f"revoked, but the state file was not saved: {self._scrub(exc)}"
            )
        notes = self._manual(rotator, created)
        if getattr(rotator, "revocable", False) and not old_ids:
            return self._outcome(
                rotator,
                UNREVOKED,
                "no previous credential id was recorded, so nothing was revoked; revoke the old credential by hand",
                notes,
            )
        return self._outcome(rotator, status, "", notes)

    def _manual(self, rotator: Rotator, created: Created | None) -> tuple[str, ...]:
        return tuple(rotator.after_revoke(created))

    def _leftover(self, rotator: Rotator, record: Mapping[str, str], resume: bool) -> Outcome:
        phase = record.get("phase", "?")
        if not resume:
            return self._outcome(
                rotator, FAILED, f"an earlier rotation did not finish (phase {phase}); run rotate --resume"
            )
        new_ids = {key.removeprefix("new."): value for key, value in record.items() if key.startswith("new.")}
        if phase == PHASE_PROMOTED:
            return self._revoke(rotator, None, record, RESUMED)
        if phase == PHASE_PROMOTING:
            values: dict[str, Secret] = {}
            for name in rotator.names:
                value = self.store.get(name + PENDING_SUFFIX) or self.store.get(name)
                if value is None:
                    return self._outcome(rotator, FAILED, f"cannot finish promoting: no value for {name} is stored")
                self.redactor.add(value.reveal(), *value.reveal().splitlines())
                values[name] = value
            return self._forward(rotator, Created(values, new_ids), dict(record), RESUMED)
        # Interrupted before promotion: undo whatever it did, then a plain rotate can start again.
        outcome = self._rollback(rotator, Created({}, new_ids), "resuming an interrupted rotation")
        if outcome.status == ROLLED_BACK:
            outcome.status = RESUMED
            outcome.detail = "rolled back an interrupted rotation; run rotate again to start a new one"
        return outcome
