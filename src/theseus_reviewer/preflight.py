from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import capacity, cli, local, omp_pin, promotion, selection
from .policy import PolicyError, Reviewer

SCHEMA = 1
EXIT_REVIEWABLE = 0
EXIT_NOT_REVIEWABLE = 3
EXIT_UNDETERMINED = 2
PROMOTION_REASON = "contains a Theseus promotion; land the lock change as its own pull request"
UNBUILT_REASON = "the diff cannot be built"
PLAN_UNBUILT_REASON = "the review plan cannot be built"
CATALOG_REASON = "the model catalog cannot be read, so reviewer capacity cannot be determined"

CatalogReader = Callable[[Path, str], Mapping[str, int]]


class Undetermined(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    reviewable: bool
    reasons: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    code: int = EXIT_REVIEWABLE

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "reviewable": self.reviewable,
            "reasons": list(self.reasons),
            "advisories": list(self.advisories),
        }


def reviewable(*advisories: str) -> Verdict:
    return Verdict(True, (), tuple(advisories), EXIT_REVIEWABLE)


def refused(*reasons: str) -> Verdict:
    return Verdict(False, tuple(reasons), (), EXIT_NOT_REVIEWABLE)


def undetermined(*reasons: str) -> Verdict:
    return Verdict(False, tuple(reasons), (), EXIT_UNDETERMINED)


def _sized(plan: Mapping[str, Any], capacities: Mapping[str, int]) -> Verdict:
    if plan["brief_mode"] == "oversize":
        return refused(f"the change lists more files than a review brief can hold; {capacity.SPLIT_INSTRUCTION}")
    load = int(plan.get(capacity.LOAD_KEY) or 0)
    chain: list[Reviewer] = [Reviewer.from_json(item) for item in plan["chain"]]
    held = {**plan, capacity.CAPACITY_KEY: dict(capacities), "pool_blocked_until": {}}
    step = selection.next_step(selection.SelectionInput.from_plan(held, datetime.now(UTC)), [])
    enabled = [reviewer for reviewer in chain if reviewer.enabled]
    unknown = [reviewer for reviewer in enabled if load and reviewer.name not in capacities]
    if step.kind == selection.REVIEW:
        advisories = [
            *(f"{reviewer.name}: {capacity.unknown_text('')}" for reviewer in unknown),
            *(
                f"{reviewer.name}: {capacity.skip_text(load, capacities[reviewer.name])}"
                for reviewer in enabled
                if reviewer.name in capacities and load > capacities[reviewer.name]
            ),
        ]
        return reviewable(*advisories)
    if unknown:
        return undetermined(*step.reasons)
    if step.oversize:
        return refused(f"needs about {load} tokens of reading, above every reviewer's capacity; split")
    return refused(*step.reasons)


def _catalog(trusted: local.Trusted, reader: CatalogReader, plan: Mapping[str, Any]) -> dict[str, int]:
    from ._vendor import yaml

    configured = omp_pin.configured_binary()
    try:
        binary = configured or omp_pin.install(omp_pin.pin_from_text(trusted.policy))
        config_text = dict(trusted.files)[plan["omp_config"]]
        return capacity.measure(plan, config_text, lambda: reader(binary, config_text))
    except (
        omp_pin.PinError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        capacity.CatalogError,
        yaml.YAMLError,
    ) as exc:
        raise Undetermined(f"{CATALOG_REASON}: {exc}") from exc


def _evaluate_plan(run: local.Run, trusted: local.Trusted, reader: CatalogReader) -> Verdict:
    code, _ = run.phase("plan")
    plan_file = run.layout.plan_file
    if code != 0 or not plan_file.is_file():
        raise Undetermined(PLAN_UNBUILT_REASON)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    decision = plan["decision"]
    if decision == cli.HOLD:
        return refused(str(plan["decision_reason"]))
    if decision != cli.REVIEW:
        return reviewable(str(plan["decision_reason"]))
    if plan["brief_mode"] == "oversize" or not int(plan.get(capacity.LOAD_KEY) or 0):
        return _sized(plan, {})
    return _sized(plan, _catalog(trusted, reader, plan))


def _promoted(paths: list[str]) -> bool:
    return promotion.LOCK_PATH in paths or promotion.has_planted_promotion_path(paths)


def evaluate(args: argparse.Namespace, reader: CatalogReader = capacity.read_catalog) -> Verdict:
    root = Path.cwd()
    try:
        local._default_base_ref(args, root)
        head = local._git(root, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
        base = local._git(root, "merge-base", args.base_ref, head).strip()
        files = local.changed_files(root, base, head)
    except (local.Unavailable, local.Refused) as exc:
        raise Undetermined(f"{UNBUILT_REASON}: {exc}") from exc
    if not files:
        return reviewable(f"no committed changes against {args.base_ref}")
    if _promoted([path for item in files for path in item.paths]):
        return refused(PROMOTION_REASON)
    trusted = local.Trusted.read(root, args.trusted_ref or args.base_ref, args.policy)
    work = Path(tempfile.mkdtemp(prefix="oracle-preflight-"))
    try:
        policy = trusted.write(work / "trusted")
        run = local.Run(
            root,
            work,
            policy,
            work / "trusted",
            local._repository(root, args.repo),
            head,
            args.base_ref,
            None,
            uuid.uuid4().hex,
            local.Reporter(),
            "",
        )
        return _evaluate_plan(run, trusted, reader)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _emit(verdict: Verdict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(verdict.to_json(), sort_keys=True))
        return
    print("reviewable" if verdict.reviewable else "not reviewable")
    for reason in verdict.reasons:
        print(f"  reason: {reason}")
    for note in verdict.advisories:
        print(f"  advisory: {note}")


def command_preflight(args: argparse.Namespace, reader: CatalogReader = capacity.read_catalog) -> int:
    try:
        verdict = evaluate(args, reader)
    except Undetermined as exc:
        verdict = undetermined(str(exc))
    except (local.Unavailable, local.Refused, PolicyError) as exc:
        verdict = undetermined(str(exc))
    _emit(verdict, bool(getattr(args, "json", False)))
    return verdict.code


def register(commands: Any) -> None:
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--policy", required=True)
    preflight.add_argument("--repo", default="")
    preflight.add_argument("--base-ref", default="")
    preflight.add_argument("--trusted-ref", default="")
    preflight.add_argument("--head", default="HEAD")
    preflight.add_argument("--json", action="store_true")
    preflight.set_defaults(handler=command_preflight)
