from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

COMMIT = "a" * 40


def _record_row(name: str, content: bytes) -> list[str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
    return [name, f"sha256={digest}", str(len(content))]


def make_wheel(
    name: str,
    version: str,
    files: Mapping[str, str],
    *,
    requires: Sequence[str] = (),
    scripts: Mapping[str, str] | None = None,
    requires_python: str = ">=3.11",
    tag: str = "py3-none-any",
    extra: Mapping[str, bytes] | None = None,
    skip_record_for: Sequence[str] = (),
) -> bytes:
    dist = f"{name.replace('-', '_')}-{version}.dist-info"
    entries: dict[str, bytes] = {path: text.encode("utf-8") for path, text in files.items()}
    metadata = [f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: {requires_python}"]
    metadata.extend(f"Requires-Dist: {item}" for item in requires)
    entries[f"{dist}/METADATA"] = ("\n".join(metadata) + "\n").encode("utf-8")
    entries[f"{dist}/WHEEL"] = f"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: {tag}\n".encode()
    if scripts:
        lines = "".join(f"{script} = {target}\n" for script, target in scripts.items())
        entries[f"{dist}/entry_points.txt"] = f"[console_scripts]\n{lines}".encode()
    for path, content in (extra or {}).items():
        entries[path] = content
    record_rows = [_record_row(path, content) for path, content in entries.items() if path not in skip_record_for]
    record_rows.append([f"{dist}/RECORD", "", ""])
    rendered = io.StringIO()
    for row in record_rows:
        rendered.write(",".join(row) + "\n")
    entries[f"{dist}/RECORD"] = rendered.getvalue().encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Net:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.blocked: set[str] = set()
        self.calls: list[str] = []

    def add(self, url: str, raw: bytes) -> str:
        self.blobs[url] = raw
        return url

    def __call__(self, url: str) -> bytes:
        from theseus_reviewer.bootstrap import BootstrapError

        self.calls.append(url)
        if url in self.blocked or url not in self.blobs:
            raise BootstrapError(f"cannot download {url}: blocked")
        return self.blobs[url]


@dataclass
class World:
    package: str = "fakepkg"
    version: str = "1.0.0"
    net: Net = field(default_factory=Net)
    main_wheel: bytes = b""
    dep_wheel: bytes = b""
    lock: dict = field(default_factory=dict)

    @property
    def primary(self) -> str:
        return "https://github.com/acme/theseus/releases/download/v1/fakepkg-1.0.0-py3-none-any.whl"

    @property
    def retained(self) -> str:
        return "https://files.pythonhosted.org/packages/ab/cd/fakepkg-1.0.0-py3-none-any.whl"

    @property
    def dep_primary(self) -> str:
        return "https://files.pythonhosted.org/packages/de/ad/fakedep-2.3.0-py3-none-any.whl"


MAIN_SOURCE = (
    "import sys\n"
    "import fakedep\n"
    "def main():\n"
    "    print('fakepkg ok', fakedep.VALUE, sys.flags.isolated, sys.flags.no_site)\n"
    "    return 0\n"
)


def build_world(*, main_requires: Sequence[str] = ("fakedep>=2,<3",), with_dep: bool = True) -> World:
    world = World()
    world.main_wheel = make_wheel(
        "fakepkg",
        "1.0.0",
        {"fakepkg/__init__.py": "", "fakepkg/main.py": MAIN_SOURCE},
        requires=main_requires,
        scripts={"fake": "fakepkg.main:main"},
    )
    world.dep_wheel = make_wheel("fakedep", "2.3.0", {"fakedep/__init__.py": "VALUE = 7\n"})
    world.net.add(world.primary, world.main_wheel)
    world.net.add(world.retained, world.main_wheel)
    world.net.add(world.dep_primary, world.dep_wheel)
    world.lock = lock_for(world, with_dep=with_dep)
    return world


def lock_for(world: World, *, with_dep: bool = True) -> dict:
    return {
        "schema": 1,
        "package": world.package,
        "version": world.version,
        "repository": "acme/theseus",
        "source_commit": COMMIT,
        "source_sha256": "1" * 64,
        "delta_sha256": "2" * 64,
        "artifact": {"sha256": sha(world.main_wheel), "urls": [world.primary, world.retained]},
        "dependencies": [
            {"name": "fakedep", "version": "2.3.0", "sha256": sha(world.dep_wheel), "urls": [world.dep_primary]}
        ]
        if with_dep
        else [],
        "workflow_sha": COMMIT,
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_lock(repo: Path, lock: Mapping[str, object] | str, lock_path: str = ".github/theseus-lock.json") -> str:
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
    target = repo / lock_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(lock if isinstance(lock, str) else json.dumps(lock, indent=2), encoding="utf-8")
    git(repo, "add", lock_path)
    git(repo, "commit", "-q", "-m", "lock")
    return git(repo, "rev-parse", "HEAD")
