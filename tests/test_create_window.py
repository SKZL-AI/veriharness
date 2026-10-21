"""PFIX312a: the readers that judge the link count of a file `durable_create` publishes wait for the second name to go.

`durable_create` publishes by `os.link(temp, name)` and drops the temp name afterwards, so for an instant the whole file has
two names (measured: median 60 us, worst 30 ms). `durable_io.read_single_link` has looked at the descriptor again for up to
`SECOND_NAME_GRACE_S` since PFIX311; three sibling readers counted the names themselves and refused the file for that
instant: the run-lease lock (`RunLeases._locked`, the public CI of v0.4.0a2 halted a drive AMBIGUOUS on it), the preflight
report (`preflightgate._read_report`) and the failover record (`routing._read_failover`). They now ask `settled_stat`.

The window is made deterministic, with no thread and no luck: a seam on `durable_io._abandon_own_temp` (the step after the
link) runs the reader at the instant the file has two names, and a clock that only `sleep` moves lets the writer finish
(drop its temp name) at the reader's first look again, or never. Tests write only under `tmp_path`; they start no Herdr and
no agent and use no network.
"""
from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest
from test_failure_classes import repo as repo  # a fixture; the alias form is an explicit re-export, as `test_run_lease_drive` takes it
from test_run_lease_drive import world as world  # a fixture: the real parser and `cmd_run`, a stand-in iteration

from hoh import durable_io, preflightgate, routing, runlease

PAYLOAD = b'{"kind": "record"}\n'


class Clock:
    """`durable_io.time` as a clock that only `sleep` moves: a wait is measured in the time it asked for and takes none. `finish` is
    called once, at the first sleep that leaves `finish_at` seconds behind it: it is the writer dropping its temp name while the
    reader waits."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.finish = None
        self.finish_at = 0.0
        monkeypatch.setattr(durable_io, "time", SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep), raising=False)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        assert len(self.sleeps) < 10_000, "the wait did not stop"
        if self.finish is not None and self.now >= self.finish_at:
            hook, self.finish = self.finish, None
            hook()


class Site:
    """One reader under test and the writer of its file: `create()` publishes the file through `durable_create`, `read()` is the
    reader and returns what it returned (or raises `refused`), `record` is the name the reader opens."""

    refused: type[BaseException]
    value: object

    def __init__(self, tmp_path):
        self.dir = tmp_path / "site"
        self.dir.mkdir()


class LockSite(Site):
    """The run-lease lock: created by the first opener (`RunLeases._locked` -> `durable_create`), read by every opener."""

    refused = runlease.RunLeaseError
    value = "locked"

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.dir = self.dir / runlease.DIRNAME
        self.record = self.dir / runlease.LOCKNAME

    def create(self):
        with runlease.RunLeases(self.dir)._locked():
            pass

    def read(self):
        with runlease.RunLeases(self.dir)._locked():
            return "locked"


class ReportSite(Site):
    """The preflight report: published by `durable_create`, read by `check_preflight` through `_read_report`."""

    refused = preflightgate.PreflightRefused

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.record = self.dir / "preflight.json"
        self.value = PAYLOAD

    def create(self):
        durable_io.durable_create(self.record, PAYLOAD, mode=None)

    def read(self):
        return preflightgate._read_report(self.record)


class FailoverSite(Site):
    """The failover record (and the routing pin): created by `routing._create` -> `durable_create(dir_fd=)`, read by `_read_failover`."""

    refused = routing.RoutingRefused
    value = {"kind": "record"}

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.record = self.dir / routing.FAILOVER_FILE
        self.fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def close(self):
        os.close(self.fd)

    def create(self):
        routing._create(self.fd, routing.FAILOVER_FILE, PAYLOAD)

    def read(self):
        return routing._read_failover(self.fd)


@pytest.fixture(params=[LockSite, ReportSite, FailoverSite], ids=["lock", "report", "failover"])
def site(request, tmp_path):
    made = request.param(tmp_path)
    yield made
    if hasattr(made, "close"):
        made.close()


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def _in_the_window(monkeypatch, clock, site, *, finish_at):
    """Make every `durable_create` stop between its `os.link` and the drop of its temp name, and run `site.read()` there. Returns the
    box the reader's outcome is put in: `names` (the link count seen at that instant), `value` or `error`. The writer drops its temp
    name at the first look the reader takes again once `finish_at` seconds of waiting have passed; with `None` only after the
    reader has returned."""
    real = durable_io._abandon_own_temp
    box: dict = {}

    def abandon(fd, tmp_path, **kwargs):
        done = []

        def finish():
            if not done:
                done.append(real(fd, tmp_path, **kwargs))
        box["names"] = os.stat(site.record).st_nlink
        if finish_at is not None:
            clock.finish, clock.finish_at = finish, finish_at
        try:
            box["value"] = site.read()
        except BaseException as exc:              # the outcome is the test's to judge
            box["error"] = exc
        clock.finish = None
        finish()
        return done[0]
    monkeypatch.setattr(durable_io, "_abandon_own_temp", abandon)
    return box


def test_a_reader_that_arrives_between_the_link_and_the_drop_reads_the_whole_file_once_the_second_name_is_gone(
        site, monkeypatch, clock):
    """Catches the spurious refusal ("not a regular file with one name", "has 2 names, not one"): the file has two names while its
    creator is between `os.link` and the drop of its temp name; the reader looks again after a millisecond, the creator has finished,
    and the file is read. Exactly one look: the second name is gone at the first."""
    box = _in_the_window(monkeypatch, clock, site, finish_at=0.0)
    site.create()
    assert box["names"] == 2, "the reader did not meet the window"
    assert "error" not in box, repr(box.get("error"))
    assert box["value"] == site.value
    assert clock.sleeps == [0.001]
    assert os.stat(site.record).st_nlink == 1 and stat.S_ISREG(os.stat(site.record).st_mode)


def test_a_file_that_keeps_two_names_is_refused_after_a_quarter_of_a_second_and_a_third_name_at_once(site, monkeypatch, clock):
    """Catches an unbounded wait and a wait that is no wait: two names that stay (a hard link made on purpose, nothing is writing)
    are refused, after 0.25 s of 1 ms looks -- and a third name is not the instant `durable_create` leaves, it is refused without a
    look. A refusal writes nothing."""
    site.create()
    assert site.read() == site.value
    assert clock.sleeps == []                                       # one name: no look again
    os.link(site.record, site.dir / "elsewhere")
    assert os.stat(site.record).st_nlink == 2
    before = sorted(os.listdir(site.dir))
    with pytest.raises(site.refused):
        site.read()
    assert durable_io.SECOND_NAME_GRACE_S == 0.25
    assert 249 <= len(clock.sleeps) <= 251 and abs(sum(clock.sleeps) - 0.25) < 0.005
    clock.sleeps.clear()
    with pytest.raises(site.refused):                               # a further read starts again: the counter is a local of the call
        site.read()
    assert 249 <= len(clock.sleeps) <= 251 and abs(sum(clock.sleeps) - 0.25) < 0.005
    assert sorted(os.listdir(site.dir)) == before                   # and a refusal wrote nothing
    os.link(site.record, site.dir / "and-another")
    assert os.stat(site.record).st_nlink == 3
    clock.sleeps.clear()
    with pytest.raises(site.refused):
        site.read()
    assert clock.sleeps == []
    assert sorted(os.listdir(site.dir)) == sorted(before + ["and-another"])


@pytest.mark.parametrize("needs,read", [(0.2, True), (0.3, False)], ids=["inside-the-grace", "past-the-grace"])
def test_a_creator_that_is_slow_to_finish_is_waited_for_up_to_the_grace_and_not_longer(site, monkeypatch, clock, needs, read):
    """Catches a wait that gives up early and a wait that never ends: the creator is between link and drop and drops its temp name
    after `needs` seconds of the reader's waiting. At 200 ms, inside the 250 ms of the grace, the reader is not refused; at 300 ms it is."""
    box = _in_the_window(monkeypatch, clock, site, finish_at=needs)
    site.create()
    assert box["names"] == 2, "the reader did not meet the window"
    if read:
        assert "error" not in box, repr(box.get("error"))
        assert box["value"] == site.value
        assert abs(sum(clock.sleeps) - needs) < 0.005
    else:
        assert isinstance(box.get("error"), site.refused) and "value" not in box
        assert abs(sum(clock.sleeps) - 0.25) < 0.005


def test_two_runs_taking_their_leases_at_the_first_creation_of_the_directory_both_get_them(tmp_path, monkeypatch, clock):
    """Catches the failure the public CI met (`hoh project drive` halted AMBIGUOUS, "run lease refused (unreadable): .../run-leases/.lock
    is not a regular file with one name"): the first `acquire` creates the lock, a second `acquire` for another run opens it between the
    creator's link and drop. Both runs get their lease, generation 1, and the lock has one name afterwards."""
    directory = tmp_path / "project" / runlease.DIRNAME
    first, second = runlease.RunLeases(directory), runlease.RunLeases(directory)
    taken: dict = {}
    real = durable_io._abandon_own_temp

    def abandon(fd, tmp_path_, **kwargs):
        done = []

        def finish():
            if not done:
                done.append(real(fd, tmp_path_, **kwargs))
        clock.finish = finish
        try:
            taken["second"] = second.acquire("r2", "orch-b", ttl_seconds=600)
        except BaseException as exc:
            taken["error"] = exc
        clock.finish = None
        finish()
        return done[0]
    monkeypatch.setattr(durable_io, "_abandon_own_temp", abandon)
    taken["first"] = first.acquire("r1", "orch-a", ttl_seconds=600)
    assert "error" not in taken, repr(taken.get("error"))
    assert (taken["first"].run_id, taken["first"].generation) == ("r1", 1)
    assert (taken["second"].run_id, taken["second"].generation) == ("r2", 1)
    assert os.stat(directory / runlease.LOCKNAME).st_nlink == 1
    assert sorted(os.listdir(directory)) == [".lock", "r1.json", "r2.json"]


def test_hoh_run_takes_its_lease_while_another_opener_meets_the_lock_between_its_link_and_its_drop(world, monkeypatch, clock):
    """The production path (`cli.main` -> `cmd_run` -> `rundrive.drive_lease` -> `DriveLease.acquire` -> `RunLeases.acquire` ->
    `_locked`): `hoh run r1` creates the lock of a fresh runs root, and in the instant its temp name is still linked a second opener
    (another `hoh run`, or the broker's `hoh capacity check`: any process that takes a lease) takes the lease of `r2`. The command
    ends 0, both leases are there, and the lock has one name. The window is the one the public CI of v0.4.0a2 met."""
    real = durable_io._abandon_own_temp
    taken: dict = {}

    def abandon(fd, tmp_path, **kwargs):
        done = []

        def finish():
            if not done:
                done.append(real(fd, tmp_path, **kwargs))
        if tmp_path.parent == world.lease_dir and tmp_path.name.startswith("..lock.tmp."):
            taken["names"] = os.stat(world.lease_dir / runlease.LOCKNAME).st_nlink
            clock.finish, clock.finish_at = finish, 0.0
            try:
                taken["other"] = runlease.RunLeases(world.lease_dir).acquire("r2", "other-session", ttl_seconds=600)
            except BaseException as exc:
                taken["error"] = exc
            clock.finish = None
        finish()
        return done[0]
    monkeypatch.setattr(durable_io, "_abandon_own_temp", abandon)
    assert not world.lease_dir.exists()
    rc, _out, err = world.run()
    assert taken.get("names") == 2, "the second opener did not meet the window"
    assert "error" not in taken, repr(taken.get("error"))
    assert rc == 0, err
    assert taken["other"].run_id == "r2" and taken["other"].generation == 1
    assert world.leases.read("r1").generation == 1 and world.leases.read("r2").holder == "other-session"
    assert os.stat(world.lease_dir / runlease.LOCKNAME).st_nlink == 1
