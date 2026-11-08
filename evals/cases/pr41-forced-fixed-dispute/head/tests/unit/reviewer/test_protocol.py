from __future__ import annotations

from theseus_reviewer import protocol


def test_a_fixed_prior_is_published_as_fixed() -> None:
    judgement = protocol.judge([protocol.Prior("D1", protocol.FIXED, "the guard now exists at line 12")], {})

    assert judgement.dispositions[0].label == protocol.FIXED
    assert judgement.maintained_ids == ()


def test_a_not_fixed_prior_is_maintained_and_counts_toward_escalation() -> None:
    judgement = protocol.judge([protocol.Prior("D1", protocol.NOT_FIXED, "still unguarded")], {})

    assert judgement.maintained_ids == ("D1",)
    assert protocol.escalates({"D1": 1}, judgement) == ["D1"]


def test_a_conceded_dispute_is_withdrawn() -> None:
    judgement = protocol.judge(
        [protocol.Prior("D1", protocol.NOT_FIXED, "the rebuttal holds", concedes=True)], {"D1": "Dispute: handled"}
    )

    assert judgement.dispositions[0].label == protocol.WITHDRAWN


def test_a_fixed_disposition_on_a_forced_dispute_round_stays_open() -> None:
    prior = protocol.Prior("D1", protocol.FIXED, "the guard already exists at line 12, so the finding is wrong")

    judgement = protocol.judge([prior], {"D1": "Dispute: the guard exists at line 12"}, forced=True)

    assert judgement.maintained_ids == ("D1",)
    assert judgement.dispositions[0].label == protocol.MAINTAINED
