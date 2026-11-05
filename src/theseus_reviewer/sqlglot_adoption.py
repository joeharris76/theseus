from __future__ import annotations

import email
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import promotion
from .policy import SqlglotAdoptionRules
from .sqlglot_relocation import RelocationError, relocate

KIND = "sqlglot"
SOURCE_PACKAGE_DIR = "sqlglot"
RUNTIME_SOURCE_PREFIXES = ("sqlglot/", "sqlglotc/", "downstream/", ".github/")
RUNTIME_SOURCE_FILES = frozenset({"LICENSE", "MANIFEST.in", "pyproject.toml", "setup.cfg", "setup.py"})
PIN_KEYS = frozenset(
    {
        "schema",
        "distribution",
        "namespace",
        "repository",
        "version",
        "fork_sha",
        "upstream_sha",
        "wheel",
        "archive",
        "sha256",
    }
)
PROVENANCE_KEYS = frozenset(
    {
        "schema",
        "distribution",
        "repository",
        "namespace",
        "version",
        "upstream_sha",
        "fork_sha",
        "source_date_epoch",
        "dirty",
        "manifest",
        "files",
    }
)
DIST_INFO_FILES = frozenset({"METADATA", "WHEEL", "RECORD", "top_level.txt", "licenses/LICENSE"})
FORBIDDEN_SUFFIXES = (*promotion.FORBIDDEN_WHEEL_SUFFIXES, ".pyc", ".pyo")
REPORTED_PATHS = 5
_VERSION = re.compile(r"\d+\.\d+\.\d+\.[1-9]\d*")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

BlobReader = Callable[[str, str], bytes | None]


class AdoptionError(promotion.PromotionError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdoptionError(message)


@dataclass(frozen=True)
class Pin:
    distribution: str
    namespace: str
    repository: str
    version: str
    fork_sha: str
    upstream_sha: str
    wheel: str
    archive: str
    sha256: str

    def canonical(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "distribution": self.distribution,
            "namespace": self.namespace,
            "repository": self.repository,
            "version": self.version,
            "fork_sha": self.fork_sha,
            "upstream_sha": self.upstream_sha,
            "wheel": self.wheel,
            "archive": self.archive,
            "sha256": self.sha256,
        }

    @property
    def version_key(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.version.split("."))


def parse_pin(text: str, rules: SqlglotAdoptionRules) -> Pin:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdoptionError(f"sqlglot runtime pin is not valid JSON: {exc}") from exc
    _require(isinstance(data, Mapping) and set(data) == PIN_KEYS, "sqlglot runtime pin has missing or unexpected keys")
    _require(data["schema"] == 1 and not isinstance(data["schema"], bool), "sqlglot runtime pin schema must be 1")
    for key in PIN_KEYS - {"schema"}:
        _require(isinstance(data[key], str), f"sqlglot runtime pin {key} must be a string")
    _require(
        data["distribution"] == rules.distribution, "sqlglot runtime pin distribution does not match consumer policy"
    )
    _require(data["namespace"] == rules.namespace, "sqlglot runtime pin namespace does not match consumer policy")
    _require(
        data["repository"].casefold() == f"https://github.com/{rules.fork_repository}".casefold(),
        "sqlglot runtime pin repository is not the policy fork repository",
    )
    version = data["version"]
    _require(_VERSION.fullmatch(version) is not None, "sqlglot runtime pin version must be <upstream>.<fork revision>")
    _require(_SHA1.fullmatch(data["fork_sha"]) is not None, "sqlglot runtime pin fork_sha must be a full 40-hex sha")
    _require(
        _SHA1.fullmatch(data["upstream_sha"]) is not None, "sqlglot runtime pin upstream_sha must be a full 40-hex sha"
    )
    _require(_SHA256.fullmatch(data["sha256"]) is not None, "sqlglot runtime pin sha256 must be 64-char lowercase hex")
    expected_wheel = f"{promotion._normalized_dist_name(rules.distribution)}-{version}-py3-none-any.whl"
    _require(data["wheel"] == expected_wheel, "sqlglot runtime pin wheel name does not match distribution and version")
    _require(data["archive"] == f"{expected_wheel}.zip", "sqlglot runtime pin archive is not the wheel name plus .zip")
    return Pin(
        distribution=data["distribution"],
        namespace=data["namespace"],
        repository=data["repository"],
        version=version,
        fork_sha=data["fork_sha"],
        upstream_sha=data["upstream_sha"],
        wheel=data["wheel"],
        archive=data["archive"],
        sha256=data["sha256"],
    )


def archive_path(rules: SqlglotAdoptionRules, pin: Pin) -> str:
    path = f"{rules.runtime_dir}/{pin.archive}"
    _require(promotion.valid_path(path), "sqlglot runtime archive path is not a safe repository-relative path")
    return path


def pin_binding_digest(base: Pin | None, head: Pin) -> str:
    payload = {"old": None if base is None else base.canonical(), "new": head.canonical()}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def touched(rules: SqlglotAdoptionRules | None, paths: Iterable[str]) -> bool:
    if rules is None:
        return False
    runtime = f"{rules.runtime_dir}/"
    return any(
        path == rules.pin_path
        or (path.startswith(runtime) and "/" not in path[len(runtime) :] and path.endswith((".zip", ".whl")))
        for path in paths
    )


def touched_paths(rules: SqlglotAdoptionRules | None, paths: Iterable[str]) -> list[str]:
    if rules is None:
        return []
    return sorted({path for path in paths if touched(rules, [path])})


def unpinned_archives(rules: SqlglotAdoptionRules, paths: Iterable[str], pins: Iterable[Pin | None]) -> list[str]:
    allowed = {archive_path(rules, pin) for pin in pins if pin is not None}
    runtime = f"{rules.runtime_dir}/"
    return sorted(
        {
            path
            for path in paths
            if path.startswith(runtime)
            and "/" not in path[len(runtime) :]
            and path.endswith((".zip", ".whl"))
            and path not in allowed
        }
    )


def _unpinned_reason(unpinned: Sequence[str]) -> str:
    return f"sqlglot runtime archives that no pin names are refused: {_sample(unpinned)}"


def runtime_relevant(path: str) -> bool:
    return path in RUNTIME_SOURCE_FILES or path.startswith(RUNTIME_SOURCE_PREFIXES)


def _decode(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdoptionError(f"{path} is not valid UTF-8: {exc}") from exc


def _version_module(version: str) -> bytes:
    return f"__version__ = {version!r}\n__version_tuple__ = {tuple(map(int, version.split('.')))!r}\n".encode()


def _check_manifest(manifest: Any, pin: Pin, rules: SqlglotAdoptionRules) -> None:
    _require(isinstance(manifest, Mapping), "fork downstream/patches.json is not an object")
    upstream = manifest.get("upstream")
    _require(isinstance(upstream, Mapping), "fork downstream/patches.json has no upstream record")
    _require(
        manifest.get("schema") == 1
        and manifest.get("distribution") == rules.distribution
        and manifest.get("import_package") == rules.namespace
        and manifest.get("version") == pin.version,
        "fork downstream/patches.json does not describe the pinned distribution, namespace and version",
    )
    _require(
        upstream.get("repository") == f"https://github.com/{rules.upstream_repository}"
        and upstream.get("sha") == pin.upstream_sha
        and upstream.get("tag") == f"v{pin.version.rsplit('.', 1)[0]}",
        "fork downstream/patches.json upstream repository, tag or sha disagrees with the pin",
    )


def expected_runtime_payload(
    source_tree: Mapping[str, bytes], pin: Pin, rules: SqlglotAdoptionRules
) -> dict[str, bytes]:
    prefix = f"{SOURCE_PACKAGE_DIR}/"
    payload: dict[str, bytes] = {}
    for path, raw in sorted(source_tree.items()):
        if not (path.startswith(prefix) and path.endswith(".py")):
            continue
        relative = path[len(prefix) :]
        if relative == "_version.py":
            continue
        try:
            payload[relative] = relocate(_decode(raw, path), relative, rules.namespace).encode()
        except RelocationError as exc:
            raise AdoptionError(str(exc)) from exc
    _require(f"{prefix}py.typed" in source_tree, "fork source has no sqlglot/py.typed")
    _require("LICENSE" in source_tree, "fork source has no LICENSE")
    _require("__init__.py" in payload, "fork source has no sqlglot/__init__.py")
    payload["py.typed"] = source_tree[f"{prefix}py.typed"]
    payload["LICENSE"] = source_tree["LICENSE"]
    payload["_version.py"] = _version_module(pin.version)
    return payload


def _sample(paths: Iterable[str]) -> str:
    ordered = sorted(paths)
    suffix = "" if len(ordered) <= REPORTED_PATHS else f" and {len(ordered) - REPORTED_PATHS} more"
    return ", ".join(repr(path) for path in ordered[:REPORTED_PATHS]) + suffix


def _refuse_executable_entries(entries: Mapping[str, bytes]) -> None:
    for name in entries:
        parts = name.split("/")
        _require(
            not name.lower().endswith(FORBIDDEN_SUFFIXES) and parts[-1] != "entry_points.txt",
            f"wheel contains forbidden executable or hook entry {name!r}",
        )
        _require(
            not any(part.endswith(".data") for part in parts[:-1]),
            f"wheel contains forbidden .data directory entry {name!r}",
        )


def _check_metadata(entries: Mapping[str, bytes], dist_info: str, pin: Pin, rules: SqlglotAdoptionRules) -> None:
    metadata = email.message_from_bytes(entries[f"{dist_info}METADATA"])
    wheel_metadata = email.message_from_bytes(entries[f"{dist_info}WHEEL"])
    _require(
        metadata["Name"] == pin.distribution
        and metadata["Version"] == pin.version
        and not metadata.get_all("Requires-Dist")
        and not metadata.get_all("Provides-Extra"),
        "wheel METADATA does not describe the pinned dependency-free distribution and version",
    )
    _require(
        wheel_metadata.get_all("Tag") == ["py3-none-any"] and wheel_metadata["Root-Is-Purelib"] == "true",
        "wheel is not a pure-Python py3-none-any wheel",
    )
    _require(
        entries[f"{dist_info}top_level.txt"].decode("utf-8", errors="replace").split()
        == [rules.namespace.split(".")[0]],
        "wheel top_level.txt does not name only the host namespace root",
    )


def _check_provenance(raw: bytes, payload: Mapping[str, bytes], manifest: Any, pin: Pin) -> None:
    try:
        provenance = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdoptionError(f"wheel benchbox_provenance.json is not valid JSON: {exc}") from exc
    _require(
        isinstance(provenance, Mapping) and set(provenance) == PROVENANCE_KEYS,
        "wheel provenance has missing or unexpected keys",
    )
    epoch = provenance["source_date_epoch"]
    _require(isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > 0, "wheel provenance epoch is invalid")
    expected = {
        "schema": 1,
        "distribution": pin.distribution,
        "repository": pin.repository,
        "namespace": pin.namespace,
        "version": pin.version,
        "upstream_sha": pin.upstream_sha,
        "fork_sha": pin.fork_sha,
        "dirty": False,
        "manifest": manifest,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()},
    }
    actual = {key: value for key, value in provenance.items() if key != "source_date_epoch"}
    for key, value in expected.items():
        _require(
            actual[key] == value and type(actual[key]) is type(value),
            f"wheel provenance {key} does not match the pin and the staged fork source",
        )


def verify_runtime_wheel(
    wheel_bytes: bytes, source_tree: Mapping[str, bytes], pin: Pin, rules: SqlglotAdoptionRules
) -> None:
    entries = promotion._read_wheel_entries(wheel_bytes)
    _refuse_executable_entries(entries)
    promotion.verify_wheel_record(entries, pin.distribution, pin.version)
    dist_info = f"{promotion._normalized_dist_name(pin.distribution)}-{pin.version}.dist-info/"
    dist_names = {name.removeprefix(dist_info) for name in entries if name.startswith(dist_info)}
    _require(
        dist_names == DIST_INFO_FILES,
        f"wheel dist-info files differ from the allowed set: {_sample(dist_names ^ DIST_INFO_FILES)}",
    )
    _check_metadata(entries, dist_info, pin, rules)
    _require(
        entries[f"{dist_info}licenses/LICENSE"] == source_tree.get("LICENSE"),
        "wheel dist-info license differs from the fork source LICENSE",
    )
    manifest_raw = source_tree.get("downstream/patches.json")
    _require(manifest_raw is not None, "fork source has no downstream/patches.json")
    assert manifest_raw is not None
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as exc:
        raise AdoptionError(f"fork downstream/patches.json is not valid JSON: {exc}") from exc
    _check_manifest(manifest, pin, rules)
    payload = expected_runtime_payload(source_tree, pin, rules)
    namespace_dir = rules.namespace.replace(".", "/") + "/"
    provenance_name = f"{namespace_dir}benchbox_provenance.json"
    _require(provenance_name in entries, "wheel has no benchbox_provenance.json")
    _check_provenance(entries[provenance_name], payload, manifest, pin)
    parts = rules.namespace.split(".")
    expected: dict[str, bytes] = {"/".join(parts[:depth]) + "/__init__.py": b"" for depth in range(1, len(parts))}
    for relative, content in payload.items():
        expected[f"{namespace_dir}{relative}"] = content
    expected[provenance_name] = entries[provenance_name]
    runtime = {name: content for name, content in entries.items() if not name.startswith(dist_info)}
    missing = set(expected) - set(runtime)
    extra = set(runtime) - set(expected)
    _require(not missing, f"wheel is missing runtime files the fork source maps: {_sample(missing)}")
    _require(not extra, f"wheel carries runtime files absent from the fork source mapping: {_sample(extra)}")
    differing = [name for name, content in expected.items() if runtime[name] != content]
    _require(
        not differing,
        f"wheel runtime bytes differ from the trusted relocation of the fork source: {_sample(differing)}",
    )


def _read_pin(read_blob: BlobReader, ref: str, rules: SqlglotAdoptionRules) -> Pin | None:
    raw = read_blob(ref, rules.pin_path)
    if raw is None:
        return None
    return parse_pin(_decode(raw, rules.pin_path), rules)


def _read_wheel(read_blob: BlobReader, ref: str, rules: SqlglotAdoptionRules, pin: Pin) -> bytes:
    path = archive_path(rules, pin)
    raw = read_blob(ref, path)
    _require(raw is not None, f"sqlglot runtime archive {path} is not present at {ref[:12]}")
    assert raw is not None
    actual = hashlib.sha256(raw).hexdigest()
    _require(
        actual == pin.sha256, f"sqlglot runtime archive {path} has sha256 {actual}, but the pin records {pin.sha256}"
    )
    return raw


IdentityValidator = Callable[[Pin], None]


def github_identity_validator(rules: SqlglotAdoptionRules, fetch: Callable[[str], Any]) -> IdentityValidator:
    def fetch_json(path: str) -> Any:
        try:
            return fetch(path)
        except (RuntimeError, ValueError) as exc:
            raise AdoptionError(f"sqlglot identity lookup {path} failed: {exc}") from exc

    def verify(pin: Pin) -> None:
        repository = fetch_json(f"repos/{rules.fork_repository}")
        branch = repository.get("default_branch") if isinstance(repository, Mapping) else None
        _require(isinstance(branch, str) and bool(branch), f"{rules.fork_repository} did not report a default branch")
        comparison = fetch_json(
            f"repos/{rules.fork_repository}/compare/{pin.fork_sha}...{urllib.parse.quote(str(branch), safe='/')}"
        )
        status = comparison.get("status") if isinstance(comparison, Mapping) else None
        _require(
            status in ("ahead", "identical"),
            f"pinned fork sha {pin.fork_sha[:12]} is not reachable from the {rules.fork_repository} default branch",
        )
        tag = "v" + ".".join(pin.version.split(".")[:3])
        reference = fetch_json(f"repos/{rules.upstream_repository}/git/ref/tags/{tag}")
        target = reference.get("object") if isinstance(reference, Mapping) else None
        for _ in range(4):
            if not (isinstance(target, Mapping) and target.get("type") == "tag"):
                break
            annotated = fetch_json(f"repos/{rules.upstream_repository}/git/tags/{target.get('sha')}")
            target = annotated.get("object") if isinstance(annotated, Mapping) else None
        _require(
            isinstance(target, Mapping) and target.get("type") == "commit" and target.get("sha") == pin.upstream_sha,
            f"{rules.upstream_repository} tag {tag} does not resolve to the pinned upstream sha {pin.upstream_sha[:12]}",
        )

    return verify


def _check_identity_movement(base: Pin, head: Pin, is_ancestor: promotion.AncestorCheck) -> None:
    if base.fork_sha == head.fork_sha:
        _require(
            (base.version, base.upstream_sha) == (head.version, head.upstream_sha),
            "the fork source is unchanged, so the version and upstream sha cannot change",
        )
        return
    _require(head.version_key != base.version_key, "a changed fork source must have a different runtime version")
    _require(
        is_ancestor(head.upstream_sha, head.fork_sha),
        "the pinned upstream sha is not an ancestor of the pinned fork sha",
    )
    if head.version_key < base.version_key:
        _require(
            is_ancestor(head.fork_sha, base.fork_sha),
            "a runtime rollback must restore an ancestor of the base fork sha",
        )
    else:
        _require(
            is_ancestor(base.fork_sha, head.fork_sha),
            "the base fork sha is not an ancestor of the head fork sha",
        )


def prepare_adoption(
    *,
    rules: SqlglotAdoptionRules,
    base_sha: str,
    head_sha: str,
    read_blob: BlobReader,
    source_loader: promotion.SourceLoader,
    is_ancestor: promotion.AncestorCheck,
    verify_identity: IdentityValidator,
    pr_paths: Iterable[str] = (),
) -> promotion.PromotionBundle | None:
    _require(
        not promotion.has_planted_promotion_path(pr_paths),
        f"pull request cannot touch {promotion.STAGED_PROMOTION_DIR}",
    )
    head_pin = _read_pin(read_blob, head_sha, rules)
    base_pin = _read_pin(read_blob, base_sha, rules)
    if head_pin is None:
        _require(base_pin is None, "sqlglot runtime pin cannot be removed")
    unpinned = unpinned_archives(rules, pr_paths, (base_pin, head_pin))
    _require(not unpinned, _unpinned_reason(unpinned))
    if head_pin is None:
        return None
    _require(base_pin is not None, "sqlglot runtime adoption needs a pin on the base branch")
    assert base_pin is not None
    head_wheel = _read_wheel(read_blob, head_sha, rules, head_pin)
    if base_pin == head_pin:
        return None
    _check_identity_movement(base_pin, head_pin, is_ancestor)
    verify_identity(head_pin)
    old_tree = source_loader(rules.fork_repository, base_pin.fork_sha, None)
    new_tree = source_loader(rules.fork_repository, head_pin.fork_sha, None)
    verify_runtime_wheel(head_wheel, new_tree, head_pin, rules)
    changed_files, diff_text, delta_sha256 = promotion.compute_upstream_delta(old_tree, new_tree)
    record = promotion.PromotionRecord(
        head_sha=head_sha,
        old_source_sha256=promotion.tree_sha256(old_tree),
        new_source_sha256=promotion.tree_sha256(new_tree),
        old_artifact_sha256=base_pin.sha256,
        new_artifact_sha256=head_pin.sha256,
        delta_sha256=delta_sha256,
        lock_sha256=pin_binding_digest(base_pin, head_pin),
    )
    return promotion.PromotionBundle(
        record=record,
        changed_files=changed_files,
        diff_text=diff_text,
        old_tree=old_tree,
        new_tree=new_tree,
        kind=KIND,
    )


def _record_refusals(decoded: Sequence[Mapping[str, Any]], *, head_sha: str, base: Pin, head: Pin) -> list[str]:
    from . import protocol

    label = f"sqlglot adoption to {head.version}"
    if not decoded:
        return [f"{label} has no unedited oracle review for head {head_sha[:12]}"]
    latest = decoded[-1]
    if latest.get("open_defects") or latest.get("decision") != protocol.SHIP:
        count = len(latest.get("open_defects") or ())
        return [f"{label} review for head {head_sha[:12]} decided {latest.get('decision')} with {count} open defect(s)"]
    if latest.get("carried") or latest.get("kind") == protocol.CARRY:
        return [f"{label} review for head {head_sha[:12]} cannot be carried across heads"]
    record = latest.get("promotion")
    if not promotion.valid_promotion_record(record):
        return [f"{label} has no source-aware evidence record for head {head_sha[:12]}"]
    assert isinstance(record, Mapping)
    reasons: list[str] = []
    if record["head_sha"] != head_sha or latest.get("head_sha") != head_sha:
        reasons.append(f"adoption evidence record head does not match {head_sha[:12]}")
    if record["old_artifact_sha256"] != base.sha256:
        reasons.append("adoption evidence record old runtime hash does not match the base pin")
    if record["new_artifact_sha256"] != head.sha256:
        reasons.append("adoption evidence record new runtime hash does not match the head pin")
    if record["lock_sha256"] != pin_binding_digest(base, head):
        reasons.append("adoption evidence record pin binding does not match the base and head pins")
    if record["bootstrap"]:
        reasons.append("adoption evidence record cannot be a bootstrap record")
    return reasons


def refusals(
    files: Sequence[str],
    base_pin_text: str | None,
    head_pin_text: str | None,
    head_wheel: bytes | None,
    reviews: Sequence[Mapping[str, Any]],
    head_sha: str,
    *,
    rules: SqlglotAdoptionRules,
    complete: bool = True,
    base_time: datetime | None = None,
    oracle_login: str,
    expected_run_uuid: str | None = None,
) -> tuple[bool, list[str]]:
    if promotion.has_planted_promotion_path(files):
        return True, [f"pull request cannot touch {promotion.STAGED_PROMOTION_DIR}"]
    if complete and not touched(rules, files):
        return False, []
    if head_pin_text is None:
        if base_pin_text is not None:
            return True, ["sqlglot runtime pin cannot be removed"]
        stray = unpinned_archives(rules, files, ())
        return (True, [_unpinned_reason(stray)]) if stray else (False, [])
    if base_pin_text is None:
        return True, ["sqlglot runtime adoption needs a pin on the base branch"]
    try:
        head = parse_pin(head_pin_text, rules)
        base = parse_pin(base_pin_text, rules)
    except AdoptionError as exc:
        return True, [str(exc)]
    reasons: list[str] = []
    unpinned = unpinned_archives(rules, files, (base, head))
    if unpinned:
        reasons.append(_unpinned_reason(unpinned))
    if head_wheel is None:
        reasons.append(f"sqlglot runtime archive {archive_path(rules, head)} is not present at the head")
    elif hashlib.sha256(head_wheel).hexdigest() != head.sha256:
        reasons.append("sqlglot runtime archive bytes at the head do not match the pin sha256")
    if base == head and not complete and not reasons:
        return False, []
    if base == head:
        return (True, reasons) if touched(rules, files) or reasons else (False, [])
    if not complete:
        reasons.append("the pull request's file list is incomplete, so adoption scope cannot be verified")
    decoded, history_error = promotion._head_app_records(
        reviews, head_sha=head_sha, oracle_login=oracle_login, base_time=base_time
    )
    if history_error is not None:
        reasons.append(history_error)
        return True, reasons
    reasons.extend(_record_refusals(decoded, head_sha=head_sha, base=base, head=head))
    if not reasons and expected_run_uuid is not None and decoded and decoded[-1].get("run_uuid") != expected_run_uuid:
        reasons.append("adoption review run_uuid does not match the latest oracle-verdict status")
    return True, reasons
