"""Durable timers and the sentinel: does a wait survive the process?

The cases are the plan's own fault list for the durable supervisor: DS3 (a
usage limit and its retry-at), DS4 (a terminal project disarms its timers),
DS6 (a stale master cannot write) and DS8 (the program-stuck detector catches
a non-terminal object with no next action).
"""
from __future__ import annotations

import json

import pytest

from hoh.sentinel import Sweep, stuck_nodes, stuck_run, stuck_timers, sweep
from hoh.timers import (
    Timer,
    TimerKind,
    TimerStatus,
    TimerStore,
    TimerStoreError,
    check_time,
    in_seconds,
)

PAST = "2026-09-22T10:00:00Z"
NOW = "2026-09-22T12:00:00Z"
LATER = "2026-09-22T18:00:00Z"


def _store(tmp_path) -> TimerStore:
    return TimerStore(tmp_path / "runs")


def test_an_armed_timer_is_read_back_by_a_different_store_object(tmp_path):
    """The whole point: the wait is on disk, not in the process that armed it."""
    _store(tmp_path).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=LATER,
                             reason="usage limit reached", run_id="d7")
    again = TimerStore(tmp_path / "runs").read()
    assert [t.kind for t in again] == [TimerKind.PROVIDER_RETRY]
    assert again[0].run_id == "d7" and again[0].status is TimerStatus.ARMED


def test_ds3_a_provider_retry_comes_due_and_proposes_a_probe(tmp_path):
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, run_id="d7",
                  reason="usage limit reached; resets at 10:00")
    out = sweep(store, now=NOW)
    assert [p.action for p in out.due] == ["probe_provider_and_resume"]
    assert "resume the run exactly once" in out.due[0].why


def test_a_timer_that_is_not_due_yet_is_not_proposed(tmp_path):
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=LATER, run_id="d7")
    assert sweep(store, now=NOW).quiet


def test_ds4_a_terminal_program_disarms_its_pending_recovery(tmp_path):
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, run_id="d7", program_id="p1")
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST, run_id="d8", program_id="p2")
    changed = store.disarm_all("program p1 reached a terminal state", program_id="p1")
    assert [t.run_id for t in changed] == ["d7"]
    out = sweep(store, now=NOW)
    assert [p.timer.run_id for p in out.due] == ["d8"]      # p2's timer is untouched
    assert store.read()[0].disarmed_reason.startswith("program p1 reached")


def test_a_disarmed_timer_keeps_its_row_rather_than_disappearing(tmp_path):
    store = _store(tmp_path)
    t = store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST)
    store.disarm(t.timer_id, "no longer needed")
    rows = store.read()
    assert len(rows) == 1 and rows[0].status is TimerStatus.DISARMED
    assert rows[0].disarmed_at and rows[0].armed_at


def test_ds6_a_timer_from_a_superseded_session_is_refused_not_fired(tmp_path):
    """A note left behind by a master that has been replaced must not let that
    master act after its epoch ended."""
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST, session_epoch=1, program_epoch=3)
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST, session_epoch=2, program_epoch=3)
    out = sweep(store, now=NOW, program_epoch=3, session_epoch=2)
    assert len(out.due) == 1 and out.due[0].timer.session_epoch == 2
    assert len(out.refused) == 1 and "superseded by 2" in out.refused[0][1]


def test_a_timer_from_another_program_epoch_is_refused_in_both_directions(tmp_path):
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST, program_epoch=2)
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST, program_epoch=4)
    out = sweep(store, now=NOW, program_epoch=3)
    assert not out.due
    assert {r[1].split(",")[0] for r in out.refused} == {
        "armed under program epoch 2", "armed under program epoch 4"}


def test_two_timers_with_one_id_are_refused(tmp_path):
    store = _store(tmp_path)
    t = store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST)
    with pytest.raises(TimerStoreError):
        store.arm(t)


def test_a_due_time_that_is_not_utc_is_refused(tmp_path):
    store = _store(tmp_path)
    for bad in ("2026-09-22 18:00:00", "2026-09-22T18:00:00+02:00", "later", ""):
        with pytest.raises(ValueError):
            store.arm_new(kind=TimerKind.HEARTBEAT, due_at=bad)
    assert check_time(LATER) == LATER


def test_the_store_survives_a_half_written_file_by_refusing_it(tmp_path):
    store = _store(tmp_path)
    store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST)
    store.path.write_text('{"schema_version": "hoh-timer/1", "timers": [{"tim')
    with pytest.raises(TimerStoreError):
        store.read()


def test_the_file_is_replaced_atomically_and_leaves_no_temporary_behind(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.arm_new(kind=TimerKind.HEARTBEAT, due_at=in_seconds(60 * (i + 1)))
    assert [p.name for p in store.root.iterdir() if p.name.startswith(".timers-")] == []
    assert json.loads(store.path.read_text())["schema_version"] == "hoh-timer/1"


def test_firing_records_when_and_leaves_the_others_alone(tmp_path):
    store = _store(tmp_path)
    a = store.arm_new(kind=TimerKind.HEARTBEAT, due_at=PAST)
    store.arm_new(kind=TimerKind.REVIEW_DUE, due_at=PAST)
    store.fire(a.timer_id)
    rows = {t.timer_id: t for t in store.read()}
    assert rows[a.timer_id].status is TimerStatus.FIRED and rows[a.timer_id].fired_at
    assert sum(t.status is TimerStatus.ARMED for t in rows.values()) == 1


def test_every_kind_has_an_action_so_a_due_timer_is_never_silently_ignored(tmp_path):
    store = _store(tmp_path)
    for kind in TimerKind:
        store.arm_new(kind=kind, due_at=PAST)
    out = sweep(store, now=NOW)
    assert len(out.due) == len(list(TimerKind)) and not out.defects


# --- never-stuck (DS8) ---------------------------------------------------------

class _Run:
    def __init__(self, **kw):
        self.run_id = kw.get("run_id", "d7")
        self.condition = kw.get("condition", "ACTIVE")
        self.stage = kw.get("stage", "DEVELOPING")
        self.retry_after = kw.get("retry_after")
        self.blocked_kind = kw.get("blocked_kind")
        self.blocked_reason = kw.get("blocked_reason")


def test_ds8_a_blocked_run_with_no_way_out_is_a_defect():
    found = stuck_run(_Run(condition="BLOCKED", blocked_kind="provider"))
    assert len(found) == 1 and "no retry_at" in str(found[0])


def test_a_blocked_run_with_a_retry_at_is_not_stuck():
    assert stuck_run(_Run(condition="BLOCKED", blocked_kind="provider",
                          retry_after=LATER)) == []


def test_a_blocked_run_waiting_on_a_named_human_is_not_stuck():
    assert stuck_run(_Run(condition="BLOCKED", blocked_kind="human",
                          blocked_reason="the operator must grant folder trust")) == []


def test_a_blocked_run_with_an_armed_timer_is_not_stuck():
    timer = Timer(timer_id="t1", due_at=LATER, kind=TimerKind.PROVIDER_RETRY, run_id="d7")
    assert stuck_run(_Run(condition="BLOCKED", blocked_kind="provider"), timers=[timer]) == []


def test_a_terminal_run_owes_nothing():
    for condition in ("DONE", "CANCELLED", "FAILED"):
        assert stuck_run(_Run(condition=condition)) == []


def test_an_active_run_has_the_loop_as_its_next_action():
    assert stuck_run(_Run(condition="ACTIVE")) == []


class _Node:
    def __init__(self, node_id, lifecycle, note="", settled=False):
        self.id = node_id
        self.lifecycle = lifecycle
        self.note = note
        self.settled = settled


class _Project:
    def __init__(self, nodes):
        self.nodes = nodes


def test_a_blocked_node_with_nothing_naming_an_owner_is_a_defect():
    found = stuck_nodes(_Project([_Node("n1", "BLOCKED")]))
    assert len(found) == 1 and found[0].object_id == "n1"


def test_a_blocked_node_with_a_note_or_a_timer_is_not_a_defect():
    assert stuck_nodes(_Project([_Node("n1", "BLOCKED", note="waiting on the operator")])) == []
    timer = Timer(timer_id="t", due_at=LATER, kind=TimerKind.EXTERNAL_WAIT, node_id="n1")
    assert stuck_nodes(_Project([_Node("n1", "BLOCKED")]), timers=[timer]) == []


def test_ready_and_settled_nodes_are_not_stuck():
    assert stuck_nodes(_Project([_Node("a", "READY"), _Node("b", "DONE", settled=True)])) == []


def test_a_timer_long_past_due_means_nobody_is_sweeping():
    timer = Timer(timer_id="t1", due_at="2026-09-20T10:00:00Z", kind=TimerKind.HEARTBEAT)
    assert stuck_timers([timer], now=NOW) and "still armed" in str(stuck_timers([timer], now=NOW)[0])
    assert stuck_timers([timer], now="2026-09-20T11:00:00Z") == []


def test_a_sweep_that_found_nothing_says_so():
    assert Sweep(at=NOW).quiet


# --- against the real objects, not stand-ins ------------------------------------

def _real_run(**kw):
    from hoh.contracts import RunState

    return RunState(run_id="d7", repo_path="/x", project_name="p", spec_path="/x/spec.md",
                    spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16,
                    **kw)


def test_the_detector_reads_the_real_run_state_not_only_a_stand_in():
    """The stand-in classes above hid a bug: the first draft read `.value` and
    nothing else, so a plain string field became an empty one -- an active run
    came out as a defect and a blocked node as healthy. Both shapes reach this
    code, so both are exercised."""
    from hoh.contracts import Condition, RunState, Stage

    active = _real_run()
    assert isinstance(active.condition, Condition) and isinstance(active.stage, Stage)
    assert stuck_run(active) == []

    blocked = _real_run(condition=Condition.BLOCKED, blocked_kind="provider")
    found = stuck_run(blocked)
    assert len(found) == 1, found
    assert stuck_run(_real_run(condition=Condition.BLOCKED, blocked_kind="provider",
                               retry_after=LATER)) == []

    # And what a resumed session reads back out of JSON: the same state as
    # plain strings, which must give the same answer.
    as_json = RunState.model_validate(json.loads(blocked.model_dump_json()))
    assert len(stuck_run(as_json)) == 1


def test_the_detector_reads_real_task_nodes():
    from hoh.project import Lifecycle, ProjectState, TaskNode

    state = ProjectState(project_id="p", repo_path="/x", nodes=[
        TaskNode(id="ready"),
        TaskNode(id="blocked", lifecycle=Lifecycle.BLOCKED),
        TaskNode(id="owned", lifecycle=Lifecycle.BLOCKED, note="the operator owns this"),
        TaskNode(id="done", lifecycle=Lifecycle.MERGED),
    ])
    assert [s.object_id for s in stuck_nodes(state)] == ["blocked"]
    timer = Timer(timer_id="t", due_at=LATER, kind=TimerKind.EXTERNAL_WAIT, node_id="blocked")
    assert stuck_nodes(state, timers=[timer]) == []
