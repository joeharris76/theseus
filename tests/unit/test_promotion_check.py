from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from bootstrap_support import make_wheel, sha
from tests.conftest import ALPHA_POLICY

from theseus_reviewer import promotion, promotion_check
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

REPOSITORY = "joeharris76/theseus"
PRIMARY = f"https://github.com/{REPOSITORY}/releases/download/v1/fakepkg-1.0.0-py3-none-any.whl"
RETAINED = "https://files.pythonhosted.org/packages/ab/cd/fakepkg-1.0.0-py3-none-any.whl"
COMMIT = "c" * 40


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    (source / "src" / "fakepkg").mkdir(parents=True)
    (source / "src" / "fakepkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    wheel = make_wheel("fakepkg", "1.0.0", {"fakepkg/__init__.py": "VALUE = 1\n"})
    data = yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))
    data["promotion"] = {"repository": REPOSITORY, "bootstrap_reviewers": ["joeharris76"], "package": "fakepkg"}
    policy = tmp_path / "policy.yml"
    policy.write_text(yaml.safe_dump(data), encoding="utf-8")
    lock = {
        "schema": 1,
        "package": "fakepkg",
        "version": "1.0.0",
        "repository": REPOSITORY,
        "source_commit": COMMIT,
        "source_sha256": promotion.tree_sha256(promotion.load_tree_from_dir(source)),
        "delta_sha256": "2" * 64,
        "artifact": {"sha256": sha(wheel), "urls": [PRIMARY, RETAINED]},
        "dependencies": [],
        "workflow_sha": COMMIT,
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(promotion, "default_artifact_loader", lambda: lambda url: wheel)
    return argparse.Namespace(policy=str(policy), lock=str(lock_path), source=str(source)), lock, lock_path


def test_a_lock_that_matches_its_source_and_artifact_passes(case, capsys):
    args, lock, _ = case
    assert promotion_check.command_check(args) == 0
    assert f"source {COMMIT}" in capsys.readouterr().out


def test_a_lock_with_the_wrong_source_hash_is_refused(case, capsys):
    args, lock, lock_path = case
    lock["source_sha256"] = "9" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    assert promotion_check.command_check(args) == 1
    assert "lock pins source_sha256" in capsys.readouterr().err


def test_an_edited_source_tree_is_refused_even_when_the_wheel_would_match(case, capsys):
    args, _, _ = case
    (Path(args.source) / "src" / "fakepkg" / "extra.py").write_text("X = 2\n", encoding="utf-8")
    assert promotion_check.command_check(args) == 1
    err = capsys.readouterr().err
    assert "refused" in err and ("source_sha256" in err or "file list" in err)


def test_a_lock_for_another_repository_is_refused(case, capsys):
    args, lock, lock_path = case
    lock["repository"] = "someone/else"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    assert promotion_check.command_check(args) == 1
    assert "does not match consumer policy" in capsys.readouterr().err


def test_a_lock_for_the_policy_repository_in_another_case_passes(case, capsys):
    args, lock, lock_path = case
    lock["repository"] = REPOSITORY.upper()
    lock["artifact"]["urls"] = [url.replace(REPOSITORY, REPOSITORY.upper()) for url in lock["artifact"]["urls"]]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    assert promotion_check.command_check(args) == 0


def test_a_policy_without_a_promotion_section_is_refused(case, tmp_path, capsys):
    args, _, _ = case
    data = yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))
    bare = tmp_path / "bare.yml"
    bare.write_text(yaml.safe_dump(data), encoding="utf-8")
    args.policy = str(bare)
    assert promotion_check.command_check(args) == 1
    assert "no promotion section" in capsys.readouterr().err
