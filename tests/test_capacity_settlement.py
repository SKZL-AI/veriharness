"""P3-04 (C4b): the capacity a wave was admitted against is settled on every end of the wave.

C4 (`tests/test_capacity_reservation.py`) holds what a wave was admitted against and lets the hold end by its owner asking
again, its owner's death or a day. This file pins the call that gives it back **when the wave ends**
(`ProjectController._report_wave_end` -> `CapacityReservations.settle_owned`) and **when a dead controller's wave is
recovered** (`waverecovery.recover_open`, the `capacity` callback of `_recover_open_waves`), idempotently and without
ever releasing silently: a ledger that cannot be used is a visible step or event and the rows stay as they are.

What is real: the controller, the executor, the real launcher over a fake process boundary (the harnesses of
`tests/test_wave_capacity.py`, `tests/test_capacity_reservation.py` and `tests/test_wave_recovery.py`, imported), the
ledger and its lock, `cli.main project drive`. No agent, no Herdr, no network.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from test_capacity_reservation import (  # noqa: F401 - helpers; the fixtures are collected below
    LEDGER, NOW, _check, _res, _second_project, ask, ledger, owner, plus, statuses,
)
from test_wave_capacity import (  # noqa: F401 - fixtures are collected from here
    Gates, Launcher, World, hermetic_environment, utc, waves,
)
from test_wave_capacity import world as world  # the alias form is an explicit re-export: no F811 on the parameters

from hoh.launcher import Dispatch
from hoh.orchestrator import HaltClass, ProjectController
from hoh.project import Lifecycle


def _declare(state: Path, limit: int) -> None:
    """A capacity registry of `limit` sessions in a fresh state root (what `World.sessions` does)."""
    from hoh import capacity
    from hoh.storage_registry import StorageRegistry
    state.mkdir(parents=True, exist_ok=True)
    StorageRegistry(state).init()
    capacity.write(state.resolve() / "capacity-registry.json", [capacity.Entry(
        kind="machine", name="host.sessions", limit=limit, unit="sessions", source="declared", as_of=utc())])


class Failing(Launcher):
    """`hoh run` of one node exits 1: the launch comes back with a verdict that is not an acceptance (the process is over)."""

    def _run_cli(self, argv):
        if "run" in argv and "worktree" not in argv and "start" not in argv and argv[argv.index("run") + 1] == "n2":
            return Dispatch(1, "", "the run of n2 failed")
        return super()._run_cli(argv)


class Doubtful(Launcher):
    """n1's `hoh run` answers "already being executed by another process" (the launch comes back `IN_FLIGHT`: a live
    process still runs it), n2's dies with an exception (`ERRORED`: the process may be alive), n3 is accepted."""

    def _run_cli(self, argv):
        if "run" in argv and "worktree" not in argv and "start" not in argv:
            run_id = argv[argv.index("run") + 1]
            if run_id == "n1":
                return Dispatch(2, "", "Run n1 is already being executed by another process")
            if run_id == "n2":
                raise RuntimeError("the process of n2 died")
        return super()._run_cli(argv)


# --- R1 the wave's end gives the capacity back ----------------------------------------------------------------------------

def test_hoh_project_drive_ends_the_capacity_rows_of_every_wave_settled(world, monkeypatch, capsys):
    """The production path (`cli.main` -> `hoh project drive` -> `ProjectController.run` -> `_wave_round` ->
    `_report_wave_end` -> `CapacityReservations.settle_owned`): room for one session and three nodes at
    `HOH_WAVE_WIDTH=3`: every wave admits one node and, when the wave has ended, its row is `SETTLED` (not `HELD`,
    not left for the next ask to supersede), with the reason naming the wave; the project closes and no step
    says a capacity could not be settled."""
    import test_wave_capacity as harness
    world.sessions(1)
    rc, out = harness._drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    rows = ledger(world.root)["holds"]
    assert [(h["run_id"], h["status"]) for h in rows] == [("n1", "SETTLED"), ("n2", "SETTLED")]
    assert all(h["reason"].startswith("wave w") and "ended" in h["reason"] and h["ended_at"] for h in rows)
    assert not [s for s in out["steps"] if s.startswith("WAVE_CAPACITY")], out["steps"]


def test_a_failed_or_halting_wave_gives_its_capacity_back_too(world):
    """Catches a release that only the happy path makes: the run of n2 fails (exit 1: the launch returns the verdict
    `REJECTED`, the process is over) -- and both rows of the wave are `SETTLED` anyway (the executor has returned: nothing of
    the wave is left to start), while a node the gate made wait (n3 of three at limit 2) never had a row."""
    world.sessions(2)
    launcher = Failing(world.root, world.repo, storage_root=world.state)
    ProjectController(world.store, launcher, Gates(), width=3).run()
    first = waves(world)[0]
    assert {n.node_id: n.verdict for n in first.nodes} == {"n1": "ACCEPTED", "n2": "REJECTED"}
    rows = [(h["run_id"], h["status"]) for h in ledger(world.root)["holds"]]
    assert rows[:2] == [("n1", "SETTLED"), ("n2", "SETTLED")] and "HELD" not in {s for _, s in rows}, rows


def test_a_run_that_may_still_be_alive_keeps_its_row_when_the_wave_ends(world):
    """Review A's F1 (C4's L1b, narrowed): a launch that came back `IN_FLIGHT` (another process still executes the run) or
    that raised (`ERRORED`: the process may be alive) is **not** over. Its row stays `HELD` while the controller waits (the
    controller's next ask would supersede it, as in C4); the node that was accepted is settled. Three nodes, room for
    three: n1 IN_FLIGHT, n2 raises, n3 accepted."""
    world.sessions(3)
    launcher = Doubtful(world.root, world.repo, storage_root=world.state)
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt in (HaltClass.RUN_IN_FLIGHT, HaltClass.AMBIGUOUS), (result.halt, result.reason)
    rows = {h["run_id"]: h["status"] for h in ledger(world.root)["holds"]}
    assert rows == {"n1": "HELD", "n2": "HELD", "n3": "SETTLED"}, rows
    assert not [s for s in result.steps if s.kind == "WAVE_CAPACITY_UNSETTLED"]


def test_a_second_controller_is_admitted_when_the_first_wave_has_ended_though_its_thread_lives(world):
    """What C4 left to a thread's death: project p's controller finishes its wave and its thread stays alive (a daemon
    that goes on); project q, asking next, finds the two sessions free and closes. Without the settle p's rows stay
    `HELD` (their owner lives and has not asked again) and q waits `CAPACITY_WAIT`."""
    world.sessions(2)
    done, release, result = threading.Event(), threading.Event(), {}

    def controller_p():
        result["p"] = ProjectController(world.store, world.launcher(), Gates(), width=2).run()
        done.set()
        release.wait(60)                                  # the owner thread is still alive
    thread = threading.Thread(target=controller_p, name="controller-p")
    thread.start()
    try:
        assert done.wait(60), "the first controller never finished"
        assert result["p"].halt is HaltClass.CLOSED, result["p"].reason
        store_q, repo_q = _second_project(world)
        second = Launcher(world.root, repo_q, storage_root=world.state)
        again = ProjectController(store_q, second, Gates(), width=2).run()
        assert again.halt is HaltClass.CLOSED, (again.halt, again.reason)
        assert {n.lifecycle for n in store_q.read_state().nodes} == {Lifecycle.MERGED}
    finally:
        release.set()
        thread.join(timeout=60)


def test_a_host_with_no_capacity_ledger_gets_none_from_a_wave_end(world, monkeypatch, capsys):
    """Catches a settle that creates what the opt-out never had: no registry, a drive of waves -> no ledger, no
    lock and no archive in the runs root, and no capacity step."""
    import test_wave_capacity as harness
    rc, out = harness._drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert not sorted(p.name for p in world.root.iterdir() if p.name.startswith("_capacity-reservations"))
    assert not [s for s in out["steps"] if "CAPACITY_UNSETTLED" in s]


def test_a_ledger_that_cannot_be_used_at_the_wave_end_is_a_visible_step_and_nothing_is_released(world):
    """Fail closed: the ledger is spoiled while the wave runs. The wave end names it (`WAVE_CAPACITY_UNSETTLED`, a
    note: the drive goes on), the ledger's bytes are what the spoiler left (nothing repaired, nothing ended), and the
    drive does not halt for it."""
    world.sessions(2)

    class Spoiling(Launcher):
        def _run_cli(self, argv):
            if "run" in argv and argv[argv.index("run") + 1] == "n1":
                (self.root / LEDGER).write_bytes(b"{not json")
            return super()._run_cli(argv)
    launcher = Spoiling(world.root, world.repo, storage_root=world.state)
    result = ProjectController(world.store, launcher, Gates(), width=2).run()
    [unsettled] = [s for s in result.steps if s.kind == "WAVE_CAPACITY_UNSETTLED"]
    assert "ReservationError" in unsettled.detail and unsettled.node.startswith("w")
    assert (world.root / LEDGER).read_bytes() == b"{not json"
    assert result.halt is HaltClass.CLOSED, (result.halt, result.reason)      # a note, not a halt: n3 still runs and the project closes


# --- R2 the call itself: owner-scoped, idempotent, quiet --------------------------------------------------------------------

def test_settle_owned_ends_only_the_callers_rows_once_and_a_second_call_writes_nothing(world):
    """Catches the collision review A found in C4 (node ids are project-local) and a settle that is not idempotent:
    three owners hold a node named `t1` and the third also `t2`; the middle owner's `settle_owned` ends its own `t1`
    and nobody else's; a second call over the same rows returns 0 and changes no byte; `run_ids` narrows it; a row
    whose owner has another process identity is left; with no ledger it returns 0 and creates nothing."""
    res = _res()
    world.sessions(8)
    ask(world, ["t1"], **owner(pid=1111))
    ask(world, ["t1"], **owner(pid=2222))
    ask(world, ["t1", "t2"], **owner(pid=3333))
    mine = res.CapacityReservations(world.root, **owner(pid=2222))
    assert mine.settle_owned("done") == 1
    assert [(h["owner_pid"], h["run_id"], h["status"]) for h in ledger(world.root)["holds"]] == [
        (1111, "t1", "HELD"), (2222, "t1", "SETTLED"), (3333, "t1", "HELD"), (3333, "t2", "HELD")]
    settled = [h for h in ledger(world.root)["holds"] if h["owner_pid"] == 2222][0]
    assert settled["ended_at"] == NOW and settled["reason"] == "done"
    before = (world.root / LEDGER).read_bytes()
    later = res.CapacityReservations(world.root, **owner(pid=2222, clock=lambda: plus(NOW, 5)))   # a rewrite would stamp 5 s on
    assert later.settle_owned("done again") == 0 and (world.root / LEDGER).read_bytes() == before
    ask(world, ["u1"], **owner(pid=2222, thread=8, live_threads=lambda: {7, 8}))   # another thread of the same process
    assert mine.settle_owned("not the other thread's") == 0
    assert statuses_by_pid(world.root)[2222] == ["SETTLED", "HELD"]
    res.CapacityReservations(world.root, **owner(pid=2222, thread=8)).settle_owned("its own")
    other = res.CapacityReservations(world.root, **owner(pid=3333))
    assert other.settle_owned("narrow", run_ids=["t2"]) == 1
    assert statuses_by_pid(world.root)[3333] == ["HELD", "SETTLED"]
    assert other.settle_owned("r" * 500) == 1                      # the rest of its own, the reason cut
    assert [h["reason"] for h in ledger(world.root)["holds"] if h["owner_pid"] == 3333] == ["r" * 200, "narrow"]
    # a recovery names a gone process by pid and identity: another start time is a reused pid's row
    gone = res.CapacityReservations(world.root, **owner(pid=9999))
    assert gone.settle_owned("x", pid=1111, identity=":start-other") == 0
    assert gone.settle_owned("x", pid=1111, identity=":start-1111") == 1
    assert statuses_by_pid(world.root)[1111] == ["SETTLED"]
    before_bad = (world.root / LEDGER).read_bytes()
    for bad in ("projects", "../x", "", "a\x00b"):                    # a run id that is not one is refused, nothing written
        with pytest.raises(res.ReservationError):
            mine.settle_owned("x", run_ids=[bad])
    assert (world.root / LEDGER).read_bytes() == before_bad
    empty = world.tmp / "empty-root"
    empty.mkdir()
    assert res.CapacityReservations(empty, **owner()).settle_owned("x") == 0
    assert list(empty.iterdir()) == []


def statuses_by_pid(root: Path) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for h in ledger(root)["holds"]:
        out.setdefault(h["owner_pid"], []).append(h["status"])
    return out


@pytest.mark.parametrize("how", ["not JSON", "a directory", "a link"])
def test_settle_owned_over_an_unusable_ledger_raises_and_changes_nothing(world, how):
    """Never a silent release: a ledger that is not JSON, a directory or a link raises `ReservationError`, and the
    spoiled file is exactly what it was."""
    res = _res()
    path = world.root / LEDGER
    world.root.mkdir(exist_ok=True)
    if how == "not JSON":
        path.write_bytes(b"{not json")
    elif how == "a directory":
        path.mkdir()
    else:
        (world.root / "elsewhere.json").write_text("{}")
        path.symlink_to(world.root / "elsewhere.json")
    with pytest.raises(res.ReservationError):
        res.CapacityReservations(world.root, **owner()).settle_owned("x")
    if how == "not JSON":
        assert path.read_bytes() == b"{not json"


# --- R3 the recovery ---------------------------------------------------------------------------------------------------------

def _recovery_world(tmp_path, monkeypatch, limit: int = 2, **cfg):
    import test_wave_recovery as wr
    repo, root, store = wr.world(tmp_path, **cfg)
    state = tmp_path / "state"
    _declare(state, limit)
    monkeypatch.setenv("HOH_STATE_ROOT", str(state))
    return wr, repo, root, store


def test_a_crash_left_waves_capacity_is_settled_by_the_recovery_together_with_its_holds(tmp_path, capsys, monkeypatch):
    """A controller dies between the wave record and RUNNING: its two budget holds AND its two capacity rows are left.
    The next drive's recovery ends the holds by id (as before) and settles the controller's capacity rows, says so
    (`WAVE_CAPACITY_SETTLED:<wave id>`), records it in the terminal outcome, and a further drive neither touches
    the ledger nor the outcome."""
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch)
    wave_id = wr.crash(root, capsys)
    assert statuses(root) == {"n1": "HELD", "n2": "HELD"}
    rc, out = wr.drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    path, outcome = wr.the_outcome(store)
    assert f"WAVE_RECOVERED:{wave_id}" in out["steps"] and f"WAVE_CAPACITY_SETTLED:{wave_id}" in out["steps"]
    assert "settled 2 capacity row(s)" in outcome.note
    first = [h for h in ledger(root)["holds"] if h["reason"].startswith(f"wave {wave_id}: recovered")]
    assert sorted((h["run_id"], h["status"]) for h in first) == [("n1", "SETTLED"), ("n2", "SETTLED")]
    assert "HELD" not in statuses_by_pid(root)[os.getpid()]
    fixed, bytes_ = wr.snapshot(path), (root / LEDGER).read_bytes()
    rc, again = wr.drive(root, capsys)
    assert wr.snapshot(path) == fixed and (root / LEDGER).read_bytes() == bytes_
    assert not [s for s in again["steps"] if s.startswith("WAVE_CAPACITY")], again["steps"]


@pytest.mark.parametrize("identity", ["matching", "unknown"])
def test_the_recovery_forwards_the_gone_controllers_pid_and_process_identity_to_the_ledger(
        tmp_path, capsys, monkeypatch, identity):
    """Review B's F1: every other recovery node crashes the controller in this very process, where the outcome's pid is
    ours. Here a crash-left outcome is made to name another, gone process (`pid` 2**22-7, `pid_start`
    `<this boot>:S1`), and the ledger holds a row of that pid with that identity (`ga`), a row of the same pid with another
    start time -- a reused pid's (`gc`) -- and the rows this process's crashed drive left. `matching`: the recovery ends
    `ga` (`SETTLED`, reason naming the wave and the pid), leaves `gc` (the gate's own reclaim later marks it `LOST`) and
    the crashed drive's own rows (they end `SUPERSEDED` by the next ask). `unknown`: the outcome recorded no identity --
    a foreign pid that cannot be told from a reused one -- and the recovery settles nothing of the ledger at all."""
    import json

    from hoh.succession import boot_id
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch, limit=6)
    wave_id = wr.crash(root, capsys)
    gone, here = 2**22 - 7, boot_id()

    document = ledger(root)                              # the rows are written in place: a second owner object would judge the others
    template = document["holds"][0]
    for run_id, start, thread in (("ga", "S1", 5), ("gc", "S2", 6)):
        document["holds"].append(dict(template, reservation_id=f"forged-{run_id}", run_id=run_id, owner_pid=gone,
                                      owner_thread=thread, owner_boot=here, owner_start=start))
    (root / LEDGER).write_text(json.dumps(document))
    path, _ = wr.the_outcome(store)
    wr.edit(path, lambda d: d.update(pid=gone, pid_start=f"{here}:S1" if identity == "matching" else None))
    rc, out = wr.drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    rows = {h["run_id"]: h for h in ledger(root)["holds"] if h["owner_pid"] == gone}
    recovered = [h["run_id"] for h in ledger(root)["holds"] if h["reason"].startswith(f"wave {wave_id}: recovered")]
    if identity == "matching":
        assert recovered == ["ga"] and rows["ga"]["status"] == "SETTLED" and f"(pid {gone})" in rows["ga"]["reason"]
        assert f"WAVE_CAPACITY_SETTLED:{wave_id}" in out["steps"]
    else:
        assert recovered == [] and rows["ga"]["status"] == "LOST"
        assert f"WAVE_CAPACITY_SETTLED:{wave_id}" not in out["steps"]
    assert rows["gc"]["status"] == "LOST" and "no longer exists" in rows["gc"]["reason"]        # not the recovery's
    mine = [h for h in ledger(root)["holds"] if h["owner_pid"] == os.getpid() and h["run_id"] in ("n1", "n2")][:2]
    assert {h["status"] for h in mine} == {"SUPERSEDED"} and not any(h["reason"].startswith("wave") and "recovered" in h["reason"] for h in mine)


def test_the_recovery_leaves_the_rows_of_a_live_controller_in_the_same_process_alone(tmp_path, capsys, monkeypatch):
    """Node ids are project-local and a process may drive several projects on several threads: the outcome of a
    crash-left wave names a pid, and that pid is this process -- so the recovery settles only this thread's rows. A row
    of the same node id held by another, live thread (another project's controller) stays `HELD`."""
    res = _res()
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch, limit=3)
    wave_id = wr.crash(root, capsys)
    stop = threading.Event()
    other = threading.Thread(target=stop.wait, args=(60,))
    other.start()
    try:
        res.CapacityReservations(root, thread=other.ident).reserve_atomically(
            lambda: [("n1", "machine", "host.sessions", 1, "sessions")])
        rc, out = wr.drive(root, capsys)
        assert f"WAVE_CAPACITY_SETTLED:{wave_id}" in out["steps"], out["steps"]
        foreign = [h for h in ledger(root)["holds"] if h["owner_thread"] == other.ident]
        assert [(h["run_id"], h["status"]) for h in foreign] == [("n1", "HELD")]
    finally:
        stop.set()
        other.join(timeout=30)


def test_a_capacity_only_host_has_its_crash_left_rows_settled_though_no_budget_hold_was_adopted(
        tmp_path, capsys, monkeypatch):
    """No shared budget: the outcome adopted no holds (`NOT_APPLICABLE`), but the dead controller's capacity rows are
    still ended by the recovery."""
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch, budget=None, share=None)
    wave_id = wr.crash(root, capsys)
    assert statuses(root) == {"n1": "HELD", "n2": "HELD"}
    rc, out = wr.drive(root, capsys)
    path, outcome = wr.the_outcome(store)
    assert outcome.phase is wr.waverecovery.OutcomePhase.NOT_APPLICABLE and "settled 2 capacity row(s)" in outcome.note
    assert f"WAVE_CAPACITY_SETTLED:{wave_id}" in out["steps"]
    assert all(h["status"] == "SETTLED" for h in ledger(root)["holds"] if h["reason"].startswith(f"wave {wave_id}:"))


def test_a_recovery_over_an_unusable_ledger_names_it_and_still_ends_the_budget_holds(tmp_path, capsys, monkeypatch):
    """A ledger that is not JSON does not keep the budget's holds waiting and is not repaired: the outcome becomes
    RECOVERED with a note that the capacity rows were NOT settled, an event/step says so, the ledger's bytes are the
    spoiler's, and the drive then halts `CAPACITY_WAIT` at the gate on the same ledger -- visible twice, released
    never."""
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch)
    wave_id = wr.crash(root, capsys)
    (root / LEDGER).write_bytes(b"{not json")
    rc, out = wr.drive(root, capsys)
    path, outcome = wr.the_outcome(store)
    assert outcome.phase is wr.waverecovery.OutcomePhase.RECOVERED and "NOT settled" in outcome.note
    assert f"WAVE_CAPACITY_UNSETTLED:{wave_id}" in out["steps"] and f"WAVE_RECOVERED:{wave_id}" in out["steps"]
    assert (root / LEDGER).read_bytes() == b"{not json"
    assert out["halt"] == "CAPACITY_WAIT", out


def _open_outcome(directory: Path, wave_id: str, **kw):
    from hoh import waverecovery
    return waverecovery.open_outcome(directory, wave_id, session=None, epoch=None, **kw)


def _recover(directory: Path, capacity, **kw):
    from hoh import waverecovery
    return waverecovery.recover_open(
        directory, authorise=lambda: None, session=None, epoch=None, claim=lambda: None, probe=lambda node: None,
        reservations=None, spent=None, waves_dir=directory.parent / "waves", capacity=capacity, **kw)


def test_the_recovery_calls_the_capacity_step_once_per_terminal_outcome_and_never_again(tmp_path):
    """Termination, point by point, at the unit: an outcome with nothing adopted is `NOT_APPLICABLE` -- the capacity
    callback is called once with the outcome and a reason naming the wave, its count lands in the outcome's note and in
    an event; a further pass finds a terminal outcome and calls nothing (bytes of the outcome unchanged); a callback that
    raises does not stop the outcome from becoming terminal -- the note and the event name it, there is no retry."""
    from hoh import waverecovery
    directory = tmp_path / "wave-outcomes"
    calls: list[tuple[str, str]] = []
    _open_outcome(directory, "w1-1-aa", pid=2**22 - 5, pid_start_token=None)
    report = _recover(directory, lambda outcome, reason: (calls.append((outcome.wave_id, reason)), 2)[1])
    assert [c[0] for c in calls] == ["w1-1-aa"] and "w1-1-aa" in calls[0][1]
    assert [e.kind for e in report.events] == ["CAPACITY_SETTLED"]
    path = waverecovery.outcome_path(directory, "w1-1-aa")
    outcome = waverecovery.read_outcome(path)
    assert outcome.phase is waverecovery.OutcomePhase.NOT_APPLICABLE and "settled 2 capacity row(s)" in outcome.note
    fixed = path.read_bytes()
    again = _recover(directory, lambda outcome, reason: calls.append((outcome.wave_id, "again")) or 0)
    assert len(calls) == 1 and again.events == [] and path.read_bytes() == fixed

    def broken(outcome, reason):
        raise RuntimeError("the ledger is unreadable")
    _open_outcome(directory, "w2-1-bb", pid=2**22 - 5, pid_start_token=None)
    report = _recover(directory, broken)
    [event] = report.events
    assert event.kind == "CAPACITY_UNSETTLED" and "unreadable" in event.detail
    second = waverecovery.read_outcome(waverecovery.outcome_path(directory, "w2-1-bb"))
    assert second.phase is waverecovery.OutcomePhase.NOT_APPLICABLE and "NOT settled" in second.note and second.attempts == 1


def test_a_live_owner_and_an_abandoned_outcome_never_reach_the_capacity_step(tmp_path):
    """The same rules as the budget holds: an outcome whose owner process is alive is waited for (counted, bounded) and
    one past its attempts is abandoned -- neither ends anything, so the capacity callback is never called."""
    from hoh import waverecovery
    directory = tmp_path / "wave-outcomes"
    calls: list[int] = []
    _open_outcome(directory, "w1-1-aa", pid=os.getppid(), pid_start_token=None)             # alive, another process
    for _ in range(waverecovery.MAX_WAITS + 1):
        report = _recover(directory, lambda outcome, reason: calls.append(1) or 0)
    outcome = waverecovery.read_outcome(waverecovery.outcome_path(directory, "w1-1-aa"))
    assert outcome.phase is waverecovery.OutcomePhase.ABANDONED and calls == []
    assert [e.kind for e in report.events] == ["ABANDONED"]
