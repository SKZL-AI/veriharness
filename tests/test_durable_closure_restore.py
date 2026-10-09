"""P2-41 closure S3: what `restore` may no longer do to a running run, and what
a merged restore keeps.

The follow-ups of the S1 integration reviews, in `RunStore.backup`,
`RunStore.restore`/`_restore` and `durable_io.durable_publish_tree`: the lock
files are neither backed up nor restored; an overwrite over a live run holds the
controller lock and the process lock; a merged restore keeps the empty
directories of the backup and the modes of the directories it creates (applied
after the last replace); a directory that would refuse a write fails the whole
restore before the first replace; a member the owner cannot read back is a
`StoreError`; the staging directory's creation maps a full disk like the
extraction. Every test names what it catches. Fault cases come with a control
that changes only the condition under test.

Fault injection goes through a proxy installed as `durable_io.os` (the seam of
`tests/test_durable_closure.py`, whose small helpers are copied here: test
modules do not import each other). No subprocess is started, nothing writes
outside `tmp_path`, locks are taken with `flock` on descriptors that are closed
before the test ends, and a test that changes a directory's mode restores it in
a `finally`.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from hoh import cli, durable_io
from hoh.contracts import RunState
from hoh.durable_io import DiskFullError, durable_publish_tree
from hoh.store import LockBusy, RunStore, StoreError

needs_unprivileged_user = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads and writes through any mode")


# --- the seam and the helpers ---------------------------------------------------

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


#: The two lock files at the top of a run directory, or of the root that holds
#: the run `run1`.
_LOCK_FILES = (".lock", ".process.lock", "run1/.lock", "run1/.process.lock")


def _tree(root: Path) -> dict[str, str]:
    """Every path under `root` to a sha256 of its bytes, `''` for a directory,
    `->target` for a link. Links are never followed. The two lock files at the
    top of the run (`root` is the run directory, or the root that holds `run1`)
    are left out: an overwrite restore claims, and so creates, them."""
    root = Path(root)
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel in _LOCK_FILES:
                continue
            mode = os.lstat(p).st_mode
            if stat.S_ISLNK(mode):
                out[rel] = "->" + os.readlink(p)
            elif stat.S_ISDIR(mode):
                out[rel] = ""
            else:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _run_state(**kw) -> RunState:
    base = {"run_id": "run1", "repo_path": "/tmp/repo", "project_name": "demo",
            "spec_path": "/tmp/spec.md", "spec_digest": "s", "policy_digest": "p",
            "profile_digest": "h"}
    return RunState(**{**base, **kw})


def _make_run(tmp_path: Path, root: str = "runs") -> RunStore:
    """A run with a state, a receipt and a 5000-byte log."""
    store = RunStore(tmp_path / root, "run1")
    store.ensure()
    store.write_state(_run_state())
    (store.receipts_dir / "r1.json").write_text('{"n": 1}')
    (store.logs_dir / "big.txt").write_text("x" * 5000)
    return store


def _archive_with(src: Path, dst: Path, extra: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    """A copy of the backup `src` with more members."""
    with tarfile.open(src, "r:gz") as old, tarfile.open(dst, "w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member) if member.isreg() else None
            new.addfile(member, data)
        for info, payload in extra:
            new.addfile(info, io.BytesIO(payload) if info.isreg() else None)
    return dst


def _file_member(name: str, payload: bytes, mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    return info, payload


def _dir_member(name: str, mode: int) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    return info, b""


def _open_up(root: Path) -> None:
    """Give every directory under `root` back owner rwx so it can be cleaned."""
    for dirpath, _dirnames, _filenames in os.walk(root):
        try:
            os.chmod(dirpath, 0o700)
        except OSError:
            pass
    for dirpath, _dirnames, _filenames in os.walk(root):
        try:
            os.chmod(dirpath, 0o700)
        except OSError:
            pass


@pytest.fixture
def store(tmp_path) -> RunStore:
    return _make_run(tmp_path)


@pytest.fixture
def held_process_lock(store):
    """`<run>/.process.lock` held exclusively for the length of the test."""
    fd = os.open(store.dir / ".process.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def _lock_is_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


# ==== (a) the lock files ==========================================================

def test_backup_does_not_hold_the_process_lock_or_the_controller_lock(tmp_path, store):
    """Catches: a backup that carries `.process.lock` (or `.lock`), so that an
    overwrite restore of it replaces the live run's lock file."""
    with store.process_marker():
        pass
    with store.lock():
        pass
    assert (store.dir / ".process.lock").exists() and (store.dir / ".lock").exists()
    archive = store.backup(tmp_path / "backups")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "run1/state.json" in names
    assert "run1/.process.lock" not in names
    assert "run1/.lock" not in names


def test_overwrite_restore_refuses_a_run_whose_process_lock_is_held(
        tmp_path, store, held_process_lock):
    """Catches: an overwrite restore that replaces the files of a run that a
    `hoh run` is executing. Nothing is extracted: no staging directory."""
    archive = store.backup(tmp_path / "backups")
    (store.receipts_dir / "r2.json").write_text('{"n": 2}')
    before = _tree(store.dir)
    with pytest.raises(LockBusy, match="being executed"):
        store.restore(archive, overwrite=True)
    assert _tree(store.dir) == before
    assert not (store.root / "_restore").exists()


def test_overwrite_restore_control_succeeds_when_no_lock_is_held(tmp_path, store):
    """Control of the two refusals: the same restore, no lock held, goes
    through, and what the backup does not hold stays."""
    archive = store.backup(tmp_path / "backups")
    (store.receipts_dir / "r2.json").write_text('{"n": 2}')
    assert store.restore(archive, overwrite=True) == store.dir
    assert (store.receipts_dir / "r2.json").read_text() == '{"n": 2}'
    assert (store.receipts_dir / "r1.json").read_text() == '{"n": 1}'


def test_overwrite_restore_refuses_a_run_whose_controller_lock_is_held(tmp_path, store):
    """Catches: an overwrite restore that ignores the controller lock, so that a
    controller iterating the run has its files replaced under it."""
    archive = store.backup(tmp_path / "backups")
    before = _tree(store.dir)
    with store.lock(), pytest.raises(LockBusy, match="held by another controller"):
        store.restore(archive, overwrite=True)
    assert _tree(store.dir) == before
    assert not (store.root / "_restore").exists()


def test_overwrite_restore_holds_both_locks_while_it_publishes(tmp_path, store, monkeypatch):
    """Catches: a restore that claims the locks and lets go before it publishes
    (or claims only one of them). Seen from inside the publish: a second claim
    on `.process.lock` fails and so does the controller lock; both are free
    afterwards."""
    archive = store.backup(tmp_path / "backups")
    seen: dict[str, bool] = {}
    real = durable_io.durable_publish_tree

    def spying(staging, target, **kwargs):
        fd = os.open(store.dir / ".process.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["process_lock_free"] = True
        except OSError:
            seen["process_lock_free"] = False
        finally:
            os.close(fd)
        try:
            with store.lock():
                seen["controller_lock_free"] = True
        except LockBusy:
            seen["controller_lock_free"] = False
        return real(staging, target, **kwargs)

    monkeypatch.setattr(durable_io, "durable_publish_tree", spying)
    store.restore(archive, overwrite=True)
    assert seen == {"process_lock_free": False, "controller_lock_free": False}
    monkeypatch.undo()
    assert _lock_is_free(store.dir / ".process.lock")
    with store.lock():
        pass


def test_a_lock_file_in_an_older_backup_is_never_restored(tmp_path, store):
    """Catches: an older backup (made before the lock files were left out) whose
    `.process.lock` is laid over the live lock file, giving it a new inode while
    another process may hold the old one."""
    with store.process_marker():
        pass
    with store.lock():
        pass
    live = {name: os.stat(store.dir / name).st_ino for name in (".process.lock", ".lock")}
    older = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "older.tar.gz",
        [_file_member("run1/.process.lock", b"from the backup"),
         _file_member("run1/.lock", b"from the backup")])
    beside = store.restore(older)
    assert not (beside / ".process.lock").exists()
    assert not (beside / ".lock").exists()
    store.restore(older, overwrite=True)
    for name, inode in live.items():
        assert os.stat(store.dir / name).st_ino == inode
        assert b"from the backup" not in (store.dir / name).read_bytes()


def test_a_member_below_a_lock_file_name_is_skipped_with_the_lock_file(tmp_path, store):
    """Catches: a filter by exact name. A member `.process.lock/x` would turn the
    lock file into a directory and break the next `hoh run`."""
    with store.process_marker():
        pass
    older = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "older.tar.gz",
        [_file_member("run1/.process.lock/x", b"x"), _file_member("run1/.lock/y", b"y")])
    beside = store.restore(older)
    assert not (beside / ".process.lock").exists()
    assert not (beside / ".lock").exists()
    store.restore(older, overwrite=True)
    assert (store.dir / ".process.lock").is_file()
    assert not (store.dir / ".lock").is_dir()
    with store.process_marker():
        pass


def test_a_lock_file_name_below_a_directory_is_an_ordinary_member(tmp_path, store):
    """Catches: a filter at any depth. `receipts/.lock` is a receipt, not the
    run's lock file; it is restored."""
    archive = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "with.tar.gz",
        [_file_member("run1/receipts/.lock", b"a receipt")])
    store.restore(archive, overwrite=True)
    assert (store.receipts_dir / ".lock").read_text() == "a receipt"


@pytest.mark.parametrize("state", ["absent", "empty"])
def test_overwrite_restore_of_a_run_that_is_not_there_takes_no_locks(tmp_path, state):
    """Catches: a restore that claims the locks whenever `overwrite` is set, and
    so creates lock files in a run that does not exist yet (or is empty)."""
    source = _make_run(tmp_path, "source")
    archive = source.backup(tmp_path / "backups")
    target = RunStore(tmp_path / "runs", "run1")
    if state == "empty":
        target.dir.mkdir(parents=True)
    assert target.restore(archive, overwrite=True) == target.dir
    assert (target.dir / "state.json").is_file()
    assert not (target.dir / ".lock").exists()
    assert not (target.dir / ".process.lock").exists()


def test_overwrite_restore_refusal_is_a_message_and_exit_2_on_the_command_line(
        tmp_path, store, held_process_lock, capsys):
    """Catches: a `LockBusy` that surfaces as a traceback from `hoh restore`."""
    archive = store.backup(tmp_path / "backups")
    rc = cli.main(["--root", str(store.root), "restore", "run1", "--archive", str(archive),
                   "--overwrite"])
    assert rc == 2
    assert "being executed" in capsys.readouterr().err


# ==== (d) the staging directory ===================================================

def test_restore_staging_directory_full_disk_is_disk_full_and_the_run_untouched(
        tmp_path, store, monkeypatch):
    """Catches: a full disk at the creation of the staging directory that
    surfaces as a bare `OSError`, without saying that the run is untouched."""
    archive = store.backup(tmp_path / "backups")
    before = _tree(store.root)
    real = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self.parent.name == "_restore":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(DiskFullError, match="untouched"):
        store.restore(archive, overwrite=True)
    monkeypatch.undo()
    assert _tree(store.root) == before


# ==== (b) the directories of a merged restore =====================================

def test_merged_restore_creates_directories_with_the_backups_modes_and_keeps_empty_ones(
        tmp_path, store):
    """Catches: a merge that creates directories with the default mode and skips
    the empty ones. An existing directory keeps its own mode."""
    os.chmod(store.receipts_dir, 0o711)
    archive = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "dirs.tar.gz",
        [_dir_member("run1/inbox", 0o700), _dir_member("run1/private", 0o750),
         _file_member("run1/private/f.txt", b"inside")])
    store.restore(archive, overwrite=True)
    assert (store.dir / "inbox").is_dir()
    assert list((store.dir / "inbox").iterdir()) == []
    assert stat.S_IMODE((store.dir / "inbox").stat().st_mode) == 0o700
    assert stat.S_IMODE((store.dir / "private").stat().st_mode) == 0o750
    assert (store.dir / "private" / "f.txt").read_text() == "inside"
    assert stat.S_IMODE(store.receipts_dir.stat().st_mode) == 0o711


def test_merged_restore_syncs_the_directories_it_created(tmp_path, store, monkeypatch):
    """Catches: a merge that does not fsync the directories it created. An empty
    directory is no file's parent, so only the bookkeeping of the created ones
    reaches it. Compared by the live path: the staged copy of `inbox` is synced
    too."""
    archive = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "inbox.tar.gz",
        [_dir_member("run1/inbox", 0o700)])
    synced: list[str] = []
    real = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    _install(monkeypatch, fsync=fsync)
    store.restore(archive, overwrite=True)
    assert os.path.realpath(store.dir / "inbox") in synced


@pytest.mark.parametrize("kind", ["file", "link"])
def test_merge_refuses_a_staged_directory_over_a_file_before_it_replaces_anything(
        tmp_path, kind):
    """Catches: a merge that finds out half way that a staged directory lies over
    a file or over a link, after `state.json` was replaced."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "state.json").write_text("old")
    if kind == "file":
        (live / "inbox").write_text("a file")
    else:
        (tmp_path / "elsewhere").mkdir()
        (live / "inbox").symlink_to(tmp_path / "elsewhere")
    stage = tmp_path / "stage"
    (stage / "inbox").mkdir(parents=True)
    (stage / "state.json").write_text("new")
    with pytest.raises(NotADirectoryError):
        durable_publish_tree(stage, live, merge=True, last=("state.json",))
    assert (live / "state.json").read_text() == "old"


@needs_unprivileged_user
def test_merge_into_a_new_read_only_directory_publishes_everything_or_nothing(tmp_path):
    """Catches (review p241s3spec-a, finding 2): a staged directory of mode 0o500
    that refuses to give up its file after `a.txt` and `state.json` were
    replaced, or a created directory given its mode before its files are in."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_text("old")
    (live / "state.json").write_text("old")
    stage = tmp_path / "stage"
    (stage / "ro").mkdir(parents=True)
    (stage / "a.txt").write_text("new")
    (stage / "state.json").write_text("new")
    (stage / "ro" / "f.txt").write_text("new")
    os.chmod(stage / "ro", 0o500)
    try:
        durable_publish_tree(stage, live, merge=True, last=("state.json",))
        assert (live / "a.txt").read_text() == "new"
        assert (live / "state.json").read_text() == "new"
        assert (live / "ro" / "f.txt").read_text() == "new"
        assert stat.S_IMODE((live / "ro").stat().st_mode) == 0o500
    finally:
        _open_up(tmp_path)


@needs_unprivileged_user
def test_restore_with_a_read_only_directory_member_publishes_everything(tmp_path, store):
    """Catches (review p241s3spec-a, finding 2, through `restore`): a backup with a
    directory member of mode 0o500 whose restore replaced some files and then
    failed. Everything is published and the directory ends 0o500."""
    (store.dir / "a.txt").write_text("backed up")
    backed_up_state = (store.dir / "state.json").read_bytes()
    archive = _archive_with(
        store.backup(tmp_path / "backups"), tmp_path / "ro.tar.gz",
        [_dir_member("run1/ro", 0o500), _file_member("run1/ro/f.txt", b"inside")])
    (store.dir / "a.txt").write_text("newer")
    moved = store.read_state()
    moved.note("moved on")
    store.write_state(moved)
    try:
        store.restore(archive, overwrite=True)
        assert (store.dir / "a.txt").read_text() == "backed up"
        assert (store.dir / "ro" / "f.txt").read_text() == "inside"
        assert (store.dir / "state.json").read_bytes() == backed_up_state
        assert stat.S_IMODE((store.dir / "ro").stat().st_mode) == 0o500
    finally:
        _open_up(tmp_path)


@needs_unprivileged_user
def test_merge_refuses_a_directory_it_cannot_write_into_before_it_replaces_anything(
        tmp_path, store):
    """Catches: a merge that meets a live directory of mode 0o500 only when it
    reaches it, after `logs/big.txt` (which sorts before `receipts/`) was
    replaced."""
    archive = store.backup(tmp_path / "backups")
    (store.logs_dir / "big.txt").write_text("changed")
    os.chmod(store.receipts_dir, 0o500)
    try:
        with pytest.raises(PermissionError):
            store.restore(archive, overwrite=True)
        assert (store.logs_dir / "big.txt").read_text() == "changed"
    finally:
        _open_up(tmp_path)


# ==== (c) members the owner cannot read back =======================================

@needs_unprivileged_user
@pytest.mark.parametrize("member", [
    pytest.param(_file_member("run1/secret.txt", b"x", mode=0), id="mode-0 file"),
    pytest.param(_file_member("run1/secret.txt", b"x", mode=0o200), id="mode-0o200 file"),
    pytest.param(_dir_member("run1/closed", 0o300), id="directory 0o300"),
    pytest.param(_dir_member("run1/closed", 0o400), id="directory 0o400"),
])
def test_restore_refuses_a_member_the_owner_cannot_read_back_as_a_store_error(
        tmp_path, store, member):
    """Catches: a restore that extracts a member nothing can read back and ends
    in a raw `PermissionError` (or publishes half of it). Refused by name,
    before anything is created. A directory needs read and search (0o500); the
    0o400 one kills a mask of 0o400. Control: a mode-0o400 file restores."""
    base = store.backup(tmp_path / "backups")
    archive = _archive_with(base, tmp_path / "bad.tar.gz", [member])
    before = _tree(store.root)
    with pytest.raises(StoreError, match="owner cannot read"):
        store.restore(archive, overwrite=True)
    assert _tree(store.root) == before
    assert not (store.root / "_restore").exists()

    control = _archive_with(base, tmp_path / "good.tar.gz",
                            [_file_member("run1/secret.txt", b"x", mode=0o400)])
    store.restore(control, overwrite=True)
    assert (store.dir / "secret.txt").read_bytes() == b"x"


# ==== P2-41 closure S2: the lock-file filter, normalised ===========================

def _plant_locks(store: RunStore) -> dict[str, int]:
    """Both lock files exist; their inodes."""
    with store.process_marker():
        pass
    with store.lock():
        pass
    return {name: os.stat(store.dir / name).st_ino for name in (".lock", ".process.lock")}


def _locks_untouched(store: RunStore, inodes: dict[str, int], planted: bytes) -> None:
    for name, inode in inodes.items():
        assert os.stat(store.dir / name).st_ino == inode, name
        assert planted not in (store.dir / name).read_bytes(), name


@pytest.mark.parametrize("name", ["run1/x/../.lock", "run1/receipts/../.process.lock"])
def test_a_member_that_reaches_a_lock_file_through_dotdot_is_refused(tmp_path, store, name):
    """Catches (p241s3int-a/-b, finding 1): a lock-file skip that looks at the
    first component of the name as written, so `x/../.lock` was not skipped and
    an overwrite restore replaced the live lock file with a new inode. Refused
    by name before anything is created."""
    inodes = _plant_locks(store)
    archive = _archive_with(store.backup(tmp_path / "backups"), tmp_path / "bad.tar.gz",
                            [_file_member(name, b"planted")])
    before = _tree(store.root)
    with pytest.raises(StoreError, match="'..' component"):
        store.restore(archive, overwrite=True)
    assert _tree(store.root) == before
    assert not (store.root / "_restore").exists()
    _locks_untouched(store, inodes, b"planted")


@pytest.mark.parametrize("kind", ["symbolic", "hard"])
def test_a_link_member_is_refused_whatever_it_points_at(tmp_path, store, kind):
    """Catches (p241s3int-a/-b, finding 1): a link member `lk` -> `.` that lets
    `lk/.process.lock` reach the live lock file, and a hard-link member, which
    crashed `restore` with a raw `KeyError` (its `linkname` still carries the run
    id). Both are a `StoreError`, before anything is created."""
    inodes = _plant_locks(store)
    link = tarfile.TarInfo("run1/lk")
    if kind == "symbolic":
        link.type = tarfile.SYMTYPE
        link.linkname = "."
    else:
        link.type = tarfile.LNKTYPE
        link.linkname = "run1/state.json"
    archive = _archive_with(store.backup(tmp_path / "backups"), tmp_path / "bad.tar.gz",
                            [(link, b""), _file_member("run1/lk/.process.lock", b"planted")])
    before = _tree(store.root)
    with pytest.raises(StoreError, match="is a link"):
        store.restore(archive, overwrite=True)
    assert _tree(store.root) == before
    assert not (store.root / "_restore").exists()
    _locks_untouched(store, inodes, b"planted")


def test_a_staged_tree_that_holds_a_lock_file_is_refused_before_it_is_published(
        tmp_path, store, monkeypatch):
    """Catches: the last line of defence missing. Whatever a member name did, if
    the extraction left a lock file in the staging tree, publishing it would give
    the live lock file a new inode. The extraction is wrapped to leave one."""
    inodes = _plant_locks(store)
    archive = store.backup(tmp_path / "backups")
    real = tarfile.TarFile.extractall

    def extractall(self, path=".", *args, **kwargs):
        real(self, path, *args, **kwargs)
        (Path(path) / ".process.lock").write_text("planted")

    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)
    with pytest.raises(StoreError, match="staged tree holds"):
        store.restore(archive, overwrite=True)
    _locks_untouched(store, inodes, b"planted")


@needs_unprivileged_user
def test_merge_refuses_a_live_directory_without_search_permission_before_replacing_anything(
        tmp_path, store):
    """Catches (p241s3int-a, finding 4): a merge that meets a live directory it
    cannot search only when it reaches it. `logs/` sorts before `zz-closed/`, so
    a restore that got there late would already have replaced `logs/big.txt`.
    (A dropped `os.X_OK` in the writability check is equivalent here: the
    destination check stats inside the directory first and raises the same
    `PermissionError`.)"""
    archive = store.backup(tmp_path / "backups")
    closed = store.dir / "zz-closed"
    closed.mkdir()
    os.chmod(closed, 0o600)
    try:
        archive = _archive_with(archive, tmp_path / "closed.tar.gz",
                                [_file_member("run1/zz-closed/f.txt", b"inside")])
        (store.logs_dir / "big.txt").write_text("changed")
        with pytest.raises(PermissionError):
            store.restore(archive, overwrite=True)
        assert (store.logs_dir / "big.txt").read_text() == "changed"
    finally:
        _open_up(tmp_path)
