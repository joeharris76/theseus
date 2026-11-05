from __future__ import annotations

import io
import stat
import struct
import tarfile
import zipfile
import zipimport
import zlib
from pathlib import Path

import pytest

from theseus_reviewer import github, promotion

APPROVED = b"VALUE = 'approved'\n"
TAIL = b"import os\nos.system('echo owned')\n"


def _deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _build_zip(name: str, method: int, payload: bytes, *, usize: int, crc: int, csize: int | None = None) -> bytes:
    encoded = name.encode()
    csize = len(payload) if csize is None else csize
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, method, 0, 0x21, crc, csize, usize, len(encoded), 0)
    local_block = local + encoded + payload
    central = (
        struct.pack(
            "<4s6H3L5H2L",
            b"PK\x01\x02",
            20,
            20,
            0,
            method,
            0,
            0x21,
            crc,
            csize,
            usize,
            len(encoded),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + encoded
    )
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(local_block), 0)
    return local_block + central + end


def _zip64_directory_swap() -> bytes:
    approved = _build_zip("pkg/mod.py", zipfile.ZIP_STORED, APPROVED, usize=len(APPROVED), crc=zlib.crc32(APPROVED))
    approved_directory = approved.index(b"PK\x01\x02")
    approved_local = approved[:approved_directory]
    approved_central = bytearray(approved[approved_directory:-22])
    hidden = b"VALUE = 'hidden runtime'\n"
    alternate = _build_zip("pkg/mod.py", zipfile.ZIP_STORED, hidden, usize=len(hidden), crc=zlib.crc32(hidden))
    alternate_directory = alternate.index(b"PK\x01\x02")
    hidden_local = alternate[:alternate_directory]
    hidden_central = bytearray(alternate[alternate_directory:-22])
    hidden_offset = len(approved_local) + len(approved_central)
    struct.pack_into("<L", hidden_central, 42, hidden_offset)
    directory_offset = hidden_offset + len(hidden_local)
    zip64_end = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, len(hidden_central), directory_offset)
    comment = hidden_local + hidden_central + zip64_end + bytes(20)
    struct.pack_into("<H", approved_central, 32, len(comment))
    directory = bytes(approved_central) + comment
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), len(approved_local), 0)
    return approved_local + directory + end


def _exploit(method: int) -> bytes:
    full = APPROVED + TAIL
    payload = full if method == zipfile.ZIP_STORED else _deflate(full)
    return _build_zip("pkg/mod.py", method, payload, usize=len(APPROVED), crc=zlib.crc32(APPROVED))


def _runtime_bytes(tmp_path: Path, archive: bytes) -> bytes:
    path = tmp_path / "runtime.zip"
    path.write_bytes(archive)
    return zipimport.zipimporter(str(path)).get_data(f"{path}/pkg/mod.py")


def _zipfile_bytes(archive: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return zf.read("pkg/mod.py")


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_undersized_declared_length_diverges_between_zipfile_and_zipimport(tmp_path: Path, method: int) -> None:
    archive = _exploit(method)
    assert _zipfile_bytes(archive) == APPROVED
    assert _runtime_bytes(tmp_path, archive) == APPROVED + TAIL


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_read_wheel_entries_refuses_undersized_declared_length(method: int) -> None:
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(_exploit(method))


def test_hidden_zip64_directory_cannot_substitute_unreviewed_runtime_bytes(tmp_path: Path) -> None:
    archive = _zip64_directory_swap()
    assert _zipfile_bytes(archive) == APPROVED
    assert _runtime_bytes(tmp_path, archive) in (APPROVED, b"VALUE = 'hidden runtime'\n")
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(archive)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_read_wheel_entries_accepts_consistent_entries(tmp_path: Path, method: int) -> None:
    payload = APPROVED if method == zipfile.ZIP_STORED else _deflate(APPROVED)
    archive = _build_zip("pkg/mod.py", method, payload, usize=len(APPROVED), crc=zlib.crc32(APPROVED))
    assert promotion._read_wheel_entries(archive) == {"pkg/mod.py": APPROVED}
    assert _runtime_bytes(tmp_path, archive) == APPROVED


def test_read_wheel_entries_accepts_zipfile_written_archive() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("a.py", b"x = 1\n", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("b.py", b"y = 2\n", compress_type=zipfile.ZIP_STORED)
        zf.writestr("empty.py", b"", compress_type=zipfile.ZIP_DEFLATED)
    assert promotion._read_wheel_entries(buffer.getvalue()) == {"a.py": b"x = 1\n", "b.py": b"y = 2\n", "empty.py": b""}


def test_read_wheel_entries_refuses_deflate_trailing_bytes() -> None:
    payload = _deflate(APPROVED) + b"\x00junk"
    archive = _build_zip("pkg/mod.py", zipfile.ZIP_DEFLATED, payload, usize=len(APPROVED), crc=zlib.crc32(APPROVED))
    with pytest.raises(promotion.PromotionError, match="trailing"):
        promotion._read_wheel_entries(archive)


def test_read_wheel_entries_refuses_truncated_deflate_stream() -> None:
    payload = _deflate(APPROVED)[:-3]
    archive = _build_zip("pkg/mod.py", zipfile.ZIP_DEFLATED, payload, usize=len(APPROVED), crc=zlib.crc32(APPROVED))
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(archive)


def test_read_wheel_entries_refuses_stored_size_mismatch() -> None:
    archive = _build_zip(
        "pkg/mod.py",
        zipfile.ZIP_STORED,
        APPROVED,
        usize=len(APPROVED),
        crc=zlib.crc32(APPROVED),
        csize=len(APPROVED) + 4,
    )
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(archive)


def test_read_wheel_entries_refuses_unsupported_compression() -> None:
    archive = _build_zip("pkg/mod.py", 12, APPROVED, usize=len(APPROVED), crc=zlib.crc32(APPROVED))
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(archive)


def test_read_wheel_entries_refuses_overlong_deflate_output_without_unbounded_inflation() -> None:
    bomb = _deflate(b"\x00" * (8 << 20))
    archive = _build_zip("pkg/mod.py", zipfile.ZIP_DEFLATED, bomb, usize=4, crc=zlib.crc32(b"\x00" * 4))
    with pytest.raises(promotion.PromotionError):
        promotion._read_wheel_entries(archive)


def _tar(names: list[tuple[str, bool]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for name, is_dir in names:
            info = tarfile.TarInfo(name)
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = stat.S_IFDIR | 0o755
                bundle.addfile(info)
            else:
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


def test_extract_tar_tree_counts_directories_toward_member_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_ARCHIVE_MEMBERS", 3)
    archive = _tar([("d1", True), ("d2", True), ("d3", True), ("d4", True)])
    with pytest.raises(promotion.PromotionError, match="file count"):
        promotion.extract_tar_tree(archive)


def test_extract_tar_tree_counts_filtered_entries_toward_member_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_ARCHIVE_MEMBERS", 2)
    archive = _tar([("skip/a", False), ("skip/b", False), ("keep/c", False)])
    with pytest.raises(promotion.PromotionError, match="file count"):
        promotion.extract_tar_tree(archive, prefixes=("keep",))


def test_scoped_consumer_source_keeps_its_own_file_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_TREE_FILES", 1)
    archive = _tar([("skip/a", False), ("skip/b", False), ("keep/c", False)])
    assert promotion.extract_tar_tree(archive, prefixes=("keep",)) == {"keep/c": b"x"}


def test_filtered_source_bytes_still_count_toward_the_input_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_ARCHIVE_BYTES", 2)
    archive = _tar([("skip/a", False), ("skip/b", False), ("keep/c", False)])
    with pytest.raises(promotion.PromotionError):
        promotion.extract_tar_tree(archive, prefixes=("keep",))


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    script = tmp_path / "gh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_gh_bytes_capped_returns_payload_within_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(tmp_path, monkeypatch, "printf abcde")
    assert github._gh_bytes_capped("x", limit=5) == b"abcde"


def test_capped_reader_waits_for_success_after_the_process_closes_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, monkeypatch, "printf abcde; exec 1>&-; sleep 0.1; exit 0")
    assert github._gh_bytes_capped("x", limit=5) == b"abcde"


def test_gh_bytes_capped_refuses_oversized_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(tmp_path, monkeypatch, "yes")
    with pytest.raises(github.GitHubError, match="exceeds"):
        github._gh_bytes_capped("x", limit=1000)


def test_gh_bytes_capped_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(tmp_path, monkeypatch, "echo boom >&2; exit 1")
    with pytest.raises(github.GitHubError, match="boom"):
        github._gh_bytes_capped("x", limit=10)


def test_default_source_loader_caps_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "MAX_TREE_BYTES", 100)
    _fake_gh(tmp_path, monkeypatch, "yes")
    monkeypatch.delenv(promotion.SOURCES_ENV, raising=False)
    loader = promotion.default_source_loader()
    with pytest.raises(promotion.PromotionError, match="exceeds"):
        loader("owner/repo", "a" * 40, None)
