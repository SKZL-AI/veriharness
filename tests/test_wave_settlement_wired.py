"""P3-22 / P3-23 / P3-24, slice 2: the settlement, the QA reserve and the degradation
reason are reached by the production path.

The path is `hoh project drive` (cli.main) -> resumeloop.drive_governed ->
ProjectController.run -> `_wave_round` -> the wave executor -> the real
HohRunLauncher (only the process boundary is replaced, as in
`tests/test_wave_controller.py`, whose harness this file imports). Everything is
hermetic: repositories, worktrees, ledgers and records are under `tmp_path`; no agent
is started; the driver factory is imported by name through `driver.json`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_wave_controller import FakeCliLauncher, Gates, _held_ids, _nodes, _world
from hoh import cli
from hoh.launcher import Dispatch, HohRunLauncher, RunVerdict
from hoh.project import Lifecycle
from hoh.reservation import BudgetReservations, HoldStatus
from hoh.wave import NodeState, read_record, settle_wave
from hoh.wslease import WorkspaceLease

CONFIG: dict = {}
HERE = Path(__file__).resolve().parent


class Launcher(FakeCliLauncher):
    """The wave-controller harness plus two faults of a shared ledger's life:
    `leak` names runs whose launch cannot end its hold (a busy ledger: the launcher
    notes it and the hold stays open), and the script word "inflight" is a `hoh run`
    that finds another process executing the run."""

    def __init__(self, *a, leak=(), **kw):
        super().__init__(*a, **kw)
        self.leak = set(leak)

    def _settle(self, run_id, reason):
        if run_id in self.leak and reason == "the dispatch returned":
            self.reservation_notes.append(f"the hold of {run_id} stays open: ledger busy")
            return
        super()._settle(run_id, reason)

    def _hoh_run(self, run_id):
        if self.script.get(run_id) == "inflight":
            self.events.append(("start", run_id, 0.0))
            return Dispatch(2, "", f"Run {run_id} is already being executed by pid 4242")
        return super()._hoh_run(run_id)


def build_driver(store):
    """The `driver.json` factory: the real launcher, a budget, a fake process boundary."""
    cfg = CONFIG
    # the reserve is passed only when a test asks for one, so that the tests of the
    # settlement build exactly the launcher the base has
    extra = {"qa_reserve": cfg["qa_reserve"]} if cfg.get("qa_reserve") else {}
    launcher = Launcher(cfg["root"], cfg["repo"], cfg["script"], leak=cfg.get("leak", ()),
                        dispatch_budget=cfg.get("budget"), dispatch_reservation=cfg.get("share"),
                        wave_width=cfg.get("width", 1), **extra)
    for key, hook in cfg.get("hooks", {}).items():
        launcher.hooks[key] = hook
    cfg["launcher"] = launcher
    return launcher, Gates()


def drive(tmp_path, monkeypatch, capsys, names=("n1", "n2"), by_name=None, **cfg):
    repo, root, store = _world(tmp_path, names=names, by_name=by_name)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, **cfg)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_settlement_wired:build_driver", "path": str(HERE)}))
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    out = json.loads(capsys.readouterr().out)
    return repo, root, store, rc, out


def the_record(store):
    [path] = sorted((store.dir / "waves").iterdir())
    return path, read_record(path)


# --------------------------------------------------------------------------- #
# P3-22: the controller ends what a launch could not end
# --------------------------------------------------------------------------- #

def test_hoh_project_drive_ends_through_settle_wave_a_hold_its_launch_could_not_end(
        tmp_path, monkeypatch, capsys):
    """Catches a leaked hold nobody ends: n2's launch cannot settle its hold (the
    launcher notes it and leaves it open for a dispatch that is over), so on the base
    the budget stays smaller for good. Through cli.py -> drive_governed ->
    ProjectController -> `_wave_round` the wave's own record is read back and the hold
    ended by its id: `WAVE_SETTLED`, a SETTLED row whose reason names the wave, and the
    other node's row untouched (its launch ended it)."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "ok"}, budget=6, share=3,
        width=2, leak=("n2",))
    assert rc == 0 and out["halt"] == "CLOSED", out
    [wave_step] = [s for s in out["steps"] if s.startswith("WAVE:")]
    wave_id = wave_step.split(":", 1)[1]
    assert f"WAVE_SETTLED:{wave_id}" in out["steps"]
    assert not [s for s in out["steps"] if s.startswith("WAVE_DEGRADED")]   # a full wave says nothing
    assert _held_ids(root) == []
    rows = {r.run_id: r for r in BudgetReservations(root).read()}
    assert rows["n1"].reason == "the dispatch returned"
    assert rows["n2"].reason == f"wave {wave_id} settled"
    assert rows["n2"].status is HoldStatus.SETTLED
    assert [n.lifecycle for n in _nodes(store).values()] == [Lifecycle.MERGED] * 2
    assert CONFIG["launcher"].reservation_notes == ["the hold of n2 stays open: ledger busy"]


def test_a_wave_in_which_every_launch_ended_its_hold_adds_no_settlement_step(
        tmp_path, monkeypatch, capsys):
    """Catches the second chance reporting itself when there was nothing to end (a noisy
    step stream), and the settlement writing the ledger for nothing."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "ok"}, budget=6, share=3, width=2)
    assert out["halt"] == "CLOSED", out
    assert not [s for s in out["steps"] if s.startswith(("WAVE_SETTLE", "WAVE_DEGRADED"))]
    assert {r.reason for r in BudgetReservations(root).read()} == {"the dispatch returned"}


def test_the_hold_of_a_run_another_process_executes_survives_the_settlement_of_its_wave(
        tmp_path, monkeypatch, capsys):
    """Catches the settlement ending a hold under a live run (the L2 caveat of P3-02s2r):
    n1's hold is leaked and is ended, n2's `hoh run` finds another process executing the
    run (IN_FLIGHT, a race the pre-launch evaluation cannot see) and `launch` keeps its
    hold by design. The record says IN_FLIGHT; `settle_wave` skips the row; the hold is
    still HELD after the drive, and the row is reported IN_FLIGHT, never ENDED."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "inflight"}, budget=6, share=3,
        width=2, leak=("n1",))
    path, record = the_record(store)
    assert record.node("n2").state is NodeState.RETURNED and record.node("n2").verdict == "IN_FLIGHT"
    wave_id = record.wave_id
    assert f"WAVE_SETTLED:{wave_id}" in out["steps"]                  # n1's leaked hold was ended
    rows = {r.run_id: r for r in BudgetReservations(root).read()}
    assert rows["n1"].status is HoldStatus.SETTLED and rows["n1"].reason == f"wave {wave_id} settled"
    assert rows["n2"].status is HoldStatus.HELD                      # the live run's hold
    assert _held_ids(root) == ["n2"]
    launcher = CONFIG["launcher"]
    again = settle_wave(path, BudgetReservations(root), launcher.spent_by_run)
    assert [(n.node_id, n.state, n.released) for n in again.nodes] == [
        ("n1", "ALREADY_ENDED", 3), ("n2", "IN_FLIGHT", 0)]
    assert _held_ids(root) == ["n2"]


def test_settle_wave_skips_exactly_the_launchers_in_flight_verdict(tmp_path):
    """Catches a settlement whose idea of "another process executes it" drifts from the
    launcher's: the name `settle_wave` skips is `RunVerdict.IN_FLIGHT.value`, and every
    other verdict of a returned node (and an errored one) is ended -- the rule of
    `HohRunLauncher.launch`, which keeps a hold only for IN_FLIGHT."""
    from test_wave_settlement import Rig, leaky
    from hoh import wave
    verdicts = {"n1": "ACCEPTED", "n2": "IN_FLIGHT", "n3": "UNDETERMINED", "n4": "REJECTED"}
    rig = Rig(tmp_path, list(verdicts) + ["n5"], ceiling=40, want=4, width=5)

    def launch(node, admission):
        rig.spent[node.id] = 1
        if node.id == "n5":
            raise RuntimeError("the dispatch raised")
        return SimpleNamespace(verdict=verdicts[node.id])

    res = rig.executor(reservations=leaky(rig)).run_wave("w1", rig.plan(*verdicts, "n5"), launch)
    assert {n.node_id: n.holds_ended for n in res.record.nodes} == {k: None for k in (*verdicts, "n5")}
    assert len(rig.held()) == 5
    settlement = rig.settle("w1")
    assert {n.node_id: n.state for n in settlement.nodes} == {
        "n1": "ENDED", "n2": "IN_FLIGHT", "n3": "ENDED", "n4": "ENDED", "n5": "ENDED"}
    assert [r for r, _ in rig.held()] == ["n2"]
    assert {n.node_id: n.used for n in settlement.nodes} == {k: 1 for k in ("n1", "n2", "n3", "n4", "n5")}
    assert settlement.released == 4 * 3 and [n.released for n in settlement.nodes if n.node_id == "n2"] == [0]
    # a second call changes nothing, and is still not a decision about the live run
    before = rig.ledger_bytes()
    assert {n.node_id: n.state for n in rig.settle("w1").nodes}["n2"] == "IN_FLIGHT"
    assert rig.ledger_bytes() == before
    assert wave._IN_FLIGHT == RunVerdict.IN_FLIGHT.value == "IN_FLIGHT"      # the name is the launcher's


def test_a_settlement_that_cannot_run_is_a_note_in_the_steps_not_a_halt(
        tmp_path, monkeypatch, capsys):
    """Catches the second chance turning a refusal into the end of the drive: `settle_wave`
    raises after a wave that ran and merged everything -- a `WaveRecordError` (a record that
    is not one) and a plain `RuntimeError` (anything else: a ledger that vanished) -- and
    the loop reports `WAVE_SETTLE_FAILED` and closes the project; any `Exception` is a note."""
    import hoh.orchestrator as orchestrator
    from hoh.wave import WaveRecordError

    for i, exc in enumerate((WaveRecordError("the record is not a wave record"),
                             RuntimeError("the ledger disappeared"))):
        def refuse(*args, _exc=exc, **kwargs):
            raise _exc

        monkeypatch.setattr(orchestrator, "settle_wave", refuse, raising=False)
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        repo, root, store, rc, out = drive(
            sub, monkeypatch, capsys, script={"n1": "ok", "n2": "ok"}, budget=6, share=3, width=2)
        path, record = the_record(store)
        assert out["halt"] == "CLOSED", (type(exc).__name__, out)
        assert f"WAVE_SETTLE_FAILED:{record.wave_id}" in out["steps"], type(exc).__name__
        assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.MERGED}


# --------------------------------------------------------------------------- #
# P3-23: the launcher keeps the QA reserve for the repair of what integration found
# --------------------------------------------------------------------------- #

def test_hoh_project_drive_keeps_the_qa_reserve_from_developer_nodes_and_gives_it_to_a_repair(
        tmp_path, monkeypatch, capsys):
    """Catches a reserve that holds only in the executor (L2 of the first slice) and not
    through the launcher a controller wave actually uses: budget 6, a share of 2, a
    reserve of 2. Three developer nodes: the third finds only the reserve left, is refused
    by `prepare` **before** its worktree exists, and parks BLOCKED naming the reserve;
    the same wave with a repair node in the third place (the node a failed gate
    creates: `repair_of`) is granted the reserve and runs."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, names=("n1", "n2", "n3"),
        script={"n1": "ok", "n2": "ok", "n3": "ok"}, budget=6, share=2, width=3, qa_reserve=2)
    nodes = _nodes(store)
    assert nodes["n1"].lifecycle is Lifecycle.MERGED and nodes["n2"].lifecycle is Lifecycle.MERGED
    assert nodes["n3"].lifecycle is Lifecycle.BLOCKED
    assert "QA reserve of 2" in nodes["n3"].note and "repair" in nodes["n3"].note
    assert "BUDGET_EXHAUSTED:n3" in out["steps"], out            # the sibling is blocked alone
    assert out["halt"] == "BLOCKED_DEPENDENCY", out              # and the loop ends on it
    # a node left out at `prepare` is its own step; the wave the executor was given ran whole
    assert not [s for s in out["steps"] if s.startswith("WAVE_DEGRADED")], out["steps"]
    launcher = CONFIG["launcher"]
    named = re.compile(r"(?<![A-Za-z0-9])n3(?![A-Za-z0-9])")      # not the "n3" of "python3"
    assert not [c for c in launcher.calls if any(named.search(part) for part in c)]  # nothing was created for it
    starts = [c for c in launcher.calls if "start" in c]
    assert [c[c.index("--max-dispatches") + 1] for c in starts] == ["2", "2"]

    other = tmp_path / "repair"
    other.mkdir()
    repo, root, store, rc, out = drive(
        other, monkeypatch, capsys, names=("n1", "n2", "r1"), by_name={"r1": {"repair_of": "n0"}},
        script={"n1": "ok", "n2": "ok", "r1": "ok"}, budget=6, share=2, width=3, qa_reserve=2)
    assert out["halt"] == "CLOSED", out
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.MERGED}
    starts = [c for c in CONFIG["launcher"].calls if "start" in c]
    assert sorted(c[c.index("--max-dispatches") + 1] for c in starts) == ["2", "2", "2"]


def test_the_launcher_refuses_a_reserve_that_is_not_a_whole_number_of_at_least_zero(tmp_path):
    """Catches a reserve of the wrong type or sign being accepted (a `bool` is an `int`,
    a float or a string is not a count); every case is tried and all failures are listed."""
    repo, root, store = _world(tmp_path)
    accepted = []
    for bad in (-1, True, 1.5, "1", None):
        try:
            HohRunLauncher(root, repo, dispatch_budget=6, qa_reserve=bad)
        except ValueError as exc:
            if "qa_reserve" not in str(exc):
                accepted.append((bad, str(exc)))
        else:
            accepted.append((bad, "accepted"))
    assert accepted == []


def test_the_launcher_refuses_a_reserve_with_no_budget_or_of_the_whole_budget(tmp_path):
    """Catches the two reserves that make no sense -- nothing to keep it from, and all of
    the budget (every node that is not a repair would be refused) -- and the boundary:
    5 of 6 is accepted, 0 is the default."""
    repo, root, store = _world(tmp_path)
    problems = []
    for kwargs, match in (({"qa_reserve": 1}, "needs a dispatch_budget"),
                          ({"dispatch_budget": 6, "qa_reserve": 6}, "below the dispatch_budget"),
                          ({"dispatch_budget": 6, "qa_reserve": 7}, "below the dispatch_budget")):
        try:
            HohRunLauncher(root, repo, **kwargs)
        except ValueError as exc:
            if match not in str(exc):
                problems.append((kwargs, str(exc)))
        else:
            problems.append((kwargs, "accepted"))
    assert problems == []
    assert HohRunLauncher(root, repo, dispatch_budget=6, qa_reserve=5).qa_reserve == 5
    assert HohRunLauncher(root, repo).qa_reserve == 0


def _resumed(tmp_path):
    """Four runs that already exist (an earlier drive started them) and a launcher with
    budget 6, share 2, reserve 2 that holds n1 and n2: 2 are left, and all of it is the reserve."""
    repo, root, store = _world(tmp_path, names=("n1", "n2", "n3", "r1"),
                               by_name={"r1": {"repair_of": "n0"}})
    launcher = Launcher(root, repo, {}, dispatch_budget=6, dispatch_reservation=2, qa_reserve=2)
    n1, n2, n3, r1 = (store.read_state().node(n) for n in ("n1", "n2", "n3", "r1"))
    assert launcher.prepare(n1) is None and launcher.prepare(n2) is None
    for node in (n3, r1):
        assert Launcher(root, repo, {}).prepare(node) is None
    assert BudgetReservations(root).left("n3", ceiling=6, spent=launcher.spent_by_run) == 2
    return root, launcher, n3, r1


def test_a_resumed_run_of_a_developer_node_is_refused_the_reserve(tmp_path):
    """Catches `take_hold` -- the second way a hold is taken, for a run that already
    exists -- forgetting the reserve: only the reserve is left, the developer node is
    refused with a message that names it, and nothing is held for it."""
    from hoh.launcher import BudgetExhausted
    root, launcher, n3, r1 = _resumed(tmp_path)
    with pytest.raises(BudgetExhausted, match="QA reserve of 2"):
        launcher.take_hold(n3)
    assert _held_ids(root) == ["n1", "n2"]


def test_a_resumed_repair_node_is_granted_the_reserve(tmp_path):
    """Catches `take_hold` refusing a repair node the reserve exists for."""
    root, launcher, n3, r1 = _resumed(tmp_path)
    assert launcher.take_hold(r1) is True
    assert _held_ids(root) == ["n1", "n2", "r1"]
    assert [r.amount for r in BudgetReservations(root).held() if r.run_id == "r1"] == [2]


def test_the_hold_prepare_takes_is_trimmed_to_leave_the_reserve(tmp_path, monkeypatch, capsys):
    """Catches `prepare`'s own hold forgetting the reserve while the early refusal still
    saves the day: with budget 6 every node is either granted its share or refused whole,
    so the grant `min(want, left - keep)` is never reached. With budget 7, a share of 2 and
    a reserve of 2 the third developer node finds 3 left and may hold only 1: the starts
    carry `--max-dispatches` 2, 2 and 1, and 2 of the 7 stay unheld."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, names=("n1", "n2", "n3"),
        script={"n1": "ok", "n2": "ok", "n3": "ok"}, budget=7, share=2, width=3, qa_reserve=2)
    assert out["halt"] == "CLOSED", out
    starts = [c for c in CONFIG["launcher"].calls if "start" in c]
    assert sorted(c[c.index("--max-dispatches") + 1] for c in starts) == ["1", "2", "2"], out["steps"]
    assert sorted(r.amount for r in BudgetReservations(root).read()) == [1, 2, 2]


# --------------------------------------------------------------------------- #
# P3-24: the reason a wave ran narrower is said where a person reads the drive
# --------------------------------------------------------------------------- #

def test_hoh_project_drive_says_why_the_executor_admitted_fewer_nodes_than_it_was_given(
        tmp_path, monkeypatch, capsys):
    """Catches a degraded wave that is only a row in a file: n2's worktree is held by
    someone else when the wave admits it (WORKSPACE refusal), so the wave ran at 1 of 2.
    `hoh project drive` prints `WAVE_DEGRADED:<wave id>:WORKSPACE` among its steps, the
    reasons are those `WaveRecord.degradation` reads from the persisted record, and a
    wave that ran at full width prints nothing (the first test)."""
    keep = []

    def occupy(worktree):
        handle = WorkspaceLease(worktree, "someone-else")
        handle.acquire()
        keep.append(handle)

    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "ok"}, budget=6, share=3,
        width=2, hooks={"n2": occupy})
    path, record = the_record(store)
    degraded = [s for s in out["steps"] if s.startswith("WAVE_DEGRADED:")]
    assert degraded == [f"WAVE_DEGRADED:{record.wave_id}:WORKSPACE"], out["steps"]
    assert record.degradation.reasons[0].value == "WORKSPACE"
    assert (record.degradation.ready, record.degradation.admitted) == (2, 1)
    assert record.node("n2").refusal == "WORKSPACE"

    # two reasons at once, each printed, in the vocabulary's order: n3 reads what n1 writes, so
    # the planner defers it (CONFLICT), and n2's worktree is held by someone else (WORKSPACE)
    sub = tmp_path / "two"
    sub.mkdir()
    repo, root, store, rc, out = drive(
        sub, monkeypatch, capsys, names=("n1", "n2", "n3"),
        by_name={"n3": {"semantic_reads": ["n1.txt"]}},
        script={"n1": "ok", "n2": "ok", "n3": "ok"}, budget=9, share=3, width=2, hooks={"n2": occupy})
    first = sorted((store.dir / "waves").iterdir())[0]
    record = read_record(first)
    assert [s for s in out["steps"] if s.startswith("WAVE_DEGRADED:")][0] == (
        f"WAVE_DEGRADED:{record.wave_id}:WORKSPACE,CONFLICT"), out["steps"]
    assert [r.value for r in record.degradation.reasons] == ["WORKSPACE", "CONFLICT"]


def test_a_degraded_wave_is_reported_with_no_shared_budget_at_all(tmp_path, monkeypatch, capsys):
    """Catches the degradation step hiding behind the budget guard of the second chance: a
    drive with no `dispatch_budget` (the default) still says why a wave ran narrower."""
    keep = []

    def occupy(worktree):
        handle = WorkspaceLease(worktree, "someone-else")
        handle.acquire()
        keep.append(handle)

    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "ok"}, width=2, hooks={"n2": occupy})
    path, record = the_record(store)
    assert CONFIG["launcher"].dispatch_budget is None
    assert [s for s in out["steps"] if s.startswith("WAVE_DEGRADED:")] == [
        f"WAVE_DEGRADED:{record.wave_id}:WORKSPACE"], out["steps"]
    assert not [s for s in out["steps"] if s.startswith("WAVE_SETTLE")]


def test_a_wave_whose_only_open_hold_is_a_live_runs_reports_nothing_settled(
        tmp_path, monkeypatch, capsys):
    """Catches `WAVE_SETTLED` being printed for a hold the settlement did not end: n1's launch
    ended its own hold, n2's run is IN_FLIGHT (its hold is kept, and skipped), so there was
    nothing to end and no settlement step may claim there was."""
    repo, root, store, rc, out = drive(
        tmp_path, monkeypatch, capsys, script={"n1": "ok", "n2": "inflight"}, budget=6, share=3,
        width=2)
    path, record = the_record(store)
    assert record.node("n2").verdict == "IN_FLIGHT"
    assert not [s for s in out["steps"] if s.startswith("WAVE_SETTLE")], out["steps"]
    assert _held_ids(root) == ["n2"]
