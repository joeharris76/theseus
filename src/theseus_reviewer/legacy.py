from __future__ import annotations

WORD = "oracle"
MARKER_PREFIX = f"{WORD}-"

VERDICT_HEADING_CONTEXT = "oracle-verdict"
PROTOCOL_MARKER_OPEN = f"<!-- {MARKER_PREFIX}protocol:"
DEFECT_MARKER_NAME = f"{MARKER_PREFIX}defect"
FINDING_MARKER_OPEN = f"<!-- {MARKER_PREFIX}finding:"
LOCAL_RECORD_MARKER_OPEN = f"<!-- {MARKER_PREFIX}local-record:"
ADJUDICATION_PREFIX = "Oracle adjudication: "
BACKOFF_RESET_PREFIX = "Oracle backoff reset: "
