"""P2-41 closure S2: ENOSPC, EIO and zero-byte-write tests of two storage
helpers that keep their own temp-fsync-publish code: `storage_host._write_new`
and `archive_bundle._write_new`.

`DECISIONS.md` (orchestrator, 2026-10-08) requires them before P2-41 is lifted:
the writer scan lists these helpers as `OWN_DURABLE`, not as users of
`durable_write`, so nothing else says that a full disk leaves no half-written
file under a final name. A proxy installed as the module's `os` injects the
fault; everything else is the real `os`. Every test names what it catches.
Fault cases come with a control that changes only the condition under test.

Stated limit: `storage_host._write_new` writes through a text file object
(`os.fdopen`), so a zero-byte write cannot be injected there through `os`; the
zero-byte case is covered only for `archive_bundle._write_new`, whose loop did
spin before the guard in this change.

No disk is filled, no subprocess is started, nothing writes outside `tmp_path`.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

import hoh.archive_bundle as ab
from hoh import storage_host

# --- the seam ----------------------------------------------------------------

class _FaultyOS:
    """Stands in for `os` inside one module only; overrides win."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


def _fd_name(fd: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return ""


def _is_reg(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _err_write_for(prefix: str, err: int = errno.ENOSPC):
    real = os.write

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            raise OSError(err, os.strerror(err))
        return real(fd, data)
    return write


def _zero_write_for(prefix: str = "", limit: int = 50):
    """`os.write` returns 0 for `prefix*`; the test fails after `limit` calls, so
    a regression that spins fails instead of hanging."""
    real = os.write
    calls = {"n": 0}

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            calls["n"] += 1
            if calls["n"] > limit:
                pytest.fail(f"os.write was called {limit}+ times: the loop spins")
            return 0
        return real(fd, data)
    return write


class _FullFile:
    """A text file whose `write` runs out of room."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False

    def write(self, _data):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()


# ==== storage_host._write_new ========================================================

def test_storage_host_write_new_full_disk_publishes_nothing(tmp_path, monkeypatch):
    """Catches: a helper that writes at its final name, so a full disk leaves a
    half-written `inv.json`, or that replaces an existing file. The final name
    is published by `os.replace` of a finished temp onto a name that does not
    exist: after the failure `inv.json` is absent, `keep.json` still reads
    `old`, and everything else in the directory is a `.tmp.` temp. Control: after
    `undo` the same call writes."""
    (tmp_path / "keep.json").write_text("old", encoding="utf-8")
    monkeypatch.setattr(storage_host, "os", _FaultyOS(
        fdopen=lambda fd, *a, **kw: _FullFile(os.fdopen(fd, *a, **kw))))
    with pytest.raises(OSError) as caught:
        storage_host._write_new(tmp_path, "inv.json", "payload")
    assert caught.value.errno == errno.ENOSPC
    with pytest.raises(OSError) as again:
        storage_host._write_new(tmp_path, "keep.json", "payload")
    assert again.value.errno == errno.ENOSPC
    assert not (tmp_path / "inv.json").exists()
    assert (tmp_path / "keep.json").read_text(encoding="utf-8") == "old"
    others = sorted(p.name for p in tmp_path.iterdir() if p.name != "keep.json")
    assert others and all(".tmp." in n for n in others), others

    monkeypatch.undo()
    written = storage_host._write_new(tmp_path, "inv.json", "payload")
    assert written == tmp_path / "inv.json"
    assert written.read_text(encoding="utf-8") == "payload"


def test_storage_host_write_new_fsync_error_publishes_nothing(tmp_path, monkeypatch):
    """Catches: a helper that publishes before the file is fsynced, or that
    swallows the fsync error. An EIO at the fsync of the temp file is an
    `OSError` and no `inv.json` exists. Control: after `undo` the call writes."""
    real = os.fsync

    def fsync(fd):
        if _is_reg(fd):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(fd)

    monkeypatch.setattr(storage_host, "os", _FaultyOS(fsync=fsync))
    with pytest.raises(OSError) as caught:
        storage_host._write_new(tmp_path, "inv.json", "payload")
    assert caught.value.errno == errno.EIO
    assert not (tmp_path / "inv.json").exists()

    monkeypatch.undo()
    storage_host._write_new(tmp_path, "inv.json", "payload")
    assert (tmp_path / "inv.json").read_text(encoding="utf-8") == "payload"


# ==== archive_bundle._write_new ======================================================

def test_archive_bundle_write_new_full_disk_raises_closes_the_descriptor_and_never_replaces(
        tmp_path, monkeypatch):
    """Catches (X8): a helper opened without `O_EXCL`, which would write over an
    existing file; a full disk that is swallowed; a descriptor left open on the
    failure. Control: after `undo` a fresh path is written."""
    existing = tmp_path / "exists.bin"
    existing.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        ab._write_new(existing, b"other")
    assert existing.read_bytes() == b"precious"

    fresh = tmp_path / "fresh.bin"
    before = _open_fds()
    monkeypatch.setattr(ab, "os", _FaultyOS(write=_err_write_for("fresh.bin")))
    with pytest.raises(OSError) as caught:
        ab._write_new(fresh, b"payload")
    assert caught.value.errno == errno.ENOSPC
    assert _open_fds() == before
    assert existing.read_bytes() == b"precious"

    monkeypatch.undo()
    other = tmp_path / "other.bin"
    ab._write_new(other, b"payload")
    assert other.read_bytes() == b"payload"


def test_archive_bundle_write_new_zero_byte_write_fails_instead_of_spinning(
        tmp_path, monkeypatch):
    """Catches (X7): a write loop `view = view[written:]` that never ends on a
    write returning 0. The fake fails the test after 50 calls, so a regression
    fails instead of hanging; the guard turns it into `OSError(ENOSPC)`."""
    target = tmp_path / "zero.bin"
    before = _open_fds()
    monkeypatch.setattr(ab, "os", _FaultyOS(write=_zero_write_for("zero.bin")))
    with pytest.raises(OSError) as caught:
        ab._write_new(target, b"payload")
    assert caught.value.errno == errno.ENOSPC
    assert _open_fds() == before


def _small_bundle(tmp_path: Path, name: str = "out") -> Path:
    pytest.importorskip("zstandard")
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "f").write_bytes(b"x")
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    return ab.ArchiveBundle.build_tree(
        source, out, label="t", meta={"project_id": "p", "run_id": "r"},
        ownership={"status": "OWNED", "root": str(tmp_path)})


def test_archive_receipt_full_disk_leaves_no_receipt_under_its_name(tmp_path, monkeypatch):
    """Catches: a receipt written at its final name, so a full disk leaves a
    half-written `ARCHIVE_RECEIPT.json` that later reads as a receipt. It is
    written to a hidden temp name and renamed into place only afterwards.
    Control: with room it is written and reads `ARCHIVED_VERIFIED`."""
    bundle = _small_bundle(tmp_path)
    root = tmp_path / "extract-1"
    root.mkdir()
    monkeypatch.setattr(ab, "os", _FaultyOS(write=_err_write_for("." + ab.RECEIPT)))
    with pytest.raises(OSError) as caught:
        ab.write_receipt(bundle, extract_root=root, destination={"kind": "test"})
    assert caught.value.errno == errno.ENOSPC
    assert not (bundle / ab.RECEIPT).exists()

    monkeypatch.undo()
    root2 = tmp_path / "extract-2"
    root2.mkdir()
    receipt = ab.write_receipt(bundle, extract_root=root2, destination={"kind": "test"})
    assert json.loads(receipt.read_text(encoding="ascii"))["status"] == ab.ARCHIVED_VERIFIED


def test_archive_build_full_disk_leaves_no_bundle_under_a_final_name(tmp_path, monkeypatch):
    """Catches: a build that writes the manifest into a directory that already
    has its final name. A full disk at the `MANIFEST.json` write raises, and the
    output directory holds nothing whose name does not end in `.building` (the
    staging directory, marked incomplete). Control: with room the build leaves a
    bundle under a final name."""
    pytest.importorskip("zstandard")
    monkeypatch.setattr(ab, "os", _FaultyOS(write=_err_write_for(ab.MANIFEST)))
    with pytest.raises(OSError) as caught:
        _small_bundle(tmp_path)
    assert caught.value.errno == errno.ENOSPC
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names and all(n.endswith(".building") for n in names), names

    monkeypatch.undo()
    bundle = _small_bundle(tmp_path, "out2")
    assert not bundle.name.endswith(".building")
