"""P2-41 / STORAGE-B1: `durable_io.durable_write` against real short writes.

Every test names the selector it is run by and says which failure it catches.
Each fault case comes with a negative control that changes only the condition
under test. Fault injection goes through a proxy installed as `durable_io.os`,
so a broken `os.write`/`os.fsync` hits the code under test and never pytest.
Tests write only under `tmp_path`.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from hoh import durable_io, store
from hoh.durable_io import (DiskFullError, DurableWriteNotSynced,
                             _abandon_own_temp, durable_write)
from hoh.timers import Timer, TimerKind, TimerStore

SRC = Path(__file__).resolve().parents[1] / "src"
OLD = b"old record bytes\n"
NEW = b"new record bytes, longer than the old ones\n"


def _tmp_entries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp." in p.name)


class _FaultyOS:
    """Stands in for `os` inside `hoh.durable_io` only; overrides win."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


def _is_regular(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


def _install(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(durable_io, "os", _FaultyOS(**overrides))


def _statvfs_reporting(free_bytes: int):
    def fake(_path):
        return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1,
                                     f_bsize=1, f_blocks=free_bytes,
                                     f_bfree=free_bytes)
    return fake


# --- K1: a real short write (RLIMIT_FSIZE) ---------------------------------

_CHILD = textwrap.dedent("""
    import resource, signal, sys
    from pathlib import Path
    from hoh.durable_io import DiskFullError, durable_write

    target, size, limit = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    try:
        durable_write(target, b"n" * size, min_free_bytes=0)
    except DiskFullError as exc:
        print("DiskFullError", exc.errno, exc.needed_bytes)
        sys.exit(0)
    except BaseException as exc:
        print("OTHER", type(exc).__name__, exc)
        sys.exit(2)
    print("WROTE")
    sys.exit(0)
""")


def _run_child(target: Path, size: int, limit: int) -> str:
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(target), str(size), str(limit)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode in (0, 2), proc.stderr
    return proc.stdout.strip()


def test_short_write_rlimit_keeps_target(tmp_path):
    """Catches: a real short write (file-size limit hit mid-payload) that
    truncates or replaces the existing record, leaves a temp file, or surfaces
    as anything but DiskFullError. Control: a limit above the payload writes
    the new bytes."""
    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    size = 64 * 1024

    out = _run_child(target, size, limit=4096)
    assert out.split()[:2] == ["DiskFullError", str(errno.ENOSPC)], out
    assert target.read_bytes() == OLD
    assert _tmp_entries(tmp_path) == []

    # Control: only the limit changes.
    out = _run_child(target, size, limit=size * 4)
    assert out == "WROTE", out
    assert target.read_bytes() == b"n" * size
    assert _tmp_entries(tmp_path) == []


def test_short_write_absent_target_stays_absent(tmp_path):
    """Catches: a failed short write that creates the target (empty or
    partial) where none existed. Control: a limit above the payload creates
    it with the full bytes."""
    target = tmp_path / "new.json"
    size = 64 * 1024

    out = _run_child(target, size, limit=4096)
    assert out.startswith("DiskFullError"), out
    assert not target.exists()
    assert _tmp_entries(tmp_path) == []

    out = _run_child(target, size, limit=size * 4)
    assert out == "WROTE", out
    assert target.read_bytes() == b"n" * size


# --- K2: the free-space pre-check ------------------------------------------

def test_precheck_refuses_before_open(tmp_path, monkeypatch):
    """Catches: a pre-check that runs after the temp file is opened (or not
    at all), so a full disk still creates or touches something in the record
    directory. Controls: the same guards with enough space reach `os.open`;
    without the guard, enough space writes the new bytes."""
    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    before_list = sorted(os.listdir(tmp_path))
    before_mtime = os.stat(tmp_path).st_mtime_ns

    def open_must_not_be_called(*_a, **_k):
        raise AssertionError("os.open called before the free-space check refused")

    with monkeypatch.context() as m:
        m.setattr(os, "statvfs", _statvfs_reporting(100))
        m.setattr(os, "open", open_must_not_be_called)
        with pytest.raises(DiskFullError) as info:
            durable_write(target, NEW, min_free_bytes=1000)
    err = info.value
    assert isinstance(err, OSError) and err.errno == errno.ENOSPC
    assert err.needed_bytes == len(NEW) + 1000
    assert err.free_bytes == 100
    assert err.path == target
    assert sorted(os.listdir(tmp_path)) == before_list
    assert os.stat(tmp_path).st_mtime_ns == before_mtime
    assert target.read_bytes() == OLD

    # Control 1: only the reported space changes -> the guard on os.open fires.
    with monkeypatch.context() as m:
        m.setattr(os, "statvfs", _statvfs_reporting(len(NEW) + 1000))
        m.setattr(os, "open", open_must_not_be_called)
        with pytest.raises(AssertionError, match="os.open called"):
            durable_write(target, NEW, min_free_bytes=1000)

    # Control 2: enough space, no guard -> the new bytes are in place.
    with monkeypatch.context() as m:
        m.setattr(os, "statvfs", _statvfs_reporting(len(NEW) + 1000))
        durable_write(target, NEW, min_free_bytes=1000)
    assert target.read_bytes() == NEW
    assert _tmp_entries(tmp_path) == []


# --- K3: the abandon path removes only its own temp ------------------------

def _own_temp(directory: Path, name: str = ".x.tmp.1.abc") -> tuple[int, Path]:
    tmp = directory / name
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, b"partial")
    return fd, tmp


def test_own_temp_foreign_inode_kept(tmp_path, monkeypatch):
    """Catches: an abandon that removes by name alone, so a different file
    swapped in at the temp path (a different inode) would be removed.
    Control: test_own_temp_removed, where the inode matches."""
    # Direct: own temp parked aside, a foreign file renamed in at its name.
    fd, tmp = _own_temp(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign bytes")
    os.rename(tmp, tmp_path / "parked")
    os.rename(foreign, tmp)
    assert _abandon_own_temp(fd, tmp) is False
    assert tmp.read_bytes() == b"foreign bytes"

    # Through durable_write: the swap happens between write and abandon.
    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    real_write = os.write
    swapped: list[Path] = []

    def write_then_swap(fd_, data):
        if _is_regular(fd_) and not swapped:
            real_write(fd_, data)
            (name,) = [p for p in tmp_path.iterdir() if p.name.startswith(".state.json.tmp.")]
            other = tmp_path / "other"
            other.write_bytes(b"swapped in")
            os.rename(name, tmp_path / "parked2")
            os.rename(other, name)
            swapped.append(name)
            raise OSError(errno.EIO, "injected after swap")
        return real_write(fd_, data)

    _install(monkeypatch, write=write_then_swap)
    with pytest.raises(OSError):
        durable_write(target, NEW, min_free_bytes=0)
    assert swapped and swapped[0].read_bytes() == b"swapped in"
    assert target.read_bytes() == OLD


def test_own_temp_removed(tmp_path, monkeypatch):
    """Catches: an abandon that leaves this call's own partial temp file
    behind. Control: test_own_temp_foreign_inode_kept (inode mismatch)."""
    fd, tmp = _own_temp(tmp_path)
    assert _abandon_own_temp(fd, tmp) is True
    assert not tmp.exists()
    with pytest.raises(OSError):           # the fd was closed by the abandon
        os.fstat(fd)

    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    _install(monkeypatch, write=_fail_write_on_regular(errno.ENOSPC))
    with pytest.raises(DiskFullError):
        durable_write(target, NEW, min_free_bytes=0)
    assert _tmp_entries(tmp_path) == []
    assert target.read_bytes() == OLD


# --- K4a: the run store and the timer store route through it ---------------

def _recorder(monkeypatch) -> list:
    calls: list = []

    def record(path, payload, **kwargs):
        calls.append((Path(path), payload, kwargs))

    monkeypatch.setattr(durable_io, "durable_write", record)
    return calls


def test_wired_store(tmp_path, monkeypatch):
    """Catches: store._atomic_write writing on its own instead of through
    durable_io.durable_write (or with the wrong path, payload or mode), and
    losing the parent mkdir."""
    calls = _recorder(monkeypatch)
    path = tmp_path / "runs" / "r1" / "state.json"
    store._atomic_write(path, '{"a": 1}')
    assert calls == [(path, '{"a": 1}', {"mode": 0o644})]
    assert path.parent.is_dir()
    assert not path.exists()               # the recorder wrote nothing


def _timer(timer_id: str = "t1") -> Timer:
    return Timer(timer_id=timer_id, due_at="2026-10-01T00:00:00Z",
                 kind=TimerKind.PROVIDER_RETRY)


def test_wired_timers(tmp_path, monkeypatch):
    """Catches: TimerStore.write writing on its own instead of through
    durable_io.durable_write, or with the wrong path, payload or mode."""
    calls = _recorder(monkeypatch)
    ts = TimerStore(tmp_path / "timers")
    ts.write([_timer()])
    assert len(calls) == 1
    path, payload, kwargs = calls[0]
    assert path == ts.path
    assert kwargs == {"mode": 0o600}
    assert payload.endswith("}\n")
    doc = json.loads(payload)
    assert set(doc) == {"schema_version", "written_at", "timers"}
    assert [t["timer_id"] for t in doc["timers"]] == ["t1"]
    assert payload == json.dumps(doc, indent=2) + "\n"
    assert not ts.path.exists()


def test_timer_wired_leaves_no_tmp(tmp_path, monkeypatch):
    """Catches: a timer write that leaves a '.tmp.' entry behind on success or
    on a failed replace, or that changes timers.json when the replace fails.
    Control: the same write without the failing replace succeeds."""
    ts = TimerStore(tmp_path / "timers")
    ts.write([_timer("t1")])
    assert _tmp_entries(ts.root) == []
    old = ts.path.read_bytes()

    def failing_replace(*_a, **_k):
        raise OSError(errno.EIO, "injected replace failure")

    _install(monkeypatch, replace=failing_replace)
    with pytest.raises(OSError):
        ts.write([_timer("t1"), _timer("t2")])
    assert _tmp_entries(ts.root) == []
    assert ts.path.read_bytes() == old

    monkeypatch.setattr(durable_io, "os", os)
    ts.write([_timer("t1"), _timer("t2")])
    assert [t.timer_id for t in ts.read()] == ["t1", "t2"]
    assert _tmp_entries(ts.root) == []


# --- K5: explicit final modes ----------------------------------------------

@pytest.mark.parametrize("umask", [0o000, 0o022])
def test_modes_preserved(tmp_path, umask):
    """Catches: a final mode left to the umask -- the run-store file must be
    0o644 and timers.json 0o600 whatever the umask is."""
    previous = os.umask(umask)
    try:
        run_file = tmp_path / "runs" / "r1" / "state.json"
        store._atomic_write(run_file, "{}")
        ts = TimerStore(tmp_path / "timers")
        ts.write([_timer()])
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(run_file).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(ts.path).st_mode) == 0o600
    # Rewriting an existing file still ends at the explicit mode.
    os.chmod(run_file, 0o600)
    store._atomic_write(run_file, "{}")
    assert stat.S_IMODE(os.stat(run_file).st_mode) == 0o644


# --- Fault injections with controls ----------------------------------------

def _fail_write_on_regular(err: int):
    real = os.write

    def write(fd, data):
        if _is_regular(fd):
            raise OSError(err, os.strerror(err))
        return real(fd, data)
    return write


#: A loop that retries a zero-byte write forever must fail the test, not hang it.
_ZERO_WRITE_CAP = 100


def _zero_writes(first_bytes: int = 0):
    """Writes `first_bytes` once, then returns 0 -- until the cap, after which
    a retrying write loop is reported instead of spinning forever."""
    state = {"n": 0}

    def write(fd, data):
        if not _is_regular(fd):
            return os.write(fd, data)
        state["n"] += 1
        if state["n"] > _ZERO_WRITE_CAP:
            raise AssertionError("zero-byte write retried instead of failing")
        if state["n"] == 1 and first_bytes:
            return os.write(fd, bytes(data[:first_bytes]))
        return 0
    return write


def _fail_fsync_on_regular(fd):
    if _is_regular(fd):
        raise OSError(errno.EIO, "injected fsync failure")
    return os.fsync(fd)


def _enospc_fsync_on_regular(fd):
    if _is_regular(fd):
        raise OSError(errno.ENOSPC, "injected fsync ENOSPC")
    return os.fsync(fd)


def _fail_replace(*_a, **_k):
    raise OSError(errno.EIO, "injected replace failure")


FAULTS = {
    "enospc_write": ({"write": _fail_write_on_regular(errno.ENOSPC)}, DiskFullError),
    "edquot_write": ({"write": _fail_write_on_regular(errno.EDQUOT)}, DiskFullError),
    "efbig_write": ({"write": _fail_write_on_regular(errno.EFBIG)}, DiskFullError),
    "zero_write": (None, DiskFullError),
    "short_then_zero_write": (5, DiskFullError),
    "eio_write": ({"write": _fail_write_on_regular(errno.EIO)}, OSError),
    "fsync_error": ({"fsync": _fail_fsync_on_regular}, OSError),
    "enospc_fsync": ({"fsync": _enospc_fsync_on_regular}, DiskFullError),
    "replace_error": ({"replace": _fail_replace}, OSError),
}


@pytest.mark.parametrize("absent", [False, True], ids=["existing", "absent"])
@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_injection_target_unchanged(tmp_path, monkeypatch, fault, absent):
    """Catches: a failure before the replace (ENOSPC/EDQUOT/EFBIG/EIO at
    os.write, a zero-byte or short-then-zero write, an error at fsync or at
    os.replace) that changes or creates the target, leaves a temp file, or is
    mapped to the wrong error (space errors -> DiskFullError, EIO stays a
    plain OSError). Control: test_fault_injection_control."""
    overrides, expected = FAULTS[fault]
    if not isinstance(overrides, dict):     # zero-write fakes keep per-test state
        overrides = {"write": _zero_writes(first_bytes=overrides or 0)}
    target = tmp_path / "state.json"
    if not absent:
        target.write_bytes(OLD)
    _install(monkeypatch, **overrides)
    with pytest.raises(OSError) as info:
        durable_write(target, NEW, min_free_bytes=0)
    assert type(info.value) is expected
    if expected is DiskFullError:
        assert info.value.errno == errno.ENOSPC
        assert info.value.needed_bytes == len(NEW)
    if absent:
        assert not target.exists()
    else:
        assert target.read_bytes() == OLD
    assert _tmp_entries(tmp_path) == []


@pytest.mark.parametrize("absent", [False, True], ids=["existing", "absent"])
def test_fault_injection_control(tmp_path, monkeypatch, absent):
    """Catches: a primitive that fails even without a fault (so the fault
    tests would pass vacuously). Same proxy, no override: new bytes in place."""
    target = tmp_path / "state.json"
    if not absent:
        target.write_bytes(OLD)
    _install(monkeypatch)
    durable_write(target, NEW, min_free_bytes=0)
    assert target.read_bytes() == NEW
    assert _tmp_entries(tmp_path) == []


def test_fault_short_writes_are_progress(tmp_path, monkeypatch):
    """Catches: a write loop that treats a short (but non-zero) write as the
    end and ships a truncated record. Control for short_then_zero_write."""
    real = os.write

    def three_bytes_at_a_time(fd, data):
        if _is_regular(fd):
            return real(fd, bytes(data[:3]))
        return real(fd, data)

    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    _install(monkeypatch, write=three_bytes_at_a_time)
    durable_write(target, NEW, min_free_bytes=0)
    assert target.read_bytes() == NEW


def test_fault_interrupt_is_reraised_unchanged(tmp_path, monkeypatch):
    """Catches: a signal-driven exception (KeyboardInterrupt) mid-write that
    is swallowed or remapped, or that leaves a temp file / changed target."""
    def interrupted(fd, data):
        if _is_regular(fd):
            raise KeyboardInterrupt
        return os.write(fd, data)

    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    _install(monkeypatch, write=interrupted)
    with pytest.raises(KeyboardInterrupt):
        durable_write(target, NEW, min_free_bytes=0)
    assert target.read_bytes() == OLD
    assert _tmp_entries(tmp_path) == []


# --- Behaviour on success ---------------------------------------------------

def test_success_str_encoded_and_temp_named(tmp_path, monkeypatch):
    """Catches: str payloads not encoded as UTF-8, a temp file outside the
    target's directory or not named `.<name>.tmp.<pid>.<random>`, a temp not
    opened with O_EXCL, or a missing directory fsync."""
    opened: list[tuple[str, int]] = []
    fsynced: list[bool] = []
    real_open, real_fsync = os.open, os.fsync

    def spy_open(path, flags, *args, **kwargs):
        opened.append((str(path), flags))
        return real_open(path, flags, *args, **kwargs)

    def spy_fsync(fd):
        fsynced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real_fsync(fd)

    target = tmp_path / "state.json"
    _install(monkeypatch, open=spy_open, fsync=spy_fsync)
    durable_write(target, "grüße\n", min_free_bytes=0)
    assert target.read_bytes() == "grüße\n".encode("utf-8")
    tmp_name, flags = opened[0]
    tmp = Path(tmp_name)
    assert tmp.parent == tmp_path
    assert tmp.name.startswith(f".state.json.tmp.{os.getpid()}.")
    assert len(tmp.name) > len(f".state.json.tmp.{os.getpid()}.")
    want = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    assert flags & want == want
    assert fsynced == [False, True]         # the file, then the directory


def test_success_does_not_create_directories(tmp_path):
    """Catches: durable_write creating missing parent directories (that is
    the caller's job, e.g. store._atomic_write)."""
    with pytest.raises(FileNotFoundError):
        durable_write(tmp_path / "missing" / "x.json", b"x", min_free_bytes=0)
    assert not (tmp_path / "missing").exists()


def test_success_store_bytes_identical(tmp_path):
    """Catches: store._atomic_write changing the bytes or the final path it
    writes (it must match the old `open(..., "w", encoding="utf-8")`)."""
    path = tmp_path / "a" / "b" / "state.json"
    text = '{"x": "ä"}\n'
    store._atomic_write(path, text)
    assert path.read_bytes() == text.encode("utf-8")
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


# --- The one removal ---------------------------------------------------------

_REMOVALS = {"unlink", "remove", "rmdir", "rmtree", "removedirs"}


def test_source_scan_single_removal_in_abandon():
    """Catches: any removal call in durable_io.py other than the single
    sanctioned one inside _abandon_own_temp."""
    source = Path(durable_io.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.attr if isinstance(callee, ast.Attribute) else \
                    getattr(callee, "id", None)
                if name in _REMOVALS:
                    found.append(func.name)
    total = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr in _REMOVALS)
        or (isinstance(node.func, ast.Name) and node.func.id in _REMOVALS)))
    assert total == 1
    assert found == ["_abandon_own_temp"]


# --- K7: the foreign-data guard of _abandon_own_temp, pinned ---------------

def test_own_temp_symlink_swap_kept(tmp_path):
    """Catches: an abandon whose stat follows symlinks. A symlink swapped in
    at the temp name, pointing at this call's own (parked) temp, then looks
    like the own inode and the link -- not ours -- would be unlinked.
    Control: the same setup without the swap removes the own temp."""
    fd, tmp = _own_temp(tmp_path)
    parked = tmp_path / "parked"
    os.rename(tmp, parked)
    os.symlink(parked, tmp)
    assert _abandon_own_temp(fd, tmp) is False
    assert tmp.is_symlink() and os.readlink(tmp) == str(parked)
    assert parked.read_bytes() == b"partial"

    # Control: only the swap is missing.
    fd, tmp = _own_temp(tmp_path, ".y.tmp.1.abc")
    assert _abandon_own_temp(fd, tmp) is True
    assert not os.path.lexists(tmp)


def _swap_parent_after_stat(parent: Path, foreign_bytes: bytes):
    """A stat for durable_io that, once it has answered, moves `parent` aside
    and puts a fresh directory with a foreign file at the same path."""
    moved = parent.with_name(parent.name + ".moved")

    def stat_then_swap(name, *args, **kwargs):
        result = os.stat(name, *args, **kwargs)
        os.rename(parent, moved)
        parent.mkdir()
        (parent / name).write_bytes(foreign_bytes)
        return result
    return stat_then_swap, moved


def test_own_temp_parent_swap_kept(tmp_path, monkeypatch):
    """Catches: an abandon that unlinks by path (`os.unlink(tmp_path)`)
    instead of relative to the directory fd it compared in. When the parent
    directory is swapped between the stat and the unlink, the path names a
    foreign file in the new directory; it must be kept, and the own temp (in
    the moved directory) removed. Control: no swap -> own temp removed."""
    parent = tmp_path / "records"
    parent.mkdir()
    fd, tmp = _own_temp(parent)
    stat_then_swap, moved = _swap_parent_after_stat(parent, b"foreign record")
    _install(monkeypatch, stat=stat_then_swap)
    assert _abandon_own_temp(fd, tmp) is True
    assert tmp.read_bytes() == b"foreign record"       # the foreign file stays
    assert not (moved / tmp.name).exists()             # our own temp is gone

    # Control: only the swap is missing.
    monkeypatch.setattr(durable_io, "os", os)
    other = tmp_path / "records2"
    other.mkdir()
    fd, tmp = _own_temp(other)
    assert _abandon_own_temp(fd, tmp) is True
    assert not tmp.exists()


def _stat_on_device(delta: int):
    """A stat for durable_io that reports the real inode on st_dev + delta."""
    def fake(name, *args, **kwargs):
        real = os.stat(name, *args, **kwargs)
        return types.SimpleNamespace(st_dev=real.st_dev + delta, st_ino=real.st_ino,
                                     st_mode=real.st_mode)
    return fake


def test_own_temp_other_device_kept(tmp_path, monkeypatch):
    """Catches: an abandon that compares st_ino only. The same inode number
    on another device is a different file and must be kept. Control: the same
    fake with the same device -> the own temp is removed."""
    fd, tmp = _own_temp(tmp_path)
    _install(monkeypatch, stat=_stat_on_device(1))
    assert _abandon_own_temp(fd, tmp) is False
    assert tmp.read_bytes() == b"partial"

    # Control: only the device changes back.
    fd, tmp = _own_temp(tmp_path, ".y.tmp.1.abc")
    _install(monkeypatch, stat=_stat_on_device(0))
    assert _abandon_own_temp(fd, tmp) is True
    assert not tmp.exists()


def test_precheck_uses_bavail_frsize(tmp_path, monkeypatch):
    """Catches: a free-space pre-check that counts f_bfree (blocks reserved
    for root, the ext4 reserve) or multiplies by f_bsize instead of f_frsize.
    The fake makes both wrong formulas admit the write and only
    f_bavail * f_frsize refuse it. Control: one more available fragment
    admits the write."""
    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    min_free = 1000
    needed = len(NEW) + min_free

    def fake(bavail: int):
        def statvfs(_path):
            return types.SimpleNamespace(f_bavail=bavail, f_bfree=needed * 2,
                                         f_frsize=1, f_bsize=4096,
                                         f_blocks=needed * 4)
        return statvfs

    bavail = needed - 1
    sv = fake(bavail)(None)
    assert sv.f_bavail * sv.f_bsize >= needed > sv.f_bavail * sv.f_frsize
    assert sv.f_bfree * sv.f_frsize >= needed

    _install(monkeypatch, statvfs=fake(bavail))
    with pytest.raises(DiskFullError) as info:
        durable_write(target, NEW, min_free_bytes=min_free)
    assert info.value.free_bytes == bavail * 1
    assert info.value.needed_bytes == needed
    assert target.read_bytes() == OLD
    assert _tmp_entries(tmp_path) == []

    # Control: only f_bavail changes, by one fragment.
    _install(monkeypatch, statvfs=fake(needed))
    durable_write(target, NEW, min_free_bytes=min_free)
    assert target.read_bytes() == NEW


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_no_fd_leak_after_many_writes(tmp_path):
    """Catches: a descriptor left open on success (the temp fd or the
    directory fd), which over a long unattended run exhausts the fd table.
    Control: one deliberately leaked descriptor is seen by the same count."""
    target = tmp_path / "state.json"
    durable_write(target, b"warm-up", min_free_bytes=0)
    before = _open_fds()
    for i in range(50):
        durable_write(target, f"record {i}\n", min_free_bytes=0)
    assert _open_fds() == before
    assert target.read_bytes() == b"record 49\n"

    # Control: the count does see a single leaked descriptor.
    leaked = os.open(tmp_path, os.O_RDONLY)
    try:
        assert _open_fds() == before + 1
    finally:
        os.close(leaked)


# --- K8: after the replace; the source scan -------------------------------

@pytest.mark.parametrize("err", [errno.EIO, errno.ENOSPC], ids=["eio", "enospc"])
def test_dir_fsync_failure_after_replace_named(tmp_path, monkeypatch, err):
    """Catches: a directory fsync failure after the replace that is swallowed,
    surfaces as a plain OSError, or -- for ENOSPC -- as DiskFullError (which
    would claim the target is untouched while it already holds the new
    bytes). It must be DurableWriteNotSynced, new bytes in place, no temp.
    Control: the same proxy without the fault succeeds silently."""
    def fsync_dir_fails(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, os.strerror(err))
        return os.fsync(fd)

    target = tmp_path / "state.json"
    target.write_bytes(OLD)
    _install(monkeypatch, fsync=fsync_dir_fails)
    with pytest.raises(DurableWriteNotSynced) as info:
        durable_write(target, NEW, min_free_bytes=0)
    exc = info.value
    assert isinstance(exc, OSError) and not isinstance(exc, DiskFullError)
    assert exc.errno == err
    assert "in place" in str(exc) and "may not be durable" in str(exc)
    assert isinstance(exc.__cause__, OSError)
    assert target.read_bytes() == NEW
    assert _tmp_entries(tmp_path) == []

    # Control: only the fault is missing.
    _install(monkeypatch)
    durable_write(target, OLD, min_free_bytes=0)
    assert target.read_bytes() == OLD


#: Names whose reference, in any form, counts as a removal.
_REMOVAL_NAMES = {"unlink", "remove", "rmdir", "rmtree", "removedirs"}
#: Modules through which removals are reachable indirectly.
_REMOVAL_MODULES = {"os", "shutil"}


def _scan_removals(source: str) -> tuple[list[str], list[str]]:
    """Return (functions holding a removal reference, one per reference;
    forbidden indirect forms found). A reference is any attribute access or
    name with a removal name (call, alias or bare), any import of one, any
    string constant equal to one, and any import of shutil. Indirect forms are
    getattr(os|shutil, ...), vars(os|shutil) and os|shutil.__dict__."""
    tree = ast.parse(source)
    owner: dict[int, str] = {}
    # ast.walk is breadth-first, so an inner function overwrites its outer one.
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                owner[id(node)] = func.name

    refs: list[str] = []
    indirect: list[str] = []
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_nodes.add(id(body[0].value))

    def where(node) -> str:
        return owner.get(id(node), "<module>")

    def is_mod(node) -> bool:
        return isinstance(node, ast.Name) and node.id in _REMOVAL_MODULES

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _REMOVAL_NAMES:
                refs.append(where(node))
            if node.attr == "__dict__" and is_mod(node.value):
                indirect.append(f"{node.value.id}.__dict__")
        elif isinstance(node, ast.Name) and node.id in _REMOVAL_NAMES:
            refs.append(where(node))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in _REMOVAL_NAMES or alias.name.split(".")[0] == "shutil":
                    refs.append(where(node))
            if isinstance(node, ast.ImportFrom) and node.module == "shutil":
                refs.append(where(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in _REMOVAL_NAMES and id(node) not in doc_nodes:
            refs.append(where(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in {"getattr", "vars"} and node.args and is_mod(node.args[0]):
            indirect.append(f"{node.func.id}({node.args[0].id}, ...)")
    return refs, indirect


def test_source_scan_refuses_indirect_os_attribute():
    """Catches: a removal reached around a call-only scan -- an alias
    (`rm = os.unlink`), a from-import, `getattr(os, "unlink")`,
    `getattr(shutil, ...)`, `vars(os)` -- or a second removal anywhere in
    durable_io.py. Exactly one removal reference may exist, inside
    _abandon_own_temp, and no indirect os/shutil attribute access at all.
    Controls: each evasion appended to the real source is detected."""
    source = Path(durable_io.__file__).read_text(encoding="utf-8")
    refs, indirect = _scan_removals(source)
    assert refs == ["_abandon_own_temp"], refs
    assert indirect == [], indirect

    # Controls: only the appended evasion changes.
    evasions = {
        "alias": ("def f():\n    rm = os.unlink\n", "refs"),
        "from_import": ("from os import remove as _r\n", "refs"),
        "getattr_os": ("def f(p):\n    getattr(os, 'un' + 'link')(p)\n", "indirect"),
        "getattr_shutil": ("import shutil\ndef f(p):\n    getattr(shutil, x)(p)\n", "indirect"),
        "vars_os": ("def f(p):\n    vars(os)[n](p)\n", "indirect"),
        "os_dict": ("def f(p):\n    os.__dict__[n](p)\n", "indirect"),
        "string_name": ("def f(p):\n    m = 'rmdir'\n", "refs"),
        "path_unlink": ("def f(p):\n    Path(p).unlink()\n", "refs"),
    }
    for label, (extra, kind) in evasions.items():
        mrefs, mindirect = _scan_removals(source + "\n" + extra)
        if kind == "refs":
            assert len(mrefs) > 1, label
        else:
            assert mindirect, label


def test_docstring_names_remaining_writers():
    """Catches: a module docstring that drops a writer not yet routed through
    durable_write, or claims P2-41 PROVEN -- the remaining gap must stay
    visible."""
    doc = durable_io.__doc__ or ""
    for writer in ("projectstore.py", "epoch.py", "telemetry.py", "succession.py",
                   "trust.py", "goalbook.py", "migrations.py",
                   "RunStore._park", '"xb"', "RunStore.backup"):
        assert writer in doc, writer
    assert "not PROVEN" in doc
