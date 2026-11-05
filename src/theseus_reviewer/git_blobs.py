from __future__ import annotations

import hashlib
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import promotion

REGULAR_MODES = frozenset({"100644", "100755"})
SYMLINK_MODE = "120000"
_SHA1 = re.compile(r"[0-9a-f]{40}")

FetchJson = Callable[[str], Any]
FetchBlob = Callable[[str], bytes]
BlobReader = Callable[[str, str], bytes | None]


class BlobError(promotion.PromotionError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BlobError(message)


def _regular_sha(mode: str, kind: str, sha: str, path: str, ref: str) -> str:
    _require(
        kind == "blob" and mode in REGULAR_MODES,
        f"{path} at {ref[:12]} is not a regular file (mode {mode}, type {kind}); symlinks and special entries are refused",
    )
    _require(_SHA1.fullmatch(sha) is not None, f"{path} at {ref[:12]} has an invalid blob sha")
    return sha


class TreeReader:
    def __init__(self, repo: str, fetch_json: FetchJson, fetch_blob: FetchBlob) -> None:
        self._repo = repo
        self._fetch_json = fetch_json
        self._fetch_blob = fetch_blob
        self._roots: dict[str, str] = {}
        self._trees: dict[str, dict[str, tuple[str, str, str]]] = {}

    def _root(self, ref: str) -> str:
        cached = self._roots.get(ref)
        if cached is not None:
            return cached
        commit = self._fetch_json(f"repos/{self._repo}/git/commits/{urllib.parse.quote(ref, safe='')}")
        tree = commit.get("tree") if isinstance(commit, dict) else None
        sha = tree.get("sha") if isinstance(tree, dict) else None
        _require(isinstance(sha, str) and _SHA1.fullmatch(sha) is not None, f"commit {ref[:12]} has no tree")
        assert isinstance(sha, str)
        self._roots[ref] = sha
        return sha

    def _tree(self, sha: str) -> dict[str, tuple[str, str, str]]:
        cached = self._trees.get(sha)
        if cached is not None:
            return cached
        payload = self._fetch_json(f"repos/{self._repo}/git/trees/{sha}")
        _require(
            isinstance(payload, dict) and isinstance(payload.get("tree"), list) and payload.get("truncated") is False,
            f"git tree {sha[:12]} is truncated or malformed",
        )
        entries: dict[str, tuple[str, str, str]] = {}
        for item in payload["tree"]:
            _require(
                isinstance(item, dict)
                and all(isinstance(item.get(key), str) for key in ("path", "mode", "type", "sha")),
                f"git tree {sha[:12]} has a malformed entry",
            )
            entries[item["path"]] = (item["mode"], item["type"], item["sha"])
        self._trees[sha] = entries
        return entries

    def __call__(self, ref: str, path: str) -> bytes | None:
        *directories, name = path.split("/")
        tree = self._root(ref)
        walked = ""
        for part in directories:
            walked = f"{walked}{part}/"
            entry = self._tree(tree).get(part)
            if entry is None:
                return None
            mode, kind, sha = entry
            if kind != "tree":
                _require(
                    mode != SYMLINK_MODE,
                    f"{walked.rstrip('/')} at {ref[:12]} is a symlink; symlinked paths are refused",
                )
                return None
            tree = sha
        entry = self._tree(tree).get(name)
        if entry is None:
            return None
        sha = _regular_sha(entry[0], entry[1], entry[2], path, ref)
        raw = self._fetch_blob(sha)
        _require(
            hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() == sha,
            f"blob {sha[:12]} for {path} does not match its git object id",
        )
        return raw


def local_reader(root: Path) -> BlobReader:
    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)

    def read(ref: str, path: str) -> bytes | None:
        *directories, _name = path.split("/")
        for index in range(len(directories)):
            prefix = "/".join(directories[: index + 1])
            listed = git("ls-tree", "-z", ref, "--", prefix)
            _require(listed.returncode == 0, f"git ls-tree {ref[:12]}:{prefix} failed")
            for record in filter(None, listed.stdout.split(b"\0")):
                meta, _, _entry_path = record.partition(b"\t")
                _require(meta.split(b" ")[0] != SYMLINK_MODE.encode(), f"{prefix} at {ref[:12]} is a symlink")
        listed = git("ls-tree", "-z", ref, "--", path)
        _require(listed.returncode == 0, f"git ls-tree {ref[:12]}:{path} failed")
        records = [record for record in listed.stdout.split(b"\0") if record]
        if not records:
            return None
        _require(len(records) == 1, f"git ls-tree {ref[:12]}:{path} listed more than one entry")
        meta, _, entry_path = records[0].partition(b"\t")
        fields = meta.decode("ascii", errors="replace").split(" ")
        _require(
            len(fields) == 3 and entry_path.decode("utf-8", errors="replace") == path,
            f"git ls-tree {ref[:12]}:{path} is malformed",
        )
        sha = _regular_sha(fields[0], fields[1], fields[2], path, ref)
        shown = git("cat-file", "blob", sha)
        _require(shown.returncode == 0, f"git cat-file {sha[:12]} for {path} failed")
        return shown.stdout

    return read
