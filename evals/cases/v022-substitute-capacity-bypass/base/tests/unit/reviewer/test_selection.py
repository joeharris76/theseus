from __future__ import annotations

from theseus_reviewer.selection import Reviewer, SelectionInput, next_reviewer, skip_reason


def test_a_load_above_the_slot_capacity_is_skipped() -> None:
    selection = SelectionInput(load_tokens=500, capacities={"small": 400, "large": 900})

    assert skip_reason(Reviewer("small"), selection) is not None
    assert next_reviewer([Reviewer("small"), Reviewer("large")], selection) == Reviewer("large")


def test_an_unknown_capacity_fails_closed() -> None:
    selection = SelectionInput(load_tokens=10, capacities={}, capacity_error="catalog unreadable")

    assert "unknown" in (skip_reason(Reviewer("small"), selection) or "")
