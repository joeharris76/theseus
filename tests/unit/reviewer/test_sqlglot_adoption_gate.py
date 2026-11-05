from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.conftest import CONSUMER_ALPHA

from theseus_reviewer import gate, promotion, protocol, review_check

from .test_sqlglot_adoption import (
    BASE_SHA,
    HEAD_SHA,
    NEW_VERSION,
    OLD_VERSION,
    PIN_PATH,
    RULES,
    Fixture,
    _archive_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ORACLE = "benchbox-oracle[bot]"
SLUG = "BenchBox-dev/BenchBox"
RUN = "c" * 32
SUBMITTED_AT = "2026-11-02T12:00:00Z"
STATUS_AT = "2026-11-02T12:01:00Z"
CONFIG = gate.load_gate_config(".github/oracle-reviewers.yml", CONSUMER_ALPHA)
FILES = [PIN_PATH, _archive_path(NEW_VERSION)]


def _promo(fixture: Fixture, **overrides: Any) -> dict[str, Any]:
    bundle = fixture.prepare()
    assert bundle is not None
    return {**bundle.record.to_json(), **overrides}


def _review(
    promo: dict[str, Any] | None,
    *,
    head_sha: str = HEAD_SHA,
    decision: str = protocol.SHIP,
    open_defects: list[dict[str, Any]] | None = None,
    carried: bool = False,
    kind: str = protocol.FIRST,
    run_uuid: str = RUN,
    last_edited_at: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "v": protocol.MARKER_VERSION,
        "cycle": 1,
        "round": 1,
        "kind": kind,
        "decision": decision,
        "head_sha": head_sha,
        "base_ref": "develop",
        "reviewer": "large",
        "tier": "large",
        "strikes_after": 0,
        "open_defects": open_defects or [],
        "next_id": 1 + len(open_defects or []),
        "patch_digest": "d" * 32,
        "carried": carried,
        "summary": "",
        "run_uuid": run_uuid,
    }
    if promo is not None:
        record["promotion"] = promo
    return {
        "id": 10,
        "login": ORACLE,
        "user_type": "Bot",
        "commit_id": head_sha,
        "state": "COMMENTED",
        "submitted_at": SUBMITTED_AT,
        "body": f"### oracle-verdict: success for `{head_sha}`\n\n{protocol.encode_marker(record)}",
        "last_edited_at": last_edited_at,
        "includes_created_edit": False,
    }


def _status(state: str = "success", run_uuid: str = RUN) -> dict[str, Any]:
    return {
        "id": 20,
        "context": "oracle-verdict",
        "state": state,
        "description": f"large: SHIP [run {run_uuid}]",
        "login": ORACLE,
        "user_type": "Bot",
        "created_at": STATUS_AT,
    }


def _pull(*, head_repo: str = SLUG, changed: int = 2) -> dict[str, Any]:
    return {
        "changed_files": changed,
        "base": {"ref": "develop", "sha": BASE_SHA},
        "head": {"sha": HEAD_SHA, "repo": {"full_name": head_repo}},
    }


def _serve_git(monkeypatch: pytest.MonkeyPatch, fixture: Fixture) -> None:
    monkeypatch.setattr(
        review_check, "_request", lambda _token, url: fixture.git_json(url.removeprefix(review_check.API_ROOT + "/"))
    )
    monkeypatch.setattr(
        review_check,
        "_fetch_blob",
        lambda _token, repo, sha: fixture.git_bytes(
            f"repos/{repo}/git/blobs/{sha}", accept="application/vnd.github.raw"
        ),
    )


def _refusals(
    monkeypatch: pytest.MonkeyPatch,
    fixture: Fixture,
    reviews: list[dict[str, Any]],
    *,
    statuses: list[dict[str, Any]] | None = None,
    files: list[str] | None = None,
    listed: int | None = None,
    changed: int = 2,
    head_repo: str = SLUG,
    rules: Any = RULES,
) -> tuple[bool, list[str]]:
    chosen = FILES if files is None else files

    _serve_git(monkeypatch, fixture)
    return review_check.fetch_adoption_refusals(
        "tok",
        SLUG,
        _pull(head_repo=head_repo, changed=changed),
        chosen,
        len(chosen) if listed is None else listed,
        reviews,
        statuses=[_status()] if statuses is None else statuses,
        oracle_login=CONFIG.binding.bot_login,
        rules=rules,
    )


def test_a_matching_unedited_source_aware_review_clears_the_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    adopting, reasons = _refusals(monkeypatch, fixture, [_review(_promo(fixture))])
    assert (adopting, reasons) == (True, [])
    status, message = review_check.decide(
        HEAD_SHA,
        FILES,
        [_status()],
        lambda _files: False,
        None,
        [],
        binding=CONFIG.binding,
        reviews=[_review(_promo(fixture))],
        adoption_refusals=reasons,
        is_promotion=adopting,
    )
    assert status == review_check.PASS and "pass (oracle verdict" in message


def test_a_pin_bump_is_not_exempt_for_the_gate_and_a_standin_cannot_waive_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    adopting, reasons = _refusals(monkeypatch, fixture, [])
    assert adopting and reasons
    standin = {
        "login": "joeharris76",
        "user_type": "User",
        "body": f"Stand-in oracle review: APPROVE {HEAD_SHA}",
        "created_at": "2026-11-02T12:05:00Z",
        "updated_at": "2026-11-02T12:05:00Z",
    }
    status, message = review_check.decide(
        HEAD_SHA,
        FILES,
        [],
        lambda _files: False,
        None,
        [standin],
        binding=CONFIG.binding,
        adoption_refusals=reasons,
        is_promotion=adopting,
    )
    assert status == review_check.WAITING and "refused" in message
    status, _ = review_check.decide(
        HEAD_SHA,
        FILES,
        [],
        lambda _files: False,
        None,
        [standin],
        binding=CONFIG.binding,
        adoption_refusals=[],
        is_promotion=adopting,
    )
    assert status == review_check.WAITING


def test_a_review_without_a_promotion_record_is_not_adoption_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    _, reasons = _refusals(monkeypatch, fixture, [_review(None)])
    assert any("no source-aware evidence record" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"old_artifact_sha256": "0" * 64}, "old runtime hash"),
        ({"new_artifact_sha256": "0" * 64}, "new runtime hash"),
        ({"lock_sha256": "0" * 64}, "pin binding"),
        ({"head_sha": "c" * 40}, "record head"),
        ({"bootstrap": True}, "bootstrap"),
    ],
)
def test_the_record_must_bind_the_base_and_head_pins_and_head(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    fixture = Fixture()
    _, reasons = _refusals(monkeypatch, fixture, [_review(_promo(fixture, **overrides))])
    assert any(message in reason for reason in reasons)


def test_a_review_made_against_a_moved_base_no_longer_clears_the_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewed = Fixture()
    moved = Fixture(base_fork_sha="5" * 40, base_value=7)
    stale = _review(_promo(reviewed))
    _, reasons = _refusals(monkeypatch, moved, [stale])
    assert any("old runtime hash" in reason or "pin binding" in reason for reason in reasons)


def test_a_review_of_an_earlier_head_does_not_clear_a_later_head(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    earlier = _review(_promo(fixture, head_sha="d" * 40), head_sha="d" * 40)
    _, reasons = _refusals(monkeypatch, fixture, [earlier])
    assert any("no unedited oracle review" in reason for reason in reasons)


def test_open_defects_do_not_ship_carried_and_edited_reviews_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    promo = _promo(fixture)
    defect = {"id": "D1", "severity": "High", "path": "a.py", "line": 1, "title": "t", "body": "b"}
    cases = {
        "decided": _review(promo, decision=protocol.DO_NOT_SHIP),
        "open defect": _review(promo, open_defects=[defect]),
        "carried": _review(promo, carried=True),
        "edited": _review(promo, last_edited_at="2026-11-02T13:00:00Z"),
    }
    for name, review in cases.items():
        _, reasons = _refusals(monkeypatch, fixture, [review])
        assert reasons, name


def test_the_review_must_belong_to_the_latest_verdict_status_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    review = _review(_promo(fixture))
    _, reasons = _refusals(monkeypatch, fixture, [review], statuses=[_status(run_uuid="e" * 32)])
    assert any("run_uuid" in reason for reason in reasons)


def test_a_head_archive_that_is_not_the_pinned_blob_refuses_even_with_a_review(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    review = _review(_promo(fixture))
    fixture.blobs[(HEAD_SHA, _archive_path(NEW_VERSION))] += b"tampered"
    _, reasons = _refusals(monkeypatch, fixture, [review])
    assert any("do not match the pin sha256" in reason for reason in reasons)
    del fixture.blobs[(HEAD_SHA, _archive_path(NEW_VERSION))]
    _, reasons = _refusals(monkeypatch, fixture, [review])
    assert any("is not present at the head" in reason for reason in reasons)


def test_an_invalid_head_pin_or_a_removed_pin_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    fixture.set_head_pin('{"schema": 1}')
    adopting, reasons = _refusals(monkeypatch, fixture, [])
    assert adopting and any("missing or unexpected keys" in reason for reason in reasons)
    del fixture.blobs[(HEAD_SHA, PIN_PATH)]
    assert _refusals(monkeypatch, fixture, []) == (True, ["sqlglot runtime pin cannot be removed"])


def test_a_new_pin_without_a_base_pin_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    del fixture.blobs[(BASE_SHA, PIN_PATH)]
    assert _refusals(monkeypatch, fixture, []) == (True, ["sqlglot runtime adoption needs a pin on the base branch"])


def test_a_fork_pull_request_cannot_adopt(monkeypatch: pytest.MonkeyPatch) -> None:
    adopting, reasons = _refusals(monkeypatch, Fixture(), [], head_repo="someone/BenchBox")
    assert adopting and "fork pull requests cannot adopt" in reasons[0]


def test_unrelated_pull_requests_read_nothing_and_are_not_adoptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any) -> bytes:
        raise AssertionError("no read expected")

    monkeypatch.setattr(review_check, "_request", explode)
    monkeypatch.setattr(review_check, "_fetch_blob", explode)
    pull = _pull(changed=1)
    for rules in (RULES, None):
        assert review_check.fetch_adoption_refusals(
            "tok", SLUG, pull, ["docs/readme.md"], 1, [], oracle_login="benchbox-oracle", rules=rules
        ) == (False, [])
    assert review_check.fetch_adoption_refusals(
        "tok", SLUG, pull, FILES, 2, [], oracle_login="benchbox-oracle", rules=None
    ) == (False, [])


def test_an_unchanged_pin_with_a_touched_archive_still_needs_a_real_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel
    assert _refusals(monkeypatch, fixture, [], files=[_archive_path(OLD_VERSION)], changed=1) == (True, [])
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel + b"x"
    adopting, reasons = _refusals(monkeypatch, fixture, [], files=[_archive_path(OLD_VERSION)], changed=1)
    assert adopting and any("do not match the pin sha256" in reason for reason in reasons)


def test_an_incomplete_file_list_refuses_adoption_scope_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture()
    review = _review(_promo(fixture))
    _, reasons = _refusals(monkeypatch, fixture, [review], files=["docs/readme.md"], listed=1, changed=300)
    assert any("file list is incomplete" in reason for reason in reasons)
    same = Fixture()
    same.blobs[(HEAD_SHA, PIN_PATH)] = same.base_pin.encode()
    same.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = same.base_wheel
    adopting, reasons = _refusals(monkeypatch, same, [], files=["docs/readme.md"], listed=1, changed=300)
    assert not adopting and not reasons


def test_incomplete_file_lists_cannot_hide_an_invalid_unchanged_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel + b"changed"
    adopting, reasons = _refusals(monkeypatch, fixture, [], files=["docs/readme.md"], listed=1, changed=300)
    status, _ = review_check.decide(
        HEAD_SHA,
        ["docs/readme.md"],
        [_status()],
        lambda files: False,
        binding=CONFIG.binding,
        adoption_refusals=reasons,
        is_promotion=adopting,
    )
    assert status == review_check.WAITING


def test_an_unpinned_archive_refuses_with_or_without_a_pin_change(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = "benchbox/_vendor/extra.whl"
    fixture = Fixture()
    review = _review(_promo(fixture))
    _, reasons = _refusals(monkeypatch, fixture, [review], files=[*FILES, extra], changed=3)
    assert any("no pin names" in reason for reason in reasons)
    same = Fixture()
    same.blobs[(HEAD_SHA, PIN_PATH)] = same.base_pin.encode()
    same.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = same.base_wheel
    adopting, reasons = _refusals(monkeypatch, same, [], files=[extra], changed=1)
    assert adopting and any("no pin names" in reason for reason in reasons)
    del same.blobs[(BASE_SHA, PIN_PATH)]
    del same.blobs[(HEAD_SHA, PIN_PATH)]
    adopting, reasons = _refusals(monkeypatch, same, [], files=[extra], changed=1)
    assert adopting and any("no pin names" in reason for reason in reasons)


def test_swapping_the_pinned_archive_for_the_new_pinned_one_is_not_an_unpinned_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture()
    files = [PIN_PATH, _archive_path(OLD_VERSION), _archive_path(NEW_VERSION)]
    _, reasons = _refusals(monkeypatch, fixture, [_review(_promo(fixture))], files=files, changed=3)
    assert reasons == []


@pytest.mark.parametrize("target", ["pin", "archive"])
def test_a_symlinked_head_pin_or_archive_refuses_instead_of_reading_its_target(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    fixture = Fixture()
    review = _review(_promo(fixture))
    fixture.modes[(HEAD_SHA, PIN_PATH if target == "pin" else _archive_path(NEW_VERSION))] = "120000"
    adopting, reasons = _refusals(monkeypatch, fixture, [review])
    assert adopting and len(reasons) == 1 and "not a regular file" in reasons[0]


def test_the_planted_staging_directory_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [*FILES, f"{promotion.STAGED_PROMOTION_DIR}/new/x.py"]
    adopting, reasons = _refusals(monkeypatch, Fixture(), [], files=files)
    assert adopting and "cannot touch" in reasons[0]


def test_evaluate_combines_the_adoption_refusal_with_the_gate_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(CONFIG, policy=replace(CONFIG.policy, sqlglot_adoption=RULES))
    pull = _pull(changed=1)
    monkeypatch.setattr(gate, "read_pull", lambda *_args: (pull, "develop", HEAD_SHA))
    monkeypatch.setattr(gate, "fetch_files", lambda *_args: [{"filename": "docs/readme.md"}])
    monkeypatch.setattr(gate, "fetch_statuses", lambda *_args: [_status()])
    monkeypatch.setattr(gate, "fetch_base_change_date", lambda *_args: None)
    monkeypatch.setattr(gate, "fetch_comments", lambda *_args: [])
    monkeypatch.setattr(gate, "fetch_reviews", lambda *_args: [])
    monkeypatch.setattr(gate, "annotate_receipt", lambda *_args: None)
    monkeypatch.setattr(gate, "fetch_bump_refusals", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gate, "fetch_promotion_refusals", lambda *_args, **_kwargs: (False, []))
    seen: dict[str, Any] = {}

    def adoption(*_args: Any, **kwargs: Any) -> tuple[bool, list[str]]:
        seen.update(kwargs)
        return True, ["sqlglot adoption has no unedited oracle review"]

    monkeypatch.setattr(gate, "fetch_adoption_refusals", adoption)
    _, status, message = gate.evaluate(SLUG, 7, "token", config)
    assert seen["rules"] == RULES and seen["oracle_login"] == CONFIG.binding.bot_login
    assert status == review_check.WAITING and "no unedited oracle review" in message
