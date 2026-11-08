from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MALFORMED = (
    "refusal",
    "short",
    "order",
    "wrong-type",
    "string-probability",
    "boolean-probability",
    "nan",
    "sum",
    "label",
    "level",
    "score-range",
    "model",
    "not-json",
    "no-confidence",
)


class FakeDecisions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str] = []
        self.script: list[str] = []
        self.choices: dict[str, str] = {
            "size": "medium",
            "capability": "routine",
            "template": "standard",
            "impact": "low",
        }
        self.level = 1
        self.top = 0.9
        self.delay = 0.0
        self.model = "gpt-6-luna"
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    def start(self) -> FakeDecisions:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake._lock:
                    fake.requests.append(body)
                    fake.authorizations.append(self.headers.get("Authorization", ""))
                    mode = fake.script.pop(0) if fake.script else "ok"
                if fake.delay:
                    time.sleep(fake.delay)
                if mode.startswith("status:"):
                    self._send(int(mode.split(":")[1]), {"error": {"message": "fake failure"}})
                    return
                if mode == "not-json":
                    self._raw(200, b"<html>")
                    return
                self._send(200, fake.respond(body, mode))

            def _send(self, status: int, payload: Any) -> None:
                self._raw(status, json.dumps(payload).encode())

            def _raw(self, status: int, data: bytes) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    return

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def respond(self, body: dict[str, Any], mode: str) -> dict[str, Any]:
        answers = [self._answer(question) for question in body["questions"]]
        model = "other-model" if mode == "model" else self.model
        if mode == "refusal":
            answers[0] = {"type": "refusal", "name": answers[0]["name"]}
        elif mode == "short":
            answers.pop()
        elif mode == "order" and len(answers) > 1:
            answers.reverse()
        elif mode == "wrong-type":
            answers[0]["type"] = "score" if answers[0]["type"] == "choice" else "choice"
        elif mode == "string-probability":
            answers[0]["probabilities"][0]["probability"] = "0.9"
        elif mode == "boolean-probability":
            answers[0]["probabilities"][0]["probability"] = True
        elif mode == "nan":
            answers[0]["probabilities"][0]["probability"] = float("nan")
        elif mode == "sum":
            answers[0]["probabilities"][0]["probability"] = 0.2
        elif mode == "label":
            answers[0]["probabilities"][0]["value"] = "invented"
        elif mode == "level":
            score = next(item for item in answers if item["type"] == "score")
            score["probabilities"][0]["value"] = 99
        elif mode == "score-range":
            score = next(item for item in answers if item["type"] == "score")
            score["score"] = 99
        elif mode == "no-confidence":
            del answers[0]["confidence"]
        return {
            "model": model,
            "answers": answers,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            },
        }

    def _answer(self, question: dict[str, Any]) -> dict[str, Any]:
        base = question["name"].split(".")[0]
        if question["type"] == "choice":
            values = [item["value"] for item in question["choices"]]
            chosen = self.choices[base] if self.choices[base] in values else values[0]
            rest = (1 - self.top) / (len(values) - 1)
            probabilities = [{"value": value, "probability": self.top if value == chosen else rest} for value in values]
            return {
                "type": "choice",
                "name": question["name"],
                "choice": chosen,
                "probabilities": probabilities,
                "confidence": self.top,
            }
        levels = question["levels"]
        rest = (1 - self.top) / (len(levels) - 1)
        probabilities = [
            {"value": index, "label": item["label"], "probability": self.top if index == self.level else rest}
            for index, item in enumerate(levels)
        ]
        return {
            "type": "score",
            "name": question["name"],
            "score": float(self.level),
            "probabilities": probabilities,
            "confidence": self.top,
        }

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def reset(self) -> None:
        with self._lock:
            self.requests = []
            self.authorizations = []
            self.script = []

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
