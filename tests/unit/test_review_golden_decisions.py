from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "review_golden"
HEADING = re.compile(r"(?:### review-verdict: |<!-- review-verdict: v2 )(?P<state>[a-z]+) ")
DECISION = re.compile(r'<!-- review-protocol: \{.*?"decision": "(?P<decision>[A-Z_]+)"')


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [text for item in items for text in _strings(item)]


def _receipts() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        for text in _strings(json.loads(path.read_text(encoding="utf-8"))):
            heading, record = HEADING.search(text), DECISION.search(text)
            if heading and record:
                found.append((path.name, heading.group("state"), record.group("decision")))
    return found


def test_every_golden_receipt_with_a_record_ships_if_and_only_if_the_state_is_success() -> None:
    receipts = _receipts()
    assert {name for name, _, _ in receipts} >= {"first_review_ships.json", "first_review_with_a_defect.json"}
    for name, state, decision in receipts:
        assert (decision == "SHIP") == (state == "success"), (name, state, decision)
