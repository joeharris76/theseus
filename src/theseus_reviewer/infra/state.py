"""The state file: provider ids and versions, never values.

Each secret has a ``version`` that goes up whenever this tool writes a new value to the store, and a record of
which version each GitHub environment last received. A second run compares the two, so it needs no value.
Provider ids (an OAuth client id, a service account id) are recorded so a re-run or a rollback can find them.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
_MIN_GUARD_LENGTH = 8


class StateError(RuntimeError):
    pass


@dataclass
class State:
    path: Path
    secrets: dict[str, dict[str, Any]] = field(default_factory=dict)
    providers: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read the state file {path}: {type(exc).__name__}") from exc
        if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
            raise StateError(f"{path} is not a schema {SCHEMA} state file")
        secrets = data.get("secrets", {})
        providers = data.get("providers", {})
        if not isinstance(secrets, dict) or not isinstance(providers, dict):
            raise StateError(f"{path} is malformed")
        return cls(path, secrets, providers)

    def version(self, name: str) -> int:
        return int(self.secrets.get(name, {}).get("version", 0))

    def bump(self, name: str) -> int:
        """Record that a new value for ``name`` is in the store. Environments then need it again."""
        record = self.secrets.setdefault(name, {})
        record["version"] = self.version(name) + 1
        return int(record["version"])

    def delivered(self, name: str, environment: str) -> int:
        return int(self.secrets.get(name, {}).get("environments", {}).get(environment, 0))

    def mark_delivered(self, name: str, environment: str) -> None:
        record = self.secrets.setdefault(name, {})
        record.setdefault("environments", {})[environment] = max(self.version(name), 1)
        record.setdefault("version", max(self.version(name), 1))

    def environments(self, name: str) -> list[str]:
        return sorted(self.secrets.get(name, {}).get("environments", {}))

    def provider(self, kind: str) -> dict[str, str]:
        return dict(self.providers.get(kind, {}))

    def set_provider(self, kind: str, **ids: str) -> None:
        self.providers[kind] = dict(ids)

    def clear_provider(self, kind: str) -> None:
        self.providers.pop(kind, None)

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "secrets": self.secrets, "providers": self.providers}

    def save(self, forbidden: Iterable[str] = ()) -> None:
        """Write atomically with mode 0600. Refuses to write if any known secret value would appear in the file."""
        text = json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"
        for value in forbidden:
            if len(value) >= _MIN_GUARD_LENGTH and value in text:
                raise StateError("refusing to write a secret value into the state file")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, self.path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
