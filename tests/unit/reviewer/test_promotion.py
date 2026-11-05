from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import promotion, protocol
from theseus_reviewer.diff import hunk_index
from theseus_reviewer.policy import Policy, PromotionRules

pytestmark = [pytest.mark.unit, pytest.mark.fast]

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _build_wheel(package: str, version: str, pkg_files: dict[str, bytes]) -> tuple[bytes, str]:
    norm_pkg = package.replace("-", "_")
    dist_info = f"{norm_pkg}-{version}.dist-info"
    entries: dict[str, bytes] = {
        **pkg_files,
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_lines: list[str] = []
    for name, content in sorted(entries.items()):
        b64 = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
        record_lines.append(f"{name},sha256={b64},{len(content)}")
    record_path = f"{dist_info}/RECORD"
    record_lines.append(f"{record_path},,")
    entries[record_path] = ("\n".join(record_lines) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(entries.items()):
            zf.writestr(name, content)
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def _lock_payload(
    *,
    version: str = "0.2.0",
    source_commit: str = SHA_B,
    source_sha256: str = "1" * 64,
    delta_sha256: str = "2" * 64,
    artifact_sha256: str = "3" * 64,
    workflow_sha: str | None = None,
    bootstrap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "package": "theseus-reviewer",
        "version": version,
        "repository": "joeharris76/theseus",
        "source_commit": source_commit,
        "source_sha256": source_sha256,
        "delta_sha256": delta_sha256,
        "artifact": {
            "sha256": artifact_sha256,
            "urls": [
                f"https://github.com/joeharris76/theseus/releases/download/v{version}/theseus_reviewer-{version}-py3-none-any.whl",
                f"https://files.pythonhosted.org/packages/theseus/theseus_reviewer-{version}-py3-none-any.whl",
            ],
        },
        "dependencies": [
            {
                "name": "pyyaml",
                "version": "6.0.3",
                "sha256": "4" * 64,
                "urls": ["https://files.pythonhosted.org/packages/pyyaml-6.0.3.whl"],
            }
        ],
        "workflow_sha": source_commit if workflow_sha is None else workflow_sha,
    }
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap
    return payload


def _base_record(head_sha: str = SHA_A, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "v": protocol.MARKER_VERSION,
        "cycle": 1,
        "round": 1,
        "kind": protocol.FIRST,
        "decision": protocol.SHIP,
        "head_sha": head_sha,
        "base_ref": "develop",
        "reviewer": "large",
        "tier": "large",
        "strikes_after": 0,
        "open_defects": [],
        "next_id": 1,
        "patch_digest": "d" * 32,
        "carried": False,
        "summary": "",
        "run_uuid": "9" * 32,
        "coverage": 1,
    }
    return {**record, **overrides}


def test_lock_requires_two_distinct_https_artifact_sources_and_matching_workflow_sha() -> None:
    single = _lock_payload()
    single["artifact"] = {
        "sha256": "3" * 64,
        "urls": ["https://github.com/joeharris76/theseus/releases/download/v0.2.0/wheel.whl"],
    }
    with pytest.raises(promotion.PromotionError, match="at least 2 https url"):
        promotion.parse_lock(json.dumps(single))

    duplicate = _lock_payload()
    duplicate["artifact"] = {
        "sha256": "3" * 64,
        "urls": [
            "https://github.com/joeharris76/theseus/releases/download/v0.2.0/wheel.whl",
            "https://github.com/joeharris76/theseus/releases/download/v0.2.0/wheel.whl",
        ],
    }
    with pytest.raises(promotion.PromotionError, match="distinct"):
        promotion.parse_lock(json.dumps(duplicate))

    mismatched_wf = _lock_payload(workflow_sha=SHA_C)
    with pytest.raises(promotion.PromotionError, match="workflow_sha must equal"):
        promotion.parse_lock(json.dumps(mismatched_wf))

    untrusted_host = _lock_payload()
    untrusted_host["artifact"]["urls"] = [
        "https://github.com/joeharris76/theseus/releases/download/v0.2.0/wheel.whl",
        "https://attacker.example/evil.whl",
    ]
    parsed = promotion.parse_lock(json.dumps(untrusted_host))
    with pytest.raises(promotion.PromotionError, match="allowed_artifact_hosts"):
        promotion.validate_lock_policy(parsed, PromotionRules(dependencies={"pyyaml==6.0.3": "4" * 64}))


def test_verify_wheel_matches_source_rejects_tampered_bytes_extra_files_and_pth_hooks() -> None:
    source_tree = {
        "src/theseus_reviewer/__init__.py": b"VERSION = '0.2.0'\n",
        "src/theseus_reviewer/cli.py": b"def main():\n    return 0\n",
        "README.md": b"docs\n",
    }
    good_wheel, _ = _build_wheel(
        "theseus-reviewer",
        "0.2.0",
        {
            "theseus_reviewer/__init__.py": b"VERSION = '0.2.0'\n",
            "theseus_reviewer/cli.py": b"def main():\n    return 0\n",
        },
    )
    promotion.verify_wheel_matches_source(
        good_wheel,
        source_tree,
        package="theseus-reviewer",
        version="0.2.0",
    )

    backdoored_wheel, _ = _build_wheel(
        "theseus-reviewer",
        "0.2.0",
        {
            "theseus_reviewer/__init__.py": b"VERSION = '0.2.0'\n",
            "theseus_reviewer/cli.py": b"def main():\n    return 99\n",
        },
    )
    with pytest.raises(promotion.PromotionError, match="bytes do not match"):
        promotion.verify_wheel_matches_source(
            backdoored_wheel,
            source_tree,
            package="theseus-reviewer",
            version="0.2.0",
        )

    pth_wheel, _ = _build_wheel(
        "theseus-reviewer",
        "0.2.0",
        {
            "theseus_reviewer/__init__.py": b"VERSION = '0.2.0'\n",
            "theseus_reviewer/cli.py": b"def main():\n    return 0\n",
            "evil.pth": b"import os\n",
        },
    )
    with pytest.raises(promotion.PromotionError, match="forbidden executable or hook"):
        promotion.verify_wheel_matches_source(
            pth_wheel,
            source_tree,
            package="theseus-reviewer",
            version="0.2.0",
        )


def test_bootstrap_path_map_and_delta_digests_are_deterministic() -> None:
    benchbox_tree = {
        "_project/scripts/oracle_reviewers/cli.py": b"def run():\n    return 1\n",
        "_project/scripts/oracle_gate.py": b"GATE = True\n",
        "benchbox/core/unrelated.py": b"UNRELATED = 1\n",
    }
    path_map = {
        "_project/scripts/oracle_reviewers": "src/theseus_reviewer",
        "_project/scripts/oracle_gate.py": "src/theseus_reviewer/oracle_gate.py",
    }
    mapped_old = promotion.apply_path_map(benchbox_tree, path_map)
    assert set(mapped_old) == {
        "src/theseus_reviewer/cli.py",
        "src/theseus_reviewer/oracle_gate.py",
    }
    new_tree = {
        "src/theseus_reviewer/cli.py": b"def run():\n    return 2\n",
        "src/theseus_reviewer/oracle_gate.py": b"GATE = True\n",
        "src/theseus_reviewer/new_mod.py": b"NEW = True\n",
    }
    changed, diff_text, delta_sha = promotion.compute_upstream_delta(mapped_old, new_tree)
    assert [item.path for item in changed] == [
        ".oracle-promotion/new/src/theseus_reviewer/cli.py",
        ".oracle-promotion/new/src/theseus_reviewer/new_mod.py",
    ]
    assert "+    return 2" in diff_text and "+NEW = True" in diff_text
    assert len(delta_sha) == 64


def test_extract_tar_tree_rejects_symlinks_and_parent_traversal() -> None:
    symlink_buf = io.BytesIO()
    with tarfile.open(fileobj=symlink_buf, mode="w") as archive:
        info = tarfile.TarInfo("repo-root/link.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(promotion.PromotionError, match="non-regular"):
        promotion.extract_tar_tree(symlink_buf.getvalue(), strip_single_root=True)

    traversal_buf = io.BytesIO()
    with tarfile.open(fileobj=traversal_buf, mode="w") as archive:
        info = tarfile.TarInfo("repo-root/../escape.py")
        payload = b"x = 1\n"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(promotion.PromotionError, match="invalid path"):
        promotion.extract_tar_tree(traversal_buf.getvalue(), strip_single_root=True)


def test_baseline_archive_is_bounded_only_by_the_mapped_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_TREE_FILES", 4)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in [*(f"repo-root/benchmarks/data{index}.csv" for index in range(10)), "repo-root/tool/a.py"]:
            payload = name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("repo-root/tool/link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    with pytest.raises(promotion.PromotionError, match="maximum file count"):
        promotion.extract_tar_tree(buffer.getvalue(), strip_single_root=True)
    with pytest.raises(promotion.PromotionError, match="non-regular"):
        promotion.extract_tar_tree(buffer.getvalue(), strip_single_root=True, prefixes=("tool",))
    assert promotion.extract_tar_tree(buffer.getvalue(), strip_single_root=True, prefixes=("tool/a.py",)) == {
        "tool/a.py": b"repo-root/tool/a.py"
    }


def test_unrooted_archive_keeps_leading_dot_paths() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in [".github/oracle-reviewers.yml", "./.gitignore", "src/a.py"]:
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    assert set(promotion.extract_tar_tree(buffer.getvalue())) == {
        ".github/oracle-reviewers.yml",
        ".gitignore",
        "src/a.py",
    }


def test_protocol_marker_preserves_legacy_v1_compatibility_and_validates_promotion() -> None:
    legacy_encoded = protocol.encode_marker(_base_record())
    legacy_decoded = protocol.decode_marker(legacy_encoded)
    assert legacy_decoded is not None and "promotion" not in legacy_decoded

    promo = promotion.PromotionRecord(
        head_sha=SHA_A,
        old_source_sha256="1" * 64,
        new_source_sha256="2" * 64,
        old_artifact_sha256="3" * 64,
        new_artifact_sha256="4" * 64,
        delta_sha256="5" * 64,
        lock_sha256="6" * 64,
        bootstrap=False,
    ).to_json()
    promo_encoded = protocol.encode_marker(_base_record(promotion=promo))
    promo_decoded = protocol.decode_marker(promo_encoded)
    assert promo_decoded is not None and promo_decoded["promotion"] == promo

    malformed = _base_record(promotion={**promo, "delta_sha256": "short"})
    assert protocol.checked_record(malformed) is None


def test_promotion_rounds_never_carry_across_byte_identical_rebase_while_ordinary_rounds_still_carry() -> None:
    prior = _base_record(head_sha=SHA_A)
    history = protocol.History(records=[prior])
    ordinary = protocol.plan_round(
        history,
        head_sha=SHA_B,
        base_ref="develop",
        digest="d" * 32,
        patches={"abcd1234": "0123456789abcdef"},
        paths=[".github/theseus-lock.json"],
        strike_count=0,
        max_do_not_ship=3,
        is_promotion=False,
    )
    assert ordinary.kind == protocol.CARRY

    promoted = protocol.plan_round(
        history,
        head_sha=SHA_B,
        base_ref="develop",
        digest="d" * 32,
        patches={"abcd1234": "0123456789abcdef"},
        paths=[".github/theseus-lock.json", ".oracle-promotion/new/src/theseus_reviewer/cli.py"],
        strike_count=0,
        max_do_not_ship=3,
        is_promotion=True,
    )
    assert promoted.kind == protocol.FIRST

    prior_with_promo = _base_record(
        head_sha=SHA_A,
        promotion=promotion.PromotionRecord(
            head_sha=SHA_A,
            old_source_sha256="1" * 64,
            new_source_sha256="2" * 64,
            old_artifact_sha256="3" * 64,
            new_artifact_sha256="4" * 64,
            delta_sha256="5" * 64,
            lock_sha256="6" * 64,
        ).to_json(),
    )
    rebased_from_promo = protocol.plan_round(
        protocol.History(records=[prior_with_promo]),
        head_sha=SHA_C,
        base_ref="develop",
        digest="d" * 32,
        patches={"abcd1234": "0123456789abcdef"},
        paths=["README.md"],
        strike_count=0,
        max_do_not_ship=3,
        is_promotion=False,
    )
    assert rebased_from_promo.kind == protocol.FIRST


def test_stage_promotion_workspace_is_git_ignored_and_detects_tampering(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "--quiet"], check=True)
    (ws / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e", "add", "tracked.py"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--quiet", "-m", "init"],
        check=True,
    )

    bundle = promotion.PromotionBundle(
        record=promotion.PromotionRecord(
            head_sha=SHA_A,
            old_source_sha256="1" * 64,
            new_source_sha256="2" * 64,
            old_artifact_sha256="3" * 64,
            new_artifact_sha256="4" * 64,
            delta_sha256="5" * 64,
            lock_sha256="6" * 64,
        ),
        changed_files=(),
        diff_text="diff --git a/a.py b/a.py\n",
        old_tree={"a.py": b"old\n"},
        new_tree={"a.py": b"new\n"},
    )
    plan_dir = tmp_path / "plan"
    promotion.write_materialized_bundle(bundle, plan_dir)
    staged = promotion.stage_promotion_workspace(plan_dir, ws)
    assert staged is not None
    status = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    assert status == ""
    before = promotion.staged_tree_digest(ws)
    assert before is not None and before != "invalid"
    (staged / "new" / "a.py").write_text("tampered\n", encoding="utf-8")
    assert promotion.staged_tree_digest(ws) != before


def test_planted_oracle_promotion_directory_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws-planted"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "--quiet"], check=True)
    planted = ws / ".oracle-promotion" / "new"
    planted.mkdir(parents=True)
    (planted / "a.py").write_text("benign\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e", "add", ".oracle-promotion/new/a.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--quiet", "-m", "plant"],
        check=True,
    )
    bundle = promotion.PromotionBundle(
        record=promotion.PromotionRecord(
            head_sha=SHA_A,
            old_source_sha256="1" * 64,
            new_source_sha256="2" * 64,
            old_artifact_sha256="3" * 64,
            new_artifact_sha256="4" * 64,
            delta_sha256="5" * 64,
            lock_sha256="6" * 64,
        ),
        changed_files=(),
        diff_text="diff --git a/a.py b/a.py\n",
        old_tree={"a.py": b"old\n"},
        new_tree={"a.py": b"malicious\n"},
    )
    plan_dir = tmp_path / "plan-planted"
    promotion.write_materialized_bundle(bundle, plan_dir)
    assert promotion.stage_promotion_workspace(plan_dir, ws) is None


def test_removed_upstream_files_stage_under_old_and_duplicate_path_map_keys_fail() -> None:
    changed, _, _ = promotion.compute_upstream_delta(
        {"src/guard.py": b"def guard():\n    return True\n"},
        {},
    )
    assert len(changed) == 1
    assert changed[0].path == ".oracle-promotion/old/src/guard.py"
    assert changed[0].status == "removed"
    assert changed[0].deletions == 2

    colliding = _lock_payload(
        bootstrap={
            "baseline_repo": "BenchBox-dev/BenchBox",
            "baseline_commit": SHA_A,
            "baseline_source_sha256": "1" * 64,
            "path_map": {"pkg/a": "dst/one", "pkg/a/": "dst/two"},
            "reviewer": "joeharris76",
            "approved_head": SHA_B,
        }
    )
    with pytest.raises(promotion.PromotionError, match="duplicate bootstrap path_map"):
        promotion.parse_lock(json.dumps(colliding))


def _preparation_inputs(
    *, bootstrap: bool = False, old_extra: dict[str, bytes] | None = None, new_extra: dict[str, bytes] | None = None
) -> dict[str, Any]:
    old_tree = {"src/theseus_reviewer/__init__.py": b"VALUE = 1\n", **(old_extra or {})}
    new_tree = {"src/theseus_reviewer/__init__.py": b"VALUE = 2\n", **(new_extra or {})}
    wheel, wheel_sha = _build_wheel("theseus-reviewer", "0.2.0", {"theseus_reviewer/__init__.py": b"VALUE = 2\n"})
    dependency, dependency_sha = _build_wheel("pyyaml", "6.0.3", {"pyyaml/__init__.py": b"SAFE = True\n"})
    head = _lock_payload(
        source_sha256=promotion.tree_sha256(new_tree),
        delta_sha256=promotion.compute_delta_sha256(old_tree, new_tree),
        artifact_sha256=wheel_sha,
    )
    head["dependencies"][0]["sha256"] = dependency_sha
    base = _lock_payload(source_commit=SHA_C, source_sha256=promotion.tree_sha256(old_tree))
    path_map = {"src/theseus_reviewer": "src/theseus_reviewer"}
    if bootstrap:
        head["bootstrap"] = {
            "baseline_repo": "BenchBox-dev/BenchBox",
            "baseline_commit": SHA_C,
            "baseline_source_sha256": promotion.tree_sha256(old_tree),
            "path_map": path_map,
            "reviewer": "joeharris76",
            "approved_head": SHA_B,
        }
    return {
        "repo": "BenchBox-dev/BenchBox",
        "base_sha": SHA_C,
        "head_sha": SHA_A,
        "base_lock_text": None if bootstrap else json.dumps(base),
        "head_lock_text": json.dumps(head),
        "source_loader": lambda _repo, commit, _prefixes: new_tree if commit == SHA_B else old_tree,
        "is_ancestor": lambda _ancestor, _descendant: True,
        "artifact_loader": lambda url: dependency if "pyyaml" in url else wheel,
        "rules": PromotionRules(
            repository="joeharris76/theseus",
            bootstrap_reviewers=("joeharris76",),
            dependencies={"pyyaml==6.0.3": dependency_sha},
            bootstrap_baseline_commit=SHA_C,
            bootstrap_path_map=path_map,
        ),
        "approval_comments": [],
    }


def test_preparation_accepts_trusted_dependency_pin_and_rejects_unpinned_author_hash() -> None:
    inputs = _preparation_inputs()
    assert promotion.prepare_promotion(**inputs) is not None
    inputs["rules"] = replace(inputs["rules"], dependencies={})
    with pytest.raises(promotion.PromotionError, match="not pinned in the base review policy"):
        promotion.prepare_promotion(**inputs)


def test_preparation_rejects_changed_dependency_even_when_lock_matches_attacker_bytes() -> None:
    inputs = _preparation_inputs()
    head = json.loads(inputs["head_lock_text"])
    malicious, digest = _build_wheel("pyyaml", "6.0.3", {"pyyaml/__init__.py": b"BACKDOOR = True\n"})
    head["dependencies"][0]["sha256"] = digest
    original_loader = inputs["artifact_loader"]
    inputs["head_lock_text"] = json.dumps(head)
    inputs["artifact_loader"] = lambda url: malicious if "pyyaml" in url else original_loader(url)
    with pytest.raises(promotion.PromotionError, match="not pinned in the base review policy"):
        promotion.prepare_promotion(**inputs)


def test_dependency_policy_uses_normalized_distribution_names() -> None:
    payload = _lock_payload()
    payload["dependencies"][0]["name"] = "my_package.name"
    promotion.validate_lock_policy(
        promotion.parse_lock(json.dumps(payload)),
        PromotionRules(dependencies={"my-package-name==6.0.3": "4" * 64}),
    )


def test_bootstrap_preparation_requires_authenticated_approval_but_local_remains_advisory() -> None:
    inputs = _preparation_inputs(bootstrap=True)
    with pytest.raises(promotion.PromotionError, match="unedited pull request comment"):
        promotion.prepare_promotion(**inputs)
    inputs["approval_comments"] = None
    assert promotion.prepare_promotion(**inputs) is not None
    lock = promotion.parse_lock(inputs["head_lock_text"])
    inputs["approval_comments"] = [
        {
            "login": "joeharris76",
            "user_type": "User",
            "created_at": "2026-11-02T12:00:00Z",
            "updated_at": "2026-11-02T12:00:00Z",
            "body": promotion.bootstrap_approval_line(lock),
        }
    ]
    assert promotion.prepare_promotion(**inputs) is not None


def test_bootstrap_preparation_compares_the_baseline_repository_without_case() -> None:
    inputs = _preparation_inputs(bootstrap=True)
    inputs["approval_comments"] = None
    inputs["repo"] = "benchbox-dev/benchbox"
    assert promotion.prepare_promotion(**inputs) is not None
    for other in ("BenchBox-dev/Other", "Other-dev/BenchBox"):
        inputs["repo"] = other
        with pytest.raises(promotion.PromotionError, match="baseline_repo does not match"):
            promotion.prepare_promotion(**inputs)


def test_the_lock_repository_is_compared_with_the_policy_without_case() -> None:
    inputs = _preparation_inputs()
    inputs["rules"] = replace(inputs["rules"], repository="JoeHarris76/Theseus")
    assert promotion.prepare_promotion(**inputs) is not None
    for other in ("joeharris76/other", "other/theseus"):
        inputs["rules"] = replace(inputs["rules"], repository=other)
        with pytest.raises(promotion.PromotionError, match="repository does not match consumer policy"):
            promotion.prepare_promotion(**inputs)


def test_a_github_artifact_url_is_matched_to_the_lock_repository_without_case() -> None:
    payload = _lock_payload()
    payload["dependencies"] = []
    rules = PromotionRules(repository="joeharris76/theseus")
    payload["artifact"]["urls"][0] = payload["artifact"]["urls"][0].replace(
        "joeharris76/theseus", "JoeHarris76/Theseus"
    )
    promotion.validate_lock_policy(promotion.parse_lock(json.dumps(payload)), rules)
    payload["artifact"]["urls"][0] = payload["artifact"]["urls"][0].replace("JoeHarris76/Theseus", "other/theseus")
    with pytest.raises(promotion.PromotionError, match="github.com artifact url must start with"):
        promotion.validate_lock_policy(promotion.parse_lock(json.dumps(payload)), rules)


def test_the_local_checkout_serves_its_own_repository_without_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(promotion.SOURCES_ENV, raising=False)
    served: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        promotion, "_git_archive_tree", lambda root, commit, prefixes=None: served.append((root, commit)) or {}
    )
    loader = promotion.default_source_loader(local_git_root=tmp_path, local_consumer_repo="Joeharris76/Theseus")
    assert loader("joeharris76/theseus", SHA_A, None) == {}
    assert served == [(tmp_path, SHA_A)]


def test_trusted_dependency_pin_still_requires_matching_downloaded_bytes() -> None:
    inputs = _preparation_inputs()
    original_loader = inputs["artifact_loader"]
    inputs["artifact_loader"] = lambda url: b"tampered" if "pyyaml" in url else original_loader(url)
    with pytest.raises(promotion.PromotionError, match="dependency pyyaml .* has sha256"):
        promotion.prepare_promotion(**inputs)


@pytest.mark.parametrize("fetch_fails", [False, True])
@pytest.mark.parametrize("status", ["added", "renamed"])
def test_ci_bootstrap_reads_authenticated_comments_and_holds_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, policy: Policy, fetch_fails: bool, status: str
) -> None:
    from theseus_reviewer import cli, github
    from theseus_reviewer.classifier import ChangedFile

    inputs = _preparation_inputs(bootstrap=True)
    lock = promotion.parse_lock(inputs["head_lock_text"])
    requests: list[str] = []
    lock_requests: list[str] = []

    def lock_text(endpoint: str, **_kwargs: Any) -> str:
        lock_requests.append(endpoint)
        if endpoint.endswith(f"?ref={SHA_C}"):
            raise github.GitHubError("gh: Not Found (HTTP 404)")
        if endpoint.startswith(f"repos/BenchBox-dev/BenchBox/contents/{promotion.WORKFLOW_DIR}"):
            return "[]"
        return inputs["head_lock_text"]

    def comments(endpoint: str) -> list[dict[str, Any]]:
        requests.append(endpoint)
        if fetch_fails:
            raise github.GitHubError("comment fetch unavailable")
        return [
            {
                "user": {"login": "joeharris76", "type": "User"},
                "body": promotion.bootstrap_approval_line(lock),
                "created_at": "2026-11-02T12:00:00Z",
                "updated_at": "2026-11-02T12:00:00Z",
            }
        ]

    monkeypatch.setattr(github, "_gh", lock_text)
    monkeypatch.setattr(github, "get_paginated", comments)
    monkeypatch.setattr(promotion, "default_source_loader", lambda: inputs["source_loader"])
    monkeypatch.setattr(promotion, "default_artifact_loader", lambda: inputs["artifact_loader"])
    monkeypatch.setattr(promotion, "ci_ancestor_check", lambda _repo: inputs["is_ancestor"])
    bundle, reason = cli._load_ci_promotion(
        inputs["repo"],
        {"pr": 7, "base_sha": SHA_C, "head_sha": SHA_A},
        replace(policy, promotion=inputs["rules"]),
        [
            ChangedFile.from_api(
                {
                    "filename": promotion.LOCK_PATH,
                    "previous_filename": "alternate-lock.json" if status == "renamed" else None,
                    "status": status,
                }
            )
        ],
    )
    assert requests == ["repos/BenchBox-dev/BenchBox/issues/7/comments?per_page=100"]
    expected_requests = [
        f"repos/BenchBox-dev/BenchBox/contents/{promotion.LOCK_PATH}?ref={SHA_C}",
        f"repos/BenchBox-dev/BenchBox/contents/{promotion.LOCK_PATH}?ref={SHA_A}",
    ]
    if not fetch_fails:
        expected_requests.extend(
            f"repos/BenchBox-dev/BenchBox/contents/{promotion.WORKFLOW_DIR}?ref={ref}" for ref in (SHA_C, SHA_A)
        )
    assert lock_requests == expected_requests
    if fetch_fails:
        assert bundle is None
        assert reason is not None and "comment fetch unavailable" in reason
    else:
        assert bundle is not None
        assert reason is None


@pytest.mark.parametrize("suffix", [".so", ".pyd", ".dll", ".dylib", ".data/data/payload"])
def test_pinned_native_dependency_is_verified_without_applying_main_wheel_purity_rules(suffix: str) -> None:
    inputs = _preparation_inputs()
    dependency, digest = _build_wheel("pyyaml", "6.0.3", {f"pyyaml/native{suffix}": b"trusted native or data bytes"})
    head = json.loads(inputs["head_lock_text"])
    head["dependencies"][0]["sha256"] = digest
    inputs["head_lock_text"] = json.dumps(head)
    inputs["rules"] = replace(inputs["rules"], dependencies={"pyyaml==6.0.3": digest})
    original_loader = inputs["artifact_loader"]
    inputs["artifact_loader"] = lambda url: dependency if "pyyaml" in url else original_loader(url)
    assert promotion.prepare_promotion(**inputs) is not None
    inputs["artifact_loader"] = lambda url: dependency + b"tampered" if "pyyaml" in url else original_loader(url)
    with pytest.raises(promotion.PromotionError, match="dependency pyyaml .* has sha256"):
        promotion.prepare_promotion(**inputs)


@pytest.mark.parametrize("suffix", [".so", ".pyd", ".dll", ".dylib", ".data/data/payload"])
def test_main_wheel_still_rejects_native_and_data_entries(suffix: str) -> None:
    files = {
        "theseus_reviewer/__init__.py": b"VALUE = 1\n",
        f"theseus_reviewer/native{suffix}": b"native or data bytes",
    }
    wheel, _ = _build_wheel("theseus-reviewer", "0.2.0", files)
    with pytest.raises(promotion.PromotionError, match="forbidden"):
        promotion.verify_wheel_matches_source(wheel, files, package="theseus-reviewer", version="0.2.0")


@pytest.mark.parametrize("other_name", ["my_package", "my..package", "my-_package"])
@pytest.mark.parametrize("other_version", ["6.0.3", "6.0.4"])
def test_lock_rejects_duplicate_normalized_dependencies(other_name: str, other_version: str) -> None:
    payload = _lock_payload()
    dependency = payload["dependencies"][0]
    dependency["name"] = "my-package"
    payload["dependencies"].append({**dependency, "name": other_name, "version": other_version})
    with pytest.raises(promotion.PromotionError, match="duplicate normalized dependencies"):
        promotion.parse_lock(json.dumps(payload))


@pytest.mark.parametrize("error", ["gh: Forbidden (HTTP 403)", "gh: server error (HTTP 500)", "network unavailable"])
def test_ci_lock_fetch_errors_other_than_not_found_hold(
    monkeypatch: pytest.MonkeyPatch, policy: Policy, error: str
) -> None:
    from theseus_reviewer import cli, github
    from theseus_reviewer.classifier import ChangedFile

    def fail(*_args: Any, **_kwargs: Any) -> str:
        raise github.GitHubError(error)

    monkeypatch.setattr(github, "_gh", fail)
    bundle, reason = cli._load_ci_promotion(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": SHA_C, "head_sha": SHA_A},
        policy,
        [ChangedFile.from_api({"filename": promotion.LOCK_PATH, "status": "added"})],
    )
    assert bundle is None
    assert reason is not None and error in reason


def test_ci_rename_away_from_canonical_lock_is_removal_hold(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    from theseus_reviewer import cli, github
    from theseus_reviewer.classifier import ChangedFile

    inputs = _preparation_inputs()

    def lock_text(endpoint: str, **_kwargs: Any) -> str:
        if endpoint.endswith(f"?ref={SHA_C}"):
            return inputs["base_lock_text"]
        raise github.GitHubError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github, "_gh", lock_text)
    bundle, reason = cli._load_ci_promotion(
        inputs["repo"],
        {"pr": 7, "base_sha": SHA_C, "head_sha": SHA_A},
        replace(policy, promotion=inputs["rules"]),
        [
            ChangedFile.from_api(
                {
                    "filename": "alternate-lock.json",
                    "previous_filename": promotion.LOCK_PATH,
                    "status": "renamed",
                }
            )
        ],
    )
    assert bundle is None
    assert reason is not None and "lock cannot be removed" in reason


@pytest.mark.parametrize("rename_into_lock", [True, False])
def test_local_lock_renames_resolve_only_canonical_base_and_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: Policy, rename_into_lock: bool
) -> None:
    from theseus_reviewer import cli
    from theseus_reviewer.classifier import ChangedFile

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()

    inputs = _preparation_inputs(bootstrap=rename_into_lock)
    git("init", "--quiet")
    (tmp_path / ".github").mkdir()
    old_path = "alternate-lock.json" if rename_into_lock else promotion.LOCK_PATH
    new_path = promotion.LOCK_PATH if rename_into_lock else "alternate-lock.json"
    (tmp_path / old_path).write_text(
        json.dumps(_lock_payload()) if rename_into_lock else inputs["base_lock_text"], encoding="utf-8"
    )
    git("add", old_path)
    git("commit", "--quiet", "-m", "base")
    base_sha = git("rev-parse", "HEAD")
    git("mv", old_path, new_path)
    if rename_into_lock:
        (tmp_path / new_path).write_text(inputs["head_lock_text"], encoding="utf-8")
    git("add", new_path)
    git("commit", "--quiet", "-m", "rename lock")
    head_sha = git("rev-parse", "HEAD")
    with pytest.raises(promotion.PromotionError, match="git ls-tree"):
        cli._git_lock_text(tmp_path, "f" * 40)
    monkeypatch.setenv("ORACLE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(promotion, "default_source_loader", lambda **_kwargs: inputs["source_loader"])
    monkeypatch.setattr(promotion, "default_artifact_loader", lambda: inputs["artifact_loader"])
    monkeypatch.setattr(promotion, "local_ancestor_check", lambda _root: inputs["is_ancestor"])
    files = [ChangedFile.from_api({"filename": new_path, "previous_filename": old_path, "status": "renamed"})]
    round_inputs = cli.RoundInputs(
        files=files,
        in_scope={promotion.LOCK_PATH},
        full_diff=None,
        states=None,
        found=protocol.History(records=[]),
        previous_strikes=0,
        pool_blocks={},
        manual=False,
        event_name="local",
        base_changed_at=None,
        base_change_unreadable=False,
    )
    bundle, reason = cli._resolve_promotion_bundle(
        {"repo": inputs["repo"], "base_sha": base_sha, "head_sha": head_sha},
        replace(policy, promotion=inputs["rules"]),
        round_inputs,
    )
    if rename_into_lock:
        assert bundle is not None and bundle.record.bootstrap
        assert reason is None
    else:
        assert bundle is None
        assert reason is not None and "lock cannot be removed" in reason


def test_pinned_native_dependency_still_requires_valid_record_integrity() -> None:
    inputs = _preparation_inputs()
    dependency, _ = _build_wheel("pyyaml", "6.0.3", {"pyyaml/native.so": b"original native bytes"})
    entries = promotion._read_wheel_entries(dependency)
    entries["pyyaml/native.so"] = b"changed bytes with stale RECORD"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    dependency = buf.getvalue()
    digest = hashlib.sha256(dependency).hexdigest()
    head = json.loads(inputs["head_lock_text"])
    head["dependencies"][0]["sha256"] = digest
    inputs["head_lock_text"] = json.dumps(head)
    inputs["rules"] = replace(inputs["rules"], dependencies={"pyyaml==6.0.3": digest})
    original_loader = inputs["artifact_loader"]
    inputs["artifact_loader"] = lambda url: dependency if "pyyaml" in url else original_loader(url)
    with pytest.raises(promotion.PromotionError, match="wheel RECORD sha256 mismatch"):
        promotion.prepare_promotion(**inputs)


@pytest.mark.parametrize("encrypted", [True, False])
def test_unreadable_wheel_entries_raise_promotion_errors(encrypted: bool) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("package/module.py", b"VALUE = 1\n")
    raw = bytearray(buf.getvalue())
    central = raw.index(b"PK\x01\x02")
    if encrypted:
        raw[6:8] = (1).to_bytes(2, "little")
        raw[central + 8 : central + 10] = (1).to_bytes(2, "little")
    else:
        raw[8:10] = (99).to_bytes(2, "little")
        raw[central + 10 : central + 12] = (99).to_bytes(2, "little")
    with pytest.raises(promotion.PromotionError, match="invalid wheel archive"):
        promotion._read_wheel_entries(bytes(raw))


def test_oversized_record_field_raises_promotion_error() -> None:
    wheel, _ = _build_wheel("pyyaml", "6.0.3", {"pyyaml/__init__.py": b""})
    entries = promotion._read_wheel_entries(wheel)
    entries["pyyaml-6.0.3.dist-info/RECORD"] = b"x" * (promotion.csv.field_size_limit() + 1)
    with pytest.raises(promotion.PromotionError, match="invalid wheel RECORD"):
        promotion.verify_wheel_record(entries, "pyyaml", "6.0.3")


STAGED_NEW = f"{promotion.STAGED_PROMOTION_DIR}/new/"
STAGED_OLD = f"{promotion.STAGED_PROMOTION_DIR}/old/"
THESEUS = "joeharris76/theseus"
CONSUMER_WORKFLOWS = {
    ".github/workflows/oracle.yml": (
        "jobs:\n"
        "  review:\n"
        f"    uses: {THESEUS}/.github/workflows/review.yml@{SHA_B}\n"
        "  gate:\n"
        f"    uses: '{THESEUS}/.github/workflows/gate.yml@{SHA_B}'\n"
        "  other:\n"
        "    uses: someone/else/.github/workflows/review.yml@v1\n"
        "  # uses: joeharris76/theseus/.github/workflows/commented.yml@v1\n"
    )
}
SIMILAR_BODY = b"".join(f"line {number}\n".encode() for number in range(1, 21))
SURFACE_OLD = {
    "src/theseus_reviewer/__init__.py": b"VALUE = 1\n",
    "src/theseus_reviewer/cli.py": b"def run():\n    return 1\n",
    "src/theseus_reviewer/moved_from.py": b"identical\ncontent\nmoved\n",
    "src/theseus_reviewer/edited_from.py": SIMILAR_BODY,
    "pyproject.toml": b'[project]\nname = "theseus-reviewer"\nversion = "1"\n',
    ".github/workflows/review.yml": b"name: review\n",
    ".github/workflows/gate.yml": b"name: gate\n",
    ".github/workflows/ci.yml": b"name: ci\n",
    "docs/guide.md": b"guide\n",
    "tests/test_cli.py": b"def test():\n    pass\n",
    "tests/test_old_name.py": b"alpha\nbeta\ngamma\n",
}
SURFACE_NEW = {
    "src/theseus_reviewer/__init__.py": b"VALUE = 1\n",
    "src/theseus_reviewer/cli.py": b"def run():\n    return 2\n",
    "src/theseus_reviewer/moved_to.py": b"identical\ncontent\nmoved\n",
    "src/theseus_reviewer/edited_to.py": SIMILAR_BODY.replace(b"line 20", b"line twenty"),
    "pyproject.toml": b'[project]\nname = "theseus-reviewer"\nversion = "2"\n',
    ".github/workflows/review.yml": b"name: review changed\n",
    ".github/workflows/gate.yml": b"name: gate\n",
    ".github/workflows/ci.yml": b"name: ci changed\n",
    "docs/guide.md": b"guide changed\n",
    "tests/test_cli.py": b"def test():\n    assert True\n",
    "tests/test_new_name.py": b"alpha\nbeta\ngamma\n",
}


def _surface(tree: dict[str, bytes], workflows: dict[str, str] | None = None, *, strict: bool = True) -> frozenset[str]:
    return promotion.executed_surface(
        tree,
        package="theseus-reviewer",
        repository=THESEUS,
        consumer_workflows=(chosen := CONSUMER_WORKFLOWS if workflows is None else workflows),
        pins=dict.fromkeys(chosen, SHA_B),
        strict=strict,
    )


def _required(old: dict[str, bytes], new: dict[str, bytes], workflows: dict[str, str] | None = None) -> frozenset[str]:
    changed, _, _ = promotion.compute_upstream_delta(old, new)
    return promotion.required_read_paths(
        changed, old, new, _surface(new, workflows), _surface(old, workflows, strict=False)
    )


def test_delta_detects_exact_and_similar_renames() -> None:
    changed, diff_text, _ = promotion.compute_upstream_delta(SURFACE_OLD, SURFACE_NEW)
    renames = {item.path: item for item in changed if item.status == "renamed"}
    assert set(renames) == {
        f"{STAGED_NEW}src/theseus_reviewer/moved_to.py",
        f"{STAGED_NEW}src/theseus_reviewer/edited_to.py",
        f"{STAGED_NEW}tests/test_new_name.py",
    }
    exact = [item for item in renames.values() if (item.additions, item.deletions) == (0, 0)]
    assert sorted(item.path for item in exact) == [
        f"{STAGED_NEW}src/theseus_reviewer/moved_to.py",
        f"{STAGED_NEW}tests/test_new_name.py",
    ]
    similar = renames[f"{STAGED_NEW}src/theseus_reviewer/edited_to.py"]
    assert similar.previous_path == f"{STAGED_OLD}src/theseus_reviewer/edited_from.py"
    assert (similar.additions, similar.deletions) == (1, 1)
    assert "rename from src/theseus_reviewer/edited_from.py\nrename to src/theseus_reviewer/edited_to.py" in diff_text
    assert "diff --git a/src/theseus_reviewer/edited_from.py b/src/theseus_reviewer/edited_to.py\n" in diff_text
    index = hunk_index(diff_text)
    assert len(index["src/theseus_reviewer/edited_to.py"]) == 1
    assert not index.get("src/theseus_reviewer/moved_to.py")
    assert not any(item.status == "removed" for item in changed)
    assert {item.status for item in changed} == {"renamed", "modified"}


def test_removed_files_keep_a_whole_old_content_hunk() -> None:
    changed, diff_text, _ = promotion.compute_upstream_delta(
        {"src/guard.py": b"a\nb\nc\n", "keep.py": b"k\n"}, {"keep.py": b"k\n"}
    )
    assert [(item.path, item.status, item.deletions) for item in changed] == [
        (f"{STAGED_OLD}src/guard.py", "removed", 3)
    ]
    hunks = hunk_index(diff_text)["src/guard.py"]
    assert len(hunks) == 1 and not hunks[0].additive


def test_identical_move_needs_no_read() -> None:
    changed, _, _ = promotion.compute_upstream_delta(SURFACE_OLD, SURFACE_NEW)
    assert "src/theseus_reviewer/moved_to.py" in _surface(SURFACE_NEW)
    required = _required(SURFACE_OLD, SURFACE_NEW)
    assert f"{STAGED_NEW}src/theseus_reviewer/moved_to.py" not in required
    assert f"{STAGED_NEW}src/theseus_reviewer/edited_to.py" in required


def test_required_reads_are_the_executed_surface() -> None:
    changed, _, _ = promotion.compute_upstream_delta(SURFACE_OLD, SURFACE_NEW)
    required = _required(SURFACE_OLD, SURFACE_NEW)
    assert required == {
        f"{STAGED_NEW}src/theseus_reviewer/cli.py",
        f"{STAGED_NEW}src/theseus_reviewer/edited_to.py",
        f"{STAGED_NEW}pyproject.toml",
        f"{STAGED_NEW}.github/workflows/review.yml",
    }
    assert {item.path for item in changed} - required >= {
        f"{STAGED_NEW}docs/guide.md",
        f"{STAGED_NEW}tests/test_cli.py",
        f"{STAGED_NEW}.github/workflows/ci.yml",
    }


def test_flat_layout_package_files_are_executed() -> None:
    tree = {"theseus_reviewer/__init__.py": b"x\n", "theseus_reviewer/data.json": b"{}\n", "other/mod.py": b"y\n"}
    assert _surface(tree, {}) == {"theseus_reviewer/__init__.py", "theseus_reviewer/data.json"}


def test_unreferenced_workflow_change_is_not_required() -> None:
    changed, _, _ = promotion.compute_upstream_delta(SURFACE_OLD, SURFACE_NEW)
    assert ".github/workflows/ci.yml" not in _surface(SURFACE_NEW)
    required = _required(SURFACE_OLD, SURFACE_NEW)
    assert f"{STAGED_NEW}.github/workflows/ci.yml" not in required
    assert f"{STAGED_NEW}.github/workflows/review.yml" in required


def test_nested_workflow_references_inside_the_theseus_tree_are_followed() -> None:
    tree = {
        **SURFACE_NEW,
        ".github/workflows/review.yml": b"jobs:\n  a:\n    uses: ./.github/workflows/ci.yml\n",
    }
    assert ".github/workflows/ci.yml" in _surface(tree)
    tree[".github/workflows/review.yml"] = (
        f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/missing.yml@{SHA_B}\n".encode()
    )
    with pytest.raises(promotion.PromotionError, match="missing.yml"):
        _surface(tree)


def test_missing_referenced_workflow_holds() -> None:
    tree = {path: content for path, content in SURFACE_NEW.items() if path != ".github/workflows/gate.yml"}
    with pytest.raises(promotion.PromotionError, match="gate.yml.*missing"):
        _surface(tree)
    inputs = _preparation_inputs()
    inputs["workflow_loader"] = lambda ref: {
        name: text.replace(SHA_B, SHA_C) if ref == SHA_C else text for name, text in CONSUMER_WORKFLOWS.items()
    }
    with pytest.raises(promotion.PromotionError, match="missing from the promoted source"):
        promotion.prepare_promotion(**inputs)


def test_preparation_records_the_required_paths_of_the_executed_surface() -> None:
    inputs = _preparation_inputs()
    inputs["workflow_loader"] = lambda _ref: {}
    bundle = promotion.prepare_promotion(**inputs)
    assert bundle is not None
    assert bundle.required_paths == {f"{STAGED_NEW}src/theseus_reviewer/__init__.py"}


def test_identical_move_into_the_executed_surface_is_required() -> None:
    old = {"src/theseus_reviewer/__init__.py": b"x\n", "tests/helper.py": b"payload\ncontent\nhere\n"}
    new = {"src/theseus_reviewer/__init__.py": b"x\n", "src/theseus_reviewer/helper.py": b"payload\ncontent\nhere\n"}
    assert _required(old, new, {}) == {f"{STAGED_NEW}src/theseus_reviewer/helper.py"}


def test_identical_move_within_the_executed_surface_is_skipped() -> None:
    old = {"src/theseus_reviewer/a.py": b"payload\ncontent\nhere\n"}
    new = {"src/theseus_reviewer/b.py": b"payload\ncontent\nhere\n"}
    assert _required(old, new, {}) == frozenset()


def test_removed_executed_file_is_required_and_removed_other_file_is_not() -> None:
    old = {
        "src/theseus_reviewer/__init__.py": b"x\n",
        "src/theseus_reviewer/guard.py": b"def guard():\n    return True\n",
        "docs/guide.md": b"guide\n",
    }
    new = {"src/theseus_reviewer/__init__.py": b"x\n"}
    assert _required(old, new, {}) == {f"{STAGED_OLD}src/theseus_reviewer/guard.py"}


def test_flow_style_workflow_reference_is_found() -> None:
    workflows = {"w.yml": f"jobs: {{gate: {{uses: '{THESEUS}/.github/workflows/gate.yml@{SHA_B}'}}}}\n"}
    assert ".github/workflows/gate.yml" in _surface(SURFACE_NEW, workflows)
    assert ".github/workflows/review.yml" not in _surface(SURFACE_NEW, workflows)


def test_anchored_and_aliased_workflow_reference_is_found() -> None:
    workflows = {
        "w.yml": (
            "x-shared: &shared\n"
            f"  uses: {THESEUS}/.github/workflows/review.yml@{SHA_B}\n"
            "jobs:\n"
            "  first: *shared\n"
            "  second:\n"
            "    <<: *shared\n"
        )
    }
    assert ".github/workflows/review.yml" in _surface(SURFACE_NEW, workflows)


def test_unparseable_workflow_holds() -> None:
    with pytest.raises(promotion.PromotionError, match="cannot be parsed"):
        _surface(SURFACE_NEW, {"w.yml": "jobs: [unclosed\n  - : :\n"})


def test_workflow_references_from_the_base_widen_the_surface() -> None:
    inputs = _preparation_inputs()
    inputs["workflow_loader"] = lambda ref: (
        {"w.yml": f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/gate.yml@{SHA_C}\n"} if ref == SHA_C else {}
    )
    with pytest.raises(promotion.PromotionError, match="gate.yml"):
        promotion.prepare_promotion(**inputs)


def test_delta_digest_unchanged_by_rename_detection() -> None:
    assert promotion.compute_upstream_delta(SURFACE_OLD, SURFACE_NEW)[2] == (
        "d75284f89ff241c8c6067792a687119abe64b258fbfefb92ae646a0324d02ca5"
    )
    moved_old = {"a.py": b"1\n", "dir/b.py": b"two\n"}
    moved_new = {"a.py": b"1\n", "dir2/b.py": b"two\n", "empty.txt": b""}
    assert promotion.compute_upstream_delta(moved_old, moved_new)[2] == (
        "bd51cb82572fffdce95981357b187ed57a92a1f4d9a371524895c9a0f38b7e55"
    )
    assert promotion.compute_upstream_delta(moved_new, moved_old)[2] == (
        "9d5e1a48d28478fcd2d531dbf47ca6bc14c873932d7973f8dd0adaa9f121abce"
    )


def _surface_pair(
    old: dict[str, bytes],
    new: dict[str, bytes],
    base_workflows: dict[str, str],
    head_workflows: dict[str, str],
) -> tuple[frozenset[str], frozenset[str]]:
    return _surface(new, {**base_workflows, **head_workflows}), _surface(old, base_workflows, strict=False)


def test_newly_referenced_workflow_is_required_whole() -> None:
    old = {**SURFACE_OLD, ".github/workflows/gate.yml": b"a\nb\nc\nd\n"}
    new = {**SURFACE_NEW, ".github/workflows/gate.yml": b"a\nb\nc\nD\n"}
    base = {"b.yml": f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/review.yml@{SHA_B}\n"}
    head = {"h.yml": f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/gate.yml@{SHA_B}\n"}
    surface, old_surface = _surface_pair(old, new, base, head)
    assert ".github/workflows/gate.yml" in surface
    assert ".github/workflows/gate.yml" not in old_surface
    changed, _, _ = promotion.compute_upstream_delta(old, new)
    required, whole = promotion.read_requirements(changed, old, new, surface, old_surface)
    assert f"{STAGED_NEW}.github/workflows/gate.yml" in required
    assert f"{STAGED_NEW}.github/workflows/gate.yml" in whole
    assert f"{STAGED_NEW}.github/workflows/review.yml" not in whole


def test_workflow_referenced_by_the_base_needs_only_its_hunks() -> None:
    old = {**SURFACE_OLD, ".github/workflows/gate.yml": b"a\nb\nc\nd\n"}
    new = {**SURFACE_NEW, ".github/workflows/gate.yml": b"a\nb\nc\nD\n"}
    base = {"b.yml": f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/gate.yml@{SHA_B}\n"}
    surface, old_surface = _surface_pair(old, new, base, {})
    changed, _, _ = promotion.compute_upstream_delta(old, new)
    required, whole = promotion.read_requirements(changed, old, new, surface, old_surface)
    assert f"{STAGED_NEW}.github/workflows/gate.yml" in required
    assert f"{STAGED_NEW}.github/workflows/gate.yml" not in whole


def test_preparation_requires_a_workflow_newly_referenced_by_the_head_whole() -> None:
    gate = ".github/workflows/gate.yml"
    inputs = _preparation_inputs(old_extra={gate: b"a\nb\nc\nd\n"}, new_extra={gate: b"a\nb\nc\nD\n"})
    reference = f"jobs:\n  a:\n    uses: {THESEUS}/{gate}@{SHA_B}\n"
    base_reference = f"jobs:\n  a:\n    uses: {THESEUS}/{gate}@{SHA_C}\n"
    inputs["workflow_loader"] = lambda ref: {"w.yml": reference} if ref == SHA_A else {}
    bundle = promotion.prepare_promotion(**inputs)
    assert f"{STAGED_NEW}{gate}" in bundle.required_paths
    assert f"{STAGED_NEW}{gate}" in bundle.whole_paths
    inputs["workflow_loader"] = lambda ref: {"w.yml": base_reference} if ref == SHA_C else {}
    bundle = promotion.prepare_promotion(**inputs)
    assert f"{STAGED_NEW}{gate}" in bundle.required_paths
    assert f"{STAGED_NEW}{gate}" not in bundle.whole_paths


ACTION_WORKFLOW = f"jobs:\n  a:\n    steps:\n      - uses: {THESEUS}/.github/actions/setup@{SHA_B}\n"
ACTION_TREE = {
    **SURFACE_NEW,
    ".github/actions/setup/action.yml": (
        b"runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/inner\n    - uses: actions/checkout@v4\n"
    ),
    ".github/actions/inner/action.yaml": b"runs:\n  using: composite\n  steps:\n    - run: echo\n      shell: bash\n",
}


def test_step_level_composite_actions_are_in_the_surface_and_required() -> None:
    surface = _surface(ACTION_TREE, {"w.yml": ACTION_WORKFLOW})
    assert {".github/actions/setup/action.yml", ".github/actions/inner/action.yaml"} <= surface
    old = {
        **ACTION_TREE,
        ".github/actions/inner/action.yaml": b"runs:\n  using: composite\n  steps:\n    - run: true\n      shell: bash\n",
    }
    required = _required(old, ACTION_TREE, {"w.yml": ACTION_WORKFLOW})
    assert f"{STAGED_NEW}.github/actions/inner/action.yaml" in required


@pytest.mark.parametrize(
    "uses",
    [
        f"{THESEUS}/.github/actions/missing@{SHA_B}",
        f"{THESEUS}/.github/actions/setup",
        f"{THESEUS}/../escape@{SHA_B}",
        f"https://github.com/{THESEUS}/x",
    ],
)
def test_unresolvable_theseus_step_reference_holds(uses: str) -> None:
    workflow = f"jobs:\n  a:\n    steps:\n      - uses: {uses}\n"
    with pytest.raises(promotion.PromotionError):
        _surface(ACTION_TREE, {"w.yml": workflow})


def test_missing_nested_action_holds() -> None:
    nested = {
        **ACTION_TREE,
        ".github/actions/setup/action.yml": b"runs:\n  using: composite\n  steps:\n    - uses: ./nowhere\n",
    }
    with pytest.raises(promotion.PromotionError, match="missing"):
        _surface(nested, {"w.yml": ACTION_WORKFLOW})


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            b"runs:\n  using: node20\n  main: dist/index.js\n",
            "node actions in the Theseus repository are not supported",
        ),
        (
            b"runs:\n  using: node16\n  main: ../lib/evil.js\n",
            "node actions in the Theseus repository are not supported",
        ),
        (b"runs:\n  using: docker\n  image: Dockerfile\n", "unsupported runs.using 'docker'"),
        (b"runs:\n  using: docker\n  image: docker://alpine:3\n", "unsupported runs.using 'docker'"),
        (b"runs:\n  using: bash\n", "unsupported runs.using 'bash'"),
    ],
)
def test_theseus_action_that_is_not_composite_holds(body: bytes, message: str) -> None:
    tree = {**ACTION_TREE, ".github/actions/setup/action.yml": body}
    with pytest.raises(promotion.PromotionError, match=message):
        _surface(tree, {"w.yml": ACTION_WORKFLOW})


def test_step_references_to_other_repositories_are_ignored() -> None:
    workflow = (
        "jobs:\n  a:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: someone/else/.github/actions/x@v1\n"
        f"      - uses: {THESEUS}-extras/x@{SHA_B}\n"
        "      - uses: docker://alpine:3\n"
        "      - uses: ./.github/actions/consumer-local\n"
        "      - run: echo\n"
    )
    assert _surface(SURFACE_NEW, {"w.yml": workflow}) == _surface(SURFACE_NEW, {})


def test_unchanged_files_newly_executed_by_the_head_are_required_whole() -> None:
    action = ".github/actions/payload/action.yml"
    script = ".github/actions/payload/payload.py"
    unchanged = {
        action: b"runs:\n  using: composite\n  steps:\n    - run: python ${{ github.action_path }}/payload.py\n",
        script: b"print(1)\n",
    }
    inputs = _preparation_inputs(old_extra=unchanged, new_extra=unchanged)
    head_reference = f"jobs:\n  a:\n    steps:\n      - uses: {THESEUS}/.github/actions/payload@{SHA_B}\n"
    inputs["workflow_loader"] = lambda ref: {"w.yml": head_reference} if ref == SHA_A else {}
    bundle = promotion.prepare_promotion(**inputs)
    assert {f"{STAGED_NEW}{action}", f"{STAGED_NEW}{script}"} <= bundle.required_paths
    assert {f"{STAGED_NEW}{action}", f"{STAGED_NEW}{script}"} <= bundle.whole_paths
    assert not any(item.path.endswith("payload.py") for item in bundle.changed_files)


def test_action_surface_includes_every_file_in_the_action_directory() -> None:
    tree = {
        **ACTION_TREE,
        ".github/actions/setup/payload.py": b"print(1)\n",
        ".github/actions/setup/nested/more.sh": b"true\n",
    }
    surface = _surface(tree, {"w.yml": ACTION_WORKFLOW})
    assert {".github/actions/setup/payload.py", ".github/actions/setup/nested/more.sh"} <= surface


@pytest.mark.parametrize(
    "run",
    [
        "python ${{ github.action_path }}/../lib/evil.py",
        "python ${{ github.workspace }}/.github/actions/other/x.py",
        "python $GITHUB_WORKSPACE/x.py",
        "cd .. && ./run.sh",
        "python /home/runner/work/_actions/joeharris76/theseus/abc/x.py",
    ],
)
def test_composite_run_reaching_outside_the_action_directory_holds(run: str) -> None:
    tree = {
        **ACTION_TREE,
        ".github/actions/setup/action.yml": (
            f"runs:\n  using: composite\n  steps:\n    - run: {run!r}\n      shell: bash\n"
        ).encode(),
    }
    with pytest.raises(promotion.PromotionError, match="outside its own directory"):
        _surface(tree, {"w.yml": ACTION_WORKFLOW})


def test_inline_and_action_path_runs_are_accepted() -> None:
    body = (
        b"runs:\n  using: composite\n  steps:\n"
        b"    - run: echo hello && python ${{ github.action_path }}/payload.py\n      shell: bash\n"
    )
    tree = {**ACTION_TREE, ".github/actions/setup/action.yml": body, ".github/actions/setup/payload.py": b"x\n"}
    assert ".github/actions/setup/payload.py" in _surface(tree, {"w.yml": ACTION_WORKFLOW})


@pytest.mark.parametrize("ref", ["main", "v1", "refs/heads/main", "refs/tags/v1", SHA_C, SHA_B.upper(), SHA_B[:7]])
@pytest.mark.parametrize("level", ["step", "job"])
def test_theseus_reference_not_pinned_to_the_promoted_commit_holds(level: str, ref: str) -> None:
    if level == "step":
        workflow = f"jobs:\n  a:\n    steps:\n      - uses: {THESEUS}/.github/actions/setup@{ref}\n"
    else:
        workflow = f"jobs:\n  a:\n    uses: {THESEUS}/.github/workflows/review.yml@{ref}\n"
    with pytest.raises(promotion.PromotionError, match="not the promoted commit"):
        _surface(ACTION_TREE | SURFACE_NEW, {"w.yml": workflow})


def test_theseus_reference_matches_owner_and_repository_case_insensitively() -> None:
    owner, name = THESEUS.split("/")
    spelled = f"{owner.upper()}/{name.title()}"
    workflow = (
        f"jobs:\n  a:\n    uses: {spelled}/.github/workflows/review.yml@{SHA_B}\n"
        f"  b:\n    steps:\n      - uses: {spelled}/.github/actions/setup@{SHA_B}\n"
    )
    surface = _surface(ACTION_TREE | SURFACE_NEW, {"w.yml": workflow})
    assert {".github/workflows/review.yml", ".github/actions/setup/action.yml"} <= surface


def test_git_suffix_on_the_repository_name_is_the_same_repository() -> None:
    workflow = (
        f"jobs:\n  a:\n    uses: {THESEUS}.git/.github/workflows/review.yml@{SHA_B}\n"
        f"  b:\n    steps:\n      - uses: {THESEUS}.git/.github/actions/setup@{SHA_B}\n"
    )
    surface = _surface(ACTION_TREE | SURFACE_NEW, {"w.yml": workflow})
    assert {".github/workflows/review.yml", ".github/actions/setup/action.yml"} <= surface
    with pytest.raises(promotion.PromotionError, match="not the promoted commit"):
        _surface(ACTION_TREE | SURFACE_NEW, {"w.yml": workflow.replace(SHA_B, "main")})


@pytest.mark.parametrize(
    "uses",
    [
        f"https://github.com/{THESEUS}/.github/workflows/review.yml@{SHA_B}",
        f"{THESEUS}/not-a-workflow.yml@{SHA_B}",
        f"{THESEUS}/.github/workflows/review.yml",
        f"{THESEUS}@{SHA_B}",
    ],
)
def test_job_level_theseus_reference_that_cannot_be_resolved_holds(uses: str) -> None:
    with pytest.raises(promotion.PromotionError, match="cannot be resolved|without a version"):
        _surface(SURFACE_NEW, {"w.yml": f"jobs:\n  a:\n    uses: {uses}\n"})


def test_nested_local_job_reference_that_is_not_a_workflow_holds() -> None:
    tree = {**SURFACE_NEW, ".github/workflows/review.yml": b"jobs:\n  a:\n    uses: ./scripts/run.yml\n"}
    with pytest.raises(promotion.PromotionError, match="cannot be resolved"):
        _surface(tree)
