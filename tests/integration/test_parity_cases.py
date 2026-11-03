from __future__ import annotations

import json
from typing import Any

import pytest

from theseus_reviewer.policy import SIZE_BUCKETS

from . import fake_github
from .fake_github import OWNER_TOKEN
from .golden import run_capture
from .harness import ALPHA_ROOT, CHECKER, POLICY, SLUG, Harness
from .parity import record, step, tree

pytestmark = [pytest.mark.integration, pytest.mark.medium]

FIXED_CHECKER = "def checker(a, b):\n    if a is None or b is None:\n        return False\n    return a == b\n"
IDENTITY_CHECKER = "def checker(a, b):\n    return a is b\n"
SHIP = {"decision": "SHIP", "summary": "No defects.", "files_examined": [CHECKER]}
HIGH = {
    "severity": "High",
    "file": CHECKER,
    "line": 2,
    "title": "Identity comparison replaces equality",
    "detail": "checker returns a is b, so equal values held in distinct objects compare unequal.",
}
BLOCKING = {"decision": "SHIP_WITH_FIXES", "summary": "One defect.", "files_examined": [CHECKER], "defects": [HIGH]}
STRANGER_TOKEN = "gho_strangertoken" + "S" * 23
REBUTTAL = f"Dispute: identity is intended here; callers intern every value, see {CHECKER}:2."


def _gate_capture(harness: Harness, pr: int, head: str) -> dict[str, Any]:
    result = harness.gate(pr, head)
    return {
        "gate": step(result),
        "gate_statuses": harness.github.gate_statuses(head),
        "oracle_statuses": harness.github.oracle_statuses(head),
    }


def _comment(harness: Harness, pr: int, body: str, token: str) -> None:
    harness.gh(
        "comment",
        ["api", "--method", "POST", f"repos/{SLUG}/issues/{pr}/comments", "-f", f"body={body}"],
        harness.base_env(),
        token,
    )


@pytest.mark.parametrize("size", SIZE_BUCKETS)
def test_parity_classification_selects_the_same_plan_for_each_size(harness: Harness, size: str) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(40, "feat/checker")
    harness.script_omp([SHIP])
    template = "deep" if size in ("large", "huge", "max") else "standard"
    run = harness.review(40, classification=(size, template))
    record(f"classification-{size}", run_capture(harness, run, 40, head), harness)


def test_parity_a_documentation_change_is_classified_without_soundness_scope(harness: Harness) -> None:
    head = harness.branch("feat/docs", {"docs/notes.md": "# Notes\n\nA plain note.\n"}, "add a note")
    harness.github.open_pull(41, "feat/docs")
    harness.script_omp([SHIP])
    run = harness.review(41, classification=("trivial", "standard"))
    record("classification-docs-only", run_capture(harness, run, 41, head), harness)


def test_parity_gate_waits_without_a_verdict(harness: Harness) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(50, "feat/checker")
    captured = _gate_capture(harness, 50, head)
    assert captured["gate"]["code"] == 3
    record("gate-no-verdict", captured, harness)


def test_parity_gate_passes_after_a_ship_verdict(harness: Harness) -> None:
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(51, "feat/checker")
    harness.script_omp([SHIP])
    harness.review(51)
    captured = _gate_capture(harness, 51, head)
    assert captured["gate"]["code"] == 0
    record("gate-after-ship", captured, harness)


def test_parity_gate_holds_after_a_blocking_verdict(harness: Harness) -> None:
    head = harness.branch("feat/checker", {CHECKER: IDENTITY_CHECKER}, "compare by identity")
    harness.github.open_pull(52, "feat/checker")
    harness.script_omp([BLOCKING])
    harness.review(52)
    captured = _gate_capture(harness, 52, head)
    assert captured["gate"]["code"] != 0
    record("gate-after-blocking-verdict", captured, harness)


def test_parity_gate_accepts_an_attested_standin_and_refuses_a_stranger(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_github.ACCOUNTS, STRANGER_TOKEN, {"login": "stranger", "type": "User"})
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.github.open_pull(53, "feat/checker")
    _comment(harness, 53, f"Stand-in oracle review: APPROVE {head}", STRANGER_TOKEN)
    refused = _gate_capture(harness, 53, head)
    assert refused["gate"]["code"] == 3
    _comment(harness, 53, f"Stand-in oracle review: APPROVE {head}", OWNER_TOKEN)
    accepted = _gate_capture(harness, 53, head)
    assert accepted["gate"]["code"] == 0
    record("gate-standin", {"stranger": refused, "owner": accepted}, harness)


def test_parity_gate_ignores_a_pull_request_for_another_base(harness: Harness) -> None:
    harness.branch("release", {"README.md": "release\n"}, "release base")
    head = harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands", base="release")
    harness.github.open_pull(54, "feat/checker", base_ref="release")
    captured = _gate_capture(harness, 54, head)
    record("gate-other-base", captured, harness)


@pytest.mark.parametrize(
    "paths",
    [
        [CHECKER],
        ["README.md"],
        ["benchbox/core/equivalence/deep/nested.py", "docs/a.md"],
        [".github/workflows/ci.yml"],
        ["results-data/bundles/x/y.override.json"],
        ["benchbox/core/validation.py", "benchbox/core/sub/validation.py"],
        ["scripts/pr_arm.py"],
        ["unrelated/file.txt", ""],
    ],
    ids=lambda paths: "-".join(path.replace("/", "_") for path in paths) or "empty",
)
def test_parity_matcher_results(harness: Harness, paths: list[str]) -> None:
    listing = harness.root / "paths.txt"
    listing.write_text("\n".join(paths) + "\n", encoding="utf-8")
    manifest = harness.trusted / ".github" / "soundness-paths.txt"
    results: dict[str, Any] = {}
    for form in ("plain", "shell", "github-output"):
        result = harness.cli(
            "matcher",
            ["matcher", "check", "--manifest", str(manifest), "--paths-file", str(listing), "--format", form],
            harness.base_env(),
        )
        results[form] = step(result)
        assert result.code == 0, result.stderr
    key = "-".join(path.replace("/", "_") for path in paths) or "empty"
    record(f"matcher-{key}", results, harness)


def test_parity_gateway_clients_keep_the_client_identity(harness: Harness) -> None:
    primary = harness.root / "client.token"
    named = harness.root / "local-client.token"
    primary.write_text("p" * 48, encoding="utf-8")
    named.write_text("n" * 48, encoding="utf-8")
    accounts = harness.root / "accounts.json"
    accounts.write_text(
        json.dumps(
            {
                "accounts": [
                    {"provider": "anthropic", "identityKey": "email:a@example.com"},
                    {"provider": "openai-codex", "identityKey": "email:o@example.com"},
                    {"provider": "muse-code", "identityKey": "account:m@example.com"},
                    {"provider": "google-antigravity", "identityKey": "email:g@example.com"},
                ]
            }
        ),
        encoding="utf-8",
    )
    out = harness.root / "clients.json"
    args = [
        "gateway-clients",
        "--token-file",
        str(primary),
        "--client",
        f"oracle-local={named}",
        "--accounts",
        str(accounts),
        "--out",
        str(out),
        "--omp-config",
        str(ALPHA_ROOT / ".github" / "oracle-omp-config.yml"),
    ]
    result = harness.cli("clients", args, harness.base_env())
    assert result.code == 0, result.stderr
    record(
        "gateway-clients",
        {"result": step(result), "clients": json.loads(out.read_text(encoding="utf-8")), "mode": out.stat().st_mode},
        harness,
    )


def test_parity_local_review_persists_findings_disputes_and_captures(harness: Harness) -> None:
    harness.branch("feat/checker", {CHECKER: IDENTITY_CHECKER}, "compare by identity")
    harness.script_omp([BLOCKING])
    first = harness.local_review("feat/checker")
    assert first.code == 1, first.stdout + first.stderr
    maintained = {
        "status": "not_fixed",
        "evidence": f"{CHECKER}:2 still returns a is b, so interning does not make identity equality.",
        "claim": REBUTTAL,
        "concedes": False,
    }
    harness.script_omp([{**SHIP, "decision": "SHIP_WITH_FIXES", "defects": [], "priors": {"D1": maintained}}])
    second = harness.local_review("feat/checker", rebuttals={"c1-D1": REBUTTAL})
    withdrawn = {**maintained, "status": "withdrawn", "concedes": True}
    withdrawn["evidence"] = f"{CHECKER}:2 compares interned values, so identity equals equality here."
    harness.script_omp([{**SHIP, "priors": {"D1": withdrawn}}])
    third = harness.local_review("feat/checker", rebuttals={"c1-D1": REBUTTAL})
    record(
        "local-review-state",
        {
            "runs": [step(first), step(second), step(third)],
            "state": tree(harness.local_state),
            "record_out": harness.record_out.read_text(encoding="utf-8") if harness.record_out.is_file() else None,
            "policy": POLICY,
        },
        harness,
    )


def test_parity_local_review_withholds_secrets_from_its_capture(harness: Harness) -> None:
    harness.branch("feat/checker", {CHECKER: FIXED_CHECKER}, "guard None operands")
    harness.script_omp([SHIP], "OMP_GATEWAY_TOKEN")
    review = harness.local_review("feat/checker")
    assert review.code == 0, review.stdout + review.stderr
    record(
        "local-review-withheld-secrets",
        {"run": step(review), "captures": harness.local_captures()},
        harness,
    )
