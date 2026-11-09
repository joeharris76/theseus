from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import absence, capacity, commands, coverage, evidence, omp_pin
from ._vendor import yaml
from .candidate_canaries import CANARIES, GROUPS, PROVIDERS, WHERES, Canary, control_name, group_canaries, overlay_name
from .candidate_chain import (
    AGENTS_TOKEN,
    BREACH_TOOLS,
    CLAUDE_TOKEN,
    CR_SOURCE,
    LONG_SOURCE,
    NATIVE_TOKEN,
    TRANSPORT_HEAD,
    TRANSPORT_PROMPT,
    TRANSPORT_TAIL,
    WIDE_LINE,
    WIDE_SOURCE,
    ClientRun,
)
from .candidate_env import quiet_env
from .policy import OMP_THINKING_LEVELS
from .verdict import omp_terminal_error, parse_output

PROBE_ROWS = (3, 7)
EXPECTED_SESSION_VERSION = 3
COMMAND_SECONDS = 60
ROSTER = frozenset(commands.OMP_READ_TOOLS.split(","))
UPSTREAM_PATH = "/v1/messages"
UPSTREAM_MODEL = "claude-sonnet-5-5"
QUOTA_PRIMARY = f"anthropic/{UPSTREAM_MODEL}"
QUOTA_PROVIDER = QUOTA_PRIMARY.partition("/")[0]
REVIEW_REQUESTS = 5
ARGV_PROMPT_LIMIT = 128 * 1024
ELISION_FOOTER = "elided; re-read needed ranges"
FIXTURE_PREFIX = "fixture-"
_ENUM_LINE = re.compile(r"^\s+(?P<key>\S+) = .* \((?P<options>[^()|]+(?:\|[^()|]+)+)\)\s*$", re.MULTILINE)
_EXPECTED = re.compile(r"Expected one of: (?P<values>[^\n]+?)\.?$", re.MULTILINE)
_BUILTIN_TOOLS = re.compile(r"Built-in tools: (?P<tools>[^.\n]+)\.")
_VALUE_TYPES = {bool: ("boolean",), list: ("array",), str: ("enum", "string"), dict: ("record",), int: ("number",)}

CONTRACT_ROWS = {
    1: "CLI flag set and flag semantics",
    2: "tool roster and read-only enforcement",
    3: "configuration overlay keys",
    4: "reviewer-home configuration and fallback-chain matching",
    5: "provider transport configuration",
    6: "event stream shape and error fields",
    7: "tool-call trace for read evidence",
    8: "error text calibration",
    9: "exit-code conventions",
    10: "process and on-disk behaviour",
    11: "version output format",
    12: "auth-gateway serve protocol and client tokens",
    13: "model catalog listing under a placeholder credential",
    14: "prompt transport on standard input",
    15: "served model set requires provider credentials",
    16: "disabledProviders hides a credentialed provider from the served set",
    17: "rewritten reviewer role selects the chosen model",
}
RECORDED_REASONS = {
    8: "provider reset-time wording is originated by each provider and is never observable offline; reset parsing stays covered by the calibrated absence tests",
}


@dataclass(frozen=True)
class Check:
    name: str
    rows: tuple[int, ...]
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def _check(name: str, rows: Iterable[int], failures: Iterable[str]) -> Check:
    return Check(name, tuple(rows), tuple(failures))


def covered_rows(checks: Iterable[Check]) -> frozenset[int]:
    return frozenset(row for check in checks if not check.name.startswith(FIXTURE_PREFIX) for row in check.rows)


def unprobed_providers(
    overlay_text: str = commands.OMP_OVERLAY_TEXT, observed: Collection[str] = frozenset()
) -> tuple[str, ...]:
    disabled = yaml.safe_load(overlay_text).get("disabledProviders") or []
    return tuple(provider for provider in disabled if provider not in observed)


def uncovered_rows(checks: Iterable[Check]) -> tuple[int, ...]:
    return tuple(row for row in CONTRACT_ROWS if row not in covered_rows(checks) and row not in RECORDED_REASONS)


def run_omp(binary: Path, home: Path, *args: str) -> tuple[int, str]:
    command = [str(binary)]
    command.extend(args)
    try:
        result = subprocess.run(
            command,
            cwd=home,
            env=quiet_env(home),
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f"{type(exc).__name__}"
    return result.returncode, result.stdout + result.stderr


def derived_flags() -> tuple[str, ...]:
    with tempfile.TemporaryDirectory(prefix="omp-flags-") as root:
        invocation = commands.build_omp(
            selector="@default",
            effort="high",
            workspace=Path(root) / "workspace",
            scratch=Path(root) / "scratch",
            prompt="probe",
            binary=Path("omp"),
        )
    return tuple(dict.fromkeys(token for token in invocation.argv[1:] if token.startswith("--")))


def check_version(binary: Path, home: Path, version: str) -> Check:
    code, output = run_omp(binary, home, "--version")
    tokens = omp_pin.VERSION_PATTERN.findall(output)
    failures = []
    if code != 0:
        failures.append(f"--version exited {code}")
    if len(tokens) != 1:
        failures.append(
            f"--version printed {len(tokens)} version-like tokens; the first-match parser needs exactly one"
        )
    elif tokens[0] != version:
        failures.append(f"--version reports {tokens[0]}, expected {version}")
    return _check("version-format", (11,), failures)


def check_flags_listed(binary: Path, home: Path) -> Check:
    code, text = run_omp(binary, home, "--help")
    failures = [] if code == 0 else [f"--help exited {code}"]
    for flag in derived_flags():
        if re.search(rf"(?m)^\s+(?:-\w,\s+)?{re.escape(flag)}(?:=|\s|$)", text) is None:
            failures.append(f"the build does not list {flag}")
    return _check("flags-listed", (1,), failures)


def _expected_values(output: str) -> frozenset[str]:
    found = _EXPECTED.search(output)
    return frozenset(item.strip() for item in found.group("values").split(",")) if found else frozenset()


def check_flag_values(binary: Path, home: Path) -> Check:
    failures: list[str] = []
    wanted = {"--mode": {"json"}, "--approval-mode": {"always-ask"}, "--thinking": set(OMP_THINKING_LEVELS)}
    for flag, values in wanted.items():
        code, output = run_omp(binary, home, "--print", flag, "no-such-value", "--no-session", "probe")
        accepted = _expected_values(output)
        if code == 0:
            failures.append(f"{flag} accepted an invalid value")
        if not values <= accepted:
            failures.append(f"{flag} no longer accepts {', '.join(sorted(values - accepted))}")
    code, output = run_omp(binary, home, "--print", "--tools", "no-such-tool", "--no-session", "probe")
    listed = _BUILTIN_TOOLS.search(output)
    tools = frozenset(item.strip() for item in listed.group("tools").split(",")) if listed else frozenset()
    if code == 0:
        failures.append("--tools accepted an unknown tool")
    if not tools >= ROSTER:
        failures.append(f"the build no longer offers the tools {', '.join(sorted(ROSTER - tools))}")
    return _check("flag-values", (1, 2), failures)


def _schema(binary: Path, home: Path) -> tuple[dict[str, Any], dict[str, tuple[str, ...]], list[str]]:
    code, text = run_omp(binary, home, "config", "list", "--json")
    try:
        schema = json.loads(text) if code == 0 else {}
    except ValueError:
        schema = {}
    if not isinstance(schema, dict) or not schema:
        return {}, {}, ["config list --json gave no settings schema"]
    code, listing = run_omp(binary, home, "config", "list")
    enums = {match["key"]: tuple(match["options"].split("|")) for match in _ENUM_LINE.finditer(listing)}
    return schema, enums, []


def _leaves(data: Mapping[str, Any], schema: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    for key, value in data.items():
        path = f"{prefix}{key}"
        if path not in schema and isinstance(value, dict) and any(name.startswith(f"{path}.") for name in schema):
            yield from _leaves(value, schema, f"{path}.")
        else:
            yield path, value


def _setting_failures(settings: Mapping[str, Any], schema: Mapping[str, Any], enums: Mapping[str, Any]) -> list[str]:
    failures = []
    for path, value in _leaves(settings, schema):
        entry = schema.get(path)
        if entry is None:
            failures.append(f"the build has no setting {path}; it would be ignored")
            continue
        if entry.get("type") not in _VALUE_TYPES.get(type(value), ()):
            failures.append(f"{path} is a {entry.get('type')} setting in this build, not {type(value).__name__}")
        elif entry.get("type") == "enum" and path not in enums:
            failures.append(f"the build lists no accepted values for the enum setting {path}")
        elif path in enums and value not in enums[path]:
            failures.append(f"{path} no longer accepts {value!r}; accepted: {', '.join(enums[path])}")
    return failures


def check_overlay_schema(binary: Path, home: Path, overlay_text: str = commands.OMP_OVERLAY_TEXT) -> Check:
    schema, enums, failures = _schema(binary, home)
    if not failures:
        failures = _setting_failures(yaml.safe_load(overlay_text), schema, enums)
    return _check("overlay-schema", (3,), failures)


def check_reviewer_config_schema(binary: Path, home: Path, config_text: str) -> Check:
    schema, enums, failures = _schema(binary, home)
    if not failures:
        failures = _setting_failures(yaml.safe_load(config_text), schema, enums)
    return _check("reviewer-config-schema", (4,), failures)


def check_model_catalog(binary: Path, config_text: str) -> Check:
    try:
        config = yaml.safe_load(config_text)
        catalog = capacity.read_catalog(binary, config_text)
    except (yaml.YAMLError, capacity.CatalogError) as exc:
        return _check("model-catalog", (13,), [str(exc)])
    missing = capacity.unlisted_models(config if isinstance(config, Mapping) else {}, catalog)
    return _check("model-catalog", (13,), [f"the model catalog does not list {name}" for name in missing])


def static_checks(binary: Path, home: Path, version: str, config_text: str) -> list[Check]:
    return [
        check_version(binary, home, version),
        check_flags_listed(binary, home),
        check_flag_values(binary, home),
        check_overlay_schema(binary, home),
        check_reviewer_config_schema(binary, home, config_text),
        check_model_catalog(binary, config_text),
    ]


def _events(stdout: str) -> Iterable[dict[str, Any]]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def assistant_messages(stdout: str) -> list[dict[str, Any]]:
    return [
        event["message"]
        for event in _events(stdout)
        if event.get("type") == "message_end"
        and isinstance(event.get("message"), dict)
        and event["message"].get("role") == "assistant"
    ]


def session_failures(stdout: str) -> list[str]:
    first = next(_events(stdout), None)
    if not isinstance(first, dict) or first.get("type") != "session":
        return ["the stream does not open with a session event"]
    if first.get("version") != EXPECTED_SESSION_VERSION:
        return [f"the session event version is {first.get('version')!r}, expected {EXPECTED_SESSION_VERSION}"]
    return []


def _classify(stdout: str, exit_code: int, now: datetime | None = None) -> absence.Absence:
    return absence.classify("omp", exit_code, stdout, "", timed_out=False, now=now or datetime.now(UTC))


def review_stream_failures(stdout: str) -> list[str]:
    failures = session_failures(stdout)
    try:
        verdict = parse_output("omp", stdout)
    except ValueError as exc:
        failures.append(f"the final answer no longer parses: {exc}")
    else:
        if not isinstance(verdict, dict) or "decision" not in verdict:
            failures.append("the final answer carries no decision")
    traced = evidence.trace("omp", stdout)
    if not traced.reads:
        failures.append("the trace credits no read call")
    if not traced.greps:
        failures.append("the trace credits no grep call")
    outcome = _classify(stdout, 0)
    if outcome.kind != absence.OK:
        failures.append(f"a clean exit classifies as {outcome.kind}")
    return failures


def terminal_failures(stdout: str, exit_code: int, *, status: int, provider: str, kind: str) -> list[str]:
    failures = session_failures(stdout)
    terminal = omp_terminal_error(stdout)
    if terminal is None:
        return [*failures, "the stream ends without a terminal error message"]
    if terminal.status != status:
        failures.append(f"the terminal error status is {terminal.status!r}, expected {status}")
    if terminal.provider != provider:
        failures.append(f"the terminal error provider is {terminal.provider!r}, expected {provider!r}")
    outcome = _classify(stdout, exit_code)
    if outcome.kind != kind:
        failures.append(f"the failure classifies as {outcome.kind}, expected {kind}")
    if kind == absence.QUOTA and outcome.provider != provider:
        failures.append(f"the quota absence names provider {outcome.provider!r}, expected {provider!r}")
    return failures


def walk_failures(stdout: str, *, first: str, then: str) -> list[str]:
    failures = session_failures(stdout)
    messages = assistant_messages(stdout)
    errors = [(item.get("provider"), item.get("errorStatus")) for item in messages if item.get("stopReason") == "error"]
    if not errors or errors[0] != (first.split("/")[0], 429):
        failures.append(f"the first failure is {errors[0] if errors else None}, expected a 429 from {first}")
    targets = [f"{item.get('provider')}/{item.get('model')}" for item in messages]
    following = next((target for target in targets if target != first), None)
    if following != then:
        failures.append(f"the fallback chain did not move to {then}; the stream visited {targets}")
    return failures


def tool_results(stdout: str) -> list[tuple[str, bool]]:
    return [
        (str(event["message"].get("toolName")), event["message"].get("isError") is True)
        for event in _events(stdout)
        if event.get("type") == "message_end"
        and isinstance(event.get("message"), dict)
        and event["message"].get("role") == "toolResult"
    ]


def refusal_failures(stdout: str) -> list[str]:
    results = dict(tool_results(stdout))
    return [
        f"a {name} call outside the read roster was not refused"
        for name in BREACH_TOOLS
        if results.get(name) is not True
    ]


def elision_failures(stdout: str) -> list[str]:
    traced = evidence.trace("omp", stdout)
    failures = [] if traced.elisions else ["the trace records no elided read span"]
    if ELISION_FOOTER not in stdout:
        failures.append("the read result carries no re-read footer")
    return failures


READ_DEFAULT_LINES = 300


def _ledger_entry(traced: evidence.Trace, suffix: str, *, multi: bool = False) -> tuple[Any, ...] | None:
    for entry in traced.ledger:
        if entry[0].endswith(suffix) and (";" in entry[0]) == multi:
            return entry
    return None


def read_line_failures(stdout: str) -> list[str]:
    traced = evidence.trace("omp", stdout)
    failures = []

    def expect(
        suffix: str,
        label: str,
        start: int,
        end: int,
        *,
        exact: bool = False,
        truncated: bool | None = None,
        total: int | None = None,
    ) -> tuple[Any, ...] | None:
        entry = _ledger_entry(traced, suffix)
        if entry is None:
            failures.append(f"the {label} read is missing from the stream")
            return None
        _, ranges, cut, _ = entry
        if not coverage.contains(ranges, start, end):
            failures.append(f"the {label} read reports lines {list(ranges)}, expected lines {start}-{end} among them")
        if exact and ranges != ((start, end),):
            failures.append(f"the {label} read reports lines {list(ranges)}, expected exactly {start}-{end}")
        if any(high > (total or long_total) for _, high in ranges):
            failures.append(f"the {label} read reports lines beyond the {total or long_total} the file has")
        if truncated is not None and cut is not truncated:
            failures.append(f"the {label} read truncation flag is {cut}, expected {truncated}")
        return entry

    long_total = len(LONG_SOURCE.splitlines())
    expect("equivalence/checker.py", "bare", 1, 2, exact=True, truncated=False)
    expect("long_module.py", "bare long", 1, READ_DEFAULT_LINES, exact=True, truncated=True)
    expect("long_module.py:5-9", "ranged", 5, 9)
    expect("long_module.py:raw", "raw", 1, READ_DEFAULT_LINES, exact=True, truncated=True)
    expect("long_module.py:raw:5-9", "raw ranged", 5, 9, exact=True, truncated=False)
    multi = _ledger_entry(traced, "long_module.py:1-3", multi=True)
    if multi is None:
        failures.append("the multi-path read is missing from the stream")
    elif multi[1]:
        failures.append(
            f"the multi-path read reports lines {list(multi[1])}, expected none for a read of several paths"
        )
    carriage_total = CR_SOURCE.count(b"\n")
    expect("carriage_returns.py", "carriage return", 1, carriage_total, exact=True, total=carriage_total)
    wide_total = WIDE_SOURCE.count("\n")
    expect("wide_line.py", "wide line", 1, WIDE_LINE - 1, total=wide_total)
    wide = expect("wide_line.py", "wide line", WIDE_LINE + 1, wide_total, total=wide_total)
    if wide is not None and coverage.contains(wide[1], WIDE_LINE, WIDE_LINE):
        failures.append(f"the wide line read credits the cut line {WIDE_LINE}")
    window = expect("wide_line.py:3-5", "wide line ranged", 5, 5, total=wide_total)
    if window is not None and coverage.contains(window[1], WIDE_LINE, WIDE_LINE):
        failures.append(f"the wide line ranged read credits the cut line {WIDE_LINE}")
    expect("wide_line.py:raw", "wide line raw", 1, wide_total, total=wide_total)
    return failures


def _tool_names(request: Mapping[str, Any]) -> frozenset[str]:
    body = request.get("body")
    tools = body.get("tools") if isinstance(body, dict) else None
    return frozenset(tool.get("name", "") for tool in tools or () if isinstance(tool, dict))


def _prompt_text(requests: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps([request.get("body") for request in requests])


def _run_failures(run: ClientRun, *, succeeds: bool) -> list[str]:
    failures = []
    if run.timed_out:
        failures.append("the client timed out")
    if run.survivors:
        failures.append(f"the client left {len(run.survivors)} process(es) running")
    if succeeds and run.exit_code != 0:
        failures.append(f"the client exited {run.exit_code}, expected 0")
    if not succeeds and run.exit_code in (0, None):
        failures.append(f"the client exited {run.exit_code} on a failed review, expected a non-zero code")
    return failures


def review_checks(run: ClientRun) -> list[Check]:
    traced = evidence.trace("omp", run.stdout)
    requests = run.upstream
    roster = [
        f"request {index} offered {sorted(_tool_names(item))}"
        for index, item in enumerate(requests, 1)
        if _tool_names(item) != ROSTER
    ]
    served = [
        item
        for item in requests
        if item.get("path") != UPSTREAM_PATH or item.get("body", {}).get("model") != UPSTREAM_MODEL
    ]
    unread = [read for read in traced.reads if read.startswith("http")]
    overlay = []
    if run.canary_hits:
        overlay.append("the read tool fetched a URL despite fetch.enabled false")
    if run.mcp_started:
        overlay.append("a project MCP server started despite mcp.enableProjectConfig false")
    if any(token in _prompt_text(requests) for token in (AGENTS_TOKEN, CLAUDE_TOKEN, NATIVE_TOKEN)):
        overlay.append("project context files reached the model despite disabledProviders")
    if len(requests) != REVIEW_REQUESTS:
        overlay.append(f"the model was called {len(requests)} times, expected {REVIEW_REQUESTS}")
    if traced.elisions:
        overlay.append("a plain read was summarised despite read.summarize.enabled false")
    if unread or traced.failed != 1:
        overlay.append(
            f"the read trace credits {unread} and {traced.failed} failed calls, expected the URL read to fail"
        )
    return [
        _check("live-review-run", (9, 10), _run_failures(run, succeeds=True)),
        _check("live-review-stream", (6, 7, 9), review_stream_failures(run.stdout)),
        _check("live-tool-roster", (1, 2), roster),
        _check(
            "live-gateway-route",
            (4, 5, 12),
            [f"request reached {item.get('path')} for model {item.get('body', {}).get('model')}" for item in served]
            + ([] if requests else ["no request reached the stub upstream through the gateway"]),
        ),
        _check("live-overlay-behaviour", (3, 7), overlay),
        _check(
            "live-no-side-effects",
            (2, 10),
            [f"left session state {path}" for path in run.session_files]
            + (["the review workspace was modified"] if run.workspace_changed else []),
        ),
    ]


def read_line_checks(run: ClientRun) -> list[Check]:
    return [_check("live-read-lines", (7,), _run_failures(run, succeeds=True) + read_line_failures(run.stdout))]


def transport_checks(run: ClientRun) -> list[Check]:
    failures = _run_failures(run, succeeds=True)
    prompt = _prompt_text(run.upstream)
    for token in (TRANSPORT_HEAD, TRANSPORT_TAIL):
        if token not in prompt:
            failures.append(f"a token sent only on standard input did not reach the model request ({token})")
    if TRANSPORT_PROMPT not in prompt:
        failures.append("the prompt sent on standard input did not reach the model request whole and unchanged")
    if any(TRANSPORT_HEAD in item or TRANSPORT_TAIL in item for item in run.argv):
        failures.append("the command line carries the prompt")
    if len(TRANSPORT_PROMPT.encode("utf-8")) <= ARGV_PROMPT_LIMIT:
        failures.append("the transport probe no longer exceeds the command-line limit")
    return [_check("live-prompt-transport", (14,), failures)]


def control_checks(run: ClientRun) -> list[Check]:
    traced = evidence.trace("omp", run.stdout)
    blind = []
    if not run.canary_hits:
        blind.append("the URL canary is no longer observable")
    if not run.mcp_started:
        blind.append("the MCP canary is no longer observable")
    prompt = _prompt_text(run.upstream)
    if NATIVE_TOKEN not in prompt:
        blind.append("the native .omp context canary is no longer observable")
    if not traced.elisions:
        blind.append("the read-summary canary is no longer observable")
    return [
        _check(
            "live-probe-sensitivity",
            (3, 7),
            [f"{item}; update the probe" for item in blind] + _run_failures(run, succeeds=True),
        )
    ]


def _seen(canary: Canary, run: ClientRun) -> tuple[str, ...]:
    prompt = _prompt_text(run.upstream)
    return tuple(where for where in WHERES if canary.token(where) in prompt or canary.marker(where) in run.started)


def blind_canaries(runs: Mapping[str, ClientRun]) -> tuple[Canary, ...]:
    return tuple(
        canary
        for canary in CANARIES
        if (run := runs.get(control_name(canary.group))) is None or not set(canary.required) <= set(_seen(canary, run))
    )


def observed_providers(runs: Mapping[str, ClientRun]) -> frozenset[str]:
    return PROVIDERS - {canary.provider for canary in blind_canaries(runs)}


def provider_checks(runs: Mapping[str, ClientRun]) -> list[Check]:
    failures = []
    for group in GROUPS:
        for name in (control_name(group), overlay_name(group)):
            run = runs.get(name)
            if run is None:
                failures.append(f"the {name} run did not execute")
                continue
            failures.extend(f"{name}: {item}" for item in _run_failures(run, succeeds=True))
            if not run.upstream:
                failures.append(f"{name}: no request reached the stub upstream, so the canaries went unobserved")
        overlay = runs.get(overlay_name(group))
        if overlay is not None:
            failures.extend(
                f"{canary.provider} reached the model from {canary.path} ({where}) despite disabledProviders"
                for canary in group_canaries(group)
                for where in _seen(canary, overlay)
            )
    return [_check("live-provider-canaries", PROBE_ROWS, failures)]


def breach_checks(run: ClientRun) -> list[Check]:
    failures = _run_failures(run, succeeds=True) + refusal_failures(run.stdout)
    if run.workspace_changed:
        failures.append("a tool call outside the read roster changed the review workspace")
    return [_check("live-write-refused", (2,), failures)]


def quota_checks(walk: ClientRun, terminal: ClientRun) -> list[Check]:
    walk_errors = _run_failures(walk, succeeds=False)
    if len(walk.model_chain) < 2:
        walk_errors.append("the configured reviewer role has no fallback candidate to probe")
    else:
        walk_errors.extend(walk_failures(walk.stdout, first=walk.model_chain[0], then=walk.model_chain[1]))
    return [
        _check(
            "live-quota-fallback-walk",
            (4, 6, 9),
            walk_errors,
        ),
        _check(
            "live-quota-classification",
            (6, 8, 9),
            _run_failures(terminal, succeeds=False)
            + terminal_failures(
                terminal.stdout, terminal.exit_code or 1, status=429, provider=QUOTA_PROVIDER, kind=absence.QUOTA
            ),
        ),
    ]


def auth_checks(upstream: ClientRun, bad_token: ClientRun) -> list[Check]:
    walked = {f"{item.get('provider')}" for item in assistant_messages(upstream.stdout)} - {QUOTA_PROVIDER}
    gateway = _run_failures(bad_token, succeeds=False)
    if bad_token.upstream:
        gateway.append("the gateway forwarded a request for a client with the wrong token")
    terminal = omp_terminal_error(bad_token.stdout)
    if terminal is None or "401" not in terminal.message:
        gateway.append("the gateway rejection is not reported as a 401")
    if _classify(bad_token.stdout, bad_token.exit_code or 1).kind in (absence.OK, absence.QUOTA):
        gateway.append("a gateway rejection classifies as usable output or quota")
    return [
        _check(
            "live-auth-classification",
            (4, 8, 9),
            _run_failures(upstream, succeeds=False)
            + terminal_failures(
                upstream.stdout, upstream.exit_code or 1, status=401, provider=QUOTA_PROVIDER, kind=absence.AUTH
            )
            + ([f"an authentication failure walked the fallback chain to {sorted(walked)}"] if walked else []),
        ),
        _check("live-gateway-token", (12,), gateway),
    ]


def served_checks(run: ClientRun) -> list[Check]:
    served = run.served or {}
    empty = []
    if "no-credentials" not in served:
        empty.append("the no-credentials served set was not observed")
    elif served["no-credentials"]:
        empty.append(f"the gateway serves models without credentials: {list(served['no-credentials'])}")
    disabled = []
    credentialed = served.get("credentialed", ())
    if QUOTA_PRIMARY not in credentialed:
        disabled.append(f"the credentialed control does not serve {QUOTA_PRIMARY}")
    if QUOTA_PRIMARY not in served.get("disabled-control", ()):
        disabled.append(f"the equivalent disabled-provider control does not serve {QUOTA_PRIMARY}")
    if "disabled-provider" not in served:
        disabled.append("the disabled-provider served set was not observed")
    elif served["disabled-provider"]:
        disabled.append(f"the gateway serves models despite disabledProviders: {list(served['disabled-provider'])}")
    return [
        _check("live-served-without-credentials", (12, 15), empty),
        _check("live-served-disabled-provider", (12, 16), disabled),
    ]


def rewritten_role_checks(run: ClientRun) -> list[Check]:
    failures = _run_failures(run, succeeds=True)
    expected = run.chosen_model.partition("/")[2]
    if not expected or (run.model_chain and run.chosen_model == run.model_chain[0]):
        failures.append("the rewritten-role probe has no distinct chosen model")
    if not run.upstream:
        failures.append("no rewritten-role request reached the stub upstream")
    for request in run.upstream:
        if request.get("path") != UPSTREAM_PATH or request.get("body", {}).get("model") != expected:
            failures.append(
                f"the rewritten role requested {request.get('body', {}).get('model')!r}, expected {expected}"
            )
    return [_check("live-rewritten-role", (4, 5, 17), failures)]


def live_checks(runs: Mapping[str, ClientRun]) -> list[Check]:
    return [
        *review_checks(runs["review"]),
        *served_checks(runs["review"]),
        *rewritten_role_checks(runs["rewritten-role"]),
        *read_line_checks(runs["read-lines"]),
        *control_checks(runs["control"]),
        *transport_checks(runs["prompt-transport"]),
        *provider_checks(runs),
        *breach_checks(runs["roster-breach"]),
        *quota_checks(runs["quota-walk"], runs["quota-terminal"]),
        *auth_checks(runs["upstream-auth"], runs["bad-token"]),
    ]


def fixture_checks(directory: Path) -> list[Check]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    checks = []
    for name, entry in manifest["streams"].items():
        stdout = (directory / entry["file"]).read_text(encoding="utf-8")
        kind = entry["kind"]
        if kind == "review":
            failures = review_stream_failures(stdout)
        elif kind == "terminal":
            failures = terminal_failures(
                stdout, 1, status=entry["status"], provider=entry["provider"], kind=entry["classification"]
            )
        elif kind == "walk":
            failures = walk_failures(stdout, first=entry["first"], then=entry["then"])
        elif kind == "elision":
            failures = elision_failures(stdout)
        elif kind == "read-lines":
            failures = read_line_failures(stdout)
        elif kind == "refusal":
            failures = refusal_failures(stdout)
        else:
            failures = [f"unknown fixture kind {kind!r}"]
        checks.append(_check(f"{FIXTURE_PREFIX}{name}", entry["rows"], failures))
    return checks
