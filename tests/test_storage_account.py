"""The storage account (I10b slice 1, plan row P5-40): `hoh.storage_account` and
`hoh storage account`.

The world is `test_storage_tiers.World`: a real registry with injected OS readers, a canonical
repository, owned scratch and receipts, every tree under pytest's `tmp_path`. Nothing here reads
the host's ``/tmp`` or ``~`` or a real state root, starts Herdr or an agent, or removes anything:
tests create, write and rename only. The seams the module names (`_lstat`, `_scandir`,
`HUNG_GRACE_S`, `read_record`, `clock`, `tier_of`, `sources`) are the only way a test makes a
file system call fail, block or take time.

Every test name carries the selector it is run by; every docstring names the failure (the
mutant) it catches.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import os
import stat
import threading
import time
from pathlib import Path

import pytest

import hoh.storage_account as sa
import hoh.storage_ephemeral as se
import hoh.storage_steward as steward
from hoh import cli
from hoh import storage_tiers as st
from test_storage_tiers import (  # noqa: F401  (isolated_env is an autouse fixture)
    OTHER,
    PROJECT,
    SETUP_PID,
    STEWARD_PID,
    World,
    isolated_env,
    run_state,
)

RUN_A = "20261001t0300z-held"
RUN_B = "20261001t0400z-other"
GHOST = "20261001t0500z-ghost"


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path / "w")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def account(world: World, **kw) -> dict:
    kw.setdefault("tiers", False)
    return sa.take_account(world.registry, PROJECT, **kw)


def item_of(acc: dict, path) -> dict:
    found = [i for i in acc["items"] if i["path"] == str(path)]
    assert len(found) == 1, f"{path}: {[i['path'] for i in acc['items']]}"
    return found[0]


def measure(path: Path) -> tuple[int, int, int]:
    """An independent measure: bytes (regular files), allocated bytes, inodes."""
    seen: set = set()
    totals = [0, 0, 0]

    def one(p: str) -> None:
        info = os.lstat(p)
        key = (info.st_dev, info.st_ino)
        if key in seen:
            return
        seen.add(key)
        totals[2] += 1
        totals[1] += info.st_blocks * 512
        if stat.S_ISREG(info.st_mode):
            totals[0] += info.st_size

    one(str(path))
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            one(os.path.join(dirpath, name))
    return totals[0], totals[1], totals[2]


def snapshot(*roots: Path) -> dict:
    out = {}
    for root in roots:
        paths = [str(root)]
        for dirpath, dirnames, filenames in os.walk(root):
            paths += [os.path.join(dirpath, n) for n in dirnames + filenames]
        for p in paths:
            info = os.lstat(p)
            out[p] = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino, info.st_nlink)
    return out


class Ticker:
    """A clock that advances by `step` at every reading."""

    def __init__(self, step: float = 1.0) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class Recorder:
    """Wraps a module seam and records the paths it is called with."""

    def __init__(self, real) -> None:
        self.real, self.paths = real, []

    def __call__(self, path, *args):
        self.paths.append(str(path))
        return self.real(path, *args)


def under(paths, root) -> list[str]:
    root = str(root)
    return [p for p in paths if p == root or p.startswith(root + os.sep)]


def blocking(real, gate: threading.Event, match):
    """A seam that blocks (up to 30 s, or until `gate` is set) for the paths `match` names."""
    def wrapper(path, *args):
        if match(str(path)):
            gate.wait(30)
        return real(path, *args)
    return wrapper


def failing(real, match, exc):
    def wrapper(path, *args):
        if match(str(path)):
            raise exc
        return real(path, *args)
    return wrapper


def admit(world: World, *parents: Path) -> None:
    """The operator act: list admissible parents for external roots in CONFIG.json."""
    (world.registry.state_root / "CONFIG.json").write_text(json.dumps({
        "schema": "veriharness.storage-config/1", "sequence": 1,
        "admissible_external_parents": [str(p) for p in parents]}), encoding="utf-8")


def make_runs_base(world: World, parents: tuple[Path, ...] = ()) -> Path:
    parent = (world.tmp / "runs-parent").resolve()
    parent.mkdir(exist_ok=True)
    admit(world, parent, *parents)
    entry = world.registry.create_external_root(PROJECT, parent / "runs", "runs_base")
    world._runs = Path(entry["path"])
    return world._runs


def workspace(world: World, *, run_id=None, purpose="check") -> se.Workspace:
    roots = se.EphemeralRoots(world.registry, PROJECT, "s1", getpid=lambda: SETUP_PID)
    return roots.create(creator="test", purpose=purpose, run_id=run_id)


def tally_sums(acc: dict) -> None:
    """Every grouping sums to the total (items, bytes, allocated bytes, entries)."""
    total = acc["total"]
    for group in ("by_tier", "by_kind", "by_session", "by_run", "by_purpose"):
        rows = acc[group].values()
        assert sum(r["items"] for r in rows) == total["items"], group
        for key in ("bytes", "allocated_bytes", "entries"):
            if total[key] == sa.UNMEASURED:
                assert sum(r[f"{key}_lower_bound"] if r[key] == sa.UNMEASURED else r[key]
                           for r in rows) == total[f"{key}_lower_bound"], (group, key)
            else:
                assert sum(r[key] for r in rows) == total[key], (group, key)


# --------------------------------------------------------------------------- #
# R1 -- bytes, entries, each inode once, ownership per entry, fail closed
# --------------------------------------------------------------------------- #


def test_account_counts_bytes_and_entries_of_an_owned_item(w):
    """Catches a count that differs from an independent measure (M15: `stat` for
    `lstat`; entries by name; the item directory forgotten; a symlink followed):
    bytes, allocated bytes and inodes of an owned item equal the independent
    measure, the item directory and the symlink are entries of their own (the
    symlink of size 0), the registry's own record is bookkeeping, the account
    proper is owned + bookkeeping."""
    item = w.scratch / "proj"
    (item / "sub" / "deep").mkdir(parents=True)
    (item / "empty").mkdir()
    (item / "a.txt").write_text("alpha\n", encoding="utf-8")
    (item / "sub" / "b.bin").write_bytes(b"x" * 5000)
    (item / "sub" / "deep" / "c").write_text("c", encoding="utf-8")
    (item / "link").symlink_to("a.txt")
    (item / "dirlink").symlink_to("sub")
    acc = account(w)
    one = item_of(acc, item)
    assert one["status"] == "MEASURED" and one["reason"] is None
    assert (one["bytes"], one["allocated_bytes"], one["entries"]) == measure(item)
    assert one["entries"] == 9 and one["bytes"] == 6 + 5000 + 1
    assert one["unproven"] == {"entries": 0, "bytes": 0, "not_descended": 0,
                               "other_names_of_counted_inodes": 0, "samples": []}
    assert one["vanished"] == 0
    record = item_of(acc, w.scratch / ".vh-owner.json")
    assert record["entries"] == 1 and record["bytes"] == os.lstat(
        w.scratch / ".vh-owner.json").st_size
    assert acc["owned"]["entries"] == 9
    assert acc["bookkeeping"]["entries"] == 2          # one record per registered root
    assert acc["total"]["entries"] == 11 == sum(i["entries"] for i in acc["items"])
    assert acc["total"]["bytes"] == acc["owned"]["bytes"] + acc["bookkeeping"]["bytes"]
    assert acc["total"]["allocated_bytes"] == sum(i["allocated_bytes"] for i in acc["items"])
    assert acc["schema"] == sa.SCHEMA == "veriharness.storage-account/1"
    assert acc["complete"] is True and acc["scope_complete"] is True and acc["problems"] == []
    assert acc["unproven"]["entries"] == 0
    # the keys of the account and of an item are the specified ones
    assert set(acc) == {
        "schema", "project_id", "state_root", "taken_utc", "budget_s", "elapsed_s", "complete",
        "scope_complete", "problems", "tiers", "total", "owned", "bookkeeping", "unproven",
        "by_tier", "reclaimable_now", "by_kind", "by_session", "by_run", "by_purpose",
        "not_accounted_roots", "items"}
    assert set(one) == {
        "path", "root_kind", "session_id", "run_id", "run_id_basis", "purpose", "workspace_id",
        "label_status", "status", "reason", "tier", "tier_reason", "eligible", "candidate",
        "bytes", "allocated_bytes", "entries", "unproven", "vanished"}
    json.dumps(acc)


def test_hard_link_is_counted_once_and_is_not_owned(w):
    """Catches the inode dedupe off (M1): a regular file with several names is
    one `unproven` entry whose size counts once (the registry never calls it
    OWNED), its other names add nothing (`other_names_of_counted_inodes`, also
    from a later item), hard-linked symlinks are one owned entry. Negative
    control: a plain file next to them is owned."""
    first, second = w.scratch / "a-item", w.scratch / "b-item"
    first.mkdir()
    second.mkdir()
    (first / "f1").write_text("hello world", encoding="utf-8")
    os.link(first / "f1", first / "f2")
    os.link(first / "f1", second / "g")
    (first / "plain").write_text("12345", encoding="utf-8")
    (first / "l1").symlink_to("plain")
    os.link(first / "l1", first / "l2", follow_symlinks=False)
    acc = account(w)
    one, two = item_of(acc, first), item_of(acc, second)
    assert one["unproven"]["entries"] == 1 and one["unproven"]["bytes"] == len("hello world")
    assert one["unproven"]["other_names_of_counted_inodes"] == 1          # f2
    sample = one["unproven"]["samples"]
    assert [s["rel"] for s in sample] == ["f1"] and "HARDLINKED" in sample[0]["reason"]
    assert sample[0]["status"] == "UNKNOWN"
    # the item directory, the plain file and the symlink (once, l1 before l2)
    assert one["entries"] == 3 and one["bytes"] == 5
    assert two["entries"] == 1 and two["bytes"] == 0
    assert two["unproven"]["entries"] == 0
    assert two["unproven"]["other_names_of_counted_inodes"] == 1          # g, counted in a-item
    assert acc["unproven"]["entries"] == 1
    assert acc["unproven"]["other_names_of_counted_inodes"] == 2
    assert acc["unproven"]["bytes"] == len("hello world")


def test_account_does_not_enter_a_directory_the_registry_does_not_own(w, monkeypatch, tmp_path):
    """Catches a directory entered although the registry does not own it (M2): a
    directory on another mount below an item and a symlink to a foreign
    directory are one `unproven` entry each, and `scandir` is never called on
    them (the recorder), nor `lstat` on what lies below. Negative control: the
    ordinary subdirectory beside them is entered."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "secret.txt").write_text("not ours", encoding="utf-8")
    item = w.scratch / "mixed"
    (item / "othermount" / "inner").mkdir(parents=True)
    (item / "othermount" / "inner" / "f").write_text("f", encoding="utf-8")
    (item / "fine").mkdir()
    (item / "fine" / "g").write_text("gg", encoding="utf-8")
    (item / "out").symlink_to(foreign)
    monkeypatch.setattr(w.registry, "_mount_id_reader",
                        lambda p: 2 if "othermount" in Path(p).parts else 1)
    scans, stats = Recorder(sa._scandir), Recorder(sa._lstat)
    monkeypatch.setattr(sa, "_scandir", scans)
    monkeypatch.setattr(sa, "_lstat", stats)
    acc = account(w)
    one = item_of(acc, item)
    assert one["status"] == "MEASURED"
    assert one["entries"] == 3                         # mixed, fine, fine/g
    assert one["unproven"]["entries"] == 2
    assert one["unproven"]["not_descended"] == 1       # the mount; the symlink is no directory
    assert {s["rel"] for s in one["unproven"]["samples"]} == {"othermount", "out"}
    assert not under(scans.paths, item / "othermount")
    assert not under(stats.paths, item / "othermount" / "inner")
    assert not under(scans.paths, foreign) and not under(stats.paths, foreign)
    assert under(scans.paths, item / "fine")


def test_an_entry_that_vanishes_during_the_walk_is_skipped_not_unmeasured(w, monkeypatch):
    """Catches a vanished entry read as an unreadable one (M16): a name that is
    gone between the listing and its `lstat` is skipped and counted in
    `vanished`, the item stays measured. Negative control: any other OSError on
    an `lstat` makes the item UNMEASURED (UNREADABLE)."""
    item = w.scratch / "racy"
    item.mkdir()
    (item / "kept").write_text("k", encoding="utf-8")
    (item / "gone").write_text("g", encoding="utf-8")
    (item / "denied").write_text("d", encoding="utf-8")
    monkeypatch.setattr(sa, "_lstat", failing(
        sa._lstat, lambda p: p.endswith("/gone"), FileNotFoundError("gone")))
    one = item_of(account(w), item)
    assert one["status"] == "MEASURED" and one["vanished"] == 1
    assert one["entries"] == 3 and one["bytes"] == 2          # racy, kept, denied
    monkeypatch.setattr(sa, "_lstat", failing(
        sa._lstat, lambda p: p.endswith("/denied"), PermissionError("denied")))
    other = item_of(account(w), item)
    assert other["status"] == "UNMEASURED" and other["reason"] == "UNREADABLE"
    assert other["entries"] == sa.UNMEASURED


def test_an_owner_of_that_raises_gives_an_unproven_entry(w, monkeypatch):
    """Catches an `owner_of` that raises read as owned (M22): the entry is one
    unproven entry (with the exception in its sample), never an owned one, and
    the walk goes on with its siblings. Negative control: the sibling is
    owned."""
    item = w.scratch / "boomy"
    item.mkdir()
    (item / "boom").write_text("123", encoding="utf-8")
    (item / "calm").write_text("45", encoding="utf-8")
    real = w.registry.owner_of

    def owner_of(path):
        if str(path).endswith("/boom"):
            raise RuntimeError("registry fell over")
        return real(path)

    monkeypatch.setattr(w.registry, "owner_of", owner_of)
    acc = account(w)
    one = item_of(acc, item)
    assert one["status"] == "MEASURED"
    assert one["entries"] == 2 and one["bytes"] == 2          # boomy, calm
    assert one["unproven"]["entries"] == 1 and one["unproven"]["bytes"] == 3
    (sample,) = one["unproven"]["samples"]
    assert sample["rel"] == "boom" and "owner_of raised RuntimeError" in sample["reason"]
    assert acc["owned"]["entries"] == 2


def test_a_protected_directory_is_bookkeeping_and_is_not_entered(w, monkeypatch):
    """Catches a PROTECTED directory entered (M23) or summed as owned (M3): a
    directory the registry answers PROTECTED is one bookkeeping entry; nothing
    below it is listed, read or counted. Negative control: the file beside it
    is owned."""
    item = w.scratch / "guarded"
    (item / "prot" / "below").mkdir(parents=True)
    (item / "prot" / "below" / "f").write_text("f", encoding="utf-8")
    (item / "file").write_text("12", encoding="utf-8")
    real = w.registry.owner_of

    def owner_of(path):
        answer = real(path)
        if str(path).endswith("/prot"):
            return dataclasses.replace(answer, status="PROTECTED", reason="a registry record")
        return answer

    monkeypatch.setattr(w.registry, "owner_of", owner_of)
    scans = Recorder(sa._scandir)
    monkeypatch.setattr(sa, "_scandir", scans)
    acc = account(w)
    one = item_of(acc, item)
    assert one["entries"] == 3 and one["bytes"] == 2          # guarded, prot, file
    assert one["unproven"]["entries"] == 0 and one["unproven"]["not_descended"] == 1
    assert not under(scans.paths, item / "prot")
    # guarded + file are owned; prot and the two roots' records are bookkeeping
    assert acc["owned"]["entries"] == 2 and acc["bookkeeping"]["entries"] == 3


# --------------------------------------------------------------------------- #
# R2 -- UNMEASURED and the bound, on every read
# --------------------------------------------------------------------------- #


def test_unreadable_item_is_unmeasured_never_zero(w, monkeypatch):
    """Catches an unreadable item read as 0 (and an aggregate that drops the
    marker, M4): an unreadable subdirectory makes its item `UNMEASURED` with a
    lower bound (what was seen), the item beside it is a number, and every
    aggregate that contains the unreadable one says `UNMEASURED` (with
    `*_lower_bound` and `unmeasured_items`) while the others stay numbers; the
    account is not complete."""
    locked, fine = w.scratch / "locked", w.scratch / "fine"
    (locked / "sub").mkdir(parents=True)
    (locked / "sub" / "f.txt").write_text("hidden", encoding="utf-8")
    fine.mkdir()
    (fine / "ok.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p.endswith("/locked/sub"), PermissionError("denied")))
    acc = account(w)
    bad, good = item_of(acc, locked), item_of(acc, fine)
    assert bad["status"] == "UNMEASURED" and bad["reason"] == "UNREADABLE"
    assert (bad["bytes"], bad["allocated_bytes"], bad["entries"]) == (sa.UNMEASURED,) * 3
    assert bad["lower_bound"]["entries"] == 2 and "unproven" not in bad
    assert good["status"] == "MEASURED" and good["entries"] == 2 and good["bytes"] == 2
    for tally in (acc["total"], acc["owned"], acc["by_kind"]["scratch"],
                  acc["by_session"]["s1"], acc["by_tier"]["NOT_RUN"]):
        assert tally["bytes"] == tally["entries"] == tally["allocated_bytes"] == sa.UNMEASURED
        assert tally["unmeasured_items"] == 1
        assert tally["entries_lower_bound"] >= 4
    assert acc["bookkeeping"]["bytes"] == sa.UNMEASURED            # what was not read may hold records
    assert acc["by_kind"]["receipts"]["entries"] == 1
    assert acc["complete"] is False and acc["scope_complete"] is True
    tally_sums(acc)


def test_time_budget_stops_the_walk_and_says_so(w):
    """Catches a clock read once per directory instead of per entry (M5, M12):
    with a clock that advances one second per reading and a budget of 40 s a flat
    directory of 200 names is stopped mid-walk (UNMEASURED, BUDGET, a lower
    bound that is neither 0 nor the whole), and the items after it are not
    reached (UNMEASURED, BUDGET, lower bound 0). The account returns, is not
    complete and is no verdict."""
    flat = w.scratch / "b-flat"
    flat.mkdir()
    for n in range(200):
        (flat / f"f{n:03d}").write_text("x", encoding="utf-8")
    for name in ("a-first", "c-after", "d-after"):
        (w.scratch / name).mkdir()
    acc = account(w, budget_s=40.0, clock=Ticker(1.0))
    assert item_of(acc, w.scratch / "a-first")["status"] == "MEASURED"
    cut = item_of(acc, flat)
    assert cut["status"] == "UNMEASURED" and cut["reason"] == "BUDGET"
    assert 0 < cut["lower_bound"]["entries"] < 201 and cut["entries"] == sa.UNMEASURED
    for name in ("c-after", "d-after"):
        late = item_of(acc, w.scratch / name)
        assert (late["status"], late["reason"]) == ("UNMEASURED", "BUDGET")
        assert late["lower_bound"] == {"bytes": 0, "allocated_bytes": 0, "entries": 0}
    assert acc["complete"] is False and acc["total"]["entries"] == sa.UNMEASURED
    assert acc["total"]["unmeasured_items"] == 3 and acc["budget_s"] == 40.0
    assert isinstance(acc["elapsed_s"], float)


def test_hung_file_system_call_is_abandoned_at_the_budget(w, monkeypatch):
    """Catches a blocked `lstat` waited for (M6): the call is abandoned at budget
    plus grace, the item is UNMEASURED (BUDGET), the items after it are not
    reached, the account comes back (well before the gate's own time-out) and is
    not complete. Negative control: the item before it is a number."""
    for name in ("a-ok", "b-hang", "c-late"):
        (w.scratch / name).mkdir()
    gate = threading.Event()
    monkeypatch.setattr(sa, "_lstat", blocking(sa._lstat, gate, lambda p: p.endswith("/b-hang")))
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    started = time.monotonic()
    try:
        acc = account(w, budget_s=0.5)
    finally:
        gate.set()
    assert time.monotonic() - started < 10
    assert item_of(acc, w.scratch / "a-ok")["status"] == "MEASURED"
    for name in ("b-hang", "c-late"):
        late = item_of(acc, w.scratch / name)
        assert (late["status"], late["reason"]) == ("UNMEASURED", "BUDGET")
    assert acc["complete"] is False


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, 3601, True, "5"])
def test_budget_outside_the_range_is_refused(w, bad):
    """Catches a budget accepted that is not a finite number in (0, 3600]: zero,
    negative, NaN, infinity, over an hour, a bool and a string are refused
    with `ValueError` before anything is read."""
    with pytest.raises(ValueError):
        account(w, budget_s=bad)
    assert account(w, budget_s=3600)["budget_s"] == 3600.0         # the edge is allowed
    assert account(w, budget_s=1)["budget_s"] == 1.0                # an int is a number


def test_a_hung_root_listing_is_abandoned_at_the_budget(w, monkeypatch):
    """Catches the listing of a root run outside the bound (M17): a root whose
    `scandir` blocks is abandoned at budget plus grace and is one UNMEASURED
    pseudo-item (BUDGET); the root listed before it is measured; the account
    comes back and is not complete."""
    (w.scratch / "x").mkdir()
    gate = threading.Event()
    root = str(w.scratch)
    monkeypatch.setattr(sa, "_scandir", blocking(sa._scandir, gate, lambda p: p == root))
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    started = time.monotonic()
    try:
        acc = account(w, budget_s=0.5)
    finally:
        gate.set()
    assert time.monotonic() - started < 10
    pseudo = item_of(acc, w.scratch)
    assert (pseudo["root_kind"], pseudo["status"], pseudo["reason"]) == (
        "scratch", "UNMEASURED", "BUDGET")
    assert item_of(acc, w.receipts / ".vh-owner.json")["status"] == "MEASURED"
    assert not [i for i in acc["items"] if i["path"] == str(w.scratch / "x")]
    assert acc["complete"] is False


class HungSources:
    """Index reads that block on a gate (and an optional failing one)."""

    def __init__(self, gate=None, error=None) -> None:
        self.gate, self.error = gate, error

    def scratch_dirs(self, *args):
        if self.gate is not None:
            self.gate.wait(30)
        if self.error is not None:
            raise self.error
        return []

    def runs_bases(self):
        return []

    def legacy_roots(self):
        return []


def test_a_hung_index_read_is_abandoned_and_stated(w, monkeypatch):
    """Catches the registry's index reads run outside the bound (M18): an index
    read that blocks is abandoned at budget plus grace, the account has no items,
    says so in `problems`, is neither complete nor scope-complete and comes
    back."""
    gate = threading.Event()
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    started = time.monotonic()
    try:
        acc = account(w, budget_s=0.5, sources=HungSources(gate))
    finally:
        gate.set()
    assert time.monotonic() - started < 10
    assert acc["items"] == [] and len(acc["problems"]) == 1
    assert "the roots could not be listed" in acc["problems"][0]
    assert acc["complete"] is False and acc["scope_complete"] is False


def test_labels_are_not_read_after_the_budget_is_spent(w, monkeypatch):
    """Catches labels read after the budget is spent (M19): once the clock is past
    the deadline neither a `_valid_run_dir` call nor a record read is made, the
    label status of the items is NOT_REACHED and the account is not complete.
    Negative control: with time left, the same world gets its run labels."""
    make_runs_base(w)
    w.run(RUN_A, files={"out/x.txt": "x\n"})
    w.run(RUN_B)
    ws = workspace(w, run_id=RUN_A)
    clock = {"now": 0.0}
    calls, reads = [], []
    real_valid, real_read = w.registry._valid_run_dir, sa.read_record
    monkeypatch.setattr(w.registry, "_valid_run_dir",
                        lambda child: (calls.append(clock["now"]), real_valid(child))[1])
    monkeypatch.setattr(sa, "read_record",
                        lambda path: (reads.append(clock["now"]), real_read(path))[1])
    trigger = {"path": None}
    real_scandir = sa._scandir

    def jumping(path):
        listing = real_scandir(path)
        if str(path) == trigger["path"]:            # the clock jumps right after this listing
            clock["now"] = 100.0
        return listing

    monkeypatch.setattr(sa, "_scandir", jumping)

    def take() -> dict:
        clock["now"] = 0.0
        calls.clear()
        reads.clear()
        return account(w, budget_s=10.0, clock=lambda: clock["now"])

    acc = take()                                     # the control: time left
    assert acc["complete"] is True and calls and reads
    assert item_of(acc, w.runs_base() / RUN_A)["run_id"] == RUN_A
    assert item_of(acc, ws.path)["label_status"] == "OK"
    trigger["path"] = str(w.runs_base())
    acc = take()
    assert calls == []
    runs = [i for i in acc["items"] if i["root_kind"] == "runs_base"]
    assert runs and all(i["label_status"] == "NOT_REACHED" and i["run_id"] is None
                        and i["reason"] == "BUDGET" for i in runs)
    assert acc["complete"] is False
    trigger["path"] = str(ws.path.parent)            # the tmp root: its items are listed, not read
    acc = take()
    assert [r for r in reads if r >= 10.0] == [] and calls
    one = item_of(acc, ws.path)
    assert one["label_status"] == "NOT_REACHED" and one["run_id"] is None
    assert one["reason"] == "BUDGET"
    assert acc["complete"] is False


def test_a_hung_label_read_is_abandoned(w, monkeypatch):
    """Catches the label reads run outside the bound (M20): a record read that
    blocks is abandoned at budget plus grace; the item keeps its figures, has
    label status TIMEOUT and no labels, and the account is not complete. A
    record read that raises is label status ERROR."""
    ws = workspace(w, run_id=RUN_A)
    gate = threading.Event()
    real = sa.read_record
    monkeypatch.setattr(sa, "read_record", lambda path: (
        gate.wait(30) if str(path) == str(ws.path) else None, real(path))[1])
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    started = time.monotonic()
    try:
        acc = account(w, budget_s=0.5)
    finally:
        gate.set()
    assert time.monotonic() - started < 10
    one = item_of(acc, ws.path)
    assert one["label_status"] == "TIMEOUT" and one["status"] == "MEASURED"
    assert one["entries"] == 1 and one["run_id"] is None and one["purpose"] is None
    assert acc["complete"] is False

    def broken(path):
        raise RuntimeError("record unreadable")

    monkeypatch.setattr(sa, "read_record", broken)
    one = item_of(account(w), ws.path)
    assert one["label_status"] == "ERROR" and one["entries"] == 1


# --------------------------------------------------------------------------- #
# R3 -- the namespace, read-only, scope
# --------------------------------------------------------------------------- #


def test_account_reads_only_the_projects_namespace(w, monkeypatch, tmp_path):
    """Catches a read outside the project's registered roots: a foreign
    directory, another registered project's scratch and a stray beside the state
    root are never listed, opened, `lstat`ed or asked of the registry; every
    item lies in a root of the project."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "f").write_text("f", encoding="utf-8")
    stray = w.registry.state_root.parent / "stray"
    stray.mkdir()
    (stray / "g").write_text("g", encoding="utf-8")
    other_canon = tmp_path / "other-canon"
    other_canon.mkdir()
    w.registry.register_project(OTHER, other_canon)
    w.registry.open_session(OTHER, "ox", pid=SETUP_PID, boot_id="boot-d")
    other = w.registry.scratch_dir(OTHER, "ox", "scratch")
    (other / "theirs").mkdir()
    (other / "theirs" / "t").write_text("t", encoding="utf-8")
    (w.scratch / "mine").mkdir()
    scans, stats, owners = Recorder(sa._scandir), Recorder(sa._lstat), []
    real = w.registry.owner_of
    monkeypatch.setattr(sa, "_scandir", scans)
    monkeypatch.setattr(sa, "_lstat", stats)
    monkeypatch.setattr(w.registry, "owner_of", lambda p: (owners.append(str(p)), real(p))[1])
    acc = account(w)
    for outside in (foreign, stray, other):
        for seen in (scans.paths, stats.paths, owners):
            assert not under(seen, outside)
    mine = {str(w.scratch), str(w.receipts)}
    assert acc["items"] and all(Path(i["path"]).parent in map(Path, mine)
                                for i in acc["items"])
    assert item_of(acc, w.scratch / "mine")["status"] == "MEASURED"
    with pytest.raises(ValueError):
        sa.take_account(w.registry, "nobody", tiers=False)


def test_account_writes_nothing_and_takes_no_lease(w, monkeypatch):
    """Catches a write, a session or a lease (M10): after an account with tiers
    on, every file and directory of the world has the same size, mtime and
    inode, no session was opened, no scratch directory created, no external
    root created."""
    (w.scratch / "dup").mkdir()
    w.copy("dup2")
    (w.scratch / "dup" / "f").write_text("f", encoding="utf-8")
    w.end_setup()
    sessions = sorted(os.listdir(w.registry._project_dir(PROJECT) / "sessions"))
    before = snapshot(w.tmp)

    def forbidden(*args, **kw):
        raise AssertionError("the account must not change the registry")

    for name in ("open_session", "close_session", "scratch_dir", "create_external_root",
                 "register_project"):
        monkeypatch.setattr(w.registry, name, forbidden)
    acc = sa.take_account(w.registry, PROJECT, tiers=True, sources=w.sources())
    assert acc["tiers"]["run"] is True and acc["items"]
    assert snapshot(w.tmp) == before
    assert sorted(os.listdir(w.registry._project_dir(PROJECT) / "sessions")) == sessions
    source = Path(sa.__file__).read_text(encoding="utf-8")
    for word in ("unlink", "rmdir", "rmtree", "os.remove", "os.rename", "write_text",
                 "write_bytes", "os.mkdir", "makedirs", "open_session", "reserve("):
        assert word not in source.replace("What this is not", ""), word


def test_not_accounted_roots_are_named_not_hidden(w, tmp_path):
    """Catches the external roots silently skipped (M11) and `scope_complete`
    blind to them (M25): a registered `target_clone` is named under
    `not_accounted_roots` (reason LATER_SLICE), `scope_complete` is false while
    `complete` stays true. Negative control: without a clone the list is empty
    and `scope_complete` is true."""
    clean = account(w)
    assert clean["not_accounted_roots"] == [] and clean["scope_complete"] is True
    parent = (tmp_path / "ext").resolve()
    parent.mkdir()
    admit(w, parent)
    entry = w.registry.create_external_root(PROJECT, parent / "clone", "target_clone")
    acc = account(w)
    assert acc["not_accounted_roots"] == [
        {"kind": "target_clone", "path": entry["path"], "reason": "LATER_SLICE"}]
    assert acc["scope_complete"] is False and acc["complete"] is True
    assert not [i for i in acc["items"] if i["path"].startswith(entry["path"])]


def test_unlistable_roots_and_unreadable_indexes_are_stated_not_empty(w, monkeypatch):
    """Catches `problems` that do not make the account incomplete (M24) and a
    root that cannot be listed read as empty: an index read that raises gives
    `problems` and no items; a root whose listing raises is one UNMEASURED
    pseudo-item (UNREADABLE) and the other root stays measured."""
    acc = account(w, sources=HungSources(error=OSError("index unreadable")))
    assert acc["items"] == [] and "index unreadable" in acc["problems"][0]
    assert acc["complete"] is False and acc["scope_complete"] is False
    root = str(w.scratch)
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p == root, PermissionError("denied")))
    (w.scratch / "x").mkdir()
    acc = account(w)
    pseudo = item_of(acc, w.scratch)
    assert (pseudo["status"], pseudo["reason"]) == ("UNMEASURED", "UNREADABLE")
    assert pseudo["entries"] == sa.UNMEASURED
    assert item_of(acc, w.receipts / ".vh-owner.json")["status"] == "MEASURED"
    assert acc["complete"] is False and acc["problems"] == []
    assert acc["total"]["entries"] == sa.UNMEASURED


# --------------------------------------------------------------------------- #
# R4 -- tiers and reclaimable now
# --------------------------------------------------------------------------- #


def test_tiers_are_the_tier_modules_and_reclaimable_is_the_eligible_sum(w, tmp_path):
    """Catches a tier the account invented or an eligibility forced on (M7):
    each item's tier equals `classify_tier`'s on the same sources -- a verified
    duplicate is eligible, a plain directory is not, a hard-linked one is
    UNTIERED with its reason -- and `reclaimable_now` is the eligible sum, with
    `basis: GROSS`."""
    dup = w.copy("dup")
    plain = w.scratch / "plain"
    plain.mkdir()
    (plain / "n.txt").write_text("not a duplicate\n", encoding="utf-8")
    linked = w.copy("linked")
    foreign = tmp_path / "foreign-a.txt"
    foreign.write_text("alpha\n", encoding="utf-8")
    (linked / "a.txt").rename(tmp_path / "moved-a.txt")
    os.link(foreign, linked / "a.txt")
    w.end_setup()
    sources = w.sources()
    acc = sa.take_account(w.registry, PROJECT, tiers=True, sources=sources)
    assert acc["tiers"]["run"] is True
    for path in (dup, plain, linked, w.scratch / ".vh-owner.json"):
        decision = w.classify(path)
        one = item_of(acc, path)
        assert one["tier"] == (decision.tier or sa.UNTIERED), path
        assert one["eligible"] is decision.eligible, path
    assert item_of(acc, dup)["tier"] == st.VERIFIED_DUPLICATE and item_of(acc, dup)["eligible"]
    assert item_of(acc, plain)["tier"] == st.COLD and not item_of(acc, plain)["eligible"]
    untiered = item_of(acc, linked)
    assert untiered["tier"] == sa.UNTIERED and "UNKNOWN_OWNERSHIP" in untiered["tier_reason"]
    assert untiered["eligible"] is False
    reclaim = acc["reclaimable_now"]
    assert reclaim["basis"] == "GROSS" and reclaim["items"] == 1
    assert reclaim["bytes"] == item_of(acc, dup)["bytes"] and reclaim["lower_bound"] is False
    assert reclaim["entries"] == item_of(acc, dup)["entries"]
    assert acc["by_tier"][st.VERIFIED_DUPLICATE]["items"] == 1
    tally_sums(acc)


def test_a_tier_that_cannot_be_classified_is_unmeasured(w, monkeypatch):
    """Catches a guessed tier (M13, M35): a classification that raises or hangs
    is tier UNMEASURED (never a guess), `complete` is false although every walk
    finished, and `reclaimable_now` is a lower bound. `tiers=False` reads
    NOT_RUN, not 0, and is complete again."""
    w.copy("dup")
    (w.scratch / "bad").mkdir()
    (w.scratch / "hang").mkdir()
    w.end_setup()
    gate = threading.Event()
    real = w.classify

    def tier_of(path):
        if path.endswith("/bad"):
            raise RuntimeError("classifier fell over")
        if path.endswith("/hang"):
            gate.wait(30)
        return real(path)

    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    try:
        acc = account(w, tiers=True, tier_of=tier_of, budget_s=1.0)
    finally:
        gate.set()
    assert item_of(acc, w.scratch / "bad")["tier"] == sa.UNMEASURED
    assert "RuntimeError" in item_of(acc, w.scratch / "bad")["tier_reason"]
    assert item_of(acc, w.scratch / "hang")["tier"] == sa.UNMEASURED
    assert item_of(acc, w.scratch / "dup")["tier"] == st.VERIFIED_DUPLICATE
    assert acc["total"]["unmeasured_items"] == 0 and acc["complete"] is False
    assert acc["reclaimable_now"]["lower_bound"] is True
    assert acc["reclaimable_now"]["tier_unmeasured_items"] >= 2
    off = account(w, tiers=False)
    assert item_of(off, w.scratch / "dup")["tier"] == sa.NOT_RUN and off["complete"] is True
    assert off["tiers"]["run"] is False
    reclaim = off["reclaimable_now"]
    assert reclaim["basis"] == "GROSS"
    assert reclaim["bytes"] == reclaim["entries"] == reclaim["items"] == sa.NOT_RUN


def test_groups_by_run_session_kind_and_purpose(w):
    """Catches a grouping that does not sum to the total or a wrong key: runs of
    a runs base, a tmp workspace and plain scratch group by tier, kind, session,
    run and purpose; every grouping sums to the total; the runs base's session
    key is `-` (M34)."""
    make_runs_base(w)
    w.run(RUN_A, files={"out/x.txt": "x\n"})
    w.run(RUN_B)
    ws = workspace(w, run_id=RUN_A, purpose="test")
    (ws.path / "f").write_text("12345", encoding="utf-8")
    (w.scratch / "plain").mkdir()
    w.end_setup()
    acc = sa.take_account(w.registry, PROJECT, tiers=True, sources=w.sources())
    tally_sums(acc)
    assert set(acc["by_kind"]) == {"runs_base", "scratch", "receipts", "tmp"}
    assert set(acc["by_session"]) == {"-", "s1", "steward"}
    assert acc["by_session"]["-"]["items"] == acc["by_kind"]["runs_base"]["items"] == 2
    assert acc["by_purpose"]["test"]["items"] == 1 and acc["by_purpose"]["test"]["bytes"] == 5
    assert acc["by_run"][RUN_A]["items"] == 2          # the run directory and the workspace
    assert acc["by_run"][RUN_B]["items"] == 1
    assert acc["by_run"]["-"]["items"] == acc["total"]["items"] - 3
    assert acc["by_purpose"]["-"]["items"] == acc["total"]["items"] - 1
    assert item_of(acc, ws.path)["workspace_id"] == ws.workspace_id


def test_reclaimable_now_is_the_stewards_reach_and_says_gross(w):
    """Catches `reclaimable_now` wider than the steward's reach (M21, M32): a
    verified duplicate under `receipts`, one under a hidden name and one under a
    quarantine name are eligible (the tier module's word) but not candidates and
    not counted; a plain one directly in `scratch` is. The data says
    `basis: GROSS`. The candidate kinds are the steward's."""
    w.fake.procs.pop(STEWARD_PID, None)
    counted = w.copy("counted")
    in_receipts = w.copy("dup-receipts", parent=w.receipts)
    hidden = w.copy(".dup-hidden")
    quarantined = w.copy("dup.vh-quarantine.20261009T000000Z")
    w.end_setup()
    acc = sa.take_account(w.registry, PROJECT, tiers=True, sources=w.sources())
    for path in (counted, in_receipts, hidden):
        assert item_of(acc, path)["eligible"] is True, path
    assert [item_of(acc, p)["candidate"] for p in (counted, in_receipts, hidden, quarantined)] \
        == [True, False, False, False]
    reclaim = acc["reclaimable_now"]
    assert reclaim["basis"] == "GROSS" and reclaim["items"] == 1
    assert reclaim["bytes"] == item_of(acc, counted)["bytes"]
    assert set(sa._CANDIDATE_KINDS) == set(steward._CANDIDATE_KINDS) | {"runs_base",
                                                                        "legacy_review"}
    assert "receipts" not in sa._CANDIDATE_KINDS and "tmp" not in sa._CANDIDATE_KINDS


def test_an_unmeasured_eligible_item_makes_reclaimable_a_lower_bound(w, monkeypatch):
    """Catches a lower bound that ignores an unmeasured eligible item (M29): an
    eligible candidate whose walk is unreadable keeps its tier, makes
    `reclaimable_now` read UNMEASURED with `lower_bound` true and
    `unmeasured_items` 1. Negative control: a readable one is a number and not
    a lower bound."""
    ok = w.copy("dup-ok")
    bad = w.copy("dup-bad")
    w.end_setup()
    sources = w.sources()
    clean = sa.take_account(w.registry, PROJECT, tiers=True, sources=sources)
    assert clean["reclaimable_now"]["items"] == 2 and clean["reclaimable_now"]["lower_bound"] is False
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p.endswith("/dup-bad/sub"), PermissionError("denied")))
    acc = sa.take_account(w.registry, PROJECT, tiers=True, sources=sources.fresh())
    assert item_of(acc, bad)["status"] == "UNMEASURED"
    assert item_of(acc, bad)["tier"] == st.VERIFIED_DUPLICATE and item_of(acc, ok)["eligible"]
    reclaim = acc["reclaimable_now"]
    assert reclaim["bytes"] == sa.UNMEASURED and reclaim["unmeasured_items"] == 1
    assert reclaim["lower_bound"] is True and reclaim["items"] == 2
    assert reclaim["bytes_lower_bound"] >= item_of(acc, ok)["bytes"]


def test_the_tier_pass_stops_when_the_budget_is_spent(w):
    """Catches a tier pass that does not check the budget (M28): when the first
    classification spends the budget, `tier_of` is not called again; the other
    items are tier UNMEASURED (BUDGET) and the account is not complete."""
    for name in ("a", "b", "c"):
        (w.scratch / name).mkdir()
    now = {"t": 0.0}
    calls = []

    def tier_of(path):
        calls.append(path)
        now["t"] += 1000.0
        return st.TierDecision(path=path, tier=st.COLD, reasons=("test",))

    acc = account(w, tiers=True, tier_of=tier_of, budget_s=100.0, clock=lambda: now["t"])
    assert len(calls) == 1
    tiers_seen = [i["tier"] for i in acc["items"]]
    assert tiers_seen.count(st.COLD) == 1 and tiers_seen.count(sa.UNMEASURED) == len(tiers_seen) - 1
    late = [i for i in acc["items"] if i["tier"] == sa.UNMEASURED]
    assert all(i["tier_reason"].startswith("BUDGET") for i in late)
    assert acc["complete"] is False


# --------------------------------------------------------------------------- #
# R5 -- the command
# --------------------------------------------------------------------------- #


def use_world(monkeypatch, world: World) -> None:
    """The command builds `StorageRegistry(root)`; the tests hand it the world's registry
    (its OS readers are injected: the real mount ids differ from the fake ones the world's
    scratch is indexed with) and the world's fake ``/proc``."""
    monkeypatch.setattr("hoh.storage_registry.StorageRegistry", lambda root: world.registry)
    real = st.EvidenceSources
    monkeypatch.setattr(st, "EvidenceSources",
                        lambda registry, project: real(registry, project,
                                                       proc_root=str(world.proc)))


def run_cli(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(["storage", "account", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_prints_the_account_and_writes_nothing(w, monkeypatch, capsys):
    """Catches a command that prints something else, writes or fails on a good
    root: the JSON has `storage_root` and `account`, exit 0, and the world is
    unchanged. `--no-tiers` reads NOT_RUN."""
    use_world(monkeypatch, w)
    (w.scratch / "proj").mkdir()
    (w.scratch / "proj" / "f").write_text("12345", encoding="utf-8")
    before = snapshot(w.tmp)
    code, out, err = run_cli(capsys, "--project", PROJECT, "--no-tiers",
                             "--storage-root", str(w.registry.state_root))
    assert code == 0 and err == ""
    printed = json.loads(out)
    assert set(printed) == {"storage_root", "account"}
    assert printed["storage_root"] == str(w.registry.state_root)
    acc = printed["account"]
    assert acc["schema"] == sa.SCHEMA and acc["project_id"] == PROJECT
    assert acc["budget_s"] == 120.0 and acc["tiers"]["run"] is False
    assert item_of(acc, w.scratch / "proj")["bytes"] == 5
    assert item_of(acc, w.scratch / "proj")["tier"] == sa.NOT_RUN
    assert snapshot(w.tmp) == before


def test_cli_with_tiers_prints_a_tier_per_item(w, monkeypatch, capsys):
    """Catches a command that does not run the tiers by default: every item of
    the printed account has a tier of the tier module (a verified duplicate is
    eligible), and `--budget` is taken as a float."""
    use_world(monkeypatch, w)
    dup = w.copy("dup")
    w.end_setup()
    code, out, err = run_cli(capsys, "--project", PROJECT, "--budget", "30.5",
                             "--storage-root", str(w.registry.state_root))
    assert code == 0 and err == ""
    acc = json.loads(out)["account"]
    assert acc["budget_s"] == 30.5 and acc["tiers"]["run"] is True
    assert all(i["tier"] not in (sa.NOT_RUN,) for i in acc["items"])
    assert item_of(acc, dup)["tier"] == st.VERIFIED_DUPLICATE and item_of(acc, dup)["eligible"]
    assert acc["reclaimable_now"]["items"] == 1


def test_cli_refuses_what_it_cannot_account(w, monkeypatch, capsys, tmp_path):
    """Catches a command that accounts what it must refuse (M14): an
    uninitialised root (no directory is created), an unregistered project and
    five bad budgets exit 2 with `storage account refused:` on stderr and nothing
    on stdout; `--budget abc` is argparse's own exit 2."""
    absent = tmp_path / "no-state"
    code, out, err = run_cli(capsys, "--project", PROJECT, "--storage-root", str(absent))
    assert code == 2 and out == ""
    assert err.startswith("storage account refused: storage state root is not initialised")
    assert not absent.exists()
    use_world(monkeypatch, w)
    root = str(w.registry.state_root)
    code, out, err = run_cli(capsys, "--project", "nobody", "--storage-root", root)
    assert code == 2 and out == "" and err.startswith("storage account refused:")
    assert "nobody" in err
    for bad in ("0", "-1", "nan", "inf", "3601"):
        code, out, err = run_cli(capsys, "--project", PROJECT, "--storage-root", root,
                                 f"--budget={bad}")
        assert code == 2 and out == "" and err.startswith("storage account refused:"), bad
    with pytest.raises(SystemExit) as raised:
        cli.main(["storage", "account", "--project", PROJECT, "--storage-root", root,
                  "--budget", "abc"])
    assert raised.value.code == 2
    out = capsys.readouterr()
    assert out.out == "" and "invalid float value" in out.err
    assert "storage account refused" not in out.err


def test_cli_prints_a_partial_account_with_exit_zero(w, monkeypatch, capsys):
    """Catches a partial account turned into a verdict (M9): an item that cannot
    be read is printed UNMEASURED, `complete` is false and the exit code is 0."""
    use_world(monkeypatch, w)
    (w.scratch / "locked" / "sub").mkdir(parents=True)
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p.endswith("/locked/sub"), PermissionError("denied")))
    code, out, err = run_cli(capsys, "--project", PROJECT, "--no-tiers",
                             "--storage-root", str(w.registry.state_root))
    assert code == 0 and err == ""
    acc = json.loads(out)["account"]
    assert acc["complete"] is False and acc["total"]["entries"] == sa.UNMEASURED
    assert item_of(acc, w.scratch / "locked")["status"] == "UNMEASURED"


# --------------------------------------------------------------------------- #
# R6 -- groups and labels
# --------------------------------------------------------------------------- #


def test_run_labels_are_validated_or_claimed_and_records_are_checked(w):
    """Catches a label taken from a name or an unchecked record (M8, M26, M27,
    M33): a run directory the registry accepts is its run (VALIDATED), a
    lookalike (no state, or a state of another run) is no run; a tmp workspace
    is VALIDATED when a registered runs base holds that run and CLAIMED (key
    `claimed:<id>`) when not; a record whose run id or purpose is not a short
    lower-case id gives no label; a child of a `runs` scratch directory is
    validated by its state."""
    base = make_runs_base(w)
    w.run(RUN_A, files={"out/x.txt": "x\n"})
    (base / "20261001t0600z-lookalike").mkdir()
    liar = base / "20261001t0700z-liar"
    liar.mkdir()
    (liar / "state.json").write_text(run_state(RUN_B), encoding="utf-8")
    validated = workspace(w, run_id=RUN_A, purpose="test")
    claimed = workspace(w, run_id=GHOST, purpose="check")
    forged = workspace(w, run_id=RUN_B, purpose="check")
    bad_record = se.record_path(forged.path)
    record = json.loads(bad_record.read_text(encoding="utf-8"))
    record.update({"run_id": "../Not A Run", "purpose": "Bad Purpose!", "workspace_id": "X Y"})
    bad_record.write_text(json.dumps(record), encoding="utf-8")
    runs_dir = w.registry.scratch_dir(PROJECT, "s1", "runs")
    (runs_dir / RUN_B).mkdir()
    (runs_dir / RUN_B / "state.json").write_text(run_state(RUN_B), encoding="utf-8")
    (runs_dir / "20261001t0800z-nostate").mkdir()
    acc = account(w)
    run_a = item_of(acc, base / RUN_A)
    assert (run_a["run_id"], run_a["run_id_basis"], run_a["label_status"]) == (
        RUN_A, "VALIDATED", "OK")
    for name in ("20261001t0600z-lookalike", "20261001t0700z-liar"):
        assert item_of(acc, base / name)["run_id"] is None
    assert item_of(acc, base / "20261001t0600z-lookalike")["label_status"] == "OK"
    one = item_of(acc, validated.path)
    assert (one["run_id"], one["run_id_basis"], one["purpose"]) == (RUN_A, "VALIDATED", "test")
    assert one["workspace_id"] == validated.workspace_id
    two = item_of(acc, claimed.path)
    assert (two["run_id"], two["run_id_basis"]) == (GHOST, "CLAIMED")
    three = item_of(acc, forged.path)
    assert (three["run_id"], three["run_id_basis"], three["purpose"],
            three["workspace_id"]) == (None, None, None, None)
    assert three["label_status"] == "OK"
    scratch_run = item_of(acc, runs_dir / RUN_B)
    assert (scratch_run["run_id"], scratch_run["run_id_basis"]) == (RUN_B, "VALIDATED")
    assert item_of(acc, runs_dir / "20261001t0800z-nostate")["run_id"] is None
    assert acc["by_run"][RUN_A]["items"] == 2 and f"claimed:{GHOST}" in acc["by_run"]
    assert GHOST not in acc["by_run"] and acc["by_run"][RUN_B]["items"] == 1
    assert acc["by_purpose"]["test"]["items"] == 1 and acc["by_purpose"]["check"]["items"] == 1
    assert acc["by_session"]["-"]["items"] == 3
    tally_sums(acc)
    # a record is a record: a workspace of another name beside the same record gets no labels
    assert item_of(acc, w.scratch / ".vh-owner.json")["label_status"] == "NOT_APPLICABLE"


def test_items_are_in_sorted_order_and_the_default_budget_is_120(w):
    """Catches an unsorted listing (M30) and a changed default budget (M31):
    items come out roots by kind, session and path, children by name (hidden
    names first); the default budget is 120 s in the module, the signature, the
    account and the command's parser."""
    make_runs_base(w)
    w.run(RUN_B)
    w.run(RUN_A)
    for name in ("zeta", "alpha", "mid"):
        (w.scratch / name).mkdir()
    workspace(w, run_id=RUN_A)
    acc = account(w)
    keys = [(i["root_kind"], i["session_id"] or "", str(Path(i["path"]).parent), Path(i["path"]).name)
            for i in acc["items"]]
    assert keys == sorted(keys)
    names = [Path(i["path"]).name for i in acc["items"] if i["root_kind"] == "scratch"]
    assert names == [".vh-owner.json", "alpha", "mid", "zeta"]
    assert sa.DEFAULT_BUDGET_S == 120.0 and acc["budget_s"] == 120.0
    assert inspect.signature(sa.take_account).parameters["budget_s"].default == 120.0
    args = cli.build_parser().parse_args(["storage", "account", "--project", PROJECT])
    assert args.budget == 120.0 and args.no_tiers is False and args.storage_root is None


# --------------------------------------------------------------------------- #
# R7 -- the repair `i10bf` of the accepted slice
# --------------------------------------------------------------------------- #

UNPROVEN_FIGURES = ("entries", "bytes", "not_descended", "other_names_of_counted_inodes",
                    "items_with_unproven")


class SpySources:
    """Index readers that answer nothing and record that they were asked."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def scratch_dirs(self, *args):
        self.calls.append("scratch_dirs")
        return []

    def runs_bases(self):
        self.calls.append("runs_bases")
        return []

    def legacy_roots(self):
        self.calls.append("legacy_roots")
        return []


def test_the_unproven_aggregate_reads_unmeasured_when_an_item_was_not_measured(
        w, monkeypatch, tmp_path):
    """Catches an aggregate that breaks the headline rule (B1, FM1): when an
    `owner_of` that hangs leaves an item UNMEASURED, every figure of the
    account-level `unproven` reads the string UNMEASURED with the sum of what
    was seen in `<figure>_lower_bound`, `unmeasured_items` counts the item and
    `lower_bound` is true. Negative control: the same world with nothing
    hanging has plain integers, `lower_bound` false, no `*_lower_bound` key
    and `unmeasured_items` 0."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    links = w.scratch / "a-links"
    links.mkdir()
    (links / "f1").write_text("hello world", encoding="utf-8")
    os.link(links / "f1", links / "f2")
    hang = w.scratch / "b-hang"
    hang.mkdir()
    (hang / "a-out").symlink_to(foreign)
    (hang / "z-late").write_text("late", encoding="utf-8")
    gate = threading.Event()
    real = w.registry.owner_of

    def owner_of(path):
        if str(path).endswith("/b-hang/z-late"):
            gate.wait(30)
        return real(path)

    monkeypatch.setattr(w.registry, "owner_of", owner_of)
    gate.set()                                                   # the control: nothing blocks
    clean = account(w)["unproven"]
    assert clean == {"entries": 2, "bytes": 11, "not_descended": 0,
                     "other_names_of_counted_inodes": 1, "items_with_unproven": 2,
                     "unmeasured_items": 0, "lower_bound": False}
    gate.clear()
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    try:
        acc = account(w, budget_s=0.5)
    finally:
        gate.set()
    assert item_of(acc, hang)["status"] == "UNMEASURED"
    aggregate = acc["unproven"]
    assert set(aggregate) == ({*UNPROVEN_FIGURES} | {f"{f}_lower_bound" for f in UNPROVEN_FIGURES}
                              | {"unmeasured_items", "lower_bound"})
    for figure in UNPROVEN_FIGURES:
        assert aggregate[figure] == sa.UNMEASURED, figure
    assert aggregate["entries_lower_bound"] == 2 and aggregate["bytes_lower_bound"] == 11
    assert aggregate["not_descended_lower_bound"] == 0
    assert aggregate["other_names_of_counted_inodes_lower_bound"] == 1
    assert aggregate["items_with_unproven_lower_bound"] == 2
    assert aggregate["unmeasured_items"] == acc["total"]["unmeasured_items"] >= 1
    assert aggregate["lower_bound"] is True and acc["complete"] is False
    json.dumps(acc)


def test_a_budget_too_large_for_a_float_is_a_value_error(w):
    """Catches `OverflowError` escaping the budget check (M2, FM2): an int that no
    float can hold, positive or negative, is a `ValueError` like any other bad
    budget, before anything is read. Negative control: an int in range is a
    number."""
    for bad in (10 ** 400, -(10 ** 400)):
        with pytest.raises(ValueError) as raised:
            account(w, budget_s=bad)
        assert not isinstance(raised.value, OverflowError)
        assert len(str(raised.value)) < 1000
    assert account(w, budget_s=5)["budget_s"] == 5.0


def test_the_project_record_read_is_inside_the_bound(w, monkeypatch):
    """Catches the project record read outside the bound (A2, M3, FM3): a read of
    `PROJECT.json` that blocks is abandoned at budget plus grace; the account
    comes back with no items, one `BUDGET` problem, and is neither complete
    nor scope-complete. An unregistered project is still a `ValueError`."""
    gate = threading.Event()
    real = w.registry._project_record

    def slow(project_id):
        gate.wait(30)
        return real(project_id)

    monkeypatch.setattr(w.registry, "_project_record", slow)
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    started = time.monotonic()
    try:
        acc = account(w, budget_s=0.5)
    finally:
        gate.set()
    assert time.monotonic() - started < 10
    assert acc["items"] == [] and acc["not_accounted_roots"] == []
    assert len(acc["problems"]) == 1 and acc["problems"][0].startswith("BUDGET")
    assert acc["complete"] is False and acc["scope_complete"] is False
    assert acc["total"]["items"] == 0 and acc["unproven"]["lower_bound"] is False
    with pytest.raises(ValueError):                      # the gate is open: a real answer
        sa.take_account(w.registry, "nobody", tiers=False)


def test_an_abandoned_walker_counts_nothing_more(w, monkeypatch):
    """Catches a walker that keeps counting after the account gave up on it (A6,
    FM4): the `owner_of` of one entry blocks, the item is abandoned (UNMEASURED,
    BUDGET); once the call returns the walker's counters (`walk.owned`,
    `walk.local`) have not moved from what the account read, and the walker
    thread has ended."""
    item = w.scratch / "b-hang"
    item.mkdir()
    (item / "a-first").write_text("1", encoding="utf-8")
    (item / "b-blocked").write_text("22", encoding="utf-8")
    (item / "c-never").write_text("333", encoding="utf-8")
    gate = threading.Event()
    real = w.registry.owner_of

    def owner_of(path):
        if str(path).endswith("/b-hang/b-blocked"):
            gate.wait(30)
        return real(path)

    seen: dict = {}
    real_classify = sa._Taker.classify

    def classify(self, one):
        if one.path == str(item):
            seen.update(walk=one.walk, owned=list(one.walk.owned), local=set(one.walk.local),
                        status=(one.status, one.reason))
        return real_classify(self, one)

    monkeypatch.setattr(w.registry, "owner_of", owner_of)
    monkeypatch.setattr(sa._Taker, "classify", classify)
    monkeypatch.setattr(sa, "HUNG_GRACE_S", 0.2)
    walkers: list[threading.Thread] = []
    try:
        account(w, budget_s=0.5)
        walkers = [t for t in threading.enumerate() if t.name == "vh-bounded-storage-account-walk"]
        assert walkers and seen["status"] == ("UNMEASURED", "BUDGET")
    finally:
        gate.set()
    for thread in walkers:
        thread.join(10)
    assert not any(t.is_alive() for t in walkers)
    walk = seen["walk"]
    assert walk.abandoned is True
    assert seen["owned"][0] == 1 and seen["owned"][2] == 2         # the directory and a-first
    assert walk.owned == seen["owned"] and walk.local == seen["local"]


def test_both_unaccounted_kinds_are_named(w, tmp_path):
    """Catches a `linked_worktree` left out of `not_accounted_roots` (FM11): a
    registered `target_clone` and a registered `linked_worktree` are each one
    entry (reason LATER_SLICE), sorted by kind; `scope_complete` is false and
    `complete` stays true; neither is listed as an item."""
    parent = (tmp_path / "ext").resolve()
    parent.mkdir()
    admit(w, parent)
    clone = w.registry.create_external_root(PROJECT, parent / "clone", "target_clone")
    worktree = w.registry.create_external_root(PROJECT, parent / "wt", "linked_worktree")
    acc = account(w)
    assert acc["not_accounted_roots"] == [
        {"kind": "linked_worktree", "path": worktree["path"], "reason": "LATER_SLICE"},
        {"kind": "target_clone", "path": clone["path"], "reason": "LATER_SLICE"}]
    assert acc["scope_complete"] is False and acc["complete"] is True
    assert not [i for i in acc["items"]
                if i["path"].startswith((clone["path"], worktree["path"]))]
    assert sa._NOT_ACCOUNTED_KINDS == ("target_clone", "linked_worktree")


def test_unproven_samples_are_capped_at_ten(w, tmp_path):
    """Catches an unbounded sample list (FM9): twelve entries the registry does not
    own are counted in full (`entries` 12) but only the first ten, in name
    order, are listed as samples."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    item = w.scratch / "many"
    item.mkdir()
    for n in range(12):
        (item / f"out{n:02d}").symlink_to(foreign)
    one = item_of(account(w), item)
    assert one["status"] == "MEASURED" and one["unproven"]["entries"] == 12
    assert [s["rel"] for s in one["unproven"]["samples"]] == [f"out{n:02d}" for n in range(10)]
    assert sa._MAX_SAMPLES == 10


def test_a_candidate_is_a_directory(w):
    """Catches a regular file or a symlink read as a steward candidate (FM10): in a
    root of a candidate kind a directory is a candidate; a regular file and a
    symlink to a directory are not."""
    (w.scratch / "adir").mkdir()
    (w.scratch / "afile").write_text("x", encoding="utf-8")
    (w.scratch / "alink").symlink_to("adir")
    acc = account(w)
    assert item_of(acc, w.scratch / "adir")["candidate"] is True
    assert item_of(acc, w.scratch / "afile")["candidate"] is False
    assert item_of(acc, w.scratch / "alink")["candidate"] is False


def test_the_index_read_is_not_started_when_the_budget_is_already_spent(w, monkeypatch):
    """Catches the roots' index read started with the budget spent (FM12): when the
    clock is past the deadline before the roots are listed, neither the sources
    nor the registry's indexes are asked, the problem reads BUDGET, there are
    no items, and the account is neither complete nor scope-complete."""
    asked: list[str] = []
    for name in ("_scratch_entries", "_scratch_entry", "_root_entries", "_accepted_roots",
                 "legacy_roots"):
        def spy(*args, _real=getattr(w.registry, name), _name=name, **kw):
            asked.append(_name)
            return _real(*args, **kw)
        monkeypatch.setattr(w.registry, name, spy)
    readings = {"n": 0}

    def clock() -> float:
        readings["n"] += 1
        return 0.0 if readings["n"] == 1 else 1000.0

    sources = SpySources()
    acc = account(w, sources=sources, budget_s=10.0, clock=clock)
    assert acc["problems"] == ["BUDGET: the budget was spent before the roots were listed"]
    assert acc["items"] == [] and acc["not_accounted_roots"] == []
    assert acc["complete"] is False and acc["scope_complete"] is False
    assert sources.calls == [] and asked == []


def test_a_directory_that_vanishes_before_its_listing_is_skipped(w, monkeypatch):
    """Catches a directory that is gone between its `lstat` and its listing read
    as unreadable (FM13): the item stays MEASURED, `vanished` counts the
    directory and what the listing never gave is not counted. Negative control:
    any other OSError on a listing makes the item UNMEASURED."""
    item = w.scratch / "racy"
    (item / "gone-dir").mkdir(parents=True)
    (item / "gone-dir" / "inside").write_text("i", encoding="utf-8")
    (item / "kept").write_text("kk", encoding="utf-8")
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p.endswith("/racy/gone-dir"), FileNotFoundError("gone")))
    one = item_of(account(w), item)
    assert one["status"] == "MEASURED" and one["reason"] is None and one["vanished"] == 1
    assert one["entries"] == 3 and one["bytes"] == 2       # racy, gone-dir, kept
    monkeypatch.setattr(sa, "_scandir", failing(
        sa._scandir, lambda p: p.endswith("/racy/gone-dir"), PermissionError("denied")))
    other = item_of(account(w), item)
    assert other["status"] == "UNMEASURED" and other["reason"] == "UNREADABLE"


def test_a_registered_root_that_fails_the_identity_check_is_named_not_intact(w, tmp_path):
    """Catches a registered root the registry no longer accepts skipped silently
    (A3, FM5a, FM5b): a scratch directory (record intact, inode another), a
    runs base and a target clone, each renamed away and replaced by a fresh
    directory at the same path, are one `not_accounted_roots` entry each with
    reason NOT_INTACT (an intact clone is LATER_SLICE); `scope_complete` is
    false, `complete` stays true, nothing of them is listed as an item."""
    ext = (tmp_path / "ext").resolve()
    ext.mkdir()
    base = make_runs_base(w, (ext,))
    w.run(RUN_A)
    clone = Path(w.registry.create_external_root(PROJECT, ext / "clone", "target_clone")["path"])
    acc = account(w)
    assert acc["not_accounted_roots"] == [
        {"kind": "target_clone", "path": str(clone), "reason": "LATER_SLICE"}]
    assert item_of(acc, base / RUN_A)["status"] == "MEASURED"

    def replace(path: Path) -> None:
        moved = path.with_name(path.name + ".v1")
        path.rename(moved)
        path.mkdir()
        record = moved / ".vh-owner.json"
        if record.exists():                         # the record stays right; the inode does not
            (path / ".vh-owner.json").write_bytes(record.read_bytes())

    expected = [{"kind": "target_clone", "path": str(clone), "reason": "LATER_SLICE"}]
    scratch = w.scratch
    replace(scratch)
    expected.append({"kind": "scratch", "path": str(scratch), "reason": "NOT_INTACT"})
    acc = account(w)
    assert acc["not_accounted_roots"] == sorted(expected, key=lambda e: (e["kind"], e["path"]))
    assert acc["scope_complete"] is False and acc["complete"] is True
    assert not [i for i in acc["items"] if i["root_kind"] == "scratch"]
    replace(base)
    expected.append({"kind": "runs_base", "path": str(base), "reason": "NOT_INTACT"})
    acc = account(w)
    assert acc["not_accounted_roots"] == sorted(expected, key=lambda e: (e["kind"], e["path"]))
    assert not [i for i in acc["items"] if i["root_kind"] == "runs_base"]
    replace(clone)
    expected[0] = {"kind": "target_clone", "path": str(clone), "reason": "NOT_INTACT"}
    acc = account(w)
    assert acc["not_accounted_roots"] == sorted(expected, key=lambda e: (e["kind"], e["path"]))
    assert acc["scope_complete"] is False and acc["complete"] is True and acc["problems"] == []
    assert {r["reason"] for r in acc["not_accounted_roots"]} == {"NOT_INTACT"}
    assert item_of(acc, w.receipts / ".vh-owner.json")["status"] == "MEASURED"


def test_a_legacy_root_that_fails_the_identity_check_is_named_not_intact(w, tmp_path):
    """Catches a legacy root the registry drops (`intact_only`) skipped silently
    (A3, FM5c, L2): a REGISTERED entry of a synthetic `LEGACY_INDEX.json` whose
    recorded inode is not the directory's is one NOT_INTACT entry and no item;
    with the recorded inode it is a root, its children are items and nothing is
    named."""
    legacy = tmp_path / "legacy"
    (legacy / "r1").mkdir(parents=True)
    info = os.lstat(legacy)
    index = w.registry._project_dir(PROJECT) / "LEGACY_INDEX.json"

    def write(ino: int) -> None:
        index.write_text(json.dumps({
            "schema": "veriharness.storage-legacy-index/1", "project_id": PROJECT,
            "sequence": 1, "entries": [{
                "path": str(legacy), "project_id": PROJECT, "kind": "legacy_review",
                "state": "REGISTERED", "children": {}, "st_dev": info.st_dev, "st_ino": ino}]}),
            encoding="utf-8")

    write(info.st_ino + 1)
    acc = account(w)
    assert acc["not_accounted_roots"] == [
        {"kind": "legacy_review", "path": str(legacy), "reason": "NOT_INTACT"}]
    assert acc["scope_complete"] is False and acc["complete"] is True
    assert not [i for i in acc["items"] if i["root_kind"] == "legacy_review"]
    write(info.st_ino)
    acc = account(w)
    assert acc["not_accounted_roots"] == [] and acc["scope_complete"] is True
    assert [i["path"] for i in acc["items"] if i["root_kind"] == "legacy_review"] == [
        str(legacy / "r1")]


def test_a_validated_run_must_be_owned_by_the_registry(w, monkeypatch):
    """Catches VALIDATED on the run parser alone (A4, FM6): a run directory the
    parser accepts but the registry does not own (another mount: UNKNOWN) gets
    no run id and no `by_run` row, and a tmp workspace naming it is CLAIMED
    (key `claimed:<id>`). Negative control: before the mount differs, the same
    run is VALIDATED, for the directory and for the workspace."""
    make_runs_base(w)
    w.run(RUN_A)
    w.run(RUN_B)
    ws = workspace(w, run_id=RUN_B)
    acc = account(w)
    assert (item_of(acc, w.runs_base() / RUN_B)["run_id"],
            item_of(acc, w.runs_base() / RUN_B)["run_id_basis"]) == (RUN_B, "VALIDATED")
    assert item_of(acc, ws.path)["run_id_basis"] == "VALIDATED" and RUN_B in acc["by_run"]
    monkeypatch.setattr(w.registry, "_mount_id_reader",
                        lambda p: 2 if RUN_B in Path(p).parts else 1)
    acc = account(w)
    other = item_of(acc, w.runs_base() / RUN_B)
    assert other["run_id"] is None and other["run_id_basis"] is None
    assert other["label_status"] == "OK" and other["unproven"]["entries"] == 1
    claimed = item_of(acc, ws.path)
    assert (claimed["run_id"], claimed["run_id_basis"]) == (RUN_B, "CLAIMED")
    assert RUN_B not in acc["by_run"] and acc["by_run"][f"claimed:{RUN_B}"]["items"] == 1
    assert item_of(acc, w.runs_base() / RUN_A)["run_id_basis"] == "VALIDATED"
    assert acc["by_run"][RUN_A]["items"] == 1
    tally_sums(acc)


def test_the_label_pass_checks_the_clock_before_every_run_check(w, monkeypatch):
    """Catches a run check made after the deadline (FM7): the clock jumps past the
    deadline after the label pass has begun on an item (at its first `_label`
    call), so only the check right before the registry's run check can stop
    it. For a runs-base child and for a tmp workspace's record: no
    `_valid_run_dir` call is made after the deadline and the label status is
    NOT_REACHED. Negative control: without the jump the same items are
    labelled."""
    make_runs_base(w)
    w.run(RUN_A)
    clock = {"now": 0.0}
    calls: list[float] = []
    arm = {"on": None}
    real_valid, real_label = w.registry._valid_run_dir, sa._label
    monkeypatch.setattr(w.registry, "_valid_run_dir",
                        lambda child: (calls.append(clock["now"]), real_valid(child))[1])

    def label(value):
        if arm["on"] is not None and value == arm["on"]:
            clock["now"] = 100.0
        return real_label(value)

    monkeypatch.setattr(sa, "_label", label)

    def take() -> dict:
        clock["now"] = 0.0
        calls.clear()
        return account(w, budget_s=10.0, clock=lambda: clock["now"])

    acc = take()                                              # the control: no jump
    assert acc["complete"] is True and calls
    assert item_of(acc, w.runs_base() / RUN_A)["run_id_basis"] == "VALIDATED"
    arm["on"] = RUN_A                                         # the runs-base child's name
    acc = take()
    child = item_of(acc, w.runs_base() / RUN_A)
    assert child["label_status"] == "NOT_REACHED" and child["run_id"] is None
    assert [t for t in calls if t >= 10.0] == [] and acc["complete"] is False
    ws = workspace(w, run_id=RUN_A, purpose="sandbox")
    arm["on"] = None
    acc = take()
    assert item_of(acc, ws.path)["run_id_basis"] == "VALIDATED" and calls
    arm["on"] = "sandbox"                               # the record's purpose
    acc = take()
    record = item_of(acc, ws.path)
    assert record["label_status"] == "NOT_REACHED" and record["run_id"] is None
    assert [t for t in calls if t >= 10.0] == []
    assert item_of(acc, w.runs_base() / RUN_A)["label_status"] == "OK"     # before the jump
    assert acc["complete"] is False


def test_cli_prints_valid_ascii_json_for_a_name_that_is_not_utf8(w, monkeypatch, capsys):
    """Catches a command that crashes or prints invalid UTF-8 for a name that is not
    UTF-8 (A1, M1, FM8): a directory named `caf\\xe9` (bytes) under a registered
    root gives exit 0, ASCII-only output that parses as JSON, and the item's
    path carries the `\\udce9` escape. The shared `_print` is unchanged."""
    use_world(monkeypatch, w)
    os.mkdir(os.fsencode(str(w.scratch)) + b"/caf\xe9")
    code, out, err = run_cli(capsys, "--project", PROJECT, "--no-tiers",
                             "--storage-root", str(w.registry.state_root))
    assert code == 0 and err == ""
    assert out.isascii() and "\\udce9" in out
    paths = [i["path"] for i in json.loads(out)["account"]["items"]]
    assert str(w.scratch) + "/caf\udce9" in paths
    assert "ensure_ascii=False" in inspect.getsource(cli._print)
