"""P3-02 slice 2: the project loop launches the independent READY nodes of its
plan together, through the wave executor, on the real launcher path.

What is real: `ProjectController` (the loop, the planner, `_settle`, the merge
call), `ParallelWaveExecutor`, `HohRunLauncher` (`prepare`, `launch`, `_verdict`,
the hold of the shared budget), `DelegatedLease`, the budget ledger, the wave
record, the project store, a git repository with real linked worktrees. What is
replaced is the **process boundary**: `HohRunLauncher._run_cli` (the `hoh
worktree`, `hoh start` and `hoh run` sub-processes, which would start agents)
and `merge` (which would integrate in the integration checkout). No agent, no
Herdr, no network.

Every test names the failure it catches. A test builds its own repository under
`tmp_path`, never removes anything, and reads nothing outside `tmp_path` except
this directory: the production-path test imports its driver factory from here
by name, as `driver.json` registers it.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoh import cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import (
    BudgetExhausted, GateRunner, HaltClass, MergeResult, ProjectController, RunVerdict,
)
from hoh.project import ActionClass, GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.reservation import BudgetReservations, HoldStatus
from hoh.store import RunStore, StaleWrite
from hoh.succession import StaleMaster
from hoh.wave import (
    NodeState, ParallelismDegradation, ParallelismDegradationReason, ParallelWaveExecutor, WaveBudget,
    WaveError, read_record, settle_wave,
)
from hoh.wslease import WorkspaceHeld, WorkspaceLease, WorkspaceLeaseError, WorkspaceLeaseLost

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: the driver factory of the CLI test reads its world from here (the module is
#: imported by name through `driver.json`, so this is the same object)
CONFIG: dict = {}


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class FakeCliLauncher(HohRunLauncher):
    """The real launcher, with only the process boundary replaced.

    `script[run_id]` says what `hoh run` of that run does: "ok" (accepts a
    candidate), "reject" (ends where it started), "block" (parks on a provider
    limit), "boom" (the process raises), "overlap" (waits at the shared barrier
    for the other run, then accepts). `events` is the order of everything.
    """

    def __init__(self, root, repo, script, *, barrier=None, **kw):
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.script = dict(script)
        self.barrier = barrier
        self.events: list[tuple] = []
        self.calls: list[list[str]] = []
        self.merged: list[str] = []
        self.hooks: dict[str, object] = {}
        self._lock = threading.Lock()

    # -- the process boundary ---------------------------------------------- #

    def _run_cli(self, argv):
        with self._lock:
            self.calls.append(list(argv))
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                git(self.repo_path, "worktree", "add", "-q", "-b", branch, str(path))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        if "start" in argv:
            run_id = argv[argv.index("--run-id") + 1]
            wt = argv[argv.index("--repo") + 1]
            spec = argv[argv.index("--spec") + 1]
            state = RunState(run_id=run_id, repo_path=wt, project_name="p", spec_path=spec,
                             spec_digest="s" * 16, policy_digest="p" * 16,
                             profile_digest="f" * 16, stage=Stage.PLANNING,
                             condition=Condition.ACTIVE)
            RunStore(self.root, run_id).write_state(state)
            hook = self.hooks.get(run_id)
            if hook is not None:
                hook(wt)
            return Dispatch(0, "", "")
        if "run" in argv:
            run_id = argv[argv.index("run") + 1]
            return self._hoh_run(run_id)
        raise AssertionError(f"unexpected cli call {argv}")

    def _hoh_run(self, run_id):
        what = self.script.get(run_id, "ok")
        store = RunStore(self.root, run_id)
        state = store.read_state()
        began = time.monotonic()
        self.events.append(("start", run_id, began))
        if what == "boom":
            raise RuntimeError(f"the agent of {run_id} exploded")
        if what == "overlap":
            assert self.barrier is not None
            self.barrier.wait(timeout=30)     # BrokenBarrierError unless both runs are in `hoh run`
        if what == "block":
            state = state.model_copy(update={
                "condition": Condition.BLOCKED, "blocked_kind": "usage_quota",
                "blocked_reason": "429 from the provider"})
        elif what in ("ok", "overlap"):
            state = state.model_copy(update={
                "stage": Stage.CHECKPOINTED,
                "last_accepted_candidate": Candidate(
                    candidate_id=f"{run_id}-i1", repo_path=state.repo_path, commit="a" * 40,
                    tree_clean=True, tree_digest="d" * 16)})
        elif what == "reject":
            pass                              # iterations spent, no checkpoint: a rejection
        store.write_state(state)
        hook = self.hooks.get("run:" + run_id)
        if hook is not None:
            hook(state.repo_path)
        self.events.append(("end", run_id, time.monotonic()))
        return Dispatch(0, "", "")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40,
                           candidate=outcome.candidate or "")


def _world(tmp_path, names=("n1", "n2"), by_name=None, **node_kw):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    root = tmp_path / "root"
    nodes = []
    for i, name in enumerate(names):
        spec = tmp_path / f"{name}.md"
        spec.write_text(f"# {name}\n", encoding="utf-8")
        nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"],
                              **{**node_kw, **(by_name or {}).get(name, {})}))
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=nodes))
    return repo, root, store


def _nodes(store):
    return {n.id: n for n in store.read_state().nodes}


def _kinds(result):
    return [s.kind for s in result.steps]


def _already_started(root, repo, store, name):
    """Makes `name` a node whose run exists and holds nothing: what a node is
    after an earlier drive started it (a launcher without a budget takes no
    hold). The controller then resumes it instead of starting it."""
    node = store.read_state().node(name)
    assert FakeCliLauncher(root, repo, {}).prepare(node) is None
    assert RunStore(root, node.run_id or node.id).state_exists()
    assert BudgetReservations(root).held() == []


def _first_hold_of_each_run(root):
    """The first ledger row of every run (a run that exists takes a hold at each of its launches, so a run
    can have several rows; the tests below are about the hold of its first round)."""
    first = {}
    for row in BudgetReservations(root).read():
        first.setdefault(row.run_id, row)
    return first


# --------------------------------------------------------------------------- #
# the race, through the real launcher path
# --------------------------------------------------------------------------- #

def test_two_independent_nodes_run_at_once_through_the_wave_executor_and_both_merge(tmp_path):
    """Catches a loop that still launches one node per round: the barrier of two
    inside `hoh run` is only passed when both runs are executing together. Also
    the record: the wave names both nodes, both usable, no shared budget."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2))
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    nodes = _nodes(store)
    assert {k: v.lifecycle for k, v in nodes.items()} == {"n1": Lifecycle.MERGED,
                                                          "n2": Lifecycle.MERGED}
    assert sorted(launcher.merged) == ["n1", "n2"]
    starts = {e[1]: e[2] for e in launcher.events if e[0] == "start"}
    ends = {e[1]: e[2] for e in launcher.events if e[0] == "end"}
    assert max(starts.values()) < min(ends.values())           # each began before the other ended
    assert [k for k in _kinds(result) if k == "WAVE"] == ["WAVE"]
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    assert record.ceiling is None and record.width == 2
    assert [(n.node_id, n.state) for n in record.nodes] == [
        ("n1", NodeState.RETURNED), ("n2", NodeState.RETURNED)]
    assert all(n.usable and n.reservation_id is None for n in record.nodes)
    assert record.usable_ids == ("n1", "n2")


def test_a_failing_sibling_does_not_stop_the_other_member_of_the_wave(tmp_path):
    """Catches a wave that is abandoned when one member's dispatch raises: the
    other member's acceptance must still be merged, and the failing node must
    stay RUNNING (its activity open) for the next drive to evaluate."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "ok", "n2": "boom"})
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.AMBIGUOUS and "n2" in result.reason
    nodes = _nodes(store)
    assert nodes["n1"].lifecycle is Lifecycle.MERGED
    assert nodes["n2"].lifecycle is Lifecycle.RUNNING
    assert launcher.merged == ["n1"]
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    assert record.node("n1").state is NodeState.RETURNED
    assert record.node("n2").state is NodeState.ERRORED and "exploded" in record.node("n2").detail


def test_a_blocked_sibling_parks_alone_and_the_other_member_is_merged(tmp_path):
    """Catches a settle loop that stops at the first halting outcome: a provider
    wait of one node must not leave an accepted sibling unmerged."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "block", "n2": "ok"})
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.BLOCKED_PROVIDER and "n1" in result.reason
    nodes = _nodes(store)
    assert nodes["n1"].lifecycle is Lifecycle.BLOCKED
    assert nodes["n2"].lifecycle is Lifecycle.MERGED        # settled although n1 came first
    assert launcher.merged == ["n2"]


def test_a_rejected_sibling_is_retried_and_the_run_still_closes(tmp_path):
    """Catches a wave that makes a rejection final: the rejected node goes back to
    READY with one rejection, the loop then runs it alone, and the project closes."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "reject", "n2": "ok"})

    runs = {"n": 0}
    original = launcher._hoh_run

    def second_time_ok(run_id):
        if run_id == "n1":
            runs["n"] += 1
            if runs["n"] >= 2:
                launcher.script["n1"] = "ok"
        return original(run_id)

    launcher._hoh_run = second_time_ok
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    nodes = _nodes(store)
    assert nodes["n1"].rejections == 1
    assert all(n.lifecycle is Lifecycle.MERGED for n in nodes.values())
    assert _kinds(result).count("WAVE") == 1                  # the retry had no sibling left: one node


# --------------------------------------------------------------------------- #
# the width: LINEAR unless somebody chose otherwise
# --------------------------------------------------------------------------- #

def test_a_wave_is_at_most_width_wide_and_the_node_past_it_is_not_even_prepared(tmp_path):
    """Catches a width that is only enforced by the executor's refusal: the
    third node would be prepared (a run, a worktree, a hold) for a wave that
    cannot take it. Three independent nodes at width 2: the first wave names
    two; the third is prepared only in the round that runs it."""
    repo, root, store = _world(tmp_path, names=("n1", "n2", "n3"))
    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2), dispatch_budget=9,
                               dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    [path] = sorted((store.dir / "waves").iterdir())            # the third ran alone: no second wave
    assert [n.node_id for n in read_record(path).nodes] == ["n1", "n2"]
    first_starts = [i for i, c in enumerate(launcher.calls) if "start" in c]
    runs = [i for i, c in enumerate(launcher.calls) if "run" in c]
    third_start = [i for i in first_starts if "n3" in launcher.calls[i]][0]
    assert third_start > max(i for i in runs if launcher.calls[i][launcher.calls[i].index("run") + 1] in ("n1", "n2"))
    assert sorted(launcher.merged) == ["n1", "n2", "n3"]


def test_the_default_is_linear_one_node_per_round_and_no_wave_is_written(tmp_path):
    """Catches a loop that widens by itself: with no width asked for, the nodes
    run one after the other, no wave record exists, and the steps carry no WAVE."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {})
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert "WAVE" not in _kinds(result) and not (store.dir / "waves").exists()
    starts = [e for e in launcher.events if e[0] == "start"]
    ends = [e for e in launcher.events if e[0] == "end"]
    assert starts[1][2] >= ends[0][2]                          # the second began after the first ended


def test_the_width_is_the_argument_then_the_environment_then_the_launcher(tmp_path, monkeypatch):
    """Catches a width resolved in another order, a silent fallback to 1 for a
    value that is not a width, and a bool or a float taken for a number."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {}, wave_width=3)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    assert ProjectController(store, launcher, Gates()).width == 3          # the launcher's
    monkeypatch.setenv("HOH_WAVE_WIDTH", "4")
    assert ProjectController(store, launcher, Gates()).width == 4          # the environment over it
    assert ProjectController(store, launcher, Gates(), width=2).width == 2  # the argument over both
    monkeypatch.delenv("HOH_WAVE_WIDTH")
    assert ProjectController(store, FakeCliLauncher(root, repo, {}), Gates()).width == 1
    for bad in (0, -1, 17, True, 2.0, "2"):
        with pytest.raises(ValueError):
            ProjectController(store, launcher, Gates(), width=bad)
    for bad in ("0", "17", "two", "2.0", " 2", "-1"):
        monkeypatch.setenv("HOH_WAVE_WIDTH", bad)
        with pytest.raises(ValueError):
            ProjectController(store, launcher, Gates())
    monkeypatch.setenv("HOH_WAVE_WIDTH", "")
    assert ProjectController(store, launcher, Gates()).width == 3          # empty is not set
    for bad in (0, True, 17, 1.5):
        with pytest.raises(ValueError):
            FakeCliLauncher(root, repo, {}, wave_width=bad)


def test_a_launcher_that_cannot_take_part_runs_one_node_per_round_and_says_so_once(tmp_path):
    """Catches a width that is honoured silently by a launcher that has no
    observed lease to offer, and a missing sign that it was not honoured."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {})
    launcher.lease_for = None                     # what a launcher without the wave API looks like
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert _kinds(result).count("WAVE_UNAVAILABLE") == 1
    assert "WAVE" not in _kinds(result) and not (store.dir / "waves").exists()
    assert sorted(launcher.merged) == ["n1", "n2"]



# --------------------------------------------------------------------------- #
# the hold: one per run, the launcher's, and the record says which
# --------------------------------------------------------------------------- #

def test_the_hold_is_the_launchers_and_the_wave_records_it_without_a_second_one(tmp_path):
    """Catches an executor that reserves again for a run the launcher prepared
    (the old hold superseded, two rows, the amount a node is told not the one
    it was started with) and one that ends a hold the launcher keeps."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2), dispatch_budget=6,
                               dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    starts = [c for c in launcher.calls if "start" in c]
    assert [c[c.index("--max-dispatches") + 1] for c in starts] == ["3", "3"]
    rows = BudgetReservations(root).read()
    assert sorted((r.run_id, r.amount, r.status) for r in rows) == [
        ("n1", 3, HoldStatus.SETTLED), ("n2", 3, HoldStatus.SETTLED)]    # one row each, none superseded
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    assert record.ceiling == 6
    by_id = {r.run_id: r for r in rows}
    for n in record.nodes:
        assert n.reservation_id == by_id[n.node_id].reservation_id and n.amount == 3
        assert n.holds_ended is None                         # not the executor's to end


def test_a_node_the_wave_refuses_gives_back_the_hold_its_prepare_took(tmp_path):
    """Catches a refused node that keeps a share of the budget for a dispatch
    that did not happen (and, in the other direction, one that ends a hold it
    did not take): n2's worktree is held by a live writer, so the wave refuses
    it after its `prepare`."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "ok", "n2": "ok"}, dispatch_budget=6,
                               dispatch_reservation=3)
    keep = []

    def occupy(worktree):
        handle = WorkspaceLease(worktree, "someone-else")
        handle.acquire()
        keep.append(handle)

    launcher.hooks["n2"] = occupy
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    first = read_record(sorted((store.dir / "waves").iterdir())[0])
    assert first.node("n1").state is NodeState.RETURNED
    refused = first.node("n2")
    assert refused.state is NodeState.REFUSED and refused.refusal == "WORKSPACE"
    assert "held" in refused.detail
    rows = _first_hold_of_each_run(root)
    assert set(rows) == {"n1", "n2"}                                   # one hold each in the wave, never a second
    assert rows["n2"].status is HoldStatus.SETTLED
    assert "the wave refused the node" in rows["n2"].reason            # ended by the refusal, not by a launch
    assert launcher.merged == ["n1", "n2"]                             # n2 ran alone in the next round
    assert keep and keep[0].check().holder == "someone-else"           # the foreign lease was not touched


def test_a_resumed_run_in_a_budgeted_wave_takes_a_hold_like_a_new_one(tmp_path):
    """Catches a wave that admits a run that already existed with no share of the
    budget held for it (n concurrent resumes could then overspend what nothing
    reserved): n1 exists and holds nothing, n2 is new; the budget is 6, a hold is
    3. Both are admitted, both rows carry a hold of 3 that the launch settled,
    and only n2 was started."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n1")
    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2), dispatch_budget=6,
                               dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    assert [(n.node_id, n.reservation_id is not None, n.amount) for n in record.nodes] == [
        ("n1", True, 3), ("n2", True, 3)]
    rows = BudgetReservations(root).read()
    assert sorted((r.run_id, r.amount, r.status) for r in rows) == [
        ("n1", 3, HoldStatus.SETTLED), ("n2", 3, HoldStatus.SETTLED)]
    assert [c[c.index("--run-id") + 1] for c in launcher.calls if "start" in c] == ["n2"]
    assert sorted(launcher.merged) == ["n1", "n2"]


def test_a_resumed_run_the_budget_cannot_hold_for_is_left_out_of_the_wave_not_run_unheld(tmp_path):
    """Catches a resumed sibling that runs in the wave with no hold when the
    other member took the whole budget: n1 is new and takes 3 of 3, n2 exists.
    n2 is left out of the wave (a WAVE_SKIPPED step, no hold row, no wave node),
    n1 runs, and n2 runs alone in the next round."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n2")
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=3, dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    skipped = [s for s in result.steps if s.kind == "WAVE_SKIPPED"]
    assert [s.node for s in skipped] == ["n2"] and "no budget to hold" in skipped[0].detail
    [path] = sorted((store.dir / "waves").iterdir())
    assert [n.node_id for n in read_record(path).nodes] == ["n1"]
    # no hold of n2 in the wave; its own launch in the next round took one (P3-INV1: a run that exists
    # holds what the others leave when it is launched), after n1's was over
    rows = BudgetReservations(root).read()
    assert [(r.run_id, r.status) for r in rows] == [("n1", HoldStatus.SETTLED), ("n2", HoldStatus.SETTLED)]
    assert launcher.merged == ["n1", "n2"]


@pytest.mark.parametrize("raised", [StaleMaster("not the master"), StaleWrite("advanced"),
                                    KeyboardInterrupt()],
                         ids=["stale_master", "stale_write", "interrupt"])
def test_every_exception_that_leaves_the_wave_before_its_dispatch_ends_the_holds(
        tmp_path, monkeypatch, raised):
    """Catches a controller that gives the prepared holds back only for the
    errors the executor wraps (WaveError, OSError): the authorisation of the
    wave's dispatch raises a stale master, a stale write or an interrupt after
    RUNNING was persisted. Nothing was launched, and the holds `prepare` took
    for both nodes are ended, not left HELD for a dispatch that did not happen."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    real = ProjectController._authorise

    def authorise(self, act):
        if act.startswith("dispatch of wave"):
            raise raised
        return real(self, act)

    monkeypatch.setattr(ProjectController, "_authorise", authorise)
    controller = ProjectController(store, launcher, Gates(), width=2)
    if isinstance(raised, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            controller.run()
    else:
        result = controller.run()
        expected = HaltClass.SUPERSEDED if isinstance(raised, StaleMaster) else HaltClass.AMBIGUOUS
        assert result.halt is expected, result.reason
    assert not [c for c in launcher.calls if "run" in c]            # nothing was dispatched
    rows = BudgetReservations(root).read()
    assert sorted((r.run_id, r.status) for r in rows) == [
        ("n1", HoldStatus.SETTLED), ("n2", HoldStatus.SETTLED)]
    assert BudgetReservations(root).held() == []


def test_a_launch_that_raises_under_a_budget_settles_its_hold_and_the_sibling_proceeds(tmp_path):
    """Catches a failing member whose hold stays open (the budget stays smaller
    for a dispatch that is over) or whose failure takes its sibling's merge with
    it: n2's process raises, with a shared budget of 6 and holds of 3."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "ok", "n2": "boom"}, dispatch_budget=6,
                               dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.AMBIGUOUS and "n2" in result.reason
    rows = {r.run_id: r for r in BudgetReservations(root).read()}
    assert {k: v.status for k, v in rows.items()} == {"n1": HoldStatus.SETTLED,
                                                      "n2": HoldStatus.SETTLED}
    assert rows["n2"].reason == "the dispatch returned"         # ended by the launch, which raised
    assert _nodes(store)["n1"].lifecycle is Lifecycle.MERGED and launcher.merged == ["n1"]


def test_the_record_of_a_controller_wave_reads_through_the_settlement_and_the_degradation(tmp_path):
    """Catches the two wave.py changes of this slice and the settlement/degradation
    of P3-22 disagreeing about one record: a controller wave that adopted its
    holds and refused one node (WORKSPACE) is read by `settle_wave` -- the adopted
    hold is ALREADY_ENDED (the launch settled it), the refused node has none --
    and says why the wave ran narrower."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "ok", "n2": "ok"}, dispatch_budget=6,
                               dispatch_reservation=3)
    keep = []

    def occupy(worktree):
        handle = WorkspaceLease(worktree, "someone-else")
        handle.acquire()
        keep.append(handle)

    launcher.hooks["n2"] = occupy
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    first = sorted((store.dir / "waves").iterdir())[0]
    record = read_record(first)
    assert record.degradation == ParallelismDegradation(
        ready=2, admitted=1, reasons=(ParallelismDegradationReason.WORKSPACE,))
    before = BudgetReservations(root).read()
    settlement = settle_wave(first, BudgetReservations(root), launcher.spent_by_run)
    assert [(n.node_id, n.state) for n in settlement.nodes] == [("n1", "ALREADY_ENDED")]
    assert settlement.degradation == record.degradation
    assert BudgetReservations(root).read() == before                 # nothing was ended again


def test_an_executor_that_adopts_holds_refuses_a_qa_reserve_it_could_not_keep(tmp_path):
    """Catches a fail-open combination of the two budget features: the QA reserve
    is kept by whoever reserves (`keep=`), and an adopting executor reserves
    nothing, so a reserve configured on it would be silently unkept."""
    reservations = BudgetReservations(tmp_path / "root")
    common = dict(width=2, lease_for=lambda n: None, depends_on=lambda a, b: None,
                  action_class=lambda n: None)
    with pytest.raises(WaveError, match="QA reserve cannot be kept on an adopted hold"):
        ParallelWaveExecutor(tmp_path / "w", budget=WaveBudget(
            reservations, 6, lambda: {}, 1, qa_reserve=2, adopt=True), **common)
    with pytest.raises(WaveError, match="adopt is a bool"):
        ParallelWaveExecutor(tmp_path / "w", budget=WaveBudget(
            reservations, 6, lambda: {}, 1, adopt="yes"), **common)
    # the same reserve on an executor that reserves itself is the P3-22 feature, and stays valid
    ParallelWaveExecutor(tmp_path / "w", budget=WaveBudget(reservations, 6, lambda: {}, 1,
                                                           qa_reserve=2), **common)
    ParallelWaveExecutor(tmp_path / "w", budget=None, **common)     # no shared budget at all


def test_a_wave_directory_that_cannot_be_one_halts_before_anything_runs_and_gives_the_holds_back(
        tmp_path):
    """Catches a wave that launches without its write-ahead record, or a halt
    that leaves the holds its `prepare` took open: `waves` is a regular file."""
    repo, root, store = _world(tmp_path)
    (store.dir / "waves").write_text("not a directory", encoding="utf-8")
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.BLOCKED_DEPENDENCY and "wave" in result.reason
    assert not [c for c in launcher.calls if "run" in c]            # nothing was dispatched
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.READY}
    assert [r.status for r in BudgetReservations(root).read()] == [HoldStatus.SETTLED] * 2


def test_a_node_id_the_record_cannot_hold_keeps_the_linear_loop(tmp_path):
    """Catches a wave refused whole, and a project halted, because one node id is
    outside the record's grammar: such a round runs one node, as before."""
    long_a, long_b = "a" * 130, "b" * 130           # run ids the store takes; 128 is the record's bound
    repo, root, store = _world(tmp_path, names=(long_a, long_b))
    launcher = FakeCliLauncher(root, repo, {})
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert "WAVE" not in _kinds(result) and not (store.dir / "waves").exists()
    assert sorted(launcher.merged) == [long_a, long_b]


def test_the_storage_boot_is_claimed_by_one_launch_at_a_time(tmp_path):
    """Catches a launcher whose boot claim runs from two wave threads at once
    (the claim is one act per boot)."""
    repo, root, store = _world(tmp_path)
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "OWNER.json").write_text("{}", encoding="utf-8")
    inside, seen = [0], []
    lock = threading.Lock()

    class Admission:
        def claim_boot(self):
            with lock:
                inside[0] += 1
                seen.append(inside[0])
            time.sleep(0.05)
            with lock:
                inside[0] -= 1

    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2), storage_root=state_root,
                               storage_admission_factory=lambda r: Admission())
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert len(seen) == 2 and max(seen) == 1


# --------------------------------------------------------------------------- #
# the lease: observed at admission and at the end, never taken here
# --------------------------------------------------------------------------- #

def test_an_accepted_node_whose_worktree_another_holder_wrote_is_not_merged(tmp_path):
    """Catches a wave that merges on a verdict alone: another holder's lease
    stands in the worktree when the run ends, so the node is usable by no
    reading and is blocked for a person, while its sibling is merged."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "ok", "n2": "ok"})
    intruders = []

    def intrude(worktree):
        handle = WorkspaceLease(worktree, "intruder")
        handle.acquire()
        intruders.append(handle)

    launcher.hooks["run:n1"] = intrude
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.AMBIGUOUS and "n1" in result.reason
    nodes = _nodes(store)
    assert nodes["n1"].lifecycle is Lifecycle.BLOCKED and "not merged" in nodes["n1"].note
    assert nodes["n2"].lifecycle is Lifecycle.MERGED
    assert launcher.merged == ["n2"]
    [path] = sorted((store.dir / "waves").iterdir())
    n1 = read_record(path).node("n1")
    assert n1.state is NodeState.RETURNED and n1.lease_held_at_end is False and not n1.usable


def test_the_run_takes_its_own_worktree_lease_while_the_wave_runs_and_never_waits_on_the_executor(
        tmp_path):
    """Catches a wave whose executor holds the node's workspace lease across the
    launch: the run's own controller (P3-06 slice 2: it leases `state.repo_path`
    at every iteration, under `holder_name(run id)`) would then be refused
    `WorkspaceHeld` and wait on its own executor. The hook plays that
    controller inside `hoh run`: it takes the lease, finds it its own, and gives
    it back -- for both members of the wave, while both are running."""
    from hoh.wslease import holder_name

    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                               barrier=threading.Barrier(2))
    taken = {}

    def as_the_controller(run_id):
        def hook(worktree):
            handle = WorkspaceLease(worktree, holder_name(run_id))
            lease = handle.acquire()                    # WorkspaceHeld if the executor held it
            taken[run_id] = (lease.holder, lease.generation)
            assert handle.check().holder == holder_name(run_id)
            handle.release(reason="the iteration is over")
        return hook

    for run_id in ("n1", "n2"):
        launcher.hooks[f"run:{run_id}"] = as_the_controller(run_id)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert set(taken) == {"n1", "n2"} and all(g == 1 for _, g in taken.values())
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    # the run's lease, taken and released inside the dispatch, is the run's own record at the end
    assert [(n.state, n.lease_held_at_end, n.lease_released) for n in record.nodes] == [
        (NodeState.RETURNED, True, True)] * 2


def test_the_delegated_lease_observes_and_writes_nothing(tmp_path):
    """Catches an observed lease that takes the worktree (the run's own
    controller would then wait on it), writes a record or a log line, reads an
    unreadable record as free, or reports a worktree held that someone else wrote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "a").write_text("1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "w", str(wt))
    from hoh.launcher import DelegatedLease      # inside the test: the module is new in this slice
    from hoh.workspace import lease_directory

    obs = DelegatedLease(wt, "run-a")
    got = obs.acquire()
    assert got.generation == 1 and obs.token is None
    assert not os.path.lexists(lease_directory(wt))            # nothing written, no directory made
    assert obs.check().holder == "run-a"
    assert obs.release().generation == 1

    # a live holder is refused, a record that is not one is never "free"
    other = WorkspaceLease(wt, "someone-else")
    other.acquire()
    with pytest.raises(WorkspaceHeld):
        DelegatedLease(wt, "run-a").acquire()
    other.release(reason="done")
    bytes_before = (lease_directory(wt) / "lease.json").read_bytes()
    seen = DelegatedLease(wt, "run-a")
    assert seen.acquire().generation == 2                       # the generation the run will take
    assert (lease_directory(wt) / "lease.json").read_bytes() == bytes_before
    # someone writes after admission: the end reads it as lost, and a live lease left behind is not released
    thief = WorkspaceLease(wt, "thief")
    thief.acquire()
    with pytest.raises(WorkspaceLeaseLost):
        seen.check()
    with pytest.raises(WorkspaceLeaseLost):
        seen.release()
    (lease_directory(wt) / "lease.json").write_text("{not a lease", encoding="utf-8")
    with pytest.raises(WorkspaceLeaseError):
        DelegatedLease(wt, "run-a").acquire()


def test_release_prepared_hold_ends_the_hold_this_launcher_took_and_no_other(tmp_path):
    """Catches a launcher that settles by run id whatever holds it: a hold of the
    same run that another process took is left alone."""
    repo, root, store = _world(tmp_path, names=("n1",))
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=9)
    node = store.read_state().node("n1")
    assert launcher.prepare(node) is None
    [mine] = BudgetReservations(root).held()
    assert launcher.release_prepared_hold(node, "test") is True
    assert BudgetReservations(root).read()[0].status is HoldStatus.SETTLED
    assert launcher.release_prepared_hold(node, "test") is False        # once
    # another process takes a hold of the same run (superseding the one prepared) before the release:
    # it is not the hold this launcher took, and it is left alone
    assert launcher.prepare(store.read_state().node("n1")) is None      # a run that exists takes none ...
    assert BudgetReservations(root).held() == []
    node2 = TaskNode(id="n2", spec_path=node.spec_path, writes=["n2.txt"])
    assert launcher.prepare(node2) is None
    [prepared] = [r for r in BudgetReservations(root).held() if r.run_id == "n2"]
    foreign = BudgetReservations(root).reserve("n2", ceiling=9, spent=lambda: {}, want=2)
    assert launcher.release_prepared_hold(node2, "test") is False
    assert [r.reservation_id for r in BudgetReservations(root).held()] == [foreign.reservation_id]
    assert prepared.reservation_id != foreign.reservation_id and mine.reservation_id != foreign.reservation_id


def test_the_record_of_a_wave_without_a_budget_or_a_hold_is_still_strict():
    """Catches a record that reads a half hold, or a hold under no ceiling, as
    fine: the two relaxations of this slice (no shared budget; a run with no open
    hold) are exactly the pair `reservation_id` / `amount` and `ceiling: null`
    with no hold at all."""
    from hoh import wave

    def node(**kw):
        base = dict(node_id="n1", run_id="n1", state=wave.NodeState.RETURNED, reservation_id=None,
                    amount=None, worktree="/w/n1", generation=1, verdict="ACCEPTED",
                    lease_held_at_end=True, lease_released=True, holds_ended=None)
        base.update(kw)
        return wave.WaveNode(**base)

    def rec(n, ceiling):
        return wave.WaveRecord("w1", wave.Phase.SETTLED, "2026-10-13T10:00:00Z",
                               "2026-10-13T10:00:01Z", 2, ceiling, (n,))

    ok = rec(node(), None)
    assert wave.parse(ok.to_bytes()) == ok                              # no budget, no hold
    held = rec(node(reservation_id="ab-n1", amount=2), 6)
    assert wave.parse(held.to_bytes()) == held
    adopted_none = rec(node(), 6)                                       # a budget, a run with no hold
    assert wave.parse(adopted_none.to_bytes()) == adopted_none
    for bad in (rec(node(reservation_id="ab-n1"), 6),                   # a half hold
                rec(node(amount=2), 6),
                rec(node(reservation_id="ab-n1", amount=2), None),      # a hold under no ceiling
                rec(node(worktree=None), 6), rec(node(generation=None), 6)):
        with pytest.raises(wave.WaveRecordError):
            wave.parse(bad.to_bytes())


# --------------------------------------------------------------------------- #
# the production path: `hoh project drive`
# --------------------------------------------------------------------------- #

def build_driver(store):
    """The `driver.json` factory of the CLI test: the real launcher, a fake
    process boundary."""
    cfg = CONFIG
    launcher = FakeCliLauncher(cfg["root"], cfg["repo"], cfg["script"], barrier=cfg["barrier"],
                               wave_width=cfg.get("wave_width", 1))
    cfg["launcher"] = launcher
    return launcher, Gates()


def test_hoh_project_drive_launches_two_independent_nodes_at_once_through_the_wave_executor(
        tmp_path, monkeypatch, capsys):
    """The production path, end to end: `hoh project drive` (cli.main) loads the
    driver the project registers, drives the governed project, and the loop
    launches the READY frontier together. Catches a wave path that only the
    controller's own tests reach: cli.py -> resumeloop.drive_governed ->
    ProjectController.run -> the wave executor -> HohRunLauncher.launch. The
    width comes from the environment (`HOH_WAVE_WIDTH=2`), no code of the
    driver or of cli.py names it."""
    repo, root, store = _world(tmp_path)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, script={"n1": "overlap", "n2": "overlap"},
                  barrier=threading.Barrier(2))
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_controller:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("WAVE:") for s in out["steps"])
    launcher = CONFIG["launcher"]
    starts = {e[1]: e[2] for e in launcher.events if e[0] == "start"}
    ends = {e[1]: e[2] for e in launcher.events if e[0] == "end"}
    assert set(starts) == {"n1", "n2"} and max(starts.values()) < min(ends.values())
    assert sorted(launcher.merged) == ["n1", "n2"]
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.MERGED}


def test_hoh_project_drive_keeps_a_failing_member_from_stopping_an_independent_one(
        tmp_path, monkeypatch, capsys):
    """The failure-isolation invariant on its own node, through the same
    production path as the concurrency test: one wave of two independent nodes,
    n1's process raises, n2 is independent and must complete and be accepted and
    merged. Catches a loop whose failed member ends the round before its sibling
    is settled -- which only the controller's own tests would have caught."""
    repo, root, store = _world(tmp_path)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, script={"n1": "boom", "n2": "ok"}, barrier=None)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_controller:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    out = json.loads(capsys.readouterr().out)
    assert out["halt"] == "AMBIGUOUS", out                  # n1's dispatch raised
    assert any(s.startswith("WAVE:") for s in out["steps"])
    nodes = {n.id: n for n in store.read_state().nodes}
    assert nodes["n2"].lifecycle is Lifecycle.MERGED         # accepted although n1 failed first
    assert nodes["n1"].lifecycle is Lifecycle.RUNNING        # open, for the next drive to evaluate
    assert CONFIG["launcher"].merged == ["n2"]
    [path] = sorted((store.dir / "waves").iterdir())
    record = read_record(path)
    assert record.node("n2").state is NodeState.RETURNED and record.node("n2").verdict == "ACCEPTED"
    assert record.node("n1").state is NodeState.ERRORED


# --------------------------------------------------------------------------- #
# the repair slice (P3-02s2rf): holds the round took before the executor, and
# the rules that had no criterion of their own
# --------------------------------------------------------------------------- #

def _held_ids(root):
    return sorted(r.run_id for r in BudgetReservations(root).held())


def _hold_process_lock(root, run_id):
    """Plays the process that executes `run_id`: takes the run's process lock
    exclusively (what `hoh run` does for its whole lifetime). Returns the fd."""
    path = RunStore(root, run_id).process_lock_path
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _let_go(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@pytest.mark.parametrize("how", ["prepare_raises", "block_raises_stale_master", "interrupt"])
def test_a_wave_that_never_reaches_its_executor_gives_every_hold_it_took_back(tmp_path, how):
    """Catches the wedge of the post-tag review (F1): the candidate loop took n1's
    hold and then a later step raised -- a sibling's `prepare` (an OSError), the
    persist of a sibling's block (a stale master), an interrupt in `evaluate` --
    and nothing gave it back, so `take_hold` refused the run in every later drive
    at width 2. Nothing is HELD afterwards and a second drive closes."""
    repo, root, store = _world(tmp_path)
    first = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    controller = ProjectController(store, first, Gates(), width=2)
    real_prepare, real_evaluate = first.prepare, first.evaluate
    if how == "prepare_raises":
        def prepare(node):
            if node.id == "n2":
                raise OSError(24, "Too many open files")
            return real_prepare(node)
        first.prepare = prepare
    elif how == "block_raises_stale_master":
        first.prepare = lambda node: "preflight says no" if node.id == "n2" else real_prepare(node)

        def persist(state):
            raise StaleMaster("a successor was accepted")
        controller._persist = persist
    else:
        def evaluate(node):
            if node.id == "n2":
                raise KeyboardInterrupt()
            return real_evaluate(node)
        first.evaluate = evaluate
    if how == "block_raises_stale_master":
        assert controller.run().halt is HaltClass.SUPERSEDED
    elif how == "prepare_raises":
        with pytest.raises(OSError):
            controller.run()
    else:
        with pytest.raises(KeyboardInterrupt):
            controller.run()
    assert _held_ids(root) == []                                   # nothing left for a dispatch that did not happen
    assert not [c for c in first.calls if "run" in c]              # and nothing ran
    second = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    result = ProjectController(store, second, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert sorted(second.merged) == ["n1", "n2"]


def test_a_hold_a_dead_controller_left_is_recovered_by_the_next_drive_and_width_2_closes(tmp_path):
    """The crash case of F1 (b), which P3-02s2rf stated as a limit with a remedy (a width-1
    drive) and P3-22 slice 3 lifts. A controller that dies between the wave's record and
    RUNNING leaves HELD rows that nobody ends. The next width-2 drive no longer halts
    BUDGET_EXHAUSTED: it reads the outcome the dead controller wrote before its executor
    started, claims the persisted state, finds both nodes READY and both runs idle, and
    ends exactly the two named holds by id (`WAVE_RECOVERED`); a hold this launcher did
    not take is still never adopted or superseded by a *wave* (W13, `take_hold`): the
    recovery is the one act that ends it, and it is bounded and pinned in
    `tests/test_wave_recovery.py`."""
    repo, root, store = _world(tmp_path)
    dying = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    dying.release_prepared_hold = lambda node, reason: False        # the process is gone before it can
    died = []

    def baseline(node):
        died.append(node.id)
        raise KeyboardInterrupt()                                    # inside `on_admitted`, before RUNNING

    dying.accepted_baseline = baseline
    with pytest.raises(KeyboardInterrupt):
        ProjectController(store, dying, Gates(), width=2).run()
    assert died and _held_ids(root) == ["n1", "n2"]                   # the crash state: both HELD
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.READY}
    recovered = ProjectController(
        store, FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3),
        Gates(), width=2).run()
    assert recovered.halt is HaltClass.CLOSED, recovered.reason
    assert [s.kind for s in recovered.steps if s.kind == "WAVE_RECOVERED"] == ["WAVE_RECOVERED"]
    assert _held_ids(root) == []
    assert {r.status for r in BudgetReservations(root).read()} == {HoldStatus.SETTLED}


@pytest.mark.parametrize("how", ["idle", "process_lock", "running_node"])
def test_take_hold_never_adopts_or_supersedes_a_hold_it_did_not_take(tmp_path, how):
    """Catches `take_hold` keeping (adopting) or replacing (superseding) a hold
    this launcher did not take, in any state of the run: idle, executed by a
    process (its process lock is held), or RUNNING. It refuses, the foreign hold
    is untouched, and `_prepared_holds` does not learn it. No state of the run
    is read to decide: nothing here depends on a proof of idleness."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n1")
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    foreign = BudgetReservations(root).reserve(
        "n1", ceiling=6, spent=launcher.spent_by_run, want=3)
    node = store.read_state().node("n1")
    if how == "running_node":
        node = node.model_copy(update={"lifecycle": Lifecycle.RUNNING})
    fd = _hold_process_lock(root, "n1") if how == "process_lock" else None
    try:
        with pytest.raises(BudgetExhausted, match="did not take"):
            launcher.take_hold(node)
    finally:
        if fd is not None:
            _let_go(fd)
    assert [r.reservation_id for r in BudgetReservations(root).read()] == [foreign.reservation_id]
    assert [r.reservation_id for r in BudgetReservations(root).held()] == [foreign.reservation_id]
    assert "n1" not in launcher._prepared_holds


def test_a_corrupt_ledger_refuses_take_hold_and_the_ledger_is_not_touched(tmp_path):
    """Fail closed on the one read that `take_hold` still makes: a ledger that is
    not what the module wrote refuses (BudgetExhausted, "cannot be read") and is
    not rewritten."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n1")
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    BudgetReservations(root).reserve("n1", ceiling=6, spent=launcher.spent_by_run, want=3)
    ledger = BudgetReservations(root).path
    ledger.write_bytes(b"{ not json")
    before = ledger.read_bytes()
    with pytest.raises(BudgetExhausted, match="cannot be read"):
        launcher.take_hold(store.read_state().node("n1"))
    assert ledger.read_bytes() == before


def test_a_hold_kept_for_a_run_another_process_executes_is_not_released_by_a_later_halt(tmp_path):
    """Catches the exit handler ending the hold of a run that is IN_FLIGHT: n1's
    earlier launch (by this launcher object) came back IN_FLIGHT, so `launch`
    kept the hold and `_prepared_holds` kept its id; a later drive of the same
    object halts RUN_IN_FLIGHT on n1 and must leave that hold open -- the other
    process is still executing the run."""
    from hoh.launcher import RunOutcome
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    n1 = store.read_state().node("n1")
    assert launcher.prepare(n1) is None                              # a new run and its hold
    kept = _held_ids(root)
    real = launcher._launch
    launcher._launch = lambda node: RunOutcome(RunVerdict.IN_FLIGHT, "another process executes it")
    launcher.launch(n1)                                              # IN_FLIGHT: the hold stays by design
    launcher._launch = real
    assert kept == ["n1"] and _held_ids(root) == ["n1"]
    fd = _hold_process_lock(root, "n1")                              # the other process, executing n1
    try:
        result = ProjectController(store, launcher, Gates(), width=2).run()
    finally:
        _let_go(fd)
    assert result.halt is HaltClass.RUN_IN_FLIGHT, result.reason
    assert _held_ids(root) == ["n1"]                                 # untouched
    assert [r.status for r in BudgetReservations(root).read()] == [HoldStatus.HELD]


def test_a_sibling_whose_start_fails_holds_nothing_and_the_round_goes_on(tmp_path):
    """Pins why a node left out of the wave after `prepare` has no hold to leak:
    `prepare` ends its own hold when `hoh start` fails. n2's start fails; n2 is
    BLOCKED alone, its hold is SETTLED by `prepare` ("could not be started"), n1
    runs and merges, and nothing is HELD."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    real = launcher._run_cli

    def run_cli(argv):
        if "start" in argv and argv[argv.index("--run-id") + 1] == "n2":
            return Dispatch(1, "", "start refused")
        return real(argv)

    launcher._run_cli = run_cli
    ProjectController(store, launcher, Gates(), width=2).run()
    nodes = _nodes(store)
    assert nodes["n2"].lifecycle is Lifecycle.BLOCKED and "could not start run n2" in nodes["n2"].note
    assert nodes["n1"].lifecycle is Lifecycle.MERGED and launcher.merged == ["n1"]
    rows = {r.run_id: r for r in BudgetReservations(root).read()}
    assert rows["n2"].status is HoldStatus.SETTLED and "could not be started" in rows["n2"].reason
    assert _held_ids(root) == []


def test_a_siblings_obstacle_blocks_it_alone_and_the_head_still_runs(tmp_path):
    """Catches a round that halts because a *sibling* cannot be made runnable
    (W3): n2's `prepare` reports an obstacle; n2 is BLOCKED alone and persisted,
    n1 -- the head -- is launched and merged."""
    repo, root, store = _world(tmp_path)
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    real = launcher.prepare
    launcher.prepare = lambda node: "preflight says no" if node.id == "n2" else real(node)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    nodes = _nodes(store)
    assert nodes["n2"].lifecycle is Lifecycle.BLOCKED and "preflight says no" in nodes["n2"].note
    assert nodes["n1"].lifecycle is Lifecycle.MERGED and launcher.merged == ["n1"], result.reason
    assert _held_ids(root) == []


def test_a_reserving_executor_gives_back_its_own_holds_and_leases_when_on_admitted_raises(tmp_path):
    """Catches `run_wave` not giving back what the *executor* took when the hook
    that persists RUNNING raises: the controller's wave adopts (and the hook's
    raise is then only about leases), but an executor that reserves itself --
    the slice-1 mode, every `lease_for` caller outside the loop -- holds one
    share per node, and a missing give-back leaves them HELD."""
    from test_wave import Rig, ok
    rig = Rig(tmp_path, ["n1", "n2"], ceiling=20, want=2)
    launched = []

    def hook(nodes):
        raise RuntimeError("could not persist RUNNING")

    with pytest.raises(RuntimeError, match="could not persist"):
        rig.executor().run_wave("w1", rig.plan("n1", "n2"),
                                lambda n, a: launched.append(n.id) or ok(), on_admitted=hook)
    assert launched == []
    assert sorted((r.run_id, r.status) for r in rig.ledger()) == [
        ("n1", HoldStatus.SETTLED), ("n2", HoldStatus.SETTLED)]
    rig.assert_everything_given_back()


def test_a_non_head_external_node_is_left_to_a_later_round_not_prepared_in_the_wave(tmp_path):
    """Catches an irreversible external action that is prepared (and launched)
    as a wave sibling: n2 is EXTERNAL and not the head, so the wave skips it
    (it is the head of a later round, where the loop halts for a person)."""
    repo, root, store = _world(tmp_path, by_name={"n2": {"action_class": ActionClass.EXTERNAL}})
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.BLOCKED_EXTERNAL, result.reason
    assert _nodes(store)["n1"].lifecycle is Lifecycle.MERGED
    assert _nodes(store)["n2"].lifecycle is Lifecycle.READY
    assert not [c for c in launcher.calls if "n2" in c]               # never prepared, never started
    assert launcher.merged == ["n1"]


def test_a_sibling_another_process_executes_is_skipped_not_launched_twice(tmp_path):
    """Catches a wave that launches a sibling whose run a live process still
    executes (a duplicate dispatch): n2's process lock is held; the round skips
    it (`WAVE_SKIPPED`), launches and merges n1, and the next round halts
    RUN_IN_FLIGHT on n2 as the sequential loop does."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n2")
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    fd = _hold_process_lock(root, "n2")
    try:
        result = ProjectController(store, launcher, Gates(), width=2).run()
    finally:
        _let_go(fd)
    assert result.halt is HaltClass.RUN_IN_FLIGHT and "n2" in result.reason
    assert [s.node for s in result.steps if s.kind == "WAVE_SKIPPED"] == ["n2"]
    assert launcher.merged == ["n1"]
    assert not [c for c in launcher.calls if "run" in c and "n2" in c]
    assert _held_ids(root) == []


def test_an_already_accepted_sibling_is_settled_alone_and_the_earlier_holds_go_back(tmp_path):
    """Catches the holds taken for earlier candidates staying HELD when a later
    one turns out to be already accepted: n1 is new (its `prepare` took a hold),
    n2's run already holds an acceptance. n2 is merged alone, and n1's hold is
    ended *by the round that re-planned*, not by whichever launch comes later."""
    repo, root, store = _world(tmp_path)
    _already_started(root, repo, store, "n2")
    run = RunStore(root, "n2")
    state = run.read_state()
    run.write_state(state.model_copy(update={
        "stage": Stage.CHECKPOINTED,
        "last_accepted_candidate": Candidate(
            candidate_id="n2-i1", repo_path=state.repo_path, commit="a" * 40,
            tree_clean=True, tree_digest="d" * 16)}))
    launcher = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    result = ProjectController(store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert any(s.kind == "ALREADY_ACCEPTED" and s.node == "n2" for s in result.steps)
    rows = _first_hold_of_each_run(root)
    assert rows["n1"].status is HoldStatus.SETTLED
    assert "dispatch returned" not in rows["n1"].reason              # ended by the round, not by a launch
    assert "n2" not in rows                                         # the accepted run was never held for


def test_an_adopting_executor_refuses_a_run_with_more_than_one_open_hold(tmp_path):
    """Catches adoption of one of several open holds of a run (which one is the
    run's? the others would never be ended): two HELD rows of n1, an adopting
    executor. n1 is refused BUDGET, nothing launches, both holds are untouched."""
    from test_wave import Rig, ok
    from hoh import wave
    rig = Rig(tmp_path, ["n1"], ceiling=20, want=2)
    reservations = rig.reservations()
    first = reservations.reserve("n1", ceiling=20, spent=lambda: {}, want=2)
    rows = reservations.read()
    reservations._write(rows + [rows[0].model_copy(update={"reservation_id": "deadbeef-n1"})])
    launched = []
    executor = rig.executor(budget=wave.WaveBudget(reservations, 20, lambda: {}, 2, adopt=True))
    result = executor.run_wave("w1", rig.plan("n1"), lambda n, a: launched.append(n.id) or ok())
    row = result.record.node("n1")
    assert row.state is NodeState.REFUSED and row.refusal == "BUDGET" and "2 open holds" in row.detail
    assert launched == []
    assert sorted(r.reservation_id for r in reservations.held()) == sorted(
        [first.reservation_id, "deadbeef-n1"])
    lease = rig.lease_on_disk("n1")
    assert lease is None or lease.released_at
