from __future__ import annotations

from typing import Any

import pytest

from conftest import CONSUMER_ALPHA, CONSUMER_BETA
from theseus_reviewer import gate, omp_pin
from theseus_reviewer import review_check as check

ALPHA = gate.load_gate_config(".github/review.yml", CONSUMER_ALPHA)
BETA = gate.load_gate_config(".github/review.yml", CONSUMER_BETA)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

HEAD = "a" * 40
POLICY_PATH = ".github/review.yml"
POLICY = (
    "retry: 1\nomp:\n  version: {version}\n  builds:\n    darwin-arm64:\n      url: https://b.example/mac\n"
    "      sha256: {mac}\n    linux-x64:\n      url: https://b.example/linux\n      sha256: {linux}\n"
)
OLD = POLICY.format(version="18.12.1", mac="a1" * 32, linux="b2" * 32)
NEW = POLICY.format(version="18.13.2", mac="c3" * 32, linux="d4" * 32)
NEW_DIGEST = omp_pin.pin_digest(omp_pin.pin_from_text(NEW))


def _evidence(
    version: str = "18.13.2", sha: str = "c3" * 32, pin: str = NEW_DIGEST, head: str = HEAD, **fields: Any
) -> dict[str, Any]:
    line = (
        f"Omp candidate check: version={version} platform=darwin-arm64 sha256={sha} pin={pin} head={head} "
        "result=releasable"
    )
    return {
        "login": "joeharris76",
        "user_type": "User",
        "body": f"from the Mac\n{line}\n",
        "created_at": "2026-10-27T20:00:00Z",
        "updated_at": "2026-10-27T20:00:00Z",
        **fields,
    }


def _pull(changed: int = 1) -> dict[str, Any]:
    return {"changed_files": changed, "base": {"ref": "develop"}, "head": {"sha": HEAD, "repo": {"full_name": "o/r"}}}


@pytest.fixture
def texts(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    served = {"develop": OLD, HEAD: NEW}
    monkeypatch.setattr(check, "fetch_text", lambda _token, _repo, _path, ref: served[ref])
    return served


def _refusals(
    comments: list[dict[str, Any]],
    files: list[str],
    pull: dict[str, Any] | None = None,
    listed: int | None = None,
    config: gate.GateConfig = ALPHA,
) -> list[str]:
    return check.fetch_bump_refusals(
        "t",
        "o/r",
        _pull(len(files)) if pull is None else pull,
        files,
        len(files) if listed is None else listed,
        comments,
        policy_path=POLICY_PATH,
        fixture_prefix=config.consumer.omp_fixture_prefix,
        attesters=config.binding.attesters,
    )


def _gate(
    comments: list[dict[str, Any]], files: list[str] | None = None, config: gate.GateConfig = ALPHA
) -> tuple[int, str]:
    files = [POLICY_PATH] if files is None else files
    refusals = _refusals(comments, files, config=config)
    return check.decide(HEAD, files, [], lambda _f: True, None, [], binding=config.binding, bump_refusals=refusals)


def test_a_pin_bump_with_no_owner_evidence_is_held(texts: dict[str, str]) -> None:
    status, message = _gate([])
    assert status == check.WAITING
    assert "omp pin bump refused" in message and "has no owner evidence" in message


@pytest.mark.parametrize(
    "comment",
    [
        _evidence(pin="9" * 64),
        _evidence(version="18.13.1"),
        _evidence(head="b" * 40),
        _evidence(sha="e9" * 32),
        _evidence(updated_at="2026-10-27T21:00:00Z"),
        _evidence(login="someone-else"),
        _evidence(user_type="Bot"),
    ],
)
def test_evidence_that_is_wrong_edited_or_not_the_owners_does_not_release_the_bump(
    texts: dict[str, str], comment: dict[str, Any]
) -> None:
    status, message = _gate([comment])
    assert status == check.WAITING and "has no owner evidence" in message


def test_matching_unedited_owner_evidence_releases_the_bump(texts: dict[str, str]) -> None:
    status, message = _gate([_evidence()])
    assert status == check.WAITING
    assert "omp pin bump refused" not in message and "waiting for the review's verdict" in message


def test_a_bump_that_changes_other_files_is_held_even_with_evidence(texts: dict[str, str]) -> None:
    status, message = _gate([_evidence()], [POLICY_PATH, "benchbox/core/x.py"])
    assert status == check.WAITING and "also changes benchbox/core/x.py" in message


def test_a_pull_request_that_leaves_the_pin_alone_is_not_held(texts: dict[str, str]) -> None:
    texts[HEAD] = OLD.replace("retry: 1", "retry: 2")
    assert _refusals([], [POLICY_PATH], _pull()) == []


def test_a_pull_request_without_the_policy_file_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object) -> str:
        raise AssertionError("no read expected")

    monkeypatch.setattr(check, "fetch_text", boom)
    assert _refusals([], ["README.md"], _pull()) == []


def test_a_truncated_file_list_still_checks_the_pin(texts: dict[str, str]) -> None:
    reasons = " ".join(_refusals([], ["README.md"], _pull(5000), 3000))
    assert "has no owner evidence" in reasons and "file list is incomplete" in reasons


def test_a_truncated_file_list_holds_the_bump_even_with_matching_owner_evidence(texts: dict[str, str]) -> None:
    reasons = _refusals([_evidence()], [POLICY_PATH], _pull(5000), 3000)
    assert len(reasons) == 1 and "file list is incomplete" in reasons[0]


def test_evidence_for_an_earlier_head_does_not_survive_a_later_push(texts: dict[str, str]) -> None:
    later = "f" * 40
    pull = {**_pull(), "head": {"sha": later, "repo": {"full_name": "o/r"}}}
    texts[later] = NEW
    reasons = _refusals([_evidence()], [POLICY_PATH], pull)
    assert len(reasons) == 1 and "has no owner evidence" in reasons[0] and later[:9] in reasons[0]


def test_an_unreadable_policy_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(*_args: object) -> str:
        raise check.CheckError("HTTP 500")

    monkeypatch.setattr(check, "fetch_text", unreadable)
    with pytest.raises(check.CheckError):
        _refusals([], [POLICY_PATH], _pull())


def test_the_owner_evidence_attesters_come_from_the_binding(texts: dict[str, str]) -> None:
    owner = _evidence()
    beta_owner = _evidence(login="beta-owner")
    assert "has no owner evidence" not in " ".join(_refusals([owner], [POLICY_PATH]))
    assert "has no owner evidence" in " ".join(_refusals([beta_owner], [POLICY_PATH]))
    assert "has no owner evidence" in " ".join(_refusals([owner], [POLICY_PATH], config=BETA))
    for attester in ("beta-owner", "beta-deputy"):
        evidence = _evidence(login=attester)
        assert "has no owner evidence" not in " ".join(_refusals([evidence], [POLICY_PATH], config=BETA))
    status, message = _gate([_evidence(login="beta-deputy")], config=BETA)
    assert status == check.WAITING and "omp pin bump refused" not in message


def test_captured_fixtures_under_the_consumer_prefix_ride_along_with_a_bump(texts: dict[str, str]) -> None:
    prefix = ALPHA.consumer.omp_fixture_prefix
    assert prefix
    fixture = f"{prefix}capture.jsonl"
    assert _refusals([_evidence()], [POLICY_PATH, fixture]) == []
    assert "also changes" in " ".join(_refusals([_evidence()], [POLICY_PATH, f"{prefix}capture.txt"]))
    assert "also changes" in " ".join(_refusals([_evidence()], [POLICY_PATH, fixture], config=BETA))
    beta_fixture = f"{BETA.consumer.omp_fixture_prefix}capture.jsonl"
    assert _refusals([_evidence(login="beta-owner")], [POLICY_PATH, beta_fixture], config=BETA) == []
