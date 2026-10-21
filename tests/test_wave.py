"""P3-02 ParallelWaveExecutor and WaveRecord, slice 1.

Every test names the failure it catches. A test builds its own repository and
linked worktrees under `tmp_path`, starts no Herdr and no agent, reads nothing
outside `tmp_path`, and runs `launch` as a plain function of the test: there is
no run, no provider and no quota. The module is imported **inside** each test,
so that on a tree without it every node fails for one reason of its own.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

START = "2026-10-12T10:00:00Z"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("print('one')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    return repo


def make_worktree(repo, name):
    target = repo.parent / name
    git(repo, "worktree", "add", "-q", "-b", name, str(target))
    return target


def _node(node_id, **kw):
    from hoh.project import Lifecycle, TaskNode
    return TaskNode(id=node_id, lifecycle=kw.pop("lifecycle", Lifecycle.READY), **kw)


class Rig:
    """One wave's world: a repository with one linked worktree per node name, a
    runs root for the budget ledger, a record directory, a clock and a spend."""

    def __init__(self, tmp_path, names, *, ceiling=10, want=2, width=4, ttl=600):
        self.repo = make_repo(tmp_path)
        self.trees = {n: make_worktree(self.repo, n) for n in names}
        self.runs = tmp_path / "runs"
        self.records = tmp_path / "waves"
        self.records.mkdir()
        self.now = START
        self.spent: dict[str, int] = {}
        self.ceiling, self.want, self.width, self.ttl = ceiling, want, width, ttl
        self.nodes = {n: _node(n) for n in names}

    def handle(self, name, holder=None, ttl=None, tree=None):
        from hoh.wslease import WorkspaceLease
        return WorkspaceLease(tree if tree is not None else self.trees[name], holder or name,
                              ttl_seconds=ttl or self.ttl, clock=lambda: self.now)

    def lease_for(self, node):
        return self.handle(node.id)

    def reservations(self):
        from hoh.reservation import BudgetReservations
        return BudgetReservations(self.runs)

    def executor(self, **over):
        from hoh import wave
        from hoh.project import ActionClass
        args = dict(
            record_dir=self.records, width=self.width,
            budget=wave.WaveBudget(self.reservations(), self.ceiling,
                                   lambda: dict(self.spent), self.want),
            lease_for=self.lease_for, depends_on=lambda a, b: False,
            action_class=lambda n: ActionClass.INTERNAL, clock=lambda: self.now)
        args.update(over)
        return wave.ParallelWaveExecutor(**args)

    def plan(self, *names, deferred=()):
        from hoh.frontier import Deferral, FrontierPlan
        return FrontierPlan(
            tuple(self.nodes[n] for n in names),
            tuple(Deferral(self.nodes[a], self.nodes[b]) for a, b in deferred))

    def record_path(self, wave_id):
        return self.records / f"wave-{wave_id}.json"

    def ledger(self):
        return self.reservations().read()

    def lease_on_disk(self, name):
        return self.handle(name, holder="probe").read()

    def lease_dir(self, name):
        from hoh.workspace import lease_directory
        return lease_directory(self.trees[name])

    def assert_nothing_taken(self, *names):
        assert not os.path.lexists(self.runs / "_budget-reservations.json")
        assert sorted(os.listdir(self.records)) == []
        for n in names:
            assert not os.path.lexists(self.lease_dir(n))

    def assert_everything_given_back(self):
        from hoh.reservation import HoldStatus
        assert [r for r in self.ledger() if r.status is HoldStatus.HELD] == []
        for n in self.trees:
            lease = self.lease_on_disk(n)
            assert lease is None or lease.released_at, n


def fresh(tmp_path, name, names, **kw):
    """A second, independent rig under its own directory of `tmp_path`."""
    sub = tmp_path / name
    sub.mkdir()
    return Rig(sub, names, **kw)


def ok(verdict="ACCEPTED"):
    return SimpleNamespace(verdict=verdict)


def snapshot(nodes):
    return {n.id: n.model_dump() for n in nodes}


# --------------------------------------------------------------------------- #
# W1: the wave runs together, each node under its own hold and lease
# --------------------------------------------------------------------------- #

def test_independent_nodes_run_together_each_under_its_own_hold_and_lease(tmp_path):
    """Catches a wave that is serial (the barrier of three never fills), a node
    launched without its hold or its lease, a hold that is not the amount the
    node is told, a lease that is not live while the node runs, and a wave that
    leaves a hold open, a lease held or a thread behind."""
    from hoh import wave
    from hoh.orchestrator import RunVerdict
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=6, want=2)
    before = snapshot(rig.nodes.values())
    threads = threading.active_count()
    barrier = threading.Barrier(3, timeout=10)
    seen, returned = {}, {}

    def launch(node, adm):
        barrier.wait()                  # BrokenBarrierError unless all three are running
        held = [r for r in rig.ledger() if r.status is HoldStatus.HELD]
        # The write-ahead record is private while it says ADMITTED (the final write would mask it).
        assert stat.S_IMODE(os.lstat(rig.record_path("w1")).st_mode) == 0o600
        seen[node.id] = (adm, {r.run_id: r for r in held}, rig.lease_on_disk(node.id),
                         len(held))
        if node.id == "n1":
            rig.now = "2026-10-12T10:00:05Z"                  # the wave takes five seconds
        returned[node.id] = ok(RunVerdict.ACCEPTED if node.id == "n1" else "ACCEPTED")
        return returned[node.id]

    result = rig.executor().run_wave("w1", rig.plan("n1", "n2", "n3"), launch)

    rec = result.record
    assert rec.phase is wave.Phase.SETTLED and rec.wave_id == "w1"
    assert rec.created_at == START and rec.settled_at == "2026-10-12T10:00:05Z"
    assert (rec.width, rec.ceiling) == (4, 6)
    assert [n.node_id for n in rec.nodes] == ["n1", "n2", "n3"]
    assert all(n.state is wave.NodeState.RETURNED and n.verdict == "ACCEPTED" and n.usable
               for n in rec.nodes)
    assert rec.usable_ids == ("n1", "n2", "n3")
    assert set(result.outcomes) == {"n1", "n2", "n3"}
    assert all(result.outcomes[n] is returned[n] for n in returned)   # the object, not its verdict
    assert sorted(seen) == ["n1", "n2", "n3"]
    for name, (adm, held, lease, n_held) in seen.items():
        assert n_held == 3                               # all three holds, at once
        assert held[name].amount == adm.amount == 2      # the node is told what it holds
        assert adm.reservation_id == held[name].reservation_id
        assert adm.run_id == name and adm.wave_id == "w1" and adm.node is rig.nodes[name]
        assert adm.worktree == rig.trees[name].resolve()
        assert lease is not None and lease.holder == name and not lease.released_at
        assert lease.generation == adm.generation == 1
        row = rec.node(name)
        assert (row.amount, row.generation, row.reservation_id) == (2, 1, adm.reservation_id)
        assert (row.lease_held_at_end, row.lease_released, row.holds_ended) == (True, True, 1)
    # Everything is given back, nothing was kept by the wave, nothing was written elsewhere.
    assert [r.status for r in rig.ledger()] == [HoldStatus.SETTLED] * 3
    rig.assert_everything_given_back()
    assert threading.active_count() == threads
    assert snapshot(rig.nodes.values()) == before        # the executor writes no project state
    assert sorted(os.listdir(rig.records)) == ["wave-w1.json"]
    assert wave.read_record(rig.record_path("w1")) == rec
    for name in rig.trees:
        assert git(rig.trees[name], "status", "--porcelain") == ""


# --------------------------------------------------------------------------- #
# W2: the record is on disk before the first launch
# --------------------------------------------------------------------------- #

def test_the_record_is_on_disk_before_the_first_launch_and_a_wave_id_is_used_once(tmp_path,
                                                                                  monkeypatch):
    """Catches a record written after the launches, a wave that starts when its
    record cannot be written, a wave id that replaces an earlier record, a
    final record that fails silently, and a record directory that is a link."""
    from hoh import wave
    from hoh import durable_io

    rig = Rig(tmp_path, ["n1", "n2", "n3"])
    seen = {}

    def launch(node, adm):
        rec = wave.read_record(rig.record_path("w1"))
        seen[node.id] = rec
        return ok()

    plan = rig.plan("n1", "n2", deferred=[("n3", "n1")])
    result = rig.executor().run_wave("w1", plan, launch)
    for rec in seen.values():
        assert rec is not None and rec.phase is wave.Phase.ADMITTED and rec.settled_at is None
        assert [n.state for n in rec.nodes] == [wave.NodeState.ADMITTED] * 2
        assert all(n.reservation_id and n.amount == 2 and n.worktree for n in rec.nodes)
        assert [(d.node_id, d.blocked_by, d.reason) for d in rec.deferred] == [
            ("n3", "n1", "SEMANTIC_DEPENDENCY")]
    assert result.record.phase is wave.Phase.SETTLED
    assert result.record.deferred == seen["n1"].deferred
    path = rig.record_path("w1")
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert sorted(os.listdir(rig.records)) == ["wave-w1.json"]

    # A wave id is 1 to 64 characters of the grammar; the record is named after it and only it.
    from hoh.frontier import FrontierPlan
    for bad_id in ("", "w 1", "w/1", "../x", "w1\n", ".w", "w" * 65, None, 1):
        with pytest.raises(wave.WaveError):
            rig.executor().run_wave(bad_id, FrontierPlan(), lambda n, a: ok())
    assert sorted(os.listdir(rig.records)) == ["wave-w1.json"]
    longest = "w" * 64
    empty = rig.executor().run_wave(longest, FrontierPlan(), lambda n, a: ok())
    assert empty.record.nodes == () and empty.record.phase is wave.Phase.SETTLED
    assert wave.read_record(rig.record_path(longest)) == empty.record

    # A wave id is used once: nothing is held, leased, launched or replaced.
    ledger, bytes_ = rig.ledger(), path.read_bytes()
    calls = []
    with pytest.raises(wave.WaveError):
        rig.executor().run_wave("w1", rig.plan("n1"), lambda n, a: calls.append(n) or ok())
    assert calls == [] and rig.ledger() == ledger and path.read_bytes() == bytes_

    # The record cannot be written after admission: nothing launches, everything goes back.
    real = durable_io.durable_create

    def full(*a, **kw):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(wave, "durable_io", SimpleNamespace(
        durable_create=full, durable_write=durable_io.durable_write,
        read_single_link=durable_io.read_single_link))
    with pytest.raises(wave.WaveError, match="nothing was launched"):
        rig.executor().run_wave("w2", rig.plan("n1", "n2"), lambda n, a: calls.append(n) or ok())
    assert calls == [] and not os.path.lexists(rig.record_path("w2"))
    rig.assert_everything_given_back()
    assert rig.lease_on_disk("n1").generation == 2        # taken and given back, once

    # The final record cannot be written: the nodes ran and went back, and the caller is told.
    def broken(*a, **kw):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(wave, "durable_io", SimpleNamespace(
        durable_create=real, durable_write=broken, read_single_link=durable_io.read_single_link))
    with pytest.raises(wave.WaveNotSettled) as caught:
        rig.executor().run_wave("w3", rig.plan("n1", "n2"), lambda n, a: calls.append(n.id) or ok())
    assert sorted(calls) == ["n1", "n2"]
    assert caught.value.result.record.phase is wave.Phase.SETTLED
    assert [n.state for n in caught.value.result.record.nodes] == [wave.NodeState.RETURNED] * 2
    on_disk = durable_io.read_single_link(rig.record_path("w3"))
    assert wave.parse(on_disk).phase is wave.Phase.ADMITTED   # the safe reading: outcome unknown
    rig.assert_everything_given_back()

    # ... or the replace happened and only the sync of its directory failed: the file says SETTLED.
    def not_synced(path, payload, **kw):
        durable_io.durable_write(path, payload, **kw)
        raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "io"))

    monkeypatch.setattr(wave, "durable_io", SimpleNamespace(
        durable_create=real, durable_write=not_synced, read_single_link=durable_io.read_single_link))
    with pytest.raises(wave.WaveNotSettled):
        rig.executor().run_wave("w3b", rig.plan("n1", "n2"), lambda n, a: ok())
    assert wave.read_record(rig.record_path("w3b")).phase is wave.Phase.SETTLED
    rig.assert_everything_given_back()

    # A pending interrupt is not replaced by WaveNotSettled: it propagates, after the cleanup.
    monkeypatch.setattr(wave, "durable_io", SimpleNamespace(
        durable_create=real, durable_write=broken, read_single_link=durable_io.read_single_link))

    def exits(node, adm):
        raise SystemExit(7)

    with pytest.raises(SystemExit):
        rig.executor().run_wave("w3c", rig.plan("n1"), exits)
    assert wave.read_record(rig.record_path("w3c")).phase is wave.Phase.ADMITTED
    rig.assert_everything_given_back()
    monkeypatch.undo()

    # An unwritable record directory is the same refusal, after admission, with everything given back.
    if os.geteuid() != 0:
        rig.records.chmod(0o500)
        try:
            with pytest.raises(wave.WaveError, match="nothing was launched"):
                rig.executor().run_wave("w4", rig.plan("n1", "n2"), lambda n, a: calls.append(n) or ok())
        finally:
            rig.records.chmod(0o700)
        assert not os.path.lexists(rig.record_path("w4"))
        rig.assert_everything_given_back()

    # The record directory is a directory of its own.
    for bad in (tmp_path / "missing", tmp_path / "alias", tmp_path / "a-file"):
        if bad.name == "alias":
            bad.symlink_to(rig.records)
        elif bad.name == "a-file":
            bad.write_text("x", encoding="utf-8")
        before = rig.ledger()
        with pytest.raises(wave.WaveError):
            rig.executor(record_dir=bad).run_wave("w9", rig.plan("n1"), lambda n, a: ok())
        assert rig.ledger() == before


# --------------------------------------------------------------------------- #
# W3a: no hold, no launch
# --------------------------------------------------------------------------- #

def test_a_node_the_budget_cannot_hold_is_refused_and_never_launched(tmp_path):
    """Catches a node that runs without a hold, a hold granted past the ceiling,
    a wave that goes on when the ledger cannot be read, and a budget that is not
    whole again once the wave has settled."""
    from hoh import wave
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=4, want=2)
    launched = []
    result = rig.executor().run_wave(
        "w1", rig.plan("n1", "n2", "n3"), lambda n, a: launched.append(n.id) or ok())
    assert sorted(launched) == ["n1", "n2"]
    third = result.record.node("n3")
    assert third.state is wave.NodeState.REFUSED and third.refusal == "BUDGET"
    assert third.reservation_id is None and third.amount is None and third.generation is None
    assert [r.run_id for r in rig.ledger()] == ["n1", "n2"]       # no row for the refused one
    taken = rig.lease_on_disk("n3")                               # its lease was taken, then given back
    assert taken is not None and taken.generation == 1 and taken.released_at
    assert "budget was refused" in taken.released_reason

    # The same ceiling serves the next wave: what was held is back.
    launched.clear()
    again = rig.executor().run_wave(
        "w2", rig.plan("n1", "n2", "n3"), lambda n, a: launched.append(n.id) or ok())
    assert sorted(launched) == ["n1", "n2"] and again.record.node("n3").refusal == "BUDGET"

    # A node is granted what is left when that is less than it wants (and says so).
    rig2 = fresh(tmp_path, "second", ["n1", "n2", "n3"], ceiling=5, want=2)
    amounts = {}
    rig2.executor().run_wave(
        "w1", rig2.plan("n1", "n2", "n3"), lambda n, a: amounts.update({n.id: a.amount}) or ok())
    assert amounts == {"n1": 2, "n2": 2, "n3": 1}

    # What other runs already spent counts against the ceiling.
    rig3 = fresh(tmp_path, "third", ["n1", "n2"], ceiling=4, want=2)
    rig3.spent = {"someone": 3}
    got = {}
    r3 = rig3.executor().run_wave(
        "w1", rig3.plan("n1", "n2"), lambda n, a: got.update({n.id: a.amount}) or ok())
    assert got == {"n1": 1} and r3.record.node("n2").refusal == "BUDGET"

    # A ledger that cannot be read is not a budget of zero held: every node is refused.
    rig4 = fresh(tmp_path, "fourth", ["n1", "n2"], ceiling=4, want=2)
    rig4.runs.mkdir()
    (rig4.runs / "_budget-reservations.json").write_text("{not json", encoding="utf-8")
    calls = []
    r4 = rig4.executor().run_wave(
        "w1", rig4.plan("n1", "n2"), lambda n, a: calls.append(n.id) or ok())
    assert calls == []
    assert [n.refusal for n in r4.record.nodes] == ["BUDGET", "BUDGET"]
    assert r4.record.usable_ids == ()
    for name in ("n1", "n2"):
        assert rig4.lease_on_disk(name).released_at           # taken, found no budget, given back
    for each in (rig, rig2, rig3):
        assert HoldStatus.HELD not in {r.status for r in each.ledger()}


# --------------------------------------------------------------------------- #
# W3b: no lease, no launch, and the hold goes back
# --------------------------------------------------------------------------- #

def test_a_node_whose_worktree_is_not_free_is_refused_and_its_hold_is_returned(tmp_path):
    """Catches a node that runs in a worktree somebody else holds, two nodes of
    one wave in one worktree (a link is a second spelling), a hold stranded by a
    refused lease, a lease file that is read as 'no lease', and a lease factory
    that fails open."""
    from hoh import wave
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2", "n3", "n4", "n5"], ceiling=20, want=2)
    shared = rig.handle("n1")
    again = rig.executor(lease_for=lambda node: shared).run_wave(
        "w0", rig.plan("n1", "n2"), lambda n, a: ok())      # one handle for two nodes
    assert again.record.node("n1").state is wave.NodeState.RETURNED
    assert again.record.node("n2").refusal == "WORKSPACE"
    assert "already the worktree" in again.record.node("n2").detail
    rig.assert_everything_given_back()
    alias = tmp_path / "alias-of-n1"
    alias.symlink_to(rig.trees["n1"])
    rig.nodes["n1b"] = _node("n1b")
    other = rig.handle("n2", holder="someone-else")
    other.acquire()                                       # a live holder: n2 is not free
    corrupt = rig.lease_dir("n3")
    corrupt.mkdir()
    (corrupt / "lease.json").write_text("{\"schema\": \"nope\"}", encoding="utf-8")
    corrupt_bytes = (corrupt / "lease.json").read_bytes()

    def lease_for(node):
        if node.id == "n1b":
            return rig.handle("n1b", tree=alias)
        if node.id == "n4":
            raise RuntimeError("no worktree for n4")
        if node.id == "n5":
            return object()                               # not a WorkspaceLease
        return rig.handle(node.id)

    launched = []
    result = rig.executor(lease_for=lease_for).run_wave(
        "w1", rig.plan("n1", "n1b", "n2", "n3", "n4", "n5"),
        lambda n, a: launched.append(n.id) or ok())
    assert launched == ["n1"]
    rows = {n.node_id: n for n in result.record.nodes}
    assert rows["n1"].state is wave.NodeState.RETURNED
    for name in ("n1b", "n2", "n3", "n4", "n5"):
        assert rows[name].state is wave.NodeState.REFUSED, name
        assert rows[name].refusal == "WORKSPACE" and rows[name].generation is None
    assert "held" in rows["n2"].detail
    assert "RuntimeError" in rows["n4"].detail
    # The foreign lease and the odd file are exactly as they were.
    assert other.check().holder == "someone-else"
    assert (corrupt / "lease.json").read_bytes() == corrupt_bytes
    # The lease comes first: the refused nodes were never near the ledger.
    by_run = {r.run_id: r.status for r in rig.ledger()}
    assert set(by_run) == {"n1"}
    assert HoldStatus.HELD not in set(by_run.values())
    assert rig.lease_on_disk("n1").released_at


# --------------------------------------------------------------------------- #
# W4: the width and the argument bounds
# --------------------------------------------------------------------------- #

def test_the_width_and_the_argument_bounds_are_enforced_by_behaviour(tmp_path):
    """Catches a width that is not enforced, a width or amount that is a bool,
    a float or a string, a width of 17, a plan above 256 nodes or 1024
    deferrals, and a refusal that takes a hold before it refuses."""
    from hoh import wave
    from hoh.frontier import Deferral, FrontierPlan

    rig = Rig(tmp_path, ["n1", "n2", "n3"], width=2)
    launched = []
    result = rig.executor().run_wave(
        "w1", rig.plan("n1", "n2", "n3"), lambda n, a: launched.append(n.id) or ok())
    assert sorted(launched) == ["n1", "n2"]
    third = result.record.node("n3")
    assert third.refusal == "WIDTH" and third.state is wave.NodeState.REFUSED
    assert [r.run_id for r in rig.ledger()] == ["n1", "n2"]      # no hold for the node past the width
    assert not os.path.lexists(rig.lease_dir("n3"))
    assert result.record.width == 2

    launched.clear()
    one = rig.executor(width=1).run_wave(
        "w2", rig.plan("n1", "n2", "n3"), lambda n, a: launched.append(n.id) or ok())
    assert launched == ["n1"]
    assert [n.refusal for n in one.record.nodes] == ["", "WIDTH", "WIDTH"]

    for bad in (0, -1, True, 2.0, "2", None, 17):
        with pytest.raises(wave.WaveError):
            rig.executor(width=bad)
    assert rig.executor(width=16).width == 16
    assert rig.executor(width=1).width == 1

    def budget(**kw):
        base = dict(reservations=rig.reservations(), ceiling=4, spent=lambda: {}, want=2)
        base.update(kw)
        return wave.WaveBudget(**base)

    for kw in ({"want": 0}, {"want": True}, {"want": None}, {"want": "2"}, {"want": 2.0},
               {"ceiling": 0}, {"ceiling": True}, {"ceiling": None}, {"spent": None}):
        with pytest.raises(wave.WaveError):
            rig.executor(budget=budget(**kw))
    assert rig.executor(budget=budget(want=1, ceiling=1)).budget.want == 1
    assert rig.executor(budget=budget(want=2**31, ceiling=2**31)).budget.want == 2**31
    for kw in ({"want": 2**31 + 1}, {"ceiling": 2**31 + 1}):
        with pytest.raises(wave.WaveError):
            rig.executor(budget=budget(**kw))
    with pytest.raises(wave.WaveError):
        rig.executor(budget="a budget")

    # A plan a record cannot hold is refused whole, before anything is held.
    many = tuple(_node(f"m{i}") for i in range(257))
    with pytest.raises(wave.WaveError):
        rig.executor().run_wave("w3", FrontierPlan(many), lambda n, a: ok())
    deferrals = tuple(Deferral(many[1 + i % 200], many[0]) for i in range(1025))
    with pytest.raises(wave.WaveError):
        rig.executor().run_wave("w4", FrontierPlan((many[0],), deferrals), lambda n, a: ok())
    assert not os.path.lexists(rig.record_path("w3")) and not os.path.lexists(rig.record_path("w4"))
    assert [r.run_id for r in rig.ledger()] == ["n1", "n2", "n1"]   # only the waves above

    # ... and exactly 256 nodes with 1024 deferrals are not: they are written and read back.
    from hoh.project import ActionClass
    edge = rig.executor(action_class=lambda n: ActionClass.EXTERNAL).run_wave(
        "w5", FrontierPlan(many[:256], deferrals[:1024]), lambda n, a: ok())
    assert len(edge.record.nodes) == 256 and len(edge.record.deferred) == 1024
    assert {n.refusal for n in edge.record.nodes} == {"EXTERNAL"}
    assert wave.read_record(rig.record_path("w5")) == edge.record


# --------------------------------------------------------------------------- #
# W5: the plan is checked, not trusted
# --------------------------------------------------------------------------- #

def test_a_plan_that_is_not_a_frontier_is_refused_whole_and_odd_nodes_are_refused_alone(tmp_path):
    """Catches a wave that runs two nodes that conflict because the plan said
    so, one node twice, two nodes as one run, a node that is no longer READY, an
    irreversible action, and a run id that is not one."""
    from hoh import wave
    from hoh.frontier import FrontierPlan
    from hoh.project import ActionClass, Lifecycle

    rig = Rig(tmp_path, ["n1", "n2", "n3", "n4"])
    calls = []

    def launch(node, adm):
        calls.append(node.id)
        return ok()

    def run(plan, wave_id="w1", **over):
        return rig.executor(**over).run_wave(wave_id, plan, launch)

    # Not pairwise independent, in every spelling of "not provably independent".
    for answer in (lambda a, b: True, lambda a, b: None, lambda a, b: 0,
                   lambda a, b: a.id == "n2", lambda a, b: b.id == "n2"):
        with pytest.raises(wave.WaveError, match="independent"):
            run(rig.plan("n1", "n2"), depends_on=answer)
    # Malformed plans.
    n1 = rig.nodes["n1"]
    twin = _node("n1")
    shared_a, shared_b = _node("s1", run_id="shared"), _node("s2", run_id="shared")
    for plan in (FrontierPlan((n1, n1)), FrontierPlan((n1, twin)), FrontierPlan((shared_a, shared_b)),
                 FrontierPlan((_node("a b"),)), FrontierPlan((_node("n\u00e9"),)),
                 FrontierPlan((_node("n\x00"),)), FrontierPlan((_node("x" * 129),)), [n1], SimpleNamespace(selected=(n1,), deferred=()),
                 None):
        with pytest.raises(wave.WaveError):
            run(plan)
    for name in ("n1", "n2"):
        assert not os.path.lexists(rig.lease_dir(name))
    rig.assert_nothing_taken()
    assert calls == []

    # One node at a time is refused, the rest go on.
    blocked = _node("n2", lifecycle=Lifecycle.BLOCKED)
    external = _node("n3")
    odd_run = _node("n4", run_id="a/b")

    def action(node):
        return ActionClass.EXTERNAL if node.id == "n3" else ActionClass.INTERNAL

    rig.nodes.update(n2=blocked, n3=external, n4=odd_run)
    before = snapshot([blocked, external, odd_run])
    result = run(rig.plan("n1", "n2", "n3", "n4"), "w2", action_class=action)
    assert calls == ["n1"]
    rows = {n.node_id: n for n in result.record.nodes}
    assert [rows[k].refusal for k in ("n1", "n2", "n3", "n4")] == [
        "", "NOT_READY", "EXTERNAL", "RUN_ID"]
    assert rows["n4"].run_id is None
    assert snapshot([blocked, external, odd_run]) == before

    # A run id with a trailing newline is not a run id (`$` would let it through).
    odd = run(rig.plan("n1"), "w2c", run_id_of=lambda n: "n1\n")
    assert odd.record.node("n1").refusal == "RUN_ID" and calls == ["n1"]

    # An action class that cannot be read, or is not an ActionClass, is not INTERNAL.
    def raises(node):
        raise RuntimeError("no class")

    for i, fn in enumerate((raises, lambda n: None, lambda n: "INTERNAL")):
        calls.clear()
        res = run(rig.plan("n1"), f"x{i}", action_class=fn)
        assert calls == [] and res.record.node("n1").refusal == "EXTERNAL"
    rig.assert_everything_given_back()


# --------------------------------------------------------------------------- #
# W6: a launch that fails, and a lease that is lost
# --------------------------------------------------------------------------- #

def test_a_failing_launch_is_recorded_and_everything_is_given_back_whatever_happened(tmp_path):
    """Catches a node whose failure stops its siblings, leaves its hold open or
    its worktree held, an outcome with no verdict taken for a result, an
    interrupt that skips the cleanup, and a lease lost during the run that is
    reported as clean."""
    from hoh import wave

    rig = Rig(tmp_path, ["n1", "n2", "n3", "n4"], ceiling=20)

    def launch(node, adm):
        if node.id == "n2":
            raise ValueError("boom")
        if node.id == "n3":
            return SimpleNamespace()                      # no verdict
        if node.id == "n4":
            return ok("not a verdict!")
        return ok("PROVIDER_UNAVAILABLE")

    result = rig.executor().run_wave("w1", rig.plan("n1", "n2", "n3", "n4"), launch)
    rows = {n.node_id: n for n in result.record.nodes}
    assert rows["n1"].state is wave.NodeState.RETURNED and rows["n1"].verdict == "PROVIDER_UNAVAILABLE"
    for name in ("n2", "n3", "n4"):
        assert rows[name].state is wave.NodeState.ERRORED and rows[name].verdict == ""
        assert not rows[name].usable
    assert "ValueError: boom" in rows["n2"].detail
    assert set(result.outcomes) == {"n1"}
    assert result.record.usable_ids == ("n1",)
    assert all(n.lease_released and n.holds_ended == 1 for n in result.record.nodes)
    rig.assert_everything_given_back()

    # What came back may be hostile: a verdict that cannot be read is that node's error alone.
    class Hostile:
        @property
        def verdict(self):
            raise RuntimeError("no verdict for you")

    def hostile(node, adm):
        return Hostile() if node.id == "n1" else ok()

    res = rig.executor().run_wave("w1b", rig.plan("n1", "n2"), hostile)
    assert res.record.node("n1").state is wave.NodeState.ERRORED
    assert res.record.node("n2").state is wave.NodeState.RETURNED
    rig.assert_everything_given_back()

    # A verdict name is at most 64 characters: 64 is a result, 65 is that node's error (and the record
    # that is written must read back).
    def long_names(node, adm):
        return ok("V" * 64) if node.id == "n1" else ok("V" * 65)

    res = rig.executor().run_wave("w1c", rig.plan("n1", "n2"), long_names)
    assert res.record.node("n1").state is wave.NodeState.RETURNED
    assert res.record.node("n1").verdict == "V" * 64
    assert res.record.node("n2").state is wave.NodeState.ERRORED
    assert wave.read_record(rig.record_path("w1c")) == res.record
    rig.assert_everything_given_back()

    # An interrupt in a launch propagates, after everything has been given back and recorded.
    def interrupted(node, adm):
        if node.id == "n2":
            raise SystemExit(3)
        return ok()

    with pytest.raises(SystemExit):
        rig.executor().run_wave("w2", rig.plan("n1", "n2"), interrupted)
    rig.assert_everything_given_back()
    rec = wave.read_record(rig.record_path("w2"))
    assert rec.phase is wave.Phase.SETTLED
    assert rec.node("n2").state is wave.NodeState.ERRORED
    assert rec.node("n1").state is wave.NodeState.RETURNED

    # No thread can be started: everything admitted is given back and the error propagates.
    import hoh.wave as wave_module
    real_pool = wave_module.ThreadPoolExecutor

    class NoThreads:
        def __init__(self, *a, **kw):
            raise RuntimeError("can't start new thread")

    wave_module.ThreadPoolExecutor = NoThreads
    try:
        with pytest.raises(RuntimeError, match="thread"):
            rig.executor().run_wave("w2b", rig.plan("n1", "n2"), lambda n, a: ok())
    finally:
        wave_module.ThreadPoolExecutor = real_pool
    rig.assert_everything_given_back()
    assert wave.read_record(rig.record_path("w2b")).phase is wave.Phase.ADMITTED

    # A lease that ran out during the run: the verdict came back, the outcome is not usable.
    short = {"n1": 60}

    def lease_for(node):
        return rig.handle(node.id, ttl=short.get(node.id))

    def slow(node, adm):
        if node.id == "n1":
            rig.now = "2026-10-12T10:02:00Z"              # n1's 60 s lease has run out; n2's has not
        return ok()

    res = rig.executor(lease_for=lease_for).run_wave("w3", rig.plan("n1", "n2"), slow)
    lost, kept = res.record.node("n1"), res.record.node("n2")
    assert lost.state is wave.NodeState.RETURNED and lost.verdict == "ACCEPTED"
    assert (lost.lease_held_at_end, lost.lease_released, lost.usable) == (False, False, False)
    assert lost.holds_ended == 1                          # the hold is ended regardless
    assert (kept.lease_held_at_end, kept.lease_released, kept.usable) == (True, True, True)
    assert res.record.usable_ids == ("n2",)


# --------------------------------------------------------------------------- #
# W7: the record is strict
# --------------------------------------------------------------------------- #

def _good_record():
    from hoh import wave
    refused = wave.WaveNode("n1", "n1", wave.NodeState.REFUSED, refusal="WIDTH", detail="full")
    returned = wave.WaveNode(
        "n2", "n2", wave.NodeState.RETURNED, reservation_id="ab12cd34-n2", amount=2,
        worktree="/tmp/x/n2", generation=3, verdict="ACCEPTED", lease_held_at_end=True,
        lease_released=True, holds_ended=1)
    errored = wave.WaveNode(
        "n3", "n3", wave.NodeState.ERRORED, reservation_id="ab12cd34-n3", amount=1,
        worktree="/tmp/x/n3", generation=1, detail="ValueError: boom", lease_held_at_end=False,
        lease_released=False, holds_ended=None)
    return wave.WaveRecord(
        "w-1.a", wave.Phase.SETTLED, "2026-10-12T10:00:00Z", "2026-10-12T10:05:00Z", 4, 6,
        (refused, returned, errored), (wave.WaveDeferral("n4", "n2", "SEMANTIC_DEPENDENCY"),))


def test_the_record_round_trips_and_parse_refuses_everything_that_is_not_one():
    """Catches a record that does not read back as written, a bool taken for a
    count, a field that may be missing, extra or repeated, a state the fields
    contradict, text that is not printable ASCII, and a size bound that is not
    one."""
    from hoh import wave

    rec = _good_record()
    raw = rec.to_bytes()
    assert wave.parse(raw) == rec
    assert raw == wave.parse(raw).to_bytes() and raw.endswith(b"\n") and raw.isascii()

    def variant(fn):
        doc = json.loads(raw)
        fn(doc)
        return json.dumps(doc).encode("utf-8")

    def node(doc, i=1):
        return doc["nodes"][i]

    odd = {
        "schema": variant(lambda d: d.update(schema="hoh-wave-record/2")),
        "extra key": variant(lambda d: d.update(extra=1)),
        "missing key": variant(lambda d: d.pop("ceiling")),
        "phase": variant(lambda d: d.update(phase="DONE")),
        "settled with no time": variant(lambda d: d.update(settled_at=None)),
        "time month 13": variant(lambda d: d.update(created_at="2026-13-01T00:00:00Z")),
        "time with a space": variant(lambda d: d.update(created_at="2026-10-12 10:00:00")),
        "time newline": variant(lambda d: d.update(created_at="2026-10-12T10:00:00Z\n")),
        "wave id space": variant(lambda d: d.update(wave_id="w 1")),
        "wave id newline": variant(lambda d: d.update(wave_id="w1\n")),
        "wave id empty": variant(lambda d: d.update(wave_id="")),
        "wave id 65": variant(lambda d: d.update(wave_id="w" * 65)),
        "width bool": variant(lambda d: d.update(width=True)),
        "width 0": variant(lambda d: d.update(width=0)),
        "width 17": variant(lambda d: d.update(width=17)),
        "width str": variant(lambda d: d.update(width="4")),
        "width float": variant(lambda d: d.update(width=4.0)),
        "ceiling 0": variant(lambda d: d.update(ceiling=0)),
        "nodes not a list": variant(lambda d: d.update(nodes={})),
        "deferred not a list": variant(lambda d: d.update(deferred="x")),
        "node state": variant(lambda d: node(d).update(state="DONE")),
        "node id space": variant(lambda d: node(d).update(node_id="a b")),
        "node id newline": variant(lambda d: node(d).update(node_id="n2\n")),
        "node twice": variant(lambda d: d["nodes"].append(dict(node(d)))),
        "node extra key": variant(lambda d: node(d).update(extra=1)),
        "amount bool": variant(lambda d: node(d).update(amount=True)),
        "amount 0": variant(lambda d: node(d).update(amount=0)),
        "generation 0": variant(lambda d: node(d).update(generation=0)),
        "refusal unknown": variant(lambda d: node(d, 0).update(refusal="BOGUS")),
        "refused with a hold": variant(lambda d: node(d, 0).update(reservation_id="x-n1")),
        "refused with a verdict": variant(lambda d: node(d, 0).update(verdict="ACCEPTED")),
        "returned with no verdict": variant(lambda d: node(d).update(verdict="")),
        "errored with a verdict": variant(lambda d: node(d, 2).update(verdict="ACCEPTED")),
        "verdict lower case": variant(lambda d: node(d).update(verdict="accepted")),
        "finished with no lease": variant(lambda d: node(d).update(lease_released=None)),
        "admitted in a settled record": variant(lambda d: node(d).update(
            state="ADMITTED", verdict="", lease_held_at_end=None, lease_released=None,
            holds_ended=None)),
        "returned in an unsettled record": variant(lambda d: d.update(phase="ADMITTED",
                                                                      settled_at=None)),
        "refused and refusal empty": variant(lambda d: node(d, 0).update(refusal="")),
        "detail NUL": variant(lambda d: node(d).update(detail="a\u0000b")),
        "detail lone surrogate": variant(lambda d: node(d).update(detail="\ud800")),
        "detail non-ASCII": variant(lambda d: node(d).update(detail="café")),
        "detail newline": variant(lambda d: node(d).update(detail="a\nb")),
        "detail 301": variant(lambda d: node(d).update(detail="x" * 301)),
        "deferral key": variant(lambda d: d["deferred"][0].pop("reason")),
        "deferral reason": variant(lambda d: d["deferred"][0].update(reason="semantic")),
        "deferral id": variant(lambda d: d["deferred"][0].update(blocked_by="a b")),
    }
    for name, data in odd.items():
        with pytest.raises(wave.WaveRecordError):
            wave.parse(data)
        assert name
    text = raw.decode("ascii")
    for name, data in {
        "repeated key": text.replace('"width": 4', '"width": 4, "width": 4').encode(),
        "NaN": text.replace('"ceiling": 6', '"ceiling": NaN').encode(),
        "Infinity": text.replace('"ceiling": 6', '"ceiling": Infinity').encode(),
        "not UTF-8": b"\xff\xfe{}",
        "UTF-16": text.encode("utf-16"),
        "empty": b"",
        "a list": b"[]",
        "null": b"null",
        "a string": b'"x"',
        "a number": b"1",
        "truncated": raw[: len(raw) // 2],
        "not bytes": text,
    }.items():
        with pytest.raises(wave.WaveRecordError):
            wave.parse(data)
    # Every upper bound of the tables, to the character and to the unit: the bound parses, one more refuses.
    def at(path, value):
        def edit(d):
            target = d
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return variant(edit)

    bounds = [
        (("ceiling",), 2**31), (("nodes", 1, "amount"), 2**31), (("nodes", 1, "generation"), 2**40),
        (("nodes", 1, "holds_ended"), 2**31), (("nodes", 1, "worktree"), "x" * 512),
        (("nodes", 1, "run_id"), "r" * 255), (("nodes", 1, "reservation_id"), "a" * 300),
        (("nodes", 1, "detail"), "d" * 300), (("nodes", 1, "verdict"), "V" * 64),
        (("nodes", 1, "node_id"), "n" * 128), (("deferred", 0, "reason"), "R" * 40),
        (("deferred", 0, "node_id"), "n" * 128), (("deferred", 0, "blocked_by"), "b" * 128),
        (("wave_id",), "w" * 64), (("width",), 16),
    ]
    over = {("ceiling",): 2**31 + 1, ("nodes", 1, "amount"): 2**31 + 1,
            ("nodes", 1, "generation"): 2**40 + 1, ("nodes", 1, "holds_ended"): 2**31 + 1,
            ("nodes", 1, "worktree"): "x" * 513, ("nodes", 1, "run_id"): "r" * 256,
            ("nodes", 1, "reservation_id"): "a" * 301, ("nodes", 1, "detail"): "d" * 301,
            ("nodes", 1, "verdict"): "V" * 65, ("nodes", 1, "node_id"): "n" * 129,
            ("deferred", 0, "reason"): "R" * 41, ("deferred", 0, "node_id"): "n" * 129,
            ("deferred", 0, "blocked_by"): "b" * 129, ("wave_id",): "w" * 65, ("width",): 17}
    for path, ok_value in bounds:
        assert wave.parse(at(path, ok_value)) is not None, path
        with pytest.raises(wave.WaveRecordError):
            wave.parse(at(path, over[path]))
    with pytest.raises(wave.WaveRecordError):
        wave.parse(at(("nodes", 1, "holds_ended"), -1))
    assert wave.parse(at(("nodes", 1, "holds_ended"), 0)).nodes[1].holds_ended == 0

    # The size bound is one mebibyte of bytes, to the byte.
    pad = wave.MAX_RECORD_BYTES - len(raw)
    assert wave.MAX_RECORD_BYTES == 1048576
    assert wave.parse(raw + b" " * pad) == rec
    with pytest.raises(wave.WaveRecordError):
        wave.parse(raw + b" " * (pad + 1))


def test_a_record_file_that_is_not_one_is_never_read_as_no_wave(tmp_path):
    """Catches a link, a dangling link, a directory, a FIFO, a second name, an
    unreadable file and an oversize file that read as 'no such wave'."""
    from hoh import wave

    rec = _good_record()
    path = tmp_path / "wave-w.json"
    assert wave.read_record(path) is None
    path.write_bytes(rec.to_bytes())
    assert wave.read_record(path) == rec

    def odd(name):
        return tmp_path / name

    odd("link").symlink_to(path)
    odd("dangling").symlink_to(tmp_path / "nothing")
    odd("dir").mkdir()
    os.mkfifo(odd("fifo"))
    os.link(path, odd("second-name"))
    big = odd("big")
    big.write_bytes(b" " * (wave.MAX_RECORD_BYTES + 1))
    junk = odd("junk")
    junk.write_bytes(b"{}")
    for name in ("link", "dangling", "dir", "fifo", "second-name", "big", "junk"):
        with pytest.raises(wave.WaveRecordError):
            wave.read_record(odd(name))
    if os.geteuid() != 0:
        locked = odd("locked")
        locked.write_bytes(rec.to_bytes())
        locked.chmod(0)
        try:
            with pytest.raises(wave.WaveRecordError):
                wave.read_record(locked)
        finally:
            locked.chmod(0o600)


# --------------------------------------------------------------------------- #
# W8: the shape of this module
# --------------------------------------------------------------------------- #

def test_the_module_writes_only_through_durable_io_and_starts_nothing_else():
    """Pins the shape of this slice's module (an equivalent module changes the
    sets in the same hunk): no raw write, no subprocess, no clock of its own, no
    caller above it and no module of the controller layer below it."""
    tree = ast.parse((ROOT / "src" / "hoh" / "wave.py").read_text(encoding="utf-8"))
    plain, froms = set(), {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            plain |= {a.name for a in n.names}
            assert all(a.asname is None for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            froms.setdefault((n.level, n.module), set()).update(a.name for a in n.names)
            assert all(a.asname is None for a in n.names)
    assert plain == {"json", "os", "re", "stat"}
    assert {k: sorted(v) for k, v in froms.items()} == {
        (0, "__future__"): ["annotations"],
        (0, "collections.abc"): ["Callable", "Mapping"],
        (0, "concurrent.futures"): ["ThreadPoolExecutor"],
        (0, "dataclasses"): ["asdict", "dataclass", "replace"],
        (0, "datetime"): ["UTC", "datetime"],
        (0, "enum"): ["StrEnum"],
        (0, "pathlib"): ["Path"],
        (1, None): ["durable_io"],
        (1, "contracts"): ["utcnow"],
        (1, "frontier"): ["FrontierPlan", "ReadyFrontierPlanner"],
        (1, "project"): ["ActionClass", "Lifecycle", "TaskNode"],
        (1, "reservation"): ["BudgetReservations", "check_run_id"],
        (1, "wslease"): ["WorkspaceLease"],
    }
    os_attrs, durable, bare = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id == "os":
                os_attrs.add(n.attr)
            if n.value.id == "durable_io":
                durable.add(n.attr)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            bare.add(n.func.id)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute):
            if isinstance(n.value.value, ast.Name) and n.value.value.id == "os":
                os_attrs.add(f"{n.value.attr}.{n.attr}")
    assert os_attrs <= {"lstat", "path", "path.lexists"}
    assert durable == {"durable_create", "durable_write", "read_single_link"}
    assert not bare & {"open", "exec", "eval", "compile", "print", "__import__", "input"}


# --------------------------------------------------------------------------- #
# W9: a refused node never touches a hold or a lease it does not own
# --------------------------------------------------------------------------- #

def test_a_refused_node_never_touches_a_hold_or_a_lease_it_does_not_own(tmp_path):
    """Catches a refused node that supersedes and then settles the open hold of
    the same run id (the double-spend the reservation exists to close), a node
    that reserves over a hold some other caller holds, and a lease that is not
    given back when the budget refuses."""
    from hoh import wave
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=20, want=2)
    second = tmp_path / "waves-second"
    second.mkdir()
    done = threading.Event()
    seen = {}

    def rows():
        return sorted((r.run_id, r.reservation_id, r.amount, r.status.value) for r in rig.ledger())

    def launch(node, adm):
        if node.id == "n2":
            assert done.wait(20)
            return ok()
        # A second wave over the nodes of the running one: both refused, neither hold touched.
        seen["before"] = rows()
        seen["inner"] = rig.executor(record_dir=second).run_wave(
            "w2", rig.plan("n1", "n2"), lambda n, a: pytest.fail("launched"))
        seen["after"] = rows()
        done.set()
        return ok()

    result = rig.executor().run_wave("w1", rig.plan("n1", "n2"), launch)
    assert [(n.state, n.refusal) for n in seen["inner"].record.nodes] == [
        (wave.NodeState.REFUSED, "WORKSPACE")] * 2
    assert [r[3] for r in seen["before"]] == ["HELD", "HELD"]
    assert seen["after"] == seen["before"]               # same ids, same amounts, still HELD
    assert [n.holds_ended for n in result.record.nodes] == [1, 1]
    assert [r[3] for r in rows()] == ["SETTLED", "SETTLED"]
    rig.assert_everything_given_back()

    # A hold of the same run id that this wave did not take: the node is refused, the hold is untouched,
    # and the lease it had just taken is given back.
    other = rig.reservations().reserve("n1", ceiling=20, spent=lambda: {}, want=3)
    calls = []
    res = rig.executor().run_wave("w3", rig.plan("n1", "n2"),
                                  lambda n, a: calls.append(n.id) or ok())
    assert calls == ["n2"]
    refused = res.record.node("n1")
    assert (refused.state, refused.refusal) == (wave.NodeState.REFUSED, "BUDGET")
    assert "already holds" in refused.detail
    held = [r for r in rig.ledger() if r.run_id == "n1" and r.status is HoldStatus.HELD]
    assert [(r.reservation_id, r.amount) for r in held] == [(other.reservation_id, 3)]
    lease = rig.lease_on_disk("n1")
    assert lease is not None and lease.released_at and lease.generation == 2

    # The same with the worktree held by somebody else: the foreign hold and the foreign lease both stay.
    foreign_hold = rig.reservations().reserve("n2", ceiling=20, spent=lambda: {}, want=4)
    foreign_lease = rig.handle("n2", holder="someone-else")
    foreign_lease.acquire()
    res = rig.executor().run_wave("w4", rig.plan("n2"), lambda n, a: pytest.fail("launched"))
    assert res.record.node("n2").refusal == "WORKSPACE"
    held = [r for r in rig.ledger() if r.run_id == "n2" and r.status is HoldStatus.HELD]
    assert [(r.reservation_id, r.amount) for r in held] == [(foreign_hold.reservation_id, 4)]
    assert foreign_lease.check().holder == "someone-else"


# --------------------------------------------------------------------------- #
# W10: an interrupt while admitting gives back what was taken
# --------------------------------------------------------------------------- #

def test_an_interrupt_while_admitting_gives_back_everything_that_was_taken(tmp_path):
    """Catches an admission that keeps the holds and leases of the nodes admitted
    before an interrupt, and a node whose lease is kept when the interrupt comes
    from the reservation."""
    from hoh import wave
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=20, want=2)

    def lease_for(node):
        if node.id == "n2":
            raise KeyboardInterrupt
        return rig.handle(node.id)

    calls = []
    with pytest.raises(KeyboardInterrupt):
        rig.executor(lease_for=lease_for).run_wave(
            "w1", rig.plan("n1", "n2", "n3"), lambda n, a: calls.append(n.id) or ok())
    assert calls == [] and sorted(os.listdir(rig.records)) == []
    assert [(r.run_id, r.status) for r in rig.ledger()] == [("n1", HoldStatus.SETTLED)]
    rig.assert_everything_given_back()

    # The interrupt comes from inside the reservation (the spend is read under its lock).
    reads = []

    def spent():
        reads.append(1)
        if len(reads) == 2:
            raise KeyboardInterrupt
        return {}

    rig.spent = None
    ex = rig.executor(budget=wave.WaveBudget(rig.reservations(), 20, spent, 2))
    with pytest.raises(KeyboardInterrupt):
        ex.run_wave("w2", rig.plan("n1", "n2", "n3"), lambda n, a: calls.append(n.id) or ok())
    assert calls == [] and sorted(os.listdir(rig.records)) == []
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}
    rig.assert_everything_given_back()
    assert rig.lease_on_disk("n2").released_at            # taken before the reservation, given back
    # the ledger lock was left free: a new hold is granted at once
    assert rig.reservations().reserve("n3", ceiling=20, spent=lambda: {}, want=1).amount == 1


# --------------------------------------------------------------------------- #
# W11: the lease is judged when the node ended
# --------------------------------------------------------------------------- #

def test_a_nodes_lease_is_judged_when_that_node_ended_not_when_the_wave_did(tmp_path):
    """Catches a node that is reported as having lost its lease because a slower
    sibling kept the wave open past the lease, and a node that keeps its
    worktree until the whole wave has joined."""
    rig = Rig(tmp_path, ["n1", "n2"], ceiling=20, ttl=60)
    freed_while_n2_ran = []

    def launch(node, adm):
        if node.id == "n1":
            return ok()
        for _ in range(500):                              # up to 5 s for n1 to be given back
            lease = rig.lease_on_disk("n1")
            if lease is not None and lease.released_at:
                freed_while_n2_ran.append(True)
                break
            time.sleep(0.01)
        rig.now = "2026-10-12T10:02:00Z"                  # past both 60 s leases
        return ok()

    res = rig.executor().run_wave("w1", rig.plan("n1", "n2"), launch)
    first, slow = res.record.node("n1"), res.record.node("n2")
    assert freed_while_n2_ran == [True]                   # n1 was already free while n2 was running
    assert (first.lease_held_at_end, first.lease_released, first.usable) == (True, True, True)
    assert (slow.lease_held_at_end, slow.lease_released, slow.usable) == (False, False, False)
    assert res.record.usable_ids == ("n1",)


# --------------------------------------------------------------------------- #
# W12: the record names the worktree it leases, or the node is not admitted
# --------------------------------------------------------------------------- #

def _path_of_length(base, total):
    """A not-yet-existing path of exactly `total` characters below `base`."""
    rest, parts = total - len(str(base)), []
    while rest > 0:
        take = rest - 1 if rest <= 201 else (199 if rest == 202 else 200)
        parts.append("d" * take)
        rest -= take + 1
    path = base.joinpath(*parts)
    assert len(str(path)) == total
    return path


def test_the_record_names_the_worktree_it_leases_or_the_node_is_not_admitted(tmp_path):
    """Catches a record that garbles a non-ASCII worktree path or cuts a long one
    at 512 characters while the node runs in it, so that nothing reading the
    record can find the lease."""
    from hoh import wave

    rig = Rig(tmp_path, ["n1"], ceiling=20)
    base = tmp_path.resolve()
    accented = base / "w\u00e9-tree"
    exact = _path_of_length(base, 512)
    longer = _path_of_length(base, 513)
    for target in (accented, exact, longer):
        target.parent.mkdir(parents=True, exist_ok=True)
        git(rig.repo, "worktree", "add", "-q", "-b", f"b{abs(hash(str(target))) % 10**8}", str(target))
    trees = {"a": accented, "e": exact, "l": longer, "n1": rig.trees["n1"]}
    for key in ("a", "e", "l"):
        rig.nodes[key] = _node(key)

    def lease_for(node):
        return rig.handle(node.id, tree=trees[node.id])

    launched = []
    res = rig.executor(lease_for=lease_for, width=4).run_wave(
        "w1", rig.plan("n1", "a", "e", "l"), lambda n, a: launched.append(n.id) or ok())
    assert sorted(launched) == ["e", "n1"]
    assert res.record.node("n1").worktree == str(rig.trees["n1"].resolve())
    assert res.record.node("e").worktree == str(exact) and len(str(exact)) == 512
    for key in ("a", "l"):
        row = res.record.node(key)
        assert (row.state, row.refusal) == (wave.NodeState.REFUSED, "WORKSPACE"), key
        assert "record could not name it" in row.detail
    # Never acquired, so nothing of the refused worktrees was touched, and no hold was taken for them.
    from hoh.workspace import lease_directory
    assert not os.path.lexists(lease_directory(accented))
    assert not os.path.lexists(lease_directory(longer))
    assert sorted({r.run_id for r in rig.ledger()}) == ["e", "n1"]
    assert wave.read_record(rig.record_path("w1")) == res.record


# --------------------------------------------------------------------------- #
# P3-02f: the repair slice -- faults that strike after a write has happened
# --------------------------------------------------------------------------- #

def _acquire_log_of(rig, name):
    """True for the `durable_append` of the `acquire` line of `name`'s lease log."""
    def match(path, line):
        return str(rig.lease_dir(name)) in str(path) and '"event": "acquire"' in str(line)
    return match


def test_a_lease_written_before_acquire_raised_is_released_and_the_node_refused(tmp_path,
                                                                                monkeypatch):
    """Catches a WORKSPACE refusal that keeps the lease `acquire` wrote before its
    log line failed: the node is refused, the worktree stays held until the TTL
    and nobody can take it (repair finding A-F1)."""
    from hoh import durable_io, wave
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=10, want=2)
    real, hits = durable_io.durable_append, []
    is_n1 = _acquire_log_of(rig, "n1")

    def flaky(path, line, *a, **kw):
        if is_n1(path, line):
            hits.append(1)
            raise OSError(errno.ENOSPC, "injected: no space for the lease log line")
        return real(path, line, *a, **kw)

    monkeypatch.setattr(durable_io, "durable_append", flaky)
    calls = []
    res = rig.executor().run_wave("w1", rig.plan("n1", "n2"),
                                  lambda n, a: calls.append(n.id) or ok())
    monkeypatch.undo()
    assert hits == [1]
    first, second = res.record.node("n1"), res.record.node("n2")
    assert (first.state, first.refusal) == (wave.NodeState.REFUSED, "WORKSPACE")
    assert "OSError" in first.detail and first.generation is None
    assert second.state is wave.NodeState.RETURNED and calls == ["n2"]     # the wave goes on
    lease = rig.lease_on_disk("n1")
    assert lease is not None and lease.released_at and "refused" in lease.released_reason
    assert rig.handle("n1", holder="somebody-else").acquire().generation == 2   # the worktree is free
    assert {r.run_id for r in rig.ledger()} == {"n2"}                    # never near the ledger
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}
    assert wave.read_record(rig.record_path("w1")) == res.record


def test_an_interrupt_inside_acquire_gives_back_the_lease_it_wrote_and_everything_before(
        tmp_path, monkeypatch):
    """Catches a `KeyboardInterrupt` from inside `acquire` (after the lease was
    written) that propagates with the lease live and in no list: C4 says an
    interrupt during admission gives back what was taken (A-F1, B-F1)."""
    from hoh import durable_io
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=10, want=2)
    real = durable_io.durable_append
    is_n2 = _acquire_log_of(rig, "n2")

    def interrupted(path, line, *a, **kw):
        if is_n2(path, line):
            raise KeyboardInterrupt
        return real(path, line, *a, **kw)

    monkeypatch.setattr(durable_io, "durable_append", interrupted)
    calls = []
    with pytest.raises(KeyboardInterrupt):
        rig.executor().run_wave("w1", rig.plan("n1", "n2", "n3"),
                                lambda n, a: calls.append(n.id) or ok())
    monkeypatch.undo()
    assert calls == [] and sorted(os.listdir(rig.records)) == []
    rig.assert_everything_given_back()
    assert rig.lease_on_disk("n2").released_at           # written, then given back
    assert not os.path.lexists(rig.lease_dir("n3"))
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}


def test_an_acquire_whose_lease_the_handle_cannot_release_is_reported_not_hidden(tmp_path,
                                                                                 monkeypatch):
    """Pins the limit L13 (A-F3): when the directory sync after the lease's
    replace fails, `acquire` raises before it sets its token, so the handle
    cannot release what is on disk. The node is refused and the record says
    that a lease may remain -- "nothing was taken" is never written then."""
    from hoh import durable_io, wave

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=10, want=2)
    real, fired = durable_io._fsync_parent, []
    target = str(rig.lease_dir("n1") / "lease.json")

    def not_synced(path, **kw):
        if str(path) == target and not fired:
            fired.append(1)
            raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "y" * 400))
        return real(path, **kw)

    monkeypatch.setattr(durable_io, "_fsync_parent", not_synced)
    calls = []
    res = rig.executor().run_wave("w1", rig.plan("n1", "n2"),
                                  lambda n, a: calls.append(n.id) or ok())
    monkeypatch.undo()
    assert fired == [1] and calls == ["n2"]
    row = res.record.node("n1")
    assert (row.state, row.refusal) == (wave.NodeState.REFUSED, "WORKSPACE")
    lease = rig.lease_on_disk("n1")
    live = lease is not None and not lease.released_at
    assert ("stays until it runs out" in row.detail) == live      # reported exactly when it is true
    assert {r.run_id for r in rig.ledger()} == {"n2"}


def test_a_hold_written_before_reserve_raised_is_ended_and_the_retry_is_not_refused(tmp_path,
                                                                                    monkeypatch):
    """Catches a BUDGET refusal that leaves the HELD row `reserve` wrote before
    its directory sync failed: the budget is held by nobody and the executor's
    own rule then refuses every retry of that run id (repair finding A-F2)."""
    from hoh import durable_io, wave
    from hoh.reservation import HoldStatus

    real = durable_io._fsync_parent

    def ledger_sync(armed):
        def sync(path, **kw):
            if armed["on"] and "_budget-reservations" in str(path):
                armed["fired"] += 1
                raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "injected"))
            return real(path, **kw)
        return sync

    # (a) the first write of the ledger is replaced and its sync fails; later writes are fine.
    rig = Rig(tmp_path, ["a", "b"], ceiling=10, want=2)
    armed = {"on": True, "fired": 0}
    def first_only(path, **kw):
        if armed["on"] and "_budget-reservations" in str(path):
            armed["on"] = False
            armed["fired"] += 1
            raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "x" * 400))
        return real(path, **kw)

    monkeypatch.setattr(durable_io, "_fsync_parent", first_only)
    calls = []
    res = rig.executor().run_wave("w1", rig.plan("a", "b"),
                                  lambda n, a: calls.append(n.id) or ok())
    monkeypatch.undo()
    assert armed["fired"] == 1
    a, b = res.record.node("a"), res.record.node("b")
    assert (a.state, a.refusal) == (wave.NodeState.REFUSED, "BUDGET")
    assert "was ended" in a.detail                       # the stranded hold was found and settled
    assert b.state is wave.NodeState.RETURNED and calls == ["b"]
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}
    rig.assert_everything_given_back()
    # ... and on a healthy disk the node is admitted: the executor's own rule does not refuse it.
    again = rig.executor().run_wave("w2", rig.plan("a"), lambda n, a: calls.append(n.id) or ok())
    assert again.record.node("a").state is wave.NodeState.RETURNED
    assert "already holds" not in again.record.node("a").detail

    # (b) every ledger write is replaced and then fails to sync (repro of the review): the settle
    # raises too, but its row is on disk; the record reports what the ledger says, not the raise.
    rig2 = fresh(tmp_path, "second", ["a", "b"], ceiling=10, want=2)
    armed2 = {"on": True, "fired": 0}
    monkeypatch.setattr(durable_io, "_fsync_parent", ledger_sync(armed2))
    calls.clear()
    res2 = rig2.executor().run_wave("w1", rig2.plan("a", "b"),
                                    lambda n, a: calls.append(n.id) or ok())
    monkeypatch.undo()
    assert calls == [] and armed2["fired"] >= 2
    assert [n.refusal for n in res2.record.nodes] == ["BUDGET", "BUDGET"]
    assert all("was ended" in n.detail for n in res2.record.nodes)    # judged by the ledger, not by the raise
    assert HoldStatus.HELD not in {r.status for r in rig2.ledger()}
    rig2.assert_everything_given_back()
    assert rig2.reservations().left("zz", ceiling=10, spent=lambda: {}) == 10   # nothing is held


def test_an_interrupt_inside_reserve_after_its_write_settles_the_hold_and_the_lease(
        tmp_path, monkeypatch):
    """Catches a `KeyboardInterrupt` that strikes `reserve` after its ledger
    replace and leaves the hold HELD (the lease is given back by the handler,
    the hold was not)."""
    from hoh import durable_io
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["a"], ceiling=10, want=2)
    real = durable_io._fsync_parent
    state = {"on": True}

    def interrupt(path, **kw):
        if state["on"] and "_budget-reservations" in str(path):
            state["on"] = False
            raise KeyboardInterrupt
        return real(path, **kw)

    monkeypatch.setattr(durable_io, "_fsync_parent", interrupt)
    with pytest.raises(KeyboardInterrupt):
        rig.executor().run_wave("w1", rig.plan("a"), lambda n, a: ok())
    monkeypatch.undo()
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}
    assert [r.status for r in rig.ledger()] == [HoldStatus.SETTLED]
    rig.assert_everything_given_back()
    assert sorted(os.listdir(rig.records)) == []


def test_a_refused_node_does_not_release_the_lease_its_handle_holds_for_another_node(tmp_path):
    """Pins the guard of the repair: the executor releases a lease only if its
    own `acquire` call raised, never because the handle has a token -- one
    handle named for two nodes keeps the first node's lease when the second is
    refused."""
    from hoh import wave

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=10, want=2)
    shared = rig.handle("n1")
    during = {}

    def launch(node, adm):
        lease = rig.lease_on_disk("n1")
        during["live"] = lease is not None and not lease.released_at
        return ok()

    res = rig.executor(lease_for=lambda node: shared).run_wave(
        "w1", rig.plan("n1", "n2"), launch)
    first, second = res.record.node("n1"), res.record.node("n2")
    assert second.refusal == "WORKSPACE" and "already the worktree" in second.detail
    assert during == {"live": True}                      # n1's lease was live while it ran
    assert first.state is wave.NodeState.RETURNED and first.usable
    assert (first.lease_held_at_end, first.lease_released) == (True, True)

    # A live lease of the same holder name and pid that another handle took is refused as held, and the
    # record does not claim it as a lease this acquire wrote (the generation did not move).
    other = rig.handle("n2")
    other.acquire()
    res = rig.executor().run_wave("w2", rig.plan("n2"), lambda n, a: pytest.fail("launched"))
    row = res.record.node("n2")
    assert row.refusal == "WORKSPACE" and "held" in row.detail
    assert "could not be released" not in row.detail
    assert other.check().generation == 1                  # untouched


def test_a_plan_the_checks_cannot_read_and_a_record_dir_or_clock_that_is_not_one_are_wave_errors(
        tmp_path):
    """Catches the raw exceptions that left `run_wave` and the constructor
    (repair findings B-F2, B-F3): a `depends_on` that raises, a plan with no
    selected tuple, a record directory that is not a path, a clock that raises
    or returns no timestamp. Each is a `WaveError` before anything is taken."""
    from hoh import wave
    from hoh.frontier import FrontierPlan

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=10, want=2)
    calls = []

    def launch(node, adm):
        calls.append(node.id)
        return ok()

    def boom(a, b):
        raise RuntimeError("depends_on is broken")

    with pytest.raises(wave.WaveError, match="could not be checked") as caught:
        rig.executor(depends_on=boom).run_wave("w1", rig.plan("n1", "n2"), launch)
    assert not isinstance(caught.value, wave.WaveRecordError)
    with pytest.raises(wave.WaveError, match="could not be checked"):
        rig.executor().run_wave("w2", FrontierPlan(selected=None), launch)   # type: ignore[arg-type]
    # one node: `depends_on` is never asked, and a raising one is harmless
    one = rig.executor(depends_on=boom).run_wave("w3", rig.plan("n1"), launch)
    assert one.record.node("n1").state is wave.NodeState.RETURNED
    calls.clear()

    for bad in (None, 5, b"waves", "~no-such-user-for-p302f/waves"):
        with pytest.raises(wave.WaveError):
            rig.executor(record_dir=bad)

    def yesterday():
        return "yesterday"

    def broken_clock():
        raise RuntimeError("no clock")

    for fn in (yesterday, broken_clock):
        with pytest.raises(wave.WaveError) as err:
            rig.executor(clock=fn).run_wave("w4", rig.plan("n1", "n2"), launch)
        assert not isinstance(err.value, wave.WaveRecordError)
    assert calls == []
    assert sorted(os.listdir(rig.records)) == ["wave-w3.json"]       # only the one real wave
    assert {r.run_id for r in rig.ledger()} == {"n1"}


# --------------------------------------------------------------------------- #
# P3-02f, minors fold: a lease this wave did not take is not this wave's to release
# --------------------------------------------------------------------------- #

def test_a_handle_that_already_holds_the_worktree_is_refused_and_its_lease_is_left_alone(tmp_path):
    """Catches a cached handle (token set, lease live) that a second, overlapping
    wave is handed: `acquire` returns its lease unchanged, the node is admitted
    into the running wave's worktree and the refusal, or the end of the second
    wave, releases the first wave's lease (reviewers A-N2, B-N2). Also a handle
    the caller holds whose `acquire` raises before it writes anything."""
    from hoh import wave
    from hoh.wslease import WorkspaceLease

    rig = Rig(tmp_path, ["n1", "n2"], ceiling=20, want=2)
    cache = {n: rig.handle(n) for n in ("n1", "n2")}
    second = tmp_path / "waves-second"
    second.mkdir()
    done, seen = threading.Event(), {}

    def launch(node, adm):
        if node.id == "n2":
            assert done.wait(20)
            return ok()
        seen["before"] = sorted((r.run_id, r.reservation_id, r.status.value) for r in rig.ledger())
        seen["inner"] = rig.executor(record_dir=second, lease_for=lambda n: cache[n.id]).run_wave(
            "w2", rig.plan("n1", "n2"), lambda n, a: pytest.fail("launched into a running wave's worktree"))
        seen["after"] = sorted((r.run_id, r.reservation_id, r.status.value) for r in rig.ledger())
        seen["lease_n1"] = rig.lease_on_disk("n1").released_at
        done.set()
        return ok()

    res = rig.executor(lease_for=lambda n: cache[n.id]).run_wave("w1", rig.plan("n1", "n2"), launch)
    for node in seen["inner"].record.nodes:
        assert (node.state, node.refusal) == (wave.NodeState.REFUSED, "WORKSPACE")
        assert "already held through this handle" in node.detail
    assert seen["after"] == seen["before"]              # the second wave never touched the ledger
    assert seen["lease_n1"] is None                     # ... nor the lease, while the first wave ran
    assert all(n.usable and n.lease_held_at_end for n in res.record.nodes)
    rig.assert_everything_given_back()

    # A handle the caller holds, whose acquire fails before any write: it is not released.
    class Flaky(WorkspaceLease):
        broken = False

        def acquire(self):
            if self.broken:
                raise OSError(errno.EMFILE, "injected: too many open files")
            return super().acquire()

    mine = Flaky(rig.trees["n1"], "n1", ttl_seconds=600, clock=lambda: rig.now)
    mine.acquire()
    mine.broken = True
    r2 = rig.executor(lease_for=lambda n: mine).run_wave(
        "w3", rig.plan("n1"), lambda n, a: pytest.fail("launched"))
    row = r2.record.node("n1")
    assert (row.state, row.refusal) == (wave.NodeState.REFUSED, "WORKSPACE")
    assert "Errno 24" in row.detail                      # EMFILE
    lease = rig.lease_on_disk("n1")
    assert lease is not None and lease.released_at is None   # still the caller's, live
    mine.broken = False
    assert mine.check().holder == "n1"


def test_a_release_whose_log_line_failed_is_reported_as_released(tmp_path, monkeypatch):
    """Catches a refusal that says the lease "stays until it runs out" when
    `release` replaced the record and only its log line failed (A-N3), and a
    settlement that reports `lease_released` false for the same lease."""
    from hoh import durable_io, wave

    real = durable_io.durable_append

    def log_fails(path, line, *a, **kw):
        if '"event": "release"' in str(line):
            raise OSError(errno.ENOSPC, "injected: no space for the release log line")
        return real(path, line, *a, **kw)

    # (a) a budget refusal after the lease was taken: the lease is released, the text says so
    rig = Rig(tmp_path, ["n1"], ceiling=10, want=2)
    rig.spent = {"someone": 10}                          # nothing left to hold
    monkeypatch.setattr(durable_io, "durable_append", log_fails)
    res = rig.executor().run_wave("w1", rig.plan("n1"), lambda n, a: pytest.fail("launched"))
    monkeypatch.undo()
    row = res.record.node("n1")
    assert row.refusal == "BUDGET" and "stays until it runs out" not in row.detail
    assert rig.lease_on_disk("n1").released_at

    # (b) the settlement of an admitted node: the record reads what the lease on disk reads
    rig2 = fresh(tmp_path, "second", ["n1"], ceiling=10, want=2)
    monkeypatch.setattr(durable_io, "durable_append", log_fails)
    res2 = rig2.executor().run_wave("w1", rig2.plan("n1"), lambda n, a: ok())
    monkeypatch.undo()
    node = res2.record.node("n1")
    assert node.state is wave.NodeState.RETURNED and node.lease_released is True
    assert rig2.lease_on_disk("n1").released_at


def test_an_interrupt_while_ending_a_stranded_hold_still_gives_the_lease_back(tmp_path, monkeypatch):
    """Catches a reserve handler that settles the stranded hold before it
    releases the lease with no `finally`: a second interrupt inside that settle
    leaks the lease (reviewer A-N1, the handler-order window)."""
    from hoh import durable_io
    from hoh.reservation import HoldStatus

    rig = Rig(tmp_path, ["a"], ceiling=10, want=2)
    real = durable_io._fsync_parent
    calls = []

    def sync(path, **kw):
        if "_budget-reservations" in str(path):
            calls.append(1)
            if len(calls) == 1:
                raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "injected"))
            raise KeyboardInterrupt                      # the settle's own write, after its replace
        return real(path, **kw)

    monkeypatch.setattr(durable_io, "_fsync_parent", sync)
    with pytest.raises(KeyboardInterrupt):
        rig.executor().run_wave("w1", rig.plan("a"), lambda n, a: ok())
    monkeypatch.undo()
    assert len(calls) == 2
    assert rig.lease_on_disk("a").released_at            # given back although the settle was interrupted
    assert HoldStatus.HELD not in {r.status for r in rig.ledger()}
