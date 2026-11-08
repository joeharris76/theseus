from __future__ import annotations

from theseus_reviewer.selection import Reviewer, SelectionInput, next_reviewer, skip_reason


def test_a_load_above_the_slot_capacity_is_skipped() -> None:
    selection = SelectionInput(load_tokens=500, capacities={"small": 400, "large": 900})

    reason, _ = skip_reason(Reviewer("small"), selection)
    assert reason is not None
    assert next_reviewer([Reviewer("small"), Reviewer("large")], selection) == Reviewer("large")


def test_an_unknown_capacity_fails_closed() -> None:
    selection = SelectionInput(load_tokens=10, capacities={}, capacity_error="catalog unreadable")

    reason, _ = skip_reason(Reviewer("small"), selection)
    assert "unknown" in (reason or "")


def test_a_served_fallback_runs_when_the_primary_is_not_served() -> None:
    selection = SelectionInput(
        load_tokens=300,
        capacities={"small": 400},
        availability=True,
        served=frozenset({"fallback"}),
        windows={"fallback": 1000},
        candidates={"small": ("primary", "fallback")},
    )

    reason, resolution = skip_reason(Reviewer("small"), selection)
    assert reason is None
    assert resolution is not None and resolution.substituted and resolution.target == "fallback"


def test_no_served_candidate_is_unavailable() -> None:
    selection = SelectionInput(
        load_tokens=300,
        capacities={"small": 400},
        availability=True,
        served=frozenset(),
        windows={"fallback": 1000},
        candidates={"small": ("primary", "fallback")},
    )

    reason, _ = skip_reason(Reviewer("small"), selection)
    assert reason is not None and reason.startswith("unavailable")
