"""P3-24 slice 4: every cut the controller makes is persisted, per node, with its reason.

A wave round leaves nodes out *before* the executor is asked: the capacity gate (P3-19),
the backpressure gate (P3-07), a budget that cannot hold for a node (`prepare`, `take_hold`),
a node that carries an external action, a run another process executes, an obstacle that
blocks the node. Until this slice only the executor's own reasons reached the wave's outcome.
Now the outcome says, with the vocabulary of `ParallelismDegradationReason`: how many nodes
the plan was made from, how many ran, why not more, and **which node was cut for which
reason**. A round that launches no wave says it in `wave-latest-cuts.json`.

The tests drive `ProjectController` (the harness of `tests/test_wave_controller.py`,
`tests/test_wave_capacity.py` and `tests/test_backpressure_wave.py`; only the launcher's
process boundary is a fake) and `hoh project drive` through `cli.main`. Hermetic: everything
is under `tmp_path`."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import test_backpressure_wave as bpw
import test_wave_capacity as capw
import test_wave_controller as twc
from hoh import durable_io, waverecovery
from hoh.orchestrator import HaltClass, ProjectController
from hoh.project import ActionClass, Lifecycle

OUT = waverecovery.OUTCOME_DIR
#: named with defaults so that on the base the nodes fail on the behaviour that is missing
ROUND_ID = getattr(waverecovery, "ROUND_ID", "latest-cuts")
MAX_CUT_DETAIL = getattr(waverecovery, "MAX_CUT_DETAIL", 200)


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def cap_world(tmp_path):
    return capw.World(tmp_path)


@pytest.fixture
def bp_world(tmp_path):
    return bpw.World(tmp_path)


def outcomes(store) -> dict:
    d = store.dir / OUT
    return {p.name: waverecovery.read_outcome(p) for p in sorted(d.iterdir())} if d.is_dir() else {}


def wave_outcomes(store) -> list:
    return [o for n, o in outcomes(store).items() if o.wave_id != ROUND_ID]


def the_round(store):
    return outcomes(store).get(f"wave-{ROUND_ID}.json")


def cuts_of(outcome) -> list[tuple[str, str]]:
    assert hasattr(outcome, "cuts"), "an outcome of this tree has no cuts"
    return [(c.node_id, c.reason) for c in outcome.cuts]


def snapshot(path: Path):
    st = os.stat(path)
    return path.read_bytes(), st.st_ino, st.st_mtime_ns


# --------------------------------------------------------------------------- #
# each cut of the controller, in the outcome of the wave it narrowed
# --------------------------------------------------------------------------- #

def test_a_node_the_capacity_gate_leaves_out_is_in_the_outcome_of_the_wave_with_its_reason(cap_world):
    """Catches the capacity cut that never reached the record: three nodes at width 3 and
    room for two sessions -> the wave's outcome says 2 of 3 ran, reason CAPACITY, and names
    n3 with the scheduler's own words; no round launched nothing, so there is no
    `latest-cuts` file."""
    cap_world.sessions(2)
    launcher = cap_world.launcher()
    result = ProjectController(cap_world.store, launcher, capw.Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    first, *later = wave_outcomes(cap_world.store)
    assert first.phase is waverecovery.OutcomePhase.CLOSED
    assert first.degradation == (3, 2, ("CAPACITY",)), first.degradation
    assert cuts_of(first) == [("n3", "CAPACITY")] and "exhausted" in first.cuts[0].detail
    assert later == [] and the_round(cap_world.store) is None


def test_a_round_the_capacity_gate_halts_is_in_the_latest_cuts_outcome_and_is_written_once(
        cap_world):
    """Catches a halting round that leaves no trace, and one that leaves a new file every
    time a continuation unit drives it again: a stale registry halts CAPACITY_WAIT with every
    node READY; `wave-latest-cuts.json` says 3 of 3 cut (0 ran), reason CAPACITY, one row per
    node; three more drives -- the registry's age in each detail moves with the clock --
    leave it byte for byte (bytes, inode, mtime) and add no file."""
    cap_world.sessions(5, as_of=capw.utc(-10 * 86400))
    result = ProjectController(cap_world.store, cap_world.launcher(), capw.Gates(), width=3).run()
    assert result.halt is HaltClass.CAPACITY_WAIT
    latest = the_round(cap_world.store)
    assert latest.phase is waverecovery.OutcomePhase.CLOSED and latest.holds == ()
    assert latest.degradation == (3, 0, ("CAPACITY",))
    assert cuts_of(latest) == [("n1", "CAPACITY"), ("n2", "CAPACITY"), ("n3", "CAPACITY")]
    assert all("stale" in c.detail for c in latest.cuts)
    time.sleep(1.1)                        # the age "stated N s ago" in each detail has moved
    assert {n.lifecycle for n in cap_world.store.read_state().nodes} == {Lifecycle.READY}
    path = cap_world.store.dir / OUT / f"wave-{ROUND_ID}.json"
    fixed, files = snapshot(path), sorted(p.name for p in path.parent.iterdir())
    for _ in range(3):
        again = ProjectController(cap_world.store, cap_world.launcher(), capw.Gates(), width=3).run()
        assert again.halt is HaltClass.CAPACITY_WAIT
        assert snapshot(path) == fixed and sorted(p.name for p in path.parent.iterdir()) == files


def test_hoh_project_drive_persists_the_backpressure_cut_of_every_wave_it_narrowed(
        bp_world, monkeypatch, capsys):
    """The production path: a limited provider narrows the wave (n2 and n3 wait in round 1,
    n3 in round 2), `hoh project drive` closes the project, and the outcome of each wave
    says so: 3 planned, 1 ran, BACKPRESSURE, and the nodes it cut with the gate's words."""
    bp_world.park("other-run")
    rc, out = bpw._drive(bp_world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    waves = sorted(wave_outcomes(bp_world.store), key=lambda o: o.created_at + o.wave_id)
    shapes = sorted((o.degradation, tuple(cuts_of(o))) for o in waves)
    assert shapes == [((2, 1, ("BACKPRESSURE",)), (("n3", "BACKPRESSURE"),)),
                      ((3, 1, ("BACKPRESSURE",)), (("n2", "BACKPRESSURE"), ("n3", "BACKPRESSURE")))]
    assert all("LIMITED" in c.detail for o in waves for c in o.cuts)


def test_a_node_the_budget_cannot_hold_for_is_in_the_outcome_whether_prepare_or_take_hold_refused(
        tmp_path):
    """Catches the budget cut at either place: (a) budget 3, share 3, two new runs: n2's
    `prepare` finds nothing left -> BUDGET for n2, node BLOCKED, the wave ran n1; (b) the
    same budget with n2 an existing run (`take_hold` refuses) -> BUDGET for n2, left READY."""
    repo, root, store = twc._world(tmp_path)
    launcher = twc.FakeCliLauncher(root, repo, {}, dispatch_budget=3, dispatch_reservation=3)
    ProjectController(store, launcher, twc.Gates(), width=2).run()
    [first] = wave_outcomes(store)[:1]
    assert first.degradation == (2, 1, ("BUDGET",)) and cuts_of(first) == [("n2", "BUDGET")]
    assert "no budget left" in first.cuts[0].detail

    other = tmp_path / "resumed"
    other.mkdir()
    repo, root, store = twc._world(other)
    twc._already_started(root, repo, store, "n2")
    launcher = twc.FakeCliLauncher(root, repo, {}, dispatch_budget=3, dispatch_reservation=3)
    ProjectController(store, launcher, twc.Gates(), width=2).run()
    [first, *_] = wave_outcomes(store)
    assert first.degradation == (2, 1, ("BUDGET",)) and cuts_of(first) == [("n2", "BUDGET")]
    assert "no budget to hold" in first.cuts[0].detail


def test_an_external_node_that_is_not_the_head_and_a_run_another_process_executes_are_cuts(tmp_path):
    """Catches the two silent skips: n2 carries an external action (not the head: skipped,
    EXTERNAL) and n3's run is executed by another process (a held process lock: IN_FLIGHT);
    n1 runs. The outcome says 3 planned, 1 ran, EXTERNAL and IN_FLIGHT, each with its node."""
    repo, root, store = twc._world(
        tmp_path, names=("n1", "n2", "n3"), by_name={"n2": {"action_class": ActionClass.EXTERNAL}})
    twc._already_started(root, repo, store, "n3")
    fd = twc._hold_process_lock(root, "n3")
    try:
        launcher = twc.FakeCliLauncher(root, repo, {}, dispatch_budget=9, dispatch_reservation=3)
        ProjectController(store, launcher, twc.Gates(), width=3).run()
    finally:
        twc._let_go(fd)
    [first, *_] = wave_outcomes(store)
    assert first.degradation == (3, 1, ("EXTERNAL", "IN_FLIGHT")), first.degradation
    assert sorted(cuts_of(first)) == [("n2", "EXTERNAL"), ("n3", "IN_FLIGHT")]


def test_a_head_the_round_halts_on_is_in_the_latest_cuts_outcome(tmp_path):
    """Catches a halt with no record: n1 (the head) is executed by another process ->
    RUN_IN_FLIGHT, nothing launched; `wave-latest-cuts.json` says 2 planned, 0 ran, IN_FLIGHT
    for n1 -- and no wave outcome exists."""
    repo, root, store = twc._world(tmp_path)
    twc._already_started(root, repo, store, "n1")
    fd = twc._hold_process_lock(root, "n1")
    try:
        launcher = twc.FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
        result = ProjectController(store, launcher, twc.Gates(), width=2).run()
    finally:
        twc._let_go(fd)
    assert result.halt is HaltClass.RUN_IN_FLIGHT, result.reason
    latest = the_round(store)
    assert latest.degradation == (2, 0, ("IN_FLIGHT",)) and cuts_of(latest) == [("n1", "IN_FLIGHT")]
    assert wave_outcomes(store) == []


def test_a_node_left_out_by_the_width_is_a_width_reason_and_a_wave_that_ran_whole_has_no_cut(tmp_path):
    """Catches a width narrowing that is invisible, and noise: three independent nodes at
    width 2 -> the first wave's outcome says 3 planned, 2 ran, WIDTH (no row: the node was
    never individually decided); two nodes at width 2 -> degradation None, no cuts, no
    `latest-cuts` file."""
    repo, root, store = twc._world(tmp_path, names=("n1", "n2", "n3"))
    ProjectController(store, twc.FakeCliLauncher(root, repo, {}), twc.Gates(), width=2).run()
    [first, *_] = wave_outcomes(store)
    assert first.degradation == (3, 2, ("WIDTH",)) and first.cuts == ()

    other = tmp_path / "whole"
    other.mkdir()
    repo, root, store = twc._world(other)
    result = ProjectController(store, twc.FakeCliLauncher(root, repo, {}), twc.Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED
    [only] = wave_outcomes(store)
    assert only.degradation is None and only.cuts == () and the_round(store) is None


# --------------------------------------------------------------------------- #
# the file: strict, versioned, and a failing write is a note
# --------------------------------------------------------------------------- #

def test_the_cuts_of_an_outcome_are_strict_and_a_slice_3_file_is_still_read(cap_world):
    """Catches a schema bump that strands what slice 3 wrote, and a cuts field read loosely:
    a `/1` file (no cuts) parses with `cuts == ()`; a `/2` file without `cuts`, a `/1` file
    with them, a repeated node, 65 cuts, a reason outside the vocabulary, a non-ASCII or
    over-long detail, a bool, an extra key are each `OutcomeError`."""
    cap_world.sessions(2)
    ProjectController(cap_world.store, cap_world.launcher(), capw.Gates(), width=3).run()
    [first, *_] = wave_outcomes(cap_world.store)
    path = cap_world.store.dir / OUT / f"wave-{first.wave_id}.json"
    good = json.loads(path.read_bytes())
    assert good["schema"] == "hoh-wave-outcome/2" and good["cuts"]

    def variant(**changes):
        d = json.loads(json.dumps(good))
        for key, value in changes.items():
            if value is KeyError:
                d.pop(key)
            else:
                d[key] = value
        return json.dumps(d).encode()

    one = {"node_id": "n3", "reason": "CAPACITY", "detail": "x"}
    old = waverecovery.parse(variant(schema="hoh-wave-outcome/1", cuts=KeyError))
    assert old.cuts == () and old.degradation == first.degradation
    bad = {
        "v2 without cuts": variant(cuts=KeyError),
        "v1 with cuts": variant(schema="hoh-wave-outcome/1"),
        "unknown schema": variant(schema="hoh-wave-outcome/3"),
        "node twice": variant(cuts=[one, one]),
        "65 cuts": variant(cuts=[{**one, "node_id": f"n{i}"} for i in range(65)]),
        "reason outside": variant(cuts=[{**one, "reason": "BECAUSE"}]),
        "reason lower": variant(cuts=[{**one, "reason": "capacity"}]),
        "detail non-ascii": variant(cuts=[{**one, "detail": "café"}]),
        "detail too long": variant(cuts=[{**one, "detail": "x" * (MAX_CUT_DETAIL + 1)}]),
        "detail bool": variant(cuts=[{**one, "detail": True}]),
        "extra key in a cut": variant(cuts=[{**one, "why": "x"}]),
        "cuts not a list": variant(cuts={"n3": "CAPACITY"}),
        "node id newline": variant(cuts=[{**one, "node_id": "n3\n"}]),
    }
    accepted = []
    for label, data in bad.items():
        try:
            waverecovery.parse(data)
        except waverecovery.OutcomeError:
            continue
        accepted.append(label)
    assert accepted == []
    assert waverecovery.parse(path.read_bytes()).cuts == first.cuts


def test_a_latest_cuts_outcome_that_cannot_be_written_is_a_note_and_not_a_halt_or_a_loop(
        cap_world, monkeypatch):
    """Catches a failing record that changes the project's halt or is retried: every write
    of the latest-cuts file fails (`OSError`); the round still halts CAPACITY_WAIT, the failure
    is one `WAVE_OUTCOME_UNWRITTEN:latest-cuts` step, and nothing is retried inside the drive."""
    cap_world.sessions(5, as_of=capw.utc(-10 * 86400))
    real = durable_io.durable_write

    def failing(path, data, *a, **k):
        if Path(path).name == f"wave-{ROUND_ID}.json":
            raise OSError(28, "No space left on device (injected)")
        return real(path, data, *a, **k)

    monkeypatch.setattr(durable_io, "durable_write", failing)
    result = ProjectController(cap_world.store, cap_world.launcher(), capw.Gates(), width=3).run()
    assert result.halt is HaltClass.CAPACITY_WAIT
    notes = [s for s in result.steps if s.kind == "WAVE_OUTCOME_UNWRITTEN"]
    assert [s.node for s in notes] == [ROUND_ID], result.steps
    assert the_round(cap_world.store) is None


# --------------------------------------------------------------------------- #
# one node per cut site: every place the controller leaves a node out
# --------------------------------------------------------------------------- #

def _two_nodes(tmp_path, **kw):
    repo, root, store = twc._world(tmp_path, **kw)
    return repo, root, store, twc.FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)


def test_a_node_whose_prepare_finds_an_obstacle_is_blocked_alone_and_is_a_cut(tmp_path):
    """Catches the BLOCKED cut that was never recorded: n2's `prepare` reports an obstacle
    (n2 BLOCKED alone, n1 the head still runs): the wave's outcome says 2 planned, 1 ran,
    BLOCKED, a row for n2 with the obstacle's words."""
    repo, root, store, launcher = _two_nodes(tmp_path)
    real = launcher.prepare
    launcher.prepare = lambda node: "preflight says no" if node.id == "n2" else real(node)
    ProjectController(store, launcher, twc.Gates(), width=2).run()
    [first, *_] = wave_outcomes(store)
    assert first.degradation == (2, 1, ("BLOCKED",)), first.degradation
    assert cuts_of(first) == [("n2", "BLOCKED")] and "preflight says no" in first.cuts[0].detail


def test_a_head_whose_prepare_finds_an_obstacle_halts_the_round_and_the_round_says_why(tmp_path):
    """Catches a halting head with no record: n1's `prepare` reports an obstacle ->
    BLOCKED_DEPENDENCY, nothing launched; `latest-cuts` says 2 planned, 0 ran, BLOCKED, n1."""
    repo, root, store, launcher = _two_nodes(tmp_path)
    launcher.prepare = lambda node: "preflight says no"
    result = ProjectController(store, launcher, twc.Gates(), width=2).run()
    assert result.halt is HaltClass.BLOCKED_DEPENDENCY, result.reason
    latest = the_round(store)
    assert latest.degradation == (2, 0, ("BLOCKED",)) and cuts_of(latest) == [("n1", "BLOCKED")]
    assert wave_outcomes(store) == []


def test_a_head_whose_prepare_finds_no_budget_halts_the_round_and_the_round_says_why(tmp_path):
    """Catches the same for the budget at `prepare`: n1's `prepare` raises `BudgetExhausted` ->
    BUDGET_EXHAUSTED; `latest-cuts` says 2 planned, 0 ran, BUDGET, n1 with the launcher's words."""
    from hoh.orchestrator import BudgetExhausted

    repo, root, store, launcher = _two_nodes(tmp_path)

    def empty(node):
        raise BudgetExhausted("nothing is left of the ceiling")

    launcher.prepare = empty
    result = ProjectController(store, launcher, twc.Gates(), width=2).run()
    assert result.halt is HaltClass.BUDGET_EXHAUSTED, result.reason
    latest = the_round(store)
    assert latest.degradation == (2, 0, ("BUDGET",)) and cuts_of(latest) == [("n1", "BUDGET")]
    assert "nothing is left" in latest.cuts[0].detail and wave_outcomes(store) == []


def test_a_head_the_budget_cannot_hold_for_halts_the_round_and_the_round_says_why(tmp_path):
    """Catches the same for `take_hold`: n1 is an existing run and the budget cannot hold for it
    -> BUDGET_EXHAUSTED ("no budget to hold"); `latest-cuts` says 2 planned, 0 ran, BUDGET, n1."""
    from hoh.orchestrator import BudgetExhausted

    repo, root, store, launcher = _two_nodes(tmp_path)

    def empty(node):
        raise BudgetExhausted("nothing is left to hold")

    launcher.take_hold = empty
    twc._already_started(root, repo, store, "n1")
    result = ProjectController(store, launcher, twc.Gates(), width=2).run()
    assert result.halt is HaltClass.BUDGET_EXHAUSTED, result.reason
    latest = the_round(store)
    assert latest.degradation == (2, 0, ("BUDGET",)) and cuts_of(latest) == [("n1", "BUDGET")]
    assert "no budget to hold" in latest.cuts[0].detail


def test_the_round_that_launched_nothing_counts_the_nodes_the_plan_deferred_as_ready(tmp_path):
    """Catches `ready` that forgets the deferrals: n3 reads what n2 writes, so the plan
    selects n1 and n2 and defers n3; n1's run is executed by another process (RUN_IN_FLIGHT).
    `latest-cuts` says **3** ready (2 selected + 1 deferred), 0 ran, IN_FLIGHT."""
    repo, root, store, launcher = _two_nodes(
        tmp_path, names=("n1", "n2", "n3"), by_name={"n3": {"semantic_reads": ["n2.txt"]}})
    twc._already_started(root, repo, store, "n1")
    fd = twc._hold_process_lock(root, "n1")
    try:
        result = ProjectController(store, launcher, twc.Gates(), width=3).run()
    finally:
        twc._let_go(fd)
    assert result.halt is HaltClass.RUN_IN_FLIGHT, result.reason
    assert the_round(store).degradation == (3, 0, ("IN_FLIGHT",))


def test_the_latest_cuts_file_is_replaced_when_the_round_changes_and_only_then(tmp_path):
    """Catches a record that goes stale or is rewritten for nothing -- the other half of
    CU2: (1) through the controller, a head executed elsewhere (IN_FLIGHT) and then a head with an
    obstacle (BLOCKED): the file is **replaced**; (2) `record_round` writes an absent file,
    does nothing for the same round, and replaces it when only the cuts differ (same counts and
    reasons, another node), when only the counts differ (same cuts, another `ready`) and when
    only the reasons differ."""
    repo, root, store, launcher = _two_nodes(tmp_path)
    twc._already_started(root, repo, store, "n1")
    fd = twc._hold_process_lock(root, "n1")
    try:
        ProjectController(store, launcher, twc.Gates(), width=2).run()
    finally:
        twc._let_go(fd)
    assert (the_round(store).degradation, cuts_of(the_round(store))) == ((2, 0, ("IN_FLIGHT",)), [("n1", "IN_FLIGHT")])
    launcher.prepare = lambda node: "preflight says no"
    ProjectController(store, launcher, twc.Gates(), width=2).run()
    assert (the_round(store).degradation, cuts_of(the_round(store))) == ((2, 0, ("BLOCKED",)), [("n1", "BLOCKED")])

    (tmp_path / "unit").mkdir()
    directory = tmp_path / "unit" / OUT
    Cut = waverecovery.Cut

    def call(ready, cuts, extra=()):
        return waverecovery.record_round(directory, session=None, epoch=None, ready=ready,
                                         cuts=cuts, extra_reasons=extra)

    path = directory / f"wave-{ROUND_ID}.json"
    one = [Cut("n1", "CAPACITY", "wait")]
    assert call(3, one) is True and path.is_file()                  # absent: written
    fixed = snapshot(path)
    assert call(3, [Cut("n1", "CAPACITY", "another wording of the same wait")]) is False
    assert snapshot(path) == fixed                                   # same round: untouched
    assert call(3, [Cut("n2", "CAPACITY", "wait")]) is True          # only the cuts differ
    assert cuts_of(waverecovery.read_outcome(path)) == [("n2", "CAPACITY")]
    assert call(4, [Cut("n2", "CAPACITY", "wait")]) is True          # only the counts differ
    assert waverecovery.read_outcome(path).degradation == (4, 0, ("CAPACITY",))
    assert call(4, [Cut("n2", "CAPACITY", "wait")], extra=["WIDTH"]) is True     # only the reasons differ
    assert waverecovery.read_outcome(path).degradation == (4, 0, ("WIDTH", "CAPACITY"))


def test_the_reasons_of_a_round_are_in_the_vocabulary_order(tmp_path):
    """Catches reasons in any other order (alphabetical would be BUDGET before WIDTH): the
    outcome's vocabulary is the executor's reasons in the enum's order, then the controller's.
    (1) through the controller: three nodes at width 2 with the budget gone for n2 -> a wave
    outcome `(3, 1, ('WIDTH', 'BUDGET'))`; (2) `record_round` with an EXTERNAL, a BUDGET and a
    CAPACITY cut and a width reason -> `('EXTERNAL', 'WIDTH', 'BUDGET', 'CAPACITY')`."""
    repo, root, store = twc._world(tmp_path, names=("n1", "n2", "n3"))
    launcher = twc.FakeCliLauncher(root, repo, {}, dispatch_budget=3, dispatch_reservation=3)
    ProjectController(store, launcher, twc.Gates(), width=2).run()
    [first, *_] = wave_outcomes(store)
    assert first.degradation == (3, 1, ("WIDTH", "BUDGET")), first.degradation
    (tmp_path / "unit").mkdir()
    directory = tmp_path / "unit" / OUT
    waverecovery.record_round(
        directory, session=None, epoch=None, ready=4,
        cuts=[waverecovery.Cut("n1", "CAPACITY", "w"), waverecovery.Cut("n2", "BUDGET", "b"),
              waverecovery.Cut("n3", "EXTERNAL", "e")], extra_reasons=["WIDTH"])
    latest = waverecovery.read_outcome(directory / f"wave-{ROUND_ID}.json")
    assert latest.degradation == (4, 0, ("EXTERNAL", "WIDTH", "BUDGET", "CAPACITY")), latest.degradation
