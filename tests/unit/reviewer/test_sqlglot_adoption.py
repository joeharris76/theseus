from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer import cli, git_blobs, github, local, promotion, protocol, sqlglot_adoption
from theseus_reviewer.classifier import ChangedFile, soundness_scope
from theseus_reviewer.matchers import Matchers, load_matchers
from theseus_reviewer.policy import Policy, SqlglotAdoptionRules
from theseus_reviewer.soundness_paths import Matcher, Rule

pytestmark = [pytest.mark.unit, pytest.mark.fast]

NAMESPACE = "benchbox._vendor.sqlglot"
NAMESPACE_DIR = "benchbox/_vendor/sqlglot/"
DISTRIBUTION = "benchbox-sqlglot"
FORK = "BenchBox-dev/benchbox-sqlglot"
PIN_PATH = "benchbox/_vendor/sqlglot.json"
RULES = SqlglotAdoptionRules(
    pin_path=PIN_PATH,
    fork_repository=FORK,
    upstream_repository="tobymao/sqlglot",
    distribution=DISTRIBUTION,
    namespace=NAMESPACE,
)
OLD_VERSION = "30.29.0.1"
NEW_VERSION = "30.30.0.1"
BASE_FORK = "1" * 40
HEAD_FORK = "2" * 40
OLD_UPSTREAM = "3" * 40
NEW_UPSTREAM = "4" * 40
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
EPOCH = 1793713913


def _archive_name(version: str) -> str:
    return f"benchbox_sqlglot-{version}-py3-none-any.whl.zip"


def _archive_path(version: str) -> str:
    return f"benchbox/_vendor/{_archive_name(version)}"


def _manifest(version: str, upstream_sha: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "distribution": DISTRIBUTION,
        "import_package": NAMESPACE,
        "version": version,
        "upstream": {
            "repository": "https://github.com/tobymao/sqlglot",
            "tag": "v" + version.rsplit(".", 1)[0],
            "sha": upstream_sha,
        },
    }


def fork_tree(
    *, version: str, upstream_sha: str, value: int, extra: Mapping[str, bytes] | None = None
) -> dict[str, bytes]:
    tree = {
        "sqlglot/__init__.py": b"from sqlglot import exp\n",
        "sqlglot/exp.py": f"from sqlglot.helper import seq\nVALUE = {value}\n".encode(),
        "sqlglot/helper.py": b"def seq():\n    return 1\n",
        "sqlglot/py.typed": b"",
        "LICENSE": b"MIT License\n",
        "downstream/patches.json": json.dumps(_manifest(version, upstream_sha)).encode(),
        "downstream/relocate.py": b"PACKAGE = 'benchbox._vendor.sqlglot'\n",
        "tests/test_exp.py": b"def test_value():\n    assert True\n",
        "docs/notes.md": b"notes\n",
        "CLAUDE.md": promotion.SYMLINK_MARKER + b"AGENTS.md\n",
    }
    tree.update(extra or {})
    return tree


def runtime_payload(tree: Mapping[str, bytes], version: str) -> dict[str, bytes]:
    payload = {
        path.removeprefix("sqlglot/"): raw.replace(b"from sqlglot", f"from {NAMESPACE}".encode())
        for path, raw in tree.items()
        if path.startswith("sqlglot/") and path.endswith(".py")
    }
    payload["py.typed"] = tree["sqlglot/py.typed"]
    payload["LICENSE"] = tree["LICENSE"]
    payload["_version.py"] = (
        f"__version__ = {version!r}\n__version_tuple__ = {tuple(map(int, version.split('.')))!r}\n".encode()
    )
    return payload


def _record(entries: Mapping[str, bytes], dist_info: str) -> bytes:
    lines = []
    for name, content in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        lines.append(f"{name},sha256={digest},{len(content)}")
    lines.append(f"{dist_info}/RECORD,,")
    return ("\n".join(lines) + "\n").encode()


def build_wheel(
    tree: Mapping[str, bytes],
    version: str,
    fork_sha: str,
    upstream_sha: str,
    *,
    mutate: Callable[[dict[str, bytes]], None] | None = None,
    epoch: int = EPOCH,
    stale_record: bool = False,
) -> bytes:
    payload = runtime_payload(tree, version)
    provenance = {
        "schema": 1,
        "distribution": DISTRIBUTION,
        "repository": f"https://github.com/{FORK}",
        "namespace": NAMESPACE,
        "version": version,
        "upstream_sha": upstream_sha,
        "fork_sha": fork_sha,
        "source_date_epoch": epoch,
        "dirty": False,
        "manifest": json.loads(tree["downstream/patches.json"]),
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()},
    }
    dist_info = f"benchbox_sqlglot-{version}.dist-info"
    entries: dict[str, bytes] = {"benchbox/__init__.py": b"", "benchbox/_vendor/__init__.py": b""}
    entries.update({f"{NAMESPACE_DIR}{name}": content for name, content in payload.items()})
    entries[f"{NAMESPACE_DIR}benchbox_provenance.json"] = json.dumps(provenance, indent=2).encode()
    entries[f"{dist_info}/licenses/LICENSE"] = tree["LICENSE"]
    entries[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.4\nName: {DISTRIBUTION}\nVersion: {version}\n".encode()
    entries[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries[f"{dist_info}/top_level.txt"] = b"benchbox\n"
    if mutate is not None:
        mutate(entries)
    record = _record(entries, dist_info)
    if stale_record:
        record = record.replace(b"sha256=", b"sha256=A", 1)
    entries[f"{dist_info}/RECORD"] = record
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    return buffer.getvalue()


def pin_text(version: str, fork_sha: str, upstream_sha: str, wheel: bytes, **overrides: Any) -> str:
    pin: dict[str, Any] = {
        "schema": 1,
        "distribution": DISTRIBUTION,
        "namespace": NAMESPACE,
        "repository": f"https://github.com/{FORK}",
        "version": version,
        "fork_sha": fork_sha,
        "upstream_sha": upstream_sha,
        "wheel": f"benchbox_sqlglot-{version}-py3-none-any.whl",
        "archive": _archive_name(version),
        "sha256": hashlib.sha256(wheel).hexdigest(),
    }
    pin.update(overrides)
    return json.dumps(pin, indent=2) + "\n"


def edit_provenance(**changes: Any) -> Callable[[dict[str, bytes]], None]:
    def mutate(entries: dict[str, bytes]) -> None:
        name = f"{NAMESPACE_DIR}benchbox_provenance.json"
        data = json.loads(entries[name])
        data.update(changes)
        entries[name] = json.dumps(data, indent=2).encode()

    return mutate


def _label_sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


class Fixture:
    def __init__(
        self,
        *,
        head_extra: Mapping[str, bytes] | None = None,
        head_mutate: Callable[[dict[str, bytes]], None] | None = None,
        head_version: str = NEW_VERSION,
        head_fork_sha: str = HEAD_FORK,
        head_upstream: str = NEW_UPSTREAM,
        head_value: int = 2,
        head_epoch: int = EPOCH,
        base_fork_sha: str = BASE_FORK,
        base_upstream: str = OLD_UPSTREAM,
        base_version: str = OLD_VERSION,
        base_value: int = 1,
        stale_record: bool = False,
    ) -> None:
        self.base_tree = fork_tree(version=base_version, upstream_sha=base_upstream, value=base_value)
        self.head_tree = fork_tree(version=head_version, upstream_sha=head_upstream, value=head_value, extra=head_extra)
        self.base_wheel = build_wheel(self.base_tree, base_version, base_fork_sha, base_upstream)
        self.head_wheel = build_wheel(
            self.head_tree,
            head_version,
            head_fork_sha,
            head_upstream,
            mutate=head_mutate,
            epoch=head_epoch,
            stale_record=stale_record,
        )
        self.base_pin = pin_text(base_version, base_fork_sha, base_upstream, self.base_wheel)
        self.head_pin = pin_text(head_version, head_fork_sha, head_upstream, self.head_wheel)
        self.blobs: dict[tuple[str, str], bytes] = {
            (BASE_SHA, PIN_PATH): self.base_pin.encode(),
            (BASE_SHA, _archive_path(base_version)): self.base_wheel,
            (HEAD_SHA, PIN_PATH): self.head_pin.encode(),
            (HEAD_SHA, _archive_path(head_version)): self.head_wheel,
        }
        self.trees = {base_fork_sha: self.base_tree, head_fork_sha: self.head_tree}
        self.head_version = head_version
        self.ancestry: list[tuple[str, str]] = []
        self.sources: list[tuple[str, str]] = []
        self.modes: dict[tuple[str, str], str] = {}
        self.main_shas = {base_fork_sha, head_fork_sha}
        self.tags: dict[str, dict[str, str]] = {
            f"v{version.rsplit('.', 1)[0]}": {"type": "commit", "sha": upstream}
            for version, upstream in ((base_version, base_upstream), (head_version, head_upstream))
        }
        self.annotated: dict[str, dict[str, str]] = {}
        self.requested: list[str] = []

    def read_blob(self, ref: str, path: str) -> bytes | None:
        return self.blobs.get((ref, path))

    def identity_json(self, path: str) -> Any:
        self.requested.append(path)
        if path == f"repos/{FORK}":
            return {"default_branch": "main"}
        if path.startswith(f"repos/{FORK}/compare/"):
            sha = path.rsplit("/", 1)[1].split("...")[0]
            assert path.endswith("...main")
            return {"status": "ahead" if sha in self.main_shas else "diverged"}
        if path.startswith("repos/tobymao/sqlglot/git/ref/tags/"):
            tag = path.rsplit("/", 1)[1]
            if tag not in self.tags:
                raise github.GitHubError("gh: Not Found (HTTP 404)")
            return {"object": self.tags[tag]}
        if path.startswith("repos/tobymao/sqlglot/git/tags/"):
            return {"object": self.annotated[path.rsplit("/", 1)[1]]}
        raise AssertionError(f"unexpected identity lookup {path}")

    def verify_identity(self, pin: sqlglot_adoption.Pin) -> None:
        sqlglot_adoption.github_identity_validator(RULES, self.identity_json)(pin)

    def git_json(self, path: str) -> Any:
        self.requested.append(path)
        prefix = "repos/BenchBox-dev/BenchBox/git/"
        assert path.startswith(prefix), path
        kind, _, name = path.removeprefix(prefix).partition("/")
        if kind == "commits":
            return {"tree": {"sha": _label_sha(f"{name}:")}}
        assert kind == "trees"
        ref, _, directory = self.tree_labels[name]
        entries: dict[str, tuple[str, str, str]] = {}
        for (blob_ref, blob_path), content in self.blobs.items():
            if blob_ref != ref or not blob_path.startswith(directory):
                continue
            rest = blob_path[len(directory) :]
            head, slash, _tail = rest.partition("/")
            if slash:
                entries[head] = ("040000", "tree", _label_sha(f"{ref}:{directory}{head}/"))
            else:
                mode = self.modes.get((blob_ref, blob_path), "100644")
                entries[head] = (mode, "blob", _blob_sha(content))
        return {
            "truncated": False,
            "tree": [
                {"path": entry, "mode": mode, "type": kind, "sha": sha} for entry, (mode, kind, sha) in entries.items()
            ],
        }

    def git_bytes(self, endpoint: str, *, accept: str | None = None) -> bytes:
        assert accept == "application/vnd.github.raw"
        sha = endpoint.rsplit("/", 1)[1]
        self.requested.append(endpoint)
        for content in self.blobs.values():
            if _blob_sha(content) == sha:
                return content
        raise github.GitHubError("gh: Not Found (HTTP 404)")

    @property
    def tree_labels(self) -> dict[str, tuple[str, str, str]]:
        labels: dict[str, tuple[str, str, str]] = {}
        for ref, path in self.blobs:
            labels[_label_sha(f"{ref}:")] = (ref, ":", "")
            parts = path.split("/")[:-1]
            for index in range(len(parts)):
                directory = "/".join(parts[: index + 1]) + "/"
                labels[_label_sha(f"{ref}:{directory}")] = (ref, ":", directory)
        return labels

    def load_source(self, repo: str, sha: str, prefixes: tuple[str, ...] | None) -> dict[str, bytes]:
        self.sources.append((repo, sha))
        if repo != FORK or sha not in self.trees or prefixes is not None:
            raise promotion.PromotionError(f"unknown fork source {repo}@{sha[:12]}")
        return dict(self.trees[sha])

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self.ancestry.append((ancestor, descendant))
        return True

    def prepare(self, **overrides: Any) -> promotion.PromotionBundle | None:
        arguments: dict[str, Any] = {
            "rules": RULES,
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "read_blob": self.read_blob,
            "source_loader": self.load_source,
            "is_ancestor": self.is_ancestor,
            "verify_identity": self.verify_identity,
            "pr_paths": [PIN_PATH, _archive_path(self.head_version)],
        }
        arguments.update(overrides)
        return sqlglot_adoption.prepare_adoption(**arguments)

    def set_head_pin(self, text: str) -> None:
        self.blobs[(HEAD_SHA, PIN_PATH)] = text.encode()


def _many_modules(count: int) -> dict[str, bytes]:
    return {f"sqlglot/mod{index}.py": f"VALUE = {index}\n".encode() for index in range(count)}


def test_a_substantial_fork_delta_under_a_tiny_pin_edit_becomes_the_reviewed_source() -> None:
    fixture = Fixture(head_extra={**_many_modules(25), "sqlglot/exp2.py": b"from sqlglot import exp\nVALUE = 9\n"})
    bundle = fixture.prepare()
    assert bundle is not None
    assert bundle.kind == sqlglot_adoption.KIND
    staged = {item.path: item for item in bundle.changed_files}
    assert f"{promotion.STAGED_PROMOTION_DIR}/new/sqlglot/mod24.py" in staged
    assert f"{promotion.STAGED_PROMOTION_DIR}/new/sqlglot/exp.py" in staged
    assert staged[f"{promotion.STAGED_PROMOTION_DIR}/new/sqlglot/exp.py"].status == "modified"
    assert staged[f"{promotion.STAGED_PROMOTION_DIR}/new/sqlglot/mod3.py"].status == "added"
    assert f"{promotion.STAGED_PROMOTION_DIR}/new/CLAUDE.md" not in staged
    assert len(bundle.changed_files) >= 27
    assert "+++ b/sqlglot/mod5.py" in bundle.diff_text
    assert bundle.old_tree == fixture.base_tree and bundle.new_tree == fixture.head_tree
    assert fixture.sources == [(FORK, BASE_FORK), (FORK, HEAD_FORK)]


def test_the_evidence_record_binds_both_pins_both_runtimes_and_the_actual_delta() -> None:
    fixture = Fixture()
    bundle = fixture.prepare()
    assert bundle is not None
    base = sqlglot_adoption.parse_pin(fixture.base_pin, RULES)
    head = sqlglot_adoption.parse_pin(fixture.head_pin, RULES)
    record = bundle.record
    assert promotion.valid_promotion_record(record.to_json())
    assert record.head_sha == HEAD_SHA
    assert record.old_source_sha256 == promotion.tree_sha256(fixture.base_tree)
    assert record.new_source_sha256 == promotion.tree_sha256(fixture.head_tree)
    assert record.old_artifact_sha256 == base.sha256
    assert record.new_artifact_sha256 == head.sha256
    assert record.delta_sha256 == promotion.compute_delta_sha256(fixture.base_tree, fixture.head_tree)
    assert record.lock_sha256 == sqlglot_adoption.pin_binding_digest(base, head)
    assert not record.bootstrap


def test_ancestry_of_the_upstream_and_the_base_fork_is_required() -> None:
    fixture = Fixture()
    fixture.prepare()
    assert fixture.ancestry == [(NEW_UPSTREAM, HEAD_FORK), (BASE_FORK, HEAD_FORK)]
    for refused in (NEW_UPSTREAM, BASE_FORK):
        with pytest.raises(promotion.PromotionError, match="not an ancestor"):
            fixture.prepare(is_ancestor=lambda ancestor, _descendant, refused=refused: ancestor != refused)


def test_the_binding_digest_changes_when_the_base_or_the_head_pin_moves() -> None:
    first = Fixture()
    moved_base = Fixture(base_fork_sha="5" * 40, base_value=7)
    moved_head = Fixture(head_fork_sha="6" * 40, head_value=8)
    digests = set()
    for fixture in (first, moved_base, moved_head):
        bundle = fixture.prepare()
        assert bundle is not None
        digests.add(bundle.record.lock_sha256)
    assert len(digests) == 3


def test_a_pin_that_did_not_change_needs_no_adoption_bundle() -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel
    assert fixture.prepare(pr_paths=[_archive_path(OLD_VERSION)]) is None


def test_an_archive_changed_without_its_pin_is_refused_on_digest() -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel + b"tampered"
    with pytest.raises(promotion.PromotionError, match="has sha256"):
        fixture.prepare(pr_paths=[_archive_path(OLD_VERSION)])


def test_the_head_archive_is_read_as_the_exact_head_blob_and_a_different_blob_is_refused() -> None:
    fixture = Fixture()
    other = build_wheel(fixture.head_tree, NEW_VERSION, HEAD_FORK, NEW_UPSTREAM, epoch=EPOCH + 1)
    fixture.blobs[(HEAD_SHA, _archive_path(NEW_VERSION))] = other
    with pytest.raises(promotion.PromotionError, match="but the pin records"):
        fixture.prepare()
    del fixture.blobs[(HEAD_SHA, _archive_path(NEW_VERSION))]
    with pytest.raises(promotion.PromotionError, match="is not present at"):
        fixture.prepare()


def test_a_missing_head_pin_or_base_pin_is_refused() -> None:
    fixture = Fixture()
    del fixture.blobs[(HEAD_SHA, PIN_PATH)]
    with pytest.raises(promotion.PromotionError, match="cannot be removed"):
        fixture.prepare()
    fixture = Fixture()
    del fixture.blobs[(BASE_SHA, PIN_PATH)]
    with pytest.raises(promotion.PromotionError, match="needs a pin on the base"):
        fixture.prepare()


def test_a_pull_request_cannot_plant_the_staged_promotion_directory() -> None:
    with pytest.raises(promotion.PromotionError, match="cannot touch"):
        Fixture().prepare(pr_paths=[PIN_PATH, f"{promotion.STAGED_PROMOTION_DIR}/new/x.py"])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"distribution": "other-dist"}, "distribution does not match"),
        ({"namespace": "other.ns"}, "namespace does not match"),
        ({"repository": "https://github.com/evil/benchbox-sqlglot"}, "not the policy fork repository"),
        ({"version": "30.30.0"}, "version must be"),
        ({"version": "30.30.0.0"}, "version must be"),
        ({"fork_sha": "2" * 39}, "fork_sha must be a full 40-hex"),
        ({"fork_sha": "A" * 40}, "fork_sha must be a full 40-hex"),
        ({"upstream_sha": "v30.30.0"}, "upstream_sha must be a full 40-hex"),
        ({"sha256": "f" * 63}, "sha256 must be 64-char"),
        ({"sha256": "G" * 64}, "sha256 must be 64-char"),
        ({"wheel": "benchbox_sqlglot-9.9.9.9-py3-none-any.whl"}, "wheel name does not match"),
        ({"archive": "../escape.whl.zip"}, "archive is not the wheel name plus .zip"),
        ({"archive": "sub/benchbox_sqlglot-30.30.0.1-py3-none-any.whl.zip"}, "archive is not the wheel name"),
        ({"schema": 2}, "schema must be 1"),
        ({"schema": True}, "schema must be 1"),
        ({"fork_sha": 5}, "fork_sha must be a string"),
        ({"extra": "x"}, "missing or unexpected keys"),
    ],
)
def test_invalid_head_pin_fields_are_refused(overrides: dict[str, Any], message: str) -> None:
    fixture = Fixture()
    pin = json.loads(fixture.head_pin)
    pin.update(overrides)
    fixture.set_head_pin(json.dumps(pin))
    with pytest.raises(promotion.PromotionError, match=message):
        fixture.prepare()


def test_a_head_pin_with_a_missing_key_or_bad_json_is_refused() -> None:
    fixture = Fixture()
    pin = json.loads(fixture.head_pin)
    del pin["sha256"]
    fixture.set_head_pin(json.dumps(pin))
    with pytest.raises(promotion.PromotionError, match="missing or unexpected keys"):
        fixture.prepare()
    fixture.set_head_pin("{not json")
    with pytest.raises(promotion.PromotionError, match="not valid JSON"):
        fixture.prepare()


def test_an_unknown_fork_source_is_refused_not_trusted() -> None:
    fixture = Fixture()
    del fixture.trees[HEAD_FORK]
    with pytest.raises(promotion.PromotionError, match="unknown fork source"):
        fixture.prepare()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}exp.py", b"VALUE = 3\n"),
        lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}helper.py", b"def seq():\n    return 2\n"),
        lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}_version.py", b"__version__ = '0'\n"),
        lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}py.typed", b"x"),
        lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}LICENSE", b"changed\n"),
    ],
)
def test_a_runtime_file_that_differs_from_the_relocated_source_is_nonapproval(
    mutate: Callable[[dict[str, bytes]], None],
) -> None:
    fixture = Fixture(head_mutate=mutate)
    with pytest.raises(promotion.PromotionError, match="differ"):
        fixture.prepare()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}sitecustomize.py", b"import os\n"),
            "absent from the fork",
        ),
        (lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}extra.json", b"{}"), "absent from the fork"),
        (lambda entries: entries.__setitem__("other_pkg/__init__.py", b""), "absent from the fork"),
        (lambda entries: entries.pop(f"{NAMESPACE_DIR}helper.py"), "missing runtime files"),
        (lambda entries: entries.pop("benchbox/_vendor/__init__.py"), "missing runtime files"),
        (lambda entries: entries.__setitem__("benchbox/__init__.py", b"import os\n"), "differ"),
    ],
)
def test_extra_or_missing_runtime_files_are_nonapproval(
    mutate: Callable[[dict[str, bytes]], None], message: str
) -> None:
    with pytest.raises(promotion.PromotionError, match=message):
        Fixture(head_mutate=mutate).prepare()


@pytest.mark.parametrize(
    "name",
    [
        f"{NAMESPACE_DIR}native.so",
        f"{NAMESPACE_DIR}native.pyd",
        f"{NAMESPACE_DIR}native.dylib",
        f"{NAMESPACE_DIR}native.dll",
        f"{NAMESPACE_DIR}cached.pyc",
        "benchbox_sqlglot_hook.pth",
        "benchbox_sqlglot-30.30.0.1.dist-info/entry_points.txt",
        "benchbox_sqlglot-30.30.0.1.data/scripts/run",
    ],
)
def test_executable_hook_and_data_entries_are_refused_before_anything_else(name: str) -> None:
    fixture = Fixture(head_mutate=lambda entries: entries.__setitem__(name, b"payload"))
    with pytest.raises(promotion.PromotionError, match="forbidden"):
        fixture.prepare()


def test_a_stale_wheel_record_is_refused() -> None:
    with pytest.raises(promotion.PromotionError, match="RECORD sha256 mismatch"):
        Fixture(stale_record=True).prepare()


def test_a_dist_info_file_outside_the_allowed_set_is_refused() -> None:
    fixture = Fixture(
        head_mutate=lambda entries: entries.__setitem__("benchbox_sqlglot-30.30.0.1.dist-info/extra.txt", b"x")
    )
    with pytest.raises(promotion.PromotionError, match="dist-info files differ"):
        fixture.prepare()


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("METADATA", b"Metadata-Version: 2.4\nName: other\nVersion: 30.30.0.1\n", "METADATA does not describe"),
        (
            "METADATA",
            b"Metadata-Version: 2.4\nName: benchbox-sqlglot\nVersion: 30.30.0.1\nRequires-Dist: evil\n",
            "METADATA does not describe",
        ),
        (
            "METADATA",
            b"Metadata-Version: 2.4\nName: benchbox-sqlglot\nVersion: 1.0.0.1\n",
            "METADATA does not describe",
        ),
        ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n", "not a pure-Python"),
        ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp311-cp311-linux_x86_64\n", "not a pure-Python"),
        ("top_level.txt", b"benchbox\nother\n", "top_level.txt"),
        ("licenses/LICENSE", b"Different\n", "license differs"),
    ],
)
def test_wheel_metadata_must_describe_the_pinned_pure_python_distribution(
    name: str, content: bytes, message: str
) -> None:
    fixture = Fixture(
        head_mutate=lambda entries: entries.__setitem__(f"benchbox_sqlglot-30.30.0.1.dist-info/{name}", content)
    )
    with pytest.raises(promotion.PromotionError, match=message):
        fixture.prepare()


@pytest.mark.parametrize(
    "changes",
    [
        {"fork_sha": "9" * 40},
        {"upstream_sha": "9" * 40},
        {"version": "30.30.0.2"},
        {"dirty": True},
        {"dirty": 0},
        {"namespace": "other.ns"},
        {"repository": "https://github.com/evil/benchbox-sqlglot"},
        {"distribution": "other-dist"},
        {"schema": 2},
        {"manifest": {"schema": 1}},
        {"files": {"__init__.py": "0" * 64}},
    ],
)
def test_wheel_provenance_must_agree_with_the_pin_and_the_staged_source(changes: dict[str, Any]) -> None:
    fixture = Fixture(head_mutate=edit_provenance(**changes))
    with pytest.raises(promotion.PromotionError, match="provenance"):
        fixture.prepare()


@pytest.mark.parametrize("epoch", [0, -5, True, "1793713913", 1.5])
def test_wheel_provenance_needs_an_integer_epoch(epoch: Any) -> None:
    fixture = Fixture(head_mutate=edit_provenance(source_date_epoch=epoch))
    with pytest.raises(promotion.PromotionError, match="epoch is invalid"):
        fixture.prepare()


def test_wheel_provenance_with_an_extra_or_missing_key_is_refused() -> None:
    with pytest.raises(promotion.PromotionError, match="unexpected keys"):
        Fixture(head_mutate=edit_provenance(extra=1)).prepare()

    def drop(entries: dict[str, bytes]) -> None:
        name = f"{NAMESPACE_DIR}benchbox_provenance.json"
        data = json.loads(entries[name])
        del data["dirty"]
        entries[name] = json.dumps(data).encode()

    with pytest.raises(promotion.PromotionError, match="unexpected keys"):
        Fixture(head_mutate=drop).prepare()


@pytest.mark.parametrize(
    "mutate_manifest",
    [
        lambda manifest: manifest.update(version="30.30.0.2"),
        lambda manifest: manifest["upstream"].update(sha="9" * 40),
        lambda manifest: manifest["upstream"].update(tag="v30.31.0"),
        lambda manifest: manifest["upstream"].update(repository="https://github.com/evil/sqlglot"),
        lambda manifest: manifest.update(import_package="other.ns"),
        lambda manifest: manifest.update(distribution="other-dist"),
    ],
)
def test_the_fork_manifest_must_describe_the_pinned_identity(mutate_manifest: Callable[[dict[str, Any]], Any]) -> None:
    tree = fork_tree(version=NEW_VERSION, upstream_sha=NEW_UPSTREAM, value=2)
    manifest = json.loads(tree["downstream/patches.json"])
    mutate_manifest(manifest)
    fixture = Fixture(head_extra={"downstream/patches.json": json.dumps(manifest).encode()})
    with pytest.raises(promotion.PromotionError, match="patches.json"):
        fixture.prepare()


def test_a_fork_source_that_the_trusted_relocation_refuses_is_nonapproval() -> None:
    fixture = Fixture(head_extra={"sqlglot/dynamic.py": b'import importlib\nMOD = f"sqlglot.{1}"\n'})
    with pytest.raises(promotion.PromotionError, match="Unreviewed dynamic import string"):
        fixture.prepare()
    fixture = Fixture(head_extra={"sqlglot/broken.py": b"def broken(:\n"})
    with pytest.raises(promotion.PromotionError, match="cannot be relocated"):
        fixture.prepare()


def test_a_fork_source_without_the_required_runtime_inputs_is_nonapproval() -> None:
    for missing in ("sqlglot/py.typed", "LICENSE", "downstream/patches.json", "sqlglot/__init__.py"):
        fixture = Fixture()
        tree = fixture.trees[HEAD_FORK]
        del tree[missing]
        with pytest.raises(promotion.PromotionError):
            fixture.prepare()


def test_a_symlinked_python_source_cannot_stand_in_for_a_runtime_module() -> None:
    fixture = Fixture(head_extra={"sqlglot/linked.py": promotion.SYMLINK_MARKER + b"exp.py\n"})
    with pytest.raises(promotion.PromotionError, match="cannot be relocated"):
        fixture.prepare()


def test_the_same_source_with_a_rebuilt_runtime_is_verified_not_waved_through() -> None:
    fixture = Fixture(
        head_version=OLD_VERSION,
        head_fork_sha=BASE_FORK,
        head_upstream=OLD_UPSTREAM,
        head_value=1,
        head_epoch=EPOCH + 5,
    )
    bundle = fixture.prepare()
    assert bundle is not None
    assert bundle.changed_files == ()
    assert bundle.record.delta_sha256 == promotion.compute_delta_sha256(fixture.base_tree, fixture.base_tree)
    assert bundle.record.old_artifact_sha256 != bundle.record.new_artifact_sha256
    assert fixture.ancestry == []


def test_the_same_source_with_a_changed_runtime_is_nonapproval() -> None:
    fixture = Fixture(
        head_version=OLD_VERSION,
        head_fork_sha=BASE_FORK,
        head_upstream=OLD_UPSTREAM,
        head_value=1,
        head_epoch=EPOCH + 5,
        head_mutate=lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}exp.py", b"VALUE = 99\n"),
    )
    with pytest.raises(promotion.PromotionError, match="differ"):
        fixture.prepare()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"head_version": "30.29.0.2"}, "unchanged"),
        ({"head_upstream": "7" * 40}, "unchanged"),
    ],
)
def test_the_same_fork_source_cannot_change_version_or_upstream(kwargs: dict[str, Any], message: str) -> None:
    fixture = Fixture(head_fork_sha=BASE_FORK, head_value=1, **kwargs)
    with pytest.raises(promotion.PromotionError, match=message):
        fixture.prepare()


def test_a_changed_fork_source_cannot_reuse_the_base_runtime_version() -> None:
    fixture = Fixture(head_version=OLD_VERSION)
    with pytest.raises(promotion.PromotionError, match="different runtime version"):
        fixture.prepare()


def test_an_ancestor_runtime_can_be_restored_with_full_source_review() -> None:
    fixture = Fixture(head_version="30.28.0.1")
    ancestry = {(NEW_UPSTREAM, HEAD_FORK), (HEAD_FORK, BASE_FORK)}
    bundle = fixture.prepare(is_ancestor=lambda ancestor, descendant: (ancestor, descendant) in ancestry)
    assert bundle is not None
    assert bundle.old_tree == fixture.base_tree
    assert bundle.new_tree == fixture.head_tree
    assert "30.28.0.1" in bundle.diff_text
    assert bundle.record.new_artifact_sha256 == json.loads(fixture.head_pin)["sha256"]


def test_a_lower_version_on_an_unrelated_fork_source_is_refused() -> None:
    fixture = Fixture(head_version="30.28.0.1")
    with pytest.raises(promotion.PromotionError, match="rollback must restore an ancestor"):
        fixture.prepare(is_ancestor=lambda ancestor, descendant: ancestor == NEW_UPSTREAM)


def test_a_fork_archive_beyond_the_file_bound_fails_closed_instead_of_truncating() -> None:
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for index in range(promotion.MAX_TREE_FILES + 1):
            info = tarfile.TarInfo(f"sqlglot/mod{index}.py")
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
    with pytest.raises(promotion.PromotionError, match="maximum file count"):
        promotion.extract_tar_tree(buffer.getvalue(), symlinks=True)


def test_touched_matches_the_pin_and_runtime_archives_only() -> None:
    assert sqlglot_adoption.touched(RULES, [PIN_PATH])
    assert sqlglot_adoption.touched(RULES, [_archive_path(NEW_VERSION)])
    assert sqlglot_adoption.touched(RULES, ["benchbox/_vendor/old.whl"])
    assert not sqlglot_adoption.touched(RULES, ["benchbox/_vendor/__init__.py", "benchbox/_vendor/yaml/x.zip"])
    assert not sqlglot_adoption.touched(RULES, ["docs/benchbox/_vendor/sqlglot.json"])
    assert not sqlglot_adoption.touched(None, [PIN_PATH])
    assert sqlglot_adoption.touched_paths(RULES, [PIN_PATH, "README.md"]) == [PIN_PATH]


def test_runtime_relevant_paths_cover_the_source_the_wheel_is_built_from() -> None:
    for path in ("sqlglot/expressions/core.py", "downstream/relocate.py", "LICENSE", "pyproject.toml", ".github/x.yml"):
        assert sqlglot_adoption.runtime_relevant(path)
    for path in ("docs/notes.md", "tests/test_exp.py", "posts/blog.md"):
        assert not sqlglot_adoption.runtime_relevant(path)


def test_the_staged_bundle_carries_the_source_the_reviewer_must_read(tmp_path: Path) -> None:
    fixture = Fixture(head_extra=_many_modules(3))
    bundle = fixture.prepare()
    assert bundle is not None
    root = promotion.write_materialized_bundle(bundle, tmp_path)
    assert promotion.load_tree_from_dir(root / "old") == fixture.base_tree
    assert promotion.load_tree_from_dir(root / "new") == fixture.head_tree
    assert (root / promotion.UPSTREAM_DIFF_NAME).read_text(encoding="utf-8") == bundle.diff_text
    assert (root / "new" / "CLAUDE.md").read_bytes().startswith(promotion.SYMLINK_MARKER)


def test_symlinks_are_recorded_as_link_text_only_when_the_loader_asks(tmp_path: Path) -> None:
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("CLAUDE.md")
        info.type = tarfile.SYMTYPE
        info.linkname = "AGENTS.md"
        archive.addfile(info)
        content = b"agents\n"
        regular = tarfile.TarInfo("AGENTS.md")
        regular.size = len(content)
        archive.addfile(regular, io.BytesIO(content))
    raw = buffer.getvalue()
    with pytest.raises(promotion.PromotionError, match="non-regular"):
        promotion.extract_tar_tree(raw)
    tree = promotion.extract_tar_tree(raw, symlinks=True)
    assert tree == {"CLAUDE.md": promotion.SYMLINK_MARKER + b"AGENTS.md\n", "AGENTS.md": b"agents\n"}
    source = tmp_path / "tree"
    source.mkdir()
    (source / "AGENTS.md").write_bytes(b"agents\n")
    (source / "CLAUDE.md").symlink_to("AGENTS.md")
    (source / "linked").symlink_to("elsewhere")
    with pytest.raises(promotion.PromotionError, match="not allowed"):
        promotion.load_tree_from_dir(source)
    assert promotion.load_tree_from_dir(source, symlinks=True) == {
        "AGENTS.md": b"agents\n",
        "CLAUDE.md": promotion.SYMLINK_MARKER + b"AGENTS.md\n",
        "linked": promotion.SYMLINK_MARKER + b"elsewhere\n",
    }


def _matchers(policy: Policy, alpha_root: Path) -> Matchers:
    return load_matchers(policy.consumer, alpha_root)


def _narrow_matchers() -> Matchers:
    return Matchers(Matcher((Rule("file", ".github/soundness-paths.txt"),)), None)


def _exempting_policy(policy: Policy) -> Policy:
    classifier = replace(policy.classifier, exempt_paths=(PIN_PATH, "benchbox/_vendor/*.zip"))
    return replace(policy, classifier=classifier, sqlglot_adoption=RULES)


def _pull(changed: int) -> dict[str, Any]:
    return {"changed_files": changed, "head": {"repo": {"full_name": "BenchBox-dev/BenchBox"}}}


def _pr_files(version: str = NEW_VERSION) -> list[ChangedFile]:
    return [
        ChangedFile.from_api({"filename": PIN_PATH, "additions": 3, "deletions": 3, "status": "modified"}),
        ChangedFile.from_api({"filename": _archive_path(version), "additions": 0, "deletions": 0, "status": "added"}),
    ]


def test_a_pin_bump_is_never_exempt_even_when_the_consumer_exempts_its_paths(policy: Policy) -> None:
    exempting = _exempting_policy(policy)
    matchers = _narrow_matchers()
    assert not soundness_scope(_pr_files(), exempting.classifier, matchers).soundness
    plan: dict[str, Any] = {"decision": "skip", "decision_reason": ""}
    scope, stopped = cli._initial_scope_stop(plan, _pull(2), _pr_files(), exempting, "BenchBox-dev/BenchBox", matchers)
    assert not stopped
    assert scope == {PIN_PATH, _archive_path(NEW_VERSION)}
    assert plan["decision"] != cli.SUCCESS
    assert any("sqlglot runtime pin or archive" in reason for reason in plan["scope_reasons"])


def test_local_review_stages_source_even_when_the_pin_and_archive_are_exempt(
    policy: Policy, alpha_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Fixture()
    root = tmp_path / "consumer"
    root.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.com", *arguments],
            text=True,
        ).strip()

    git("init", "--quiet", "--initial-branch=develop")
    for ref, message in ((BASE_SHA, "base runtime"), (HEAD_SHA, "candidate runtime")):
        for (blob_ref, path), content in fixture.blobs.items():
            if blob_ref == ref:
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        git("add", PIN_PATH, _archive_path(OLD_VERSION if ref == BASE_SHA else NEW_VERSION))
        git("commit", "--quiet", "-m", message)
        if ref == BASE_SHA:
            base = git("rev-parse", "HEAD")
    environment = local.LocalEnvironment.from_environ(
        {
            local.WORK_ENV: str(tmp_path / "work"),
            local.REPOSITORY_ENV: "BenchBox-dev/BenchBox",
            local.ROOT_ENV: str(root),
            local.TRUSTED_ENV: str(alpha_root),
            local.BASE_ENV: base,
            local.HEAD_ENV: git("rev-parse", "HEAD"),
            local.STATE_ENV: str(tmp_path / "state"),
        },
        alpha_root / ".github/oracle-reviewers.yml",
    )
    monkeypatch.setenv("ORACLE_LOCAL_ROOT", str(root))
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setattr(promotion, "default_source_loader", lambda **kwargs: fixture.load_source)
    monkeypatch.setattr(promotion, "ci_ancestor_check", lambda repo: fixture.is_ancestor)
    monkeypatch.setattr(github, "get_json", fixture.identity_json)
    monkeypatch.setattr(local, "load_matchers", lambda consumer, trusted: _narrow_matchers())
    out_dir = environment.layout.plan
    out_dir.mkdir(parents=True)
    assert environment._plan(_exempting_policy(policy), out_dir, None) == 0
    written = _read_plan(out_dir)
    assert written["decision"] == cli.REVIEW
    assert written["promotion_kind"] == sqlglot_adoption.KIND
    assert PIN_PATH in written["scope_paths"]
    assert f"{promotion.STAGED_PROMOTION_DIR}/new/sqlglot/exp.py" in written["evidence_files"]
    assert (out_dir / "promotion/new/sqlglot/exp.py").read_bytes() == fixture.head_tree["sqlglot/exp.py"]


def test_without_adoption_rules_an_exempt_pin_path_still_stops_as_exempt(policy: Policy) -> None:
    exempting = replace(_exempting_policy(policy), sqlglot_adoption=None)
    matchers = _narrow_matchers()
    plan: dict[str, Any] = {"decision": "skip", "decision_reason": ""}
    scope, stopped = cli._initial_scope_stop(plan, _pull(2), _pr_files(), exempting, "BenchBox-dev/BenchBox", matchers)
    assert stopped and scope is None
    assert plan["decision"] == cli.SUCCESS


def _plan(policy: Policy) -> dict[str, Any]:
    plan = cli._base_plan(policy, "BenchBox-dev/BenchBox", None, "1")
    plan.update({"base_sha": BASE_SHA, "head_sha": HEAD_SHA, "base_ref": "develop"})
    return plan


def _round_inputs(files: list[ChangedFile], bundle: promotion.PromotionBundle | None) -> cli.RoundInputs:
    return cli.RoundInputs(
        files=files,
        in_scope={path for item in files for path in item.paths},
        full_diff=None,
        states=None,
        found=protocol.History(records=[]),
        previous_strikes=0,
        pool_blocks={},
        manual=False,
        event_name="local",
        base_changed_at=None,
        base_change_unreadable=False,
        promotion=bundle,
    )


def _read_plan(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / cli.PLAN_FILE).read_text(encoding="utf-8"))


def test_the_actual_source_delta_not_the_pin_edit_drives_the_review_plan(
    policy: Policy, alpha_root: Path, tmp_path: Path
) -> None:
    fixture = Fixture(head_extra=_many_modules(12))
    bundle = fixture.prepare()
    assert bundle is not None
    adoption_policy = _exempting_policy(policy)
    evidence = replace(adoption_policy.evidence, exempt_paths=("sqlglot/**", "downstream/**", "docs/*.md", "tests/**"))
    adoption_policy = replace(adoption_policy, evidence=evidence)
    plan = _plan(adoption_policy)
    cli.plan_decision(
        plan, adoption_policy, tmp_path, _round_inputs(_pr_files(), bundle), _matchers(adoption_policy, alpha_root)
    )
    written = _read_plan(tmp_path)
    prefix = promotion.STAGED_PROMOTION_DIR
    assert written["decision"] == cli.REVIEW
    assert written["promotion_kind"] == sqlglot_adoption.KIND
    assert written["promotion"] == bundle.record.to_json()
    assert written["brief_mode"] == "file-list"
    assert written["diff_inline"] is None
    for index in range(12):
        assert f"{prefix}/new/sqlglot/mod{index}.py" in written["evidence_files"]
    assert f"{prefix}/new/sqlglot/exp.py" in written["evidence_files"]
    assert f"{prefix}/new/downstream/patches.json" in written["evidence_files"]
    assert f"{prefix}/new/tests/test_exp.py" not in written["evidence_files"]
    assert f"{prefix}/new/docs/notes.md" not in written["evidence_files"]
    reviewed = {item["path"]: item for item in written["reviewed_files"]}
    assert reviewed[f"{prefix}/new/sqlglot/mod0.py"]["additions"] == 1
    assert PIN_PATH in reviewed
    assert (tmp_path / "promotion" / "new" / "sqlglot" / "exp.py").is_file()


def test_runtime_source_cannot_be_evidence_exempted_by_consumer_globs(
    policy: Policy, alpha_root: Path, tmp_path: Path
) -> None:
    bundle = Fixture().prepare()
    assert bundle is not None
    greedy = replace(_exempting_policy(policy), evidence=replace(policy.evidence, exempt_paths=("**/*",)))
    cli.plan_decision(
        _plan(greedy), greedy, tmp_path, _round_inputs(_pr_files(), bundle), _matchers(greedy, alpha_root)
    )
    written = _read_plan(tmp_path)
    prefix = promotion.STAGED_PROMOTION_DIR
    assert f"{prefix}/new/sqlglot/exp.py" in written["evidence_files"]
    assert f"{prefix}/new/downstream/patches.json" in written["evidence_files"]
    assert f"{prefix}/new/docs/notes.md" not in written["evidence_files"]


def test_an_oversized_source_delta_is_never_a_truncated_review(
    policy: Policy, alpha_root: Path, tmp_path: Path
) -> None:
    fixture = Fixture(head_extra=_many_modules(120))
    bundle = fixture.prepare()
    assert bundle is not None
    small = replace(_exempting_policy(policy), brief_max_bytes=2000)
    cli.plan_decision(_plan(small), small, tmp_path, _round_inputs(_pr_files(), bundle), _matchers(small, alpha_root))
    written = _read_plan(tmp_path)
    assert written["decision"] == cli.REVIEW
    assert written["brief_mode"] == "oversize"
    assert written["promotion"]["new_artifact_sha256"] == bundle.record.new_artifact_sha256
    assert len(written["reviewed_files"]) > 120


def test_the_prompt_names_the_source_delta_as_the_subject_for_a_sqlglot_adoption(
    policy: Policy, alpha_root: Path, tmp_path: Path
) -> None:
    from theseus_reviewer.classify import Classification

    bundle = Fixture(head_extra=_many_modules(2)).prepare()
    assert bundle is not None
    adoption_policy = _exempting_policy(policy)
    cli.plan_decision(
        _plan(adoption_policy),
        adoption_policy,
        tmp_path,
        _round_inputs(_pr_files(), bundle),
        _matchers(adoption_policy, alpha_root),
    )
    written = _read_plan(tmp_path)
    classification = Classification("large", "deep", "deep", 1.0, (), ())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(workspace), "init", "--quiet"], check=True)
    prompt = cli._assemble_prompt(written, tmp_path, classification, workspace)
    assert "SQLGlot runtime adoption review" in prompt
    assert "not the pin edit" in prompt
    assert "Theseus promotion review" not in prompt
    assert (workspace / promotion.STAGED_PROMOTION_DIR / "new" / "sqlglot" / "exp.py").is_file()


def _git_bundle_environment(monkeypatch: pytest.MonkeyPatch, fixture: Fixture) -> None:
    def get_json(path: str) -> Any:
        return (
            fixture.git_json(path) if "/git/commits/" in path or "/git/trees/" in path else fixture.identity_json(path)
        )

    monkeypatch.setattr(promotion, "default_source_loader", lambda **_kwargs: fixture.load_source)
    monkeypatch.setattr(promotion, "ci_ancestor_check", lambda _repo: fixture.is_ancestor)
    monkeypatch.setattr(github, "get_json", get_json)
    monkeypatch.setattr(github, "_gh_bytes", fixture.git_bytes)


def test_ci_reads_both_pins_and_the_head_archive_by_exact_sha_and_prepares_the_bundle(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    fixture = Fixture(head_extra=_many_modules(2))
    _git_bundle_environment(monkeypatch, fixture)
    bundle, hold = cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        _pr_files(),
    )
    assert hold is None and bundle is not None and bundle.kind == sqlglot_adoption.KIND
    blob_reads = [item for item in fixture.requested if "/git/blobs/" in item]
    assert blob_reads == [
        f"repos/BenchBox-dev/BenchBox/git/blobs/{_blob_sha(fixture.blobs[(HEAD_SHA, PIN_PATH)])}",
        f"repos/BenchBox-dev/BenchBox/git/blobs/{_blob_sha(fixture.blobs[(BASE_SHA, PIN_PATH)])}",
        f"repos/BenchBox-dev/BenchBox/git/blobs/{_blob_sha(fixture.head_wheel)}",
    ]
    assert not any("/contents/" in item for item in fixture.requested)


def test_ci_holds_with_the_refusal_when_the_wheel_does_not_match_its_source(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    fixture = Fixture(
        head_mutate=lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}exp.py", b"VALUE = 'backdoor'\n")
    )

    _git_bundle_environment(monkeypatch, fixture)
    bundle, hold = cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        _pr_files(),
    )
    assert bundle is None
    assert hold is not None and hold.startswith("sqlglot adoption hold:") and "differ" in hold


def test_ci_does_nothing_for_a_pull_request_that_does_not_touch_the_runtime(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("no GitHub read is expected")

    monkeypatch.setattr(github, "_gh_bytes", explode)
    monkeypatch.setattr(github, "_gh", explode)
    other = [ChangedFile.from_api({"filename": "docs/readme.md", "status": "modified"})]
    assert cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        other,
    ) == (None, None)


def test_a_pull_request_cannot_adopt_and_promote_at_once(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    fixture = Fixture()
    sentinel = object()

    _git_bundle_environment(monkeypatch, fixture)
    monkeypatch.setattr(cli, "_load_ci_promotion", lambda *_args: (sentinel, None))
    bundle, hold = cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        _pr_files(),
    )
    assert bundle is None and hold is not None and "cannot both promote theseus and adopt" in hold


def _bundle_repository(tmp_path: Path, fixture: Fixture) -> tuple[Path, str, str]:
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "--quiet")
    (tmp_path / "benchbox/_vendor").mkdir(parents=True)
    (tmp_path / PIN_PATH).write_bytes(fixture.base_pin.encode())
    (tmp_path / _archive_path(OLD_VERSION)).write_bytes(fixture.base_wheel)
    git("add", "-A")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / PIN_PATH).write_bytes(fixture.head_pin.encode())
    (tmp_path / _archive_path(OLD_VERSION)).unlink()
    (tmp_path / _archive_path(NEW_VERSION)).write_bytes(fixture.head_wheel)
    git("add", "-A")
    git("commit", "--quiet", "-m", "adopt")
    return tmp_path, base, git("rev-parse", "HEAD")


def test_the_local_review_reads_the_git_blobs_of_the_exact_base_and_head(
    monkeypatch: pytest.MonkeyPatch, policy: Policy, tmp_path: Path
) -> None:
    fixture = Fixture(head_extra=_many_modules(2))
    root, base_sha, head_sha = _bundle_repository(tmp_path, fixture)
    monkeypatch.setenv("ORACLE_LOCAL_ROOT", str(root))
    _git_bundle_environment(monkeypatch, fixture)
    reader = git_blobs.local_reader(root)
    assert reader(base_sha, PIN_PATH) == fixture.base_pin.encode()
    assert reader(head_sha, _archive_path(NEW_VERSION)) == fixture.head_wheel
    assert reader(base_sha, _archive_path(NEW_VERSION)) is None
    with pytest.raises(promotion.PromotionError, match="git ls-tree"):
        reader("f" * 40, PIN_PATH)
    bundle, hold = cli._resolve_promotion_bundle(
        {"repo": "BenchBox-dev/BenchBox", "base_sha": base_sha, "head_sha": head_sha},
        replace(policy, sqlglot_adoption=RULES),
        _round_inputs(_pr_files(), None),
    )
    assert hold is None and bundle is not None and bundle.kind == sqlglot_adoption.KIND
    assert bundle.record.new_artifact_sha256 == hashlib.sha256(fixture.head_wheel).hexdigest()
    moved = Fixture(head_mutate=lambda entries: entries.__setitem__(f"{NAMESPACE_DIR}exp.py", b"VALUE = 0\n"))
    root2 = tmp_path / "second"
    root2.mkdir()
    root2, base2, head2 = _bundle_repository(root2, moved)
    monkeypatch.setenv("ORACLE_LOCAL_ROOT", str(root2))
    _git_bundle_environment(monkeypatch, moved)
    bundle, hold = cli._resolve_promotion_bundle(
        {"repo": "BenchBox-dev/BenchBox", "base_sha": base2, "head_sha": head2},
        replace(policy, sqlglot_adoption=RULES),
        _round_inputs(_pr_files(), None),
    )
    assert bundle is None and hold is not None and hold.startswith("sqlglot adoption hold:")


def test_an_unpinned_archive_is_refused_even_when_the_pin_did_not_change() -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel
    with pytest.raises(promotion.PromotionError, match="no pin names"):
        fixture.prepare(pr_paths=[_archive_path("99.0.0.1")])
    with pytest.raises(promotion.PromotionError, match="no pin names"):
        fixture.prepare(pr_paths=[PIN_PATH, _archive_path(NEW_VERSION), "benchbox/_vendor/extra.whl"])


def test_an_extra_archive_beside_a_changed_pin_is_refused_but_swapping_the_pinned_archive_is_not() -> None:
    fixture = Fixture()
    with pytest.raises(promotion.PromotionError, match="no pin names"):
        fixture.prepare(pr_paths=[PIN_PATH, _archive_path(NEW_VERSION), _archive_path("30.30.0.2")])
    assert fixture.prepare(pr_paths=[PIN_PATH, _archive_path(OLD_VERSION), _archive_path(NEW_VERSION)]) is not None


def test_an_archive_without_any_pin_is_refused() -> None:
    fixture = Fixture()
    del fixture.blobs[(BASE_SHA, PIN_PATH)]
    del fixture.blobs[(HEAD_SHA, PIN_PATH)]
    with pytest.raises(promotion.PromotionError, match="no pin names"):
        fixture.prepare(pr_paths=[_archive_path(NEW_VERSION)])


def test_the_upstream_tag_must_resolve_to_the_pinned_upstream_sha() -> None:
    fixture = Fixture()
    fixture.tags["v30.30.0"] = {"type": "commit", "sha": "9" * 40}
    with pytest.raises(promotion.PromotionError, match="tag v30.30.0 does not resolve"):
        fixture.prepare()
    del fixture.tags["v30.30.0"]
    with pytest.raises(promotion.PromotionError, match="identity lookup"):
        fixture.prepare()


def test_an_annotated_upstream_tag_is_peeled_to_its_commit() -> None:
    fixture = Fixture()
    fixture.tags["v30.30.0"] = {"type": "tag", "sha": "7" * 40}
    fixture.annotated["7" * 40] = {"type": "commit", "sha": NEW_UPSTREAM}
    assert fixture.prepare() is not None
    fixture.annotated["7" * 40] = {"type": "tree", "sha": NEW_UPSTREAM}
    with pytest.raises(promotion.PromotionError, match="does not resolve"):
        fixture.prepare()


def test_a_fork_sha_that_is_not_on_the_fork_default_branch_is_refused() -> None:
    fixture = Fixture()
    fixture.main_shas.discard(HEAD_FORK)
    with pytest.raises(promotion.PromotionError, match="not reachable from the .* default branch"):
        fixture.prepare()


def test_the_trusted_identity_is_not_consulted_when_the_pin_did_not_change() -> None:
    fixture = Fixture()
    fixture.blobs[(HEAD_SHA, PIN_PATH)] = fixture.base_pin.encode()
    fixture.blobs[(HEAD_SHA, _archive_path(OLD_VERSION))] = fixture.base_wheel
    fixture.prepare(pr_paths=[PIN_PATH], verify_identity=lambda _pin: pytest.fail("identity checked"))


def _ci_hold(monkeypatch: pytest.MonkeyPatch, policy: Policy, fixture: Fixture) -> tuple[Any, str | None]:
    _git_bundle_environment(monkeypatch, fixture)
    return cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        _pr_files(),
    )


@pytest.mark.parametrize("target", ["pin", "archive"])
def test_a_symlinked_pin_or_archive_is_held_instead_of_following_the_link(
    monkeypatch: pytest.MonkeyPatch, policy: Policy, target: str
) -> None:
    fixture = Fixture()
    linked = PIN_PATH if target == "pin" else _archive_path(NEW_VERSION)
    fixture.modes[(HEAD_SHA, linked)] = "120000"
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert bundle is None
    assert hold is not None and hold.startswith("sqlglot adoption hold:") and "not a regular file" in hold


def test_an_executable_regular_file_mode_is_still_a_regular_file(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    fixture = Fixture()
    fixture.modes[(HEAD_SHA, _archive_path(NEW_VERSION))] = "100755"
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert hold is None and bundle is not None


def test_a_truncated_git_tree_is_held_not_read_partially(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    fixture = Fixture()
    original = fixture.git_json

    def truncated(path: str) -> Any:
        payload = original(path)
        if "/git/trees/" in path:
            payload["truncated"] = True
        return payload

    monkeypatch.setattr(fixture, "git_json", truncated)
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert bundle is None and hold is not None and "truncated" in hold


def test_a_pinned_directory_that_is_a_symlink_is_held(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    fixture = Fixture()
    original = fixture.git_json

    def linked(path: str) -> Any:
        payload = original(path)
        for entry in payload.get("tree", ()) if "/git/trees/" in path else ():
            if entry["path"] == "_vendor":
                entry.update(mode="120000", type="blob")
        return payload

    monkeypatch.setattr(fixture, "git_json", linked)
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert bundle is None and hold is not None and "symlink" in hold


def test_ci_holds_an_adoption_whose_fork_identity_is_off_the_default_branch(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    fixture = Fixture()
    fixture.main_shas.discard(HEAD_FORK)
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert bundle is None and hold is not None and "default branch" in hold


def test_ci_holds_an_adoption_whose_upstream_tag_names_a_different_commit(
    monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    fixture = Fixture()
    fixture.tags["v30.30.0"] = {"type": "commit", "sha": "9" * 40}
    bundle, hold = _ci_hold(monkeypatch, policy, fixture)
    assert bundle is None and hold is not None and "does not resolve" in hold


def test_ci_holds_an_adoption_that_adds_an_unpinned_archive(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    fixture = Fixture()
    _git_bundle_environment(monkeypatch, fixture)
    bundle, hold = cli._load_ci_bundle(
        "BenchBox-dev/BenchBox",
        {"pr": 7, "base_sha": BASE_SHA, "head_sha": HEAD_SHA},
        replace(policy, sqlglot_adoption=RULES),
        [
            *_pr_files(),
            ChangedFile.from_api({"filename": "benchbox/_vendor/evil.zip", "status": "added"}),
        ],
    )
    assert bundle is None and hold is not None and "no pin names" in hold


def test_the_local_reader_refuses_symlinked_pins_archives_and_directories(tmp_path: Path) -> None:
    import os
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "--quiet")
    (tmp_path / "real").mkdir()
    (tmp_path / "real/data.zip").write_bytes(b"payload")
    os.symlink("real/data.zip", tmp_path / "link.zip")
    os.symlink("real", tmp_path / "linked")
    git("add", "-A")
    git("commit", "--quiet", "-m", "links")
    head = git("rev-parse", "HEAD")
    reader = git_blobs.local_reader(tmp_path)
    assert reader(head, "real/data.zip") == b"payload"
    with pytest.raises(promotion.PromotionError, match="not a regular file"):
        reader(head, "link.zip")
    with pytest.raises(promotion.PromotionError, match="symlink"):
        reader(head, "linked/data.zip")
    assert reader(head, "absent/data.zip") is None
