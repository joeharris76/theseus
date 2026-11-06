from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import absence, capacity, driver, promotion, providers, report, retry
from theseus_reviewer.policy import Policy
from theseus_reviewer.selection import ABSENT, PENDING, REVIEW, Attempt, SelectionInput, next_step

from .test_driver import _environment, _plan
from .test_selection import _input, _reviewer

pytestmark = [pytest.mark.unit, pytest.mark.fast]

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CONFIG = {
    "modelRoles": {
        "default": "anthropic/claude-sonnet-5-5:high",
        "review": "anthropic/claude-opus-5-5:medium",
        "smol": "openai-codex/gpt-6-luna:xhigh",
        "other": "unlisted/some-model",
    },
    "retry": {
        "fallbackChains": {
            "anthropic/claude-sonnet-5-5": [
                "openai-codex/gpt-6.1-sol:medium",
                "google-antigravity/gemini-3.8-flash:high",
            ],
            "anthropic/claude-opus-5-5": ["openai-codex/gpt-6.1-sol:high"],
            "openai-codex/gpt-6-luna": ["anthropic/claude-sonnet-5-5:medium"],
        }
    },
}
CATALOG = {
    "anthropic/claude-sonnet-5-5": 872_000,
    "anthropic/claude-opus-5-5": 872_000,
    "openai-codex/gpt-6.1-sol": 372_000,
    "openai-codex/gpt-6-luna": 144_000,
}
PR3942_REQUIRED_BYTES = int(1_050_000 * capacity.BYTES_PER_TOKEN)


def _synthetic_plan(
    policy: Policy, load: int, capacities: dict[str, int], brief_mode: str = "file-list"
) -> dict[str, Any]:
    chain = [policy.reviewers[policy.buckets[bucket]].to_json() for bucket in ("medium", "large", "huge")]
    return {
        "chain": chain,
        "brief_mode": brief_mode,
        "max_attempts": 5,
        capacity.LOAD_KEY: load,
        capacity.CAPACITY_KEY: capacities,
    }


def test_review_load_adds_the_overhead_and_both_token_estimates() -> None:
    assert capacity.tokens(0) == 0
    assert capacity.tokens(227) == 100
    for mode, overhead in capacity.OVERHEAD_TOKENS.items():
        assert capacity.review_load(227_000, 22_700, mode) == overhead + 100_000 + 10_000
    assert set(capacity.OVERHEAD_TOKENS) == {"inline", "file-list", "promotion"}
    assert capacity.OVERHEAD_TOKENS["inline"] < capacity.OVERHEAD_TOKENS["file-list"]


def test_required_bytes_counts_hunk_blocks_of_required_files_only() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n keep\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    hunk = "@@ -1,2 +1,2 @@\n-old\n+new\n keep\n"
    assert capacity.required_bytes(["a.py"], diff) == len(hunk)
    assert capacity.required_bytes(["a.py", "missing.py"], diff) == len(hunk)
    assert capacity.required_bytes([], diff) == 0
    assert capacity.required_bytes(["a.py"], None) == 0


def test_required_bytes_counts_whole_removed_promotion_files_and_staged_upstream_hunks(tmp_path: Path) -> None:
    root = tmp_path / promotion.PROMOTION_PLAN_DIR
    (root / "old").mkdir(parents=True)
    (root / "old" / "gone.py").write_bytes(b"x" * 500)
    upstream = "diff --git a/kept.py b/kept.py\n--- a/kept.py\n+++ b/kept.py\n@@ -1 +1 @@\n-x\n+y\n"
    (root / promotion.UPSTREAM_DIFF_NAME).write_text(upstream, encoding="utf-8")
    staged = promotion.STAGED_PROMOTION_DIR
    total = capacity.required_bytes([f"{staged}/old/gone.py", f"{staged}/new/kept.py"], "", tmp_path)
    assert total == 500 + len("@@ -1 +1 @@\n-x\n+y\n")


def test_required_bytes_frames_staged_upstream_hunks_on_newlines_only(tmp_path: Path) -> None:
    root = tmp_path / promotion.PROMOTION_PLAN_DIR
    root.mkdir(parents=True)
    hunk = "@@ -1 +1 @@\n-a\rb\n+c\n"
    upstream = f"diff --git a/kept.py b/kept.py\n--- a/kept.py\n+++ b/kept.py\n{hunk}"
    (root / promotion.UPSTREAM_DIFF_NAME).write_bytes(upstream.encode("utf-8"))
    staged = promotion.STAGED_PROMOTION_DIR
    assert capacity.required_bytes([f"{staged}/new/kept.py"], "", tmp_path) == len(hunk.encode("utf-8"))


def test_capacity_uses_fallback_chain_minimum() -> None:
    assert capacity.reviewer_capacity("@review", CONFIG, CATALOG) == 372_000
    assert capacity.reviewer_capacity("@smol", CONFIG, CATALOG) == 144_000
    assert capacity.reviewer_capacity("anthropic/claude-opus-5-5", CONFIG, CATALOG) == 372_000
    assert capacity.reviewer_capacity("anthropic/claude-sonnet-5-5:high", CONFIG, CATALOG) == 372_000
    alone = {**CONFIG, "retry": {"fallbackChains": {}}}
    assert capacity.reviewer_capacity("@review", alone, CATALOG) == 872_000


def test_unknown_catalog_model_is_ignored() -> None:
    assert providers.candidates("@default", CONFIG)[-1].startswith("google-antigravity/")
    assert capacity.reviewer_capacity("@default", CONFIG, CATALOG) == 372_000
    assert capacity.reviewer_capacity("@other", CONFIG, CATALOG) is None
    assert capacity.reviewer_capacity("@missing-role", CONFIG, CATALOG) is None
    chain = [{"name": "a", "model": "@default"}, {"name": "b", "model": "@other"}]
    assert capacity.reviewer_capacities(chain, CONFIG, CATALOG) == {"a": 372_000}
    assert capacity.unlisted_models(CONFIG, CATALOG) == [
        "unlisted/some-model",
        "google-antigravity/gemini-3.8-flash",
    ]


def test_usage_limit_fallbacks_do_not_lower_a_reviewers_capacity() -> None:
    limited = {**CONFIG, "retry": {**CONFIG["retry"], "fallbackOn": "usage-limit"}}
    assert capacity.reviewer_capacity("@review", limited, CATALOG) == 872_000
    assert capacity.reviewer_capacity("@smol", limited, CATALOG) == 144_000
    always = {**CONFIG, "retry": {**CONFIG["retry"], "fallbackOn": "any-error"}}
    assert capacity.reviewer_capacity("@review", always, CATALOG) == 372_000
    assert capacity.reviewer_capacity("@review", CONFIG, CATALOG) == 372_000
    assert capacity.reviewer_capacity("@other", limited, CATALOG) is None


def test_catalog_capacity_is_window_minus_output_limit() -> None:
    text = json.dumps(
        {
            "models": [
                {"selector": "anthropic/claude-opus-5-5", "contextWindow": 1_000_000, "maxTokens": 128_000},
                {"selector": "broken/no-window", "maxTokens": 1},
            ]
        }
    )
    assert capacity.parse_catalog(text) == {"anthropic/claude-opus-5-5": 872_000}
    for bad in ("not json", "{}", json.dumps({"models": []})):
        with pytest.raises(capacity.CatalogError):
            capacity.parse_catalog(bad)


def test_the_catalog_is_listed_with_the_reviewer_home_and_overlay(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, env=kwargs["env"])
        seen["config"] = (Path(kwargs["env"]["HOME"]) / ".omp" / "agent" / "config.yml").read_text(encoding="utf-8")
        overlay = argv[argv.index("--config") + 1]
        seen["overlay"] = Path(overlay).read_text(encoding="utf-8")
        body = {"models": [{"selector": "a/b", "contextWindow": 10, "maxTokens": 4}]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    assert capacity.read_catalog(Path("omp"), "modelRoles: {}\n", fake_run) == {"a/b": 6}
    assert seen["argv"][:4] == ["omp", "models", "--json", "--no-extensions"]
    assert seen["config"] == "modelRoles: {}\n"
    assert seen["overlay"]
    assert seen["env"]["HOME"] != str(Path.home())

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    with pytest.raises(capacity.CatalogError):
        capacity.read_catalog(Path("omp"), "", failing)

    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("omp")

    with pytest.raises(capacity.CatalogError):
        capacity.read_catalog(Path("omp"), "", missing)


def test_select_skip_text() -> None:
    chain = [_reviewer("small"), _reviewer("large")]
    step = next_step(
        _input(chain, load_tokens=300_000, capacities={"small": 144_000, "large": 872_000}),
        [],
    )
    assert step.kind == REVIEW and step.reviewer is not None and step.reviewer.name == "large"
    only = next_step(_input(chain[:1], load_tokens=300_000, capacities={"small": 144_000}), [])
    assert only.kind == PENDING
    assert only.reasons[0] == (
        "small: skipped: the change needs about 300000 tokens of reading; this reviewer's capacity is 144000"
    )
    unknown = next_step(_input(chain[:1], load_tokens=300_000), [])
    assert unknown.kind == PENDING and not unknown.oversize
    assert unknown.reasons == (
        "small: skipped: this reviewer's capacity cannot be determined: its primary model is not in the model catalog",
    )
    explained = next_step(
        SelectionInput(**{**_input(chain[:1], load_tokens=300_000).__dict__, "capacity_error": "the catalog is empty"}),
        [],
    )
    assert explained.reasons == ("small: skipped: this reviewer's capacity cannot be determined: the catalog is empty",)
    assert next_step(_input(chain[:1], load_tokens=150_000, capacities={"small": 200_000}), []).kind == REVIEW
    assert next_step(_input(chain[:1], load_tokens=0, capacities={"small": 1}), []).kind == REVIEW


def test_pr3942_sized_plan_is_pending_split_before_any_attempt(policy: Policy) -> None:
    load = capacity.review_load(PR3942_REQUIRED_BYTES, 60_000, "file-list")
    assert load > 1_150_000
    plan = _synthetic_plan(policy, load, {"medium": 372_000, "large": 372_000, "huge": 872_000})
    step = next_step(SelectionInput.from_plan(plan, NOW), [])
    assert step.kind == PENDING and step.oversize and step.reviewer is None
    assert step.reasons[-1] == capacity.SPLIT_INSTRUCTION
    assert all(reason.split(": ", 1)[1].startswith(capacity.SKIP_PREFIX) for reason in step.reasons[:-1])
    assert report._settled(plan, step)[1] == retry.OVERSIZE


def test_a_plan_that_fits_one_reviewer_selects_it_and_is_not_oversize(policy: Policy) -> None:
    plan = _synthetic_plan(policy, 400_000, {"medium": 372_000, "large": 372_000, "huge": 872_000})
    step = next_step(SelectionInput.from_plan(plan, NOW), [])
    assert step.kind == REVIEW and step.reviewer is not None and step.reviewer.name == policy.buckets["huge"]


def test_a_quota_absence_alongside_a_capacity_skip_is_not_reported_as_oversize() -> None:
    chain = [_reviewer("small"), _reviewer("large", pool="busy")]
    attempts = [Attempt(1, "large", ABSENT, "quota", "limit", provider="p")]
    held = _input(chain, load_tokens=300_000, capacities={"small": 144_000, "large": 872_000})
    step = next_step(held, attempts)
    assert step.kind == PENDING and not step.oversize
    exhausted = next_step(_input(chain, load_tokens=900_000, capacities={"small": 1, "large": 1}), [])
    assert exhausted.oversize


def test_context_overflow_absence_stops_same_reviewer() -> None:
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "error",
            "errorMessage": "prompt is too long: 1100000 tokens > 1000000 maximum",
            "provider": "anthropic",
        },
    }
    found = absence.classify("omp", 1, json.dumps(event) + "\n", "", timed_out=False, now=NOW)
    assert found.kind == absence.CONTEXT_OVERFLOW and "prompt is too long" in found.detail
    assert absence.CONTEXT_OVERFLOW in absence.KINDS
    status = {**event, "message": {**event["message"], "errorStatus": 413, "errorMessage": "too big"}}
    assert absence.classify("omp", 1, json.dumps(status), "", timed_out=False, now=NOW).kind == absence.CONTEXT_OVERFLOW
    assert absence.classify("omp", 0, "{}", "", timed_out=False, now=NOW).kind == absence.OK

    history = [Attempt(1, "r1", ABSENT, absence.CONTEXT_OVERFLOW, "prompt is too long")]
    assert next_step(_input([_reviewer("r1")]), history).kind == PENDING
    chosen = next_step(_input([_reviewer("r1"), _reviewer("r2")]), history)
    assert chosen.kind == REVIEW and chosen.reviewer is not None and chosen.reviewer.name == "r2"
    assert any("context-overflow" in reason for reason in next_step(_input([_reviewer("r1")]), history).reasons)


def test_the_harness_records_reviewer_capacity_in_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    environment = _environment(tmp_path)
    config = tmp_path / "omp.yml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    chain = [_reviewer("large", model="@review").to_json(), _reviewer("odd", model="@other").to_json()]
    _plan(environment.layout, chain=chain, omp_config=str(config))
    calls: list[tuple[Path, str]] = []

    def reader(binary: Path, text: str) -> dict[str, int]:
        calls.append((binary, text))
        return dict(CATALOG)

    driver._record_capacity(environment, tmp_path / "omp", reader)

    stored = json.loads(environment.layout.plan_file.read_text(encoding="utf-8"))
    assert stored[capacity.CAPACITY_KEY] == {"large": 372_000}
    assert stored["decision"] == "review" and len(calls) == 1


def test_an_unreadable_catalog_records_the_reason_and_no_reviewer_is_selected_for_a_sized_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _environment(tmp_path)
    config = tmp_path / "omp.yml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    _plan(environment.layout, chain=[_reviewer("large", model="@review").to_json()], omp_config=str(config))

    def broken(binary: Path, text: str) -> dict[str, int]:
        raise capacity.CatalogError("the model catalog could not be listed: exit code 1")

    driver._record_capacity(environment, tmp_path / "omp", broken)

    stored = json.loads(environment.layout.plan_file.read_text(encoding="utf-8"))
    assert stored[capacity.ERROR_KEY] == "the model catalog could not be listed: exit code 1"
    assert capacity.CAPACITY_KEY not in stored and stored["decision"] == "review"
    assert "reviewer capacity is unknown" in capsys.readouterr().out
    sized = {**stored, "brief_mode": "file-list", "max_attempts": 5, capacity.LOAD_KEY: 190_000}
    step = next_step(SelectionInput.from_plan(sized, NOW), [])
    assert step.kind == PENDING and not step.oversize and step.reviewer is None
    assert step.reasons == (
        "large: skipped: this reviewer's capacity cannot be determined: "
        "the model catalog could not be listed: exit code 1",
    )
    unsized = {**stored, "brief_mode": "file-list", "max_attempts": 5, capacity.LOAD_KEY: 0}
    assert next_step(SelectionInput.from_plan(unsized, NOW), []).kind == REVIEW


def test_a_reviewer_whose_primary_model_is_not_in_the_catalog_has_unknown_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    environment = _environment(tmp_path)
    config = tmp_path / "omp.yml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    chain = [_reviewer("large", model="@review").to_json(), _reviewer("odd", model="@other").to_json()]
    _plan(environment.layout, chain=chain, omp_config=str(config), brief_mode="file-list", max_attempts=5)
    driver._record_capacity(environment, tmp_path / "omp", lambda binary, text: dict(CATALOG))
    stored = json.loads(environment.layout.plan_file.read_text(encoding="utf-8"))

    only_odd = {**stored, "chain": chain[1:], capacity.LOAD_KEY: 100_000, capacity.CAPACITY_KEY: {}}
    step = next_step(SelectionInput.from_plan(only_odd, NOW), [])

    assert stored[capacity.CAPACITY_KEY] == {"large": 372_000}
    assert step.kind == PENDING and not step.oversize
    assert step.reasons == (f"odd: {capacity.unknown_text('')}",)


GATED_OMP = """#!{python}
import json, os, re, sys
from pathlib import Path

MODELS = {{
    "anthropic": {{"claude-sonnet-5-5": 1000000, "claude-opus-5-5": 1000000}},
    "openai-codex": {{"gpt-6.1-sol": 400000, "gpt-6-luna": 272000}},
    "muse-code": {{"muse-spark-1.3-contributor": 262144}},
    "google-antigravity": {{"gemini-3.8-flash": 262144}},
    "polluted": {{"model": 1000}},
}}
agent = Path(os.environ["HOME"]) / ".omp" / "agent"
sheet = agent / "models.yml"
providers = re.findall(r"^  (\\S+):$", sheet.read_text(), re.M) if sheet.is_file() else []
token = os.environ.get("OMP_GATEWAY_TOKEN", "")
listed = [{{"selector": "apple/on-device", "contextWindow": 8192, "maxTokens": 8192}}]
for provider in providers:
    if not token:
        continue
    if provider == "muse-code":
        try:
            held = json.loads(token)
            ok = held["oauthAccessToken"].strip() and held["apiKey"].strip()
        except Exception:
            ok = False
        if not ok:
            sys.exit("Muse Code credential is invalid; sign in again")
    for name, window in MODELS.get(provider, {{}}).items():
        listed.append({{"selector": provider + "/" + name, "contextWindow": window, "maxTokens": 128000}})
print(json.dumps({{"models": listed}}))
"""


@pytest.fixture
def gated_omp(tmp_path: Path) -> Path:
    path = tmp_path / "gated-omp"
    path.write_text(GATED_OMP.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _polluted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user = tmp_path / "user-home"
    agent = user / ".omp" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.yml").write_text("providers:\n  polluted:\n    apiKey: X\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.delenv("OMP_GATEWAY_TOKEN", raising=False)
    return user


def test_the_catalog_lists_every_gateway_provider_without_a_real_credential(
    gated_omp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _polluted_home(tmp_path, monkeypatch)
    monkeypatch.delenv("OMP_GATEWAY_TOKEN", raising=False)

    listed = capacity.read_catalog(gated_omp, json.dumps(CONFIG))

    assert listed["anthropic/claude-opus-5-5"] == 872_000
    assert listed["openai-codex/gpt-6-luna"] == 144_000
    assert "google-antigravity/gemini-3.8-flash" in listed
    assert not any(selector.startswith("polluted/") for selector in listed)


def test_the_catalog_routes_a_provider_only_the_config_names(
    gated_omp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _polluted_home(tmp_path, monkeypatch)
    extended = {**CONFIG, "modelRoles": {**CONFIG["modelRoles"], "extra": "muse-code/muse-spark-1.3-contributor"}}
    monkeypatch.setenv("OMP_GATEWAY_TOKEN", "unused")
    listed = capacity.read_catalog(gated_omp, json.dumps(extended))
    assert listed["muse-code/muse-spark-1.3-contributor"] == 262_144 - 128_000


def test_a_real_gateway_token_in_the_environment_never_reaches_the_catalog_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMP_GATEWAY_TOKEN", "real-gateway-token-value-0123456789")
    seen: dict[str, str] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])
        body = {"models": [{"selector": "a/b", "contextWindow": 10, "maxTokens": 4}]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    capacity.read_catalog(Path("omp"), "", fake_run)

    assert seen["OMP_GATEWAY_TOKEN"] == capacity.CATALOG_TOKEN


def test_local_and_hosted_capacity_agree_for_the_same_plan(
    gated_omp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from theseus_reviewer import local, preflight

    _polluted_home(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    config = tmp_path / "omp.yml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    chain = [_reviewer("large", model="@review").to_json(), _reviewer("small", model="@default").to_json()]
    environment = _environment(tmp_path)
    _plan(environment.layout, chain=chain, omp_config=str(config))

    driver._record_capacity(environment, gated_omp)
    hosted = json.loads(environment.layout.plan_file.read_text(encoding="utf-8"))[capacity.CAPACITY_KEY]

    monkeypatch.setenv("REVIEW_OMP_BINARY", str(gated_omp))
    plan = {"chain": chain, "omp_config": "omp.yml"}
    trusted = local.Trusted(policy="", files=(("omp.yml", config.read_text(encoding="utf-8")),))
    local_held = preflight._catalog(trusted, capacity.read_catalog, plan)

    assert hosted == local_held
    assert hosted == {"large": 272_000, "small": 134_144}


def test_an_unreadable_catalog_gives_the_same_assumption_locally_and_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from theseus_reviewer import local, preflight

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    broken = tmp_path / "broken-omp"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    config = tmp_path / "omp.yml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    chain = [_reviewer("large", model="@review").to_json()]
    environment = _environment(tmp_path)
    _plan(environment.layout, chain=chain, omp_config=str(config))

    driver._record_capacity(environment, broken)
    hosted = capsys.readouterr().out

    monkeypatch.setenv("REVIEW_OMP_BINARY", str(broken))
    trusted = local.Trusted(policy="", files=(("omp.yml", config.read_text(encoding="utf-8")),))
    with pytest.raises(preflight.Undetermined) as raised:
        preflight._catalog(trusted, capacity.read_catalog, {"chain": chain, "omp_config": "omp.yml"})

    reason = "the model catalog could not be listed: exit code 3"
    assert f"reviewer capacity is unknown ({reason});" in hosted
    assert json.loads(environment.layout.plan_file.read_text(encoding="utf-8"))[capacity.ERROR_KEY] == reason
    assert str(raised.value) == f"{preflight.CATALOG_REASON}: {reason}"
