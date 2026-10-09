"""P2-15 on the product's own path: the orchestration executes `programlogic.reduce`.

`hoh.programlogic` is the pure reducer of the plan's durable execution rule
(COMMAND -> ACTIVITY -> EVENT -> STATE REDUCER -> MATERIALIZED STATE). Until the
dispatch activity log existed nothing in the product executed it. These tests
run the product -- `ProjectController.run`, `resume_decision`, `abandon` and the
`hoh project` commands through `hoh.cli.main` -- and hold three things:

* the controller *records* the dispatch as events, durably, in the same write as
  the lifecycle transition (the effect is separate from the decision);
* the decision path *folds* those events with `programlogic.reduce` and its
  result changes what the product does (the decision is a pure function of the
  events, with no living chat);
* the product decides **as it did before** wherever the log and the lifecycles
  agree, which is every state the controller writes (differential tests against
  the controller with the log switched off).

Hermetic: a scripted launcher and gate runner, no Herdr, no agent, no network.
"""

from __future__ import annotations

import json

import pytest

from hoh import cli, migrations, programlogic
from hoh import projectactivity as pa
from hoh.orchestrator import (
    GateRunner,
    HaltClass,
    ProjectController,
    RunLauncher,
    RunOutcome,
    RunVerdict,
)
from hoh.programlogic import ActivityStatus, Event, EventKind
from hoh.project import (
    ActionClass,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
)
from hoh.projectstore import BLOCK, EVALUATE, RESUME, ProjectStore, abandon, resume_decision
from hoh.store import StoreError

NOW = "2026-10-08T00:00:00Z"
STAMP = "2026-10-08T00-00-00Z"


# --------------------------------------------------------------------------- #
# The scripted outside world
# --------------------------------------------------------------------------- #

class Launcher(RunLauncher):
    def __init__(self, verdicts=None, evaluations=None, on_launch=None, retry_after=None):
        self.verdicts = {k: list(v) if isinstance(v, list) else [v]
                         for k, v in (verdicts or {}).items()}
        self.evaluations = dict(evaluations or {})
        self.on_launch = on_launch
        self.retry_after = retry_after
        self.launched: list[str] = []
        self.merged: list[str] = []

    def action_class(self, node):
        return ActionClass.INTERNAL

    def depends_on(self, a, b):
        return False

    def launch(self, node):
        self.launched.append(node.id)
        if self.on_launch is not None:
            self.on_launch(node)
        queue = self.verdicts.get(node.id) or [RunVerdict.ACCEPTED]
        verdict = queue.pop(0) if len(queue) > 1 else queue[0]
        return RunOutcome(verdict=verdict, detail=f"scripted {verdict.value}",
                          candidate=f"{node.id}-cand", candidate_tree=f"{node.id}-tree",
                          retry_after=self.retry_after
                          if verdict is RunVerdict.PROVIDER_UNAVAILABLE else None)

    def accepted_baseline(self, node):
        return None

    def evaluate(self, node):
        verdict = self.evaluations.get(node.id, RunVerdict.UNDETERMINED)
        return RunOutcome(verdict=verdict, detail="from the recorded run state",
                          candidate=f"{node.id}-cand", candidate_tree=f"{node.id}-tree")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return True


class Gates(GateRunner):
    def subject(self):
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject, exit_code=0)]


def project(tmp_path, nodes, pid="p") -> ProjectStore:
    store = ProjectStore(tmp_path, pid)
    store.create(ProjectState(project_id=pid, repo_path=str(tmp_path), nodes=list(nodes)))
    return store


def fold(state):
    """The product's own reading: the pure reducer over the recorded events."""
    return programlogic.reduce(state.activity_events)


def kinds(state):
    return [e.kind for e in state.activity_events]


def event(seq, kind, node="a"):
    return Event(event_id=f"dispatch/{node}/{seq}", sequence=seq, kind=kind,
                 activity_id=f"dispatch/{node}",
                 payload={"name": "dispatch", "idempotency_key": f"dispatch:{node}", "node": node},
                 recorded_at="2026-10-08T00:00:00Z")


class Crash(BaseException):
    """A process dying inside the launch: not an exception the controller handles."""


# --------------------------------------------------------------------------- #
# The controller records the dispatch as events
# --------------------------------------------------------------------------- #

def test_an_accepted_dispatch_folds_to_a_completed_activity(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    result = ProjectController(store, Launcher(), Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    state = store.read_state()
    assert kinds(state) == [EventKind.STARTED, EventKind.SUCCEEDED]
    folded = fold(state)
    assert folded.completed == ("dispatch:a",)
    assert folded.open_keys == () and folded.failed == ()
    assert folded.activities["dispatch/a"].attempts == 1
    assert folded.activities["dispatch/a"].result_digest
    assert pa.in_flight_disagreements(state) == []


def test_started_is_durable_before_the_launch_and_in_the_write_that_says_running(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    seen = {}

    def during_launch(node):
        on_disk = store.read_state()
        seen["lifecycle"] = on_disk.node("a").lifecycle
        seen["open"] = fold(on_disk).open_keys

    ProjectController(store, Launcher(on_launch=during_launch), Gates()).run()
    assert seen == {"lifecycle": Lifecycle.RUNNING, "open": ("dispatch:a",)}


def test_a_rejection_is_a_failed_attempt_and_the_limit_abandons_the_activity(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    launcher = Launcher(verdicts={"a": RunVerdict.REJECTED})
    ProjectController(store, launcher, Gates()).run()
    state = store.read_state()
    assert state.node("a").lifecycle is Lifecycle.ABANDONED
    assert len(launcher.launched) == 4
    folded = fold(state)
    assert folded.activities["dispatch/a"].status is ActivityStatus.ABANDONED
    assert folded.activities["dispatch/a"].attempts == 4
    assert folded.failed == ("dispatch:a",) and folded.open_keys == ()
    assert kinds(state).count(EventKind.FAILED) == 4
    assert kinds(state)[-1] is EventKind.ABANDONED


@pytest.mark.parametrize("verdict", [
    RunVerdict.PROVIDER_UNAVAILABLE, RunVerdict.NEEDS_APPROVAL, RunVerdict.BUDGET_EXHAUSTED,
    RunVerdict.NOT_RUN, RunVerdict.STORAGE_UNAVAILABLE, RunVerdict.UNDETERMINED,
    RunVerdict.NOT_STARTED,
])
def test_every_other_verdict_closes_the_activity_it_started(tmp_path, verdict):
    store = project(tmp_path, [TaskNode(id="a")])
    ProjectController(store, Launcher(verdicts={"a": verdict}), Gates(), max_rounds=2).run()
    state = store.read_state()
    folded = fold(state)
    assert folded.open_keys == (), f"{verdict}: the activity was left open"
    assert folded.activities["dispatch/a"].status is ActivityStatus.FAILED
    assert verdict.value in folded.activities["dispatch/a"].failure
    assert pa.in_flight_disagreements(state) == []


def test_a_run_still_executing_elsewhere_keeps_its_activity_open(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    result = ProjectController(store, Launcher(verdicts={"a": RunVerdict.IN_FLIGHT}), Gates()).run()
    assert result.halt is HaltClass.RUN_IN_FLIGHT
    state = store.read_state()
    assert state.node("a").lifecycle is Lifecycle.RUNNING
    assert fold(state).open_keys == ("dispatch:a",)
    assert pa.in_flight_disagreements(state) == []


def test_settling_an_in_flight_verdict_leaves_the_activity_open_in_memory_too(tmp_path):
    """The IN_FLIGHT branch persists nothing today; the guard keeps the in-memory state
    truthful so a future persist there cannot close a dispatch that is still executing."""
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED)]
    controller = ProjectController(store, Launcher(), Gates())
    result = controller._settle(state, "a", RunOutcome(verdict=RunVerdict.IN_FLIGHT), 1, [])
    assert result.halt is HaltClass.RUN_IN_FLIGHT
    assert fold(state).open_keys == ("dispatch:a",)
    assert kinds(state) == [EventKind.STARTED]


def test_a_resumed_session_settles_the_open_activity_without_a_second_launch(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])

    def crash(node):
        raise Crash()

    first = Launcher(on_launch=crash)
    with pytest.raises(Crash):
        ProjectController(store, first, Gates()).run()
    crashed = store.read_state()
    assert crashed.node("a").lifecycle is Lifecycle.RUNNING
    assert fold(crashed).open_keys == ("dispatch:a",)

    second = Launcher(evaluations={"a": RunVerdict.ACCEPTED})
    result = ProjectController(store, second, Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert second.launched == [] and second.merged == ["a"]
    state = store.read_state()
    assert kinds(state) == [EventKind.STARTED, EventKind.SUCCEEDED]
    assert fold(state).completed == ("dispatch:a",)


def test_abandon_closes_a_dispatch_that_is_open(tmp_path):
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED)]
    store.write_state(state)

    settled = abandon(store, "a", "the work should never have started")
    assert settled.node("a").lifecycle is Lifecycle.ABANDONED
    assert fold(settled).activities["dispatch/a"].status is ActivityStatus.ABANDONED
    assert fold(settled).open_keys == ()
    assert pa.in_flight_disagreements(settled) == []


def test_resuming_a_state_from_before_the_log_invents_no_history(tmp_path):
    """A RUNNING node with no logged dispatch is settled by its run record, and the log
    stays empty: nothing records a STARTED that nobody saw."""
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    launcher = Launcher(evaluations={"a": RunVerdict.ACCEPTED})
    assert ProjectController(store, launcher, Gates()).run().halt is HaltClass.CLOSED
    assert launcher.merged == ["a"] and launcher.launched == []
    assert store.read_state().activity_events == []


def test_abandon_on_a_state_without_a_log_records_nothing(tmp_path):
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED)])
    settled = abandon(store, "a", "nothing to repair")
    assert settled.activity_events == []


# --------------------------------------------------------------------------- #
# The decision path executes the reducer, and its result decides
# --------------------------------------------------------------------------- #

def counting_reduce(monkeypatch, replacement=None):
    calls = []
    real = programlogic.reduce

    def spy(events, **kw):
        calls.append(len(list(events)))
        return replacement if replacement is not None else real(events, **kw)

    monkeypatch.setattr(pa, "reduce", spy)
    return calls


def test_project_resume_through_the_cli_executes_the_reducer(tmp_path, capsys, monkeypatch):
    from hoh.cli import main

    store = project(tmp_path, [TaskNode(id="a")])
    state = store.read_state()
    state.nodes = [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]
    state.activity_events = [event(1, EventKind.STARTED)]
    store.write_state(state)

    calls = counting_reduce(monkeypatch)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == EVALUATE
    assert calls == [1]


def test_project_status_and_list_execute_the_reducer(tmp_path, capsys, monkeypatch):
    from hoh.cli import main

    project(tmp_path, [TaskNode(id="a")])
    calls = counting_reduce(monkeypatch)
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 0
    assert main(["--root", str(tmp_path), "project", "list"]) == 0
    assert len(calls) == 2


def test_the_controller_executes_the_reducer_when_it_starts_and_when_it_settles(tmp_path, monkeypatch):
    store = project(tmp_path, [TaskNode(id="a")])
    calls = counting_reduce(monkeypatch)
    ProjectController(store, Launcher(), Gates()).run()
    assert len(calls) >= 2          # the start-of-drive check, and the close of the dispatch


def test_a_log_that_holds_a_dispatch_open_on_a_node_that_is_not_running_blocks_the_resume(
        tmp_path, capsys):
    from hoh.cli import main

    store = project(tmp_path, [TaskNode(id="a")])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED)]       # node a is READY
    store.write_state(state)

    verdict, why = resume_decision(store.read_state())
    assert verdict == BLOCK
    assert "disagree" in why and "dispatch of a" in why and "READY" in why
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == BLOCK


def test_a_running_node_whose_dispatch_the_log_shows_finished_blocks_the_resume(tmp_path):
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED), event(2, EventKind.SUCCEEDED)]
    store.write_state(state)
    verdict, why = resume_decision(store.read_state())
    assert verdict == BLOCK
    assert "RUNNING" in why and "finished" in why


def test_a_gap_in_the_log_blocks_instead_of_being_skipped(tmp_path, capsys):
    from hoh.cli import main

    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED), event(3, EventKind.FAILED)]
    store.write_state(state)
    verdict, why = resume_decision(store.read_state())
    assert verdict == BLOCK
    assert "gap in event sequence" in why
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 0
    assert "block" in capsys.readouterr().out


def test_a_duplicate_sequence_blocks(tmp_path):
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED), event(1, EventKind.FAILED)]
    store.write_state(state)
    verdict, why = resume_decision(store.read_state())
    assert verdict == BLOCK and "duplicate event sequence" in why


def test_a_running_node_the_log_never_heard_of_is_still_evaluated(tmp_path):
    """A state from before the log: judged by its lifecycle alone, as it always was."""
    state = ProjectState(project_id="p", repo_path=str(tmp_path),
                         nodes=[TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    assert state.activity_events == []
    assert resume_decision(state)[0] == EVALUATE


def test_the_controller_will_not_drive_on_a_disagreeing_log(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED)]       # a is READY, its dispatch "open"
    store.write_state(state)
    launcher = Launcher()
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.AMBIGUOUS
    assert "disagree" in result.reason
    assert launcher.launched == [] and launcher.merged == []
    assert store.read_state().node("a").lifecycle is Lifecycle.READY


def test_the_controller_halts_on_a_log_it_cannot_fold(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED), event(3, EventKind.FAILED)]
    store.write_state(state)
    launcher = Launcher()
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.CORRUPT_STATE
    assert "gap in event sequence" in result.reason
    assert launcher.launched == []


def test_the_reducers_answer_is_what_the_decision_uses(tmp_path, monkeypatch):
    """Not merely called: swap what the reducer returns and the verdict follows."""
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED)]
    store.write_state(state)
    state = store.read_state()
    assert resume_decision(state)[0] == EVALUATE

    forged = programlogic.reduce([event(1, EventKind.STARTED), event(2, EventKind.SUCCEEDED)])
    counting_reduce(monkeypatch, replacement=forged)
    assert resume_decision(state)[0] == BLOCK


def test_the_not_started_reset_closes_the_dispatch_it_resets(tmp_path):
    """The start-of-drive RESET_TO_READY: the node becomes READY, so its dispatch must be closed in
    that same write -- else the next resume reads a READY node with an open dispatch and blocks."""
    store = project(tmp_path, [TaskNode(id="n1", lifecycle=Lifecycle.RUNNING),
                               TaskNode(id="n2", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED, "n1"), event(2, EventKind.STARTED, "n2")]
    store.write_state(state)
    launcher = Launcher(evaluations={"n1": RunVerdict.NOT_STARTED, "n2": RunVerdict.IN_FLIGHT})
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.RUN_IN_FLIGHT, result.reason
    after = store.read_state()
    assert after.node("n1").lifecycle is Lifecycle.READY and after.node("n2").lifecycle is Lifecycle.RUNNING
    folded = fold(after)
    assert folded.open_keys == ("dispatch:n2",)
    assert folded.activities["dispatch/n1"].status is ActivityStatus.FAILED
    assert "NOT_STARTED" in folded.activities["dispatch/n1"].failure
    assert pa.in_flight_disagreements(after) == []
    assert resume_decision(after)[0] == EVALUATE          # n2 is still the one to evaluate, not a block


def test_two_running_nodes_reset_together_are_both_reset(tmp_path):
    """The reset loop persists after each node, which replaces the state object; a lifecycle written to
    the stale one is lost (a defect on the pin, found while wiring the log). Both nodes are reset, both run."""
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING),
                               TaskNode(id="b", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED, "a"), event(2, EventKind.STARTED, "b")]
    store.write_state(state)
    launcher = Launcher(evaluations={"a": RunVerdict.NOT_STARTED, "b": RunVerdict.NOT_STARTED})
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert launcher.launched == ["a", "b"] and sorted(launcher.merged) == ["a", "b"]
    final = store.read_state()
    assert {n.id: n.lifecycle for n in final.nodes} == {"a": Lifecycle.MERGED, "b": Lifecycle.MERGED}
    assert fold(final).completed == ("dispatch:a", "dispatch:b") and fold(final).open_keys == ()
    assert pa.in_flight_disagreements(final) == []


def written_states(monkeypatch):
    """Every state handed to `ProjectStore.write_state`, as a deep copy at the moment of the write."""
    seen = []
    real = ProjectStore.write_state

    def spy(self, state):
        seen.append(state.model_copy(deep=True))
        return real(self, state)

    monkeypatch.setattr(ProjectStore, "write_state", spy)
    return seen


@pytest.mark.parametrize("name", ["chain-accepted", "rejected-to-abandon", "provider-unavailable",
                                  "needs-approval", "in-flight", "rejected-then-accepted"])
def test_every_state_the_controller_writes_agrees_with_its_log(tmp_path, monkeypatch, name):
    """The log and the lifecycle are one write: no state the controller persists has a RUNNING node
    without an open dispatch, an open dispatch without a RUNNING node, or a finished dispatch on a
    RUNNING one. (A split of the event from its transition shows up as a written state that disagrees.)"""
    nodes, verdicts, evaluations = SCENARIOS[name]
    store = project(tmp_path, [n.model_copy(deep=True) for n in nodes])
    seen = written_states(monkeypatch)
    ProjectController(store, Launcher(verdicts=verdicts, evaluations=evaluations), Gates(),
                      max_rounds=12).run()
    assert seen, "nothing was written"
    for state in seen:
        running = {n.id for n in state.nodes if n.lifecycle is Lifecycle.RUNNING}
        opened = {k.split(":", 1)[1] for k in fold(state).open_keys}
        assert running == opened, (state.write_seq, running, opened)
        assert pa.in_flight_disagreements(state) == []


def test_a_dispatch_open_for_a_node_that_is_not_in_the_state_blocks(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    state = store.read_state()
    state.activity_events = [event(1, EventKind.STARTED, "ghost")]
    store.write_state(state)
    verdict, why = resume_decision(store.read_state())
    assert verdict == BLOCK and "'ghost'" in why and "no such node" in why
    launcher = Launcher()
    result = ProjectController(store, launcher, Gates()).run()
    assert result.halt is HaltClass.AMBIGUOUS and launcher.launched == []


def test_abandon_still_settles_a_node_whose_log_cannot_be_folded(tmp_path):
    """The operator's way out does not depend on the log it is getting out of."""
    store = project(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)])
    state = store.read_state()
    damaged = [event(1, EventKind.STARTED), event(3, EventKind.FAILED)]
    state.activity_events = list(damaged)
    store.write_state(state)
    settled = abandon(store, "a", "the work should never have started")      # does not raise
    assert settled.node("a").lifecycle is Lifecycle.ABANDONED
    assert [e.sequence for e in settled.activity_events] == [1, 3]           # the log is left as it is
    assert resume_decision(settled)[0] == BLOCK                              # and still blocks, by name
    assert store.parked_states(), "the predecessor is parked: that is the recovery"


# --------------------------------------------------------------------------- #
# The schema: version 2 carries the log, version 1 is refused until migrated
# --------------------------------------------------------------------------- #

def test_a_version_1_project_state_is_refused_until_migrated_then_reads_with_an_empty_log(
        tmp_path, capsys):
    root = tmp_path / "runs"
    store = project(root, [TaskNode(id="a")])
    path = store.state_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2 and doc["activity_events"] == []
    doc["schema_version"] = 1
    del doc["activity_events"]                                       # what a version-1 build wrote
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    old = path.read_bytes()

    steps = migrations.STATE_REGISTRIES["project"].steps
    assert [s.name for s in steps] == ["project-add-activity-events"]
    assert migrations.STATE_REGISTRIES["project"].current_version == 2

    # refused on read, naming the command -- by the store, by the CLI, by the controller
    with pytest.raises(StoreError, match="hoh migrate"):
        store.read_state()
    capsys.readouterr()
    assert cli.main(["--root", str(root), "project", "resume", "p"]) == 3
    assert "hoh migrate" in capsys.readouterr().err
    launcher = Launcher()
    assert ProjectController(store, launcher, Gates()).run().halt is HaltClass.CORRUPT_STATE
    assert launcher.launched == [] and path.read_bytes() == old

    # the plan writes nothing and names the step
    capsys.readouterr()
    assert cli.main(["--root", str(root), "migrate"]) == 0
    plan = json.loads(capsys.readouterr().out)
    record = next(r for r in plan["records"] if r["kind"] == "project")
    assert (record["direction"], record["from_version"], record["to_version"], record["steps"]) == (
        "FORWARD", 1, 2, ["project-add-activity-events"])
    assert path.read_bytes() == old

    # applied: the old bytes are parked, the record is version 2 with the empty log, and it reads
    assert cli.main(["--root", str(root), "migrate", "--apply", "--now", NOW]) == 0
    capsys.readouterr()
    assert (path.parent / f"project.json.v1.{STAMP}").read_bytes() == old
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2 and migrated["activity_events"] == []
    state = store.read_state()
    assert state.activity_events == [] and resume_decision(state)[0] == RESUME
    assert ProjectController(store, Launcher(), Gates()).run().halt is HaltClass.CLOSED
    assert kinds(store.read_state()) == [EventKind.STARTED, EventKind.SUCCEEDED]

    # a second apply finds nothing to do
    after = path.read_bytes()
    assert cli.main(["--root", str(root), "migrate", "--apply", "--now", NOW]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["forward"] == 0 and path.read_bytes() == after


# --------------------------------------------------------------------------- #
# Behaviour equivalence: the product decides as it did without the log
# --------------------------------------------------------------------------- #

SCENARIOS = {
    "chain-accepted": (
        [TaskNode(id="a"), TaskNode(id="b", dependencies=["a"])], {}, {}),
    "rejected-then-accepted": (
        [TaskNode(id="a")], {"a": [RunVerdict.REJECTED, RunVerdict.REJECTED, RunVerdict.ACCEPTED]}, {}),
    "rejected-to-abandon": (
        [TaskNode(id="a"), TaskNode(id="b")], {"a": RunVerdict.REJECTED}, {}),
    "provider-unavailable": (
        [TaskNode(id="a"), TaskNode(id="b")], {"a": RunVerdict.PROVIDER_UNAVAILABLE}, {}),
    "needs-approval": (
        [TaskNode(id="a")], {"a": RunVerdict.NEEDS_APPROVAL}, {}),
    "not-run": (
        [TaskNode(id="a")], {"a": RunVerdict.NOT_RUN}, {}),
    "in-flight": (
        [TaskNode(id="a")], {"a": RunVerdict.IN_FLIGHT}, {}),
    "resume-running-accepted": (
        [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)], {}, {"a": RunVerdict.ACCEPTED}),
    "resume-running-undetermined": (
        [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)], {}, {}),
    "resume-running-not-started": (
        [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)], {}, {"a": RunVerdict.NOT_STARTED}),
}


#: `ProjectStore.write_state` calls per scenario, measured on the base (b0fd7614 + p212) which has no log:
#: the log adds none (a transition and its event are one write, never two).
PIN_WRITES = {
    "chain-accepted": 6, "in-flight": 1, "needs-approval": 2, "not-run": 2, "provider-unavailable": 2,
    "rejected-then-accepted": 8, "rejected-to-abandon": 12, "resume-running-accepted": 3,
    "resume-running-not-started": 5, "resume-running-undetermined": 0,
}


def drive(tmp_path, name, with_log, monkeypatch):
    nodes, verdicts, evaluations = SCENARIOS[name]
    store = project(tmp_path / name / ("log" if with_log else "nolog"),
                    [n.model_copy(deep=True) for n in nodes])
    if not with_log:
        for attr in ("start_dispatch", "close_dispatch", "abandon_dispatch"):
            monkeypatch.setattr("hoh.orchestrator." + attr, lambda *a, **k: None)
        monkeypatch.setattr("hoh.orchestrator.in_flight_disagreements", lambda state: [])
        monkeypatch.setattr("hoh.projectstore.abandon_dispatch", lambda *a, **k: None)
    snapshots = []
    writes = []
    real_write = ProjectStore.write_state
    monkeypatch.setattr(ProjectStore, "write_state",
                        lambda self, state: (writes.append(1), real_write(self, state))[1])
    launcher = Launcher(verdicts=verdicts, evaluations=evaluations,
                        on_launch=lambda node: snapshots.append(store.read_state()))
    result = ProjectController(store, launcher, Gates(), max_rounds=12).run()
    final = store.read_state()
    snapshots.append(final)
    stripped = [s.model_copy(update={"activity_events": []}) for s in snapshots]
    return {
        "halt": result.halt, "reason": result.reason, "writes": len(writes),
        "steps": [(s.round, str(s.kind), s.node, s.detail) for s in result.steps],
        "launched": launcher.launched, "merged": launcher.merged,
        "lifecycles": {n.id: (n.lifecycle, n.rejections, n.blocked_kind) for n in final.nodes},
        "decisions": [(d.kind, d.reason) for d in final.decisions],
        "verdicts": [resume_decision(s) for s in stripped],
    }, snapshots


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_controller_decides_as_it_did_without_the_log(tmp_path, monkeypatch, name):
    with_log, snaps = drive(tmp_path, name, True, monkeypatch)
    monkeypatch.undo()
    without, _ = drive(tmp_path, name, False, monkeypatch)
    assert with_log == without
    assert with_log["writes"] == PIN_WRITES[name], "the log changed the number of writes"
    if not name.startswith("resume-"):      # those start from a state written before the log
        assert any(s.activity_events for s in snaps), "the log was not written"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_resume_decision_is_the_same_with_the_log_as_without_it_at_every_persisted_state(
        tmp_path, monkeypatch, name):
    _, snapshots = drive(tmp_path, name, True, monkeypatch)
    for snap in snapshots:
        stripped = snap.model_copy(update={"activity_events": []})
        assert resume_decision(snap) == resume_decision(stripped), snap.write_seq
        assert pa.in_flight_disagreements(snap) == []


# --------------------------------------------------------------------------- #
# The state itself
# --------------------------------------------------------------------------- #

def test_a_state_written_before_the_log_still_loads_and_the_log_round_trips(tmp_path):
    store = project(tmp_path, [TaskNode(id="a")])
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw.pop("activity_events")
    old = ProjectState.model_validate(raw)
    assert old.activity_events == []

    ProjectController(store, Launcher(), Gates()).run()
    on_disk = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert [e["kind"] for e in on_disk["activity_events"]] == ["STARTED", "SUCCEEDED"]
    assert store.read_state().activity_events == ProjectState.model_validate(on_disk).activity_events
