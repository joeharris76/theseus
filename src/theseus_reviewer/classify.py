from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .classifier import matching
from .decisions import DecisionsError, DecisionsSession
from .jev import TOTAL_TIMEOUT_SECONDS, JevError, JevSession
from .policy import SIZE_BUCKETS

CONFIDENCE_BAR = 0.60
MAX_SCORED_FILES = 40
GATE_BUCKET_FLOOR = "large"
SIZE_FLOOR_BUCKET = "large"
BACKEND_JEV = "jev"
BACKEND_DECISIONS = "decisions"
BACKEND_UNAVAILABLE = "unavailable"

SIZE_QUESTION = {
    "type": "choice",
    "instructions": (
        "Classify the size of this pull request. File count alone never pushes a change up a bucket: judge by "
        "component spread and behavioral novelty. When a change sits between two buckets, choose the smaller one; "
        "over-classification wastes expensive frontier models."
    ),
    "criteria": {
        "trivial": (
            "Tiny changes of little importance, e.g. spelling correction. Also: dependency/lockfile bumps, "
            "generated-file refreshes, doc-only or comment-only edits, and changes with no behavior change even "
            "across several files."
        ),
        "small": (
            "Simple changes with slightly more surface area, e.g. adding a new method param and calling it. "
            "Bounded tweaks, new tests for existing behavior, tiny single-purpose scripts."
        ),
        "medium": (
            "Adding or removing code in a single module, e.g. new feature no refactoring. Confined to one "
            "component or area: one workflow, one tool, one adapter, one doc tree. Multiple files within one "
            "area are still medium."
        ),
        "large": (
            "Adding or removing code over multiple modules or a large feature. Spans two or more "
            "components/areas, a single change with broad blast radius, or deletion of live machinery."
        ),
        "huge": (
            "Deep refactoring or multi-surface feature addition. Restructures existing code across areas, or "
            "adds a subsystem spanning surfaces such as backend plus UI or CLI."
        ),
        "max": "New product surfaces, architecture changes, and deep feature redesigns.",
    },
}
CAPABILITY_QUESTION = {
    "type": "choice",
    "instructions": "Which review capability does this change need? Answer routine for bounded, familiar, or mechanical changes a standard reviewer clears. Answer deep for novel logic, cross-cutting changes, vendored or generated code, reviewer-selection logic, or wide multi-area features.",
    "criteria": {
        "routine": "Small or familiar change: few areas, no new algorithms, no vendored/generated code, no review-policy logic.",
        "deep": "Novel or risky change: new logic paths, vendored/generated artifacts, cross-cutting edits across many areas, or changes to review/selection machinery itself.",
    },
}
TEMPLATE_QUESTION = {
    "type": "choice",
    "instructions": "Choose the review template. standard: bounded familiar change, single pass. deep: novel or cross-cutting change needing multi-pass scrutiny (blast radius, deletions, enforcement). gate: the change touches review/merge machinery itself and needs bypass analysis with zero benefit of doubt.",
    "criteria": {
        "standard": "Bounded, familiar, single-area change; routine scrutiny suffices.",
        "deep": "Novel logic, cross-cutting edits, large features, or deletions of live machinery; needs blast-radius and deletion passes.",
        "gate": "Touches review/merge machinery (reviewers, verdicts, gates, selection, scoring); needs bypass analysis.",
    },
}
FILE_IMPACT_QUESTION = {
    "type": "choice",
    "instructions": "How impactful is this single file change within its pull request? Answer low for generated, vendored, lockfile, docs-only, test-only, or tiny mechanical edits. Answer high for product logic, workflows, schemas, configs, review/gate machinery, new components, or large deletions of live code.",
    "criteria": {
        "low": "Generated/vendored/lockfile/docs/test-only content, or trivially small mechanical edits with no behavior change.",
        "high": "Behavior-changing code, workflows, schemas, configs, review or gate machinery, new components, or removal of live machinery.",
    },
}
FILE_SCORE_QUESTION = {
    "type": "score",
    "instructions": "How much reviewer scrutiny does this file change deserve?",
    "criteria": [
        "Mechanical change needing no scrutiny.",
        "Worth a skim for obvious mistakes.",
        "Standard careful read.",
        "High-risk change needing adversarial scrutiny.",
        "Critical path: the review must not miss a defect here.",
    ],
}


@dataclass(frozen=True)
class FileRow:
    path: str
    impact: str
    impact_probability: float
    score: int

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "impact": self.impact,
            "impact_probability": self.impact_probability,
            "score": self.score,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FileRow:
        return cls(
            path=data["path"],
            impact=data["impact"],
            impact_probability=float(data["impact_probability"]),
            score=int(data["score"]),
        )


@dataclass(frozen=True)
class FloorRules:
    size_min_lines: int | None = None
    size_exclude: tuple[str, ...] = ()
    min_bucket: str | None = None


@dataclass(frozen=True)
class Classification:
    size_bucket: str
    capability: str
    template: str
    confidence: float
    gate_flags: tuple[str, ...]
    files: tuple[FileRow, ...]
    backend: str = BACKEND_JEV
    model: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)
    floors: Mapping[str, Any] = field(default_factory=dict)
    degraded_from: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    file_backend: str = ""
    file_model: str = ""

    @property
    def degraded(self) -> bool:
        return self.backend != BACKEND_JEV or bool(self.degraded_from)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "size_bucket": self.size_bucket,
            "capability": self.capability,
            "template": self.template,
            "confidence": self.confidence,
            "gate_flags": list(self.gate_flags),
            "files": [item.to_json() for item in self.files],
            "backend": self.backend,
        }
        optional = {
            "model": self.model,
            "answers": dict(self.answers),
            "floors": dict(self.floors),
            "degraded_from": self.degraded_from,
            "usage": dict(self.usage),
            "file_backend": self.file_backend,
            "file_model": self.file_model,
        }
        data.update({key: value for key, value in optional.items() if value})
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Classification:
        legacy = BACKEND_UNAVAILABLE if data.get("source") == "fallback" else BACKEND_JEV
        return cls(
            size_bucket=data["size_bucket"],
            capability=data["capability"],
            template=data["template"],
            confidence=float(data["confidence"]),
            gate_flags=tuple(data.get("gate_flags", [])),
            files=tuple(FileRow.from_json(item) for item in data.get("files", [])),
            backend=str(data.get("backend") or legacy),
            model=str(data.get("model") or ""),
            answers=dict(data.get("answers") or {}),
            floors=dict(data.get("floors") or {}),
            degraded_from=str(data.get("degraded_from") or ""),
            usage=dict(data.get("usage") or {}),
            file_backend=str(data.get("file_backend") or ""),
            file_model=str(data.get("file_model") or ""),
        )


def pr_state(
    repo: str,
    pr: int | None,
    base_sha: str,
    head_sha: str,
    stats: Sequence[tuple[Any, ...]],
    gate_flags: Sequence[str],
) -> dict[str, Any]:
    subject = f"PR #{pr}" if pr is not None else "Unopened local change"
    top = sorted(stats, key=lambda item: -(item[1] + item[2]))[:12]
    lines = "; ".join(f"{item[0]} +{item[1]}/-{item[2]}" for item in top)
    gate = " Gate signals: touches review/merge machinery." if gate_flags else " Gate signals: none."
    total_added = sum(item[1] for item in stats)
    total_deleted = sum(item[2] for item in stats)
    return {
        "text": (
            f"{subject} in {repo}: {len(stats)} files, +{total_added}/-{total_deleted}, base {base_sha} head "
            f"{head_sha}.{gate} Largest changes: {lines}."
        )
    }


def file_state(
    path: str,
    added: int,
    deleted: int,
    status: str,
    siblings: Sequence[tuple[str, int, int]],
) -> dict[str, Any]:
    areas: dict[str, list[int]] = {}
    for sibling, a, d in siblings:
        top = sibling.split("/")[0] if "/" in sibling else "(root)"
        cell = areas.setdefault(top, [0, 0])
        cell[0] += a
        cell[1] += d
    ranked = sorted(areas.items(), key=lambda item: -(item[1][0] + item[1][1]))[:5]
    context = ", ".join(f"{name} +{cell[0]}/-{cell[1]}" for name, cell in ranked)
    return {"text": (f"File: {path} (+{added}/-{deleted}, {status}). Sibling areas by churn: {context}.")}


PR_QUESTIONS = {"size": SIZE_QUESTION, "capability": CAPABILITY_QUESTION, "template": TEMPLATE_QUESTION}
FILE_QUESTIONS = {"impact": FILE_IMPACT_QUESTION, "importance": FILE_SCORE_QUESTION}
MALFORMED = (KeyError, TypeError, ValueError, AttributeError)
PATH_CAP = 300


def _choice(answer: Mapping[str, Any]) -> tuple[str, dict[str, float], float]:
    probs = {key: float(value) for key, value in answer["probabilities"].items()}
    return str(answer["choice"]), probs, float(answer["confidence"])


def _score_to_int(answer: Mapping[str, Any]) -> int:
    return min(5, max(1, round(float(answer["score"])) + 1))


def apply_rules(
    size_answer: Mapping[str, Any],
    capability_answer: Mapping[str, Any],
    template_answer: Mapping[str, Any],
    gate_flags: Sequence[str],
) -> tuple[str, str, str, float]:
    size, size_probs, size_confidence = _choice(size_answer)
    if size not in SIZE_BUCKETS:
        raise ValueError(f"unknown size bucket {size!r}")
    bucket = size
    if size_probs.get(size, 0.0) < CONFIDENCE_BAR:
        stepped = SIZE_BUCKETS.index(size) - 1
        bucket = SIZE_BUCKETS[max(stepped, SIZE_BUCKETS.index("small"))]
    if bucket == "trivial" and size_probs.get("trivial", 0.0) < CONFIDENCE_BAR:
        bucket = "small"
    capability, capability_probs, _ = _choice(capability_answer)
    if capability not in ("routine", "deep"):
        raise ValueError(f"unknown capability {capability!r}")
    if capability_probs.get(capability, 0.0) < CONFIDENCE_BAR:
        capability = "routine"
    template, _, _ = _choice(template_answer)
    if template not in ("standard", "deep", "gate"):
        raise ValueError(f"unknown template {template!r}")
    if gate_flags:
        template = "gate"
        bucket = SIZE_BUCKETS[max(SIZE_BUCKETS.index(bucket), SIZE_BUCKETS.index(GATE_BUCKET_FLOOR))]
    return bucket, capability, template, size_confidence


def changed_lines(stats: Sequence[tuple[Any, ...]], exclude: Sequence[str]) -> int:
    total = 0
    for item in stats:
        path, added, deleted = item[0], item[1], item[2]
        prev = item[4] if len(item) > 4 and item[4] else None
        paths = (path,) if not prev else (path, prev)
        if not (bool(exclude) and all(matching([p], exclude) for p in paths)):
            total += added + deleted
    return total


def apply_floors(
    bucket: str,
    stats: Sequence[tuple[str, int, int, str]],
    gate_flags: Sequence[str],
    rules: FloorRules,
    *,
    min_bucket: bool,
) -> tuple[str, dict[str, Any]]:
    lines = None if rules.size_min_lines is None else changed_lines(stats, rules.size_exclude)
    candidates: dict[str, str] = {}
    if gate_flags:
        candidates["gate"] = GATE_BUCKET_FLOOR
    if rules.size_min_lines is not None and lines is not None and lines >= rules.size_min_lines:
        candidates["size"] = SIZE_FLOOR_BUCKET
    if min_bucket and rules.min_bucket:
        candidates["min_bucket"] = rules.min_bucket
    final = max([SIZE_BUCKETS.index(bucket), *(SIZE_BUCKETS.index(item) for item in candidates.values())])
    raised = [
        name for name, item in candidates.items() if SIZE_BUCKETS.index(item) == final > SIZE_BUCKETS.index(bucket)
    ]
    floors = {
        "gate": bool(gate_flags),
        "size_lines": lines,
        "size_floor": "size" in candidates,
        "min_bucket": rules.min_bucket if min_bucket else None,
        "rules_bucket": bucket,
        "raised_by": raised,
    }
    return SIZE_BUCKETS[final], floors


def _decide(
    answers: Mapping[str, Any],
    stats: Sequence[tuple[str, int, int, str]],
    gate_flags: Sequence[str],
    rules: FloorRules,
    *,
    backend: str,
    model: str,
) -> Classification:
    bucket, capability, template, confidence = apply_rules(
        answers["size"], answers["capability"], answers["template"], gate_flags
    )
    bucket, floors = apply_floors(bucket, stats, gate_flags, rules, min_bucket=backend == BACKEND_DECISIONS)
    return Classification(
        size_bucket=bucket,
        capability=capability,
        template=template,
        confidence=confidence,
        gate_flags=tuple(gate_flags),
        files=(),
        backend=backend,
        model=model,
        answers={name: dict(answers[name]) for name in PR_QUESTIONS},
        floors=floors,
    )


def _row(path: str, answers: Mapping[str, Any]) -> FileRow:
    impact, impact_probs, _ = _choice(answers["impact"])
    if impact not in ("low", "high"):
        raise ValueError(f"unknown impact {impact!r}")
    return FileRow(
        path=path,
        impact=impact,
        impact_probability=impact_probs.get(impact, 0.0),
        score=_score_to_int(answers["importance"]),
    )


def _ranked(stats: Sequence[tuple[str, int, int, str]]) -> list[tuple[str, int, int, str]]:
    return sorted(stats, key=lambda item: -(item[1] + item[2]))[:MAX_SCORED_FILES]


def _malformed(exc: Exception) -> JevError:
    return JevError(
        None,
        f"the judgment service answered unusable content: {type(exc).__name__}",
        reason="answered unusable content",
    )


def classify_diff(
    session: JevSession,
    *,
    repo: str,
    pr: int | None,
    base_sha: str,
    head_sha: str,
    stats: Sequence[tuple[str, int, int, str]],
    gate_flags: Sequence[str],
    rules: FloorRules = FloorRules(),
    deadline_seconds: float = TOTAL_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Classification:
    started = clock()
    state = pr_state(repo, pr, base_sha, head_sha, stats, gate_flags)
    answers = session.ask(state, PR_QUESTIONS)
    try:
        decided = _decide(answers, stats, gate_flags, rules, backend=BACKEND_JEV, model=session.model)
    except MALFORMED as exc:
        raise _malformed(exc) from exc
    rows = []
    for item in _ranked(stats):
        path, added, deleted, status = item[0], item[1], item[2], item[3]
        try:
            if clock() - started > deadline_seconds:
                raise JevError(None, "classification deadline exceeded", reason="classification deadline exceeded")
            siblings = [(other[0], other[1], other[2]) for other in stats if other[0] != path]
            file_answers = session.ask(file_state(path, added, deleted, status, siblings), FILE_QUESTIONS)
            try:
                rows.append(_row(path, file_answers))
            except MALFORMED as exc:
                raise _malformed(exc) from exc
        except JevError as exc:
            exc.partial = decided
            raise
    return replace(decided, files=tuple(rows))


def decisions_batch(
    stats: Sequence[tuple[str, int, int, str]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    ranked = _ranked(stats)
    areas: dict[str, list[int]] = {}
    for item in stats:
        path, added, deleted = item[0], item[1], item[2]
        cell = areas.setdefault(path.split("/")[0] if "/" in path else "(root)", [0, 0])
        cell[0] += added
        cell[1] += deleted
    state = {
        "total_files": len(stats),
        "files": [
            {"index": index, "path": item[0][:PATH_CAP], "added": item[1], "deleted": item[2], "status": item[3]}
            for index, item in enumerate(ranked, 1)
        ],
        "areas": [
            {"area": name, "added": cell[0], "deleted": cell[1]}
            for name, cell in sorted(areas.items(), key=lambda item: -(item[1][0] + item[1][1]))[:5]
        ],
    }
    questions: dict[str, dict[str, Any]] = {}
    for index in range(1, len(ranked) + 1):
        prefix = f"Consider only the file with index {index} in the files list. "
        questions[f"impact.{index}"] = {
            **FILE_IMPACT_QUESTION,
            "instructions": prefix + FILE_IMPACT_QUESTION["instructions"],
        }
        questions[f"importance.{index}"] = {
            **FILE_SCORE_QUESTION,
            "instructions": prefix + FILE_SCORE_QUESTION["instructions"],
        }
    return state, questions, [item[0] for item in ranked]


def decisions_rows(session: DecisionsSession, stats: Sequence[tuple[str, int, int, str]]) -> tuple[FileRow, ...]:
    state, questions, paths = decisions_batch(stats)
    answers = session.ask(state, questions)
    return tuple(
        _row(path, {"impact": answers[f"impact.{index}"], "importance": answers[f"importance.{index}"]})
        for index, path in enumerate(paths, 1)
    )


def classify_decisions(
    session: DecisionsSession,
    *,
    repo: str,
    pr: int | None,
    base_sha: str,
    head_sha: str,
    stats: Sequence[tuple[str, int, int, str]],
    gate_flags: Sequence[str],
    rules: FloorRules = FloorRules(),
    degraded_from: str = "",
) -> Classification:
    state = pr_state(repo, pr, base_sha, head_sha, stats, gate_flags)
    answers = session.ask(state, PR_QUESTIONS)
    decided = _decide(answers, stats, gate_flags, rules, backend=BACKEND_DECISIONS, model=session.model)
    try:
        rows = decisions_rows(session, stats)
    except DecisionsError as exc:
        exc.partial = decided
        raise
    return replace(decided, files=rows, model=session.model, usage=dict(session.usage), degraded_from=degraded_from)


def fallback_classification(
    unavailable_bucket: str,
    gate_flags: Sequence[str],
    answered_bucket: str | None = None,
    *,
    stats: Sequence[tuple[str, int, int, str]] = (),
    rules: FloorRules = FloorRules(),
    degraded_from: str = "",
) -> Classification:
    bucket = unavailable_bucket
    if answered_bucket in SIZE_BUCKETS:
        bucket = SIZE_BUCKETS[max(SIZE_BUCKETS.index(bucket), SIZE_BUCKETS.index(answered_bucket))]
    bucket, floors = apply_floors(bucket, stats, gate_flags, rules, min_bucket=False)
    return Classification(
        size_bucket=bucket,
        capability="deep",
        template="gate" if gate_flags else "deep",
        confidence=0.0,
        gate_flags=tuple(gate_flags),
        files=(),
        backend=BACKEND_UNAVAILABLE,
        floors=floors,
        degraded_from=degraded_from,
    )


def classification_note(classification: Classification) -> str:
    cause = classification.degraded_from or "TypeSafe"
    if classification.backend == BACKEND_DECISIONS:
        raised = ", ".join(classification.floors.get("raised_by") or []) or "none"
        return (
            f"Classification: {cause}; classified by OpenAI Decisions {classification.model}: "
            f"size {classification.size_bucket} (floor: {raised}), template {classification.template}."
        )
    if classification.backend == BACKEND_UNAVAILABLE:
        return (
            f"Classification unavailable ({cause}): reviewed from the "
            f"{classification.size_bucket} bucket with the {classification.template} template."
        )
    if classification.file_backend:
        return (
            f"Classification: TypeSafe answered the change; per-file priority came from OpenAI Decisions "
            f"{classification.file_model} ({cause})."
        )
    if classification.degraded_from:
        return f"Classification: TypeSafe answered the change; per-file priority is unavailable ({classification.degraded_from})."
    return ""
