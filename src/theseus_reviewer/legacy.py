from __future__ import annotations

WORD = "oracle"
MARKER_PREFIX = f"{WORD}-"

STATUS_CONTEXT_PAIRS = {"review-verdict": "oracle-verdict", "review-gate": "oracle-gate"}
VERDICT_HEADING_CONTEXT = "oracle-verdict"

PROTOCOL_MARKER_OPEN = f"<!-- {MARKER_PREFIX}protocol:"
DEFECT_MARKER_NAME = f"{MARKER_PREFIX}defect"
FINDING_MARKER_OPEN = f"<!-- {MARKER_PREFIX}finding:"
LOCAL_RECORD_MARKER_OPEN = f"<!-- {MARKER_PREFIX}local-record:"

STATE_ARTIFACT_PREFIX = "oracle-verdict-state-"
ATTEMPT_ARTIFACT_PREFIX = "oracle-verdict-attempt-"
WORKFLOW_PATH = ".github/workflows/oracle-verdict.yml"

OWNER_COMMAND = "/oracle-review"
STANDIN_APPROVAL = "Stand-in oracle review: APPROVE"
ADJUDICATION_PREFIX = "Oracle adjudication: "
BACKOFF_RESET_PREFIX = "Oracle backoff reset: "

STAGED_PROMOTION_DIR = f".{WORD}-promotion"
STAGED_DIFF_NAME = f".{WORD}-pull-request.diff"
VERDICT_CAPTURE_ARTIFACT = "oracle-verdict-capture"
SWEEP_FAILURE_SURFACE_PREFIX = "oracle-verdict failed twice without a result at: "
REVERT_SUBJECT_PREFIX = "revert(oracle): restore omp "


def mirror_contexts(context: str) -> tuple[str, ...]:
    legacy = STATUS_CONTEXT_PAIRS.get(context)
    return (legacy,) if legacy else ()


def context_aliases(context: str) -> frozenset[str]:
    return frozenset((context, *mirror_contexts(context)))
