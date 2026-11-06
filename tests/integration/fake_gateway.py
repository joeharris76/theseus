from __future__ import annotations

import atexit
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_MODELS = (
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-opus-5-5",
    "anthropic/claude-fable-5-1",
    "openai-codex/gpt-6-luna",
    "openai-codex/gpt-6.1-sol",
    "openai-codex/gpt-6-astra",
    "google-antigravity/gemini-3.8-flash",
    "google-antigravity/gemini-4-argon",
)
CLIENT_TOKEN_HEADER = "Authorization"
_certificate: tuple[Path, Path] | None = None


def certificate() -> tuple[Path, Path]:
    global _certificate
    if _certificate is None:
        directory = Path(tempfile.mkdtemp(prefix="fake-gateway-cert-"))
        atexit.register(shutil.rmtree, directory, True)
        cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        _certificate = (cert, key)
    return _certificate


class FakeGateway:
    def __init__(self, token: str) -> None:
        self.token = token
        self.served: list[str] = list(DEFAULT_MODELS)
        self.mode = "serve"
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    def serve(self, models: Iterable[str]) -> None:
        with self._lock:
            self.served = list(models)
            self.mode = "serve"

    def serve_all_but(self, provider: str) -> None:
        self.serve(model for model in DEFAULT_MODELS if not model.startswith(f"{provider}/"))

    def serve_only(self, provider: str) -> None:
        self.serve(model for model in DEFAULT_MODELS if model.startswith(f"{provider}/"))

    def start(self) -> FakeGateway:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with fake._lock:
                    fake.requests.append(self.path)
                    mode, served = fake.mode, list(fake.served)
                if mode == "down":
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/healthz":
                    self._send(200, {"ok": True})
                elif self.path == "/v1/models":
                    if mode == "malformed":
                        self._send(200, {"unexpected": []})
                    elif self.headers.get(CLIENT_TOKEN_HEADER) != f"Bearer {fake.token}" or mode == "rejected":
                        self._send(401, {"error": "invalid token"})
                    else:
                        self._send(200, {"data": [{"id": model, "owned_by": model.split("/")[0]} for model in served]})
                else:
                    self._send(404, {"error": "not found"})

            def _send(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                return None

        cert, key = certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"https://127.0.0.1:{self._server.server_address[1]}"

    @property
    def certificate_file(self) -> str:
        return str(certificate()[0])

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
