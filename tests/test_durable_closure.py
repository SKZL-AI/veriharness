"""P2-41 closure S1: the amendment, the role approval files, the governance
records and the restore go through the durable-write primitives.

`durable_io` gains `require_room`, `fsync_tree` and `durable_publish_tree`;
`amendment.park_and_amend`, `RoleApprovalPolicy.write_role_files`,
`Governor.decide`, `governance.bind`, `record_override` and `RunStore.restore`
are routed through them. Every test names what it catches. Fault cases come
with a control that changes only the condition under test.

Fault injection goes through a proxy installed as `durable_io.os` (the seam of
`tests/test_durable_writers.py`, whose small helpers are copied here: test
modules do not import each other). A fault is aimed by the file behind the
descriptor (`/proc/self/fd/<fd>`), so it hits exactly one write of a
multi-file operation. No disk is filled, no subprocess is started, nothing
writes outside `tmp_path`.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import signal
import stat
import tarfile
import types
from dataclasses import replace
from pathlib import Path

import pytest

from hoh import amendment, durable_io
from hoh import store as hoh_store
from hoh.amendment import AmendmentKind, park_and_amend
from hoh.approval_policy import RoleApprovalPolicy
from hoh.autonomy import AutonomyMode, Horizon, compile_profile
from hoh.contracts import Budgets, RunState
from hoh.durable_io import (
    DiskFullError,
    DurableWriteNotSynced,
    durable_publish_tree,
    fsync_tree,
    require_room,
)
from hoh.governance import ContractRefused, bind, pinned_path, record_override
from hoh.store import RunStore, StoreError, list_runs

needs_unprivileged_user = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a mode-0 directory")


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


def _statvfs_reporting(free_bytes: int):
    def fake(_path):
        return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1, f_bsize=1,
                                     f_blocks=free_bytes, f_bfree=free_bytes)
    return fake


def _tree(root: Path) -> dict[str, str]:
    """Every path under `root` to a sha256 of its bytes, `''` for a directory,
    `->target` for a link, `fifo` for a FIFO. Links are never followed. The two
    lock files at the top of `root` are left out: an overwrite restore claims,
    and so creates, them."""
    root = Path(root)
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel in (".lock", ".process.lock"):
                continue
            mode = os.lstat(p).st_mode
            if stat.S_ISLNK(mode):
                out[rel] = "->" + os.readlink(p)
            elif stat.S_ISDIR(mode):
                out[rel] = ""
            elif stat.S_ISREG(mode):
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                out[rel] = "fifo"
    return out


def _litter(directory: Path) -> list[str]:
    """Temp files a durable call would leave: `.<name>.tmp.<pid>.<random>`."""
    return sorted(p.name for p in Path(directory).iterdir() if ".tmp." in p.name)


def _stage(base: Path, name: str = "stage") -> Path:
    """A finished staging tree: `state.json` and `receipts/r1.json`."""
    stage = base / name
    (stage / "receipts").mkdir(parents=True)
    (stage / "state.json").write_text('{"v": "new"}')
    (stage / "receipts" / "r1.json").write_text('{"r": "new"}')
    return stage


def _live(base: Path, name: str = "live") -> Path:
    """A live tree with an older `state.json`, `receipts/r1.json` and a file the
    staged tree does not hold."""
    live = base / name
    (live / "receipts").mkdir(parents=True)
    (live / "attic").mkdir()
    (live / "state.json").write_text('{"v": "old"}')
    (live / "receipts" / "r1.json").write_text('{"r": "old"}')
    (live / "attic" / "kept.json").write_text('{"kept": true}')
    return live


class _umask:
    def __init__(self, mask: int) -> None:
        self.mask = mask

    def __enter__(self):
        self.old = os.umask(self.mask)

    def __exit__(self, *_exc):
        os.umask(self.old)


# ==== P: the three primitives =================================================

def test_require_room_refuses_before_anything_is_created(tmp_path, monkeypatch):
    """Catches: a room check that creates something, that compares without the
    reserve, or that reports the wrong free figure. Control: a smaller reserve
    lets the same call through."""
    _install(monkeypatch, statvfs=_statvfs_reporting(1000))
    with pytest.raises(DiskFullError) as caught:
        require_room(tmp_path, 10, min_free_bytes=2000)
    assert caught.value.errno == errno.ENOSPC
    assert caught.value.free_bytes == 1000
    assert list(tmp_path.iterdir()) == []
    assert require_room(tmp_path, 10, min_free_bytes=900) is None


def test_publish_tree_renames_whole_into_an_absent_or_empty_target(tmp_path):
    """Catches: a publish that copies instead of renaming, leaves the staging
    behind, or refuses an empty target directory."""
    stage = _stage(tmp_path, "stage1")
    expected = _tree(stage)
    target = tmp_path / "t1"
    durable_publish_tree(stage, target)
    assert _tree(target) == expected
    assert not stage.exists()

    stage2 = _stage(tmp_path, "stage2")
    empty = tmp_path / "t2"
    empty.mkdir()
    durable_publish_tree(stage2, empty)
    assert _tree(empty) == expected
    assert not stage2.exists()


def test_publish_tree_refuses_a_non_empty_target_without_merge(tmp_path):
    """Catches: a publish that overlays a live tree when `merge` was not asked
    for. Nothing is touched, the staging stays."""
    stage = _stage(tmp_path)
    live = _live(tmp_path)
    live_before, stage_before = _tree(live), _tree(stage)
    with pytest.raises(FileExistsError):
        durable_publish_tree(stage, live)
    assert _tree(live) == live_before
    assert _tree(stage) == stage_before


def test_publish_tree_merge_replaces_file_by_file_state_last(tmp_path, monkeypatch):
    """Catches: a merge that replaces in sorted order (`zz.json` sorts after
    `state.json`), ignores `last`, touches a file the tree does not hold, or
    leaves staged files behind."""
    stage = _stage(tmp_path)
    (stage / "zz.json").write_text('{"zz": "new"}')
    live = _live(tmp_path)
    kept_before = (live / "attic" / "kept.json").read_bytes()
    old_inode = (live / "state.json").stat().st_ino
    replaced: list[str] = []
    real = os.replace

    def replace(src, dst):
        replaced.append(Path(dst).name)
        return real(src, dst)
    _install(monkeypatch, replace=replace)
    durable_publish_tree(stage, live, merge=True, last=("state.json",))
    assert replaced == ["r1.json", "zz.json", "state.json"]
    assert (live / "attic" / "kept.json").read_bytes() == kept_before
    assert (live / "state.json").read_text() == '{"v": "new"}'
    assert (live / "state.json").stat().st_ino != old_inode
    assert (live / "receipts" / "r1.json").read_text() == '{"r": "new"}'
    assert [p for p in stage.rglob("*") if p.is_file() or p.is_symlink()] == []


def test_publish_tree_merge_checks_every_destination_before_the_first_replace(tmp_path):
    """Catches: a merge that replaces some files and then meets a link or a
    directory in the way. Both refusals leave the live tree exactly as it was."""
    stage = _stage(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    (live / "state.json").write_text('{"v": "old"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, live / "receipts")
    live_before, stage_before = _tree(live), _tree(stage)
    with pytest.raises(NotADirectoryError):
        durable_publish_tree(stage, live, merge=True, last=("state.json",))
    assert _tree(live) == live_before
    assert list(outside.iterdir()) == []
    assert _tree(stage) == stage_before

    # A directory where a file is to go.
    (live / "receipts").unlink()
    (live / "receipts" / "r1.json").mkdir(parents=True)
    live_before = _tree(live)
    with pytest.raises(IsADirectoryError):
        durable_publish_tree(stage, live, merge=True, last=("state.json",))
    assert (live / "state.json").read_text() == '{"v": "old"}'
    assert _tree(live) == live_before
    assert _tree(stage) == stage_before


def test_publish_tree_syncs_every_file_before_the_rename(tmp_path, monkeypatch):
    """Catches: a publish that renames before the staged bytes are durable.
    Exactly the two files are fsynced first, a directory before the rename and
    the parent after it."""
    stage = _stage(tmp_path)
    events: list[str] = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        events.append("dir" if _is_dir(fd) else "file")
        return real_fsync(fd)

    def rename(src, dst):
        events.append("rename")
        return real_rename(src, dst)
    _install(monkeypatch, fsync=fsync, rename=rename)
    durable_publish_tree(stage, tmp_path / "target")
    at = events.index("rename")
    assert events[:at].count("file") == 2
    assert "dir" in events[:at]
    assert "dir" in events[at + 1:]
    assert "file" not in events[at:]


def test_publish_tree_space_error_while_syncing_is_disk_full_and_nothing_is_published(
        tmp_path, monkeypatch):
    """Catches: an ENOSPC from fsync (delayed allocation) that escapes as a bare
    OSError, or that is followed by a publish anyway."""
    stage = _stage(tmp_path)
    before = _tree(stage)
    real = os.fsync

    def fsync(fd):
        if _is_reg(fd):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real(fd)
    _install(monkeypatch, fsync=fsync)
    target = tmp_path / "target"
    with pytest.raises(DiskFullError):
        durable_publish_tree(stage, target)
    assert not target.exists()
    assert _tree(stage) == before


def test_publish_tree_directory_sync_failure_is_not_synced_with_the_tree_in_place(
        tmp_path, monkeypatch):
    """Catches: a failed directory fsync after the rename reported as a space
    error or swallowed. The tree is at the target either way."""
    stage = _stage(tmp_path)
    expected = _tree(stage)
    state = {"renamed": False}
    real_fsync, real_rename = os.fsync, os.rename

    def rename(src, dst):
        real_rename(src, dst)
        state["renamed"] = True

    def fsync(fd):
        if state["renamed"] and _is_dir(fd):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)
    _install(monkeypatch, fsync=fsync, rename=rename)
    target = tmp_path / "target"
    with pytest.raises(DurableWriteNotSynced) as caught:
        durable_publish_tree(stage, target)
    assert caught.value.errno == errno.EIO
    assert _tree(target) == expected


def test_fsync_tree_skips_a_fifo_instead_of_blocking_on_it(tmp_path):
    """Catches: an fsync of every non-link entry, which blocks for ever on a
    FIFO that an archive may contain. A 5 s watchdog turns a hang into a
    failure."""
    stage = _stage(tmp_path)
    os.mkfifo(stage / "pipe")

    def alarm(_signum, _frame):
        raise AssertionError("fsync_tree blocked on a FIFO")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(5)
    try:
        durable_publish_tree(stage, tmp_path / "target")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert stat.S_ISFIFO(os.lstat(tmp_path / "target" / "pipe").st_mode)


@needs_unprivileged_user
def test_publish_tree_raises_on_a_directory_it_cannot_read(tmp_path):
    """Catches: the default `os.walk`, which skips a directory it cannot read,
    so a tree that cannot be read back completely is published smaller."""
    stage = _stage(tmp_path)
    (stage / "locked").mkdir()
    (stage / "locked" / "inside.json").write_text("{}")
    os.chmod(stage / "locked", 0)
    target = tmp_path / "target"
    try:
        with pytest.raises(PermissionError):
            durable_publish_tree(stage, target)
        assert not target.exists()
    finally:
        os.chmod(stage / "locked", 0o700)


def test_publish_tree_refuses_a_symlinked_staging_and_a_symlinked_target(tmp_path):
    """Catches: a staging path that is a link to a tree (renamed as a link), and
    a merge into a target that is a link (the replaces would land wherever the
    link points)."""
    real_stage = _stage(tmp_path, "real_stage")
    os.symlink(real_stage, tmp_path / "stage_link")
    target = tmp_path / "target"
    with pytest.raises(NotADirectoryError):
        durable_publish_tree(tmp_path / "stage_link", target)
    assert not os.path.lexists(target)

    live = _live(tmp_path, "real_live")
    os.symlink(live, tmp_path / "live_link")
    stage = _stage(tmp_path, "stage2")
    live_before, stage_before = _tree(live), _tree(stage)
    with pytest.raises(NotADirectoryError):
        durable_publish_tree(stage, tmp_path / "live_link", merge=True)
    assert _tree(live) == live_before
    assert _tree(stage) == stage_before


def test_publish_tree_merge_syncs_every_directory_it_touched_including_new_ones(
        tmp_path, monkeypatch):
    """Catches: a merge that fsyncs only each file's parent. A directory the
    merge created is an entry of its parent and must be durable there."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "state.json").write_text('{"v": "old"}')
    stage = tmp_path / "stage"
    (stage / "new" / "sub").mkdir(parents=True)
    (stage / "state.json").write_text('{"v": "new"}')
    (stage / "new" / "sub" / "x.json").write_text("{}")
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        if _is_dir(fd):
            events.append(("dir", _fd_name(fd)))
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", Path(dst).name))
        return real_replace(src, dst)
    _install(monkeypatch, fsync=fsync, replace=replace)
    durable_publish_tree(stage, live, merge=True, last=("state.json",))
    last_replace = max(i for i, (kind, _n) in enumerate(events) if kind == "replace")
    synced = {name for kind, name in events[last_replace + 1:] if kind == "dir"}
    assert {"live", "new", "sub"} <= synced
    assert (live / "new" / "sub" / "x.json").is_file()


def test_fsync_tree_syncs_directories_deepest_first_and_the_root_last(tmp_path, monkeypatch):
    """Catches: directories fsynced shallowest first, which makes an entry
    durable before the directory that holds it."""
    stage = tmp_path / "stage"
    (stage / "a" / "b" / "c").mkdir(parents=True)
    (stage / "a" / "b" / "c" / "f").write_text("x")
    order: list[str] = []
    real = os.fsync

    def fsync(fd):
        if _is_dir(fd):
            order.append(_fd_name(fd))
        return real(fd)
    _install(monkeypatch, fsync=fsync)
    fsync_tree(stage)
    assert order == ["c", "b", "a", "stage"]


def test_publish_tree_merge_moves_a_symlink_to_a_directory_as_a_name(tmp_path):
    """Catches: a merge whose walk drops a link to a directory (`os.walk` lists
    it with the directories and does not descend), or follows it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    (live / "state.json").write_text('{"v": "old"}')
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "state.json").write_text('{"v": "new"}')
    os.symlink(outside, stage / "link")
    durable_publish_tree(stage, live, merge=True, last=("state.json",))
    assert os.readlink(live / "link") == str(outside)
    assert list(outside.iterdir()) == []
    assert (live / "state.json").read_text() == '{"v": "new"}'


# ==== A: park_and_amend ========================================================

OLD_TEXT = "# Goal\n\nDo the thing.\n"
NEW_TEXT = "# Goal\n\nDo the other thing.\n"


def _amend(spec: Path):
    return park_and_amend(spec, NEW_TEXT, run_id="r", amendment_id="A-1",
                          kind=AmendmentKind.CLARIFY, actor="captain", reason="x")


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text(OLD_TEXT, encoding="utf-8")
    return spec


def test_amend_full_disk_while_parking_changes_nothing(tmp_path, monkeypatch):
    """Catches: an amendment that touches the specification before the parked
    copy is whole. Control: the same call succeeds once the fault is gone."""
    spec = _spec(tmp_path)
    _install(monkeypatch, write=_err_write_for(".spec.md.v"))
    with pytest.raises(DiskFullError):
        _amend(spec)
    assert sorted(os.listdir(tmp_path)) == ["spec.md"]
    assert spec.read_text() == OLD_TEXT

    monkeypatch.undo()
    record = _amend(spec)
    assert spec.read_text() == NEW_TEXT
    assert Path(record.from_path).read_text() == OLD_TEXT


def test_amend_full_disk_while_writing_the_new_text_keeps_the_old_one(tmp_path, monkeypatch):
    """Catches: a specification that is gone or half written when the new text
    does not fit, or a record returned for an amendment that did not happen. At
    most one parked copy is left, and it equals the old text."""
    spec = _spec(tmp_path)
    _install(monkeypatch, write=_err_write_for(".spec.md.tmp."))
    with pytest.raises(DiskFullError):
        _amend(spec)
    assert spec.read_text() == OLD_TEXT
    assert _litter(tmp_path) == []
    others = [p for p in tmp_path.iterdir() if p.name != "spec.md"]
    assert len(others) <= 1
    assert all(p.read_text() == OLD_TEXT for p in others)


def test_amend_short_write_of_the_new_text_is_not_published(tmp_path, monkeypatch):
    """Catches: a short write treated as a failure, or published as it is. A
    short write is progress; the loop finishes and the whole text is in place."""
    spec = _spec(tmp_path)
    _install(monkeypatch, write=_short_write_for(".spec.md.tmp.", 4))
    _amend(spec)
    assert spec.read_text() == NEW_TEXT
    assert _litter(tmp_path) == []


def test_amend_parks_the_exact_bytes_and_the_mode(tmp_path):
    """Catches: a parked copy that is re-encoded or newline-translated, that has
    another mode than the specification had, or that shares its inode."""
    spec = tmp_path / "spec.md"
    raw = b"# Goal\r\n\r\nDo the thing.\r\n"
    spec.write_bytes(raw)
    os.chmod(spec, 0o640)
    record = _amend(spec)
    parked = Path(record.from_path)
    assert parked.read_bytes() == raw
    assert stat.S_IMODE(parked.stat().st_mode) == 0o640
    assert parked.stat().st_nlink == 1
    assert _litter(tmp_path) == []
    assert spec.read_text() == NEW_TEXT


def test_amend_reuses_a_parked_copy_of_the_same_text_and_refuses_a_different_one(
        tmp_path, monkeypatch):
    """Catches: a same-second retry that fails on its own parked copy, and a
    different file at the parked name that is silently accepted."""
    monkeypatch.setattr(amendment, "utcnow", lambda: "2026-10-08T12:00:00Z")
    spec = _spec(tmp_path)
    parked = tmp_path / f"spec.md.v2026-10-08T12-00-00Z.{amendment.text_digest(OLD_TEXT)}"
    parked.write_bytes(OLD_TEXT.encode())
    record = _amend(spec)
    assert Path(record.from_path) == parked
    assert parked.read_text() == OLD_TEXT
    assert spec.read_text() == NEW_TEXT

    spec.write_text(OLD_TEXT, encoding="utf-8")
    parked.write_bytes(b"something else")
    with pytest.raises(FileExistsError):
        _amend(spec)
    assert spec.read_text() == OLD_TEXT
    assert parked.read_bytes() == b"something else"


# ==== AP: write_role_files =====================================================

def _policy(version: str, mode: str = "auto") -> RoleApprovalPolicy:
    return RoleApprovalPolicy(mode=mode, deny=("Bash(rm:*)",),
                              house_rules=f"house rules {version}")


def test_role_files_full_disk_keeps_the_earlier_file(tmp_path, monkeypatch):
    """Catches: a role file replaced by a partial one. A fault on the settings
    leaves the tree as it was; a fault on the prompt leaves the prompt as it
    was and the settings whole."""
    _policy("v1").write_role_files(tmp_path, "qa")
    approval = tmp_path / "approval"
    v1 = _tree(approval)
    v2 = _policy("v2", mode="dontAsk")

    _install(monkeypatch, write=_err_write_for(".qa.settings.json.tmp."))
    with pytest.raises(DiskFullError):
        v2.write_role_files(tmp_path, "qa")
    assert _tree(approval) == v1

    monkeypatch.undo()
    _install(monkeypatch, write=_err_write_for(".house_rules.md.tmp."))
    with pytest.raises(DiskFullError):
        v2.write_role_files(tmp_path, "qa")
    assert (approval / "house_rules.md").read_text() == "house rules v1\n"
    assert json.loads((approval / "qa.settings.json").read_text())["permissions"][
        "defaultMode"] == "dontAsk"
    assert _litter(approval) == []


def test_role_files_are_written_with_the_umask_mode(tmp_path):
    """Catches: a role file written with a fixed mode where `write_text` let
    the umask decide."""
    with _umask(0o027):
        settings, prompt = replace(_policy("v1")).write_role_files(tmp_path, "qa")
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert stat.S_IMODE(prompt.stat().st_mode) == 0o640
    assert prompt.read_text() == "house rules v1\n"


# ==== G: governance ============================================================

def _contract():
    return compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un", scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))


def _log(run_dir: Path) -> Path:
    return run_dir / "autonomy" / "routings.jsonl"


def test_decide_full_disk_records_nothing_and_returns_no_routing(tmp_path, monkeypatch):
    """Catches: a routing handed back as made when its record could not be
    written, or a log that changed anyway."""
    governor = bind(tmp_path, contract=_contract())
    governor.decide(decision="first")
    before = _log(tmp_path).read_bytes()
    _install(monkeypatch, write=_err_write_for("routings.jsonl"))
    with pytest.raises(DiskFullError):
        governor.decide(decision="second")
    assert _log(tmp_path).read_bytes() == before

    monkeypatch.undo()
    governor.decide(decision="second")
    assert len(governor.routings()) == 2


def test_decide_after_a_short_write_starts_on_a_line_of_its_own(tmp_path, monkeypatch):
    """Catches: the next record glued onto the fragment a failed append left."""
    governor = bind(tmp_path, contract=_contract())
    _install(monkeypatch, write=_short_write_for("routings.jsonl", 7))
    with pytest.raises(DiskFullError):
        governor.decide(decision="torn")
    monkeypatch.undo()
    governor.decide(decision="whole")
    last = _log(tmp_path).read_text().splitlines()[-1]
    assert json.loads(last)["decision"] == "whole"


def test_record_override_full_disk_raises_and_appends_nothing(tmp_path, monkeypatch):
    """Catches: an override that goes through with its record missing."""
    governor = bind(tmp_path, contract=_contract())
    routing = governor.decide(decision="publish it", delegable=False)
    before = _log(tmp_path).read_bytes()
    _install(monkeypatch, write=_err_write_for("routings.jsonl"))
    with pytest.raises(DiskFullError):
        record_override(governor, routing=routing, by="captain", what="published anyway")
    assert _log(tmp_path).read_bytes() == before

    monkeypatch.undo()
    record_override(governor, routing=routing, by="captain", what="published anyway")
    assert json.loads(_log(tmp_path).read_text().splitlines()[-1])["by"] == "captain"


def test_bind_full_disk_on_the_witness_pins_nothing_and_the_run_can_be_bound_again(
        tmp_path, monkeypatch):
    """Catches: a pin written before its witness, which leaves a run that every
    later call refuses."""
    _install(monkeypatch, write=_err_write_for(".autonomy.json.tmp."))
    with pytest.raises(DiskFullError):
        bind(tmp_path, contract=_contract())
    assert not pinned_path(tmp_path).exists()
    assert not (tmp_path / "approval" / "autonomy.json").exists()

    monkeypatch.undo()
    assert bind(tmp_path, contract=_contract()) is not None
    assert pinned_path(tmp_path).is_file()


def test_bind_full_disk_on_the_pin_leaves_a_state_that_binds_again(tmp_path, monkeypatch):
    """Catches: a half-written pin, or a witness-only state that cannot be
    repaired by offering the same contract again."""
    _install(monkeypatch, write=_err_write_for(".contract.json.tmp."))
    with pytest.raises(DiskFullError):
        bind(tmp_path, contract=_contract())
    assert not pinned_path(tmp_path).exists()

    monkeypatch.undo()
    first = bind(tmp_path, contract=_contract())
    assert first is not None
    again = bind(tmp_path)
    assert again.contract.digest() == first.contract.digest()


def test_bind_refuses_a_different_contract_when_only_the_pin_is_missing(tmp_path):
    """Catches: a different contract pinned over the record of what the run was
    bound to. Control: the original contract pins again."""
    bind(tmp_path, contract=_contract())
    pinned_path(tmp_path).rename(tmp_path / "contract.moved")
    with pytest.raises(ContractRefused, match="bound to contract"):
        bind(tmp_path, contract=compile_profile(AutonomyMode.SUPERVISED, contract_id="sup"))
    assert not pinned_path(tmp_path).exists()

    assert bind(tmp_path, contract=_contract()) is not None
    assert pinned_path(tmp_path).is_file()


def test_bind_still_refuses_a_pin_whose_witness_is_gone(tmp_path):
    """Catches: the tamper guard lost by the new write order. A pin with no
    witness and no recorded decision is still refused."""
    bind(tmp_path, contract=_contract())
    (tmp_path / "approval" / "autonomy.json").rename(tmp_path / "autonomy.moved")
    with pytest.raises(ContractRefused):
        bind(tmp_path)


# ==== R: restore ===============================================================

def _run_state(**kw) -> RunState:
    base = {"run_id": "run1", "repo_path": "/tmp/repo", "project_name": "demo",
            "spec_path": "/tmp/spec.md", "spec_digest": "s", "policy_digest": "p",
            "profile_digest": "h"}
    return RunState(**{**base, **kw})


def _make_run(tmp_path: Path) -> RunStore:
    """A run with a state, a receipt and a 5000-byte log."""
    store = RunStore(tmp_path / "runs", "run1")
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


@pytest.mark.parametrize("overwrite", [True, False])
def test_restore_full_disk_mid_extraction_leaves_the_existing_run_untouched(
        tmp_path, monkeypatch, overwrite):
    """Catches: a restore that extracts over the live run, so that a full disk
    half way leaves a run of old and new files. The staging directory is named
    in the error and left in place; nothing else changes."""
    store = _make_run(tmp_path)
    archive = store.backup(tmp_path / "backups")
    (store.receipts_dir / "r2.json").write_text('{"n": 2}')
    before, runs_before = _tree(store.dir), list_runs(store.root)
    real = tarfile.TarFile.extractall

    def flaky(self, path=".", members=None, **kwargs):
        real(self, path, members=list(members)[:3], **kwargs)
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    monkeypatch.setattr(tarfile.TarFile, "extractall", flaky)
    with pytest.raises(DiskFullError) as caught:
        store.restore(archive, overwrite=overwrite)
    staged = list((store.root / "_restore").iterdir())
    assert len(staged) == 1
    assert str(staged[0]) in str(caught.value)
    assert _tree(store.dir) == before
    assert list_runs(store.root) == runs_before
    assert not [p for p in store.root.iterdir() if p.name.startswith("run1-restored")]

    monkeypatch.undo()
    restored = store.restore(archive, overwrite=overwrite)
    assert RunStore(store.root, restored.name).read_state().run_id == "run1"
    if overwrite:
        assert restored == store.dir


def test_restore_refuses_for_lack_of_room_before_creating_anything(tmp_path, monkeypatch):
    """Catches: a room check that is not tied to the size of the backup (with a
    100-byte report the reserve alone refused, and `require_room(root, 0)`
    survived). 1 MiB + 1000 bytes is above the reserve and below the reserve
    plus the run's 5000-byte log."""
    store = _make_run(tmp_path)
    archive = store.backup(tmp_path / "backups")
    listing = sorted(os.listdir(store.root))
    _install(monkeypatch, statvfs=_statvfs_reporting((1 << 20) + 1000))
    with pytest.raises(DiskFullError):
        store.restore(archive)
    assert sorted(os.listdir(store.root)) == listing
    assert not (store.root / "_restore").exists()

    monkeypatch.undo()
    _install(monkeypatch, statvfs=_statvfs_reporting(1 << 30))
    restored = store.restore(archive)
    assert restored != store.dir and (restored / "state.json").is_file()


def test_restore_reads_the_backup_back_before_it_replaces_anything(tmp_path):
    """Catches: a restore that publishes an unloadable backup over a live run."""
    store = _make_run(tmp_path)
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info, payload = _file_member("run1/state.json", b"not json")
        tar.addfile(info, io.BytesIO(payload))
    before = _tree(store.dir)
    with pytest.raises(StoreError):
        store.restore(archive, overwrite=True)
    assert _tree(store.dir) == before
    assert list_runs(store.root) == ["run1"]


def test_restore_overwrite_replaces_what_the_archive_has_and_keeps_the_rest(
        tmp_path, monkeypatch):
    """Catches: an overwrite that drops what the backup lacks, replaces the
    state before the files it points at (`zz.json` sorts after `state.json`,
    so the alphabetical order hides a missing `last`), or replaces it twice."""
    store = _make_run(tmp_path)
    (store.dir / "zz.json").write_text('{"zz": "backed up"}')
    backed_up = store.read_state().iteration
    archive = store.backup(tmp_path / "backups")
    (store.dir / "attic").mkdir()
    (store.dir / "attic" / "old.json").write_text("{}")
    (store.receipts_dir / "r2.json").write_text('{"n": 2}')
    changed = store.read_state()
    changed.iteration = backed_up + 7
    store.write_state(changed)
    (store.dir / "zz.json").write_text('{"zz": "changed"}')

    replaced: list[str] = []
    real = os.replace

    def replace(src, dst):
        replaced.append(Path(dst).name)
        return real(src, dst)
    _install(monkeypatch, replace=replace)
    assert store.restore(archive, overwrite=True) == store.dir
    assert store.read_state().iteration == backed_up
    assert (store.dir / "zz.json").read_text() == '{"zz": "backed up"}'
    assert (store.dir / "attic" / "old.json").is_file()
    assert (store.receipts_dir / "r2.json").is_file()
    assert replaced[-1] == "state.json"
    assert replaced.count("state.json") == 1
    assert replaced.index("zz.json") < replaced.index("state.json")


def test_restore_twice_in_one_second_never_overlays(tmp_path, monkeypatch):
    """Catches: a second restore in the same second that lands on, and mixes
    into, the first one's directory."""
    store = _make_run(tmp_path)
    archive = store.backup(tmp_path / "backups")
    monkeypatch.setattr(hoh_store, "_stamp", lambda: "2026-10-08T12-00-00Z")
    first = store.restore(archive)
    (first / "marker.txt").write_text("first")
    second = store.restore(archive)
    assert second != first
    assert (first / "marker.txt").read_text() == "first"
    assert not (second / "marker.txt").exists()
    assert (second / "state.json").is_file()


def test_restore_path_outside_the_run_is_refused_with_nothing_created(tmp_path):
    """Catches: a traversal member that is refused only after a directory was
    created, or that writes outside the run."""
    store = _make_run(tmp_path)
    archive = _archive_with(store.backup(tmp_path / "backups"), tmp_path / "evil.tar.gz",
                            [_file_member("run1/../../escape.txt", b"x")])
    listing = sorted(os.listdir(store.root))
    with pytest.raises(StoreError):
        store.restore(archive)
    assert sorted(os.listdir(store.root)) == listing
    assert not (tmp_path / "escape.txt").exists()
    assert not (store.root / "escape.txt").exists()


def test_restore_sibling_prefix_path_is_refused_as_a_store_error(tmp_path):
    """Catches: a string-prefix traversal test, which lets `run1/../run1x/p`
    through (only the tar filter stopped it, with an exception that is not a
    StoreError and a staging directory left behind)."""
    store = _make_run(tmp_path)
    archive = _archive_with(store.backup(tmp_path / "backups"), tmp_path / "evil.tar.gz",
                            [_file_member("run1/../run1x/p", b"x")])
    listing = sorted(os.listdir(store.root))
    with pytest.raises(StoreError):
        store.restore(archive, overwrite=True)
    assert sorted(os.listdir(store.root)) == listing
    assert not (store.root / "run1x").exists()


def test_restore_strips_setuid_and_group_write_with_the_tar_filter(tmp_path):
    """Catches: an extraction with `fully_trusted`, which keeps setuid and the
    group/other write bits of a backup member."""
    store = _make_run(tmp_path)
    tool = store.dir / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    os.chmod(tool, 0o4777)
    archive = store.backup(tmp_path / "backups")
    restored = store.restore(archive)
    mode = (restored / "tool.sh").stat().st_mode
    assert not mode & (stat.S_ISUID | stat.S_IWGRP | stat.S_IWOTH)


@needs_unprivileged_user
def test_restore_refuses_a_backup_with_an_unreadable_directory_and_touches_nothing(tmp_path):
    """Catches: a restore that publishes a backup it cannot read back
    completely (the walk skipped the directory), over a live run. The member is
    refused by name before anything is created, so no staging directory is
    left behind."""
    store = _make_run(tmp_path)
    folder = tarfile.TarInfo("run1/locked")
    folder.type = tarfile.DIRTYPE
    folder.mode = 0
    archive = _archive_with(store.backup(tmp_path / "backups"), tmp_path / "locked.tar.gz",
                            [(folder, b""), _file_member("run1/locked/f.txt", b"x")])
    before = _tree(store.dir)
    with pytest.raises(StoreError, match="owner cannot read"):
        store.restore(archive, overwrite=True)
    assert _tree(store.dir) == before
    assert not (store.root / "_restore").exists()
