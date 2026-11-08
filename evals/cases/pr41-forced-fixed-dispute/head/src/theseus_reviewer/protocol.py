from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FIXED = "fixed"
NOT_FIXED = "not_fixed"
WITHDRAWN = "withdrawn"
MAINTAINED = "maintained"
ESCALATION_ROUNDS = 2


@dataclass(frozen=True)
class Prior:
    id: str
    status: str
    evidence: str
    concedes: bool = False


@dataclass(frozen=True)
class Disposition:
    id: str
    label: str
    evidence: str


@dataclass(frozen=True)
class Judgement:
    dispositions: tuple[Disposition, ...]
    maintained_ids: tuple[str, ...]


def judge(priors: Sequence[Prior], disputed: Mapping[str, str], forced: bool = False) -> Judgement:
    dispositions = []
    maintained = []
    for prior in priors:
        status = NOT_FIXED if forced and prior.id in disputed and prior.status == FIXED else prior.status
        if prior.id in disputed and prior.concedes:
            dispositions.append(Disposition(prior.id, WITHDRAWN, prior.evidence))
        elif status == FIXED:
            dispositions.append(Disposition(prior.id, FIXED, prior.evidence))
        else:
            dispositions.append(Disposition(prior.id, MAINTAINED, prior.evidence))
            maintained.append(prior.id)
    return Judgement(tuple(dispositions), tuple(maintained))


def escalates(maintained_rounds: Mapping[str, int], judgement: Judgement) -> list[str]:
    return [id for id in judgement.maintained_ids if maintained_rounds.get(id, 0) + 1 >= ESCALATION_ROUNDS]
