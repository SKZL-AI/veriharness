"""Registered ephemeral temporary roots (P2-42, amendment A3): `hoh.storage_ephemeral`,
the registry's ``tmp`` kind, and HoH's own run scratch (`runner.run_check(scratch=)`).

The world is `test_storage_tiers.World`: a real registry with injected OS
readers (a process table, a boot id), a canonical repository, owned scratch and
receipts. Nothing here reads the host's ``/tmp`` or ``~`` and the only ``/proc``
the product reads is its own ``/proc/self/fdinfo`` (the mount of a directory
descriptor); every temporary tree is under pytest's ``tmp_path``. A creator that "crashes" is a
process that leaves the process table (`World.end_setup`) while its workspace
stays on disk.

Every test name contains the selector it is run by; every docstring names the
failure (the mutant) it catches and the negative control that changes only the
condition under test. Tests change state by writing and renaming; the only
removals are the product's own: the creator's cleanup, and the runner's none.

What is *not* here: judging, promoting or reclaiming an orphan (P5-35).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import hoh.storage_ephemeral as se
from hoh import storage_tiers as st
from hoh.durable_io import DiskFullError
from hoh.storage_registry import StorageOwnershipError
from test_storage_tiers import (  # noqa: F401  (isolated_env is an autouse fixture)
    BOOT,
    OTHER_PID,
    PROJECT,
    SETUP_PID,
    FakeAdmission,
    World,
    isolated_env,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path / "w")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def roots(world: World, *, admission=None, session: str = "s1", pid: int = SETUP_PID,
          project: str = PROJECT) -> se.EphemeralRoots:
    return se.EphemeralRoots(world.registry, project, session, admission=admission,
                             getpid=lambda: pid)


# --------------------------------------------------------------------------- #
# R -- a registered root, a record before content, the creator's own cleanup
# --------------------------------------------------------------------------- #


def test_tmp_root_and_workspace_registered_before_content(w, monkeypatch):
    """R1. Catches a workspace handed out before it is registered: at the
    first moment the caller holds the path, the project's ``tmp`` root for the
    session is a registered scratch directory (a PROTECTED root with its
    ownership record and index entry), the workspace is an empty directory
    the registry answers OWNED for, and its record beside it already says
    ACTIVE with the project, session, run, creator, purpose, the creator's
    process lease (pid, start time, boot id) and the workspace's own inode.
    The record is private (mode 0600) from its first byte, not only after the
    rewrite. Negative control: a directory made by hand in the root has no
    record."""
    run = "20261006t0100z-r1"
    modes: list[int] = []
    real_write = se.durable_write

    def spy(path, payload, **kw):
        modes.append(stat_mode(path))            # the REGISTERED record, before it is replaced
        return real_write(path, payload, **kw)

    monkeypatch.setattr(se, "durable_write", spy)
    with roots(w).workspace(creator="runner.run_check", purpose="check", run_id=run) as ws:
        root = ws.path.parent
        assert root == w.registry.state_root / "projects" / PROJECT / "tmp" / "s1"
        assert w.registry.owner_of(root).status == "PROTECTED"
        assert (root / ".vh-owner.json").is_file()
        index = json.loads((w.registry.state_root / "projects" / PROJECT
                            / "SCRATCH_INDEX.json").read_text())
        assert any(e["kind"] == "tmp" and e["session_id"] == "s1" for e in index["entries"])
        assert list(ws.path.iterdir()) == []
        owner = w.registry.owner_of(ws.path)
        assert (owner.status, owner.kind, owner.session_id) == ("OWNED", "tmp", "s1")
        record = se.read_record(ws.path)
        info = os.lstat(ws.path)
        assert record["state"] == se.ACTIVE
        assert (record["project_id"], record["session_id"], record["run_id"]) == (
            PROJECT, "s1", run)
        assert (record["creator"], record["purpose"]) == ("runner.run_check", "check")
        assert (record["st_dev"], record["st_ino"]) == (info.st_dev, info.st_ino)
        assert (record["pid"], record["pid_start"], record["boot_id"]) == (
            SETUP_PID, w.fake.procs[SETUP_PID], BOOT)
        assert record["workspace_id"] == ws.path.name and ws.path.name.startswith(run + "-")
        assert datetime.fromisoformat(record["created_utc"]).tzinfo is not None
        assert se.record_path(ws.path) == root / f".{ws.path.name}.vh-ws.json"
        assert stat_mode(se.record_path(ws.path)) == 0o600
        assert modes == [0o600]
    monkeypatch.setattr(se, "durable_write", real_write)
    by_hand = root / "by-hand"
    by_hand.mkdir()
    assert se.read_record(by_hand) is None


def stat_mode(path: Path) -> int:
    return os.lstat(path).st_mode & 0o777


def test_a_workspace_of_a_live_creator_is_hot_and_a_lookalike_is_unknown(w, tmp_path):
    """R2. Catches a workspace that the tiers would treat as free to touch
    while its creator lives: with the creator's session lease live the
    workspace is HOT (``lease:``), the registry's own rule for every scratch kind;
    a copy of the workspace and its record outside every registered root is
    refused UNKNOWN_OWNERSHIP, record or no record. Negative control: a
    directory of the same content in the project's ``scratch`` of the same live
    session is HOT too, so it is the lease, not the name, that decides."""
    ws = roots(w).create(creator="t", purpose="test")
    (ws.path / "f.txt").write_text("x")
    decision = w.classify(ws.path)
    assert decision.tier == st.HOT and any(r.startswith("lease:") for r in decision.reasons)
    foreign = tmp_path / "foreign" / ws.path.name
    shutil.copytree(ws.path, foreign)
    shutil.copy(se.record_path(ws.path), se.record_path(foreign))
    assert w.classify(foreign).refused == st.UNKNOWN_OWNERSHIP
    shutil.copytree(ws.path, w.scratch / "same-content")
    assert w.classify(w.scratch / "same-content").tier == st.HOT


def test_workspace_cleaned_by_its_creator(w, monkeypatch):
    """R3. Catches a creator that leaves its workspace, or removes it the wrong
    way: on a clean exit, on an exception in the body (which still propagates)
    and after ``keep`` the record says RELEASED -- ``removed`` true for the
    first two, false (and the tree still there) for ``keep`` -- with the
    reservation's id, a creation time and mode 0600 after the rewrite, and the
    reservation is released with the bytes actually written **at every depth**
    (a **sparse** file is counted at what it occupies, not at its apparent size:
    a 256 MiB hole adds nothing -- the same rule books a terabyte hole at nothing
    -- and the hole is left out on a filesystem without holes).
    The removal never calls `shutil.rmtree` and every ``unlink``/``rmdir`` of the
    exit is relative to a directory descriptor (``dir_fd``), never a path.
    Negative control: before the exit the workspace is there and ACTIVE."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    probe = w.tmp / "sparse-probe"                       # does this filesystem keep holes?
    with open(probe, "wb") as hole:                      # 256 MiB: the runner's own limit is 512 MiB
        hole.truncate(256 << 20)
    sparse_ok = os.stat(probe).st_blocks * 512 < (1 << 20)
    calls: list[tuple[str, int | None]] = []
    real_unlink, real_rmdir, real_rmtree = os.unlink, os.rmdir, shutil.rmtree

    def unlink(path, *args, **kwargs):
        calls.append(("unlink", kwargs.get("dir_fd")))
        return real_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        calls.append(("rmdir", kwargs.get("dir_fd")))
        return real_rmdir(path, *args, **kwargs)

    def rmtree(*args, **kwargs):
        calls.append(("rmtree", None))
        return real_rmtree(*args, **kwargs)

    with r.workspace(creator="t", purpose="test") as ws:
        (ws.path / "f.bin").write_bytes(b"x" * 123)
        (ws.path / "d" / "e").mkdir(parents=True)
        (ws.path / "d" / "e" / "deep.bin").write_bytes(b"y" * 7)
        (ws.path / "link").symlink_to("f.bin")           # a link is no written byte
        if sparse_ok:                                    # a 256 MiB hole is not written bytes
            with open(ws.path / "sparse.bin", "wb") as hole:
                hole.truncate(256 << 20)
        assert ws.path.is_dir() and se.read_record(ws.path)["state"] == se.ACTIVE
        monkeypatch.setattr(os, "unlink", unlink)       # only the exit's removal is recorded
        monkeypatch.setattr(os, "rmdir", rmdir)
        monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(os, "rmdir", real_rmdir)
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    assert sorted(name for name, _ in calls) == (
        ["rmdir"] * 3 + ["unlink"] * (4 if sparse_ok else 3))
    assert all(isinstance(fd, int) for _, fd in calls), calls    # by name, relative to a descriptor
    assert not ws.path.exists()
    record = se.read_record(ws.path)
    assert record["state"] == se.RELEASED and record["removed"] is True
    assert record["reservation_id"] == "res-tmp_workspace"
    assert datetime.fromisoformat(record["created_utc"]).tzinfo is not None   # a UTC creation time
    assert stat_mode(se.record_path(ws.path)) == 0o600
    assert admission.reserved[0][:2] == (PROJECT, "tmp_workspace")
    [(_, actual)] = admission.released
    assert actual == 130
    assert r.release(ws) == {"removed": False, "note": "already released"}   # no second release
    assert len(admission.released) == 1
    with pytest.raises(RuntimeError, match="boom"):
        with r.workspace(creator="t", purpose="test") as failing:
            (failing.path / "f").write_text("x")
            raise RuntimeError("boom")
    assert not failing.path.exists()
    assert se.read_record(failing.path)["state"] == se.RELEASED
    with r.workspace(creator="t", purpose="test", keep=True) as kept:
        (kept.path / "sub").mkdir()
        (kept.path / "sub" / "f").write_text("12345")
    assert kept.path.is_dir() and (kept.path / "sub" / "f").exists()
    record = se.read_record(kept.path)
    assert record["state"] == se.RELEASED and record["removed"] is False
    assert len(admission.released) == 3 and admission.released[-1][1] == 5   # a kept one is measured too


@pytest.mark.parametrize("how", ["decoy-dir", "symlink", "linked-root", "swapped-between-stat-and-open",
                                 "root-renamed-away", "root-of-another-device",
                                 "root-of-another-inode"])
def test_cleanup_leaves_what_is_not_the_registered_workspace(w, how, tmp_path):
    """R4. Catches a cleanup that removes -- or even lists -- whatever lies at the
    path: the workspace is moved away and a decoy (a directory with a file, or a
    symlink to a big foreign directory) takes its name, or the workspace is
    reached through a symlink to its root (the root is opened without following
    links), or the root itself was renamed away and replaced by another directory
    (the name is missing, but the workspace is not "gone": it is refused, not
    reported removed, and 0 bytes are booked), or the registered root differs from
    the opened one in the device alone or in the inode alone (both are compared);
    the creator's exit removes nothing, the decoy and its file are
    intact, the reservation is released with **0** bytes (nothing outside the
    registered tree is measured: the foreign directory behind the symlink holds
    5 000 000), and the record says RELEASED with ``removed`` false and why.
    Negative control: unswapped, the same exit removes the workspace
    (`test_workspace_cleaned_by_its_creator`)."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    ws = r.create(creator="t", purpose="test")
    (ws.path / "mine").write_text("mine")
    if how == "linked-root":
        link = tmp_path / "root-link"
        link.symlink_to(ws.path.parent)
        result = r.release(dataclasses.replace(ws, path=link / ws.path.name))
        assert result["removed"] is False and "cannot open the root" in result["note"]
        assert (ws.path / "mine").read_text() == "mine"
        assert admission.released[-1][1] == 0
        return
    if how in ("root-of-another-device", "root-of-another-inode"):
        dev, ino = ws.root_ident                    # the registered root differs in ONE of the two
        wrong = (dev + 1, ino) if how == "root-of-another-device" else (dev, ino + 1)
        removed, note, booked = se._clean_workspace(ws.path, ws.st_dev, ws.st_ino, remove=True,
                                                    root=wrong)
        assert removed is False and "root is not the directory registered" in note and booked == 0
        assert (ws.path / "mine").read_text() == "mine"
        return
    if how == "root-renamed-away":                  # the root is another directory now
        root = ws.path.parent
        away = root.with_name(root.name + "-away")
        root.rename(away)
        root.mkdir(mode=0o700)
        result = r.release(ws)
        assert result["removed"] is False and "root is not the directory registered" in result["note"]
        assert (away / ws.path.name / "mine").read_text() == "mine"     # still on disk
        assert admission.released[-1][1] == 0
        assert not (root / ws.path.name).exists()
        return
    if how == "swapped-between-stat-and-open":
        real = se._identity

        def identity(fd, path):                       # the descriptor is another directory
            dev, ino, mnt = real(fd, path)
            return (dev, ino + 1, mnt) if path == str(ws.path) else (dev, ino, mnt)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(se, "_identity", identity)
            result = r.release(ws)
        assert result["removed"] is False and "no longer the inode" in result["note"]
        assert (ws.path / "mine").read_text() == "mine" and admission.released[-1][1] == 0
        return
    moved = ws.path.with_name(ws.path.name + "-moved")
    ws.path.rename(moved)
    decoy = tmp_path / "decoy" if how == "symlink" else ws.path
    decoy.mkdir()
    (decoy / "precious").write_bytes(b"p" * 5_000_000)
    if how == "symlink":
        ws.path.symlink_to(decoy)
    result = r.release(ws)
    assert result["removed"] is False
    assert (decoy / "precious").stat().st_size == 5_000_000 and (moved / "mine").exists()
    assert admission.released[-1][1] == 0
    record = se.read_record(ws.path)
    assert record["state"] == se.RELEASED and record["removed"] is False
    assert record["release_note"]


def seam_mount(monkeypatch, where: Path, *, same_device: bool, by_fd: bool = False) -> None:
    """Report the directory `where` through the module's `_identity` seam as
    another **mount of the same device** (what a bind mount is: the same
    ``st_dev``, another ``mnt_id``) or as another **device** (the same mount id,
    another ``st_dev``). With ``by_fd`` the descriptor opened for `where` stays
    that mount **whatever path it is asked about** (a mount is a property of the
    descriptor, not of the name the caller gives it): a cleanup that asked the
    workspace's descriptor for its *root's* mount would be told the workspace's
    own. Directory entries are listed in sorted order, so a directory named
    ``zmnt`` is always met last."""
    real, real_entries = se._identity, se._entries
    marked: set[int] = set()

    def identity(fd, path):
        dev, ino, mnt = real(fd, path)
        if path == str(where) or (by_fd and fd in marked):
            marked.add(fd)
            return (dev if same_device else dev + 1), ino, (mnt + 1000 if same_device else mnt)
        return dev, ino, mnt

    monkeypatch.setattr(se, "_identity", identity)
    monkeypatch.setattr(se, "_entries", lambda fd, path: sorted(real_entries(fd, path)))


def test_cleanup_never_descends_into_a_mount_or_writes_through_a_hard_link(w, tmp_path,
                                                                           monkeypatch):
    """R5. Catches a cleanup less careful than the audited removal: a directory
    below the workspace that lies on **another mount of the same device** (what a
    bind mount is; reported through the module's `_identity` seam), or on another
    device, keeps the whole workspace in place -- nothing below it, mount or not,
    is removed -- and the record says why; a file of the workspace hard-linked to
    a file outside keeps its other name and its bytes when the workspace is
    removed (the name is unlinked, never written through), and a symlink to a
    directory is unlinked, never entered; the same for the
    workspace directory itself being another mount than its root (also when
    it holds no directory at all). The walk
    finishes before the first removal: files that sort before the mount are still
    there. Negative control: the workspace without the mount is removed. (The
    real bind mount is R17.)"""
    r = roots(w)
    for same_device, itself in ((True, False), (False, False), (True, True), (True, "flat")):
        ws = r.create(creator="t", purpose="test")
        if itself == "flat":                     # no directory below: only the workspace itself can say
            (ws.path / "plain.txt").write_text("plain")
        else:
            (ws.path / "zmnt").mkdir()
            (ws.path / "zmnt" / "inside.txt").write_text("inside")
            (ws.path / "plain").mkdir()
            (ws.path / "plain" / "f.txt").write_text("plain")
        with monkeypatch.context() as patched:
            seam_mount(patched, ws.path if itself else ws.path / "zmnt", same_device=same_device,
                       by_fd=bool(itself))
            result = r.release(ws)
        assert result["removed"] is False
        assert ("another mount" if same_device else "another device") in result["note"]
        if itself == "flat":
            assert (ws.path / "plain.txt").read_text() == "plain"
            continue
        assert (ws.path / "zmnt" / "inside.txt").read_text() == "inside"
        assert (ws.path / "plain" / "f.txt").read_text() == "plain"     # nothing removed before the refusal
    shared = tmp_path / "shared.txt"
    shared.write_text("keep me")
    clean = r.create(creator="t", purpose="test")
    os.link(shared, clean.path / "linked.txt")
    assert os.stat(shared).st_nlink == 2
    linked_dir = tmp_path / "linked-dir"                 # a symlink to a directory is a name:
    (linked_dir / "sub").mkdir(parents=True)             # unlinked, never entered
    (linked_dir / "sub" / "keep.txt").write_text("keep")
    (clean.path / "dirlink").symlink_to(linked_dir)
    assert r.release(clean)["removed"] is True and not clean.path.exists()
    assert shared.read_text() == "keep me" and os.stat(shared).st_nlink == 1
    assert (linked_dir / "sub" / "keep.txt").read_text() == "keep"


def test_workspace_needs_the_creators_own_live_lease(w):
    """R6. Catches a workspace made without the lease of its creator: with
    the session's lease dead, or live but held by another process's pid,
    `create` raises and nothing exists -- no ``tmp`` root, no workspace, no
    record, no reservation. Negative control: the holder creates one."""
    admission = FakeAdmission("NORMAL")
    pdir = w.registry.state_root / "projects" / PROJECT
    other = roots(w, pid=OTHER_PID, admission=admission)             # a pid that holds no lease
    with pytest.raises(StorageOwnershipError, match="not this process"):
        other.create(creator="t", purpose="test")
    assert not (pdir / "tmp").exists() and admission.reserved == []
    w.end_setup()
    with pytest.raises(StorageOwnershipError, match="no live lease"):
        roots(w, admission=admission).create(creator="t", purpose="test")
    assert not (pdir / "tmp").exists() and admission.reserved == []
    w.revive_setup()
    assert roots(w, admission=admission).create(creator="t", purpose="test").path.is_dir()


def test_admission_refusal_creates_nothing(w):
    """R7. Catches temporary writes that are not admitted: a refused
    reservation raises `TempAdmissionRefused` carrying the admission's answer
    and creates nothing (no root, no workspace, no record); an admitted one is
    released even when the body raises. Negative control: admitted, the
    workspace exists."""
    pdir = w.registry.state_root / "projects" / PROJECT
    refusing = FakeAdmission("CRITICAL", admit=False)
    with pytest.raises(se.TempAdmissionRefused) as caught:
        roots(w, admission=refusing).create(creator="t", purpose="test")
    assert caught.value.refusal.admitted is False
    assert refusing.reserved == [(PROJECT, "tmp_workspace", None)]
    assert not (pdir / "tmp").exists()
    granting = FakeAdmission("NORMAL")
    with pytest.raises(RuntimeError):
        with roots(w, admission=granting).workspace(creator="t", purpose="test"):
            raise RuntimeError("body")
    assert len(granting.reserved) == 1 and len(granting.released) == 1


@pytest.mark.parametrize("failing", ["create", "activate", "activate-and-unremovable",
                                     "interrupted", "ledger-raises"])
def test_failed_registration_leaves_no_workspace(w, monkeypatch, failing):
    """R8. Catches a workspace or a record left behind: a full disk while the
    record is created, or while it is rewritten ACTIVE, raises the disk-full
    error, removes the workspace this call made **and the record it wrote for
    it**, and releases the reservation. When the workspace itself cannot be
    removed, its record stays (a workspace is never left without one) and the
    reservation is released all the same. **An interrupt (``KeyboardInterrupt``,
    which is not an ``Exception``) in the middle of a registration is cleaned up
    the same way and propagates**, and **a ledger that fails while the
    reservation is given back never hides the real error** (the disk-full error
    still propagates, not the ledger's). Negative control: without the failure
    the same call succeeds."""
    class LedgerDown(FakeAdmission):
        def release(self, reservation, actual_bytes):
            raise RuntimeError("the ledger is down")

    admission = LedgerDown("NORMAL") if failing == "ledger-raises" else FakeAdmission("NORMAL")
    real_create, real_write = se.durable_create, se.durable_write

    def boom(path, *a, **k):
        if failing == "interrupted":
            raise KeyboardInterrupt
        raise DiskFullError(path, 10, 0, "test")

    monkeypatch.setattr(se, "durable_create" if failing in ("create", "interrupted")
                        else "durable_write", boom)
    real_clean = se._clean_workspace
    if failing == "activate-and-unremovable":
        monkeypatch.setattr(se, "_clean_workspace", lambda *a, **k: (False, "cannot", 0))
    r = roots(w, admission=admission)
    with pytest.raises(KeyboardInterrupt if failing == "interrupted" else DiskFullError):
        r.create(creator="t", purpose="test")
    monkeypatch.setattr(se, "_clean_workspace", real_clean)
    root = w.registry.state_root / "projects" / PROJECT / "tmp" / "s1"
    left = sorted(p.name for p in root.iterdir() if p.name != ".vh-owner.json")
    if failing == "activate-and-unremovable":
        [record_name, workspace_name] = left                      # its record and the workspace
        assert record_name == f".{workspace_name}.vh-ws.json"
        assert se.read_record(root / workspace_name)["state"] == se.REGISTERED
    else:
        assert left == []
    assert len(admission.released) == (0 if failing == "ledger-raises" else 1)
    monkeypatch.setattr(se, "durable_create", real_create)
    monkeypatch.setattr(se, "durable_write", real_write)
    assert roots(w).create(creator="t", purpose="test").path.is_dir()


def test_arguments_are_checked_before_anything_exists(w):
    """R9. Catches a workspace made from a bad declaration: a purpose outside
    the closed list, a blank creator, a name or run id that is not a short
    lower-case id (upper case, too long, a path) is a `ValueError` and nothing
    exists -- no new entry in the root, no reservation. Negative control: every
    purpose of the list is accepted."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    first = r.create(creator="t", purpose="test")
    root = first.path.parent
    before = sorted(p.name for p in root.iterdir())
    for bad in ({"purpose": "banana"}, {"creator": " "}, {"creator": 5}, {"name": "UPPER"},
                {"name": "x" * 60}, {"name": "../escape"}, {"run_id": "Bad Id"}, {"run_id": ""}):
        with pytest.raises(ValueError):
            r.create(**{"creator": "t", "purpose": "test", **bad})
    assert sorted(p.name for p in root.iterdir()) == before and len(admission.reserved) == 1
    for purpose in sorted(se.PURPOSES):
        assert r.create(creator="t", purpose=purpose).path.is_dir()


@pytest.mark.parametrize("case,state", [
    ("alive", se.LIVE), ("pid-gone", se.DEAD), ("pid-reused", se.DEAD), ("another-boot", se.DEAD),
    ("start-time-unreadable", se.UNKNOWN), ("process-table-raises", se.UNKNOWN),
    ("record-without-lease", se.UNKNOWN)])
def test_creator_state_is_dead_only_when_proven(w, case, state):
    """R10. Catches a creator taken for dead on weak evidence: the process in
    the record is LIVE while it exists with the recorded start time; DEAD only
    when proven -- the pid is gone, the pid is another process now (the start
    time differs), or the record is from another boot (also with the same pid
    number alive); UNKNOWN, never dead, when the start time cannot be read, the
    process table raises, or the record holds no lease. (The registry's own
    `lease_is_live` answers "not live" for those; deleting something needs the
    stronger answer.) Negative control: the alive case."""
    ws = roots(w).create(creator="t", purpose="test")
    record = se.read_record(ws.path)
    if case == "pid-gone":
        w.end_setup()
    elif case == "pid-reused":
        w.fake.procs[SETUP_PID] = 5001
    elif case == "another-boot":
        w.registry._boot_id_reader = lambda: "boot-z"
    elif case == "start-time-unreadable":
        w.registry._pid_start_reader = lambda pid: None
    elif case == "process-table-raises":
        def broken(pid):
            raise OSError("no /proc")
        w.registry._pid_exists = broken
    elif case == "record-without-lease":
        record = {k: v for k, v in record.items() if k not in ("pid", "pid_start")}
    got, why = se.creator_state(w.registry, record)
    assert got == state and why, (case, got, why)


def test_read_record_only_a_regular_file_of_the_right_schema(w, tmp_path):
    """R11. Catches a record read from anything: a record that is a symlink to a
    good copy, not a regular file, malformed JSON, another schema or missing
    reads as None. Negative control: the untouched record."""
    ws = roots(w).create(creator="t", purpose="test")
    path = se.record_path(ws.path)
    good = path.read_text()
    assert se.read_record(ws.path)["workspace_id"] == ws.path.name
    copy = tmp_path / "record-copy.json"
    copy.write_text(good)
    path.unlink()
    path.symlink_to(copy)
    assert se.read_record(ws.path) is None
    path.unlink()
    path.mkdir()
    assert se.read_record(ws.path) is None
    path.rmdir()
    path.write_text("{not json")
    assert se.read_record(ws.path) is None
    path.write_text(good.replace("ephemeral-workspace/1", "ephemeral-workspace/9"))
    assert se.read_record(ws.path) is None
    path.rename(path.with_name("moved"))
    assert se.read_record(ws.path) is None


def test_release_rewrites_only_the_record_it_wrote(w, monkeypatch):
    """R13. Catches a release that stamps RELEASED onto any file at the record's
    name: when the record beside the workspace has been replaced by another
    workspace's record (or is gone), the creator's exit still removes its own
    workspace and releases the reservation, but leaves that record exactly as it
    found it and says so. Negative control: with its own record the record
    becomes RELEASED (`test_workspace_cleaned_by_its_creator`). **Two failures of
    the exit's own bookkeeping never stop it**: a record that cannot be rewritten
    (a full disk) and a ledger that fails to take the reservation back -- the
    workspace is still removed, the release still returns (the note says the
    record could not be updated), and the reservation is still given back."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    other = r.create(creator="other", purpose="test")
    ws = r.create(creator="t", purpose="test")
    foreign_record = se.record_path(other.path).read_text()
    se.record_path(ws.path).write_text(foreign_record)
    result = r.release(ws)
    assert result["removed"] is True and not ws.path.exists()
    assert "not the one written at creation" in result["note"]
    assert se.record_path(ws.path).read_text() == foreign_record
    assert se.read_record(other.path)["state"] == se.ACTIVE
    assert len(admission.released) == 1
    se._discard_own_record(other.path, "someone-else")           # not that workspace's record
    assert se.read_record(other.path) is not None
    foreign_schema = json.loads(se.record_path(other.path).read_text())
    foreign_schema["schema"] = "somebody.else/1"                  # nor a record of another schema
    se.record_path(other.path).write_text(json.dumps(foreign_schema))
    se._discard_own_record(other.path, other.path.name)
    assert se.record_path(other.path).exists()
    foreign_schema["schema"] = se.RECORD_SCHEMA
    se.record_path(other.path).write_text(json.dumps(foreign_schema))
    se._discard_own_record(other.path, other.path.name)          # its own: discarded
    assert se.read_record(other.path) is None
    # the record cannot be rewritten: the workspace is removed all the same
    full = r.create(creator="t", purpose="test")
    real_write = se.durable_write

    def boom(path, *a, **k):
        raise DiskFullError(path, 10, 0, "test")

    monkeypatch.setattr(se, "durable_write", boom)
    result = r.release(full)
    monkeypatch.setattr(se, "durable_write", real_write)
    assert result["removed"] is True and not full.path.exists()
    assert "could not be updated" in result["note"]
    assert se.read_record(full.path)["state"] == se.ACTIVE      # the record is as it was
    assert len(admission.released) == 2                          # and the reservation went back
    # the ledger fails: the release still returns, the workspace is removed
    class LedgerDown(FakeAdmission):
        def release(self, reservation, actual_bytes):
            raise RuntimeError("the ledger is down")

    down = roots(w, admission=LedgerDown("NORMAL"))
    ledgerless = down.create(creator="t", purpose="test")
    result = down.release(ledgerless)
    assert result["removed"] is True and not ledgerless.path.exists()
    assert se.read_record(ledgerless.path)["state"] == se.RELEASED


def test_cleanup_finds_a_mount_at_any_depth(w, monkeypatch):
    """R14. Catches a walk that looks only at the first level: a mount (another
    mount of the same device, or another device; reported through the module's
    `_identity` seam) three levels below the workspace keeps the whole workspace
    in place, nothing removed -- its content and a sibling file untouched -- and
    the note says why. Negative control: without the mount the same tree is
    removed (`test_cleanup_never_descends_into_a_mount_or_writes_through_a_hard_link`)."""
    r = roots(w)
    for same_device in (True, False):
        ws = r.create(creator="t", purpose="test")
        deep = ws.path / "a" / "b" / "zmnt"
        deep.mkdir(parents=True)
        (deep / "inside.txt").write_text("inside")
        (ws.path / "a" / "sibling.txt").write_text("sibling")
        (ws.path / "a" / "b" / "aaa.txt").write_text("sorts first")
        with monkeypatch.context() as patched:
            seam_mount(patched, deep, same_device=same_device)
            result = r.release(ws)
        assert result["removed"] is False
        assert ("another mount" if same_device else "another device") in result["note"]
        assert (deep / "inside.txt").read_text() == "inside"
        assert (ws.path / "a" / "sibling.txt").read_text() == "sibling"
        assert (ws.path / "a" / "b" / "aaa.txt").read_text() == "sorts first"


@pytest.mark.parametrize("failing", ["an-entry", "the-workspace-itself"])
def test_a_workspace_that_stays_is_not_reported_removed(w, monkeypatch, failing):
    """R15. Catches a release that says "removed" about a workspace that is still
    there: when an entry cannot be removed (the unlinks remove nothing), or when
    everything in it is gone but the directory itself cannot be removed, the
    result and the record say ``removed`` false with the reason, the workspace is
    still there, the record is RELEASED, and the reservation is released with the
    bytes the walk counted (not 0: they may all still be there). Negative control:
    when it does remove the workspace, ``removed`` is true
    (`test_workspace_cleaned_by_its_creator`)."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    ws = r.create(creator="t", purpose="test")
    (ws.path / "d").mkdir()
    (ws.path / "d" / "f").write_text("x")
    real_unlink, real_rmdir = os.unlink, os.rmdir

    def rmdir(path, *args, **kwargs):
        if path == ws.path.name:                       # the final removal of the workspace
            raise PermissionError(13, "denied", path)
        return real_rmdir(path, *args, **kwargs)

    if failing == "an-entry":
        monkeypatch.setattr(os, "unlink", lambda *a, **k: None)
    else:
        monkeypatch.setattr(os, "rmdir", rmdir)
    result = r.release(ws)
    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(os, "rmdir", real_rmdir)
    assert ws.path.exists()
    assert result["removed"] is False
    assert admission.released[-1][1] == 1       # what the walk counted: it may all still be there
    assert ("some entries could not be removed" if failing == "an-entry"
            else "could not be removed (") in result["note"]
    record = se.read_record(ws.path)
    assert record["state"] == se.RELEASED and record["removed"] is False


def test_a_directory_that_cannot_be_listed_keeps_the_workspace_untouched(w, monkeypatch):
    """R16. Catches an unlistable directory taken for clean: when a directory
    below the workspace cannot be listed (the walk cannot tell whether it is a
    mount), the whole workspace stays in place -- the files that could have been
    removed are not removed either -- and the note says it cannot be listed.
    (Simulated through the module's `_entries` seam, so it holds when the tests
    run as root.) The same for a tree nested deeper than the interpreter can walk
    (``RecursionError``, simulated by lowering the limit): nothing is removed, the
    note says it is nested too deeply, and the release still completes (the record
    is RELEASED, the reservation released) instead of raising out of the creator's
    exit. Negative control: without the failure the same tree is removed."""
    r = roots(w)
    ws = r.create(creator="t", purpose="test")
    (ws.path / "locked").mkdir()
    (ws.path / "locked" / "f").write_text("x")
    (ws.path / "open").mkdir()
    (ws.path / "open" / "g").write_text("y")
    real = se._entries
    locked = str(ws.path / "locked")

    def entries(fd, path):
        if path == locked:
            raise PermissionError(13, "denied", locked)
        return real(fd, path)

    monkeypatch.setattr(se, "_entries", entries)
    result = r.release(ws)
    monkeypatch.setattr(se, "_entries", real)
    assert result["removed"] is False and "cannot be listed" in result["note"]
    assert (ws.path / "open" / "g").read_text() == "y" and (ws.path / "locked" / "f").exists()
    clean = r.create(creator="t", purpose="test")
    (clean.path / "locked").mkdir()
    assert r.release(clean)["removed"] is True
    deep = r.create(creator="t", purpose="test")
    (deep.path / "top.txt").write_text("keep")
    bottom = deep.path
    for _ in range(60):
        bottom = bottom / "d"
    bottom.mkdir(parents=True)
    (bottom / "bottom.txt").write_text("keep")
    shallow = r.create(creator="t", purpose="test")
    (shallow.path / "d" / "d").mkdir(parents=True)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 30)       # the deep walk is 60 levels
    try:
        result = r.release(deep)
        control = r.release(shallow)                          # the same limit, a shallow tree
    finally:
        sys.setrecursionlimit(limit)
    assert result["removed"] is False and "nested too deeply" in result["note"]
    assert (deep.path / "top.txt").read_text() == "keep" and (bottom / "bottom.txt").exists()
    assert se.read_record(deep.path)["state"] == se.RELEASED
    assert control["removed"] is True


@pytest.mark.parametrize("where", ["workspace", "root", "below", "all-flat"])
def test_cleanup_fails_closed_when_the_mount_identity_cannot_be_read(w, monkeypatch, where):
    """R18. Catches a cleanup that proceeds without knowing the mount: when the
    mount of the workspace itself, of its root, or of a directory below it, cannot be read
    (``/proc/self/fdinfo`` gone or unparsable; simulated through `_mount_id`),
    the workspace stays untouched and the note says it fails closed. Negative
    control: with the real reader the same tree is removed; and the reader
    itself returns None -- never a number -- for a descriptor that is not open,
    for ``fdinfo`` without a ``mnt_id`` line and for one whose value is not a
    number (a 0 in both places would read as "the same mount")."""
    r = roots(w)
    ws = r.create(creator="t", purpose="test")
    kept_file = ws.path / ("f.txt" if where == "all-flat" else "sub/f.txt")
    if where == "all-flat":       # nothing is readable (no /proc) and no directory below to say so
        (ws.path / "f.txt").write_text("keep")
    else:
        (ws.path / "sub" / "deeper").mkdir(parents=True)
        (ws.path / "sub" / "f.txt").write_text("keep")
    real = se._mount_id
    seen: list[int] = []

    def mount_id(fd):
        seen.append(fd)
        # read in this order: the workspace, then its root, then the directories below
        index = len(seen)
        if (where == "workspace" and index == 1) or (where == "root" and index == 2) \
                or (where == "below" and index >= 3) or where == "all-flat":
            return None
        return real(fd)

    monkeypatch.setattr(se, "_mount_id", mount_id)
    result = r.release(ws)
    monkeypatch.setattr(se, "_mount_id", real)
    assert result["removed"] is False and "failing closed" in result["note"]
    assert kept_file.read_text() == "keep"
    clean = r.create(creator="t", purpose="test")
    (clean.path / "sub").mkdir()
    assert r.release(clean)["removed"] is True
    fd = os.open(w.tmp, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert isinstance(real(fd), int)
        assert se._mount_id(10 ** 6) is None             # not an open descriptor: None, not an exception
        for text, expected in ((b"pos:\t0\nflags:\t02000000\n", None),      # no mnt_id line at all
                               (b"mnt_id:\tabc\n", None), (b"mnt_id:\n", None),   # not a number
                               (b"pos:\t0\nmnt_id:\t17\nino:\t5\n", 17)):      # the control
            monkeypatch.setattr(se, "open", lambda *a, _t=text, **k: io.BytesIO(_t), raising=False)
            assert se._mount_id(fd) == expected, text    # None, never 0: two 0s would be "the same mount"
        monkeypatch.delattr(se, "open")
    finally:
        os.close(fd)


_BIND_CHILD = r"""
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hoh.storage_ephemeral as se
td = Path(sys.argv[2])
root = td / "root"
root.mkdir(mode=0o700)
foreign = td / "foreign"
foreign.mkdir()
(foreign / "sentinel.txt").write_text("precious")
(foreign / "sub").mkdir()
(foreign / "sub" / "deep.txt").write_text("deep")


def tree(name):
    ws = root / name
    ws.mkdir(mode=0o700)
    (ws / "a" / "b").mkdir(parents=True)
    (ws / "a" / "keep.txt").write_text("keep")
    (ws / "top.txt").write_text("top")
    return ws


def run(name, mount_rel):
    ws = tree(name)
    info = os.lstat(ws)
    where = ws / mount_rel
    subprocess.run(["mount", "--bind", str(foreign), str(where)], check=True)
    same_device = os.lstat(where).st_dev == info.st_dev
    removed, note, nbytes = se._clean_workspace(ws, info.st_dev, info.st_ino, remove=True)
    out = {"same_device": same_device, "removed": removed, "note": note, "bytes": nbytes,
           "sentinel": (foreign / "sentinel.txt").exists(), "deep": (foreign / "sub" / "deep.txt").exists(),
           "kept": (ws / "top.txt").exists(), "ws": ws.exists()}
    subprocess.run(["umount", str(where)], check=True)
    return out


def selfbind():
    ws = tree("ws-self")                       # the workspace itself bind-mounted onto itself:
    info = os.lstat(ws)                        # the same inode, the same device, another mount
    subprocess.run(["mount", "--bind", str(ws), str(ws)], check=True)
    removed, note, nbytes = se._clean_workspace(ws, info.st_dev, info.st_ino, remove=True)
    out = {"removed": removed, "note": note, "bytes": nbytes,
           "kept": (ws / "top.txt").exists() and (ws / "a" / "keep.txt").exists()}
    subprocess.run(["umount", str(ws)], check=True)
    return out


result = {"deep": run("ws-deep", "a/b"), "shallow": run("ws-shallow", "a"), "selfbind": selfbind()}
control = tree("ws-control")
info = os.lstat(control)
result["control"] = {"removed": se._clean_workspace(control, info.st_dev, info.st_ino, remove=True)[0],
                     "gone": not control.exists()}
print(json.dumps(result))
"""


def _real_bind_mount_unavailable(tmp_path: Path) -> str:
    """Why a real bind mount cannot be made here (empty when it can): the test
    needs ``unshare -rm`` (a user and mount namespace) and ``mount --bind``."""
    probe = tmp_path / "probe"
    (probe / "a").mkdir(parents=True)
    (probe / "b").mkdir()
    try:
        done = subprocess.run(
            ["unshare", "-rm", "sh", "-c", f"mount --bind {probe}/a {probe}/b && umount {probe}/b"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unshare -rm cannot be run ({type(exc).__name__}: {exc})"
    if done.returncode != 0:
        return f"unshare -rm / mount --bind refused here ({done.stderr.strip()[:120]})"
    return ""


def test_cleanup_never_crosses_a_real_bind_mount(tmp_path):
    """R17. Catches a cleanup that walks into foreign data through a bind mount
    of the **same filesystem** (the same ``st_dev``; the device alone cannot see
    it): under a private user and mount namespace (`unshare -rm`) a foreign
    directory is bind-mounted onto a directory two levels below the workspace and
    onto one directly below it; `_clean_workspace` leaves the workspace in
    place and says "another mount", the foreign files (``sentinel.txt``,
    ``sub/deep.txt``) are intact, the workspace's own file is intact, and no byte
    is booked; and a workspace bind-mounted **onto itself** (the same inode and
    device, another mount than its root) is refused with its files intact.
    Negative control: the same tree without a mount is removed.
    **NOT_RUN (skipped, with the reason) when `unshare -rm` or a bind mount is not
    available here** -- as it can be inside a sandbox; the seam tests R5, R14 and
    R18 hold the logic then."""
    reason = _real_bind_mount_unavailable(tmp_path)
    if reason:
        pytest.skip(f"NOT_RUN: {reason}")
    work = tmp_path / "bind"
    work.mkdir()
    done = subprocess.run(["unshare", "-rm", sys.executable, "-B", "-c", _BIND_CHILD,
                           str(ROOT / "src"), str(work)], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-600:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    for name in ("deep", "shallow"):
        got = result[name]
        assert got["same_device"] is True, got           # the case st_dev cannot see
        assert got["removed"] is False and "another mount" in got["note"], got
        assert got["bytes"] == 0 and got["ws"] and got["kept"], got
        assert got["sentinel"] and got["deep"], got      # nothing foreign touched
    selfbound = result["selfbind"]                       # no foreign data: its own mount is the case
    assert selfbound["removed"] is False and "another mount" in selfbound["note"], selfbound
    assert selfbound["kept"] and selfbound["bytes"] == 0, selfbound
    assert result["control"] == {"removed": True, "gone": True}


def test_release_of_a_gone_workspace_and_no_descriptor_is_leaked(w, monkeypatch):
    """R19. Catches a release that leaks descriptors or misreports a workspace that
    is already gone: releasing a workspace whose directory is gone reports
    ``removed`` true ("already gone"); and the number of descriptors this process
    holds is the same after a release that removes, one that refuses (a mount in
    it), one of a gone workspace and a kept one -- the root and directory
    descriptors of the walk are always closed; and every directory the cleanup
    opens is opened ``O_NOFOLLOW | O_DIRECTORY | O_CLOEXEC`` (a FIFO swapped in
    must not block the open, a link must not be followed, a child process must
    not inherit it). Negative control: the counts before and after are equal."""
    r = roots(w)

    def count() -> int:
        return len(os.listdir("/proc/self/fd"))

    removed_ws = r.create(creator="t", purpose="test")
    (removed_ws.path / "d").mkdir()
    refused = r.create(creator="t", purpose="test")
    (refused.path / "m").mkdir()
    gone = r.create(creator="t", purpose="test")
    kept = r.create(creator="t", purpose="test")
    (kept.path / "sub" / "deeper").mkdir(parents=True)
    gone.path.rmdir()
    before = count()
    assert r.release(removed_ws)["removed"] is True
    with monkeypatch.context() as patched:
        seam_mount(patched, refused.path / "m", same_device=True)
        assert r.release(refused)["removed"] is False
    result = r.release(gone)
    assert result["removed"] is True and "already gone" in result["note"]
    assert r.release(kept, keep=True)["removed"] is False
    assert count() == before
    # every directory the cleanup opens is opened without following links, as a
    # directory, and not inherited by a child process (a FIFO swapped in must not
    # block the open, a link must not be followed, a forked check must not hold it)
    flagged = r.create(creator="t", purpose="test")
    (flagged.path / "a" / "b").mkdir(parents=True)
    (flagged.path / "a" / "f.txt").write_text("x")
    real_open, seen_flags = os.open, []

    def recording_open(path, flags, *args, **kwargs):
        seen_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    outcome = se._clean_workspace(flagged.path, flagged.st_dev, flagged.st_ino, remove=True,
                                  root=flagged.root_ident)
    monkeypatch.setattr(os, "open", real_open)
    assert outcome[0] is True
    need = os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
    assert len(seen_flags) == 6                       # root, workspace, a, b (walk) ; a, b (removal)
    assert all(flag & need == need for flag in seen_flags), seen_flags


def test_a_mount_that_appears_after_the_walk_is_caught_by_the_removal(w, monkeypatch):
    """R20. Catches a removal that trusts the walk: a directory that is an ordinary
    one when the walk checks it but another mount when the removal reaches it (a
    mount made in between; the seam answers differently the second time) is
    refused **by the removal's own check** -- nothing below it is removed, the
    note says another mount, the mount's file is intact. Negative control: the
    same tree without the change is removed (`test_workspace_cleaned_by_its_creator`)."""
    admission = FakeAdmission("NORMAL")
    r = roots(w, admission=admission)
    ws = r.create(creator="t", purpose="test")
    (ws.path / "zmnt").mkdir()
    (ws.path / "zmnt" / "inside.txt").write_text("inside")
    real = se._identity
    seen: dict[str, int] = {}

    def identity(fd, path):
        dev, ino, mnt = real(fd, path)
        seen[path] = seen.get(path, 0) + 1
        if path == str(ws.path / "zmnt") and seen[path] >= 2:     # the second look: a mount
            return dev, ino, mnt + 1000
        return dev, ino, mnt

    monkeypatch.setattr(se, "_identity", identity)
    result = r.release(ws)
    monkeypatch.setattr(se, "_identity", real)
    assert seen[str(ws.path / "zmnt")] == 2
    assert result["removed"] is False and "another mount" in result["note"]
    assert "the removal stopped there" in result["note"]
    assert (ws.path / "zmnt" / "inside.txt").read_text() == "inside"
    assert admission.released[-1][1] == 6        # the walk's bytes stay booked: they are still there


@pytest.mark.parametrize("swap", ["directory-becomes-a-link", "directory-moved-away",
                                  "root-moved-away", "directory-replaced-by-a-foreign-one",
                                  "foreign-directory-renamed-in", "file-replaced",
                                  "directory-replaced-by-a-fifo"])
def test_the_removal_acts_on_what_it_verified_not_on_names(w, tmp_path, monkeypatch, swap):
    """R21. Catches a removal that goes back to a *name* after the walk: a same-uid
    process changes a name after the walk has seen it, and the removal must touch
    only what it opened and verified -- the same inode, device and mount as the
    walk's snapshot. ``directory-becomes-a-link``: a directory becomes a symlink
    to a foreign directory between the listing and the open -- the open does not
    follow it, nothing foreign is removed. ``directory-moved-away``: during the
    removal a directory is moved away and a symlink to the foreign directory takes
    its name -- the entries are removed from the directory that was opened
    (``unlinkat``/``rmdir`` relative to its descriptor), the foreign file and
    empty directory of the same names stay. ``root-moved-away``: the same for the
    root. **``directory-replaced-by-a-foreign-one``: a foreign real directory of
    the same filesystem is renamed into the name of a directory the walk saw --
    it is not entered and not emptied** (its ``f.txt`` and ``b`` stay), the
    removal stops with "not the directory the walk verified".
    **``foreign-directory-renamed-in``: a foreign directory renamed into a name the
    walk never listed is refused the same way** ("not what the walk verified").
    ``file-replaced``: a file replaced by another inode after the walk is not
    unlinked ("not the file the walk verified"). ``directory-replaced-by-a-fifo``: a
    directory becomes a FIFO between the listing and the open -- the open is
    ``O_DIRECTORY`` and refuses it at once ("cannot be opened"); without that flag
    it would block for ever (the test arms a ten-second alarm, so a missing flag
    fails instead of hanging). (The changes are made through the
    module's `_entries` seam, at the moment named.) Negative control: without a
    change the same tree is removed."""
    r = roots(w)
    ws = r.create(creator="t", purpose="test")
    root = ws.path.parent
    foreign = tmp_path / "foreign"
    (foreign / "b").mkdir(parents=True)
    (foreign / "f.txt").write_text("foreign")
    foreign_root = tmp_path / "foreign-root"
    (foreign_root / ws.path.name).mkdir(parents=True)
    (ws.path / "a" / "b").mkdir(parents=True)
    (ws.path / "a" / "f.txt").write_text("mine")
    (ws.path / "top.txt").write_text("mine")
    (tmp_path / "decoy.txt").write_text("decoy")
    real_entries = se._entries
    seen: dict[str, int] = {}

    def entries(fd, path):
        seen[path] = seen.get(path, 0) + 1
        n = seen[path]
        if swap == "foreign-directory-renamed-in" and path == str(ws.path) and n == 2:
            foreign.rename(ws.path / "new")                  # before the removal's listing
        elif swap == "file-replaced" and path == str(ws.path) and n == 2:
            os.replace(tmp_path / "decoy.txt", ws.path / "top.txt")
        names = real_entries(fd, path)
        if swap == "directory-becomes-a-link" and path == str(ws.path) and n == 1:
            (ws.path / "a").rename(tmp_path / "moved-a")
            (ws.path / "a").symlink_to(foreign)
        elif swap == "directory-replaced-by-a-fifo" and path == str(ws.path) and n == 1:
            (ws.path / "a").rename(tmp_path / "moved-a")
            os.mkfifo(ws.path / "a")
        elif swap == "directory-moved-away" and path == str(ws.path / "a") and n == 2:
            (ws.path / "a").rename(tmp_path / "moved-a")
            (ws.path / "a").symlink_to(foreign)
        elif swap == "root-moved-away" and path == str(ws.path) and n == 2:
            root.rename(tmp_path / "moved-root")
            root.symlink_to(foreign_root)
        elif swap == "directory-replaced-by-a-foreign-one" and path == str(ws.path) and n == 2:
            (ws.path / "a").rename(tmp_path / "moved-a")
            foreign.rename(ws.path / "a")                    # after the listing named "a"
        return names

    def blocked(signum, frame):
        raise TimeoutError("the open of a FIFO blocked: the directory open lacks O_DIRECTORY")

    previous = signal.signal(signal.SIGALRM, blocked)
    monkeypatch.setattr(se, "_entries", entries)
    signal.alarm(10)
    try:
        result = r.release(ws)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        monkeypatch.setattr(se, "_entries", real_entries)
    if swap == "root-moved-away":
        root.rename(tmp_path / "link-left")
        (tmp_path / "moved-root").rename(root)
        assert result["removed"] is True and not ws.path.exists()
        assert (foreign_root / ws.path.name).is_dir()
    else:
        assert result["removed"] is False
    if swap in ("directory-becomes-a-link", "directory-moved-away"):
        assert (foreign / "f.txt").read_text() == "foreign" and (foreign / "b").is_dir()
    if swap == "directory-becomes-a-link":
        assert "cannot be opened" in result["note"] and (tmp_path / "moved-a" / "f.txt").exists()
    if swap == "directory-replaced-by-a-fifo":
        assert "cannot be opened" in result["note"]
        assert stat.S_ISFIFO(os.lstat(ws.path / "a").st_mode)
        assert (tmp_path / "moved-a" / "f.txt").read_text() == "mine"
    if swap == "directory-replaced-by-a-foreign-one":
        assert "not the directory the walk verified" in result["note"]
        assert (ws.path / "a" / "f.txt").read_text() == "foreign" and (ws.path / "a" / "b").is_dir()
        assert (tmp_path / "moved-a" / "f.txt").read_text() == "mine"
    if swap == "foreign-directory-renamed-in":
        assert "not what the walk verified" in result["note"]
        assert (ws.path / "new" / "f.txt").read_text() == "foreign" and (ws.path / "new" / "b").is_dir()
    if swap == "file-replaced":
        assert "not the file the walk verified" in result["note"]
        assert (ws.path / "top.txt").read_text() == "decoy"
    clean = r.create(creator="t", purpose="test")
    (clean.path / "a" / "b").mkdir(parents=True)
    assert r.release(clean)["removed"] is True


def _mountinfo_id(path: str) -> int:
    """The mount id of the mount `path` lies on, from ``/proc/self/mountinfo``:
    the longest mount point that is a prefix of its real path (the last of equals)."""
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        point = fields[4].replace("\\040", " ")
        if real == point or real.startswith(point.rstrip("/") + "/"):
            if best is None or len(point) >= len(best[1]):
                best = (int(fields[0]), point)
    assert best is not None
    return best[0]


def test_the_mount_identity_is_the_kernels_mount_id(w):
    """R22. Catches a mount identity that is not the kernel's (a constant, or the
    device number): `_mount_id` of a directory descriptor equals the mount id
    ``/proc/self/mountinfo`` gives the mount that directory lies on -- for ``/``,
    for the test's directory and for ``/proc``, which is another mount (other id).
    **NOT_RUN (skipped, with the reason) when ``/proc/self/mountinfo`` cannot be
    read.** Negative control: the same lookup of ``/`` and of itself agree."""
    try:
        _mountinfo_id("/")
    except (OSError, AssertionError) as exc:
        pytest.skip(f"NOT_RUN: /proc/self/mountinfo cannot be read ({type(exc).__name__}: {exc})")
    ids = {}
    for path in ("/", str(w.tmp), "/proc"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert se._mount_id(fd) == _mountinfo_id(path), path
            ids[path] = se._mount_id(fd)
        finally:
            os.close(fd)
    assert ids["/proc"] != ids["/"]


def test_ephemeral_module_has_one_confined_removal():
    """R12. Catches a second removal path, or a return of the unchecked one: in
    `hoh.storage_ephemeral` the removal calls are exactly ``unlink`` and ``rmdir``
    in `_remove_tree`, ``rmdir`` in `_clean_workspace` and ``unlink`` in
    `_discard_own_record`; `shutil` is not imported and `rmtree` is not called (judged on the syntax tree, so a docstring may still name it);
    `_clean_workspace` is reached only from `EphemeralRoots.create` (its own
    failed registration) and `EphemeralRoots.release`, `_remove_tree` only from
    `_clean_workspace` and itself, `_discard_own_record` only from `create`; and
    `runner.py` gained no removal. Negative control: the scanner finds the
    removals where they are."""
    from test_verified_relocation import _removal_functions

    source = Path(se.__file__).read_text(encoding="utf-8")
    removers, references, loose = _removal_functions(source)
    assert loose == []
    assert removers == {"_remove_tree": {"unlink", "rmdir"}, "_clean_workspace": {"rmdir"},
                        "_discard_own_record": {"unlink"}}, removers
    tree = ast.parse(source)                    # code, not prose: a docstring may name what is gone
    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = {a.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                for a in node.names} | {node.module for node in ast.walk(tree)
                                        if isinstance(node, ast.ImportFrom)}
    assert "rmtree" not in named and "shutil" not in named and "shutil" not in imported

    def callers(name: str) -> set[str]:
        return {n for n, refs in references.items() if name in refs and n != name}

    assert callers("_clean_workspace") == {"EphemeralRoots.create", "EphemeralRoots.release"}
    assert callers("_remove_tree") == {"_clean_workspace"}
    assert callers("_discard_own_record") == {"EphemeralRoots.create"}
    found, _, stray = _removal_functions(
        (ROOT / "src" / "hoh" / "runner.py").read_text(encoding="utf-8"))
    assert stray == [] and found == {}, found


# --------------------------------------------------------------------------- #
# M -- HoH's own run scratch goes through a registered workspace
# --------------------------------------------------------------------------- #


def _check(command: str):
    from hoh.contracts import AcceptanceCheck

    return AcceptanceCheck(check_id="c1", description="d", command=command, expect_exit=0)


def _candidate(arena: Path):
    from hoh.contracts import Candidate

    return Candidate(candidate_id="c", repo_path=str(arena), commit="0" * 40, tree_clean=True,
                     tree_digest="t")


def test_run_check_scratch_is_the_registered_workspace(w, tmp_path):
    """M1. Catches a check whose scratch is not the registered workspace: with
    ``scratch=`` the command's ``$TMPDIR`` and ``$HOME`` are exactly that
    directory, what it writes lands there, and nothing is made beside the arena
    but the output buffer -- no ``.scratch`` directory. Negative control: without
    ``scratch=`` the sibling ``<arena>.scratch`` is made (the pin's behaviour)."""
    from hoh import runner

    arena = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    arena.mkdir(parents=True)
    command = 'echo "T=$TMPDIR H=$HOME"; touch "$TMPDIR/made"'
    ws = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    receipt, text = runner.run_check(_check(command), _candidate(arena), run_id="r1",
                                     iteration=1, attempt=0, scratch=ws.path)
    assert receipt.runner_ok and receipt.exit_code == 0
    assert f"T={ws.path} H={ws.path}" in text and (ws.path / "made").exists()
    assert sorted(os.listdir(arena.parent)) == sorted(["cand", ".hoh-out-r1-i1-a0-c1.log"])
    runner.run_check(_check(command), _candidate(arena), run_id="r2", iteration=1, attempt=0)
    assert (arena.parent / "cand.scratch").is_dir()


@pytest.mark.parametrize("bad", [
    "relative", "missing", "a-file", "a-symlink", "inside-arena", "holds-the-arena",
    "not-empty", "only-a-dotfile", "only-a-dot-directory", "unlistable", "group-readable",
    "world-writable", "not-owned", "via-symlinked-parent"])
def test_run_check_refuses_an_unusable_scratch_before_anything_runs(w, tmp_path, bad,
                                                                    monkeypatch):
    """M2. Catches a scratch the runner accepts although it cannot vouch for it
    (the guard `_fresh_scratch` gives the runner's own scratch: a planted
    `usercustomize.py` runs as `$HOME` before the command's first line): a
    relative path, a missing one, a file, a symlink to a directory, a directory
    inside the arena, one that holds the arena, one reached **through a symlinked
    parent that lies inside the arena** (the overlap is judged on real paths), one
    that is **not empty** (a file planted before the first check -- also when all
    there is is a dotfile such as `.gitconfig` or a dot directory such as `.local`,
    the places a `git` alias or a user site directory plant code), one **that
    cannot be listed** (the emptiness cannot be told), one with group
    or other permission bits, and one **not owned by this user** are each a
    ``ValueError`` before the
    command starts (a marker the command would write is absent). Negative
    control: an empty 0700 directory of this user beside the arena runs."""
    from hoh import runner

    arena = tmp_path / "arena" / "cand"
    arena.mkdir(parents=True)
    (tmp_path / "arena").chmod(0o700)           # the arena's parent is a valid scratch otherwise
    good = tmp_path / "good-scratch"
    good.mkdir(mode=0o700)
    marker = arena / "ran-marker"
    command = "touch ran-marker"
    other = tmp_path / "other-scratch"
    other.mkdir(mode=0o700)
    (other / "usercustomize.py").write_text("raise SystemExit('planted')")
    dotted = tmp_path / "dotted-scratch"
    dotted.mkdir(mode=0o700)
    (dotted / ".gitconfig").write_text("[alias]\n\tx = !touch planted\n")
    dot_dir = tmp_path / "dot-dir-scratch"
    (dot_dir / ".local" / "lib").mkdir(parents=True)
    dot_dir.chmod(0o700)
    unlistable = tmp_path / "unlistable-scratch"
    unlistable.mkdir(mode=0o700)
    loose = tmp_path / "loose-scratch"
    loose.mkdir()
    loose.chmod(0o750 if bad == "group-readable" else 0o707)
    given = {"relative": os.path.relpath(good), "missing": tmp_path / "nowhere",
             "a-file": tmp_path / "file", "a-symlink": tmp_path / "link",
             "inside-arena": arena / "inside", "holds-the-arena": tmp_path / "arena",
             "not-empty": other, "only-a-dotfile": dotted, "only-a-dot-directory": dot_dir,
             "unlistable": unlistable, "group-readable": loose, "world-writable": loose,
             "not-owned": good, "via-symlinked-parent": tmp_path / "arena-link" / "inside"}[bad]
    (tmp_path / "file").write_text("x")
    (tmp_path / "link").symlink_to(good)
    (tmp_path / "arena-link").symlink_to(arena)      # a symlinked parent that leads into the arena
    (arena / "inside").mkdir(mode=0o700)        # 0700 and empty: only its place is wrong
    with monkeypatch.context() as patched:
        if bad == "not-owned":
            patched.setattr(os, "geteuid", lambda: os.getuid() + 1)
        if bad == "unlistable":                 # root-proof: the listing itself fails
            real_scandir = os.scandir

            def scandir(target="."):
                if os.fspath(target) == str(unlistable):
                    raise PermissionError(13, "denied", str(target))
                return real_scandir(target)

            patched.setattr(os, "scandir", scandir)
        with pytest.raises(ValueError):
            runner.run_check(_check(command), _candidate(arena), run_id="r", iteration=1,
                             attempt=0, scratch=given)
    assert not marker.exists()
    receipt, _ = runner.run_check(_check(command), _candidate(arena), run_id="r", iteration=1,
                                  attempt=0, scratch=good)
    assert receipt.runner_ok and marker.exists()


def test_run_check_scratch_is_fresh_for_every_sandboxed_check(w, tmp_path):
    """M3. Catches the pin's per-check freshness lost under STRICT: the checks of
    one candidate under a sandbox each get an **empty** scratch (the pin's
    ``.hoh-scratch-<receipt>``), so a second sandboxed use of a workspace the
    first check wrote into is refused (``ValueError``, "own workspace") until it
    is emptied, and what the first check planted never reaches the second. **The
    unsandboxed regime is the same** (since the vet left the process's memory): a
    workspace an unsandboxed check wrote into is refused ("not empty") for the next
    unsandboxed check too -- one workspace per check, as the pin's ``.hoh-scratch-<receipt>``
    was -- and so are a *new* directory with a planted file and another directory at the
    path of a used one. Negative control: the first use of a new, empty workspace runs."""
    from hoh import runner
    from hoh.sandbox import Isolation, LaunchPlan

    class Recording:
        name = "recording"

        def __init__(self):
            self.scratch: list[Path] = []

        def unavailable(self):
            return None

        def plan(self, argv, spec):
            self.scratch.append(Path(spec.scratch))
            return LaunchPlan(argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"},
                              cwd=str(spec.candidate), proves_isolation=False)

    arena = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    arena.mkdir(parents=True)
    ws = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    backend = Recording()
    kw = dict(run_id="r1", iteration=1, attempt=0, isolation=Isolation.STRICT, sandbox=backend)
    runner.run_check(_check("true"), _candidate(arena), scratch=ws.path, **kw)
    assert backend.scratch == [ws.path]
    (ws.path / "planted.py").write_text("planted by the first check")
    with pytest.raises(ValueError, match="own workspace"):
        runner.run_check(_check("true"), _candidate(arena), scratch=ws.path, **kw)
    assert backend.scratch == [ws.path]                     # the second never reached a sandbox
    (ws.path / "planted.py").unlink()
    runner.run_check(_check("true"), _candidate(arena), scratch=ws.path, **kw)
    assert backend.scratch == [ws.path, ws.path]
    again = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    command = 'echo "first" > "$HOME/first"'
    runner.run_check(_check(command), _candidate(arena), run_id="r1", iteration=1, attempt=1,
                     scratch=again.path)
    assert (again.path / "first").read_text() == "first\n"
    with pytest.raises(ValueError, match="not empty"):   # nothing is vetted: a used workspace is
        runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1,   # refused
                         attempt=2, scratch=again.path)                                 # unsandboxed too
    with pytest.raises(ValueError, match="own workspace"):  # and for a sandbox, as before
        runner.run_check(_check("true"), _candidate(arena), scratch=again.path, **kw)
    assert backend.scratch == [ws.path, ws.path]
    swapped = again.path.with_name(again.path.name + "-swapped")
    again.path.rename(swapped)                       # the same path, another directory
    again.path.mkdir(mode=0o700)
    (again.path / "usercustomize.py").write_text("planted")
    with pytest.raises(ValueError, match="not empty"):
        runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1,
                         attempt=4, scratch=again.path)
    bystander = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1, attempt=5,
                     scratch=bystander.path)             # another directory is used meanwhile
    fresh = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    (fresh.path / "usercustomize.py").write_text("planted")
    with pytest.raises(ValueError, match="not empty"):
        runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1,
                         attempt=3, scratch=fresh.path)
    assert not [n for n in os.listdir(arena.parent) if n.startswith(".hoh-scratch-")]


def test_run_check_strict_hands_the_workspace_to_the_sandbox(w, tmp_path):
    """M4. Catches the sandboxed path ignoring the supplied scratch: with a
    recording backend the `SandboxSpec` the runner builds for STRICT names the
    workspace as its scratch, and no ``.hoh-scratch-*`` sibling is made. (The
    backend only records; the receipt is not a verdict.) Negative control:
    without ``scratch=`` the spec's scratch is the ``.hoh-scratch-<receipt>``
    sibling."""
    from hoh import runner
    from hoh.sandbox import Isolation, LaunchPlan

    class Recording:
        name = "recording"

        def __init__(self):
            self.scratch: list[Path] = []

        def unavailable(self):
            return None

        def plan(self, argv, spec):
            self.scratch.append(Path(spec.scratch))
            return LaunchPlan(argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"},
                              cwd=str(spec.candidate), proves_isolation=False)

    arena = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    arena.mkdir(parents=True)
    ws = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    backend = Recording()
    runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1, attempt=0,
                     isolation=Isolation.STRICT, sandbox=backend, scratch=ws.path)
    assert backend.scratch == [ws.path]
    assert not [n for n in os.listdir(arena.parent) if n.startswith(".hoh-scratch-")]
    runner.run_check(_check("true"), _candidate(arena), run_id="r2", iteration=1, attempt=0,
                     isolation=Isolation.STRICT, sandbox=backend)
    assert backend.scratch[1].name == ".hoh-scratch-r2-i1-a0-c1"


def test_a_refused_scratch_is_refused_again_until_it_is_emptied(w, tmp_path):
    """M5. Catches a guard that a retry walks around: a scratch directory with a
    planted file is refused (`ValueError`, "not empty") on the first call **and
    on every later call** -- the runner must not remember a directory it has
    refused as acceptable -- and the command never runs; once the file is gone the
    directory is accepted. Negative control: the empty directory runs."""
    from hoh import runner

    arena = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    arena.mkdir(parents=True)
    ws = roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")
    (ws.path / "usercustomize.py").write_text("planted")
    for attempt in range(3):
        with pytest.raises(ValueError, match="not empty"):
            runner.run_check(_check("touch ran-marker"), _candidate(arena), run_id="r1",
                             iteration=1, attempt=attempt, scratch=ws.path)
    assert not (arena / "ran-marker").exists()
    (ws.path / "usercustomize.py").unlink()
    receipt, _ = runner.run_check(_check("touch ran-marker"), _candidate(arena), run_id="r1",
                                  iteration=1, attempt=9, scratch=ws.path)
    assert receipt.runner_ok and (arena / "ran-marker").exists()


def test_a_scratch_used_by_a_sandboxed_check_is_not_trusted_by_an_unsandboxed_one(w, tmp_path):
    """M6. Catches sandboxed output reaching an unsandboxed check, **in whatever
    order the regimes alternate**: what a sandboxed check wrote into a workspace
    (here `.gitconfig`, `.local/...`: dotfiles, the very attack surface -- a
    `git x` alias or a `usercustomize.py` runs as the unsandboxed check's `$HOME`)
    is never accepted later by an unsandboxed check on the same ``scratch=``,
    which is refused ("not empty") and never runs. (1) sandboxed, then
    unsandboxed; (2) **unsandboxed, sandboxed,
    unsandboxed** -- the order in which a remembered vet once let it through;
    (3) the same where the directory is reached by **another name** (a symlinked
    parent: the directory is judged, not the name); (4) an unsandboxed use that
    wrote, then a sandboxed use that is **refused** (not empty), then an
    unsandboxed use: refused too -- a refused use changes nothing.
    Negative control: emptied, the unsandboxed check runs."""
    from hoh import runner
    from hoh.sandbox import Isolation, LaunchPlan

    class Recording:
        name = "recording"

        def unavailable(self):
            return None

        def plan(self, argv, spec):
            return LaunchPlan(argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"},
                              cwd=str(spec.candidate), proves_isolation=False)

    arena = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    arena.mkdir(parents=True)
    counter = iter(range(1, 100))

    def sandboxed(path):
        runner.run_check(_check("true"), _candidate(arena), run_id="r1", iteration=1,
                         attempt=next(counter), isolation=Isolation.STRICT,
                         sandbox=Recording(), scratch=path)

    def unsandboxed(path, marker):
        return runner.run_check(_check(f"touch {marker}"), _candidate(arena), run_id="r1",
                                iteration=1, attempt=next(counter), scratch=path)

    def plant(path):                                     # what a sandboxed check would have written
        (path / ".gitconfig").write_text("[alias]\n\tx = !echo ran-outside\n")
        (path / ".local" / "lib").mkdir(parents=True)

    def new_workspace():
        return roots(w).create(creator="runner.run_check", purpose="check", run_id="r1")

    # (1) sandboxed, then unsandboxed
    ws = new_workspace()
    sandboxed(ws.path)
    plant(ws.path)
    with pytest.raises(ValueError, match="not empty"):
        unsandboxed(ws.path, "ran-1")
    assert not (arena / "ran-1").exists()
    # (2) unsandboxed, sandboxed, unsandboxed
    ws = new_workspace()
    unsandboxed(ws.path, "ran-2a")
    assert (arena / "ran-2a").exists()
    sandboxed(ws.path)                                   # empty at the start: runs
    plant(ws.path)
    with pytest.raises(ValueError, match="not empty"):
        unsandboxed(ws.path, "ran-2b")
    assert not (arena / "ran-2b").exists()
    # (3) the same through another name of the same directory
    ws = new_workspace()
    alias = tmp_path / "alias"
    alias.symlink_to(ws.path.parent)
    unsandboxed(alias / ws.path.name, "ran-3a")          # by the real directory
    sandboxed(ws.path)
    plant(ws.path)
    with pytest.raises(ValueError, match="not empty"):
        unsandboxed(alias / ws.path.name, "ran-3b")
    assert not (arena / "ran-3b").exists()
    ws = new_workspace()
    unsandboxed(ws.path, "ran-3c")
    sandboxed(alias / ws.path.name)                      # the sandboxed use comes by the other name
    plant(ws.path)
    with pytest.raises(ValueError, match="not empty"):
        unsandboxed(ws.path, "ran-3d")
    assert not (arena / "ran-3d").exists()
    # (4) written by an unsandboxed check, a refused sandboxed use, then an unsandboxed one
    ws = new_workspace()
    unsandboxed(ws.path, "ran-4a")
    (ws.path / "kept-by-the-unsandboxed-check").write_text("x")
    with pytest.raises(ValueError, match="own workspace"):
        sandboxed(ws.path)
    with pytest.raises(ValueError, match="not empty"):
        unsandboxed(ws.path, "ran-4b")
    assert not (arena / "ran-4b").exists()
    # negative control: emptied, the unsandboxed check runs
    (ws.path / "kept-by-the-unsandboxed-check").unlink()
    receipt, _ = unsandboxed(ws.path, "ran-4c")
    assert receipt.runner_ok and (arena / "ran-4c").exists()
