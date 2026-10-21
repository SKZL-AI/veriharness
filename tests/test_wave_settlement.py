"""Wave settlement, the QA reserve and the degradation reason (V3.3 P3-22/23/24, slice 1).

`settle_wave` ends, by reservation id, the holds a finished wave left open and
says what each run used; `reserve(keep=)` and `WaveBudget.qa_reserve` keep the
last dispatches of the ceiling for QA nodes; `WaveRecord.degradation` says why a
wave ran narrower than its plan. Hermetic: every repository, worktree, ledger and
record is under `tmp_path`; nothing reads the repository the tree came from, no
agent is started, the clock is the rig's."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

START = "2026-10-12T10:00:00Z"
NOT_GIVEN = object()
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def _node(node_id, **kw):
    from hoh.project import Lifecycle, TaskNode
    return TaskNode(id=node_id, lifecycle=kw.pop("lifecycle", Lifecycle.READY), **kw)


class Rig:
    """A repository with one linked worktree per node, a runs root for the
    ledger, a record directory, a clock and a spend the test controls."""

    def __init__(self, tmp_path, names, *, ceiling=12, want=4, width=4):
        self.repo = tmp_path / "main"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("print('one')\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "first")
        self.trees = {}
        for n in names:
            target = tmp_path / n
            git(self.repo, "worktree", "add", "-q", "-b", n, str(target))
            self.trees[n] = target
        self.runs = tmp_path / "runs"
        self.records = tmp_path / "waves"
        self.records.mkdir()
        self.spent: dict[str, int] = {}
        self.ceiling, self.want, self.width = ceiling, want, width
        self.nodes = {n: _node(n) for n in names}

    def reservations(self):
        from hoh.reservation import BudgetReservations
        return BudgetReservations(self.runs)

    def lease_for(self, node):
        from hoh.wslease import WorkspaceLease
        return WorkspaceLease(self.trees[node.id], node.id, ttl_seconds=600, clock=lambda: START)

    def executor(self, *, reservations=None, qa_reserve=NOT_GIVEN, **over):
        from hoh import wave
        from hoh.project import ActionClass
        # The reserve is passed only when a test asks for one, so that the tests
        # of settlement build exactly what the executor was built with before.
        extra = {} if qa_reserve is NOT_GIVEN else {"qa_reserve": qa_reserve}
        args = dict(
            record_dir=self.records, width=self.width,
            budget=wave.WaveBudget(reservations or self.reservations(), self.ceiling,
                                   lambda: dict(self.spent), self.want, **extra),
            lease_for=self.lease_for, depends_on=lambda a, b: False,
            action_class=lambda n: ActionClass.INTERNAL, clock=lambda: START)
        args.update(over)
        return wave.ParallelWaveExecutor(**args)

    def plan(self, *names, deferred=()):
        from hoh.frontier import Deferral, FrontierPlan
        return FrontierPlan(tuple(self.nodes[n] for n in names),
                            tuple(Deferral(self.nodes[a], self.nodes[b]) for a, b in deferred))

    def record_path(self, wave_id):
        return self.records / f"wave-{wave_id}.json"

    def ledger(self):
        return self.reservations().read()

    def ledger_bytes(self):
        """The ledger file as it is: its bytes **and** which file it is and when it was
        written, because a rewrite within the same second changes neither the bytes
        (`written_at` has second resolution) nor, necessarily, anything else."""
        path = self.runs / "_budget-reservations.json"
        before = path.stat()
        return path.read_bytes(), before.st_ino, before.st_mtime_ns

    def held(self):
        from hoh.reservation import HoldStatus
        return sorted((r.run_id, r.amount) for r in self.ledger() if r.status is HoldStatus.HELD)

    def settle(self, wave_id):
        from hoh import wave
        return wave.settle_wave(self.record_path(wave_id), self.reservations(),
                                lambda: dict(self.spent))


def sub_rig(tmp_path, name, names, **kw):
    """A second, independent rig under its own directory of `tmp_path`."""
    sub = tmp_path / name
    sub.mkdir()
    return Rig(sub, names, **kw)


def ok(verdict="ACCEPTED"):
    return SimpleNamespace(verdict=verdict)


def leaky(rig):
    """A ledger whose `settle` always raises: the wave runs, and cannot end its
    own holds -- they stay open, and the node row says `holds_ended: None`."""
    from hoh.reservation import BudgetReservations, ReservationError

    class SettleFails(BudgetReservations):
        def settle(self, run_id, reason):
            raise ReservationError("the ledger is busy")

    return SettleFails(rig.runs)


def run(rig, wave_id, names, *, spend=None, deferred=(), **kw):
    """One wave over `names`; each launch records the run's spend."""
    spend = spend or {}

    def launch(node, admission):
        rig.spent[node.id] = spend.get(node.id, 0)
        return ok()

    return rig.executor(**kw).run_wave(wave_id, rig.plan(*names, deferred=deferred), launch)


# --------------------------------------------------------------------------- #
# S1-S5: settlement of a finished wave
# --------------------------------------------------------------------------- #

def test_settle_wave_ends_the_holds_a_node_could_not_end_and_says_what_each_run_used(tmp_path):
    from hoh import wave
    from hoh.reservation import HoldStatus
    rig = Rig(tmp_path, ["n1", "n2", "n3", "n4"])
    res = run(rig, "w1", ["n1", "n2", "n3"], spend={"n1": 1, "n2": 4, "n3": 0},
              reservations=leaky(rig), deferred=[("n4", "n1")])
    assert [n.holds_ended for n in res.record.nodes] == [None, None, None]
    assert res.record.phase is wave.Phase.SETTLED
    assert rig.held() == [("n1", 4), ("n2", 4), ("n3", 4)]
    # The leak is real: three dead holds still count against the ceiling.
    assert rig.reservations().left("probe", ceiling=12, spent=lambda: dict(rig.spent)) == 0

    first = rig.settle("w1")
    assert isinstance(first, wave.WaveSettlement) and first.wave_id == "w1"
    assert [(n.node_id, n.run_id, n.amount, n.used, n.released, n.state) for n in first.nodes] == [
        ("n1", "n1", 4, 1, 3, "ENDED"), ("n2", "n2", 4, 4, 0, "ENDED"),
        ("n3", "n3", 4, 0, 4, "ENDED")]
    assert [n.reservation_id for n in first.nodes] == [n.reservation_id for n in res.record.nodes]
    assert first.released == 7
    assert rig.held() == []
    assert {r.status for r in rig.ledger()} == {HoldStatus.SETTLED}
    assert {r.reason for r in rig.ledger()} == {"wave w1 settled"}
    assert rig.reservations().left("probe", ceiling=12, spent=lambda: dict(rig.spent)) == 7
    # The wave ran 3 of the 4 the plan was made from: the settlement says why.
    assert first.degradation == wave.ParallelismDegradation(
        ready=4, admitted=3, reasons=(wave.ParallelismDegradationReason.CONFLICT,))

    before = rig.ledger_bytes()
    rig.spent["n1"] = 2
    again = rig.settle("w1")
    assert [n.state for n in again.nodes] == ["ALREADY_ENDED"] * 3
    assert [n.used for n in again.nodes] == [2, 4, 0]       # the spend as read now
    assert rig.ledger_bytes() == before                       # nothing was written


def test_settle_wave_of_a_wave_that_gave_everything_back_ends_nothing_and_writes_nothing(tmp_path):
    rig = Rig(tmp_path, ["n1", "n2"])
    run(rig, "w1", ["n1", "n2"], spend={"n1": 2, "n2": 4})
    before = rig.ledger_bytes()
    s = rig.settle("w1")
    assert [(n.state, n.released) for n in s.nodes] == [("ALREADY_ENDED", 2), ("ALREADY_ENDED", 0)]
    assert s.degradation is None
    assert rig.ledger_bytes() == before


def test_settle_wave_refuses_what_is_not_a_settled_wave_record_and_ends_nothing(tmp_path):
    from hoh import wave
    rig = Rig(tmp_path, ["n1", "n2"])
    seen = {}

    def launch(node, admission):
        seen.setdefault("admitted", rig.record_path("w1").read_bytes())
        return ok()

    rig.executor(reservations=leaky(rig)).run_wave("w1", rig.plan("n1", "n2"), launch)
    settled = rig.record_path("w1").read_bytes()
    assert rig.held() == [("n1", 4), ("n2", 4)]
    ledger = rig.ledger_bytes()
    path = rig.record_path("w1")

    def refuses(cls=wave.WaveError, match=None):
        with pytest.raises(cls, match=match):
            rig.settle("w1")
        assert rig.ledger_bytes() == ledger and rig.held() == [("n1", 4), ("n2", 4)]

    # A record that still says ADMITTED: the nodes may be running, or the process died.
    path.write_bytes(seen["admitted"])
    refuses(match="ADMITTED")
    # Torn, odd and hostile spellings of the file: each is refused, none is "no wave".
    for label, data in {
        "truncated": settled[:-7], "empty": b"", "list": b"[]\n", "NUL": settled.replace(b"w1", b"w\\u00001"),
        "latin-1": settled.replace(b'"w1"', b'"w\xe91"'), "nan": settled.replace(b'"width": 4', b'"width": NaN'),
        "oversize": settled + b" " * (wave.MAX_RECORD_BYTES + 1),
    }.items():
        path.write_bytes(data)
        refuses(wave.WaveRecordError)
    path.write_bytes(settled)
    link = rig.records / "wave-link.json"
    link.symlink_to(path)
    with pytest.raises(wave.WaveRecordError):
        wave.settle_wave(link, rig.reservations(), lambda: {})
    fifo = rig.records / "wave-fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(wave.WaveRecordError):
        wave.settle_wave(fifo, rig.reservations(), lambda: {})
    directory = rig.records / "wave-dir.json"
    directory.mkdir()
    with pytest.raises(wave.WaveRecordError):
        wave.settle_wave(directory, rig.reservations(), lambda: {})
    dangling = rig.records / "wave-dangling.json"
    dangling.symlink_to(rig.records / "wave-nowhere.json")
    with pytest.raises(wave.WaveRecordError):
        wave.settle_wave(dangling, rig.reservations(), lambda: {})
    if os.geteuid() != 0:                    # root reads mode 000
        hidden = rig.records / "wave-hidden.json"
        hidden.write_bytes(settled)
        hidden.chmod(0)
        with pytest.raises(wave.WaveRecordError):
            wave.settle_wave(hidden, rig.reservations(), lambda: {})
    # No such record is an error, not a settlement of nothing.
    with pytest.raises(wave.WaveError, match="no wave to settle"):
        wave.settle_wave(rig.records / "wave-none.json", rig.reservations(), lambda: {})
    assert rig.ledger_bytes() == ledger
    # And the same file, put back, settles.
    assert [n.state for n in rig.settle("w1").nodes] == ["ENDED", "ENDED"]


def test_settle_wave_ends_nothing_when_one_hold_it_names_is_not_in_the_ledger(tmp_path):
    from hoh import wave
    from hoh.reservation import BudgetReservations
    rig = Rig(tmp_path, ["n1", "n2"])
    rig.executor(reservations=leaky(rig)).run_wave(
        "w1", rig.plan("n1", "n2"), lambda node, adm: ok())
    # A ledger that has never held anything of this wave.
    other = BudgetReservations(tmp_path / "other-runs")
    with pytest.raises(wave.WaveError):
        wave.settle_wave(rig.record_path("w1"), other, lambda: {})
    assert not os.path.lexists(tmp_path / "other-runs")
    other.reserve("zz", ceiling=5, spent=lambda: {}, want=1)
    with pytest.raises(wave.WaveError, match="holds no reservation"):
        wave.settle_wave(rig.record_path("w1"), other, lambda: {})
    assert [(r.run_id, str(r.status)) for r in other.read()] == [("zz", "HELD")]
    # The ledger knows n1's hold but not n2's: all or nothing.
    ledger = json.loads(rig.ledger_bytes()[0])
    ledger["reservations"] = [r for r in ledger["reservations"] if r["run_id"] == "n1"]
    (rig.runs / "_budget-reservations.json").write_text(json.dumps(ledger), encoding="utf-8")
    before = rig.ledger_bytes()
    with pytest.raises(wave.WaveError, match="holds no reservation"):
        rig.settle("w1")
    assert rig.ledger_bytes() == before and rig.held() == [("n1", 4)]
    # A ledger that is not one is a refusal of the settlement too, in every spelling.
    for i, spelling in enumerate(("not json", "[]", '{"schema_version": "x", "reservations": []}',
                                  b"\xff\xfe{", "fifo", "directory", "link")):
        bad = sub_rig(tmp_path, f"bad{i}", ["n1"])
        bad.executor(reservations=leaky(bad)).run_wave(
            "w1", bad.plan("n1"), lambda node, adm: ok())
        ledger_path = bad.runs / "_budget-reservations.json"
        original = ledger_path.read_bytes()
        os.rename(ledger_path, bad.runs / "aside.json")
        if spelling == "fifo":
            os.mkfifo(ledger_path)
        elif spelling == "directory":
            ledger_path.mkdir()
        elif spelling == "link":
            ledger_path.symlink_to(bad.runs / "aside.json")
        else:
            ledger_path.write_bytes(spelling if isinstance(spelling, bytes)
                                    else spelling.encode("utf-8"))
        with pytest.raises(wave.WaveError):
            bad.settle("w1")
        assert (bad.runs / "aside.json").read_bytes() == original, spelling
    # A record that names one hold for two nodes is not one this module wrote.
    twin = sub_rig(tmp_path, "twin", ["n1", "n2"])
    twin.executor(reservations=leaky(twin)).run_wave(
        "w1", twin.plan("n1", "n2"), lambda node, adm: ok())
    record = json.loads(twin.record_path("w1").read_bytes())
    record["nodes"][1]["reservation_id"] = record["nodes"][0]["reservation_id"]
    twin.record_path("w1").write_text(json.dumps(record), encoding="utf-8")
    before = twin.ledger_bytes()
    with pytest.raises(wave.WaveError, match="twice"):
        twin.settle("w1")
    assert twin.ledger_bytes() == before and twin.held() == [("n1", 4), ("n2", 4)]
    # A record whose rows swap their ids, or name another run or another amount than the
    # ledger holds for the id, is not settled as someone else's hold (review A, F3).
    for label, edit in (
        ("swapped ids", lambda d: (d["nodes"][0].update(reservation_id=d["nodes"][1]["reservation_id"]),
                                   d["nodes"][1].update(reservation_id=d["nodes"][0]["reservation_id"]))),
        ("other run", lambda d: d["nodes"][0].update(run_id="n2x")),
        ("other amount", lambda d: d["nodes"][0].update(amount=3)),
    ):
        mix = sub_rig(tmp_path, "mix" + label.split()[0] + label.split()[1], ["n1", "n2"])
        run(mix, "w1", ["n1", "n2"], reservations=leaky(mix), spend={"n1": 0, "n2": 4})
        doc = json.loads(mix.record_path("w1").read_bytes())
        edit(doc)
        mix.record_path("w1").write_text(json.dumps(doc), encoding="utf-8")
        before = mix.ledger_bytes()
        with pytest.raises(wave.WaveError, match="not |is of run"):
            mix.settle("w1")
        assert mix.ledger_bytes() == before and mix.held() == [("n1", 4), ("n2", 4)], label


def test_settle_wave_never_ends_a_hold_the_record_does_not_name(tmp_path):
    from hoh import wave
    from hoh.reservation import BudgetReservations, ReservationError
    rig = Rig(tmp_path, ["n1", "n2"])

    class SecondFails(BudgetReservations):          # n1's own settlement works, n2's does not
        def settle(self, run_id, reason):
            if run_id == "n2":
                raise ReservationError("the ledger is busy")
            return super().settle(run_id, reason)

    first = run(rig, "w1", ["n1", "n2"], spend={"n1": 1}, reservations=SecondFails(rig.runs))
    assert [n.holds_ended for n in first.record.nodes] == [1, None]
    assert rig.held() == [("n2", 4)]
    old = first.record.node("n1").reservation_id
    # A later wave takes n1's run id again; while it runs, w1 is settled.
    during = {}

    def launch(node, admission):
        try:
            during["held_before"] = rig.held()
            during["settlement"] = rig.settle("w1")
            during["held_after"] = rig.held()
        except BaseException as exc:             # a wave swallows it as ERRORED: say what it was
            during["error"] = repr(exc)
            raise
        during["new"] = admission.reservation_id
        return ok()

    second = rig.executor().run_wave("w2", rig.plan("n1"), launch)
    assert "error" not in during, during.get("error")
    assert second.record.node("n1").holds_ended == 1
    assert during["new"] != old
    assert during["held_before"] == [("n1", 4), ("n2", 4)]
    assert [(n.node_id, n.state) for n in during["settlement"].nodes] == [
        ("n1", "ALREADY_ENDED"), ("n2", "ENDED")]
    # n2's leaked hold is gone and w2's own hold, of the run id w1 also used, is not touched.
    assert during["held_after"] == [("n1", 4)]
    assert {r.reservation_id: str(r.status) for r in rig.ledger() if r.run_id == "n1"} == {
        old: "SETTLED", during["new"]: "SETTLED"}          # w2 ended its own when it finished

    # A hold the launcher re-took under the same run id supersedes the record's: the
    # record's hold is reported, the later one is not this record's to end.
    rig2 = sub_rig(tmp_path, "second", ["n1"])
    third = run(rig2, "w1", ["n1"], reservations=leaky(rig2))
    superseded = third.record.node("n1").reservation_id
    later = rig2.reservations().reserve("n1", ceiling=12, spent=lambda: {}, want=2)
    s = rig2.settle("w1")
    assert [(n.reservation_id, n.state) for n in s.nodes] == [(superseded, "SUPERSEDED")]
    assert [(n.amount, n.used, n.released) for n in s.nodes] == [(4, 0, 0)] and s.released == 0
    assert [(r.reservation_id, str(r.status)) for r in rig2.ledger() if str(r.status) == "HELD"] \
        == [(later.reservation_id, "HELD")]
    assert isinstance(s, wave.WaveSettlement)

    # The production shape: the launcher re-reserves under the run id inside the run, the run
    # spends 1, and the wave's own settlement fails. The leak is NOT closed here -- the later
    # hold has another id -- and the settlement must not say that 3 dispatches came back.
    rig3 = sub_rig(tmp_path, "third", ["n1"])
    fails = leaky(rig3)

    def launcher_like(node, admission):
        fails.reserve(node.id, ceiling=rig3.ceiling, spent=lambda: dict(rig3.spent), want=4)
        rig3.spent[node.id] = 1
        return ok()

    res = rig3.executor(reservations=fails).run_wave("w1", rig3.plan("n1"), launcher_like)
    assert [n.holds_ended for n in res.record.nodes] == [None]
    pool = lambda: rig3.reservations().left(                       # noqa: E731
        "probe", ceiling=12, spent=lambda: dict(rig3.spent))
    before, ledger = pool(), rig3.ledger_bytes()
    out = rig3.settle("w1")
    assert [(n.state, n.used, n.released) for n in out.nodes] == [("SUPERSEDED", 1, 0)]
    assert out.released == 0
    assert pool() == before and rig3.ledger_bytes() == ledger     # nothing came back, nothing was written
    assert rig3.held() == [("n1", 4)]                              # and the launcher's hold is still open


def test_settle_wave_settles_a_narrowed_wave_and_a_failed_node_by_the_run_id(tmp_path):
    """A REFUSED row has no hold, an ERRORED row has one, both are in one record (review B, 1);
    `used` is the spend of the **run** the node ran as (review B, 2)."""
    from hoh import wave
    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=20, want=4, width=2)

    def launch(node, admission):
        rig.spent["run-" + node.id] = 3 if node.id == "n1" else 1
        rig.spent[node.id] = 99                       # a decoy under the node id
        if node.id == "n1":
            raise RuntimeError("the run failed")
        return ok()

    res = rig.executor(reservations=leaky(rig), run_id_of=lambda n: "run-" + n.id).run_wave(
        "w1", rig.plan("n1", "n2", "n3"), launch)
    rows = {n.node_id: n for n in res.record.nodes}
    assert rows["n1"].state is wave.NodeState.ERRORED
    assert (rows["n3"].state, rows["n3"].refusal) == (wave.NodeState.REFUSED, "WIDTH")
    assert rig.held() == [("run-n1", 4), ("run-n2", 4)]
    s = rig.settle("w1")
    assert [(n.node_id, n.run_id, n.state, n.used, n.released) for n in s.nodes] == [
        ("n1", "run-n1", "ENDED", 3, 1), ("n2", "run-n2", "ENDED", 1, 3)]
    assert rig.held() == []
    assert s.degradation == wave.ParallelismDegradation(
        3, 2, (wave.ParallelismDegradationReason.WIDTH,))

    # A wave in which every node was refused holds nothing: the settlement is empty, takes no
    # ledger (there is none), and says why in the degradation.
    none = sub_rig(tmp_path, "none", ["n1", "n2"], ceiling=6, want=2)
    none.spent["elsewhere"] = 6
    out = none.executor().run_wave("w1", none.plan("n1", "n2"), lambda node, adm: ok())
    assert [n.refusal for n in out.record.nodes] == ["BUDGET", "BUDGET"]
    assert not os.path.lexists(none.runs / "_budget-reservations.json")
    empty = none.settle("w1")
    assert empty.nodes == () and empty.released == 0
    assert empty.degradation == wave.ParallelismDegradation(
        2, 0, (wave.ParallelismDegradationReason.BUDGET,))


def test_settle_wave_refuses_a_spend_it_cannot_trust_and_ends_nothing(tmp_path):
    from hoh import wave
    rig = Rig(tmp_path, ["n1"])
    run(rig, "w1", ["n1"], reservations=leaky(rig))
    ledger = rig.ledger_bytes()

    def boom():
        raise OSError("state unreadable")

    for label, spent in {
        "raises": boom, "list": lambda: [("n1", 1)], "negative": lambda: {"n1": -1},
        "bool": lambda: {"n1": True}, "string": lambda: {"n1": "3"}, "float": lambda: {"n1": 1.0},
        "none": lambda: None, "key": lambda: {1: 1}, "not callable": {"n1": 1}, "missing": None,
    }.items():
        with pytest.raises(wave.WaveError):
            wave.settle_wave(rig.record_path("w1"), rig.reservations(), spent)
        assert rig.ledger_bytes() == ledger, label
    with pytest.raises(wave.WaveError, match="BudgetReservations"):
        wave.settle_wave(rig.record_path("w1"), object(), lambda: {})
    assert rig.held() == [("n1", 4)]
    # A spend above the hold is a release of nothing, never a negative release.
    assert wave.settle_wave(rig.record_path("w1"), rig.reservations(),
                            lambda: {"n1": 9}).nodes[0].released == 0


# --------------------------------------------------------------------------- #
# Q1-Q4: the QA reserve
# --------------------------------------------------------------------------- #

def test_reserve_keeps_the_reserve_and_names_why_it_refused(tmp_path):
    from hoh.reservation import (BudgetReservations, NothingToReserve, ReservationError,
                                 ReserveKept)
    store = BudgetReservations(tmp_path / "runs")
    path = tmp_path / "runs" / "_budget-reservations.json"
    assert store.reserve("a", ceiling=10, spent=lambda: {}, keep=3).amount == 7   # everything left but 3
    before = path.read_bytes()
    with pytest.raises(ReserveKept) as kept:                    # left is 3, all of it kept
        store.reserve("b", ceiling=10, spent=lambda: {}, keep=3)
    assert kept.value.reserve_kept is True and isinstance(kept.value, NothingToReserve)
    assert path.read_bytes() == before                          # a refusal writes nothing
    assert store.reserve("b", ceiling=10, spent=lambda: {}, keep=2).amount == 1
    assert store.reserve("c", ceiling=10, spent=lambda: {}, keep=0).amount == 2
    for keep in (0, 3):                                         # nothing at all is left
        with pytest.raises(NothingToReserve) as gone:
            store.reserve("d", ceiling=10, spent=lambda: {}, keep=keep)
        assert gone.value.reserve_kept is False and not isinstance(gone.value, ReserveKept)
    assert NothingToReserve.reserve_kept is False
    # `want` and the spend are both below the reserve line.
    other = BudgetReservations(tmp_path / "other")
    assert other.reserve("a", ceiling=10, spent=lambda: {"x": 5}, want=1, keep=3).amount == 1
    assert other.reserve("b", ceiling=10, spent=lambda: {"x": 5}, want=9, keep=3).amount == 1
    with pytest.raises(ReserveKept):                            # 10 - 5 spent - 2 held = 3 = the reserve
        other.reserve("d", ceiling=10, spent=lambda: {"x": 5}, keep=3)
    assert other.reserve("d", ceiling=10, spent=lambda: {"x": 5}, keep=2).amount == 1
    assert BudgetReservations(tmp_path / "third").reserve(
        "c", ceiling=5, spent=lambda: {}, keep=4).amount == 1    # one more than the reserve
    # The reserve is an integer of at least 0.
    for bad in (-1, True, 1.5, "1", None):
        with pytest.raises(ReservationError):
            store.reserve("e", ceiling=10, spent=lambda: {}, keep=bad)
    assert [r.run_id for r in store.read()] == ["a", "b", "c"]
    # ... and before the lock: a refused reserve of a root that does not exist creates nothing.
    fresh = BudgetReservations(tmp_path / "never-made")
    for bad in (-1, True, 1.5):
        with pytest.raises(ReservationError):
            fresh.reserve("a", ceiling=5, spent=lambda: {}, keep=bad)
    assert not os.path.lexists(tmp_path / "never-made")


def test_the_reserve_is_kept_between_concurrent_starts(tmp_path):
    from hoh.reservation import BudgetReservations, ReserveKept
    store = BudgetReservations(tmp_path / "runs")
    gate = threading.Barrier(4)
    got: dict[str, object] = {}

    def start(name):
        gate.wait(timeout=10)
        try:
            got[name] = store.reserve(name, ceiling=10, spent=lambda: {}, want=5, keep=4).amount
        except ReserveKept:
            got[name] = "kept"

    threads = [threading.Thread(target=start, args=(f"r{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(map(str, got.values())) == ["1", "5", "kept", "kept"]
    assert sum(r.amount for r in store.held()) == 6            # 10 less the 4 kept


def test_the_developer_phase_cannot_use_the_qa_reserve_and_a_qa_node_can(tmp_path):
    from hoh import wave
    names = ["d1", "d2", "d3", "q1"]
    rig = Rig(tmp_path, names, ceiling=10, want=4)
    seen = {}

    def launch(node, admission):
        seen[node.id] = admission.amount
        return ok()

    res = rig.executor(qa_reserve=3, is_qa=lambda n: n.id.startswith("q")).run_wave(
        "w1", rig.plan(*names), launch)
    rows = {n.node_id: n for n in res.record.nodes}
    assert seen == {"d1": 4, "d2": 3, "q1": 3}          # the developers stop at 7; QA takes the last 3
    assert (rows["d3"].state, rows["d3"].refusal) == (wave.NodeState.REFUSED, "QA_RESERVE")
    assert rows["d3"].reservation_id is None and rows["d3"].worktree is None
    assert sorted((r.run_id, r.amount) for r in rig.ledger()) == [("d1", 4), ("d2", 3), ("q1", 3)]
    assert wave.parse(res.record.to_bytes()) == res.record       # the new reason is a record the parser reads
    assert res.degradation == wave.ParallelismDegradation(
        ready=4, admitted=3, reasons=(wave.ParallelismDegradationReason.QA_RESERVE,))
    lease = rig.lease_for(rig.nodes["d3"]).read()
    assert lease is None or lease.released_at                    # the refused node kept no worktree

    # No `is_qa` given: every node is a developer, whatever it is called (review B, 4).
    nodefault = sub_rig(tmp_path, "nodefault", names, ceiling=10, want=4)
    res3 = nodefault.executor(qa_reserve=3).run_wave("w1", nodefault.plan(*names), launch)
    assert [(n.node_id, n.amount, n.refusal) for n in res3.record.nodes] == [
        ("d1", 4, ""), ("d2", 3, ""), ("d3", None, "QA_RESERVE"), ("q1", None, "QA_RESERVE")]
    # A reserve is configured and nothing at all is left: that is BUDGET, not the reserve's doing
    # (review B, 5) -- the refusal follows what the ledger said, not what was configured.
    gone = sub_rig(tmp_path, "gone", ["d1"], ceiling=6, want=2)
    gone.spent["elsewhere"] = 6
    res4 = gone.executor(qa_reserve=2).run_wave("w1", gone.plan("d1"), launch)
    assert res4.record.node("d1").refusal == "BUDGET"
    assert res4.degradation.reasons == (wave.ParallelismDegradationReason.BUDGET,)

    # No reserve: the same wave as before, and the refusal is a BUDGET one.
    plain = sub_rig(tmp_path, "plain", names, ceiling=10, want=4)
    res2 = plain.executor().run_wave("w1", plain.plan(*names), launch)
    amounts = {n.node_id: n.amount for n in res2.record.nodes}
    assert amounts == {"d1": 4, "d2": 4, "d3": 2, "q1": None}
    assert res2.record.node("q1").refusal == "BUDGET"
    assert res2.degradation.reasons == (wave.ParallelismDegradationReason.BUDGET,)


def test_the_reserve_arguments_are_bounded_and_a_broken_is_qa_keeps_the_reserve(tmp_path):
    from hoh import wave
    rig = Rig(tmp_path, ["d1", "d2"], ceiling=10, want=4)
    rig.executor(qa_reserve=9)                                   # ceiling - 1 is the most
    for bad in (10, 11, -1, True, 1.5, "1", None):
        with pytest.raises(wave.WaveError):
            rig.executor(qa_reserve=bad)
    one = sub_rig(tmp_path, "one", ["d1"], ceiling=1, want=1)
    one.executor(qa_reserve=0)
    with pytest.raises(wave.WaveError):
        one.executor(qa_reserve=1)
    with pytest.raises(wave.WaveError):
        rig.executor(is_qa="yes")

    def boom(node):
        raise RuntimeError("no role")

    for label, is_qa in (("raises", boom), ("truthy", lambda n: 1), ("string", lambda n: "qa")):
        sub = sub_rig(tmp_path, label, ["d1", "d2"], ceiling=10, want=7)
        res = sub.executor(qa_reserve=4, is_qa=is_qa).run_wave(
            "w1", sub.plan("d1", "d2"), lambda node, adm: ok())
        # d1 is granted 6 (10 - 4 kept); d2 finds exactly the reserve: it is refused as a developer.
        assert (res.record.node("d1").amount, res.record.node("d2").refusal) == (6, "QA_RESERVE"), label
    strict = sub_rig(tmp_path, "strict", ["d1", "d2"], ceiling=10, want=7)
    res = strict.executor(qa_reserve=4, is_qa=lambda n: n.id == "d2").run_wave(
        "w1", strict.plan("d1", "d2"), lambda node, adm: ok())
    assert (res.record.node("d1").amount, res.record.node("d2").amount) == (6, 4)


# --------------------------------------------------------------------------- #
# D1-D2: the degradation reason
# --------------------------------------------------------------------------- #

def test_a_narrow_wave_says_why_and_a_full_one_says_nothing(tmp_path):
    from hoh import wave
    R = wave.ParallelismDegradationReason
    # Width: three nodes, two admitted.
    rig = Rig(tmp_path, ["n1", "n2", "n3"], ceiling=20, want=1, width=2)
    res = run(rig, "w1", ["n1", "n2", "n3"])
    assert res.degradation == wave.ParallelismDegradation(3, 2, (R.WIDTH,))
    # A conflict the plan deferred.
    deferred = sub_rig(tmp_path, "d", ["n1", "n2", "n3"], ceiling=20, want=1)
    res = run(deferred, "w1", ["n1", "n2"], deferred=[("n3", "n1")])
    assert res.degradation == wave.ParallelismDegradation(3, 2, (R.CONFLICT,))
    # A node deferred behind two selected nodes is one ready node, not two (review B, 3).
    twice = sub_rig(tmp_path, "twice", ["n1", "n2", "n3"], ceiling=20, want=1)
    res = run(twice, "w1", ["n1", "n2"], deferred=[("n3", "n1"), ("n3", "n2")])
    assert res.degradation == wave.ParallelismDegradation(3, 2, (R.CONFLICT,))
    # A deferral that names a node that is also selected is a degradation all the same (review A, F2):
    # `parse` still reads such a record (P3-02's R5 writes one), so the reason cannot depend on it.
    overlap = sub_rig(tmp_path, "overlap", ["n1", "n2"], ceiling=20, want=1)
    res = run(overlap, "w1", ["n1", "n2"], deferred=[("n1", "n2")])
    assert wave.parse(res.record.to_bytes()) == res.record
    assert res.degradation == wave.ParallelismDegradation(2, 2, (R.CONFLICT,))
    # Everything admitted: no degradation, even when a node then failed.
    full = sub_rig(tmp_path, "f", ["n1", "n2"], ceiling=20, want=1)

    def launch(node, admission):
        if node.id == "n1":
            raise RuntimeError("the run failed")
        return ok()

    res = full.executor().run_wave("w1", full.plan("n1", "n2"), launch)
    assert [n.state for n in res.record.nodes] == [wave.NodeState.ERRORED, wave.NodeState.RETURNED]
    assert res.degradation is None and res.record.degradation is None

    # Every reason, once, in the vocabulary's order, from the record alone.
    refusals = [wave.WaveNode(f"r{i}", f"r{i}", wave.NodeState.REFUSED, refusal=r.value, detail="x")
                for i, r in enumerate(wave.Refusal)]
    admitted = wave.WaveNode("a1", "a1", wave.NodeState.ADMITTED, reservation_id="x-a1", amount=1,
                             worktree="/w", generation=1)
    record = wave.WaveRecord(
        "w9", wave.Phase.ADMITTED, START, None, 4, 10, tuple(refusals) + (admitted,),
        (wave.WaveDeferral("c1", "a1", "SEMANTIC_DEPENDENCY"), wave.WaveDeferral("c2", "a1", "SEMANTIC_DEPENDENCY")))
    degraded = record.degradation
    assert (degraded.ready, degraded.admitted) == (len(refusals) + 1 + 2, 1)
    assert degraded.reasons == tuple(R)                          # all eight, in the enum's order
    assert [r.value for r in R] == ["NOT_READY", "EXTERNAL", "WIDTH", "RUN_ID", "BUDGET",
                                    "QA_RESERVE", "WORKSPACE", "CONFLICT"]
    assert {r.value for r in wave.Refusal} == {r.value for r in R} - {"CONFLICT"}
    # A deferral alone (nothing refused) is a degradation too: two ready nodes were held back.
    only = wave.WaveRecord("w9", wave.Phase.ADMITTED, START, None, 4, 10, (admitted,),
                           (wave.WaveDeferral("c1", "a1", "SEMANTIC_DEPENDENCY"),))
    assert only.degradation == wave.ParallelismDegradation(2, 1, (R.CONFLICT,))
    assert wave.WaveRecord("w9", wave.Phase.ADMITTED, START, None, 4, 10, (admitted,)).degradation is None


def test_settle_holds_ends_exactly_the_named_open_holds_and_refuses_anything_else(tmp_path):
    from hoh.reservation import BudgetReservations, HoldStatus, ReservationError
    store = BudgetReservations(tmp_path / "runs")
    path = tmp_path / "runs" / "_budget-reservations.json"

    def nothing():
        return {}

    # No ids: nothing is looked at, not even a ledger that is not there.
    assert store.settle_holds([], "x") == {}
    assert not os.path.lexists(tmp_path / "runs")
    assert store.settle_holds([], "x", expect={}) == {}
    with pytest.raises(ReservationError, match="expect"):
        store.settle_holds([], "x", expect={"abc-n1": ("n1", 1)})
    # ... and not a ledger that is not one: nothing is read when there is nothing to end.
    junk = BudgetReservations(tmp_path / "junk")
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "_budget-reservations.json").write_text("not json", encoding="utf-8")
    assert junk.settle_holds([], "x") == {}
    with pytest.raises(ReservationError, match="no hold is known"):
        store.settle_holds(["abc-n1"], "x")
    assert not os.path.lexists(tmp_path / "runs")

    a1 = store.reserve("a", ceiling=20, spent=nothing, want=2)
    b = store.reserve("b", ceiling=20, spent=nothing, want=2)
    c = store.reserve("c", ceiling=20, spent=nothing, want=2)
    assert store.settle("b", "b was done") == 1
    a2 = store.reserve("a", ceiling=20, spent=nothing, want=3)     # supersedes a1
    rows = {r.reservation_id: r for r in store.read()}
    assert [rows[i].status for i in (a1.reservation_id, b.reservation_id)] == [
        HoldStatus.SUPERSEDED, HoldStatus.SETTLED]
    kept = {i: rows[i].model_dump() for i in (a1.reservation_id, b.reservation_id)}

    found = store.settle_holds(
        [a1.reservation_id, b.reservation_id, c.reservation_id, a2.reservation_id], "reconciled")
    assert found == {a1.reservation_id: HoldStatus.SUPERSEDED, b.reservation_id: HoldStatus.SETTLED,
                     c.reservation_id: HoldStatus.HELD, a2.reservation_id: HoldStatus.HELD}
    after = {r.reservation_id: r for r in store.read()}
    assert [after[i].status for i in (c.reservation_id, a2.reservation_id)] == [HoldStatus.SETTLED] * 2
    assert {after[i].reason for i in (c.reservation_id, a2.reservation_id)} == {"reconciled"}
    assert all(after[i].ended_at for i in (c.reservation_id, a2.reservation_id))
    # The rows that were not open are exactly as they were, reason and time included.
    assert {i: after[i].model_dump() for i in kept} == kept
    # Nothing left open: a second call writes nothing, whatever the time.
    stamp = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    assert store.settle_holds([c.reservation_id], "again") == {c.reservation_id: HoldStatus.SETTLED}
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == stamp
    assert len(store.read()) == 4                                  # rows are never removed

    # `expect`: the (run id, amount) the caller believes each id has, checked under the lock.
    other = store.reserve("e", ceiling=20, spent=nothing, want=3)
    stamp = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    for label, ask, match in (
        ("run", {other.reservation_id: ("f", 3)}, "is of run"),
        ("amount", {other.reservation_id: ("e", 2)}, "is 3, not 2"),
        ("keys", {}, "expect names exactly"),
        ("more keys", {other.reservation_id: ("e", 3), "zz-e": ("e", 3)}, "expect names exactly"),
        ("shape", {other.reservation_id: "e"}, "expect names exactly"),
        ("not a mapping", [("e", 3)], "expect names exactly"),
    ):
        with pytest.raises(ReservationError, match=match):
            store.settle_holds([other.reservation_id], "no", expect=ask)
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == stamp, label
    assert store.settle_holds([other.reservation_id], "yes",
                              expect={other.reservation_id: ("e", 3)}) == {
        other.reservation_id: HoldStatus.HELD}

    # Refusals end nothing: a name that is not an id, a hold the ledger does not have, a repeat.
    fresh = store.reserve("d", ceiling=20, spent=nothing, want=2)
    stamp = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    exact = "x" * 300
    for ids, match in (
        (["", fresh.reservation_id], "not a reservation id"),
        ([fresh.reservation_id + "\n"], "not a reservation id"),
        ([fresh.reservation_id + " "], "not a reservation id"),
        (["a/b"], "not a reservation id"),
        (["x" * 301], "not a reservation id"),
        ([5], "not a reservation id"), ([None], "not a reservation id"),
        (fresh.reservation_id, "not one string"),        # a bare string is not a list of ids
        ([fresh.reservation_id, fresh.reservation_id], "twice"),
        ([fresh.reservation_id, exact], "holds no reservation"),     # 300 characters is an id
        ([fresh.reservation_id, "ffffffff-nobody"], "holds no reservation"),
    ):
        with pytest.raises(ReservationError, match=match):
            store.settle_holds(ids, "no")
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == stamp, ids
    assert [r.status for r in store.read() if r.reservation_id == fresh.reservation_id] == [
        HoldStatus.HELD]
    assert len(store.settle_holds([fresh.reservation_id], "x" * 500)) == 1
    assert {r.reason for r in store.read() if r.reservation_id == fresh.reservation_id} == {"x" * 200}
