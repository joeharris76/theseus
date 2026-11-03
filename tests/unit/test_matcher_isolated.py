from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from theseus_reviewer import matcher_cli, soundness_paths

pytestmark = [pytest.mark.unit, pytest.mark.fast]

MATCHER = Path(soundness_paths.__file__)
BETA = Path(__file__).resolve().parents[1] / "fixtures" / "consumer" / "beta" / ".github"
ALPHA = Path(__file__).resolve().parents[1] / "fixtures" / "consumer" / "alpha" / ".github"
CLEAN_ENV = {"PATH": os.environ.get("PATH", "")}


def _isolated(script: Path, args: list[str], stdin: str = "", cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=CLEAN_ENV,
        check=False,
    )


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ("src/engine/join.py\n", "true"),
        ("src/checks/a/b.py\n", "true"),
        ("policy/gate.cfg\n", "true"),
        ("data/x/y/z.golden\n", "true"),
        ("src/pkg/compare.py\n", "true"),
        ("README.md\ndocs/guide.md\n", "false"),
        ("benchbox/core/equivalence/checker.py\n", "false"),
    ],
)
def test_matcher_runs_under_python_dash_i_dash_s_against_a_non_benchbox_manifest(paths, expected, tmp_path):
    result = _isolated(MATCHER, ["--manifest", str(BETA / "soundness-paths.txt"), "--stdin"], paths, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_matcher_has_no_default_manifest(tmp_path):
    result = _isolated(MATCHER, ["--stdin"], "src/engine/join.py\n", cwd=tmp_path)
    assert result.returncode != 0
    assert "--manifest is required" in result.stderr
    assert "benchbox" not in result.stderr.lower()


def test_unreadable_manifest_fails_instead_of_matching_nothing(tmp_path):
    result = _isolated(MATCHER, ["--manifest", str(tmp_path / "absent.txt"), "--stdin"], "a.py\n", cwd=tmp_path)
    assert result.returncode != 0
    assert "soundness_paths:" in result.stderr


def test_malformed_manifest_fails(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("prefix\tno-trailing-slash\n", encoding="utf-8")
    result = _isolated(MATCHER, ["--manifest", str(bad), "--stdin"], "a.py\n", cwd=tmp_path)
    assert result.returncode != 0
    assert "prefix must end with '/'" in result.stderr


def test_import_performs_no_file_access_beyond_the_module_itself():
    script = (
        "import argparse, collections.abc, dataclasses, fnmatch, importlib.util, pathlib, re, sys\n"
        "sys.dont_write_bytecode = True\n"
        "events = []\n"
        "sys.addaudithook(lambda name, args: events.append((name, str(args[0]))) "
        "if name in ('open', 'os.listdir', 'os.scandir', 'os.stat', 'os.rename', 'os.remove') else None)\n"
        f"path = {str(MATCHER)!r}\n"
        "spec = importlib.util.spec_from_file_location('matcher_under_test', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['matcher_under_test'] = module\n"
        "spec.loader.exec_module(module)\n"
        "extra = [event for event in events if event[1] != path and not event[1].endswith('.pyc')]\n"
        "print(repr(extra))\n"
        "print(sorted(name for name in dir(module) if name.isupper()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script], capture_output=True, text=True, env=CLEAN_ENV, check=False
    )
    assert result.returncode == 0, result.stderr
    extra, constants = result.stdout.splitlines()
    assert extra == "[]"
    assert constants == "['CLI_DESCRIPTION', 'VALID_KINDS']"


def test_module_imports_only_the_standard_library():
    import ast

    tree = ast.parse(MATCHER.read_text(encoding="utf-8"))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert imported <= set(sys.stdlib_module_names), imported
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree))


def test_head_manifest_cannot_narrow_what_the_base_manifest_requires(tmp_path):
    base = tmp_path / "base.txt"
    head = tmp_path / "head.txt"
    base.write_text("prefix\tsrc/engine/\nfile\tpolicy/gate.cfg\n", encoding="utf-8")
    head.write_text("prefix\tsrc/other/\n", encoding="utf-8")
    both = ["--manifest", str(base), "--manifest", str(head), "--stdin"]
    assert _isolated(MATCHER, both, "src/engine/join.py\n").stdout.strip() == "true"
    assert _isolated(MATCHER, both, "policy/gate.cfg\n").stdout.strip() == "true"
    assert _isolated(MATCHER, ["--manifest", str(head), "--stdin"], "src/engine/join.py\n").stdout.strip() == "false"
    assert _isolated(MATCHER, both, "src/other/x.py\n").stdout.strip() == "true"
    assert _isolated(MATCHER, both, "docs/a.md\n").stdout.strip() == "false"


def test_one_unreadable_manifest_of_a_pair_fails_closed(tmp_path):
    base = tmp_path / "base.txt"
    base.write_text("prefix\tsrc/engine/\n", encoding="utf-8")
    result = _isolated(
        MATCHER, ["--manifest", str(base), "--manifest", str(tmp_path / "gone.txt"), "--stdin"], "docs/a.md\n"
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_output_formats(tmp_path):
    args = ["--manifest", str(BETA / "soundness-paths.txt"), "--stdin"]
    assert (
        _isolated(MATCHER, [*args, "--format", "github-output"], "src/engine/a.py\n").stdout == "soundness_path=true\n"
    )
    assert _isolated(MATCHER, [*args, "--format", "shell"], "docs/a.md\n").stdout == "SOUNDNESS_PATH=false\n"


def test_vendored_copy_behaves_identically_and_is_byte_checked(tmp_path):
    vendored = tmp_path / "vendored_matcher.py"
    shutil.copyfile(MATCHER, vendored)
    paths = "src/engine/join.py\nREADME.md\n"
    args = ["--manifest", str(ALPHA / "soundness-paths.txt"), "--stdin"]
    assert _isolated(vendored, args, paths).stdout == _isolated(MATCHER, args, paths).stdout
    assert matcher_cli.drift(vendored) == []
    digest = _isolated(vendored, ["--print-source-sha256"]).stdout.strip()
    assert digest == matcher_cli.packaged_sha256()
    assert matcher_cli.drift(vendored, digest) == []


def test_vendored_drift_is_detected(tmp_path):
    vendored = tmp_path / "vendored_matcher.py"
    vendored.write_bytes(MATCHER.read_bytes() + b"\n# local edit\n")
    problems = matcher_cli.drift(vendored)
    assert any("differs from the installed theseus matcher" in problem for problem in problems)
    pinned = matcher_cli.drift(vendored, matcher_cli.packaged_sha256())
    assert any("pins" in problem for problem in pinned)
    assert matcher_cli.drift(tmp_path / "missing.py")[0].startswith("cannot read the vendored matcher")


def test_cli_verify_exit_codes(tmp_path):
    vendored = tmp_path / "vendored_matcher.py"
    shutil.copyfile(MATCHER, vendored)
    ok = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "matcher", "verify", "--copy", str(vendored)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    vendored.write_bytes(vendored.read_bytes() + b"#\n")
    drifted = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "matcher", "verify", "--copy", str(vendored)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert drifted.returncode == 1
    assert "differs from the installed theseus matcher" in drifted.stderr


def test_matcher_semantics_match_the_alpha_consumer_manifest():
    matcher = soundness_paths.Matcher(soundness_paths.load_rules(ALPHA / "soundness-paths.txt"))
    assert matcher.is_soundness_path("benchbox/core/validation.py")
    assert matcher.is_soundness_path("benchbox/core/equivalence/deep/checker.py")
    assert matcher.is_soundness_path("results-data/bundles/a/b/x.override.json")
    assert matcher.is_soundness_path("scripts\\heavy_tier_needed.py")
    assert not matcher.is_soundness_path("")
    assert not matcher.is_soundness_path("README.md")
