"""P2-41b H-a: the register-named state writers go through the durable-write
primitive (`dogfood/writer-migration/spec.md`).

`durable_io` gains `durable_create` (publish under a name that must not exist)
and `durable_append` (one complete line); W1-W15 are routed through them and
`durable_write`. Every test names what it catches. Fault cases come with a
control that changes only the condition under test.

Fault injection goes through a proxy installed as `durable_io.os` (the seam of
`tests/test_durable_write.py`, whose small helpers are copied here: test
modules do not import each other). A fault is aimed by the file behind the
descriptor (`/proc/self/fd/<fd>`), so it hits exactly one write of a
multi-file operation. A writer that is not routed through `durable_io` never
sees the fault. No disk is filled; tests write only under `tmp_path`; the trust
tests set `CLAUDE_CONFIG_DIR` under `tmp_path` and never touch the real
`~/.claude.json`.

The last section is the writer scan (spec section 5 and Revision 2a): an AST
scan of `src/hoh/*.py` that keys every raw write call by
`(file, enclosing qualname, kind)` and fails closed on flags or modes it cannot
resolve.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hoh import durable_io
from hoh.durable_io import DiskFullError, DurableWriteNotSynced

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# --- the seam ----------------------------------------------------------------

class _FaultyOS:
    """Stands in for `os` inside one module only; overrides win."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


def _install(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(durable_io, "os", _FaultyOS(**overrides))


def _fd_name(fd: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return ""


def _is_reg(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


def _is_dir(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def _err_write_for(prefix: str, err: int = errno.ENOSPC):
    """`os.write` fails with `err` for a regular file whose name starts with
    `prefix`. Mind the double dot of a dot-file's temp (`..claude.json.tmp.`)."""
    real = os.write

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            raise OSError(err, os.strerror(err))
        return real(fd, data)
    return write


def _short_write_for(prefix: str, keep: int, times: int = 1):
    """The first `times` writes to `prefix*` write only `keep` bytes."""
    real = os.write
    n = {"n": 0}

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix) and n["n"] < times:
            n["n"] += 1
            return real(fd, bytes(data[:keep]))
        return real(fd, data)
    return write


def _short_then_enospc_for(prefix: str, keep: int = 5):
    """A short write first (progress), then ENOSPC: some bytes reach the temp
    file, so a writer that published it would leave a half copy."""
    real = os.write
    n = {"n": 0}

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            n["n"] += 1
            if n["n"] == 1:
                return real(fd, bytes(data[:keep]))
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real(fd, data)
    return write


def _fsync_reg_err(err: int):
    real = os.fsync

    def fsync(fd):
        if _is_reg(fd):
            raise OSError(err, os.strerror(err))
        return real(fd)
    return fsync


def _dir_fsync_fails(times: int = 1, err: int = errno.EIO):
    real = os.fsync
    n = {"n": 0}

    def fsync(fd):
        if _is_dir(fd) and n["n"] < times:
            n["n"] += 1
            raise OSError(err, os.strerror(err))
        return real(fd)
    return fsync


def _statvfs_reporting(free_bytes: int):
    def fake(_path):
        return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1, f_bsize=1,
                                     f_blocks=free_bytes, f_bfree=free_bytes)
    return fake


def _no_links(*_a, **_k):
    raise OSError(errno.EPERM, "no hard links on this filesystem")


def _litter(directory: Path) -> list[str]:
    """Temp files a durable call would leave: `.<name>.tmp.<pid>.<random>`."""
    return sorted(p.name for p in Path(directory).iterdir() if ".tmp." in p.name)


class _umask:
    def __init__(self, mask: int) -> None:
        self.mask = mask

    def __enter__(self):
        self.old = os.umask(self.mask)

    def __exit__(self, *_exc):
        os.umask(self.old)


# ==== K1 durable_create ======================================================

def test_create_publishes_whole_and_leaves_no_temp(tmp_path):
    """Catches: a create that leaves its temp name as a second link (nlink 2),
    ignores the mode, or reports a clean publish wrongly."""
    t = tmp_path / "r.v1"
    assert durable_io.durable_create(t, b"mine", mode=0o640) is True
    assert t.read_bytes() == b"mine"
    assert t.stat().st_nlink == 1
    assert stat.S_IMODE(t.stat().st_mode) == 0o640
    assert _litter(tmp_path) == []
    # str is encoded as UTF-8.
    u = tmp_path / "u.v1"
    durable_io.durable_create(u, "grüß")
    assert u.read_bytes() == "grüß".encode("utf-8")


def test_create_refuses_existing_name(tmp_path, monkeypatch):
    """Catches: a create that replaces an existing file, or that reports a full
    disk instead of FileExistsError for a name that is already taken (the
    existence check comes before the space pre-check). A dangling symlink
    counts as taken."""
    t = tmp_path / "r.v1"
    t.write_bytes(b"old")
    _install(monkeypatch, statvfs=_statvfs_reporting(0))
    with pytest.raises(FileExistsError):
        durable_io.durable_create(t, b"new")
    assert t.read_bytes() == b"old" and _litter(tmp_path) == []

    link = tmp_path / "dangling.v1"
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        durable_io.durable_create(link, b"new")
    assert os.path.islink(link) and not (tmp_path / "nowhere").exists()


def test_create_never_replaces_racing_file(tmp_path, monkeypatch):
    """Catches: a publish by `os.replace` (or any fallback) that overwrites a
    file appearing at the name between the check and the publish."""
    t = tmp_path / "r.v1"
    real = os.fsync

    def race(fd):
        real(fd)
        if _is_reg(fd) and not t.exists():
            t.write_bytes(b"foreign")
    _install(monkeypatch, fsync=race)
    with pytest.raises(FileExistsError):
        durable_io.durable_create(t, b"mine")
    assert t.read_bytes() == b"foreign" and _litter(tmp_path) == []


@pytest.mark.parametrize("fault,exc", [
    ("write_enospc", DiskFullError),
    ("write_eio", OSError),
    ("zero", DiskFullError),
    ("fsync_eio", OSError),
    ("fsync_enospc", DiskFullError),
    ("precheck", DiskFullError),
])
def test_create_fault_leaves_name_absent(tmp_path, monkeypatch, fault, exc):
    """Catches: a failed create that leaves a file (empty, half or whole) at
    the name, leaves its temp file behind, or surfaces a space error as
    anything but DiskFullError."""
    t = tmp_path / "r.v1"
    overrides = {
        "write_enospc": dict(write=_err_write_for(".r.v1.tmp.")),
        "write_eio": dict(write=_err_write_for(".r.v1.tmp.", errno.EIO)),
        "zero": dict(write=lambda fd, data: 0),
        "fsync_eio": dict(fsync=_fsync_reg_err(errno.EIO)),
        "fsync_enospc": dict(fsync=_fsync_reg_err(errno.ENOSPC)),
        "precheck": dict(statvfs=_statvfs_reporting(10)),
    }[fault]
    _install(monkeypatch, **overrides)
    with pytest.raises(exc) as info:
        durable_io.durable_create(t, b"x" * 100)
    if fault == "write_eio" or fault == "fsync_eio":
        assert info.value.errno == errno.EIO and not isinstance(info.value, DiskFullError)
    assert not os.path.lexists(t)
    assert _litter(tmp_path) == []
    assert sorted(os.listdir(tmp_path)) == []


def test_create_no_hardlinks_fails_closed(tmp_path, monkeypatch):
    """Catches: a fallback to `os.replace` or to writing at the final name when
    the filesystem has no hard links. The link error is re-raised."""
    t = tmp_path / "r.v1"
    _install(monkeypatch, link=_no_links)
    with pytest.raises(OSError) as info:
        durable_io.durable_create(t, b"mine")
    assert info.value.errno == errno.EPERM
    assert not os.path.lexists(t) and _litter(tmp_path) == []


def test_create_dir_fsync_failure_named(tmp_path, monkeypatch):
    """Catches: a create that does not fsync the directory after the link, or
    reports that failure as anything but DurableWriteNotSynced (the file is
    in place)."""
    t = tmp_path / "r.v1"
    _install(monkeypatch, fsync=_dir_fsync_fails(err=errno.ENOSPC))
    with pytest.raises(DurableWriteNotSynced):
        durable_io.durable_create(t, b"mine")
    assert t.read_bytes() == b"mine" and _litter(tmp_path) == []


def test_create_temp_name_left_is_reported(tmp_path, monkeypatch):
    """Catches: a create that returns True although the temp name could not be
    dropped after the link. The published file is whole either way; the temp
    name is a second link to the same inode."""
    t = tmp_path / "r.v1"
    real_stat = os.stat

    def stat_other_inode(name, *a, **k):
        st = real_stat(name, *a, **k)
        if str(name).startswith(".r.v1.tmp."):
            return os.stat_result((st.st_mode, st.st_ino + 1) + tuple(st)[2:])
        return st
    _install(monkeypatch, stat=stat_other_inode)
    assert durable_io.durable_create(t, b"mine") is False
    assert t.read_bytes() == b"mine"
    assert t.stat().st_nlink == 2
    left = _litter(tmp_path)
    assert len(left) == 1 and (tmp_path / left[0]).stat().st_ino == t.stat().st_ino


def test_create_mode_none_follows_umask(tmp_path):
    """Catches: `mode=None` treated as a fixed mode instead of the umask's.
    Control: an explicit mode is set whatever the umask."""
    t = tmp_path / "r.v1"
    with _umask(0o077):
        durable_io.durable_create(t, b"mine", mode=None)
        durable_io.durable_create(tmp_path / "e.v1", b"mine", mode=0o644)
    assert stat.S_IMODE(t.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "e.v1").stat().st_mode) == 0o644
    with _umask(0o022):
        durable_io.durable_create(tmp_path / "w.v1", b"mine", mode=None)
    assert stat.S_IMODE((tmp_path / "w.v1").stat().st_mode) == 0o644


# ==== K2 durable_append ======================================================

def test_append_writes_one_complete_line_fsynced(tmp_path, monkeypatch):
    """Catches: an append that splits a line over several writes, adds no
    newline, or skips the file or directory fsync."""
    p = tmp_path / "l.jsonl"
    calls = {"write": 0, "fsync_reg": 0, "fsync_dir": 0}
    real_write, real_fsync = os.write, os.fsync

    def write(fd, data):
        calls["write"] += 1
        return real_write(fd, data)

    def fsync(fd):
        calls["fsync_reg" if _is_reg(fd) else "fsync_dir"] += 1
        return real_fsync(fd)
    _install(monkeypatch, write=write, fsync=fsync)
    durable_io.durable_append(p, "a")
    durable_io.durable_append(p, b"b\n")
    assert p.read_bytes() == b"a\nb\n"
    assert calls == {"write": 2, "fsync_reg": 2, "fsync_dir": 2}


def test_append_enospc_surfaced_prior_intact(tmp_path, monkeypatch):
    """Catches: an ENOSPC append that is swallowed or not a DiskFullError, or
    that touches earlier lines."""
    p = tmp_path / "l.jsonl"
    durable_io.durable_append(p, "a")
    _install(monkeypatch, write=_err_write_for("l.jsonl"))
    with pytest.raises(DiskFullError):
        durable_io.durable_append(p, "b")
    assert p.read_bytes() == b"a\n"


def test_append_short_write_is_disk_full_not_retried(tmp_path, monkeypatch):
    """Catches: a short append that is retried (a loop would complete the line
    after a torn write and hide the incident) or not reported."""
    p = tmp_path / "l.jsonl"
    durable_io.durable_append(p, "a")
    _install(monkeypatch, write=_short_write_for("l.jsonl", 2))
    with pytest.raises(DiskFullError, match="short append: 2 of 5"):
        durable_io.durable_append(p, "bbbb")
    assert p.read_bytes() == b"a\nbb"


def test_append_torn_tail_terminated(tmp_path):
    """Catches: a new record glued onto a torn fragment left by an earlier
    failed append. Control: a file ending in a newline gets no blank line."""
    p = tmp_path / "l.jsonl"
    p.write_bytes(b'a\n{"tor')
    durable_io.durable_append(p, "b")
    assert p.read_bytes() == b'a\n{"tor\nb\n'
    durable_io.durable_append(p, "c")
    assert p.read_bytes() == b'a\n{"tor\nb\nc\n'


def test_append_precheck_refuses_before_open(tmp_path, monkeypatch):
    """Catches: a pre-check after `os.open`, so a refused append still creates
    the file. Control: with room the same call creates it."""
    p = tmp_path / "l.jsonl"

    def open_must_not_be_called(*_a, **_k):
        raise AssertionError("os.open called before the free-space check refused")
    _install(monkeypatch, statvfs=_statvfs_reporting(1), open=open_must_not_be_called)
    with pytest.raises(DiskFullError):
        durable_io.durable_append(p, "a")
    assert not p.exists() and os.listdir(tmp_path) == []

    _install(monkeypatch, statvfs=_statvfs_reporting(1 << 30))
    durable_io.durable_append(p, "a")
    assert p.read_bytes() == b"a\n"


_CHILD = textwrap.dedent("""
    import resource, signal, sys
    from pathlib import Path
    from hoh.durable_io import DiskFullError, durable_append
    p, limit = Path(sys.argv[1]), int(sys.argv[2])
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    try:
        durable_append(p, "y" * 50, min_free_bytes=0)
    except DiskFullError as exc:
        print("DiskFullError", exc.errno)
        sys.exit(0)
    except BaseException as exc:
        print("OTHER", type(exc).__name__, exc)
        sys.exit(2)
    print("WROTE")
""")


def test_append_real_short_write_rlimit(tmp_path):
    """Catches: a real short write (a file-size limit hit mid-line) that is not
    a DiskFullError; the torn tail is then terminated by the next append.
    Control: a limit above the line writes it whole."""
    p = tmp_path / "l.jsonl"
    p.write_bytes(b"a\n")
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", _CHILD, str(p), "20"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.split() == ["DiskFullError", str(errno.ENOSPC)], (out.stdout, out.stderr)
    assert p.read_bytes() == b"a\n" + b"y" * 18
    durable_io.durable_append(p, "z")
    assert p.read_bytes().endswith(b"y\nz\n")

    q = tmp_path / "m.jsonl"
    out = subprocess.run([sys.executable, "-c", _CHILD, str(q), "4096"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == "WROTE", (out.stdout, out.stderr)
    assert q.read_bytes() == b"y" * 50 + b"\n"


def test_append_dir_fsync_failure_named(tmp_path, monkeypatch):
    """Catches: an append that skips the directory fsync (a first append
    creates the entry) or reports its failure as anything but
    DurableWriteNotSynced (the line is in place)."""
    p = tmp_path / "l.jsonl"
    _install(monkeypatch, fsync=_dir_fsync_fails(err=errno.ENOSPC))
    with pytest.raises(DurableWriteNotSynced):
        durable_io.durable_append(p, "a")
    assert p.read_bytes() == b"a\n"


# ==== K3 epoch (W1, W2) ======================================================

T0, T1, T2 = "2026-09-25T10:00:00Z", "2026-09-25T10:01:00Z", "2026-09-25T10:02:00Z"


def _epochs(tmp_path):
    from hoh.epoch import EpochStore, Reason
    s = EpochStore(tmp_path / "program")
    s.begin("a", reason=Reason.INITIAL, now=T0)
    s.begin("b", reason=Reason.SUCCESSION, now=T1)
    return s, Reason


def _versions(directory: Path, prefix: str) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(prefix))


def test_epoch_write_enospc_keeps_chain(tmp_path, monkeypatch):
    """Catches W1 left raw: ENOSPC on the epoch write must raise and keep
    `epochs.json` and its chain, with no temp left."""
    s, Reason = _epochs(tmp_path)
    before = s.path.read_bytes()
    _install(monkeypatch, write=_err_write_for(".epochs.json.tmp."))
    with pytest.raises(DiskFullError):
        s.begin("c", reason=Reason.SUCCESSION, now=T2)
    assert s.path.read_bytes() == before and _litter(s.root) == []
    monkeypatch.undo()
    assert [e.master_id for e in s.chain()] == ["a", "b"] and s.fsck() == []


def test_epoch_preserve_enospc_no_half_version(tmp_path, monkeypatch):
    """Catches W2 left raw: ENOSPC while preserving the previous chain must not
    leave a half `epochs.json.v*`, and the chain is not written after it."""
    s, Reason = _epochs(tmp_path)
    before = s.path.read_bytes()
    versions = _versions(s.root, "epochs.json.v")
    _install(monkeypatch, write=_short_then_enospc_for(".epochs.json.v"))
    with pytest.raises(DiskFullError):
        s.begin("c", reason=Reason.SUCCESSION, now=T2)
    assert _versions(s.root, "epochs.json.v") == versions
    assert s.path.read_bytes() == before and _litter(s.root) == []


def test_epoch_preserve_never_replaces_racing_version(tmp_path, monkeypatch):
    """Catches W2 publishing over a version name taken by someone else between
    the check and the publish: that file is kept, and the loop moves on to the
    next name."""
    s, Reason = _epochs(tmp_path)
    before = s.path.read_bytes()
    real = os.fsync
    planted = {}

    def race(fd):
        real(fd)
        name = _fd_name(fd)
        if _is_reg(fd) and name.startswith(".epochs.json.v") and not planted:
            target = s.root / name.split(".tmp.")[0][1:]
            target.write_bytes(b"foreign")
            planted["p"] = target
    _install(monkeypatch, fsync=race)
    s.begin("c", reason=Reason.SUCCESSION, now=T2)
    assert planted["p"].read_bytes() == b"foreign"
    copies = [p for p in s.root.glob("epochs.json.v*") if p != planted["p"]]
    assert before in {p.read_bytes() for p in copies}
    assert [e.master_id for e in s.chain()] == ["a", "b", "c"]


def test_epoch_dir_fsync_failure_burns_epoch(tmp_path, monkeypatch):
    """Spec 3.1: the directory fsync after the epoch's replace fails --
    DurableWriteNotSynced, no lease, no token, the epoch is burned; the next
    acquire begins the epoch after it on a consistent chain. Catches a W1
    that still swallows the directory-fsync error."""
    from hoh.succession import MasterAuthority
    a = MasterAuthority(tmp_path, "sA", ttl_seconds=60, clock=lambda: T0)
    _install(monkeypatch, fsync=_dir_fsync_fails(times=1))
    with pytest.raises(DurableWriteNotSynced):
        a.acquire()
    assert [e.epoch for e in a.epochs.chain()] == [1]
    assert a.read_lease() is None and a.token is None and a.epoch is None
    monkeypatch.undo()
    epoch = a.acquire()
    assert epoch.epoch == 2 and a.epochs.fsck() == []
    assert a.read_lease().master_epoch == 2


def test_epoch_control_bytes_and_mode(tmp_path, monkeypatch):
    """Control: through the seam with no fault, the chain grows, the file holds
    the same JSON form as before (indent 2, trailing newline), the previous
    chain is preserved byte for byte, and the mode is 0o600 as `mkstemp`
    gave it."""
    s, Reason = _epochs(tmp_path)
    before = s.path.read_bytes()
    _install(monkeypatch)
    s.begin("c", reason=Reason.SUCCESSION, now=T2)
    assert [e.master_id for e in s.chain()] == ["a", "b", "c"] and s.fsck() == []
    raw = s.path.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert raw == json.dumps(doc, indent=2) + "\n" and doc["written_at"] == T2
    assert before in {p.read_bytes() for p in s.root.glob("epochs.json.v*")}
    assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
    assert _litter(s.root) == []


# ==== K4 goalbook (W3, W4) ===================================================

def _objective(i: int):
    from hoh.goalbook import Objective
    return Objective(objective_id=f"Z{i}", title=f"Objective {i}")


def test_goalbook_write_enospc_keeps_prior(tmp_path, monkeypatch):
    """Catches W3 left raw: ENOSPC on the goalbook write must raise and keep
    the prior `goalbook.json`, with no temp left."""
    from hoh.goalbook import Goalbook
    gb = Goalbook(tmp_path)
    gb.write([_objective(1)])
    before = gb.path.read_bytes()
    _install(monkeypatch, write=_err_write_for(".goalbook.json.tmp."))
    with pytest.raises(DiskFullError):
        gb.write([_objective(1), _objective(2)])
    assert gb.path.read_bytes() == before and _litter(tmp_path) == []


def test_goalbook_park_enospc_no_half_version(tmp_path, monkeypatch):
    """Catches W4 left raw: ENOSPC while parking must leave no
    `goalbook.json.v*`, and the goalbook is not written after it."""
    from hoh.goalbook import Goalbook
    gb = Goalbook(tmp_path)
    gb.write([_objective(1)])
    before = gb.path.read_bytes()
    _install(monkeypatch, write=_short_then_enospc_for(".goalbook.json.v"))
    with pytest.raises(DiskFullError):
        gb.write([_objective(1), _objective(2)])
    assert gb.path.read_bytes() == before
    assert _versions(tmp_path, "goalbook.json.v") == []
    assert _litter(tmp_path) == []


def test_goalbook_write_control(tmp_path, monkeypatch):
    """Control: through the seam with no fault, the goalbook is replaced with
    the same text form and the previous one is parked byte for byte."""
    from hoh.goalbook import Goalbook
    gb = Goalbook(tmp_path)
    gb.write([_objective(1)])
    before = gb.path.read_bytes()
    _install(monkeypatch)
    gb.write([_objective(1), _objective(2)])
    assert [o.objective_id for o in gb.read()] == ["Z1", "Z2"]
    raw = gb.path.read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    parked = [p for p in tmp_path.iterdir() if p.name.startswith("goalbook.json.v")]
    assert [p.read_bytes() for p in parked] == [before]
    assert _litter(tmp_path) == []


# ==== K5 telemetry (W5, W6) ==================================================

def _trec(i: int, start: str = T0):
    from hoh.telemetry import DispatchRecord
    return DispatchRecord(role="developer", run_id=f"r{i}", iteration=1, attempt=1,
                          started_at=start, ended_at=start, wallclock_seconds=1.0)


def test_telemetry_rewrite_enospc_keeps_log_no_partial(tmp_path, monkeypatch):
    """Catches W5 left raw: ENOSPC on the sorted rewrite must raise, keep the
    log, and leave neither a temp file nor a `.partial`."""
    from hoh.telemetry import TelemetryLog
    log = TelemetryLog(tmp_path / "t.jsonl")
    log.append(_trec(2, T1))
    log.append(_trec(1, T0))
    before = log.path.read_bytes()
    _install(monkeypatch, write=_err_write_for(".t.jsonl.tmp."))
    with pytest.raises(DiskFullError):
        log.rewrite_sorted()
    assert log.path.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == ["t.jsonl"]


def test_telemetry_append_enospc_surfaced_prior_intact(tmp_path, monkeypatch):
    """Catches W6 left raw: an ENOSPC append must raise DiskFullError with the
    earlier lines intact."""
    from hoh.telemetry import TelemetryLog
    log = TelemetryLog(tmp_path / "t.jsonl")
    log.append(_trec(1))
    before = log.path.read_bytes()
    _install(monkeypatch, write=_err_write_for("t.jsonl"))
    with pytest.raises(DiskFullError):
        log.append(_trec(2))
    assert log.path.read_bytes() == before


def test_telemetry_append_after_torn_tail_is_read_back(tmp_path, monkeypatch):
    """Catches W6 gluing the next record onto a torn fragment: after a short
    append the next record still reads back (the fragment is skipped)."""
    from hoh.telemetry import TelemetryLog
    log = TelemetryLog(tmp_path / "t.jsonl")
    log.append(_trec(1))
    _install(monkeypatch, write=_short_write_for("t.jsonl", 7))
    with pytest.raises(DiskFullError):
        log.append(_trec(2))
    monkeypatch.undo()
    log.append(_trec(3))
    assert [r.run_id for r in log.read()] == ["r1", "r3"]


def test_telemetry_control(tmp_path, monkeypatch):
    """Control: appends and the sorted rewrite through the seam with no fault
    keep every record, one per line, in start order; the log is 0o600 as the
    `mkstemp` temp gave it."""
    from hoh.telemetry import TelemetryLog
    log = TelemetryLog(tmp_path / "t.jsonl")
    _install(monkeypatch)
    log.append(_trec(2, T1))
    log.append(_trec(1, T0))
    log.rewrite_sorted()
    assert [r.run_id for r in log.read()] == ["r1", "r2"]
    assert log.path.read_text(encoding="utf-8").count("\n") == 2
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    assert sorted(os.listdir(tmp_path)) == ["t.jsonl"]


# ==== K6 migrations (W7, W8) =================================================

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _registry():
    from hoh.migrations import Migration, Registry
    r = Registry()

    def add_owner(rec):
        rec["owner"] = "c"
        return rec
    r.register(Migration(from_version=1, to_version=2, name="add-owner",
                         description="adds an owner", apply=add_owner))
    return r


def _mstore(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "state.json").write_text(json.dumps({"schema_version": 1, "run_id": "r1"}))
    return root


def test_migrations_write_enospc_keeps_record(tmp_path, monkeypatch):
    """Catches W7 left raw: ENOSPC on the record rewrite must raise and keep
    the old form, with no temp left."""
    from hoh.migrations import migrate
    root = _mstore(tmp_path)
    before = (root / "state.json").read_bytes()
    _install(monkeypatch, write=_err_write_for(".state.json.tmp."))
    with pytest.raises(DiskFullError):
        migrate(root, registry=_registry(), now=NOW, dry_run=False)
    assert (root / "state.json").read_bytes() == before and _litter(root) == []


def test_migrations_preserve_enospc_no_half_copy(tmp_path, monkeypatch):
    """Catches W8 left raw: ENOSPC while preserving must leave no half
    `<name>.v<N>.<UTC>`, and the record is not rewritten after it."""
    from hoh.migrations import migrate
    root = _mstore(tmp_path)
    before = (root / "state.json").read_bytes()
    _install(monkeypatch, write=_short_then_enospc_for(".state.json.v"))
    with pytest.raises(DiskFullError):
        migrate(root, registry=_registry(), now=NOW, dry_run=False)
    assert sorted(p.name for p in root.iterdir()) == ["state.json"]
    assert (root / "state.json").read_bytes() == before


def test_migrations_resume_after_enospc_completes(tmp_path, monkeypatch):
    """Catches a migration that cannot be resumed after ENOSPC on the rewrite:
    the re-run with the same `now` finds its own preserved copy (same bytes)
    and completes."""
    from hoh.migrations import migrate
    root = _mstore(tmp_path)
    before = (root / "state.json").read_bytes()
    _install(monkeypatch, write=_err_write_for(".state.json.tmp."))
    with pytest.raises(DiskFullError):
        migrate(root, registry=_registry(), now=NOW, dry_run=False)
    monkeypatch.undo()
    migrate(root, registry=_registry(), now=NOW, dry_run=False)
    doc = json.loads((root / "state.json").read_text())
    assert doc["schema_version"] == 2 and doc["owner"] == "c"
    copies = sorted(root.glob("state.json.v*"))
    assert [c.read_bytes() for c in copies] == [before]
    assert _litter(root) == []


# ==== K7 storage pressure (W9) ===============================================

def test_storage_pressure_state_enospc_keeps_prior(tmp_path, monkeypatch):
    """Catches W9 left raw: ENOSPC must raise and keep the prior tracker /
    host / observation / reservation file, with no temp left."""
    from hoh import storage_pressure as sp
    p = tmp_path / "tracker.json"
    sp._atomic_write(p, "old")
    _install(monkeypatch, write=_err_write_for(".tracker.json.tmp."))
    with pytest.raises(DiskFullError):
        sp._atomic_write(p, "new")
    assert p.read_text() == "old" and _litter(tmp_path) == []


def test_storage_pressure_control(tmp_path, monkeypatch):
    """Control: with no fault the file is written, its directory created as
    before (the writer's own mkdir), and replaced on the next write."""
    from hoh import storage_pressure as sp
    p = tmp_path / "sub" / "tracker.json"
    _install(monkeypatch)
    sp._atomic_write(p, "new")
    sp._atomic_write(p, "newer")
    assert p.read_text() == "newer" and _litter(p.parent) == []


# ==== K8 trust (W10, W11) ====================================================

def _conf(tmp_path, monkeypatch, mode: int = 0o600):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    c = cfg / ".claude.json"
    c.write_text(json.dumps({"projects": {}}))
    c.chmod(mode)
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    return c, root


def _baks(c: Path) -> list[Path]:
    return sorted(c.parent.glob(".claude.json.hoh-*.bak"))


def test_trust_config_enospc_keeps_config(tmp_path, monkeypatch):
    """Catches W11 left raw: ENOSPC on the config write must raise and keep the
    config's bytes, with no temp (`..claude.json.tmp.*`, no `hoh-tmp`) left."""
    from hoh import trust
    c, root = _conf(tmp_path, monkeypatch)
    before = c.read_bytes()
    _install(monkeypatch, write=_err_write_for("..claude.json.tmp."))
    with pytest.raises(DiskFullError):
        trust.register(root / "a", owned_root=root)
    assert c.read_bytes() == before and _litter(c.parent) == []
    assert not [p for p in c.parent.iterdir() if "hoh-tmp" in p.name]
    assert stat.S_IMODE(c.stat().st_mode) == 0o600


def test_trust_backup_enospc_config_untouched(tmp_path, monkeypatch):
    """Catches W10 left raw: ENOSPC on the backup must raise, leave no half
    backup, and the config is not written after it."""
    from hoh import trust
    c, root = _conf(tmp_path, monkeypatch)
    before = c.read_bytes()
    _install(monkeypatch, write=_short_then_enospc_for("..claude.json.hoh-"))
    with pytest.raises(DiskFullError):
        trust.register(root / "a", owned_root=root)
    assert c.read_bytes() == before
    assert [p.name for p in c.parent.iterdir()] == [".claude.json"]


def test_trust_config_mode_kept(tmp_path, monkeypatch):
    """Catches W11 taking the umask's mode for the config (0o644 under umask
    022) instead of its current one, in both directions; the backup takes the
    config's mode too."""
    from hoh import trust
    c, root = _conf(tmp_path, monkeypatch, 0o600)
    with _umask(0o022):
        assert trust.register(root / "a", owned_root=root) is True
    assert stat.S_IMODE(c.stat().st_mode) == 0o600
    assert [stat.S_IMODE(b.stat().st_mode) for b in _baks(c)] == [0o600]
    assert json.loads(c.read_text())["projects"][str((root / "a").resolve())] == \
        {"hasTrustDialogAccepted": True}

    # The user widened it: the user's choice wins over the umask too.
    c.chmod(0o664)
    with _umask(0o077):
        assert trust.register(root / "b", owned_root=root) is True
    assert stat.S_IMODE(c.stat().st_mode) == 0o664


def test_trust_backup_same_second_not_overwritten(tmp_path, monkeypatch):
    """Catches W10 overwriting a backup taken in the same second: the second
    one gets `-001` before `.bak`, both hold their own bytes."""
    from hoh import trust
    c, root = _conf(tmp_path, monkeypatch)
    monkeypatch.setattr(trust, "utcnow", lambda: "2026-10-03T00:00:00Z")
    first = c.read_bytes()
    trust.register(root / "a", owned_root=root)
    second = c.read_bytes()
    trust.register(root / "b", owned_root=root)
    by_name = {b.name: b.read_bytes() for b in _baks(c)}
    assert by_name == {".claude.json.hoh-2026-10-03T00:00:00Z.bak": first,
                       ".claude.json.hoh-2026-10-03T00:00:00Z-001.bak": second}


# ==== K9 project store (W12) =================================================

def _ps(tmp_path):
    from hoh.project import ProjectState
    from hoh.projectstore import ProjectStore
    ps = ProjectStore(tmp_path, "p")
    ps.create(ProjectState(project_id="p", repo_path=str(tmp_path)))
    return ps


def test_projectstore_write_enospc_keeps_state(tmp_path, monkeypatch):
    """Catches W12 left raw (`_park` moves the live state away): ENOSPC on the
    state write must leave `project.json` in place with its bytes."""
    ps = _ps(tmp_path)
    before = ps.state_path.read_bytes()
    st = ps.read_state()
    _install(monkeypatch, write=_err_write_for(".project.json.tmp."))
    with pytest.raises(DiskFullError):
        ps.write_state(st)
    assert ps.state_path.read_bytes() == before and _litter(ps.dir) == []


def test_projectstore_park_enospc_keeps_state(tmp_path, monkeypatch):
    """Catches a W12 park that is not a durable copy: ENOSPC while parking must
    raise, keep `project.json`, and leave no half version."""
    ps = _ps(tmp_path)
    before = ps.state_path.read_bytes()
    st = ps.read_state()
    _install(monkeypatch, write=_short_then_enospc_for(".project.json.v"))
    with pytest.raises(DiskFullError):
        ps.write_state(st)
    assert ps.state_path.read_bytes() == before and ps.parked_states() == []
    assert _litter(ps.dir) == []


def test_projectstore_write_control(tmp_path, monkeypatch):
    """Control: with no fault the state is replaced and the previous one is
    parked byte for byte."""
    ps = _ps(tmp_path)
    before = ps.state_path.read_bytes()
    st = ps.read_state()
    _install(monkeypatch)
    ps.write_state(st)
    assert ps.state_path.read_bytes() != before
    assert ps.read_state().write_seq == st.write_seq
    assert [p.read_bytes() for p in ps.parked_states()] == [before]
    assert _litter(ps.dir) == []


# ==== K10 succession log (W13) ===============================================

def _ma(tmp_path, clock):
    from hoh.succession import MasterAuthority
    return MasterAuthority(tmp_path, "sA", ttl_seconds=60, clock=lambda: clock["now"])


def test_succession_log_enospc_surfaced_prior_intact(tmp_path, monkeypatch):
    """Catches W13 left raw: an ENOSPC lease-log append must raise
    DiskFullError with the earlier lines intact."""
    a = _ma(tmp_path, {"now": T0})
    a.acquire()
    log = tmp_path / "lease.log.jsonl"
    before = log.read_bytes()
    _install(monkeypatch, write=_err_write_for("lease.log.jsonl"))
    with pytest.raises(DiskFullError):
        a.release(reason="test")
    assert log.read_bytes() == before


def test_succession_log_torn_tail_terminated(tmp_path, monkeypatch):
    """Catches W13 gluing the next entry onto a torn fragment: after a short
    append, the next entry is a line of its own."""
    a = _ma(tmp_path, {"now": T0})
    a.acquire()
    log = tmp_path / "lease.log.jsonl"
    _install(monkeypatch, write=_short_write_for("lease.log.jsonl", 6))
    with pytest.raises(DiskFullError):
        a.release(reason="test")
    monkeypatch.undo()
    a.acquire()
    lines = log.read_text().splitlines()
    assert json.loads(lines[0])["event"] == "acquire"
    assert json.loads(lines[-1])["event"] == "acquire"
    assert len(lines) == 3  # acquire, the torn fragment, acquire


def test_succession_lease_written_then_log_raises(tmp_path, monkeypatch):
    """Spec 3.1, pinned (not fixed, Q3): the lease is written durably, then the
    log append is refused. acquire raises DiskFullError, the handle holds no
    token, the same handle is refused (LeaseHeld) until the TTL ends, and then
    acquires the next epoch."""
    from hoh.succession import LeaseHeld
    clock = {"now": T0}
    a = _ma(tmp_path, clock)
    _install(monkeypatch, write=_err_write_for("lease.log.jsonl"))
    with pytest.raises(DiskFullError):
        a.acquire()
    monkeypatch.undo()
    lease = a.read_lease()
    assert lease.leader_session_id == "sA" and lease.master_epoch == 1
    assert a.token is None and a.epoch is None
    with pytest.raises(LeaseHeld):
        a.acquire()
    clock["now"] = "2026-09-25T10:05:00Z"
    assert a.acquire().epoch == 2


def test_succession_log_dir_fsync_failure_locks_out_until_ttl(tmp_path, monkeypatch):
    """Revision 2a item 3, pinned (not fixed, Q3): the lease and the log line
    are written, then the log's directory fsync fails. acquire raises
    DurableWriteNotSynced without a token; the same handle gets LeaseHeld until
    the TTL ends, then the next epoch."""
    from hoh.succession import LeaseHeld
    clock = {"now": T0}
    a = _ma(tmp_path, clock)
    real_open, real_fsync = os.open, os.fsync
    armed = {"on": False}

    def open_(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith("lease.log.jsonl"):
            armed["on"] = True
        return fd

    def fsync(fd):
        if armed["on"] and _is_dir(fd):
            armed["on"] = False
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)
    _install(monkeypatch, open=open_, fsync=fsync)
    with pytest.raises(DurableWriteNotSynced):
        a.acquire()
    monkeypatch.undo()
    assert a.read_lease().master_epoch == 1 and a.token is None
    lines = (tmp_path / "lease.log.jsonl").read_text().splitlines()
    assert [json.loads(x)["event"] for x in lines] == ["acquire"]
    with pytest.raises(LeaseHeld):
        a.acquire()
    clock["now"] = "2026-09-25T10:05:00Z"
    assert a.acquire().epoch == 2


# ==== K11 run store (W14, W15) ===============================================

def _rs(tmp_path):
    from hoh.contracts import RunState
    from hoh.store import RunStore
    s = RunStore(tmp_path / "runs", "r1")
    s.ensure()
    s.write_state(RunState(run_id="r1", repo_path="/x", project_name="d", spec_path="/s",
                           spec_digest="s", policy_digest="p", profile_digest="h"))
    return s


def _store_links_fail(monkeypatch):
    """Forces `RunStore._park`'s fallback: `link` fails inside `hoh.store`
    only -- never `os.link` globally, which would also break durable_create."""
    import hoh.store as store_mod
    monkeypatch.setattr(store_mod, "os", _FaultyOS(link=_no_links))


def test_runstore_park_fallback_enospc_no_half_version(tmp_path, monkeypatch):
    """Catches W14 left raw: ENOSPC in the park fallback must leave no half
    `state.json.v*`, and the state is not written after it."""
    s = _rs(tmp_path)
    before = s.state_path.read_bytes()
    st = s.read_state()
    parked_before = s.parked_states()
    _store_links_fail(monkeypatch)
    _install(monkeypatch, write=_short_then_enospc_for(".state.json.v"))
    with pytest.raises(DiskFullError):
        s.write_state(st)
    assert s.parked_states() == parked_before and s.state_path.read_bytes() == before
    assert _litter(s.dir) == []


def test_runstore_park_both_links_fail_state_intact(tmp_path, monkeypatch):
    """On a filesystem without hard links both the park link and the
    fallback's publish fail: the write is refused (fail closed), nothing is
    half and `state.json` is intact. Catches a fallback that writes at the
    final name or replaces."""
    s = _rs(tmp_path)
    before = s.state_path.read_bytes()
    st = s.read_state()
    parked_before = s.parked_states()
    _store_links_fail(monkeypatch)
    _install(monkeypatch, link=_no_links)
    with pytest.raises(OSError) as info:
        s.write_state(st)
    assert info.value.errno == errno.EPERM
    assert s.state_path.read_bytes() == before and s.parked_states() == parked_before
    assert _litter(s.dir) == []


def test_runstore_park_fallback_never_replaces_racing_version(tmp_path, monkeypatch):
    """Catches the W14 fallback overwriting a version name taken between the
    check and the publish: that file is kept and the same StoreError as the
    link branch is raised."""
    from hoh.store import StoreError
    s = _rs(tmp_path)
    before = s.state_path.read_bytes()
    st = s.read_state()
    _store_links_fail(monkeypatch)
    real = os.fsync
    planted = {}

    def race(fd):
        real(fd)
        name = _fd_name(fd)
        if _is_reg(fd) and name.startswith(".state.json.v") and not planted:
            target = s.dir / name.split(".tmp.")[0][1:]
            target.write_bytes(b"foreign")
            planted["p"] = target
    _install(monkeypatch, fsync=race)
    with pytest.raises(StoreError, match="appeared between the check and the creation"):
        s.write_state(st)
    assert planted["p"].read_bytes() == b"foreign"
    assert s.state_path.read_bytes() == before and _litter(s.dir) == []


def test_runstore_backup_enospc_no_half_archive(tmp_path, monkeypatch):
    """Catches W15 left raw (tar written straight at the final name): ENOSPC
    must leave no file at the backup name."""
    s = _rs(tmp_path)
    _install(monkeypatch, write=_err_write_for(".r1-"))
    with pytest.raises(DiskFullError):
        s.backup()
    assert [p.name for p in (s.root / "backups").iterdir()] == []


def test_runstore_backup_same_second_not_overwritten(tmp_path, monkeypatch):
    """Catches W15 overwriting a backup taken in the same second: the second
    gets `-001` before `.tar.gz`; both stay, and `restore` restores the
    in-memory-built archive."""
    import hoh.store as store_mod
    s = _rs(tmp_path)
    monkeypatch.setattr(store_mod, "_stamp", lambda: "20261003T000000Z")
    a = s.backup()
    a_bytes = a.read_bytes()
    b = s.backup()
    assert (a.name, b.name) == ("r1-20261003T000000Z.tar.gz", "r1-20261003T000000Z-001.tar.gz")
    assert a.read_bytes() == a_bytes and b.exists()
    restored = s.restore(b)
    assert restored.exists()
    assert (restored / "state.json").read_bytes() == s.state_path.read_bytes()


# ==== K12 modes ==============================================================

def _park_epoch(tmp_path):
    s, Reason = _epochs(tmp_path)

    def park():
        s.begin("c", reason=Reason.SUCCESSION, now=T2)
        return sorted(s.root.glob("epochs.json.v*"), key=lambda p: p.stat().st_mtime_ns)[-1]
    return s.path, park


def _park_goalbook(tmp_path):
    from hoh.goalbook import Goalbook
    gb = Goalbook(tmp_path)
    gb.write([_objective(1)])

    def park():
        gb.write([_objective(2)])
        return sorted(tmp_path.glob("goalbook.json.v*"))[-1]
    return gb.path, park


def _park_migrations(tmp_path):
    from hoh.migrations import migrate
    root = _mstore(tmp_path)

    def park():
        migrate(root, registry=_registry(), now=NOW, dry_run=False)
        return sorted(root.glob("state.json.v*"))[-1]
    return root / "state.json", park


def _park_project(tmp_path):
    ps = _ps(tmp_path)

    def park():
        ps.write_state(ps.read_state())
        return ps.parked_states()[-1]
    return ps.state_path, park


def _park_runstore(tmp_path, monkeypatch):
    s = _rs(tmp_path)
    _store_links_fail(monkeypatch)

    def park():
        s.write_state(s.read_state())
        return s.parked_states()[-1]
    return s.state_path, park


@pytest.mark.parametrize("writer", ["epoch", "goalbook", "migrations", "project", "runstore"])
def test_parked_copies_take_source_mode(tmp_path, monkeypatch, writer):
    """Catches a parked copy written with a fixed mode (0o644) instead of the
    mode of the file it copies."""
    make = {"epoch": _park_epoch, "goalbook": _park_goalbook,
            "migrations": _park_migrations, "project": _park_project,
            "runstore": lambda t: _park_runstore(t, monkeypatch)}[writer]
    source, park = make(tmp_path)
    source.chmod(0o640)
    parked = park()
    assert stat.S_IMODE(parked.stat().st_mode) == 0o640


@pytest.mark.parametrize("writer", ["goalbook", "migrations", "storage_pressure", "backup"])
def test_new_files_follow_umask(tmp_path, writer):
    """Catches a writer that today gets the umask's mode being given a fixed
    0o644: under umask 077 the file stays 0o600."""
    with _umask(0o077):
        if writer == "goalbook":
            from hoh.goalbook import Goalbook
            gb = Goalbook(tmp_path)
            gb.write([_objective(1)])
            out = gb.path
        elif writer == "migrations":
            from hoh.migrations import migrate
            root = _mstore(tmp_path)
            migrate(root, registry=_registry(), now=NOW, dry_run=False)
            out = root / "state.json"
        elif writer == "storage_pressure":
            from hoh import storage_pressure as sp
            out = tmp_path / "tracker.json"
            sp._atomic_write(out, "x")
        else:
            out = _rs(tmp_path).backup()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


# ==== K13 the writer scan ====================================================
#
# Keys: (file, enclosing qualname, kind) -- no line numbers, so the keys survive
# drift. Every call site counts; a key's count is pinned in CLASSIFIED.
#
# Resolution (fail closed): a flags or mode argument is resolved through plain
# assignments (`=`, `|=` and other augmented ones, annotated) to the same name
# in the enclosing function -- or, if the function never binds the name, at
# module level -- through `|` and conditional expressions. A name bound any
# other way (a parameter, a `for` or `with` target, a walrus, a tuple target,
# an import, `global`) or an expression that is not a constant (a call such as
# `getattr(os, "O_PATH", ...)`) is unresolved, and an unresolved flags or mode
# argument counts as a write of kind `os.open(unresolved)` / `open(unresolved)`.
# A resolved write flag or write mode wins over an unresolved part of the same
# expression. `*args` / `**kwargs` that may carry the argument are unresolved.
#
# Import aliases are normalised (`import os as _os`, `from os import replace`,
# `from shutil import copyfile`).
#
# `os.write(fd, ...)` on a descriptor the same function bound from `os.open`:
# when that open already counts as a write (`os.open(write)`), the write is that
# row; when its flags resolved read-only or did not resolve, the `os.write` is
# a hit of its own (kind `os.write`). So an open classified READ_BY_INSPECTION
# that starts writing turns this scan red instead of being absorbed.
#
# `.touch()` / `.hardlink_to()` are `Path` writes unless the receiver is a
# parameter annotated with a class of this package that defines that method
# itself (`ProjectStore.write_state(state: ProjectState)` calls
# `ProjectState.touch`, which stamps a time and writes nothing).

_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"})
_READ_FLAGS = frozenset({"O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK",
                         "O_CLOEXEC", "O_PATH", "O_NOATIME", "O_NOCTTY"})
_WRITE_FLAG_BITS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_UNRESOLVED = object()
_SCOPE_BREAKS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Module-level functions that write, by canonical name.
_FUNC_KINDS = {
    "os.replace": "os.replace", "os.rename": "os.rename", "os.link": "os.link",
    "os.truncate": "os.truncate",
    **{f"tempfile.{n}": f"tempfile.{n}" for n in ("mkstemp", "NamedTemporaryFile", "mkdtemp")},
    **{f"shutil.{n}": f"shutil.{n}"
       for n in ("copyfileobj", "copyfile", "copy", "copy2", "copytree", "move")},
}


def _walk_scope(node: ast.AST):
    """The nodes of one scope: nested functions, lambdas and classes are not
    entered (their bodies bind their own names)."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _SCOPE_BREAKS):
            yield from _walk_scope(child)


def _target_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _bindings(scope: ast.AST, name: str) -> tuple[list[ast.AST], bool]:
    """(values from plain assignments to `name` in this scope, whether `name`
    is also bound some other way there)."""
    values: list[ast.AST] = []
    other = False
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = scope.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        if any(p is not None and p.arg == name for p in params):
            other = True
    body = scope.body if isinstance(scope, (ast.Module, ast.FunctionDef,
                                            ast.AsyncFunctionDef)) else []
    for stmt in body:
        if isinstance(stmt, _SCOPE_BREAKS):
            if stmt.name == name:
                other = True
            continue
        for n in [stmt, *_walk_scope(stmt)]:
            if isinstance(n, _SCOPE_BREAKS):
                if getattr(n, "name", None) == name:
                    other = True
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        values.append(n.value)
                    elif not isinstance(t, ast.Name) and name in _target_names(t) \
                            and not isinstance(t, (ast.Attribute, ast.Subscript)):
                        other = True
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                if isinstance(n.target, ast.Name) and n.target.id == name \
                        and n.value is not None:
                    values.append(n.value)
            elif isinstance(n, ast.NamedExpr) and n.target.id == name:
                other = True
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) \
                    and name in _target_names(n.target):
                other = True
            elif isinstance(n, ast.withitem) and n.optional_vars is not None \
                    and name in _target_names(n.optional_vars):
                other = True
            elif isinstance(n, ast.ExceptHandler) and n.name == name:
                other = True
            elif isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                    (al.asname or al.name.split(".")[0]) == name for al in n.names):
                other = True
            elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
                other = True
    return values, other


def _package_methods(trees: list[ast.Module]) -> dict[str, set[str]]:
    """Class name -> the method names it defines, over the scanned package."""
    out: dict[str, set[str]] = {}
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                out.setdefault(n.name, set()).update(
                    m.name for m in n.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return out


class _Module:
    """One parsed source file: its import aliases and its scan."""

    def __init__(self, tree: ast.Module,
                 package_methods: dict[str, set[str]] | None = None) -> None:
        self.tree = tree
        self.package_methods = package_methods or {}
        self.aliases: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for al in n.names:
                    if al.asname:
                        self.aliases[al.asname] = al.name
                    else:
                        root = al.name.split(".")[0]
                        self.aliases[root] = root
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                for al in n.names:
                    self.aliases[al.asname or al.name] = f"{n.module}.{al.name}"
        self.modules = {v.split(".")[0] for v in self.aliases.values()}

    def canon(self, expr: ast.AST) -> str | None:
        """Dotted name of an expression with import aliases normalised."""
        if isinstance(expr, ast.Name):
            return self.aliases.get(expr.id, expr.id)
        if isinstance(expr, ast.Attribute):
            base = self.canon(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    def is_module_ref(self, expr: ast.AST) -> bool:
        """Is `expr` (the object a method is called on) an imported module?"""
        return isinstance(expr, ast.Name) and expr.id in self.aliases \
            and self.aliases[expr.id].split(".")[0] in self.modules \
            and "." not in self.aliases[expr.id]

    # -- resolution --------------------------------------------------------- #

    def resolve(self, expr: ast.AST, scopes: list[ast.AST], depth: int = 0) -> set:
        """The constants an expression can take: str or int values, or `O_*`
        flag names; `_UNRESOLVED` for anything else."""
        if depth > 12:
            return {_UNRESOLVED}
        if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, int)) \
                and not isinstance(expr.value, bool):
            return {expr.value}
        c = self.canon(expr) if isinstance(expr, (ast.Name, ast.Attribute)) else None
        if c and c.startswith("os.O_") and c.count(".") == 1:
            return {c[3:]}
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self.resolve(expr.left, scopes, depth + 1) | \
                self.resolve(expr.right, scopes, depth + 1)
        if isinstance(expr, ast.IfExp):
            return self.resolve(expr.body, scopes, depth + 1) | \
                self.resolve(expr.orelse, scopes, depth + 1)
        if isinstance(expr, ast.Name):
            for i, scope in enumerate(scopes):
                values, other = _bindings(scope, expr.id)
                if values or other:
                    out = {_UNRESOLVED} if other else set()
                    for v in values:
                        out |= self.resolve(v, scopes[i:], depth + 1)
                    return out
        return {_UNRESOLVED}

    @staticmethod
    def arg(call: ast.Call, pos: int, kw: str):
        """The argument at `pos` / keyword `kw`; None when absent; an
        unresolvable marker when `*args` or `**kwargs` may carry it."""
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Starred):
                return _UNRESOLVED
            if i == pos:
                return a
        for k in call.keywords:
            if k.arg == kw:
                return k.value
        if any(k.arg is None for k in call.keywords):
            return _UNRESOLVED
        return None

    def flags_kind(self, call: ast.Call, scopes) -> str | None:
        flags = self.arg(call, 1, "flags")
        vals = {_UNRESOLVED} if flags is None or flags is _UNRESOLVED \
            else self.resolve(flags, scopes)
        if any((isinstance(v, str) and v in _WRITE_FLAGS)
               or (isinstance(v, int) and v & _WRITE_FLAG_BITS) for v in vals):
            return "os.open(write)"
        if _UNRESOLVED in vals or any(isinstance(v, str) and v not in _READ_FLAGS
                                      for v in vals):
            return "os.open(unresolved)"
        return None

    def mode_kind(self, call: ast.Call, scopes, pos: int, label: str,
                  tar: bool = False) -> str | None:
        mode = self.arg(call, pos, "mode")
        if mode is None:
            return None  # the default mode of every scanned opener reads
        vals = {_UNRESOLVED} if mode is _UNRESOLVED else self.resolve(mode, scopes)
        strs = [v.split(":")[0] if tar else v for v in vals if isinstance(v, str)]
        if any(set(s) & set("wax+") for s in strs):
            return f"{label}(write)"
        if len(strs) != len(vals):
            return f"{label}(unresolved)"
        return None

    def own_method(self, receiver: ast.AST, method: str, scopes) -> bool:
        """Is `receiver` a parameter annotated with a package class that
        defines `method` itself?"""
        if not isinstance(receiver, ast.Name) or len(scopes) < 2:
            return False
        func = scopes[0]
        values, other = _bindings(func, receiver.id)
        if values:
            return False
        a = func.args
        for prm in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            if prm.arg == receiver.id and prm.annotation is not None:
                ann = prm.annotation
                name = ann.value if isinstance(ann, ast.Constant) else self.canon(ann)
                if isinstance(name, str):
                    return method in self.package_methods.get(name.split(".")[-1], set())
        return False

    # -- kinds -------------------------------------------------------------- #

    def kind_of(self, call: ast.Call, scopes) -> str | None:
        f = call.func
        c = self.canon(f)
        if c in _FUNC_KINDS:
            return _FUNC_KINDS[c]
        if c == "os.open":
            return self.flags_kind(call, scopes)
        if c == "os.fdopen":
            return self.mode_kind(call, scopes, 1, "os.fdopen")
        if c in ("open", "io.open", "builtins.open"):
            return self.mode_kind(call, scopes, 1, "open")
        if c in ("gzip.GzipFile", "gzip.open"):
            name = self.arg(call, 0, "filename")
            if name is None or (isinstance(name, ast.Constant) and name.value is None):
                return None
            return self.mode_kind(call, scopes, 1, c)
        if c == "tarfile.open":
            k = self.mode_kind(call, scopes, 1, "tarfile.open", tar=True)
            name = self.arg(call, 0, "name")
            no_name = name is None or (isinstance(name, ast.Constant) and name.value is None)
            fileobj = self.arg(call, 2, "fileobj")
            if k == "tarfile.open(write)" and no_name and isinstance(fileobj, ast.AST):
                if isinstance(fileobj, ast.Call) and \
                        self.canon(fileobj.func) in ("gzip.GzipFile", "gzip.open"):
                    return "tarfile.open(write)"
                return "tarfile.open(fileobj)"
            return k
        if isinstance(f, ast.Attribute) and not self.is_module_ref(f.value):
            if f.attr in ("write_text", "write_bytes", "extractall"):
                return f.attr
            if f.attr in ("touch", "hardlink_to"):
                if self.own_method(f.value, f.attr, scopes):
                    return None
                return f"Path.{f.attr}"
            if f.attr in ("replace", "rename") and len(call.args) == 1 \
                    and not call.keywords and not isinstance(call.args[0], ast.Starred):
                return f"Path.{f.attr}"
            if f.attr == "open":
                return self.mode_kind(call, scopes, 0, "Path.open")
        return None

    def write_on_own_fd(self, call: ast.Call, scopes) -> bool:
        """`os.write(fd, ...)` where this function bound `fd` from an `os.open`
        that does not already count as a write."""
        if self.canon(call.func) != "os.write" or not call.args \
                or not isinstance(call.args[0], ast.Name) or len(scopes) < 2:
            return False
        values, _other = _bindings(scopes[0], call.args[0].id)
        opens = [v for v in values
                 if isinstance(v, ast.Call) and self.canon(v.func) == "os.open"]
        return any(self.flags_kind(v, scopes) != "os.open(write)" for v in opens)

    def scan(self) -> list[tuple[str, str]]:
        """[(qualname, kind)], one per call site."""
        hits: list[tuple[str, str]] = []
        module_scope = self.tree

        def visit(node, qual, scopes):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, f"{qual}.{child.name}" if qual else child.name,
                          [child] + scopes)
                    continue
                if isinstance(child, ast.ClassDef):
                    visit(child, f"{qual}.{child.name}" if qual else child.name, scopes)
                    continue
                if isinstance(child, ast.Call):
                    k = self.kind_of(child, scopes)
                    if k:
                        hits.append((qual or "<module>", k))
                    elif self.write_on_own_fd(child, scopes):
                        hits.append((qual or "<module>", "os.write"))
                visit(child, qual, scopes)
        visit(self.tree, "", [module_scope])
        return hits


def scan(root: Path = ROOT) -> list[tuple[str, str, str]]:
    """Every raw write call site in `<root>/src/hoh/*.py`: (file, qualname, kind)."""
    paths = sorted((root / "src" / "hoh").glob("*.py"))
    trees = [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in paths]
    methods = _package_methods(trees)
    hits = []
    for path, tree in zip(paths, trees):
        hits += [(path.name, q, k) for q, k in _Module(tree, methods).scan()]
    return hits


#: The file whose writes are the primitive. PRIMITIVE is classified by file,
#: not by function, because the scope allows helpers the primitives share.
PRIMITIVE_FILE = "durable_io.py"

#: Every raw write call that remains after H-a (spec Appendix A with Revision
#: 2a): key -> (class, call sites of that kind in that function, reason).
#: PRIMITIVE rows are keyed (file, "*", kind) and count the whole file.
CLASSIFIED: dict[tuple[str, str, str], tuple[str, int, str]] = {
    # -- PRIMITIVE: durable_io ------------------------------------------------
    ("durable_io.py", "*", "os.open(write)"): (
        "PRIMITIVE", 3, "the primitives' temp files (durable_write, durable_create) and the "
        "append (durable_append, added by H-a)"),
    ("durable_io.py", "*", "os.replace"): (
        "PRIMITIVE", 3, "durable_write's publish (path form and, S2, dir_fd form) and "
        "durable_publish_tree's merge (S1)"),
    ("durable_io.py", "*", "os.rename"): (
        "PRIMITIVE", 1, "durable_publish_tree's whole-tree publish (S1)"),
    ("durable_io.py", "*", "os.link"): (
        "PRIMITIVE", 2, "durable_create's publish without replacing (added by H-a; path form "
        "and, S2, dir_fd form)"),
    ("store.py", "RunStore._restore", "extractall"): (
        "STAGED_EXTRACT", 1, "extracts into <root>/_restore/<staging> after a room check; "
        "published by durable_publish_tree"),
    # -- OWN_DURABLE: the A1 storage modules' own temp-fsync-publish helpers --
    ("archive_bundle.py", "ArchiveBundle._write_tar", "os.fdopen(write)"): (
        "OWN_DURABLE", 1, "payload into a .building staging dir, published by "
        "rename_noreplace"),
    ("archive_bundle.py", "ArchiveBundle._write_tar", "os.open(write)"): (
        "OWN_DURABLE", 1, "O_EXCL in staging dir"),
    ("archive_bundle.py", "ArchiveBundle._write_tar", "tarfile.open(fileobj)"): (
        "OWN_DURABLE", 1, "streams into the staging payload fd (fileobj); the fd's own "
        "os.open row is the write"),
    ("archive_bundle.py", "_link_noreplace", "os.link"): (
        "OWN_DURABLE", 1, "publish without replacing"),
    ("archive_bundle.py", "_write_new", "os.open(write)"): (
        "OWN_DURABLE", 1, "O_EXCL into staging or a dot-temp, published by rename_noreplace"),
    ("archive_target.py", "_copy_file", "os.open(write)"): (
        "OWN_DURABLE", 1, "dot-temp, fsync, publish without replacing"),
    ("storage_coordinator.py", "_create_exclusive", "os.open(write)"): (
        "OWN_DURABLE", 1, "dot-temp, fsync, renameat2 NOREPLACE"),
    ("storage_host.py", "_write_new", "os.fdopen(write)"): (
        "OWN_DURABLE", 1, "O_EXCL temp, fsync, replace onto a fresh name, dir fsync"),
    ("storage_host.py", "_write_new", "os.open(write)"): ("OWN_DURABLE", 1, "same"),
    ("storage_host.py", "_write_new", "os.replace"): (
        "OWN_DURABLE", 1, "same (target name proven fresh first)"),
    ("storage_registry.py", "_atomic_write_json", "os.fdopen(write)"): (
        "OWN_DURABLE", 1, "O_EXCL temp, fsync, replace, dir fsync; litter-only on failure "
        "(storage_registry reserved)"),
    ("storage_registry.py", "_atomic_write_json", "os.open(write)"): ("OWN_DURABLE", 1, "same"),
    ("storage_registry.py", "_atomic_write_json", "os.replace"): ("OWN_DURABLE", 1, "same"),
    ("verified_relocation.py", "write_new", "os.open(write)"): (
        "OWN_DURABLE", 1, "dot-temp, fsync, rename_noreplace"),
    # -- APPEND_GUARDED / APPEND_TOLERATED ------------------------------------
    ("archive_target.py", "_append_line", "os.open(write)"): (
        "APPEND_GUARDED", 1, "one write, torn-tail guard, short-write check, fsync"),
    ("storage_pressure.py", "StorageAdmission._append_history", "os.open(write)"): (
        "APPEND_GUARDED", 1, "precedent of durable_append"),
    ("verified_relocation.py", "_append_intent", "os.open(write)"): (
        "APPEND_GUARDED", 1, "torn-tail guard, whole write, fsync, dir fsync"),
    ("verified_relocation.py", "_append_line", "os.open(write)"): (
        "APPEND_GUARDED", 1, "flock, torn-tail guard, whole write, fsync"),
    ("storage_steward.py", "_append_event", "os.open(write)"): (
        "APPEND_TOLERATED", 1, "steward events dropped by design; no torn-tail guard, "
        "short write unchecked (tracked todo)"),
    # -- LOCK_FILE -------------------------------------------------------------
    ("archive_target.py", "_Flock.__enter__", "os.open(write)"): ("LOCK_FILE", 1, "flock target"),
    ("projectstore.py", "ProjectStore.lock", "open(write)"): (
        "LOCK_FILE", 1, "flock target; holder line informational"),
    ("reservation.py", "BudgetReservations._locked", "os.open(write)"): (
        "LOCK_FILE", 1, "flock target"),
    ("resumeloop.py", "_flock", "os.open(write)"): ("LOCK_FILE", 1, "flock target"),
    ("storage_coordinator.py", "StorageCoordinator._locked", "os.open(write)"): (
        "LOCK_FILE", 1, "flock target"),
    ("storage_registry.py", "StorageRegistry._locked", "os.open(write)"): (
        "LOCK_FILE", 1, "flock target"),
    ("storage_registry.py", "StorageRegistry._locked_project_only", "os.open(write)"): (
        "LOCK_FILE", 1, "flock target"),
    ("store.py", "RunStore.lock", "open(write)"): (
        "LOCK_FILE", 1, "flock target; holder line informational"),
    ("store.py", "RunStore.process_lock", "os.open(write)"): ("LOCK_FILE", 1, "process lock"),
    ("succession.py", "MasterAuthority._locked", "os.open(write)"): (
        "LOCK_FILE", 1, "flock target"),
    ("timers.py", "TimerStore._mutating", "os.open(write)"): ("LOCK_FILE", 1, "flock target"),
    ("verified_relocation.py", "VerifiedRelocation._hold_run", "os.open(write)"): (
        "LOCK_FILE", 1, "run .process.lock (O_RDONLY|O_CREAT)"),
    ("verified_relocation.py", "VerifiedRelocation.execute", "os.open(write)"): (
        "LOCK_FILE", 1, "relocation lock"),
    ("verified_relocation.py", "VerifiedRelocation.recover", "os.open(write)"): (
        "LOCK_FILE", 1, "relocation lock"),
    # -- RENAME_ONLY: versioning renames that write no bytes ------------------
    ("controller.py", "Controller._arena", "Path.rename"): (
        "RENAME_ONLY", 1, "arena versioning rename"),
    ("dispatchers.py", "HerdrDispatcher._dispatch", "Path.rename"): (
        "RENAME_ONLY", 1, "answer versioning rename"),
    ("runner.py", "_fresh_scratch", "Path.rename"): ("RENAME_ONLY", 1, "scratch versioning rename"),
    ("waverecovery.py", "_quarantine", "os.rename"): (
        "RENAME_ONLY", 1, "a file that is not an outcome set aside under a versioned name, never removed"),
    ("storage_pressure.py", "StorageAdmission._scan", "os.replace"): (
        "RENAME_ONLY", 1, "stale reservation versioning rename"),
    ("storage_pressure.py", "StorageAdmission.release", "os.replace"): (
        "RENAME_ONLY", 1, "closed reservation versioning rename"),
    ("storage_registry.py", "_rename_versioned", "os.rename"): (
        "RENAME_ONLY", 1, "versioning rename"),
    ("preflightgate.py", "measure_default", "os.rename"): (
        "RENAME_ONLY", 1, "an old preflight report put aside under a versioned name before the new one "
        "is created with `durable_create`; never removed, never overwritten (the name is checked first)"),
    ("leaseadmin.py", "archive_ended", "os.rename"): (
        "RENAME_ONLY", 1, "an ended run lease (and its parked copies) moved into the pass directory of "
        "<lease dir>-archive after the manifest is durable; the target directory is fresh, nothing is replaced"),
    ("store.py", "RunStore.prune", "Path.rename"): ("RENAME_ONLY", 5, "attic moves"),
    ("cli.py", "_park_stale_record", "Path.rename"): (
        "RENAME_ONLY", 1, "a stale integrity record set aside by a versioned rename; "
        "needs no free space"),
    # -- PARK_LINK / IN_MEMORY --------------------------------------------------
    ("dispatchers.py", "_write_leaf.park", "os.link"): (
        "PARK_LINK", 1, "the version name of the current file, a second name for the same "
        "inode; no bytes written; the new bytes go through `durable_write`"),
    ("store.py", "RunStore._park", "os.link"): (
        "PARK_LINK", 1, "no bytes written; inode condition documented in _park"),
    ("store.py", "RunStore.backup", "tarfile.open(fileobj)"): (
        "IN_MEMORY", 1, "W15 builds the archive in io.BytesIO; it reaches disk only "
        "through durable_create (added by H-a)"),
    # -- READ_BY_INSPECTION -----------------------------------------------------
    ("verified_relocation.py", "_ancestry", "os.open(unresolved)"): (
        "READ_BY_INSPECTION", 2, "flags from getattr(os, 'O_PATH', O_RDONLY) | O_DIRECTORY: "
        "not statically resolvable, read-only by inspection"),
    # -- OUTPUT_NOT_STATE --------------------------------------------------------
    ("archive_bundle.py", "_verify_tree", "extractall"): (
        "OUTPUT_NOT_STATE", 1, "verification extract into a checked extract root"),
    ("cli.py", "_project_drive", "os.fdopen(write)"): (
        "OUTPUT_NOT_STATE", 1, "--token-out file the user asked for (O_TRUNC in place)"),
    ("cli.py", "_project_drive", "os.open(write)"): ("OUTPUT_NOT_STATE", 1, "--token-out"),
    ("cli.py", "cmd_report", "write_text"): ("OUTPUT_NOT_STATE", 1, "--out report file"),
    ("runner.py", "run_check", "open(write)"): (
        "OUTPUT_NOT_STATE", 1, "check output capture (sink)"),
    ("runner.py", "_record_own_scratch", "os.open(write)"): (
        "OUTPUT_NOT_STATE", 1, "the identity record beside the runner's reusable scratch "
        "(P2-42 residuals): coordination state about a temporary directory, never evidence; a "
        "crash leaves the scratch unreused (the next process parks it), a lost record is "
        "written again by the owner's next check, a torn last line (no newline) makes callers refuse until "
        "the owner's next check closes it (fail closed)"),
    ("storage_ephemeral.py", "EphemeralRoots.create", "tempfile.mkdtemp"): (
        "OUTPUT_NOT_STATE", 1, "the workspace directory of a registered temporary root "
        "(P2-42): its record is written beside it before any content and its creator "
        "cleans it; temporary state, never evidence"),
    ("workspace.py", "materialize", "extractall"): (
        "OUTPUT_NOT_STATE", 1, "arena materialisation (reproducible scratch)"),
    ("workspace.py", "materialize", "tempfile.NamedTemporaryFile"): (
        "OUTPUT_NOT_STATE", 1, "temp tar for arena materialisation"),
    ("workspace.py", "working_tree", "tempfile.NamedTemporaryFile"): (
        "OUTPUT_NOT_STATE", 1, "temp git index"),
    # MG-RB2: the merge gate's matrix reading writes a merge tree out to a temporary directory it owns and
    # removes (a TemporaryDirectory of `_matrix_reading`), reads it, and keeps nothing of it
    ("mergegate.py", "_matrix_reading", "extractall"): (
        "OUTPUT_NOT_STATE", 1, "the merge tree's src/hoh, tools, tests written to the reading's own temporary "
        "directory (tarfile data filter); removed with it"),
    ("mergegate.py", "_matrix_reading", "write_bytes"): (
        "OUTPUT_NOT_STATE", 1, "the base's classifier written over the extracted copy, in the same temporary "
        "directory"),
    ("mergegate.py", "_matrix_reading", "os.replace"): (
        "OUTPUT_NOT_STATE", 1, "that classifier put in place by replacing the extracted entry (a link included)"),
    ("mergegate.py", "_matrix_reading", "write_text"): (
        "OUTPUT_NOT_STATE", 2, "the driver script and the rows to read, in the reading's own temporary "
        "directory"),
}

_CLASSES = {"PRIMITIVE", "PARK_LINK", "LOCK_FILE", "RENAME_ONLY", "OWN_DURABLE",
            "APPEND_GUARDED", "APPEND_TOLERATED", "IN_MEMORY", "READ_BY_INSPECTION",
            "OUTPUT_NOT_STATE", "STAGED_EXTRACT", "DEFERRED", "MIGRATE_LATER"}

#: The rows H-a migrates (W1-W15, Appendix A class MIGRATE_H): none of them may
#: be a hit afterwards.
MIGRATED = {
    ("epoch.py", "EpochStore._preserve_previous", "open(write)"),          # W2
    ("epoch.py", "EpochStore._preserve_previous", "shutil.copyfileobj"),   # W2
    ("epoch.py", "EpochStore._write", "os.fdopen(write)"),                 # W1
    ("epoch.py", "EpochStore._write", "os.replace"),                       # W1
    ("epoch.py", "EpochStore._write", "tempfile.mkstemp"),                 # W1
    ("goalbook.py", "Goalbook._park", "open(write)"),                      # W4
    ("goalbook.py", "Goalbook.write", "os.replace"),                       # W3
    ("goalbook.py", "Goalbook.write", "write_text"),                       # W3
    ("migrations.py", "_atomic_write", "open(write)"),                     # W7
    ("migrations.py", "_atomic_write", "os.replace"),                      # W7
    ("migrations.py", "_preserve", "open(write)"),                         # W8
    ("projectstore.py", "ProjectStore._park", "os.replace"),               # W12 (moved live state)
    ("storage_pressure.py", "_atomic_write", "open(write)"),               # W9
    ("storage_pressure.py", "_atomic_write", "os.replace"),                # W9
    ("store.py", "RunStore._park", "open(write)"),                         # W14 fallback
    ("store.py", "RunStore.backup", "tarfile.open(write)"),                # W15
    ("succession.py", "MasterAuthority._write", "open(write)"),            # W13
    ("telemetry.py", "TelemetryLog.append", "open(write)"),                # W6
    ("telemetry.py", "TelemetryLog.rewrite_sorted", "Path.replace"),       # W5 .partial rename
    ("telemetry.py", "TelemetryLog.rewrite_sorted", "os.fdopen(write)"),   # W5
    ("telemetry.py", "TelemetryLog.rewrite_sorted", "os.replace"),         # W5
    ("telemetry.py", "TelemetryLog.rewrite_sorted", "tempfile.mkstemp"),   # W5
    ("trust.py", "register", "os.replace"),                                # W11
    ("trust.py", "register", "write_text"),                                # W10/W11
    # P2-41 closure S1 (W16-W22):
    ("amendment.py", "park_and_amend", "Path.rename"),
    ("amendment.py", "park_and_amend", "write_text"),
    ("approval_policy.py", "RoleApprovalPolicy.write_role_files", "write_text"),
    ("governance.py", "Governor.decide", "Path.open(write)"),
    ("governance.py", "bind", "write_text"),
    ("governance.py", "record_override", "Path.open(write)"),
    # P2-41 closure S3:
    ("launcher.py", "HohRunLauncher.prepare", "write_text"),
    ("cli.py", "_cmd_run", "write_text"),
    ("cli.py", "_integrity_close", "write_text"),
    # P2-41 closure S2:
    ("dispatchers.py", "HarnessDispatcher.dispatch", "write_text"),
    ("dispatchers.py", "_append_audit", "os.open(write)"),
    ("dispatchers.py", "_write_leaf", "os.link"),
    ("dispatchers.py", "_write_leaf", "os.open(write)"),
    ("dispatchers.py", "_write_leaf", "os.replace"),
}


def _key(file: str, qual: str, kind: str) -> tuple[str, str, str]:
    return (file, "*", kind) if file == PRIMITIVE_FILE else (file, qual, kind)


def _counted(root: Path = ROOT) -> Counter:
    hits = scan(root)
    assert len({f for f, _q, _k in hits}) >= 20, "the scan read too few files"
    return Counter(_key(*h) for h in hits)


def test_scan_no_unclassified_writer():
    """Catches a raw write call in src/hoh that is not classified -- a W1-W15
    row left raw, a new writer, or a new call site of a classified kind (each
    row pins its call-site count)."""
    counted = _counted()
    unclassified = sorted(k for k in counted if k not in CLASSIFIED)
    assert unclassified == []
    wrong_count = sorted((k, counted[k], CLASSIFIED[k][1]) for k in counted
                         if counted[k] != CLASSIFIED[k][1])
    assert wrong_count == []
    assert {c for c, _n, _r in CLASSIFIED.values()} <= _CLASSES


def test_scan_allowlist_has_no_stale_entry():
    """Catches a CLASSIFIED row whose call is gone: the allowlist may only name
    writes that exist."""
    counted = _counted()
    assert sorted(k for k in CLASSIFIED if counted[k] == 0) == []


def test_scan_migrated_rows_absent():
    """Catches a W1-W15 row still writing raw. Keyed by row, so RunStore._park's
    kept os.link (PARK_LINK) and RunStore.backup's tarfile.open(fileobj)
    (IN_MEMORY) do not trip it."""
    hits = set(scan())
    assert sorted(hits & MIGRATED) == []
    assert not (set(CLASSIFIED) & MIGRATED)


_SYNTHETIC = '''
import os, tarfile, gzip
import os as _os
from os import replace as _replace, O_WRONLY
from shutil import copyfile
from pathlib import Path

MODULE_FLAGS = os.O_WRONLY | os.O_APPEND

def local_flags(p):
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL
    return os.open(p, flags, 0o600)

def read_only(p):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    return os.open(p, flags)

def unresolvable(p, extra):
    return os.open(p, extra)

def via_getattr(p):
    return os.open(p, getattr(os, "O_PATH", os.O_RDONLY))

def mode_local(p):
    mode = "a"
    return open(p, mode)

def mode_param(p, mode):
    return open(p, mode)

def mode_read(p):
    return open(p)

def in_memory(buf):
    return tarfile.open(fileobj=buf, mode="w:gz")

def read_tar(p):
    return tarfile.open(p, "r:xz")

def module_flags(p):
    return os.open(p, MODULE_FLAGS)

def reassigned_param(p, flags):
    flags |= os.O_RDONLY
    return os.open(p, flags)

def for_target(p):
    for flags in (os.O_RDONLY,):
        os.open(p, flags)

def walrus(p):
    if (flags := os.O_RDONLY):
        return os.open(p, flags)

def kwargs_open(p, **kw):
    return open(p, **kw)

def write_beats_unresolved(p):
    return os.open(p, getattr(os, "O_PATH", 0) | os.O_WRONLY)

def aliases(a, b):
    _os.replace(a, b)
    _replace(a, b)
    copyfile(a, b)
    return _os.open(a, O_WRONLY)

def new_apis(p, q, fd):
    os.truncate(p, 0)
    Path(p).touch()
    Path(q).hardlink_to(p)
    gzip.GzipFile(p, "wb")
    return tarfile.open(fileobj=gzip.GzipFile(q, mode="wb"), mode="w")

def write_on_read_fd(p, data):
    fd = os.open(p, os.O_RDONLY)
    os.write(fd, data)

def write_on_write_fd(p, data):
    fd = os.open(p, os.O_WRONLY | os.O_APPEND)
    os.write(fd, data)

def not_paths(s, d):
    s.replace("a", "b")
    d.replace(tzinfo=None)

class Stamped:
    def touch(self):
        pass

def own_touch(m: Stamped, q: Path, r):
    m.touch()
    q.touch()
    r.touch()
'''


def test_scan_resolves_flags_and_fails_closed(tmp_path):
    """The scan on a synthetic module: flags and modes held in locals resolve;
    read-only ones are no hit; anything it cannot resolve counts as a write
    (fail closed). Catches a scan that ignores local assignments or treats
    unresolvable flags as a read (either would let a writer hide)."""
    pkg = tmp_path / "src" / "hoh"
    pkg.mkdir(parents=True)
    (pkg / "synthetic.py").write_text(_SYNTHETIC, encoding="utf-8")
    got = Counter((q, k) for _f, q, k in scan(tmp_path))
    assert got == Counter({
        ("local_flags", "os.open(write)"): 1,
        ("unresolvable", "os.open(unresolved)"): 1,
        ("via_getattr", "os.open(unresolved)"): 1,
        ("mode_local", "open(write)"): 1,
        ("mode_param", "open(unresolved)"): 1,
        ("in_memory", "tarfile.open(fileobj)"): 1,
        # Revision 2a.
        ("module_flags", "os.open(write)"): 1,
        ("reassigned_param", "os.open(unresolved)"): 1,
        ("for_target", "os.open(unresolved)"): 1,
        ("walrus", "os.open(unresolved)"): 1,
        ("kwargs_open", "open(unresolved)"): 1,
        ("write_beats_unresolved", "os.open(write)"): 1,
        ("aliases", "os.replace"): 2,
        ("aliases", "shutil.copyfile"): 1,
        ("aliases", "os.open(write)"): 1,
        ("new_apis", "os.truncate"): 1,
        ("new_apis", "Path.touch"): 1,
        ("new_apis", "Path.hardlink_to"): 1,
        ("new_apis", "gzip.GzipFile(write)"): 2,
        ("new_apis", "tarfile.open(write)"): 1,
        ("write_on_read_fd", "os.write"): 1,
        ("write_on_write_fd", "os.open(write)"): 1,
        ("own_touch", "Path.touch"): 2,
    })


def test_no_deferred_or_migrate_later_row_remains():
    """Catches a CLASSIFIED row that still defers a writer: after P2-41 closure
    S2 the scan has no DEFERRED and no MIGRATE_LATER row, which is what the
    register's `equivalent` named."""
    left = sorted(k for k, (c, _n, _r) in CLASSIFIED.items()
                  if c in ("DEFERRED", "MIGRATE_LATER"))
    assert left == []


def test_docstring_names_the_migrations_and_no_remaining_writer():
    """Catches a durable_io docstring that still lists remaining writers, drops
    one of the migration sections or their order, or loses W1..W15."""
    doc = durable_io.__doc__ or ""
    assert "Still remaining" not in doc
    assert "while these remain" not in doc
    closing = "No writer remains in the list of the register's `equivalent`"
    assert closing in doc
    kept = doc.index("*(As written on 2026-10-01; superseded by the next sections.)*")
    order = [doc.index(h) for h in ("Migrated by P2-41b H-a (2026-10)",
                                    "Migrated by P2-41 closure S1",
                                    "Migrated by P2-41 closure S3",
                                    "Migrated by P2-41 closure S2")]
    assert kept < order[0] < order[1] < order[2] < order[3] < doc.index(closing)
    migrated = doc[order[0]:order[1]]
    assert all(re.search(rf"\bW{i}\b", migrated) for i in range(1, 16))
