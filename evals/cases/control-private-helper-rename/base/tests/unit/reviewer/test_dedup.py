from __future__ import annotations

from theseus_reviewer import dedup


def test_titles_differing_only_in_case_and_spacing_share_a_fingerprint() -> None:
    assert dedup.fingerprint("a.py", "Off by one") == dedup.fingerprint("a.py", "  off   BY one ")


def test_different_paths_never_share_a_fingerprint() -> None:
    assert dedup.fingerprint("a.py", "Off by one") != dedup.fingerprint("b.py", "Off by one")
