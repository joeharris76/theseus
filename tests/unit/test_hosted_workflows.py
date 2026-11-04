from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
EXAMPLES = ROOT / "examples" / "consumer" / ".github" / "workflows"
CALLEES = ("review.yml", "gate.yml")
SECRET_JOBS = {"review.yml": ("review", "sweep"), "gate.yml": ("gate", "sweep")}
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SECRET = re.compile(r"secrets\.(\w+)")
PLACEHOLDER = "THESEUS_COMMIT_SHA"
PERMISSION_RANK = {"none": 0, "read": 1, "write": 2}


def load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def steps_of(document: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return document["jobs"][job]["steps"]


def step_named(document: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    return next(step for step in steps_of(document, job) if step["name"] == name)


def index_of(document: dict[str, Any], job: str, name: str) -> int:
    return [step["name"] for step in steps_of(document, job)].index(name)


def all_steps(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, step) for job, body in document["jobs"].items() for step in body.get("steps", [])]


@pytest.fixture(params=CALLEES)
def callee(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    return request.param, load(WORKFLOWS / request.param)


def test_every_action_in_every_workflow_is_pinned_to_a_full_commit_sha() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for _, step in all_steps(load(path)):
            if "uses" in step:
                _, _, ref = step["uses"].partition("@")
                assert FULL_SHA.fullmatch(ref), f"{path.name}: {step['uses']}"


def test_the_callees_are_workflow_call_only_with_empty_top_level_permissions_and_no_shared_concurrency(
    callee: tuple[str, dict[str, Any]],
) -> None:
    _, document = callee
    assert set(document["on"]) == {"workflow_call"}
    assert document["permissions"] == {}
    assert "concurrency" not in document
    assert "run-name" not in document
    assert all("concurrency" not in body for body in document["jobs"].values())


def test_no_callee_asks_for_an_oidc_token_inherits_secrets_or_runs_on_a_candidate_trigger(
    callee: tuple[str, dict[str, Any]],
) -> None:
    name, document = callee
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "id-token" not in text
    assert all(line.strip() != "secrets: inherit" for line in text.splitlines())
    assert all("secrets" not in body for body in document["jobs"].values())
    assert "pull_request_target" not in yaml.safe_dump(document["on"])
    for body in document["jobs"].values():
        assert set(body.get("permissions", {})) <= {"actions", "contents", "pull-requests"}


def test_the_guard_job_has_no_permissions_environment_or_secrets(callee: tuple[str, dict[str, Any]]) -> None:
    name, document = callee
    guard = document["jobs"]["guard"]
    assert guard["permissions"] == {"contents": "read"}
    assert "environment" not in guard
    assert "secrets." not in yaml.safe_dump(guard)
    assert all(body.get("needs") == "guard" for job, body in document["jobs"].items() if job != "guard")
    command = step_named(document, "guard", guard["steps"][0]["name"])["run"]
    assert "[0-9a-f]{40}" in command
    assert "refs/heads/$GATED_BASE" in command
    assert "$REPOSITORY/.github/workflows/$CALLER_WORKFLOW@" in command
    assert "github.workflow_ref" in guard["steps"][0]["env"]["WORKFLOW_REF"]


LOCK_STEP = "Refuse a pin that differs from the trusted consumer lock"
LOCK_CHECKOUT = "Check out the trusted copy of the consumer lock"
LOCK_FILE = ".github/theseus-lock.json"
PIN_A = "a" * 40
PIN_B = "b" * 40
TRUSTED_LOCK_REF = {"review.yml": "${{ github.sha }}", "gate.yml": "${{ inputs.gated_base }}"}


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit_lock(root: Path, lock: dict[str, str] | None) -> str:
    target = root / LOCK_FILE
    if lock is None:
        (root / "marker").write_text("no lock", encoding="utf-8")
    else:
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(lock), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "state")
    return _git(root, "rev-parse", "HEAD")


def _pin_lock(workflow: str, source: str | None = None) -> dict[str, str]:
    return {"workflow_sha": workflow, "source_commit": source or workflow}


def _lock_check(name: str, root: Path, pin: str) -> subprocess.CompletedProcess[str]:
    document = load(WORKFLOWS / name)
    run = step_named(document, "guard", LOCK_STEP)["run"]
    return subprocess.run(
        ["bash", "-c", run],
        cwd=root.parent,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "THESEUS_REF": pin,
            "HOME": str(root.parent),
            "GITHUB_OUTPUT": str(root.parent / "github-output"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def trusted(tmp_path: Path) -> Path:
    root = tmp_path / "trusted"
    root.mkdir()
    _git(root, "init", "-q")
    return root


@pytest.mark.parametrize("name", CALLEES)
def test_the_guard_runs_when_the_pin_equals_the_trusted_lock_workflow_sha_and_source_commit(
    name: str, trusted: Path
) -> None:
    _commit_lock(trusted, _pin_lock(PIN_A))
    done = _lock_check(name, trusted, PIN_A)
    assert done.returncode == 0, done.stderr
    written = (trusted.parent / "github-output").read_text(encoding="utf-8")
    assert written == f"trusted_commit={_git(trusted, 'rev-parse', 'HEAD')}\n"


@pytest.mark.parametrize("name", CALLEES)
def test_the_guard_refuses_a_pin_that_differs_from_the_lock_workflow_sha(name: str, trusted: Path) -> None:
    _commit_lock(trusted, _pin_lock(PIN_A))
    done = _lock_check(name, trusted, PIN_B)
    assert done.returncode == 2
    assert f"theseus_ref {PIN_B} is not the trusted lock workflow_sha {PIN_A}" in done.stderr


@pytest.mark.parametrize("name", CALLEES)
def test_the_guard_refuses_a_lock_whose_source_commit_differs_from_the_pin(name: str, trusted: Path) -> None:
    _commit_lock(trusted, _pin_lock(PIN_A, PIN_B))
    done = _lock_check(name, trusted, PIN_A)
    assert done.returncode == 2
    assert f"source_commit {PIN_B}" in done.stderr


@pytest.mark.parametrize("name", CALLEES)
def test_the_guard_refuses_when_the_trusted_commit_has_no_lock(name: str, trusted: Path) -> None:
    _commit_lock(trusted, None)
    done = _lock_check(name, trusted, PIN_A)
    assert done.returncode == 2
    assert "no .github/theseus-lock.json at trusted commit" in done.stderr


@pytest.mark.parametrize("name", CALLEES)
@pytest.mark.parametrize("raw", ["not json", "[]", '{"workflow_sha": "' + PIN_A + '"}'])
def test_the_guard_refuses_an_unreadable_or_incomplete_lock(name: str, raw: str, trusted: Path) -> None:
    (trusted / ".github").mkdir()
    (trusted / LOCK_FILE).write_text(raw, encoding="utf-8")
    _git(trusted, "add", "-A")
    _git(trusted, "commit", "-q", "-m", "state")
    done = _lock_check(name, trusted, PIN_A)
    assert done.returncode == 2
    assert "trusted consumer lock is unreadable" in done.stderr


@pytest.mark.parametrize("name", CALLEES)
def test_a_pull_request_head_lock_with_a_matching_pin_does_not_satisfy_the_guard(name: str, trusted: Path) -> None:
    base = _commit_lock(trusted, _pin_lock(PIN_A))
    _commit_lock(trusted, _pin_lock(PIN_B))
    _git(trusted, "checkout", "-q", base)
    assert json.loads((trusted / LOCK_FILE).read_text(encoding="utf-8"))["workflow_sha"] == PIN_A
    done = _lock_check(name, trusted, PIN_B)
    assert done.returncode == 2
    assert f"workflow_sha {PIN_A}" in done.stderr


def test_the_guard_reads_the_lock_from_the_trusted_commit_object_not_the_working_tree(trusted: Path) -> None:
    _commit_lock(trusted, _pin_lock(PIN_A))
    (trusted / LOCK_FILE).write_text(json.dumps(_pin_lock(PIN_B)), encoding="utf-8")
    assert _lock_check("review.yml", trusted, PIN_B).returncode == 2
    assert _lock_check("review.yml", trusted, PIN_A).returncode == 0


@pytest.mark.parametrize("name", CALLEES)
def test_the_lock_comes_from_the_trusted_ref_and_precedes_nothing_secret_bearing(name: str) -> None:
    document = load(WORKFLOWS / name)
    checkout = step_named(document, "guard", LOCK_CHECKOUT)
    assert checkout["with"]["ref"] == TRUSTED_LOCK_REF[name]
    assert "pull_request" not in checkout["with"]["ref"] and "head" not in checkout["with"]["ref"]
    assert checkout["with"]["path"] == "trusted"
    assert checkout["with"]["persist-credentials"] is False
    names = [step["name"] for step in steps_of(document, "guard")]
    assert names.index(LOCK_CHECKOUT) < names.index(LOCK_STEP)
    assert names[0] == "Refuse a malformed pin, a foreign caller or a dispatch from another branch"
    assert step_named(document, "guard", LOCK_STEP)["env"] == {"THESEUS_REF": "${{ inputs.theseus_ref }}"}
    assert "inputs.theseus_ref" in yaml.safe_dump(step_named(document, "guard", LOCK_STEP))
    for job, body in document["jobs"].items():
        if job != "guard":
            assert body["needs"] == "guard"


@pytest.mark.parametrize("name", CALLEES)
def test_a_refused_guard_writes_no_trusted_commit_output(name: str, trusted: Path) -> None:
    _commit_lock(trusted, _pin_lock(PIN_A))
    assert _lock_check(name, trusted, PIN_B).returncode == 2
    assert not (trusted.parent / "github-output").exists()


def test_the_lock_never_travels_through_the_environment() -> None:
    for name in CALLEES:
        step = step_named(load(WORKFLOWS / name), "guard", LOCK_STEP)
        assert "LOCK_JSON" not in step["run"] and "export" not in step["run"]
        assert set(step["env"]) == {"THESEUS_REF"}


def test_both_guards_skip_pull_requests_for_other_bases() -> None:
    expected = "github.event_name != 'pull_request_target' || github.event.pull_request.base.ref == inputs.gated_base"
    for name in CALLEES:
        assert load(WORKFLOWS / name)["jobs"]["guard"]["if"] == expected


def test_the_guard_exports_the_resolved_trusted_commit_and_the_gate_jobs_check_out_exactly_it() -> None:
    gate = load(WORKFLOWS / "gate.yml")
    assert gate["jobs"]["guard"]["outputs"] == {"trusted_commit": "${{ steps.lock.outputs.trusted_commit }}"}
    assert step_named(gate, "guard", LOCK_STEP)["id"] == "lock"
    for job in ("gate", "sweep"):
        checkout = step_named(gate, job, "Check out the trusted gated-branch copy")
        assert checkout["with"]["ref"] == "${{ needs.guard.outputs.trusted_commit }}"
        assert "gated_base" not in checkout["with"]["ref"]
    review = load(WORKFLOWS / "review.yml")
    assert review["jobs"]["guard"]["outputs"] == {"trusted_commit": "${{ steps.lock.outputs.trusted_commit }}"}


def test_secrets_are_read_only_in_jobs_bound_to_the_caller_environment(callee: tuple[str, dict[str, Any]]) -> None:
    name, document = callee
    for job, body in document["jobs"].items():
        mentions = SECRET.findall(yaml.safe_dump(body))
        if job in SECRET_JOBS[name]:
            assert body["environment"] == "${{ inputs.environment }}"
            assert mentions
        else:
            assert mentions == [], job
    assert set(document["on"]["workflow_call"]) == {"inputs"}


def test_job_level_env_holds_only_derived_booleans_and_the_policy_path(callee: tuple[str, dict[str, Any]]) -> None:
    name, document = callee
    for job in SECRET_JOBS[name]:
        for key, value in document["jobs"][job]["env"].items():
            assert key in {"HAS_APP", "HAS_TAILNET", "REPO", "POLICY_PATH"}, key
            if "secrets." in value:
                assert value.endswith(" != '' }}") or " != '' && " in value, (key, value)


def test_every_checkout_drops_credentials_and_the_candidate_checkout_names_no_secret() -> None:
    for name in CALLEES:
        document = load(WORKFLOWS / name)
        for job, step in all_steps(document):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False, (name, job, step["name"])
    review = load(WORKFLOWS / "review.yml")
    head = step_named(review, "review", "Check out the pull request head for reading only")
    assert head["with"]["path"] == "workspace"
    assert "secrets." not in yaml.safe_dump(head)
    assert "env" not in head


def test_the_reviewer_pin_comes_from_the_trusted_base_commit_and_precedes_every_reviewer_step() -> None:
    review = load(WORKFLOWS / "review.yml")
    names = [step["name"] for step in steps_of(review, "review")]
    pin = step_named(review, "review", "Install the reviewer pin from the trusted base lock")
    trusted = step_named(review, "review", "Check out the trusted gated-branch copy")
    assert trusted["with"]["ref"] == "${{ github.sha }}"
    assert pin["env"]["TRUSTED_SHA"] == "${{ github.sha }}"
    assert '--repo trusted --commit "$TRUSTED_SHA"' in pin["run"]
    assert "latest" not in pin["run"] and "HEAD" not in pin["run"]
    assert names.index(pin["name"]) < names.index("Prepare the review")
    assert names.index("Check out the pull request head for reading only") > names.index(pin["name"])
    for _, step in all_steps(review):
        if "run" in step and step["name"] not in (
            pin["name"],
            "Install the gate pin from the current gated-branch lock",
        ):
            assert "bootstrap.py" not in step["run"] and "BOOTSTRAP" not in step["run"]
    bootstrap = step_named(review, "review", "Check out the pinned Theseus bootstrap")
    assert bootstrap["with"]["ref"] == "${{ inputs.theseus_ref }}"
    assert bootstrap["with"]["sparse-checkout"] == "src/theseus_reviewer/bootstrap.py"


def test_the_gate_pin_resolves_the_current_gated_tip_into_a_separate_install_and_process() -> None:
    review = load(WORKFLOWS / "review.yml")
    checkout = step_named(review, "review", "Check out the current gated-branch gate")
    pin = step_named(review, "review", "Install the gate pin from the current gated-branch lock")
    publish = step_named(review, "review", "Post the result")
    reviewer = step_named(review, "review", "Install the reviewer pin from the trusted base lock")
    assert checkout["with"]["ref"] == "${{ inputs.gated_base }}"
    assert checkout["with"]["path"] == "gate"
    assert "git -C gate rev-parse HEAD" in pin["run"]
    assert '--repo gate --commit "$GATE_SHA"' in pin["run"]
    assert "GITHUB_PATH" not in pin["run"]
    assert pin["env"]["INSTALL_CACHE"] != reviewer["env"]["INSTALL_CACHE"]
    assert pin["continue-on-error"] is True
    assert publish["env"]["THESEUS_GATE_COMMAND"] == "${{ steps.gate_pin.outputs.command }}"
    assert publish["env"]["THESEUS_GATE_ROOT"] == "${{ github.workspace }}/gate"
    assert set(publish["env"]) == {"GH_TOKEN", "JOB_TOKEN", "THESEUS_GATE_COMMAND", "THESEUS_GATE_ROOT"}
    names = [step["name"] for step in steps_of(review, "review")]
    assert names.index(checkout["name"]) < names.index(pin["name"]) < names.index(publish["name"])


def test_the_app_is_minted_only_after_the_reviewer_has_run_and_the_result_is_finalised() -> None:
    review = load(WORKFLOWS / "review.yml")
    names = [step["name"] for step in steps_of(review, "review")]
    mint = names.index("Mint the App token")
    for name in names:
        step = step_named(review, "review", name)
        if "ORACLE_APP" in yaml.safe_dump(step):
            assert name in {"Mint the App token", APP_REFUSAL}
    assert mint > max(i for i, name in enumerate(names) if "attempt loop" in name or "Run the attempt" in name)
    assert mint > names.index("Decide the verdict and redact every secret value from the results")
    assert mint < names.index("Post the result")
    assert "ORACLE_APP_PRIVATE_KEY" not in yaml.safe_dump(
        [step.get("env") for _, step in all_steps(review) if "env" in step]
    )


def test_gateway_and_provider_secrets_reach_only_the_phases_that_use_them() -> None:
    review = load(WORKFLOWS / "review.yml")
    gateway = {"ORACLE_GATEWAY_TOKEN", "ORACLE_GATEWAY_URL"}
    for step in steps_of(review, "review"):
        used = set(SECRET.findall(yaml.safe_dump(step.get("env", {}))))
        if used & gateway:
            assert step["name"].startswith(("Run the attempt loop", "Resume the attempt loop", "Decide the verdict"))
    for step in steps_of(review, "review"):
        if step["name"].startswith(("Check out", "Install", "Upload", "Record", "Fail", "Dispatch")):
            assert not SECRET.findall(yaml.safe_dump(step.get("env", {})))


def test_every_reviewer_phase_runs_theseus_with_the_trusted_policy_from_the_trusted_checkout() -> None:
    review = load(WORKFLOWS / "review.yml")
    for step in steps_of(review, "review"):
        if "drive" in step.get("run", ""):
            assert re.fullmatch(r'theseus drive --policy "\$POLICY_PATH" \w+', step["run"].strip()), step["name"]
            assert step["working-directory"] == "trusted"


def test_a_gate_failure_ends_the_run_after_the_state_and_dispatch_steps_have_run() -> None:
    review = load(WORKFLOWS / "review.yml")
    names = [step["name"] for step in steps_of(review, "review")]
    failing = step_named(review, "review", "Fail the run when the gate was not posted from it")
    assert names[-1] == failing["name"]
    assert failing["if"] == "always() && steps.publish.outputs.gate_failed == 'true'"
    assert names.index("Dispatch the gate for the reviewed head") < len(names) - 1


def test_the_bridge_matches_the_review_workflow_name_not_its_file_name() -> None:
    gate = load(WORKFLOWS / "gate.yml")
    inputs = gate["on"]["workflow_call"]["inputs"]
    condition = gate["jobs"]["bridge"]["if"]
    assert inputs["review_workflow_name"]["default"] == "oracle-review"
    assert "github.event.workflow_run.name == inputs.review_workflow_name" in condition
    assert "github.event.workflow_run.repository.full_name == github.repository" in condition
    assert "path" not in condition
    assert "secrets." not in yaml.safe_dump(gate["jobs"]["bridge"])
    assert "environment" not in gate["jobs"]["bridge"]
    assert not any("checkout" in str(step.get("uses", "")) for step in steps_of(gate, "bridge"))


def test_the_review_and_gate_jobs_skip_pull_requests_for_other_bases_and_other_refs() -> None:
    review = load(WORKFLOWS / "review.yml")
    gate = load(WORKFLOWS / "gate.yml")
    assert "github.ref == format('refs/heads/{0}', inputs.gated_base)" in review["jobs"]["review"]["if"]
    assert "github.ref == format('refs/heads/{0}', inputs.gated_base)" in gate["jobs"]["gate"]["if"]
    assert "github.event.pull_request.base.ref == inputs.gated_base" in review["jobs"]["guard"]["if"]


def callee_needs(callee_name: str) -> dict[str, str]:
    needed: dict[str, str] = {}
    for body in load(WORKFLOWS / callee_name)["jobs"].values():
        for key, level in body.get("permissions", {}).items():
            if PERMISSION_RANK[level] > PERMISSION_RANK[needed.get(key, "none")]:
                needed[key] = level
    return needed


@pytest.mark.parametrize(
    ("example", "callee_name", "name", "group"),
    [
        ("oracle-verdict.yml", "review.yml", "oracle-verdict", "oracle-verdict-"),
        ("oracle-gate.yml", "gate.yml", "oracle-gate", "oracle-gate-"),
    ],
)
def test_the_example_callers_keep_protocol_names_pin_the_callee_and_grant_the_permission_ceiling(
    example: str, callee_name: str, name: str, group: str
) -> None:
    caller = load(EXAMPLES / example)
    assert caller["name"] == name
    assert caller["permissions"] == {}
    assert caller["concurrency"]["group"].startswith(group)
    assert set(caller["on"]) >= {"schedule", "workflow_dispatch", "pull_request_target"}
    (job,) = caller["jobs"].values()
    assert "secrets" not in job and "steps" not in job
    uses, _, ref = job["uses"].partition("@")
    assert uses == f"joeharris76/theseus/.github/workflows/{callee_name}"
    assert ref == job["with"]["theseus_ref"] == PLACEHOLDER
    assert job["with"]["environment"] == "oracle" and job["with"]["gated_base"] == "main"
    declared = set(load(WORKFLOWS / callee_name)["on"]["workflow_call"]["inputs"])
    assert set(job["with"]) <= declared
    required = {
        key for key, spec in load(WORKFLOWS / callee_name)["on"]["workflow_call"]["inputs"].items() if spec["required"]
    }
    assert required <= set(job["with"])
    for key, level in callee_needs(callee_name).items():
        assert PERMISSION_RANK[job["permissions"].get(key, "none")] >= PERMISSION_RANK[level], key


def test_the_example_callers_use_distinct_noncolliding_concurrency_groups() -> None:
    groups = {
        load(EXAMPLES / name)["concurrency"]["group"].split("${{")[0].strip('"')
        for name in ("oracle-verdict.yml", "oracle-gate.yml")
    }
    assert groups == {"oracle-verdict-", "oracle-gate-"}
    assert "ci-" not in "".join(groups)


def test_the_example_bridge_trigger_names_the_workflow_that_the_review_event_caller_declares() -> None:
    gate = load(EXAMPLES / "oracle-gate.yml")
    review = load(EXAMPLES / "oracle-review.yml")
    assert gate["on"]["workflow_run"]["workflows"] == [review["name"]] == ["oracle-review"]
    assert gate["on"]["workflow_run"]["types"] == ["completed"]
    assert review["permissions"] == {}
    assert set(review["on"]) == {"pull_request_review"}
    assert not any("uses" in step or "secrets." in yaml.safe_dump(step) for _, step in all_steps(review))
    assert (
        load(WORKFLOWS / "gate.yml")["on"]["workflow_call"]["inputs"]["review_workflow_name"]["default"]
        == review["name"]
    )


def test_the_callee_caller_workflow_defaults_match_the_example_file_names() -> None:
    review_inputs = load(WORKFLOWS / "review.yml")["on"]["workflow_call"]["inputs"]
    gate_inputs = load(WORKFLOWS / "gate.yml")["on"]["workflow_call"]["inputs"]
    assert (
        review_inputs["caller_workflow"]["default"] == "oracle-verdict.yml"
        and (EXAMPLES / "oracle-verdict.yml").is_file()
    )
    assert gate_inputs["caller_workflow"]["default"] == "oracle-gate.yml" and (EXAMPLES / "oracle-gate.yml").is_file()


GUARD_STEP = "Refuse a malformed pin, a foreign caller or a dispatch from another branch"


def _guard_with_tag(tag: str) -> subprocess.CompletedProcess[str]:
    run = step_named(load(WORKFLOWS / "review.yml"), "guard", GUARD_STEP)["run"]
    return subprocess.run(
        ["bash", "-c", run],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "THESEUS_REF": "a" * 40,
            "GATED_BASE": "main",
            "CALLER_WORKFLOW": "oracle-verdict.yml",
            "TAILSCALE_TAG": tag,
            "WORKFLOW_REF": "o/r/.github/workflows/oracle-verdict.yml@refs/heads/main",
            "REPOSITORY": "o/r",
            "EVENT_NAME": "push",
            "CURRENT_REF": "refs/heads/main",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_review_callee_takes_an_optional_tailscale_tag_that_defaults_to_the_shared_identity() -> None:
    document = load(WORKFLOWS / "review.yml")
    spec = document["on"]["workflow_call"]["inputs"]["tailscale_tag"]
    assert spec["required"] is False and spec["type"] == "string" and spec["default"] == "tag:oracle-ci"
    assert step_named(document, "guard", GUARD_STEP)["env"]["TAILSCALE_TAG"] == "${{ inputs.tailscale_tag }}"
    join = step_named(document, "review", "Join the tailnet to reach the credential gateway")
    assert join["with"]["tags"] == "${{ inputs.tailscale_tag }}"
    assert "tag:oracle-ci" not in yaml.safe_dump(document["jobs"])


@pytest.mark.parametrize("tag", ["tag:oracle-ci", "tag:theseus-ci", "tag:a1-b2"])
def test_the_guard_accepts_a_well_formed_tailscale_tag(tag: str) -> None:
    assert _guard_with_tag(tag).returncode == 0


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "oracle-ci",
        "tag:",
        "tag:Oracle",
        "tag:a_b",
        "tag:a b",
        "tag:a,tag:b",
        "tag:a\nx",
        "tag:a\n",
        "tag:ok\ntag:evil",
    ],
)
def test_the_guard_refuses_a_malformed_tailscale_tag(tag: str) -> None:
    result = _guard_with_tag(tag)
    assert result.returncode == 2
    assert "tailscale_tag must match" in result.stderr


APP_REFUSAL = "Refuse a caller that did not pass the App secrets"
APP_JOBS = [("review.yml", "sweep"), ("review.yml", "review"), ("gate.yml", "gate"), ("gate.yml", "sweep")]


@pytest.mark.parametrize(("name", "job"), APP_JOBS)
def test_a_secret_bearing_job_fails_loudly_first_when_the_app_secrets_are_empty(name: str, job: str) -> None:
    document = load(WORKFLOWS / name)
    first = steps_of(document, job)[0]
    assert first["name"] == APP_REFUSAL
    assert first["if"] == "env.HAS_APP != 'true'"
    result = subprocess.run(
        ["bash", "-c", first["run"]],
        env={"PATH": "/usr/bin:/bin", "ENVIRONMENT": "oracle"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("::error::")
    assert "environment oracle" in result.stdout and "secrets: inherit" in result.stdout


def test_the_gate_no_longer_has_a_quiet_path_without_the_app() -> None:
    text = (WORKFLOWS / "gate.yml").read_text(encoding="utf-8")
    assert "--no-post" not in text
    assert "::notice::" not in text
