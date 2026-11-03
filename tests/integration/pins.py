from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from theseus_reviewer import bootstrap

from .harness import REPO_ROOT
from .runs import Pins

SOURCE = REPO_ROOT / "src" / "theseus_reviewer"
SUPPORT = REPO_ROOT / "tests" / "unit" / "bootstrap_support.py"
LOCK_PATH = bootstrap.LOCK_PATH
REVIEWER_COMMIT = "a" * 40
GATE_COMMIT = "b" * 40
REVIEWER_VERSION = "0.1.0"
GATE_VERSION = "0.2.0"
HOLD_DESCRIPTION = "oracle-review: held by gate pin B"
PASS_DESCRIPTION = "oracle-review: passed by gate pin B"

_spec = importlib.util.spec_from_file_location("bootstrap_support", SUPPORT)
assert _spec is not None and _spec.loader is not None
support = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("bootstrap_support", support)
_spec.loader.exec_module(support)

GATE_MAIN = """
import json
import os
import sys
import urllib.request


def run():
    argv = sys.argv[1:]
    mode = {mode!r}
    record = {{
        "pin": "B",
        "argv": argv,
        "pid": os.getpid(),
        "isolated": sys.flags.isolated,
        "no_site": sys.flags.no_site,
        "token": os.environ.get("GITHUB_TOKEN"),
        "cwd": os.getcwd(),
        "modules": sorted(name for name in sys.modules if name.startswith("theseus_reviewer")),
        "module_file": sys.modules["theseus_reviewer"].__file__,
        "yaml_importable": "yaml" in sys.modules,
    }}
    with open({record!r}, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    if mode == "crash":
        print("gate pin B crashed", file=sys.stderr)
        return 1
    head = argv[argv.index("--head-sha") + 1]
    repo = argv[argv.index("--repo") + 1]
    state, description = ("failure", {hold!r}) if mode == "hold" else ("success", {passed!r})
    body = json.dumps({{"state": state, "context": "oracle-gate", "description": description}}).encode()
    request = urllib.request.Request(
        os.environ["GITHUB_API_URL"] + "/repos/" + repo + "/statuses/" + head,
        data=body,
        method="POST",
        headers={{"Authorization": "token " + os.environ["GITHUB_TOKEN"], "Content-Type": "application/json"}},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
    with opener.open(request, timeout=30):
        pass
    return 0
"""


class Mirror:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        mirror = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                raw = mirror.blobs.get(self.path)
                if raw is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def add(self, url: str, raw: bytes) -> str:
        parts = urlsplit(url)
        self.blobs[f"/_assets/{parts.hostname}{parts.path}"] = raw
        return url

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _text_files(root: Path, prefix: str, skip: tuple[str, ...] = ()) -> dict[str, str]:
    return {
        f"{prefix}/{path.relative_to(root).as_posix()}": path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and (path.suffix in (".py", ".md") or path.name == "LICENSE")
        and path.name not in skip
    }


def reviewer_wheel() -> bytes:
    return support.make_wheel(
        "theseus-reviewer",
        REVIEWER_VERSION,
        _text_files(SOURCE, "theseus_reviewer"),
        scripts={"theseus": "theseus_reviewer.main:run"},
    )


def gate_wheel(mode: str, record: Path) -> bytes:
    main = GATE_MAIN.format(mode=mode, record=str(record), hold=HOLD_DESCRIPTION, passed=PASS_DESCRIPTION)
    return support.make_wheel(
        "theseus-reviewer",
        GATE_VERSION,
        {"theseus_reviewer/__init__.py": "PIN = 'B'\n", "theseus_reviewer/main.py": main},
        scripts={"theseus": "theseus_reviewer.main:run"},
    )


def lock_text(
    main: bytes, version: str, commit: str, urls: tuple[str, str], dependencies: list[dict[str, object]]
) -> str:
    return json.dumps(
        {
            "schema": 1,
            "package": "theseus-reviewer",
            "version": version,
            "repository": "joeharris76/theseus",
            "source_commit": commit,
            "source_sha256": "1" * 64,
            "delta_sha256": "2" * 64,
            "artifact": {"sha256": support.sha(main), "urls": list(urls)},
            "dependencies": dependencies,
            "workflow_sha": commit,
        },
        indent=2,
    )


class PinWorld:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.mirror = Mirror()
        self.record = workdir / "gate-pin-record.json"
        main = reviewer_wheel()
        self.reviewer_lock = lock_text(
            main,
            REVIEWER_VERSION,
            REVIEWER_COMMIT,
            (
                self.mirror.add("https://github.com/joeharris76/theseus/releases/download/a/a.whl", main),
                self.mirror.add("https://files.pythonhosted.org/packages/aa/aa/a.whl", main),
            ),
            [],
        )
        self.bootstrap = Path(bootstrap.__file__)

    def gate_lock(self, mode: str) -> str:
        raw = gate_wheel(mode, self.record)
        return lock_text(
            raw,
            GATE_VERSION,
            GATE_COMMIT,
            (
                self.mirror.add(f"https://github.com/joeharris76/theseus/releases/download/b-{mode}/b.whl", raw),
                self.mirror.add(f"https://files.pythonhosted.org/packages/bb/{mode}/b.whl", raw),
            ),
            [],
        )

    @property
    def pins(self) -> Pins:
        return Pins(self.bootstrap, self.mirror.origin)

    def recorded(self) -> dict[str, object] | None:
        return json.loads(self.record.read_text(encoding="utf-8")) if self.record.is_file() else None

    def close(self) -> None:
        self.mirror.close()
