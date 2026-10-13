"""I10a2 (P5-39 follow-up): a mount root is not a tab's directory, and a pane that two
overlapping reapers close gets one `closed` line.

Same harness as `tests/test_surfaces.py` (its autouse stub `herdr` keeps the log empty,
`FakeAgentControl` is the runtime, `FakeProcs` the process table), with a mount table of
the test's own (`surfaces.MOUNTINFO` points at a file under `tmp_path`). Nothing starts
Herdr or an agent; every test writes only under `tmp_path`; every thread is joined. Each
test names the failure it catches.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from hoh import pane_ledger, surfaces
from hoh.agentcontrol import OwnedAgentControl, ProgramAgentRole
from hoh.controller import DispatchError

# The harness of the surfaces tests, imported by name: the autouse `_isolation` stub
# applies to this module as it does there.
from test_surfaces import World, _isolation, _race_world, _summary  # noqa: F401
from test_surfaces import world as world  # a fixture, found by pytest by name; the alias form is an explicit re-export, so
# a test parameter named `world` is not a redefinition of an unused import (F811)



# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def mountinfo_line(mount_point: bytes, n: int = 100) -> bytes:
    """One line of `/proc/self/mountinfo`; the mount point is field 5, as given."""
    return (b"%d 1 0:%d / %s rw,relatime shared:1 - ext4 /dev/sdx rw\n"
            % (n, n, mount_point))


def escape(path: str) -> bytes:
    """A path as the kernel spells it in the table: space, tab, newline, backslash as octal."""
    out = bytearray()
    for byte in os.fsencode(path):
        if byte in b" \t\n\\":
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out)


def set_mounts(monkeypatch, tmp_path, *mount_points, name="mountinfo") -> Path:
    """Point `surfaces.MOUNTINFO` at a table that lists `/` and the given mount points."""
    table = tmp_path / name
    lines = [mountinfo_line(b"/", 1)]
    lines += [mountinfo_line(m if isinstance(m, bytes) else escape(str(m)), 100 + i)
              for i, m in enumerate(mount_points)]
    table.write_bytes(b"".join(lines))
    monkeypatch.setattr(surfaces, "MOUNTINFO", str(table))
    return table


def open_without_the_check(world, run, cwd, role) -> str:
    """A surface recorded at `cwd` the way a record written before this change (or
    by hand, or by an opener that does not run `_cwd_problem`) holds it."""
    owned = OwnedAgentControl(world.fake, surfaces.registry_for(world.run_dir(run)))
    with owned.acting_as(ProgramAgentRole.program(role), program="p", run_id=run):
        surface = owned.open_surface(cwd=str(cwd), label=role, env=None, purpose="open")
    world.procs.add_shell(surface.pane_id)
    return surface.pane_id


def refusal(world, run, cwd) -> str:
    """`hoh surface open` at `cwd`: its stderr when refused (nothing created), else ''."""
    world.reset()
    rc, out, err = world.hoh("surface", "open", run, "--label=host", f"--cwd={cwd}")
    if rc == 0:
        return ""
    assert (rc, out) == (2, ""), (rc, out, err)
    assert world.fake.calls == [] and world.controls == 0
    return err


def run_in_thread(fn):
    """Start `fn` in a thread; `.join()` joins it and raises what it raised."""
    caught: list[BaseException] = []

    def target():
        try:
            fn()
        except BaseException as exc:                   # noqa: BLE001 - re-raised on join
            caught.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    class Handle:
        def join(self, timeout=10):
            thread.join(timeout)
            assert not thread.is_alive(), "a thread of the test did not finish"
            if caught:
                raise caught[0]

    return Handle()


def tab_closes(world) -> int:
    """How many times the runtime was asked to close a tab (the fake logs the
    `close_pane` of each pane in it as well)."""
    return world.verbs().count("close_tab")


def ledger_fields(s):
    return {"pane": s["pane_id"], "tab": s["tab_id"], "role": "program:run-host",
            "via": "surface"}


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #


def test_open_refuses_a_mount_root_as_the_directory_and_reads_the_mount_table_fail_closed(
        world, tmp_path, monkeypatch):
    """Catches: `--cwd` a mount root (a drive, a bind mount) accepted, so that every
    pane anywhere under it passes the identity check; a mount table that cannot be read
    taken as "no mount"; a directory below a mount, a sibling that shares the prefix or a
    directory that only contains a mount refused; the bind mount the table does not show
    missed; an escaped mount point not recognised."""
    run = world.run("r-mnt")
    drive = tmp_path / "mnt" / "drive"
    sibling = tmp_path / "mnt" / "drive2"
    below = drive / "projects" / "x"
    spaced = tmp_path / "mnt" / "my drive"
    bind = tmp_path / "mnt" / "bind"
    for d in (below, sibling, spaced, bind):
        d.mkdir(parents=True)
    set_mounts(monkeypatch, tmp_path, drive, spaced)

    # the mount point itself: refused, spelled as it is, with a slash, with a detour
    for spelling in (str(drive), str(drive) + "/", f"{tmp_path}/mnt/./drive",
                     f"{tmp_path}/mnt/drive/projects/.."):
        err = refusal(world, run, spelling)
        assert "mount point" in err, (spelling, err)
    # a link to it is the mount point
    link = tmp_path / "work" / "link-to-drive"
    link.symlink_to(drive)
    assert "mount point" in refusal(world, run, link)
    # the octal escape of the table (a space) is undone
    assert "mount point" in refusal(world, run, spaced)
    assert not (world.run_dir(run) / "owned-surfaces.jsonl").exists()

    # a directory below the mount, a sibling that shares the prefix, a directory that
    # only contains a mount: all accepted
    for ok in (below, drive / "projects", sibling, tmp_path / "mnt"):
        assert refusal(world, run, ok) == "", ok
    assert len(world.registry(run).owned()) == 4

    # a bind mount of the same device that the table does not list: `os.path.ismount`
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount",
                        lambda p: str(p) == os.path.realpath(bind) or real_ismount(p))
    assert "mount point" in refusal(world, run, bind)
    monkeypatch.setattr(os.path, "ismount", real_ismount)
    assert refusal(world, run, bind) == ""

    # the table cannot be read, or lists no mount: nothing is ruled out, so it refuses
    monkeypatch.setattr(surfaces, "MOUNTINFO", str(tmp_path / "no-such-table"))
    with pytest.raises(OSError):
        surfaces.mount_points()
    err = refusal(world, run, sibling)
    assert "mount table cannot be read" in err, err
    (tmp_path / "empty").write_bytes(b"")
    monkeypatch.setattr(surfaces, "MOUNTINFO", str(tmp_path / "empty"))
    with pytest.raises(OSError):
        surfaces.mount_points()
    assert "mount table cannot be read" in refusal(world, run, sibling)
    # a table that is a directory (a read error of another kind) is the same
    monkeypatch.setattr(surfaces, "MOUNTINFO", str(tmp_path))
    assert "mount table cannot be read" in refusal(world, run, sibling)
    # and the table is read at the time of the call, not when the module was imported
    set_mounts(monkeypatch, tmp_path, drive)
    assert refusal(world, run, sibling) == ""


# --------------------------------------------------------------------------- #
# reap: the recorded directory
# --------------------------------------------------------------------------- #


def test_reap_refuses_a_recorded_directory_that_is_a_mount_root_and_closes_nothing(
        world, tmp_path, monkeypatch):
    """Catches: a surface recorded at a mount root (written before this change, or by
    hand) closed because every pane under the drive passes the identity check; a
    refusal that is sent to the runtime anyway, written again at every reap, or that
    stops the other surfaces of the run."""
    run = world.run("r-broad")
    info = world.open(run)                            # recorded at world.work
    good = tmp_path / "other"
    good.mkdir()
    other = world.open(run, label="b", cwd=good, role="run-b")
    world.finish(run)
    world.reset()
    set_mounts(monkeypatch, tmp_path, world.work)

    # a dry run says so and writes nothing
    rc, rows, err = world.reap(run, close=False)
    by_pane = {r["pane_id"]: r for r in rows}
    assert by_pane[info["pane_id"]]["outcome"] == "refused", rows
    assert "recorded with is too broad" in by_pane[info["pane_id"]]["why"]
    assert "mount point" in by_pane[info["pane_id"]]["why"]
    assert by_pane[other["pane_id"]]["outcome"] == "would_close"
    assert world.events(run) == [] and world.closes() == []

    # a close: the mount root stays, the other surface of the run is judged on its own
    world.reset()
    rc, rows, err = world.reap(run)
    by_pane = {r["pane_id"]: r for r in rows}
    assert by_pane[info["pane_id"]]["outcome"] == "refused", rows
    assert by_pane[other["pane_id"]]["outcome"] == "closed", rows
    assert tab_closes(world) == 1
    assert info["pane_id"] in world.fake.panes and other["pane_id"] not in world.fake.panes
    refused = [r for r in world.ledger(run) if r["event"] == "refused"]
    assert len(refused) == 1 and refused[0]["pane"] == info["pane_id"]
    assert "too broad" in refused[0]["why"]
    assert world.states(run).count("owned") == 1

    # again: nothing sent, the refusal is not written a second time
    world.reset()
    rc, rows, err = world.reap(run)
    assert [r["outcome"] for r in rows] == ["refused"]
    assert world.closes() == []
    assert [r["event"] for r in world.ledger(run)].count("refused") == 1

    # the table no longer lists it: the same record is closed (the refusal was the check)
    set_mounts(monkeypatch, tmp_path, tmp_path / "elsewhere")
    world.reset()
    assert world.one(run)["outcome"] == "closed"
    assert tab_closes(world) == 1
    assert world.states(run) == ["released", "released"]


def test_reap_applies_the_whole_directory_check_and_reads_the_mount_table_once(
        world, tmp_path, monkeypatch):
    """Catches: a reap that refuses only a mount root (a recorded home directory, one
    of its ancestors or a system directory still closed); a mount table read once per
    surface; an unreadable table that refuses nothing; a directory that no longer
    exists reported as an error."""
    run = world.run("r-all")
    home = Path(os.environ["HOME"])
    mount = tmp_path / "drive"
    fine = tmp_path / "fine"
    gone = tmp_path / "gone"
    for d in (mount, fine, gone):
        d.mkdir()
    panes = {
        "home": open_without_the_check(world, run, home, "host-home"),
        "above": open_without_the_check(world, run, tmp_path, "host-above"),
        "system": open_without_the_check(world, run, "/usr", "host-system"),
        "mount": open_without_the_check(world, run, mount, "host-mount"),
        "fine": open_without_the_check(world, run, fine, "host-fine"),
        "gone": open_without_the_check(world, run, gone, "host-gone"),
    }
    world.finish(run)
    gone.rmdir()                                      # recorded, and not there now
    world.reset()
    table = set_mounts(monkeypatch, tmp_path, mount)
    reads = []
    real_mount_points = surfaces.mount_points

    def counting():
        reads.append(1)
        return real_mount_points()

    monkeypatch.setattr(surfaces, "mount_points", counting)

    rc, rows, err = world.reap(run, close=False)
    out = {r["pane_id"]: r for r in rows}
    assert len(rows) == 6 and len(reads) == 1, (len(rows), reads)
    for key, text in (("home", "home directory"), ("above", "home directory or one of"),
                      ("system", "top-level system directory"), ("mount", "mount point")):
        row = out[panes[key]]
        assert row["outcome"] == "refused", (key, row)
        assert "recorded with is too broad" in row["why"] and text in row["why"], (key, row)
    # no existence check here: a directory that is gone is not "too broad"
    assert out[panes["gone"]]["outcome"] == "would_close", out[panes["gone"]]
    assert out[panes["fine"]]["outcome"] == "would_close"
    assert world.events(run) == [] and world.closes() == []

    # every reap reads the table once, however many surfaces it lists
    del reads[:]
    world.reap(run, close=False)
    assert len(reads) == 1

    # an unreadable table refuses every listed surface (fail closed) and is read again
    table.rename(tmp_path / "mountinfo.hidden")
    del reads[:]
    world.reset()
    rc, rows, err = world.reap(run)
    assert len(reads) == 1 and world.closes() == []
    assert len(rows) == 6 and all(r["outcome"] == "refused" for r in rows), rows
    out = {r["pane_id"]: r for r in rows}
    for key in ("mount", "fine", "gone"):             # nothing rules a mount root out
        assert "mount table cannot be read" in out[panes[key]]["why"], out[panes[key]]
    for key in ("home", "above", "system"):           # these are refused on their own
        assert "mount table" not in out[panes[key]]["why"], out[panes[key]]
    (tmp_path / "mountinfo.hidden").rename(table)
    del reads[:]
    world.reset()
    rc, rows, err = world.reap(run)
    assert len(reads) == 1
    out = {r["pane_id"]: r for r in rows}
    assert out[panes["fine"]]["outcome"] == "closed"
    assert out[panes["gone"]]["outcome"] == "closed"
    assert [out[panes[k]]["outcome"] for k in ("home", "above", "system", "mount")] == \
        ["refused"] * 4
    assert tab_closes(world) == 2


def test_a_mount_point_with_a_byte_that_is_not_utf8_is_still_a_mount_point(
        tmp_path, monkeypatch):
    """Catches: a mount table read as text with replacement characters, so that a mount
    point whose name holds a byte that is not UTF-8 (spelled raw or as an octal escape)
    never matches the directory `os.path.realpath` names; or one that matches its
    neighbours too."""
    base = os.fsencode(str(tmp_path))
    raw, octal, unlisted = (base + b"/drive\xff1", base + b"/drive\xfe2", base + b"/drive\xff3")
    for d in (raw, octal, unlisted):
        os.mkdir(d)
    table = tmp_path / "mountinfo"
    table.write_bytes(mountinfo_line(b"/", 1) + mountinfo_line(raw, 2)
                      + mountinfo_line(base + b"/drive\\376" + b"2", 3))
    monkeypatch.setattr(surfaces, "MOUNTINFO", str(table))
    points = surfaces.mount_points()
    assert os.fsdecode(raw) in points and os.fsdecode(octal) in points
    assert os.fsdecode(unlisted) not in points
    for d in (raw, octal):
        assert "mount point" in surfaces._cwd_problem(os.fsdecode(d)), d
    assert surfaces._cwd_problem(os.fsdecode(unlisted)) == ""


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def test_the_ledger_gate_is_exclusive_given_back_and_fails_closed(tmp_path, monkeypatch):
    """Catches: a gate that is not exclusive (yields True without locking); a
    descriptor that is kept after the block (the lock with it, so the next reaper never
    gets in); a gate that raises, or is held, for a directory that is missing, a link or
    a file; an `append` that waits for the gate."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(pane_ledger, "GATE_WAIT_S", 0.1)
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def recording_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        if str(path) == str(run_dir):
            opened.append(fd)
        return fd

    def recording_close(fd):
        closed.append(fd)
        return real_close(fd)

    with monkeypatch.context() as scope:
        scope.setattr(os, "open", recording_open)
        scope.setattr(os, "close", recording_close)
        with pane_ledger.gate(run_dir) as first:
            assert first is True
            # a second holder is refused, and `append` does not wait for the gate
            with pane_ledger.gate(run_dir) as second:
                assert second is False
            assert pane_ledger.append(run_dir, "kept", pane="p1", why="x") is True
        # given back: the next holder gets in
        with pane_ledger.gate(run_dir) as third:
            assert third is True
    assert len(opened) >= 3                           # (the append syncs the directory too)
    for fd in set(opened):
        assert closed.count(fd) >= opened.count(fd), (fd, opened, closed)  # lock with it

    # waits for a holder that lets go in time
    monkeypatch.setattr(pane_ledger, "GATE_WAIT_S", 5.0)
    held, release = threading.Event(), threading.Event()

    def holder():
        with pane_ledger.gate(run_dir) as ok:
            assert ok is True
            held.set()
            assert release.wait(10)

    handle = run_in_thread(holder)
    assert held.wait(10)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    with pane_ledger.gate(run_dir) as waited:
        assert waited is True
    timer.join()
    handle.join()

    # fails closed: no directory, a link to one, a file -- False, never raised, no lock left
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(run_dir)
    for bad in (tmp_path / "missing", tmp_path / "link", tmp_path / "file"):
        with pane_ledger.gate(bad) as ok:
            assert ok is False, bad
    with pane_ledger.gate(run_dir) as still:
        assert still is True

    # and the surfaces module's own gate is the ledger's
    assert not hasattr(surfaces, "gate")


def test_a_gate_that_cannot_be_had_keeps_the_surface_and_sends_nothing(world, monkeypatch):
    """Catches: a reaper that cannot get the ledger gate and closes anyway (a `closing`
    line outside the gate, a second `closed` later); a `kept` line written again at
    every reap; a surface that stays kept after the gate is free."""
    run, info, run_dir = _race_world(world, "r-busy")
    monkeypatch.setattr(pane_ledger, "GATE_WAIT_S", 0.1)
    with pane_ledger.gate(run_dir) as held:
        assert held is True
        row = world.one(run)
        assert row["outcome"] == "kept" and "ledger gate" in row["why"], row
        assert world.closes() == [] and info["pane_id"] in world.fake.panes
        assert world.events(run) == ["kept"]
        assert world.states(run) == ["owned"]
        world.reset()
        assert world.one(run)["outcome"] == "kept"
        assert world.closes() == [] and world.events(run) == ["kept"]
    assert _summary(world, run)["unconfirmed"] == []
    # the gate is free: the next reap closes it, once
    world.reset()
    assert world.one(run)["outcome"] == "closed"
    assert world.events(run) == ["kept", "closing", "closed"]
    assert tab_closes(world) == 1
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["kept_open"] == []
    assert summary["unconfirmed"] == [] and world.states(run) == ["released"]


# --------------------------------------------------------------------------- #
# one `closed` line per close
# --------------------------------------------------------------------------- #


def test_a_loser_that_waits_at_the_gate_for_its_closing_line_writes_nothing_after_the_winner(
        world, monkeypatch):
    """Catches: a loser that looked for the winner's `closed` line before it took the
    gate and writes its `closing` line after the winner's `closed` (a stale `closing` is
    then the pane's last line and `hoh status` lists a closed pane as unconfirmed)."""
    run, info, run_dir = _race_world(world, "r-once-a")
    real_gate = pane_ledger.gate
    main = threading.current_thread()
    at_gate, holding = threading.Event(), threading.Event()
    fields = ledger_fields(info)

    def loser_gate(run_dir_):
        if threading.current_thread() is main:
            at_gate.set()                             # the loser has looked and now waits
        return real_gate(run_dir_)

    def winner():
        with real_gate(run_dir) as ok:
            assert ok is True
            holding.set()
            assert at_gate.wait(10)
            pane_ledger.append(run_dir, "closing", reason="run over", scope="tab", **fields)
            world.fake.close_tab(info["tab_id"])
            pane_ledger.append(run_dir, "closed", reason="run over", **fields)

    handle = run_in_thread(winner)
    assert holding.wait(10)
    monkeypatch.setattr(pane_ledger, "gate", loser_gate)
    row = world.one(run)
    handle.join()
    assert row["outcome"] == "released" and "another reaper" in row["why"], row
    assert world.events(run) == ["closing", "closed"]
    assert tab_closes(world) == 1, "the winner's close is the only one"
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []


def test_a_loser_whose_close_fails_because_the_winner_closed_writes_no_second_closed_line(
        world, monkeypatch):
    """Catches: the window of the old check-then-append: the winner's `closed` lands
    after the loser read the pane's latest line and before the loser wrote its own, and
    `summary()['closed']` counts the pane twice."""
    run, info, run_dir = _race_world(world, "r-once-b")
    real_gate, real_close_tab = pane_ledger.gate, world.fake.close_tab
    main = threading.current_thread()
    calls, at_second = [], threading.Event()
    holding = threading.Event()
    fields = ledger_fields(info)
    handles = []

    def loser_gate(run_dir_):
        if threading.current_thread() is main:
            calls.append(1)
            if len(calls) == 2:                       # the gate of the `closed` line
                at_second.set()
        return real_gate(run_dir_)

    def winner():
        with real_gate(run_dir) as ok:
            assert ok is True
            holding.set()
            assert at_second.wait(10)
            pane_ledger.append(run_dir, "closed", reason="run over", already_gone=True,
                               **fields)

    def close_tab(tab, *, purpose=""):
        real_close_tab(tab, purpose=purpose)          # the winner closed it, ...
        handles.append(run_in_thread(winner))         # ... and is about to say so
        assert holding.wait(10)
        raise DispatchError("tab_not_found")

    monkeypatch.setattr(pane_ledger, "gate", loser_gate)
    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(run)
    handles[0].join()
    assert len(calls) == 2, calls
    assert row["outcome"] == "released", row
    assert world.events(run) == ["closing", "closed"]
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []
    assert world.states(run) == ["released"] and info["pane_id"] not in world.fake.panes


def test_a_gate_that_is_lost_after_the_close_does_not_stop_the_closed_line(
        world, monkeypatch):
    """Catches: a `closed` line that is written only by a reaper that holds the gate:
    the pane is closed, the gate is held by another process for the time of the line,
    and the pane's last line stays a `closing` that `hoh status` lists as unconfirmed."""
    run, info, run_dir = _race_world(world, "r-lost")
    monkeypatch.setattr(pane_ledger, "GATE_WAIT_S", 0.1)
    real_close_tab = world.fake.close_tab
    holding, release = threading.Event(), threading.Event()
    handles = []

    def holder():
        with pane_ledger.gate(run_dir) as ok:
            assert ok is True
            holding.set()
            assert release.wait(10)

    def close_tab(tab, *, purpose=""):
        real_close_tab(tab, purpose=purpose)          # the close takes ...
        handles.append(run_in_thread(holder))         # ... and the gate is taken after it
        assert holding.wait(10)

    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    try:
        row = world.one(run)
    finally:
        release.set()
        handles[0].join()
    assert row["outcome"] == "closed", row
    assert world.events(run) == ["closing", "closed"]
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []
    assert world.states(run) == ["released"]
    assert json.loads((run_dir / pane_ledger.LEDGER).read_text().splitlines()[-1])[
        "event"] == "closed"
