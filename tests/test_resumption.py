"""Resumption: does a due timer wake the right parked branch, and only that?

O211. Each rule of the decision layer is one way a resumption loop goes wrong,
so each has a case and a negative control that changes only the condition under
test and shows the verdict flip. The controls matter as much as the cases: a
refusal that also fires when its condition is absent is a relabelling, not a
rule.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh.resumption import (
    Resumption,
    ResumptionError,
    Verdict,
    capability_blocked,
    consume,
    due_resumptions,
    next_wakeup,
)
from hoh.timers import Timer, TimerKind, TimerStatus, TimerStore, TimerStoreError

EPOCH = 2
CONTROLLER = Path(__file__).resolve().parents[1] / "src" / "hoh" / "controller.py"
BEFORE = "2026-09-23T11:59:59Z"
NOW = "2026-09-23T12:00:00Z"
AFTER = "2026-09-23T12:00:01Z"
LATER = "2026-09-23T13:00:00Z"


def _timer(timer_id="t1", run_id="r1", due_at=NOW, *, epoch=EPOCH, generation=0,
           armed_at="2026-09-01T00:00:00Z", status=TimerStatus.ARMED,
           kind=TimerKind.REVIEW_DUE) -> Timer:
    return Timer(timer_id=timer_id, due_at=due_at, kind=kind, run_id=run_id,
                 program_epoch=epoch, generation=generation, armed_at=armed_at,
                 status=status)


def _parked(**extra) -> dict:
    return {"condition": "PARKED", "program_epoch": EPOCH, "retry_after": NOW, **extra}


def _contract_park(**extra) -> dict:
    """How a branch the contract parked looks: BLOCKED, kind parked_by_contract."""
    return {"condition": "BLOCKED", "program_epoch": EPOCH, "retry_after": NOW,
            "blocked_kind": "parked_by_contract",
            "blocked_reason": "review interval: contract parks the branch", **extra}


def _capability_block(**extra) -> dict:
    """How the P2-20 run looked: `controller._block` wrote a capability
    violation with no kind, and `block_under_contract` filled the kind with
    parked_by_contract and armed a retry."""
    return {"condition": "BLOCKED", "program_epoch": EPOCH, "retry_after": NOW,
            "blocked_kind": "parked_by_contract",
            "blocked_reason": "capability violation: write outside arena", **extra}


def _one(timers, runs, *, now=NOW, epoch=EPOCH, killed=()) -> Resumption:
    [r] = due_resumptions(timers, runs=runs, now=now, program_epoch=epoch, killed=killed)
    return r


# -- the full loop ------------------------------------------------------------

def test_full_loop_parked_run_is_not_due_then_resumes_and_is_consumed_once(tmp_path):
    """Catches a loop that never closes: park, arm, wait, due, resume, fire --
    and a crash-and-restart that fires the same timer a second time."""
    store = TimerStore(tmp_path / "runs")
    armed = store.arm_new(kind=TimerKind.EXTERNAL_WAIT, due_at=NOW, program_epoch=EPOCH,
                          run_id="r1", reason="parked by contract")
    runs = {"r1": _parked()}
    # A different store object: the decision reads durable state, not memory.
    early = _one(TimerStore(tmp_path / "runs").read(), runs, now=BEFORE)
    assert early.verdict is Verdict.NOT_DUE and early.why
    due = _one(TimerStore(tmp_path / "runs").read(), runs, now=NOW)
    assert due.verdict is Verdict.RESUME and due.timer_id == armed.timer_id
    fired = consume(store, due, now=NOW)
    assert fired.status is TimerStatus.FIRED and fired.fired_at
    with pytest.raises(ResumptionError):
        consume(store, due, now=AFTER)
    rows = store.read()
    assert [t.status for t in rows] == [TimerStatus.FIRED]
    assert rows[0].fired_at == fired.fired_at            # second consume changed nothing
    # And the fired timer is no longer resumable on the next tick.
    assert _one(rows, runs, now=AFTER).verdict is Verdict.SUPERSEDED_TIMER


def test_consume_refuses_a_refusal_and_leaves_the_store_untouched(tmp_path):
    """Catches `consume` acting on a decision that said not to resume."""
    store = TimerStore(tmp_path / "runs")
    t = store.arm_new(kind=TimerKind.EXTERNAL_WAIT, due_at=NOW, program_epoch=EPOCH, run_id="r1")
    refusal = _one(store.read(), {"r1": _parked()}, now=BEFORE)
    assert refusal.verdict is Verdict.NOT_DUE
    with pytest.raises(ResumptionError):
        consume(store, refusal, now=NOW)
    assert store.read()[0].status is TimerStatus.ARMED
    # Control: the same timer, decided when due, is consumed.
    assert consume(store, _one(store.read(), {"r1": _parked()}), now=NOW).timer_id == t.timer_id


def test_consume_refuses_a_timer_the_store_no_longer_holds_armed(tmp_path):
    """Catches a resume of a timer disarmed (e.g. by a terminal program)
    between the decision and the fire."""
    store = TimerStore(tmp_path / "runs")
    t = store.arm_new(kind=TimerKind.EXTERNAL_WAIT, due_at=NOW, program_epoch=EPOCH, run_id="r1")
    decision = _one(store.read(), {"r1": _parked()})
    assert decision.verdict is Verdict.RESUME
    store.disarm(t.timer_id, "program reached a terminal state")
    with pytest.raises(ResumptionError) as exc:
        consume(store, decision, now=NOW)
    assert isinstance(exc.value, TimerStoreError)
    assert store.read()[0].status is TimerStatus.DISARMED
    missing = Resumption(run_id="r1", timer_id="nope", verdict=Verdict.RESUME, why="",
                         due_at=NOW, program_epoch=EPOCH, timer_epoch=EPOCH)
    with pytest.raises(ResumptionError):
        consume(store, missing, now=NOW)


# -- rule 1: due boundary -----------------------------------------------------

def test_due_boundary_is_inclusive_at_the_second():
    """Catches an off-by-one: a run resuming a second early, or never on the
    boundary. due_at == now resumes; one second earlier is NOT_DUE."""
    assert _one([_timer(due_at=NOW)], {"r1": _parked()}, now=NOW).verdict is Verdict.RESUME
    assert _one([_timer(due_at=NOW)], {"r1": _parked()}, now=BEFORE).verdict is Verdict.NOT_DUE


def test_now_as_an_aware_datetime_decides_like_the_string_and_naive_is_refused():
    """Catches a local wall-clock time being read as UTC."""
    runs = {"r1": _parked()}
    at = datetime(2026, 9, 23, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))  # 12:00Z
    assert _one([_timer()], runs, now=at).verdict is Verdict.RESUME
    assert _one([_timer()], runs, now=at - timedelta(seconds=1)).verdict is Verdict.NOT_DUE
    with pytest.raises(ValueError):
        due_resumptions([_timer()], runs=runs, now=datetime(2026, 9, 23, 12),  # noqa: DTZ001
                         program_epoch=EPOCH)


def test_a_later_retry_after_on_the_run_keeps_it_not_due():
    """Catches a timer overriding the run's own later retry time (a quota that
    has not reset yet)."""
    assert _one([_timer()], {"r1": _parked(retry_after=LATER)}).verdict is Verdict.NOT_DUE
    assert _one([_timer()], {"r1": _parked(retry_after=NOW)}).verdict is Verdict.RESUME


# -- rule 2: stale epoch ------------------------------------------------------

def test_stale_epoch_timer_is_superseded_not_resumed():
    """Catches the plan's "stale epoch" failure: acting on a timer armed under
    a programme generation whose plan may no longer hold."""
    r = _one([_timer(epoch=EPOCH - 1)], {"r1": _parked()})
    assert r.verdict is Verdict.SUPERSEDED_EPOCH and r.timer_epoch == EPOCH - 1
    assert "superseded" in r.why


def test_stale_epoch_control_same_timer_at_current_epoch_resumes():
    """Control for the stale-epoch case: only the epoch changes, and it resumes."""
    assert _one([_timer(epoch=EPOCH)], {"r1": _parked()}).verdict is Verdict.RESUME


def test_stale_caller_a_timer_ahead_of_the_epoch_is_never_resumed():
    """Catches a stale caller acting on a newer generation's timer."""
    assert _one([_timer(epoch=EPOCH + 1)], {"r1": _parked()}).verdict is Verdict.SUPERSEDED_EPOCH


def test_stale_run_record_from_another_epoch_is_not_resumed():
    """Catches a current-epoch timer resuming a run the old plan owned."""
    assert (_one([_timer()], {"r1": _parked(program_epoch=EPOCH - 1)}).verdict
            is Verdict.SUPERSEDED_EPOCH)
    assert _one([_timer()], {"r1": _parked(program_epoch=EPOCH)}).verdict is Verdict.RESUME


# -- rule 3: superseded timer -------------------------------------------------

@pytest.mark.parametrize("order", ["old_first", "new_first"])
@pytest.mark.parametrize("ids", [("a", "z"), ("z", "a")])
def test_newer_of_two_timers_for_one_run_wins_whatever_order_and_ids(order, ids):
    """Catches a branch resumed twice, or the winner decided by list order or
    by id rather than by which timer is newer."""
    old_id, new_id = ids
    old = _timer(old_id, armed_at="2026-09-01T00:00:00Z")
    new = _timer(new_id, armed_at="2026-09-02T00:00:00Z")
    timers = [old, new] if order == "old_first" else [new, old]
    got = {r.timer_id: r.verdict for r in due_resumptions(
        timers, runs={"r1": _parked()}, now=NOW, program_epoch=EPOCH)}
    assert got == {new_id: Verdict.RESUME, old_id: Verdict.SUPERSEDED_TIMER}


def test_superseded_timer_control_a_single_timer_for_the_run_resumes():
    """Control: without the newer twin, the older timer is the live one."""
    assert _one([_timer("a", armed_at="2026-09-01T00:00:00Z")],
                {"r1": _parked()}).verdict is Verdict.RESUME


def test_generation_outranks_armed_at_when_deciding_the_newer_timer():
    """Catches newest decided by wall clock alone across a generation change."""
    g0 = _timer("a", generation=0, armed_at="2026-09-05T00:00:00Z")
    g1 = _timer("b", generation=1, armed_at="2026-09-01T00:00:00Z")
    got = {r.timer_id: r.verdict for r in due_resumptions(
        [g0, g1], runs={"r1": _parked()}, now=NOW, program_epoch=EPOCH)}
    assert got == {"b": Verdict.RESUME, "a": Verdict.SUPERSEDED_TIMER}


def test_a_timer_that_is_not_armed_is_superseded_and_does_not_outrank_a_live_one():
    """Catches a fired or disarmed timer being resumed again, or shadowing the
    armed timer that replaced it."""
    fired = _timer("a", armed_at="2026-09-09T00:00:00Z", status=TimerStatus.FIRED)
    live = _timer("b", armed_at="2026-09-01T00:00:00Z")
    got = {r.timer_id: r.verdict for r in due_resumptions(
        [fired, live], runs={"r1": _parked()}, now=NOW, program_epoch=EPOCH)}
    assert got == {"a": Verdict.SUPERSEDED_TIMER, "b": Verdict.RESUME}


# -- rule 4: run gone ---------------------------------------------------------

def test_gone_run_is_run_gone_never_resume():
    """Catches absence read as permission: a timer for a run nobody holds."""
    r = _one([_timer(run_id="ghost")], {"r1": _parked()})
    assert r.verdict is Verdict.RUN_GONE and "ghost" in r.why


def test_gone_control_the_same_timer_with_the_run_present_resumes():
    """Control for the gone case: only the run's presence changes."""
    assert _one([_timer(run_id="ghost")], {"ghost": _parked()}).verdict is Verdict.RESUME


def test_gone_a_timer_that_names_no_run_is_run_gone():
    """Catches a run-less timer (a heartbeat) being treated as a resume."""
    assert _one([_timer(run_id=None, kind=TimerKind.HEARTBEAT)], {}).verdict is Verdict.RUN_GONE


# -- rule 5: already running --------------------------------------------------

def test_active_run_is_already_running_not_resumed():
    """Catches duplicated accepted work: resuming a run that is running."""
    assert _one([_timer()], {"r1": _parked(condition="ACTIVE")}).verdict is Verdict.ALREADY_RUNNING


def test_already_running_control_parked_at_the_same_instant_resumes():
    """Control: the same timer, the same instant, the run PARKED."""
    assert _one([_timer()], {"r1": _parked(condition="PARKED")}).verdict is Verdict.RESUME


# -- rule 6: terminal ---------------------------------------------------------

@pytest.mark.parametrize("condition", ["ACCEPTED", "STOPPED", "CANCELLED", "FAILED"])
def test_terminal_run_is_run_not_parked(condition):
    """Catches a finished run woken by a timer it armed while alive."""
    assert _one([_timer()], {"r1": _parked(condition=condition)}).verdict is Verdict.RUN_NOT_PARKED


def test_terminal_control_parked_run_resumes():
    """Control for the terminal case: only the condition changes."""
    assert _one([_timer()], {"r1": _parked()}).verdict is Verdict.RESUME


def test_an_unknown_condition_is_not_resumed():
    """Catches fail-open on a condition this layer does not know (PAUSED)."""
    assert _one([_timer()], {"r1": _parked(condition="PAUSED")}).verdict is Verdict.RUN_NOT_PARKED


# -- rule 7: killed -----------------------------------------------------------

def test_killed_run_is_killed_even_with_a_due_timer_at_the_right_epoch():
    """Catches a kill switch the resumption loop does not honour."""
    r = _one([_timer()], {"r1": _parked()}, killed={"r1"})
    assert r.verdict is Verdict.KILLED and r.why


def test_killed_control_the_same_run_not_on_the_kill_list_resumes():
    """Control for the killed case: only the kill list changes."""
    assert _one([_timer()], {"r1": _parked()}, killed={"other"}).verdict is Verdict.RESUME


def test_killed_outranks_every_other_verdict():
    """Catches a killed run reported as merely stale or not due, which a later
    epoch bump or a later tick would turn into a resume."""
    assert _one([_timer(epoch=EPOCH - 1)], {"r1": _parked()}, killed=["r1"]).verdict is Verdict.KILLED
    assert _one([_timer(due_at=LATER)], {"r1": _parked()}, killed=["r1"]).verdict is Verdict.KILLED


# -- rule 8: capability block needs an override -------------------------------

def test_override_needed_for_capability_block_while_contract_park_resumes():
    """Catches the P2-20 busy loop: a capability-blocked run fired on every
    tick. Same timer, same instant; only the block's cause differs, and the
    verdict flips -- so the distinction is real, not a relabelling."""
    timer = _timer()
    assert _one([timer], {"r1": _capability_block()}).verdict is Verdict.NEEDS_OVERRIDE
    assert _one([timer], {"r1": _contract_park()}).verdict is Verdict.RESUME


def test_override_both_runs_in_one_call_decide_independently():
    """Catches one run's block leaking into another run's decision."""
    got = {r.run_id: r.verdict for r in due_resumptions(
        [_timer("a", "cap"), _timer("b", "par")],
        runs={"cap": _capability_block(), "par": _contract_park()}, now=NOW, program_epoch=EPOCH)}
    assert got == {"cap": Verdict.NEEDS_OVERRIDE, "par": Verdict.RESUME}


def test_override_needed_when_blocked_kind_names_the_capability_violation():
    """Catches detection that reads only the reason text."""
    run = _contract_park(blocked_kind="capability_violation", blocked_reason="")
    assert capability_blocked(run)
    assert _one([_timer()], {"r1": run}).verdict is Verdict.NEEDS_OVERRIDE


def test_override_needed_for_a_blocked_run_waiting_on_a_human():
    """Catches a human-owned block resumed by a timer; control: a usage-limit
    block, which is waiting for a time, resumes."""
    human = _contract_park(blocked_kind="human", blocked_reason="needs a decision")
    assert _one([_timer()], {"r1": human}).verdict is Verdict.NEEDS_OVERRIDE
    quota = _contract_park(blocked_kind="usage_limit", blocked_reason="usage limit reached")
    assert _one([_timer()], {"r1": quota}).verdict is Verdict.RESUME


def test_override_needed_before_not_due_so_the_refusal_is_the_real_reason():
    """Catches a capability block reported as NOT_DUE, which turns into a
    fruitless resume the moment the timer comes due."""
    assert _one([_timer(due_at=LATER)], {"r1": _capability_block()}).verdict is Verdict.NEEDS_OVERRIDE


# -- every timer gets a decision ----------------------------------------------

def test_a_decision_is_returned_for_every_timer_including_refusals_in_order():
    """Catches a caller that cannot see what was decided not to be done."""
    timers = [_timer("e", "r1", AFTER, armed_at="2026-09-02T00:00:00Z"),
              _timer("d", "ghost", NOW),
              _timer("c", "r1", NOW, epoch=EPOCH - 1),
              _timer("k", "rk", BEFORE)]
    out = due_resumptions(timers, runs={"r1": _parked(), "rk": _parked()}, now=NOW,
                          program_epoch=EPOCH, killed={"rk"})
    assert [r.timer_id for r in out] == ["k", "c", "d", "e"]         # due first, then id
    assert {r.timer_id: r.verdict for r in out} == {
        "k": Verdict.KILLED, "c": Verdict.SUPERSEDED_EPOCH,
        "d": Verdict.RUN_GONE, "e": Verdict.NOT_DUE}
    assert all(r.why.strip() for r in out)
    assert all(r.program_epoch == EPOCH for r in out)


def test_no_timers_gives_no_decisions():
    """Control for the every-timer case: the list tracks the input exactly."""
    assert due_resumptions([], runs={"r1": _parked()}, now=NOW, program_epoch=EPOCH) == []


# -- Resumption construction --------------------------------------------------

def test_a_refusal_without_a_reason_is_refused_at_construction():
    """Catches an unreadable refusal, indistinguishable from a bug."""
    for why in ("", "   "):
        with pytest.raises(ValueError):
            Resumption(run_id="r", timer_id="t", verdict=Verdict.KILLED, why=why,
                       due_at=NOW, program_epoch=EPOCH, timer_epoch=EPOCH)


def test_a_refusal_with_a_reason_and_a_resume_without_one_are_accepted():
    """Control: the check is on refusals with no reason, nothing else."""
    Resumption(run_id="r", timer_id="t", verdict=Verdict.KILLED, why="on the kill list",
               due_at=NOW, program_epoch=EPOCH, timer_epoch=EPOCH)
    Resumption(run_id="r", timer_id="t", verdict=Verdict.RESUME, why="",
               due_at=NOW, program_epoch=EPOCH, timer_epoch=EPOCH)


def test_verdict_members_are_exactly_the_specified_ones_in_order():
    """Catches collapsed or reordered verdicts."""
    assert [v.value for v in Verdict] == [
        "RESUME", "NOT_DUE", "SUPERSEDED_EPOCH", "SUPERSEDED_TIMER", "RUN_GONE",
        "RUN_NOT_PARKED", "ALREADY_RUNNING", "KILLED", "NEEDS_OVERRIDE"]


# -- next_wakeup --------------------------------------------------------------

def test_next_wakeup_returns_the_earliest_of_several_future_timers():
    """Catches a supervisor sleeping past the first due timer."""
    w = next_wakeup([_timer("f", "r3", LATER), _timer("g", "r4", "2026-09-23T12:30:00Z")],
                    now=NOW, program_epoch=EPOCH)
    assert w == datetime(2026, 9, 23, 12, 30, tzinfo=UTC)
    assert w.tzinfo is not None


def test_next_wakeup_ignores_a_stale_epoch_timer():
    """Catches a superseded timer keeping a supervisor awake; control: the same
    timer at the current epoch does set the wake-up."""
    assert next_wakeup([_timer(due_at=LATER, epoch=EPOCH - 1)], now=NOW, program_epoch=EPOCH) is None
    assert next_wakeup([_timer(due_at=LATER)], now=NOW,
                       program_epoch=EPOCH) == datetime(2026, 9, 23, 13, tzinfo=UTC)


def test_next_wakeup_ignores_an_older_timer_superseded_for_its_run():
    """Catches an older twin waking the supervisor before the live timer."""
    old = _timer("a", due_at="2026-09-23T12:10:00Z", armed_at="2026-09-01T00:00:00Z")
    new = _timer("b", due_at=LATER, armed_at="2026-09-02T00:00:00Z")
    assert next_wakeup([old, new], now=NOW, program_epoch=EPOCH) == datetime(2026, 9, 23, 13, tzinfo=UTC)


def test_next_wakeup_is_none_when_nothing_is_live():
    """Catches a supervisor polling for fired, disarmed or already-due timers."""
    assert next_wakeup([], now=NOW, program_epoch=EPOCH) is None
    assert next_wakeup([_timer(due_at=LATER, status=TimerStatus.FIRED),
                        _timer("b", "r2", LATER, status=TimerStatus.DISARMED),
                        _timer("c", "r3", NOW)], now=NOW, program_epoch=EPOCH) is None


# -- coupling to the controller's wording -------------------------------------

def test_override_wording_the_controller_capability_reason_is_still_detected():
    """Catches the controller rewording its capability-violation block reason
    and the detector silently falling back to a contract-park RESUME -- the
    P2-20 busy loop again. Reads controller.py as text (it is not imported)
    and pins the exact prefix `controller._block` writes; control: a
    contract-park reason at the same instant, with the same timer, resumes."""
    source = CONTROLLER.read_text(encoding="utf-8")
    written = re.search(r'except CapabilityViolation as exc:.*?self\._block\(\s*state,\s*'
                        r'f"(?P<prefix>[^"{]*)\{exc\}"\)', source, re.DOTALL)
    assert written, "controller.py no longer blocks a CapabilityViolation the way this detector reads"
    prefix = written.group("prefix")
    assert prefix == "capability violation: ", prefix
    timer = _timer()
    reason = f"{prefix}planner wrote into the arena"
    assert (_one([timer], {"r1": _capability_block(blocked_reason=reason)}).verdict
            is Verdict.NEEDS_OVERRIDE)
    park = "autonomy: parked by contract, review interval 3600s"
    assert (_one([timer], {"r1": _capability_block(blocked_reason=park)}).verdict
            is Verdict.RESUME)


def test_override_detection_agrees_with_the_project_classifier():
    """Catches a private matcher drifting from `hoh.taxonomy.classify_failure`,
    e.g. over case, which would give the project two opinions on one block."""
    from hoh.taxonomy import FailureClass, classify_failure

    for reason in ("capability violation: x", "Capability Violation: witness saw a write",
                   "capability violation by planner: y", "usage limit reached",
                   "autonomy: parked by contract", ""):
        want = (Verdict.NEEDS_OVERRIDE
                if classify_failure(None, detail=reason) is FailureClass.CAPABILITY_VIOLATION
                else Verdict.RESUME)
        got = _one([_timer()], {"r1": _contract_park(blocked_reason=reason)}).verdict
        assert got is want, (reason, got, want)


# -- the supervisor contract --------------------------------------------------

def test_already_due_timer_is_work_now_not_a_wakeup_time():
    """Catches a supervisor that sleeps on next_wakeup() == None and so never
    resumes a timer that is already due. The order is: call due_resumptions
    first and act on its RESUMEs, and only then sleep until next_wakeup; a
    supervisor must call due_resumptions before sleeping on a None result.
    Control: the same timer one second in the future is a wake-up time and is
    NOT_DUE."""
    timer = _timer(due_at=NOW)
    assert next_wakeup([timer], now=NOW, program_epoch=EPOCH) is None
    assert _one([timer], {"r1": _parked()}).verdict is Verdict.RESUME
    future = _timer(due_at=AFTER)
    assert next_wakeup([future], now=NOW, program_epoch=EPOCH) == datetime(
        2026, 9, 23, 12, 0, 1, tzinfo=UTC)
    assert _one([future], {"r1": _parked()}).verdict is Verdict.NOT_DUE
