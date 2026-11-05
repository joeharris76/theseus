from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from . import coverage
from .coverage import Ranges, Returned
from .verdict import FIXED, MAX_PATH, NOT_FIXED, WITHDRAWN, Finding, Verdict, display_path, sanitize, valid_path

INLINE = "inline"
COMPACTION_EVENT = "auto_compaction_start"
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


TRACED = ("omp",)
OMP_READERS = frozenset({"read", "grep"})
_CHUNK = r"(?:L?\d+(?:(?:-|\.\.)(?:L?\d+)?|\+L?\d+)?|-\d+)"
_RANGES = rf"{_CHUNK}(?:,:?{_CHUNK})*"
OMP_SELECTOR = re.compile(rf":(?:raw(?::{_RANGES})?|{_RANGES}(?::raw)?)$")
ARCHIVE_EXTENSIONS = (
    "tar.gz|tar.bz2|tar.xz|tar.zst|tar.z|tar|tgz|tbz2|tbz|txz|tzst|zip|jar|war|ear|apk|whl|ipa|xpi|vsix|nupkg|cbz"
    "|rar|cbr|7z|iso|cab|cpio|rpm|ar|a|lib|deb|lzh|lha|arj|asar|gz|bz2|xz|zst|z|lzma"
)
_ARCHIVE_READ = re.compile(
    rf"(?P<container>.+?\.(?:{ARCHIVE_EXTENSIONS}))(?::(?P<member>.*))?\Z", re.IGNORECASE | re.DOTALL
)
BINARY_ARCHIVE_ENTRY = "[Cannot read binary archive entry"
BINARY_PROBE_BYTES = 8000
FONT_EXTENSIONS = frozenset({".eot", ".otf", ".ttf", ".woff", ".woff2"})
_TRACE_PATHS = 5
_TRACE_PATH_WIDTH = 80
_TRACE_LIMIT = 700
OMP_NO_MATCHES = "No matches found"
CITATION_PREFIX = "Citation not found in the head commit"
_LINE_CITATION = re.compile(r":(?P<line>\d+)")
_REPRO_CITATION = re.compile(r"(?i)\brepro\s*:[^\S\n]*")
_CITATION_OPENERS = frozenset("([{<\"'`,;=*>|")
_CITATION_CLOSERS = frozenset(",;:)]}>\"'`!?")
UNVERIFIED_WITHDRAWAL = (
    "the withdrawal names no examined head-commit file and line that refutes it, so it counts as not fixed"
)
UNVERIFIED_MAINTAIN = (
    "the maintain names no examined head-commit file and line or reproduction, so the disposition is unsupported"
)
DEMOTED_FIXED = (
    "the fix names no verified examined head-commit file and line or reproduction, so it counts as not fixed"
)


@dataclass(frozen=True)
class Trace:
    reads: tuple[str, ...] = ()
    greps: tuple[str, ...] = ()
    elisions: tuple[tuple[str, int, int], ...] = ()
    failed: int = 0
    spans: tuple[tuple[str, Ranges], ...] = ()
    ledger: tuple[tuple[str, Ranges, bool, bool], ...] = ()


def _completed_messages(stdout: str) -> Iterable[dict]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "message_end" and isinstance(event.get("message"), dict):
            yield event["message"]


@dataclass(frozen=True)
class _Outcome:
    found: bool
    text: str
    spans: int
    lines: int
    ranges: Ranges = ()
    truncated: bool = False


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _shown_bounds(details: dict, display: dict) -> tuple[int | None, int | None] | None:
    truncation = details.get("truncation")
    meta = details.get("meta")
    meta_truncation = meta.get("truncation") if isinstance(meta, dict) else None
    shown = meta_truncation.get("shownRange") if isinstance(meta_truncation, dict) else None
    low: int | None = None
    high: int | None = None
    if isinstance(shown, dict):
        low, high = _int(shown.get("start")), _int(shown.get("end"))
        if low is None or high is None:
            return None
    if isinstance(truncation, dict):
        if truncation.get("firstLineExceedsLimit") is True:
            return None
        if truncation.get("truncated") is True and low is None:
            low = _int(display.get("startLine"))
            output = _int(truncation.get("outputLines"))
            if low is None or output is None:
                return None
            high = low + output - 1
    return low, high


def _returned_ranges(details: object) -> Ranges:
    if not isinstance(details, dict) or isinstance(details.get("summary"), dict):
        return ()
    display = details.get("displayContent")
    numbers = display.get("lineNumbers") if isinstance(display, dict) else None
    if not isinstance(numbers, list):
        return ()
    bounds = _shown_bounds(details, display)
    if bounds is None:
        return ()
    low, high = bounds
    total = _int(details.get("totalLines"))
    lines = sorted({n for n in numbers if _int(n) is not None and n >= 1})
    lines = [
        n for n in lines if (low is None or n >= low) and (high is None or n <= high) and (total is None or n <= total)
    ]
    truncation = details.get("truncation")
    if lines and isinstance(truncation, dict) and truncation.get("lastLinePartial") is True:
        lines.pop()
    return coverage.merge(lines)


def _outcome(message: dict) -> _Outcome:
    content = message.get("content")
    text = (
        "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        if isinstance(content, list)
        else ""
    )
    details = message.get("details")
    summary = details.get("summary") if isinstance(details, dict) else None
    spans, lines = (
        (_count(summary.get(key)) for key in ("elidedSpans", "elidedLines")) if isinstance(summary, dict) else (0, 0)
    )
    found = message.get("isError") is False
    if found and message.get("toolName") == "grep":
        found = not text.strip().startswith(OMP_NO_MATCHES)
    ranges = _returned_ranges(details) if found and message.get("toolName") == "read" else ()
    truncation = details.get("truncation") if isinstance(details, dict) else None
    truncated = isinstance(truncation, dict) and truncation.get("truncated") is True
    return _Outcome(found, text, spans, lines, ranges, truncated)


def _archive_container(part: str) -> str | None:
    match = None if ";" in part else _ARCHIVE_READ.fullmatch(part)
    if match is None:
        return None
    member = match.group("member") or ""
    if "?" in member or ".." in member.split("/"):
        return None
    return match.group("container")


def _omp_reads(stdout: str) -> Trace:
    calls: dict[str, tuple[str, str]] = {}
    results: dict[str, _Outcome] = {}
    for message in _completed_messages(stdout):
        if message.get("role") == "toolResult":
            results[str(message.get("toolCallId"))] = _outcome(message)
            continue
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "toolCall" or block.get("name") not in OMP_READERS:
                continue
            arguments = block.get("arguments")
            if isinstance(arguments, dict) and isinstance(arguments.get("path"), str) and arguments["path"]:
                calls[str(block.get("id"))] = (str(block.get("name")), arguments["path"])
    reads: list[str] = []
    greps: list[str] = []
    elisions: list[tuple[str, int, int]] = []
    spans: list[tuple[str, Ranges]] = []
    ledger: list[tuple[str, Ranges, bool, bool]] = []
    failed = 0
    for call, (name, path) in calls.items():
        outcome = results.get(call)
        if outcome is None or not outcome.found:
            failed += 1
            continue
        if name == "grep":
            greps.append(path)
            continue
        if _archive_container(path) is not None and outcome.text.lstrip().startswith(BINARY_ARCHIVE_ENTRY):
            failed += 1
            continue
        reads.append(path)
        if outcome.spans or outcome.lines:
            elisions.append((path, outcome.spans, outcome.lines))
        if outcome.ranges:
            spans.append((path, outcome.ranges))
        ledger.append((path, outcome.ranges, outcome.truncated, bool(outcome.spans or outcome.lines)))
    return Trace(tuple(reads), tuple(greps), tuple(elisions), failed, tuple(spans), tuple(ledger))


def compacted(harness: str, stdout: str) -> bool:
    if harness != "omp":
        return False
    for line in stdout.splitlines():
        if COMPACTION_EVENT not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == COMPACTION_EVENT:
            return True
    return False


def trace(harness: str, stdout: str) -> Trace:
    return _omp_reads(stdout) if harness == "omp" else Trace()


def _plain(path: str) -> str | None:
    segments = path.split("/")
    colon = next((index for index, segment in enumerate(segments) if ":" in segment), None)
    if colon is not None and ".." in segments[colon:]:
        return None
    return path


def _inside(root: str, candidate: str) -> bool:
    return os.path.realpath(os.path.join(root, candidate)).startswith(root + os.sep)


def _literal(root: str, part: str, workspace: Path) -> str | None:
    target = _plain(part)
    if target is None:
        return None
    candidate = _normalize(target, workspace)
    return candidate if _inside(root, candidate) and os.path.exists(os.path.join(root, candidate)) else None


def _ambiguous(root: str, part: str, workspace: Path) -> bool:
    literal = _literal(root, part, workspace)
    if literal is None:
        return False
    for match in re.finditer(r"[:?]", part):
        prefix = part[: match.start()]
        if not prefix:
            continue
        candidate = _normalize(prefix, workspace)
        if candidate != literal and _inside(root, candidate) and os.path.isfile(os.path.join(root, candidate)):
            return True
    return False


def _single(root: str, part: str, workspace: Path) -> str | None:
    request = part.strip()
    if not request or ";" in request or _ambiguous(root, request, workspace):
        return None
    candidate = _literal(root, request, workspace)
    if candidate is None:
        if _archive_container(request) is not None:
            return None
        target = _plain(OMP_SELECTOR.sub("", request))
        if target is None:
            return None
        candidate = _normalize(target, workspace)
        if not _inside(root, candidate):
            return None
    return candidate if os.path.isfile(os.path.join(root, candidate)) else None


def returned(run: Trace, workspace: Path) -> dict[str, Ranges]:
    root = os.path.realpath(workspace)
    gathered: dict[str, list[Ranges]] = {}
    for path, ranges in run.spans:
        target = _single(root, path, workspace)
        if target is not None:
            gathered.setdefault(target, []).append(ranges)
    return {path: coverage.join(*groups) for path, groups in gathered.items()}


def evaluate(
    run: Trace, targets: Mapping[str, coverage.Target], needed: Iterable[str], workspace: Path
) -> coverage.Report:
    root = os.path.realpath(workspace)
    touched = {
        coverage.canonical(target, workspace)
        for target in (_single(root, path, workspace) for path in run.reads)
        if target is not None
    }
    return coverage.evaluate(targets, needed, returned(run, workspace), workspace, touched)


def _real(path: str, workspace: Path) -> str:
    return os.path.realpath(os.path.join(os.path.realpath(workspace), path))


def _missing(required: Iterable[str], seen: Iterable[str], workspace: Path) -> list[str]:
    real = {_real(path, workspace) for path in seen}
    return [path for path in required if _real(path, workspace) not in real]


def _clip(path: str) -> str:
    return path if len(path) <= _TRACE_PATH_WIDTH else "\u2026" + path[-(_TRACE_PATH_WIDTH - 1) :]


def _listed(paths: Iterable[str]) -> str:
    items = list(paths)
    shown = ", ".join(_clip(item) for item in items[:_TRACE_PATHS])
    return f"[{shown}{f' and {len(items) - _TRACE_PATHS} more' if len(items) > _TRACE_PATHS else ''}]"


def describe(run: Trace, required: Iterable[str], workspace: Path, report: coverage.Report | None = None) -> str:
    needed = sorted(set(required))
    seen = returned(run, workspace)
    text = (
        f"trace: reads={len(run.reads)} greps={len(run.greps)} failed={run.failed}"
        f" credited={_listed(sorted(seen))} required={len(needed)}"
        f" missing={_listed(_missing(needed, seen, workspace))}"
        f" elided_reads={len(run.elisions)} elided_spans={sum(item[1] for item in run.elisions)}"
        f" elided_lines={sum(item[2] for item in run.elisions)}"
    )
    if report is not None:
        text += f" hunks_covered={report.covered}/{report.total} files_open={len(report.files)}"
    return sanitize(text, _TRACE_LIMIT)


def _normalize(path: str, workspace: Path) -> str:
    root = os.path.realpath(workspace)
    candidate = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    if candidate.startswith(root + os.sep):
        candidate = candidate[len(root) + 1 :]
    elif os.path.realpath(candidate).startswith(root + os.sep):
        candidate = os.path.realpath(candidate)[len(root) + 1 :]
    return candidate.removeprefix("./")


def relative(path: str, workspace: Path) -> str:
    return _normalize(_LINE_SUFFIX.sub("", path), workspace)


def _line_count(workspace: Path, path: str) -> int | None:
    root = os.path.realpath(workspace)
    target = os.path.realpath(os.path.join(root, path))
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        return None
    with open(target, "rb") as handle:
        data = handle.read()
    return max(1, data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1))


def _citation_error(finding: Finding, workspace: Path) -> str | None:
    count = _line_count(workspace, finding.file)
    if count is None:
        return f"{display_path(finding.file)} is not a file in the head commit"
    last = finding.end_line or finding.line
    if last > count:
        return f"{display_path(finding.file)} has {count} lines, so line {last} does not exist"
    return None


def _cited_path(candidate: str) -> str | None:
    cited = candidate.removeprefix("./")
    return cited if valid_path(cited) else None


def _line_citations(evidence: str) -> Iterable[tuple[str, int]]:
    for match in _LINE_CITATION.finditer(evidence or ""):
        start = evidence.rfind("\n", 0, match.start()) + 1
        head = evidence[max(start, match.start() - MAX_PATH - 2) : match.start()]
        for index, char in enumerate(head):
            if index and not (head[index - 1].isspace() or head[index - 1] in _CITATION_OPENERS):
                continue
            cited = _cited_path(head[index:])
            if cited is not None:
                yield cited, int(match.group("line"))


def _path_ends(following: str) -> bool:
    char = following[0]
    return (
        char.isspace() or char in _CITATION_CLOSERS or (char == "." and (len(following) == 1 or following[1].isspace()))
    )


def _repro_citations(evidence: str) -> Iterable[str]:
    for match in _REPRO_CITATION.finditer(evidence or ""):
        rest = evidence[match.end() :].split("\n", 1)[0][:MAX_PATH]
        for end in range(1, len(rest) + 1):
            following = rest[end : end + 2]
            if following and not _path_ends(following):
                continue
            cited = _cited_path(rest[:end])
            if cited is not None:
                yield cited


def _returned_line(read: Returned, workspace: Path, cited: str, line: int) -> bool:
    return any(
        coverage.contains(ranges, line, line)
        for path, ranges in read.items()
        if path == cited or coverage.canonical(path, workspace) == coverage.canonical(cited, workspace)
    )


def _withdrawal_verified(evidence: str, workspace: Path, read: Returned) -> bool:
    for cited, line in _line_citations(evidence):
        if line < 1 or not _returned_line(read, workspace, cited, line):
            continue
        count = _line_count(workspace, cited)
        if count is not None and line <= count:
            return True
    return False


def withdrawal_cited(evidence_text: str | None, read: Returned | None = None) -> bool:
    return any(
        read is None or coverage.contains(read.get(cited, ()), line, line)
        for cited, line in _line_citations(evidence_text or "")
    )


def _repro_verified(evidence: str, workspace: Path, read: Returned) -> bool:
    root = os.path.realpath(workspace)
    for cited in _repro_citations(evidence):
        if not any(
            ranges and (path == cited or coverage.canonical(path, workspace) == coverage.canonical(cited, workspace))
            for path, ranges in read.items()
        ):
            continue
        target = os.path.realpath(os.path.join(root, cited))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            continue
        return True
    return False


def maintain_verified(evidence: str, workspace: Path, read: Returned) -> bool:
    return _withdrawal_verified(evidence, workspace, read) or _repro_verified(evidence, workspace, read)


def mark_citations(
    verdict: Verdict, workspace: Path, disputed: Iterable[str] = (), read: Returned = MappingProxyType({})
) -> Verdict:
    marked = []
    for finding in verdict.defects:
        error = _citation_error(finding, workspace)
        detail = f"{CITATION_PREFIX}: {error}. {finding.detail}".rstrip() if error else finding.detail
        marked.append(replace(finding, detail=detail))
    wanted = set(disputed)
    priors = []
    for prior in verdict.prior_defects:
        if prior.status == WITHDRAWN and not _withdrawal_verified(prior.evidence, workspace, read):
            note = f"{prior.evidence} {UNVERIFIED_WITHDRAWAL}".strip()
            prior = replace(prior, status=NOT_FIXED, evidence=note, demoted="withdrawal")
        if prior.status == FIXED and prior.id in wanted and not maintain_verified(prior.evidence, workspace, read):
            note = f"{prior.evidence} {DEMOTED_FIXED}".strip()
            prior = replace(prior, status=NOT_FIXED, evidence=note, demoted="fixed")
        if prior.status == NOT_FIXED and prior.id in wanted and not maintain_verified(prior.evidence, workspace, read):
            note = f"{prior.evidence} {UNVERIFIED_MAINTAIN}".strip()
            prior = replace(prior, evidence=note)
        priors.append(prior)
    return replace(verdict, defects=tuple(marked), prior_defects=tuple(priors))


def _shown(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


@dataclass(frozen=True)
class Gap:
    reason: str
    files: tuple[str, ...]


def is_binary_candidate(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in FONT_EXTENSIONS


def has_nul_head(workspace: Path, path: str) -> bool:
    root = os.path.realpath(workspace)
    target = os.path.realpath(os.path.join(root, path))
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        return False
    try:
        with open(target, "rb") as handle:
            return b"\0" in handle.read(BINARY_PROBE_BYTES)
    except OSError:
        return False


def _segments_match(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_segments_match(path[skip:], pattern[1:]) for skip in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], pattern[0]) and _segments_match(path[1:], pattern[1:])


def glob_match(path: str, pattern: str) -> bool:
    return _segments_match(tuple(path.split("/")), tuple(pattern.split("/")))


def required_files(
    required: Iterable[str],
    workspace: Path,
    binary_files: Iterable[str],
    exempt_paths: Iterable[str],
    is_protected: Callable[[str], bool],
) -> list[str]:
    hinted = {relative(p, workspace) for p in binary_files}
    detected_binary = {path for path in hinted if is_binary_candidate(path) and has_nul_head(workspace, path)}
    patterns = tuple(exempt_paths)

    def _is_exempt(path: str) -> bool:
        norm = relative(path, workspace)
        if is_protected(norm) or is_protected(_normalize(path, workspace)):
            return False
        return norm in detected_binary or any(glob_match(norm, pattern) for pattern in patterns)

    return sorted(path for path in set(required) if not _is_exempt(path))


def check(
    verdict: Verdict,
    *,
    clears: bool,
    harness: str,
    brief_mode: str,
    workspace: Path,
    required: Iterable[str],
    run: Trace,
    targets: Mapping[str, coverage.Target],
    binary_files: Iterable[str] = (),
    exempt_paths: Iterable[str] = (),
    is_protected: Callable[[str], bool],
) -> Gap | None:
    if not clears:
        return None
    needed = required_files(required, workspace, binary_files, exempt_paths, is_protected)
    if harness in TRACED:
        if brief_mode == INLINE:
            absent = [path for path in needed if path not in targets or not targets[path].listed]
            if absent:
                return Gap(f"the verdict would ship but the brief omits the diff of {_shown(absent)}", tuple(absent))
        else:
            if needed and not any(path in targets and targets[path].listed for path in needed):
                return Gap(
                    f"the verdict would ship but the staged diff lists none of the required files: {_shown(needed)}",
                    tuple(needed),
                )
            report = evaluate(run, targets, needed, workspace)
            if not report.complete:
                return Gap(f"the verdict would ship but {coverage.reason(report)}", report.files)
    if brief_mode == INLINE:
        return None
    examined = {relative(path, workspace) for path in verdict.files_examined}
    missing = [path for path in needed if path not in examined]
    if missing:
        return Gap(f"the verdict would ship but did not examine {_shown(missing)}", tuple(missing))
    return None
