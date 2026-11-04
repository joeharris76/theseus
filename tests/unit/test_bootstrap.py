from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from bootstrap_support import COMMIT, Net, build_world, commit_lock, git, lock_for, make_wheel, sha

from theseus_reviewer import bootstrap

pytestmark = [pytest.mark.unit, pytest.mark.fast]


@pytest.fixture
def world():
    return build_world()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "consumer"


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def _lock(world, repo: Path, mutate=None) -> bootstrap.Lock:
    data = copy.deepcopy(world.lock)
    if mutate:
        mutate(data)
    commit = commit_lock(repo, data)
    return bootstrap.read_lock(repo, commit)


def _run(executable: Path, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        [str(executable)], capture_output=True, text=True, cwd=cwd, env={**os.environ, **(env or {})}, check=False
    )


def test_install_verifies_runs_isolated_and_reuses_the_warm_install(world, repo, cache):
    lock = _lock(world, repo)
    first = bootstrap.install(lock, cache, world.net)
    assert first.reconstructed is False
    result = _run(first.executable)
    assert result.stdout.strip() == "fakepkg ok 7 1 1"
    calls = len(world.net.calls)
    second = bootstrap.install(lock, cache, world.net)
    assert (second.executable, second.key, second.reconstructed) == (first.executable, first.key, False)
    assert len(world.net.calls) == calls


def test_install_key_covers_artifact_dependencies_interpreter_and_platform(world, repo):
    lock = _lock(world, repo)
    base = bootstrap.install_key(lock, {"v": 1}, "plat-a")
    assert bootstrap.install_key(lock, {"v": 2}, "plat-a") != base
    assert bootstrap.install_key(lock, {"v": 1}, "plat-b") != base
    other_dep = copy.deepcopy(world.lock)
    other_dep["dependencies"][0]["sha256"] = "9" * 64
    assert bootstrap.install_key(_lock_obj(other_dep), {"v": 1}, "plat-a") != base
    other_artifact = copy.deepcopy(world.lock)
    other_artifact["artifact"]["sha256"] = "8" * 64
    assert bootstrap.install_key(_lock_obj(other_artifact), {"v": 1}, "plat-a") != base


def _lock_obj(data: dict) -> bootstrap.Lock:
    return bootstrap.parse_lock(json.dumps(data).encode("utf-8"))


def test_wrong_artifact_hash_refuses_before_any_install(world, repo, cache):
    lock = _lock(world, repo, lambda data: data["artifact"].update(sha256="0" * 64))
    with pytest.raises(bootstrap.BootstrapError, match="expected 0{64}"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_missing_dependency_hash_refuses(world, repo, cache):
    lock = _lock(world, repo, lambda data: data.update(dependencies=[]))
    with pytest.raises(bootstrap.BootstrapError, match="fakedep, which the lock has no hash for"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_malformed_dependency_hash_refuses_at_parse(world, repo):
    def mutate(data):
        data["dependencies"][0]["sha256"] = "zz"

    with pytest.raises(bootstrap.BootstrapError, match="missing or invalid sha256 for fakedep"):
        _lock(world, repo, mutate)


def test_wrong_dependency_hash_refuses(world, repo, cache):
    lock = _lock(world, repo, lambda data: data["dependencies"][0].update(sha256="3" * 64))
    with pytest.raises(bootstrap.BootstrapError, match="fakedep from .* expected 3{64}"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_dependency_outside_the_declared_range_refuses(world, repo, cache):
    other = build_world(main_requires=("fakedep>=3",))
    lock = _lock(other, repo)
    with pytest.raises(bootstrap.BootstrapError, match="does not satisfy"):
        bootstrap.install(lock, cache, other.net)


def test_tampered_source_with_a_matching_artifact_hash_refuses_on_record(world, repo, cache):
    tampered = make_wheel(
        "fakepkg",
        "1.0.0",
        {"fakepkg/__init__.py": "", "fakepkg/main.py": "raise SystemExit('evil')\n"},
        requires=("fakedep>=2,<3",),
        scripts={"fake": "fakepkg.main:main"},
        skip_record_for=("fakepkg/main.py",),
    )
    world.net.blobs[world.primary] = world.net.blobs[world.retained] = tampered
    lock = _lock(world, repo, lambda data: data["artifact"].update(sha256=sha(tampered)))
    with pytest.raises(bootstrap.BootstrapError, match="RECORD does not list exactly"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_tampered_bytes_that_change_the_hash_refuse(world, repo, cache):
    tampered = world.main_wheel + b"x"
    world.net.blobs[world.primary] = tampered
    lock = _lock(world, repo)
    with pytest.raises(bootstrap.BootstrapError, match="has sha256"):
        bootstrap.install(lock, cache, world.net)


def test_absent_artifact_refuses(world, repo, cache):
    world.net.blobs.pop(world.primary)
    world.net.blobs.pop(world.retained)
    lock = _lock(world, repo)
    with pytest.raises(bootstrap.BootstrapError, match="no reachable byte source"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_blocked_primary_selects_the_identical_retained_bytes(world, repo, cache):
    world.net.blocked.add(world.primary)
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    assert world.net.calls[:2] == [world.primary, world.retained]
    assert _run(installed.executable).stdout.strip() == "fakepkg ok 7 1 1"


def test_blocked_primary_with_a_wrong_retained_hash_refuses(world, repo, cache):
    world.net.blocked.add(world.primary)
    world.net.blobs[world.retained] = world.main_wheel + b"tampered"
    lock = _lock(world, repo)
    with pytest.raises(bootstrap.BootstrapError, match="has sha256"):
        bootstrap.install(lock, cache, world.net)
    assert not (cache / "installs").exists()


def test_wrong_primary_hash_does_not_fall_back_to_the_retained_copy(world, repo, cache):
    world.net.blobs[world.primary] = b"not the wheel"
    lock = _lock(world, repo)
    with pytest.raises(bootstrap.BootstrapError, match="has sha256"):
        bootstrap.install(lock, cache, world.net)
    assert world.net.calls == [world.primary]


def test_verify_all_sources_requires_every_source_to_match(world, repo, cache):
    lock = _lock(world, repo)
    world.net.blobs[world.retained] = b"drifted recovery copy"
    with pytest.raises(bootstrap.BootstrapError, match="has sha256"):
        bootstrap.install(lock, cache, world.net, verify_all=True)
    world.net.blobs[world.retained] = world.main_wheel
    world.net.blocked.add(world.retained)
    with pytest.raises(bootstrap.BootstrapError, match="unreachable byte sources"):
        bootstrap.install(lock, cache / "second", world.net, verify_all=True)
    world.net.blocked.clear()
    assert bootstrap.install(lock, cache / "third", world.net, verify_all=True).executable.is_file()


def test_cached_artifact_that_no_longer_matches_is_discarded_and_refetched(world, repo, cache):
    lock = _lock(world, repo)
    bootstrap.install(lock, cache, world.net)
    retained = bootstrap._retained(cache.resolve(), lock.artifact_sha256)
    retained.write_bytes(b"poisoned warm artifact")
    installed = bootstrap.install(lock, cache, world.net)
    assert sha(retained.read_bytes()) == lock.artifact_sha256
    assert _run(installed.executable).returncode == 0


def test_warm_install_recorded_for_another_identity_refuses_until_reinstalled(world, repo, cache):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    manifest = installed.root / bootstrap.MANIFEST
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    recorded["artifact_sha256"] = "7" * 64
    manifest.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="warm install .* recorded for a different artifact"):
        bootstrap.install(lock, cache, world.net)
    rebuilt = bootstrap.install(lock, cache, world.net, reinstall=True)
    assert rebuilt.reconstructed is True
    assert _run(rebuilt.executable).returncode == 0


def test_corrupt_installed_executable_is_rebuilt_from_verified_bytes(world, repo, cache):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    installed.executable.write_text("#!/bin/sh\necho owned\n", encoding="utf-8")
    again = bootstrap.install(lock, cache, world.net)
    assert again.reconstructed is True
    assert _run(again.executable).stdout.strip() == "fakepkg ok 7 1 1"
    assert bootstrap.verify_installed(lock, cache, world.net).key == again.key
    with pytest.raises(bootstrap.BootstrapError, match="no install exists"):
        bootstrap.verify_installed(lock, cache / "elsewhere", world.net)


@pytest.mark.parametrize("planted", ["site/evil.pth", "site/fakepkg/extra.py", "site/fakepkg/__pycache__/main.pyc"])
def test_unrecorded_files_in_the_install_force_reconstruction(world, repo, cache, planted):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    target = installed.root / "tree" / planted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import os; os._exit(99)\n", encoding="utf-8")
    again = bootstrap.install(lock, cache, world.net)
    assert again.reconstructed is True
    assert not target.exists()
    assert _run(again.executable).stdout.strip() == "fakepkg ok 7 1 1"


def test_modified_dependency_file_is_rebuilt(world, repo, cache):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    (installed.root / "tree" / "site" / "fakedep" / "__init__.py").write_text("VALUE = 666\n", encoding="utf-8")
    again = bootstrap.install(lock, cache, world.net)
    assert again.reconstructed is True
    assert _run(again.executable).stdout.strip() == "fakepkg ok 7 1 1"


def test_interrupted_install_leaves_no_usable_partial_state(world, repo, cache, monkeypatch):
    lock = _lock(world, repo)
    real = bootstrap.os.replace

    def interrupted(source, destination):
        if ".build-" in str(source) and str(destination).endswith(
            bootstrap.install_key(lock, bootstrap.interpreter_identity(), bootstrap.platform_identity())
        ):
            raise KeyboardInterrupt
        return real(source, destination)

    monkeypatch.setattr(bootstrap.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bootstrap.install(lock, cache, world.net)
    installs = cache.resolve() / "installs"
    assert [path for path in installs.iterdir() if path.name.startswith(".build-")] == []
    monkeypatch.setattr(bootstrap.os, "replace", real)
    key = bootstrap.install_key(lock, bootstrap.interpreter_identity(), bootstrap.platform_identity())
    (installs / key).mkdir()
    (installs / key / "tree").mkdir()
    (installs / key / "tree" / "bin").mkdir()
    (installs / key / "tree" / "bin" / "fake").write_text("#!/bin/sh\necho partial\n", encoding="utf-8")
    rebuilt = bootstrap.install(lock, cache, world.net)
    assert rebuilt.reconstructed is True
    assert _run(rebuilt.executable).stdout.strip() == "fakepkg ok 7 1 1"


def test_hostile_pr_local_modules_and_pythonpath_are_not_imported(world, repo, cache, tmp_path):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    hostile = tmp_path / "checkout"
    hostile.mkdir()
    marker = tmp_path / "owned"
    for module in ("fakedep.py", "fakepkg.py", "sitecustomize.py", "usercustomize.py"):
        (hostile / module).write_text(f"open({str(marker)!r}, 'w').write('x')\nVALUE = 1\n", encoding="utf-8")
    result = _run(
        installed.executable,
        cwd=hostile,
        env={"PYTHONPATH": str(hostile), "PYTHONSTARTUP": str(hostile / "sitecustomize.py")},
    )
    assert result.stdout.strip() == "fakepkg ok 7 1 1"
    assert not marker.exists()


def test_changed_trusted_lock_installs_beside_the_old_one(world, repo, cache):
    first = bootstrap.install(_lock(world, repo), cache, world.net)
    newer = build_world()
    newer.dep_wheel = make_wheel("fakedep", "2.4.0", {"fakedep/__init__.py": "VALUE = 8\n"})
    newer.net.add(newer.dep_primary, newer.dep_wheel)
    newer.lock = lock_for(newer)
    newer.lock["dependencies"][0]["version"] = "2.4.0"
    second = bootstrap.install(_lock(newer, repo), cache, newer.net)
    assert second.key != first.key
    assert first.executable.is_file() and second.executable.is_file()
    assert _run(second.executable).stdout.strip() == "fakepkg ok 8 1 1"
    assert _run(first.executable).stdout.strip() == "fakepkg ok 7 1 1"


@pytest.mark.parametrize("ref", ["latest", "HEAD", "main", "abc123", "A" * 40, ""])
def test_only_a_full_commit_sha_resolves_a_lock(world, repo, ref):
    commit_lock(repo, world.lock)
    with pytest.raises(bootstrap.BootstrapError, match="full 40-hex sha"):
        bootstrap.read_lock(repo, ref)


def test_trusted_ref_refresh_failure_refuses_without_falling_back(world, repo, cache):
    first = bootstrap.install(_lock(world, repo), cache, world.net)
    with pytest.raises(bootstrap.BootstrapError, match="cannot read .github/theseus-lock.json at trusted commit"):
        bootstrap.read_lock(repo, "b" * 40)
    assert first.executable.is_file()


def test_commit_without_a_lock_refuses(world, repo):
    commit_lock(repo, world.lock)
    (repo / "other.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "other.txt")
    git(repo, "rm", "-q", ".github/theseus-lock.json")
    git(repo, "commit", "-q", "-m", "drop lock")
    with pytest.raises(bootstrap.BootstrapError, match="cannot read"):
        bootstrap.read_lock(repo, git(repo, "rev-parse", "HEAD"))


def test_the_lock_is_read_from_the_commit_not_the_working_tree(world, repo):
    commit = commit_lock(repo, world.lock)
    (repo / ".github" / "theseus-lock.json").write_text("{}", encoding="utf-8")
    assert bootstrap.read_lock(repo, commit).package == "fakepkg"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d.update(unexpected=1), "unexpected keys"),
        (lambda d: d["artifact"].update(urls=[d["artifact"]["urls"][0]]), "at least 2"),
        (lambda d: d["artifact"].update(urls=["http://x/y.whl", "https://x/z.whl"]), "https urls"),
        (lambda d: d.update(workflow_sha="b" * 40), "workflow_sha"),
        (lambda d: d.update(source_commit="main"), "source_commit"),
        (lambda d: d["dependencies"].append(dict(d["dependencies"][0])), "duplicate dependencies"),
    ],
)
def test_malformed_locks_refuse(world, mutate, message):
    data = copy.deepcopy(world.lock)
    mutate(data)
    with pytest.raises(bootstrap.BootstrapError, match=message):
        _lock_obj(data)


def test_lock_that_the_gate_parser_rejects_is_rejected_here_too(world):
    from theseus_reviewer import promotion

    mutations = [
        lambda d: d.update(schema=2),
        lambda d: d["artifact"].update(urls=[d["artifact"]["urls"][0]]),
        lambda d: d.update(workflow_sha="b" * 40),
        lambda d: d["dependencies"][0].update(sha256="nope"),
    ]
    assert promotion.parse_lock(json.dumps(world.lock)).package == "fakepkg"
    assert _lock_obj(world.lock).package == "fakepkg"
    for mutate in mutations:
        data = copy.deepcopy(world.lock)
        mutate(data)
        with pytest.raises(promotion.PromotionError):
            promotion.parse_lock(json.dumps(data))
        with pytest.raises(bootstrap.BootstrapError):
            _lock_obj(data)


def test_incompatible_requires_python_refuses(repo, cache):
    other = build_world()
    other.main_wheel = make_wheel(
        "fakepkg",
        "1.0.0",
        {"fakepkg/__init__.py": ""},
        requires_python=">=99",
        scripts={"fake": "fakepkg.main:main"},
    )
    other.net.blobs[other.primary] = other.net.blobs[other.retained] = other.main_wheel
    other.lock = lock_for(other)
    with pytest.raises(bootstrap.BootstrapError, match=r"requires Python >=99"):
        bootstrap.install(_lock(other, repo), cache, other.net)


def test_wheel_for_another_platform_refuses(repo, cache):
    other = build_world()
    other.main_wheel = make_wheel(
        "fakepkg", "1.0.0", {"fakepkg/__init__.py": ""}, tag="cp27-cp27mu-win32", scripts={"fake": "fakepkg.main:main"}
    )
    other.net.blobs[other.primary] = other.net.blobs[other.retained] = other.main_wheel
    other.lock = lock_for(other)
    with pytest.raises(bootstrap.BootstrapError, match="no wheel tag compatible"):
        bootstrap.install(_lock(other, repo), cache, other.net)


def test_native_binaries_in_the_package_wheel_refuse(repo, cache):
    other = build_world()
    other.main_wheel = make_wheel(
        "fakepkg",
        "1.0.0",
        {"fakepkg/__init__.py": ""},
        scripts={"fake": "fakepkg.main:main"},
        extra={"fakepkg/native.so": b"\x7fELF"},
    )
    other.net.blobs[other.primary] = other.net.blobs[other.retained] = other.main_wheel
    other.lock = lock_for(other)
    with pytest.raises(bootstrap.BootstrapError, match="native binary"):
        bootstrap.install(_lock(other, repo), cache, other.net)


def test_pth_hooks_in_a_dependency_refuse(repo, cache):
    other = build_world()
    other.dep_wheel = make_wheel(
        "fakedep", "2.3.0", {"fakedep/__init__.py": "VALUE = 7\n"}, extra={"evil.pth": b"import os\n"}
    )
    other.net.blobs[other.dep_primary] = other.dep_wheel
    other.lock = lock_for(other)
    with pytest.raises(bootstrap.BootstrapError, match="forbidden hook"):
        bootstrap.install(_lock(other, repo), cache, other.net)


def test_environment_markers_skip_requirements_that_do_not_apply(repo, cache):
    other = build_world(
        main_requires=("fakedep>=2,<3", "winonly>=1; sys_platform == 'plan9'", "oldpython; python_version < '3.8'")
    )
    installed = bootstrap.install(_lock(other, repo), cache, other.net)
    assert _run(installed.executable).returncode == 0


def test_applicable_marker_without_a_lock_entry_refuses(repo, cache):
    other = build_world(main_requires=("fakedep>=2,<3", "extra-thing; python_version >= '3.8'"))
    with pytest.raises(bootstrap.BootstrapError, match="extra-thing, which the lock has no hash for"):
        bootstrap.install(_lock(other, repo), cache, other.net)


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("python_version >= '3.8' and sys_platform != 'plan9'", True),
        ("python_version < '3.8' or os_name == 'nope'", False),
        ("(python_version >= '3.8') and not_a_var == 'x'", None),
        ("extra == 'tests'", False),
        ("platform_system in 'Darwin Linux Windows'", True),
    ],
)
def test_marker_evaluation(marker, expected):
    environment = bootstrap.marker_environment()
    if expected is None:
        with pytest.raises(bootstrap.BootstrapError, match="unsupported marker variable"):
            bootstrap.evaluate_marker(marker, environment)
    else:
        assert bootstrap.evaluate_marker(marker, environment) is expected


@pytest.mark.parametrize(
    ("version", "specifier", "expected"),
    [
        ("2.3.0", ">=2,<3", True),
        ("3.0.0", ">=2,<3", False),
        ("1.4.2", "~=1.4", True),
        ("2.0", "~=1.4", False),
        ("1.2.9", "==1.2.*", True),
        ("1.3.0", "!=1.3.*", False),
        ("1.0", "==1.0.0", True),
    ],
)
def test_specifier_evaluation(version, specifier, expected):
    assert bootstrap.satisfies(version, specifier) is expected


def test_cli_install_and_verify_from_a_trusted_commit(world, repo, cache, tmp_path):
    commit = commit_lock(repo, world.lock)
    mirror = _serve(world.net, tmp_path)
    env = {**os.environ, "ORACLE_PROMOTION_MIRROR": mirror, "THESEUS_INSTALL_CACHE": str(cache)}
    base = [sys.executable, "-m", "theseus_reviewer", "bootstrap"]
    flags = ["--repo", str(repo), "--commit", commit]
    installed = subprocess.run([*base, "install", *flags], capture_output=True, text=True, env=env, check=False)
    assert installed.returncode == 0, installed.stderr
    executable = Path(installed.stdout.strip())
    assert _run(executable).stdout.strip() == "fakepkg ok 7 1 1"
    verified = subprocess.run([*base, "verify", *flags], capture_output=True, text=True, env=env, check=False)
    assert verified.returncode == 0, verified.stderr
    refused = subprocess.run(
        [*base, "install", "--repo", str(repo), "--commit", "latest"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert refused.returncode == 1
    assert "refused" in refused.stderr


def test_cli_refuses_when_the_mirror_serves_wrong_bytes(world, repo, cache, tmp_path):
    commit = commit_lock(repo, world.lock)
    poisoned = Net()
    poisoned.blobs = {url: b"poison" for url in world.net.blobs}
    env = {**os.environ, "ORACLE_PROMOTION_MIRROR": _serve(poisoned, tmp_path), "THESEUS_INSTALL_CACHE": str(cache)}
    result = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "bootstrap", "install", "--repo", str(repo), "--commit", commit],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert "has sha256" in result.stderr
    assert not (cache / "installs").exists()


def test_the_installer_runs_standalone_without_the_package(world, repo, cache, tmp_path):
    commit = commit_lock(repo, world.lock)
    module = Path(bootstrap.__file__)
    env = {
        "PATH": os.environ.get("PATH", ""),
        **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {}),
        "ORACLE_PROMOTION_MIRROR": _serve(world.net, tmp_path),
        "THESEUS_INSTALL_CACHE": str(cache),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(module), "install", "--repo", str(repo), "--commit", commit],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _run(Path(result.stdout.strip())).stdout.strip() == "fakepkg ok 7 1 1"
    assert COMMIT not in result.stdout


def _serve(net: Net, tmp_path: Path) -> str:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlsplit

    by_path = {f"/_assets/{urlsplit(url).hostname}{urlsplit(url).path}": raw for url, raw in net.blobs.items()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = by_path.get(self.path)
            if raw is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pytest.importorskip("threading")
    _SERVERS.append(server)
    return f"http://127.0.0.1:{server.server_address[1]}"


_SERVERS: list = []


@pytest.fixture(autouse=True)
def _stop_servers():
    yield
    while _SERVERS:
        server = _SERVERS.pop()
        server.shutdown()
        server.server_close()


def test_a_lock_text_edit_that_keeps_the_same_artifacts_reuses_the_warm_install(world, repo, cache):
    first = bootstrap.install(_lock(world, repo), cache, world.net)
    extra_mirror = "https://github.com/acme/theseus/releases/download/v1/mirror-copy.whl"
    world.net.add(extra_mirror, world.main_wheel)

    def edit(data):
        data["artifact"]["urls"].append(extra_mirror)

    second = bootstrap.install(_lock(world, repo, edit), cache, world.net)
    assert (second.key, second.reconstructed) == (first.key, False)
    assert _run(second.executable).stdout.strip() == "fakepkg ok 7 1 1"


def test_cli_install_accepts_reinstall_and_discards_a_mismatched_warm_install(world, repo, cache, tmp_path):
    commit = commit_lock(repo, world.lock)
    env = {**os.environ, "ORACLE_PROMOTION_MIRROR": _serve(world.net, tmp_path), "THESEUS_INSTALL_CACHE": str(cache)}
    flags = ["--repo", str(repo), "--commit", commit]
    base = [sys.executable, "-m", "theseus_reviewer", "bootstrap"]
    first = subprocess.run([*base, "install", *flags], capture_output=True, text=True, env=env, check=False)
    manifest = Path(first.stdout.strip()).parents[2] / bootstrap.MANIFEST
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    recorded["platform"] = "elsewhere"
    manifest.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")
    refused = subprocess.run([*base, "install", *flags], capture_output=True, text=True, env=env, check=False)
    assert refused.returncode == 1 and "--reinstall" in refused.stderr
    rebuilt = subprocess.run(
        [*base, "install", *flags, "--reinstall"], capture_output=True, text=True, env=env, check=False
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert _run(Path(rebuilt.stdout.strip())).stdout.strip() == "fakepkg ok 7 1 1"


def test_concurrent_cold_installs_of_one_key_all_succeed_and_leave_a_valid_install(world, repo, cache):
    import threading

    lock = _lock(world, repo)
    results: list[object] = []

    def install() -> None:
        try:
            results.append(bootstrap.install(lock, cache, world.net))
        except BaseException as exc:
            results.append(exc)

    threads = [threading.Thread(target=install) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(isinstance(item, bootstrap.Installed) for item in results), results
    assert len({item.root for item in results}) == 1
    assert _run(results[0].executable).stdout.strip() == "fakepkg ok 7 1 1"
    assert [path for path in (cache.resolve() / "installs").iterdir() if path.name.startswith(".build-")] == []


def test_filesystem_failures_surface_as_a_refusal(world, repo, cache, monkeypatch):
    lock = _lock(world, repo)

    def broken(*args, **kwargs):
        raise PermissionError("read-only cache")

    monkeypatch.setattr(bootstrap, "_write_tree", broken)
    with pytest.raises(bootstrap.BootstrapError, match="cannot install into .*read-only cache"):
        bootstrap.install(lock, cache, world.net)


def test_every_requirers_specifier_is_checked_against_the_locked_version(repo, cache):
    other = build_world(main_requires=("fakedep>=2,<3", "middle"))
    middle = make_wheel("middle", "1.0.0", {"middle/__init__.py": ""}, requires=("fakedep>=3",))
    middle_url = other.net.add("https://files.pythonhosted.org/packages/mi/dd/middle-1.0.0-py3-none-any.whl", middle)
    other.lock["dependencies"].append(
        {"name": "middle", "version": "1.0.0", "sha256": sha(middle), "urls": [middle_url]}
    )
    with pytest.raises(bootstrap.BootstrapError, match=r"locked fakedep 2\.3\.0 does not satisfy middle's requirement"):
        bootstrap.install(_lock(other, repo), cache, other.net)
    compatible = make_wheel("middle", "1.0.0", {"middle/__init__.py": ""}, requires=("fakedep>=2.1",))
    other.net.blobs[middle_url] = compatible
    other.lock["dependencies"][-1]["sha256"] = sha(compatible)
    assert bootstrap.install(_lock(other, repo), cache / "ok", other.net).executable.is_file()


class _FakeMsvcrt:
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, busy: int = 0) -> None:
        self.busy = busy
        self.calls: list[tuple[int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        assert nbytes == 1
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError("locked")
        self.calls.append((mode, nbytes))


def test_key_lock_uses_msvcrt_on_windows_and_retries_while_contended(monkeypatch, cache):
    fake = _FakeMsvcrt(busy=3)
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    with bootstrap._key_lock(cache, "k"):
        assert fake.calls == [(fake.LK_NBLCK, 1)]
    assert fake.calls == [(fake.LK_NBLCK, 1), (fake.LK_UNLCK, 1)]
    assert (cache / "installs" / ".lock-k").is_file()


def test_key_lock_windows_gives_up_after_bounded_retries(monkeypatch, cache):
    fake = _FakeMsvcrt(busy=10**6)
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    with pytest.raises(OSError, match="locked"), bootstrap._key_lock(cache, "k"):
        pytest.fail("lock must not be granted")
    assert 10**6 - fake.busy == bootstrap.WINDOWS_LOCK_ATTEMPTS
    assert bootstrap.WINDOWS_LOCK_ATTEMPTS * bootstrap.WINDOWS_LOCK_INTERVAL <= 600


def test_windows_lock_sleeps_between_nonblocking_attempts(monkeypatch, cache):
    sleeps: list[float] = []
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(busy=2))
    monkeypatch.setattr(bootstrap.time, "sleep", sleeps.append)
    with bootstrap._key_lock(cache, "k"):
        pass
    assert sleeps == [bootstrap.WINDOWS_LOCK_INTERVAL] * 2


def test_key_lock_windows_failure_becomes_bootstrap_error(world, repo, cache, monkeypatch):
    lock = _lock(world, repo)
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(busy=10**6))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    with pytest.raises(bootstrap.BootstrapError, match="cannot install into .*locked"):
        bootstrap.install(lock, cache, world.net)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_key_lock_uses_flock_on_posix(monkeypatch, cache):
    import fcntl

    calls: list[int] = []
    monkeypatch.setattr(bootstrap, "_windows", lambda: False)
    monkeypatch.setattr(fcntl, "flock", lambda handle, op: calls.append(op))
    with bootstrap._key_lock(cache, "k"):
        assert calls == [fcntl.LOCK_EX]
    assert calls == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_windows_install_adds_a_cmd_wrapper_and_returns_it(world, repo, cache, monkeypatch):
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt())
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    assert installed.executable.name == "fake.cmd"
    wrapper = installed.executable.read_bytes().decode("utf-8")
    assert wrapper == f'@"{bootstrap.interpreter_identity()["executable"]}" -IBS "%~dp0fake" %*\r\n'
    assert (installed.executable.parent / "fake").is_file()
    assert bootstrap.verify_installed(lock, cache, world.net).executable == installed.executable


def test_artifact_lock_is_per_artifact_and_not_held_while_fetching(world, repo, cache, monkeypatch):
    names: list[str] = []
    state = {"held": 0}
    real = bootstrap._file_lock

    @bootstrap.contextlib.contextmanager
    def tracking(path):
        names.append(path.name)
        with real(path):
            state["held"] += 1
            try:
                yield
            finally:
                state["held"] -= 1

    def fetch(url):
        assert state["held"] == 0
        return world.net(url)

    monkeypatch.setattr(bootstrap, "_file_lock", tracking)
    bootstrap.install(_lock(world, repo), cache, fetch)
    assert f".lock-{world.lock['artifact']['sha256']}" in names
    assert ".lock" not in names


def test_print_argv_emits_the_isolated_launch_argv(world, repo, cache, tmp_path):
    commit = commit_lock(repo, world.lock)
    env = {
        **os.environ,
        "ORACLE_PROMOTION_MIRROR": _serve(world.net, tmp_path),
        "THESEUS_INSTALL_CACHE": str(cache),
    }
    result = subprocess.run(
        [sys.executable, "-m", "theseus_reviewer", "bootstrap", "install", "--repo", str(repo), "--commit", commit]
        + ["--print-argv"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    interpreter, flags, script = json.loads(result.stdout)
    assert flags == "-IBS"
    ran = subprocess.run([interpreter, flags, script], capture_output=True, text=True, check=False)
    assert ran.stdout.strip() == "fakepkg ok 7 1 1"


def test_warm_artifact_read_takes_no_lock(world, repo, cache, monkeypatch):
    lock = _lock(world, repo)
    bootstrap.install(lock, cache, world.net)
    monkeypatch.setattr(bootstrap, "_file_lock", lambda path: pytest.fail("no lock for a warm read"))
    bootstrap.build_plan(lock, cache, world.net)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_verify_on_a_read_only_cache_succeeds_and_unreadable_refuses_cleanly(world, repo, cache):
    lock = _lock(world, repo)
    installed = bootstrap.install(lock, cache, world.net)
    for path in [cache, *cache.rglob("*")]:
        if path.is_dir():
            path.chmod(0o555)
    try:
        assert bootstrap.verify_installed(lock, cache, world.net).executable == installed.executable
    finally:
        for path in [cache, *cache.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)


def test_verify_turns_os_errors_into_a_refusal(world, repo, cache, monkeypatch):
    lock = _lock(world, repo)
    bootstrap.install(lock, cache, world.net)

    def boom(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(bootstrap, "build_plan", boom)
    with pytest.raises(bootstrap.BootstrapError, match="cannot verify the install .*read-only"):
        bootstrap.verify_installed(lock, cache, world.net)


def test_windows_allows_spaces_in_the_interpreter_path(world, repo, cache, monkeypatch):
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt())
    identity = {**bootstrap.interpreter_identity(), "executable": "C:\\Program Files\\Python\\python.exe"}
    monkeypatch.setattr(bootstrap, "interpreter_identity", lambda: identity)
    installed = bootstrap.install(_lock(world, repo), cache, world.net)
    assert installed.executable.read_bytes().startswith(b'@"C:\\Program Files\\Python\\python.exe" -IBS')


@pytest.mark.parametrize("path", ["C:\\100%\\python.exe", "C:\\Jörg\\python.exe"])
def test_windows_refuses_percent_and_non_ascii_interpreter_paths(world, repo, cache, monkeypatch, path):
    monkeypatch.setattr(bootstrap, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt())
    identity = {**bootstrap.interpreter_identity(), "executable": path}
    monkeypatch.setattr(bootstrap, "interpreter_identity", lambda: identity)
    with pytest.raises(bootstrap.BootstrapError, match="cmd wrapper cannot carry"):
        bootstrap.install(_lock(world, repo), cache, world.net)


def test_posix_still_refuses_whitespace_in_the_interpreter_path(world, repo, cache, monkeypatch):
    monkeypatch.setattr(bootstrap, "_windows", lambda: False)
    identity = {**bootstrap.interpreter_identity(), "executable": "/opt/my python/bin/python"}
    monkeypatch.setattr(bootstrap, "interpreter_identity", lambda: identity)
    with pytest.raises(bootstrap.BootstrapError, match="unsuitable for a launcher shebang"):
        bootstrap.install(_lock(world, repo), cache, world.net)
