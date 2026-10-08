"""Program logic against activities (V3.3 P2-15): the reducer decides, the
activity acts.

One case per rule of the spec, each next to a negative control that changes
only the condition under test and shows the verdict flipping. Without the
controls, a reducer that raised on everything would pass every fail-closed
case, and a purity checker that never reported anything would pass the
"this module is pure" case -- a check that holds whichever way the condition
goes is a check of nothing.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from hoh import programlogic
from hoh.programlogic import (
    Activity,
    ActivityStatus,
    Event,
    EventKind,
    MaterializedState,
    purity_problems,
    reduce,
    run_activity,
)

T0 = "2026-09-24T00:00:00Z"
T1 = "2031-01-01T12:34:56Z"


def _ev(seq, kind, activity_id="a1", *, at=T0, **payload) -> Event:
    return Event(event_id=f"e{seq}", sequence=seq, kind=kind,
                 activity_id=activity_id, payload=payload, recorded_at=at)


def _history(at=T0) -> list[Event]:
    """Two activities: k1 fails once and is retried to success, k2 runs."""
    return [
        _ev(1, EventKind.SCHEDULED, "a1", at=at, name="fetch", idempotency_key="k1"),
        _ev(2, EventKind.STARTED, "a1", at=at),
        _ev(3, EventKind.FAILED, "a1", at=at, failure="TimeoutError: slow"),
        _ev(4, EventKind.SCHEDULED, "a2", at=at, name="build", idempotency_key="k2"),
        _ev(5, EventKind.STARTED, "a1", at=at),
        _ev(6, EventKind.SUCCEEDED, "a1", at=at, result_digest="d" * 64),
        _ev(7, EventKind.STARTED, "a2", at=at),
    ]


def _activity(key="k1", **kw) -> Activity:
    fields = dict(activity_id="a1", name="fetch", idempotency_key=key)
    fields.update(kw)
    return Activity(**fields)


class _Counter:
    def __init__(self, result=None, exc=None):
        self.calls, self.result, self.exc = 0, result, exc

    def __call__(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


# --------------------------------------------------------------------------- #
# The data model
# --------------------------------------------------------------------------- #

def test_activity_status_has_exactly_the_five_members_in_order():
    """Catches an added, dropped or reordered status, which would silently
    change what a replayed history means."""
    assert [s.value for s in ActivityStatus] == [
        "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "ABANDONED"]
    assert [s.name for s in ActivityStatus] == [s.value for s in ActivityStatus]


@pytest.mark.parametrize("key", ["", "   "])
def test_activity_with_empty_idempotency_key_is_refused(key):
    """Catches an activity without identity: its retry would be a second
    effect rather than a retry."""
    with pytest.raises(ValidationError):
        _activity(key=key)


def test_control_activity_with_nonempty_key_is_accepted():
    """Control: only the key changes, and construction succeeds -- the refusal
    above is about the key, not about Activity refusing everything."""
    assert _activity(key="k1").idempotency_key == "k1"


def test_materialized_state_is_frozen():
    """Catches a state that can be edited after the fold, which would let a
    decision differ from what the events say."""
    state = reduce(_history())
    with pytest.raises(ValidationError):
        state.sequence = 99


# --------------------------------------------------------------------------- #
# Replay is identical; the reducer is pure
# --------------------------------------------------------------------------- #

def test_replay_twice_gives_equal_state_digest():
    """Catches a reducer whose output depends on anything but the events."""
    first, second = reduce(_history()), reduce(_history())
    assert first == second
    assert first.state_digest == second.state_digest


def test_control_replay_with_one_changed_payload_changes_digest():
    """Control: only one payload value differs, and the digest differs -- a
    digest that ignored the content would make the equality above empty."""
    changed = _history()
    changed[5] = _ev(6, EventKind.SUCCEEDED, "a1", result_digest="e" * 64)
    assert reduce(changed).state_digest != reduce(_history()).state_digest


def test_replay_at_different_simulated_time_gives_identical_state():
    """Catches a reducer that reads recorded_at (or a clock): the same events
    replayed at another time would fold to a different state."""
    then, later = reduce(_history(at=T0)), reduce(_history(at=T1))
    assert then == later
    assert then.state_digest == later.state_digest
    assert all(a.started_at is None and a.ended_at is None
               for a in later.activities.values())


def test_replay_is_deterministic_under_input_order():
    """Catches a reducer that folds in list order instead of sequence order:
    a log read back in another order would reconstruct another history."""
    events = _history()
    assert reduce(list(reversed(events))) == reduce(events)


def test_control_replay_with_changed_recorded_at_still_changes_the_events():
    """Control: the simulated-time case changes the events themselves (their
    recorded_at differs), so its equality is not two identical inputs."""
    assert [e.recorded_at for e in _history(at=T0)] != \
        [e.recorded_at for e in _history(at=T1)]
    assert _history(at=T0) != _history(at=T1)


# --------------------------------------------------------------------------- #
# Sequence integrity
# --------------------------------------------------------------------------- #

def test_gap_in_sequence_raises():
    """Catches a reducer that skips a missing event and reconstructs a history
    that did not happen."""
    events = [e for e in _history() if e.sequence != 3]
    with pytest.raises(ValueError, match="gap"):
        reduce(events)


def test_control_complete_sequence_does_not_raise():
    """Control: the same events with nothing missing fold cleanly."""
    assert reduce(_history()).sequence == 7


def test_duplicate_sequence_raises():
    """Catches a reducer that folds two events claiming the same place in the
    log, or silently keeps one of them."""
    events = _history() + [_ev(7, EventKind.SUCCEEDED, "a2", result_digest="x")]
    with pytest.raises(ValueError, match="duplicate"):
        reduce(events)


@pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6, 7])
def test_upto_k_equals_reducing_the_first_k_events(k):
    """Catches an `upto` that folds too much or too little, making an earlier
    state unreconstructable."""
    events = _history()
    assert reduce(events, upto=k) == reduce(events[:k])


def test_upto_last_equals_full_reduce():
    """Catches an `upto` whose boundary is off by one at the end."""
    events = _history()
    assert reduce(events, upto=events[-1].sequence) == reduce(events)


def test_control_upto_earlier_differs_from_full_reduce():
    """Control: a prefix that stops earlier is a different state, so the
    equalities above are not `upto` being ignored."""
    events = _history()
    assert reduce(events, upto=5) != reduce(events)


# --------------------------------------------------------------------------- #
# Idempotency and open activities
# --------------------------------------------------------------------------- #

def _two_activities(key_a, key_b) -> list[Event]:
    return [
        _ev(1, EventKind.STARTED, "a1", name="fetch", idempotency_key=key_a),
        _ev(2, EventKind.FAILED, "a1", failure="boom"),
        _ev(3, EventKind.STARTED, "a2", name="fetch", idempotency_key=key_b),
        _ev(4, EventKind.SUCCEEDED, "a2", result_digest="r" * 64),
    ]


def test_shared_idempotency_key_folds_to_one_entry_with_accumulated_attempts():
    """Catches a retry counted as a second effect: two activities with one key
    must be one entry, the later one winning."""
    state = reduce(_two_activities("k", "k"))
    assert list(state.activities) == ["a2"]
    entry = state.activities["a2"]
    assert entry.attempts == 2
    assert entry.status == ActivityStatus.SUCCEEDED
    assert state.completed == ("k",) and state.failed == ()


def test_control_different_keys_stay_two_entries():
    """Control: only the second key changes, and the entries stay apart."""
    state = reduce(_two_activities("k", "other"))
    assert sorted(state.activities) == ["a1", "a2"]
    assert state.activities["a1"].attempts == 1
    assert state.activities["a2"].attempts == 1
    assert state.failed == ("k",) and state.completed == ("other",)


def test_activity_without_completion_is_open_and_not_succeeded():
    """Catches completion inferred from silence."""
    state = reduce(_history(), upto=5)
    assert "k1" in state.open_keys
    assert state.activities["a1"].status == ActivityStatus.RUNNING
    assert "k1" not in state.completed
    scheduled_only = reduce(_history(), upto=4)
    assert scheduled_only.activities["a2"].status == ActivityStatus.PENDING
    assert "k2" in scheduled_only.open_keys


def test_control_activity_after_completion_event_is_not_open():
    """Control: the same activity one event later, with its SUCCEEDED event,
    has left open_keys."""
    state = reduce(_history(), upto=6)
    assert "k1" not in state.open_keys
    assert state.activities["a1"].status == ActivityStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# The activity runner
# --------------------------------------------------------------------------- #

def test_run_activity_with_seen_key_does_not_call_perform():
    """Catches a retried activity performed a second time."""
    perform = _Counter(result="x")
    done, events = run_activity(_activity(), perform=perform, now=T0,
                                seen_keys={"k1"})
    assert perform.calls == 0
    assert [e.kind for e in events] == [EventKind.SKIPPED]
    assert events[0].payload["reason"] == "already done"
    assert done.status == ActivityStatus.SUCCEEDED


def test_control_run_activity_with_unseen_key_calls_perform_once():
    """Control: only the seen keys change, and perform runs exactly once."""
    perform = _Counter(result="x")
    done, events = run_activity(_activity(), perform=perform, now=T0,
                                seen_keys={"other"})
    assert perform.calls == 1
    assert [e.kind for e in events] == [EventKind.STARTED, EventKind.SUCCEEDED]
    assert done.status == ActivityStatus.SUCCEEDED and done.attempts == 1


def test_run_activity_raising_perform_is_failed_without_exception():
    """Catches an activity crash propagating into the programme."""
    perform = _Counter(exc=RuntimeError("provider down"))
    failed, events = run_activity(_activity(max_attempts=3), perform=perform, now=T0)
    assert perform.calls == 1
    assert failed.status == ActivityStatus.FAILED
    assert failed.attempts == 1
    assert "provider down" in failed.failure
    assert events[-1].kind == EventKind.FAILED


def test_run_activity_third_failure_at_max_attempts_is_abandoned():
    """Catches an activity retried forever: the failure that reaches
    max_attempts must abandon it, and the events must fold to the same."""
    perform = _Counter(exc=RuntimeError("provider down"))
    act, log, statuses = _activity(max_attempts=3), [], []
    for _ in range(3):
        act, events = run_activity(act, perform=perform, now=T0,
                                   first_sequence=len(log) + 1)
        log.extend(events)
        statuses.append(act.status)
    assert statuses == [ActivityStatus.FAILED, ActivityStatus.FAILED,
                        ActivityStatus.ABANDONED]
    assert perform.calls == 3 and act.attempts == 3
    state = reduce(log)
    assert state.activities["a1"].status == ActivityStatus.ABANDONED
    assert state.activities["a1"].attempts == 3
    assert state.failed == ("k1",) and state.open_keys == ()
    with pytest.raises(ValueError):
        run_activity(act, perform=perform, now=T0)
    assert perform.calls == 3


def test_control_second_failure_is_not_abandoned():
    """Control: one failure fewer, and the activity is still retryable."""
    perform = _Counter(exc=RuntimeError("provider down"))
    act = _activity(max_attempts=3)
    for _ in range(2):
        act, _events = run_activity(act, perform=perform, now=T0)
    assert act.status == ActivityStatus.FAILED
    assert act.attempts == 2


def test_run_activity_result_is_digested_not_stored():
    """Catches a provider's raw output landing in programme state."""
    secret = "RAW-PROVIDER-OUTPUT-4711"
    done, events = run_activity(_activity(), perform=lambda: {"text": secret},
                                now=T0)
    assert done.result_digest == programlogic.result_digest({"text": secret})
    assert len(done.result_digest) == 64
    state = reduce(events)
    assert state.activities["a1"].result_digest == done.result_digest
    assert secret not in state.model_dump_json()
    assert secret not in "".join(e.model_dump_json() for e in events)
    assert secret not in done.model_dump_json()


def test_control_different_result_gives_different_digest():
    """Control: only the returned value changes, and the digest does too --
    a constant digest would bind nothing."""
    a, _ = run_activity(_activity(), perform=lambda: {"text": "one"}, now=T0)
    b, _ = run_activity(_activity(), perform=lambda: {"text": "two"}, now=T0)
    assert a.result_digest != b.result_digest


def test_run_activity_takes_timestamps_only_from_injected_now():
    """Catches an activity reading the wall clock instead of the injected one."""
    ticks = iter(["t1", "t2"])
    done, events = run_activity(_activity(), perform=lambda: 1,
                                now=lambda: next(ticks))
    assert [e.recorded_at for e in events] == ["t1", "t2"]
    assert (done.started_at, done.ended_at) == ("t1", "t2")


def test_replay_of_run_activity_events_at_different_time_is_identical():
    """Catches activity events whose fold depends on when they were recorded."""
    _, early = run_activity(_activity(), perform=lambda: 1, now=T0)
    _, late = run_activity(_activity(), perform=lambda: 1, now=T1)
    assert reduce(early) == reduce(late)


# --------------------------------------------------------------------------- #
# The purity check
# --------------------------------------------------------------------------- #

def _impure_reduce(events):
    return _impure_helper(events)


def _impure_helper(events):
    return (datetime.now(), events)


def _pure_reduce(events):
    return _pure_helper(events)


def _pure_helper(events):
    return sorted(events)


def test_pure_reducer_has_no_purity_problems_in_programlogic():
    """Catches a clock, randomness, I/O or a perform call creeping into
    reduce or one of its helpers."""
    assert purity_problems() == []
    assert purity_problems(programlogic) == []


def test_impure_helper_calling_datetime_now_is_reported():
    """Catches a purity checker that cannot fail: an impure helper reached
    only transitively from the entry must be reported by name."""
    problems = purity_problems(_impure_reduce)
    assert problems
    assert any("_impure_helper" in p and "datetime.now" in p for p in problems)


def test_control_purity_check_is_clean_for_pure_twin():
    """Control: the same shape without the clock call is clean, so the finding
    above is about datetime.now, not about the test module."""
    assert purity_problems(_pure_reduce) == []


@pytest.mark.parametrize("body, needle", [
    ("import time\n    return time.time()", "time"),
    ("return random.random()", "random"),
    ("return open('f').read()", "open"),
    ("return os.environ['HOME']", "os.environ"),
    ("return subprocess.run(['x'])", "subprocess"),
    ("return requests.get('u')", "requests"),
    ("return urllib.request.urlopen('u')", "urllib"),
    ("return perform()", "perform"),
    ("return sorted(events, key=lambda e: e.recorded_at)", "recorded_at"),
])
def test_impure_use_of_each_forbidden_name_is_reported(body, needle):
    """Catches a checker blind to one of the names the spec forbids."""
    source = f"def reduce(events):\n    return _h(events)\n\ndef _h(events):\n    {body}\n"
    problems = purity_problems(source)
    assert any(needle in p for p in problems), problems


def test_control_pure_source_has_no_findings():
    """Control: the same source shape with a harmless body is clean."""
    source = "def reduce(events):\n    return _h(events)\n\ndef _h(events):\n    return len(events)\n"
    assert purity_problems(source) == []


def test_impure_edit_of_programlogic_reduce_is_reported():
    """Catches the exact regression the rule exists for: this module's reduce
    edited to consult the clock must stop passing the check."""
    import inspect
    source = inspect.getsource(programlogic).replace(
        "    ordered = _ordered(events)\n",
        "    ordered = _ordered(events)\n    _stamp = datetime.now()\n", 1)
    problems = purity_problems(source)
    assert any("reduce" in p and "datetime.now" in p for p in problems), problems


def test_materialized_state_is_the_reduce_return_type():
    """Catches reduce returning a loose mapping rather than the frozen state."""
    assert isinstance(reduce([]), MaterializedState)
    assert reduce([]).sequence == 0 and reduce([]).activities == {}
