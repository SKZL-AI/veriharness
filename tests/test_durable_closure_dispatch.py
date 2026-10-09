"""P2-41 closure S2: the dispatcher's leaf write, audit append and recorded
answer go through the durable-write primitives, which learn to write relative to
a directory descriptor.

`dispatchers._write_leaf` (the versioned write of prompt files and markers) and
`dispatchers._append_audit` (the prompt-binding audit file) act relative to a
directory descriptor opened with `O_NOFOLLOW`, so a role that swaps a directory
for a link between the check and the write cannot redirect the write. The
primitives therefore get a `dir_fd` form first (`durable_write`, `durable_create`,
`durable_append`, `require_room`, plus a `before_replace` hook and a
`create_mode`), and the dispatcher uses them. Every test names what it catches.
Fault cases come with a control that changes only the condition under test.

Fault injection goes through a proxy installed as `durable_io.os` (the seam of
`tests/test_durable_closure.py`, whose small helpers are copied here: test
modules do not import each other). A fault is aimed by the file behind the
descriptor (`/proc/self/fd/<fd>`), so it hits exactly one write. No disk is
filled, no subprocess is started (`subprocess.run` is replaced where a dispatcher
would call it), nothing writes outside `tmp_path`, and a test that changes the
umask restores it in a `finally`.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import types
from pathlib import Path

import pytest

from hoh import dispatchers, durable_io
from hoh.contracts import Role
from hoh.controller import DispatchError
from hoh.durable_io import (
    DiskFullError,
    DurableWriteNotSynced,
    UnsafeRecordFile,
    durable_append,
    durable_create,
    durable_write,
    require_room,
)

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


def _uninstall(monkeypatch) -> None:
    monkeypatch.setattr(durable_io, "os", os)


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
    `prefix`."""
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


def _zero_write_for(prefix: str, limit: int = 50):
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


def _statvfs_reporting(free_bytes: int):
    def fake(_path):
        return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1, f_bsize=1,
                                     f_blocks=free_bytes, f_bfree=free_bytes)
    return fake


@contextlib.contextmanager
def _dir_fd(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in Path(directory).iterdir())


def _dir(tmp_path: Path, name: str = "run") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


# ==== the dir_fd form of the primitives =============================================

def test_dir_fd_primitives_land_in_the_descriptors_directory_not_the_lexical_path(tmp_path):
    """Catches: a primitive that opens the directory again by name. The
    descriptor is opened on `run/`, which is then renamed to `moved/` and
    replaced by a link to `elsewhere/`; a write through the descriptor must land
    in `moved/` (the directory that was checked), never in `elsewhere/`. This is
    the TOCTOU property the dispatcher's writers had before the migration."""
    run = _dir(tmp_path)
    elsewhere = _dir(tmp_path, "elsewhere")
    with _dir_fd(run) as fd:
        moved = tmp_path / "moved"
        run.rename(moved)
        run.symlink_to(elsewhere)
        durable_write(Path("w.json"), b"w", mode=0o600, dir_fd=fd, min_free_bytes=0)
        durable_create(Path("c.json"), b"c", mode=0o600, dir_fd=fd, min_free_bytes=0)
        durable_append(Path("log.jsonl"), b"{}\n", single_link=True, dir_fd=fd,
                       min_free_bytes=0, create_mode=0o600)
    assert _names(moved) == ["c.json", "log.jsonl", "w.json"]
    assert _names(elsewhere) == []
    assert (moved / "w.json").read_bytes() == b"w"
    assert (moved / "c.json").read_bytes() == b"c"
    assert (moved / "log.jsonl").read_bytes() == b"{}\n"
    assert stat.S_IMODE((moved / "log.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["a/b", "../x", "..", ".", "/etc/x", ""])
def test_dir_fd_form_refuses_a_path_that_is_not_a_bare_name(tmp_path, bad):
    """Catches: a name the kernel would resolve from the descriptor and that
    could leave the checked directory (`../x`, an absolute path ignores the
    descriptor altogether). `ValueError` before anything is touched."""
    run = _dir(tmp_path)
    with _dir_fd(run) as fd:
        with pytest.raises(ValueError):
            durable_write(bad, b"x", dir_fd=fd, min_free_bytes=0)
        with pytest.raises(ValueError):
            durable_create(bad, b"x", dir_fd=fd, min_free_bytes=0)
        with pytest.raises(ValueError):
            durable_append(bad, b"x\n", dir_fd=fd, min_free_bytes=0)
    assert _names(run) == []
    assert not (tmp_path / "x").exists()


def test_dir_fd_write_full_disk_keeps_the_target_and_leaves_no_temp(tmp_path, monkeypatch):
    """Catches: an abandon that does not work relative to the descriptor (it
    would open the temp's parent `.` by path) and leaves the temp file of a full
    disk behind. Control: after `undo` the same call succeeds."""
    run = _dir(tmp_path)
    (run / "t.json").write_bytes(b"old")
    with _dir_fd(run) as fd:
        _install(monkeypatch, write=_err_write_for(".t.json.tmp."))
        with pytest.raises(DiskFullError):
            durable_write(Path("t.json"), b"new", dir_fd=fd, min_free_bytes=0)
        assert _names(run) == ["t.json"]
        assert (run / "t.json").read_bytes() == b"old"
        _uninstall(monkeypatch)
        durable_write(Path("t.json"), b"new", dir_fd=fd, min_free_bytes=0)
    assert (run / "t.json").read_bytes() == b"new"
    assert _names(run) == ["t.json"]


def test_dir_fd_space_check_refuses_before_creating_anything(tmp_path, monkeypatch):
    """Catches: a free-space pre-check that runs after the temp file is created
    (or not at all) in the descriptor form. Control: the same calls with room."""
    run = _dir(tmp_path)
    with _dir_fd(run) as fd:
        _install(monkeypatch, statvfs=_statvfs_reporting(100))
        with pytest.raises(DiskFullError):
            durable_write(Path("w.json"), b"x", dir_fd=fd)
        with pytest.raises(DiskFullError):
            durable_create(Path("c.json"), b"x", dir_fd=fd)
        with pytest.raises(DiskFullError):
            durable_append(Path("a.jsonl"), b"x\n", dir_fd=fd)
        assert _names(run) == []
        _uninstall(monkeypatch)
        durable_write(Path("w.json"), b"x", dir_fd=fd)
    assert _names(run) == ["w.json"]


def test_dir_fd_space_check_asks_the_descriptors_filesystem(tmp_path, monkeypatch):
    """Catches (review p241s2spec-b 2, X1): a space check on `path.parent`, which
    for a bare name is `Path(".")`, the process's working directory, not the
    directory that was checked. Each primitive and `require_room` must ask
    `statvfs` with the integer descriptor."""
    run = _dir(tmp_path)
    asked: list = []

    def statvfs(arg):
        asked.append(arg)
        return types.SimpleNamespace(f_bavail=1 << 40, f_frsize=1, f_bsize=1,
                                     f_blocks=1 << 40, f_bfree=1 << 40)

    with _dir_fd(run) as fd:
        _install(monkeypatch, statvfs=statvfs)
        durable_write(Path("w.json"), b"x", dir_fd=fd)
        durable_create(Path("c.json"), b"x", dir_fd=fd)
        durable_append(Path("a.jsonl"), b"x\n", dir_fd=fd)
        require_room(fd, 1)
        assert asked == [fd, fd, fd, fd]


def test_require_room_names_the_descriptor_in_its_refusal(tmp_path, monkeypatch):
    """Catches (X5): a refusal for a descriptor that names the bare directory (or
    the integer) instead of `/proc/self/fd/<n>`, which says which directory it
    was. Control: a path still names the path."""
    run = _dir(tmp_path)
    with _dir_fd(run) as fd:
        _install(monkeypatch, statvfs=_statvfs_reporting(100))
        with pytest.raises(DiskFullError) as caught:
            require_room(fd, 1)
        assert caught.value.path == Path(f"/proc/self/fd/{fd}")
        with pytest.raises(DiskFullError) as by_path:
            require_room(run, 1)
        assert by_path.value.path == run


def test_dir_fd_primitives_sync_the_descriptors_directory(tmp_path, monkeypatch):
    """Catches (X2): a descriptor form that never fsyncs the directory. After
    each call the descriptor itself is among the descriptors fsynced."""
    run = _dir(tmp_path)
    synced: list[int] = []
    real = os.fsync

    def fsync(fd_):
        synced.append(fd_)
        return real(fd_)

    with _dir_fd(run) as fd:
        _install(monkeypatch, fsync=fsync)
        for call in (
            lambda: durable_write(Path("w.json"), b"x", dir_fd=fd, min_free_bytes=0),
            lambda: durable_create(Path("c.json"), b"x", dir_fd=fd, min_free_bytes=0),
            lambda: durable_append(Path("a.jsonl"), b"x\n", dir_fd=fd, min_free_bytes=0),
        ):
            synced.clear()
            call()
            assert fd in synced
            assert len(synced) >= 2          # the file, then the directory


def test_dir_fd_primitives_report_a_failed_directory_sync_as_not_synced(tmp_path, monkeypatch):
    """Catches (X2, and the primitive half of X4): a failed fsync of the
    descriptor's directory that is lost or turned into another error. The new
    bytes are in place; only the rename's durability is uncertain."""
    run = _dir(tmp_path)
    real = os.fsync

    def fsync(fd_):
        if _is_dir(fd_):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(fd_)

    with _dir_fd(run) as fd:
        _install(monkeypatch, fsync=fsync)
        with pytest.raises(DurableWriteNotSynced):
            durable_write(Path("w.json"), b"w", dir_fd=fd, min_free_bytes=0)
        with pytest.raises(DurableWriteNotSynced):
            durable_create(Path("c.json"), b"c", dir_fd=fd, min_free_bytes=0)
        with pytest.raises(DurableWriteNotSynced):
            durable_append(Path("a.jsonl"), b"a\n", dir_fd=fd, min_free_bytes=0)
    assert (run / "w.json").read_bytes() == b"w"
    assert (run / "c.json").read_bytes() == b"c"
    assert (run / "a.jsonl").read_bytes() == b"a\n"


def test_dir_fd_create_never_replaces_and_append_refuses_a_second_name(tmp_path):
    """Catches: a descriptor form of `durable_create` that replaces, and of
    `durable_append(single_link=True)` that appends to a file with a second name
    (which may lie outside the run). Control: the same names, free, are used."""
    run = _dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    (run / "c.json").write_bytes(b"first")
    os.link(outside, run / "log.jsonl")
    with _dir_fd(run) as fd:
        with pytest.raises(FileExistsError):
            durable_create(Path("c.json"), b"second", dir_fd=fd, min_free_bytes=0)
        with pytest.raises(UnsafeRecordFile):
            durable_append(Path("log.jsonl"), b"line\n", single_link=True, dir_fd=fd,
                           min_free_bytes=0)
        durable_create(Path("d.json"), b"fresh", dir_fd=fd, min_free_bytes=0)
    assert (run / "c.json").read_bytes() == b"first"
    assert outside.read_bytes() == b"outside\n"
    assert (run / "d.json").read_bytes() == b"fresh"
    assert _names(run) == ["c.json", "d.json", "log.jsonl"]


def test_before_replace_sees_a_complete_synced_temp_and_its_failure_abandons_it(
        tmp_path, monkeypatch):
    """Catches (O5): a hook that runs before the temp file is written or fsynced
    (the dispatcher would park the old file for bytes that are not safe); a hook
    failure that leaves the temp file or changes the target; a hook that wraps
    its exception; a hook called although the space check refused."""
    run = _dir(tmp_path)
    (run / "t.json").write_bytes(b"old")
    fsyncs = {"n": 0}
    real = os.fsync

    def fsync(fd_):
        fsyncs["n"] += 1
        return real(fd_)

    seen: dict = {}

    def hook():
        temps = [n for n in os.listdir(run) if ".tmp." in n]
        seen["temps"] = temps
        seen["bytes"] = (run / temps[0]).read_bytes() if temps else None
        seen["fsyncs"] = fsyncs["n"]

    with _dir_fd(run) as fd:
        _install(monkeypatch, fsync=fsync)
        durable_write(Path("t.json"), b"new payload", dir_fd=fd, min_free_bytes=0,
                      before_replace=hook)
        assert len(seen["temps"]) == 1
        assert seen["bytes"] == b"new payload"
        assert seen["fsyncs"] >= 1
        assert (run / "t.json").read_bytes() == b"new payload"

        boom = RuntimeError("the hook failed")

        def failing():
            raise boom

        (run / "t.json").write_bytes(b"old")
        with pytest.raises(RuntimeError) as caught:
            durable_write(Path("t.json"), b"newer", dir_fd=fd, min_free_bytes=0,
                          before_replace=failing)
        assert caught.value is boom
        assert (run / "t.json").read_bytes() == b"old"
        assert _names(run) == ["t.json"]

        # The space check refuses first: the hook is never called.
        called = []
        _install(monkeypatch, statvfs=_statvfs_reporting(100))
        with pytest.raises(DiskFullError):
            durable_write(Path("t.json"), b"newer", dir_fd=fd,
                          before_replace=lambda: called.append(1))
        assert called == []


# ==== the leaf write ==================================================================

def test_leaf_write_full_disk_changes_nothing_and_a_retry_works(tmp_path, monkeypatch):
    """Catches: a leaf write that parks the old file before the new bytes are
    safe (O1), leaves its temp file on a full disk, or loses the old answer. The
    directory is exactly as it was; after `undo` the retry versions the old file.
    """
    answers = _dir(tmp_path, "answers")
    (answers / "answer.json").write_bytes(b"old")
    inode = os.stat(answers / "answer.json").st_ino
    with _dir_fd(answers) as fd:
        _install(monkeypatch, write=_err_write_for(".answer.json.tmp."))
        with pytest.raises(DispatchError):
            dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
        assert _names(answers) == ["answer.json"]
        assert os.stat(answers / "answer.json").st_ino == inode
        assert (answers / "answer.json").read_bytes() == b"old"
        monkeypatch.undo()
        version = dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
    assert version is not None and version.startswith("answer.json.v")
    assert (answers / "answer.json").read_bytes() == b"new"
    assert os.stat(answers / "answer.json").st_nlink == 1
    assert (answers / version).read_bytes() == b"old"


def test_leaf_write_a_failed_park_leaves_the_current_file_and_no_temp(tmp_path, monkeypatch):
    """Catches: a failed version link that leaves the temp file in place (the old
    behaviour) or touches the current file. No copy fallback."""
    answers = _dir(tmp_path, "answers")
    (answers / "answer.json").write_bytes(b"old")

    def eperm(*_args, **_kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(dispatchers.os, "link", eperm)
    with _dir_fd(answers) as fd, pytest.raises(DispatchError, match="parking") as caught:
        dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
    assert "unchanged" in str(caught.value)
    assert _names(answers) == ["answer.json"]
    assert (answers / "answer.json").read_bytes() == b"old"


def test_leaf_write_refuses_for_lack_of_room_before_creating_anything(tmp_path, monkeypatch):
    """Catches (O1): a leaf write that tries and fails on a nearly full disk
    instead of being refused first by the primitives' pre-check, or that parks
    before the check."""
    answers = _dir(tmp_path, "answers")
    (answers / "answer.json").write_bytes(b"old")
    _install(monkeypatch, statvfs=_statvfs_reporting(100))
    with _dir_fd(answers) as fd, pytest.raises(DispatchError):
        dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
    assert _names(answers) == ["answer.json"]
    assert (answers / "answer.json").read_bytes() == b"old"


def test_leaf_write_zero_byte_write_fails_instead_of_spinning(tmp_path, monkeypatch):
    """Catches: the write loop `view[os.write(fd, view):]`, which never ends on a
    write that returns 0. The fake fails the test after 50 calls, so a regression
    fails instead of hanging."""
    answers = _dir(tmp_path, "answers")
    _install(monkeypatch, write=_zero_write_for(".answer.json.tmp."))
    with _dir_fd(answers) as fd, pytest.raises(DispatchError):
        dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
    assert _names(answers) == []


def test_leaf_write_keeps_the_versioning_contract(tmp_path):
    """Catches: a leaf write that writes the current file in place (a hard link
    shares the inode, so the parked version would change with it), loses the
    version name, or changes the mode. Pins what must not change."""
    answers = _dir(tmp_path, "answers")
    with _dir_fd(answers) as fd:
        assert dispatchers._write_leaf(fd, "p.md", b"first", str(answers)) is None
        first_inode = os.stat(answers / "p.md").st_ino
        version = dispatchers._write_leaf(fd, "p.md", b"second", str(answers))
    assert version is not None
    assert (answers / version).read_bytes() == b"first"
    assert os.stat(answers / version).st_ino == first_inode
    current = os.stat(answers / "p.md")
    assert (answers / "p.md").read_bytes() == b"second"
    assert current.st_ino != first_inode
    assert current.st_nlink == 1
    assert stat.S_IMODE(current.st_mode) == 0o600


def test_leaf_write_and_audit_append_turn_a_failed_directory_sync_into_a_dispatch_error(
        tmp_path, monkeypatch):
    """Catches (X4): a leaf write that does not map `DurableWriteNotSynced`, so
    the generic failure message ("... failed (...); ... is unchanged") would be
    shown for a file that was written. The message must say "was written, but
    its directory entry" and the file holds the new bytes."""
    answers = _dir(tmp_path, "answers")
    real = os.fsync

    def fsync(fd_):
        if _is_dir(fd_):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(fd_)

    _install(monkeypatch, fsync=fsync)
    with _dir_fd(answers) as fd:
        with pytest.raises(DispatchError, match="was written, but its directory entry"):
            dispatchers._write_leaf(fd, "answer.json", b"new", str(answers))
        assert (answers / "answer.json").read_bytes() == b"new"
        with pytest.raises(DispatchError):
            dispatchers._append_audit(fd, {"n": 1}, str(answers))


# ==== the audit append ================================================================

def _audit_lines(directory: Path) -> list[str]:
    return (directory / dispatchers.PROMPT_BINDINGS).read_text(encoding="utf-8").split("\n")


def test_audit_append_full_disk_then_a_good_append_keeps_every_line_readable(
        tmp_path, monkeypatch):
    """Catches: an audit append without a torn-tail guard, whose fragment is
    glued to the next record (the first record after a short write would not
    parse), and a write loop that retries a short write into a torn line."""
    run = _dir(tmp_path)
    with _dir_fd(run) as fd:
        dispatchers._append_audit(fd, {"n": 1}, str(run))
        _install(monkeypatch, write=_short_write_for(dispatchers.PROMPT_BINDINGS, 5))
        with pytest.raises(DispatchError):
            dispatchers._append_audit(fd, {"n": 2}, str(run))
        monkeypatch.undo()
        dispatchers._append_audit(fd, {"n": 3}, str(run))
    lines = [ln for ln in _audit_lines(run) if ln]
    assert json.loads(lines[0]) == {"n": 1}
    assert json.loads(lines[-1]) == {"n": 3}
    assert len(lines) == 3          # the fragment is on a line of its own


def test_audit_append_refuses_for_lack_of_room_before_opening(tmp_path, monkeypatch):
    """Catches: an append that creates the audit file before the space check, or
    that tries and fails on a nearly full disk."""
    run = _dir(tmp_path)
    _install(monkeypatch, statvfs=_statvfs_reporting(100))
    with _dir_fd(run) as fd, pytest.raises(DispatchError):
        dispatchers._append_audit(fd, {"n": 1}, str(run))
    assert _names(run) == []


def test_audit_append_creates_the_file_owner_only(tmp_path):
    """Catches (O7): an audit file created with the primitive's default 0o666.
    Under umask 0o022 that would be 0o644; the dispatcher always made it 0o600."""
    run = _dir(tmp_path)
    old = os.umask(0o022)
    try:
        with _dir_fd(run) as fd:
            dispatchers._append_audit(fd, {"n": 1}, str(run))
    finally:
        os.umask(old)
    assert stat.S_IMODE((run / dispatchers.PROMPT_BINDINGS).stat().st_mode) == 0o600


def test_audit_append_still_refuses_a_second_name_and_says_so(tmp_path):
    """Catches (O3): an append that no longer checks the link count, writing to a
    file that may lie outside the run. The refusal keeps its wording ("exactly one
    link"), which existing tests pin."""
    run = _dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    os.link(outside, run / dispatchers.PROMPT_BINDINGS)
    with _dir_fd(run) as fd, pytest.raises(DispatchError, match="exactly one link"):
        dispatchers._append_audit(fd, {"n": 1}, str(run))
    assert outside.read_bytes() == b"outside\n"


# ==== the recorded answer =============================================================

def _harness_dispatcher(tmp_path: Path, monkeypatch, stdout: str = "an answer"):
    answers = tmp_path / "answers"
    answers.mkdir()
    d = dispatchers.HarnessDispatcher(
        answers_dir=answers, profiles={Role.QA: "claude"}, cwd=tmp_path)
    monkeypatch.setattr(
        dispatchers.subprocess, "run",
        lambda argv, **kwargs: types.SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    state = types.SimpleNamespace(run_id="r1", iteration=1, attempt=1)
    return d, answers, state


def test_recorded_answer_full_disk_is_a_dispatch_error_and_keeps_the_earlier_answer(
        tmp_path, monkeypatch):
    """Catches (O4): an answer written by `write_text`, which truncates the
    earlier answer on a full disk and surfaces as a raw `OSError`. The error says
    the answer could not be recorded; the earlier one is intact and no temp file
    is left. Control: after `undo` the dispatch returns the answer and records it."""
    d, answers, state = _harness_dispatcher(tmp_path, monkeypatch)
    recorded = answers / "i1-a1-qa.json"
    recorded.write_text("the earlier answer", encoding="utf-8")
    _install(monkeypatch, write=_err_write_for(".i1-a1-qa.json.tmp."))
    with pytest.raises(DispatchError, match="could not be recorded") as caught:
        d.dispatch(Role.QA, "review", state=state)
    assert caught.value.transient is False
    assert recorded.read_text(encoding="utf-8") == "the earlier answer"
    assert _names(answers) == ["i1-a1-qa.json"]
    _uninstall(monkeypatch)
    assert d.dispatch(Role.QA, "review", state=state) == "an answer"
    assert recorded.read_text(encoding="utf-8") == "an answer"


def test_recorded_answer_follows_the_umask(tmp_path, monkeypatch):
    """Catches (X6): a recorded answer written with a fixed mode. `write_text`
    followed the umask, and so does `durable_write(mode=None)`: under umask 027
    the file is 0o640."""
    d, answers, state = _harness_dispatcher(tmp_path, monkeypatch)
    old = os.umask(0o027)
    try:
        d.dispatch(Role.QA, "review", state=state)
    finally:
        os.umask(old)
    assert stat.S_IMODE((answers / "i1-a1-qa.json").stat().st_mode) == 0o640
