from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .golden import normalize
from .harness import Harness, Step

CAPTURE_ENV = "PARITY_OUT"
SCRATCH = re.compile(r"/tmp/oracle-local-[a-z0-9_]+")
HOME_SCRATCH = re.compile(r"oracle-home-(\d+)-[a-z0-9_]+")
KEY_FILE = re.compile(r"[0-9a-f]{64}\.json")
STAMP = re.compile(r"\d{8}T\d{6}\d*Z")


def record(name: str, value: Any, harness: Harness) -> Any:
    text = json.dumps(value).replace(str(harness.local_state), "<state>")
    text = HOME_SCRATCH.sub(r"oracle-home-\1-<id>", SCRATCH.sub("/tmp/oracle-local-<id>", text))
    shaped = json.loads(text)
    normalized = normalize(shaped, harness)
    target = os.environ.get(CAPTURE_ENV)
    if target:
        folder = Path(target) / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
    return normalized


def step(result: Step) -> dict[str, Any]:
    return {"code": result.code, "stdout": result.stdout, "stderr": result.stderr}


def tree(root: Path) -> dict[str, Any]:
    keyed: dict[str, list[tuple[str, Any]]] = {}
    found: dict[str, Any] = {}
    stamps: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix == ".lock":
            continue
        text = path.read_text(encoding="utf-8")
        try:
            content: Any = json.loads(text)
        except ValueError:
            content = text
        relative = path.relative_to(root)
        stamps.update(STAMP.findall(relative.as_posix()))
        if KEY_FILE.fullmatch(relative.name):
            keyed.setdefault(relative.parent.as_posix(), []).append((json.dumps(content, sort_keys=True), content))
        else:
            found[relative.as_posix()] = content
    for folder, entries in keyed.items():
        for index, (_, content) in enumerate(sorted(entries, key=lambda item: item[0])):
            found[f"{folder}/<key-{index}>.json"] = content
    order = {stamp: f"<stamp-{index}>" for index, stamp in enumerate(sorted(stamps))}
    return {STAMP.sub(lambda match: order[match.group(0)], name): content for name, content in found.items()}
