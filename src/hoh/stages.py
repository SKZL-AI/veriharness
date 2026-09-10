"""State machine of the HoH loop.

Handoff §6:

    NEW -> PLANNING -> DEVELOPING -> VERIFYING -> CHECKPOINTED
              ^                                      |
              +-------------- REPLAN ----------------+
                                                     |
                                             READY_FOR_DELIVERY

`FREEZE` is explicitly **not** a stage of its own but a persisted boundary
before VERIFYING: the plan digest and the candidate binding are written into
the RunState before QA starts. A prompt that says "this is frozen now" would
be worthless.
"""

from __future__ import annotations

from .contracts import Candidate, Condition, RunState, Stage

#: Permitted forward and back edges. Anything else is a programming error.
ALLOWED: dict[Stage, frozenset[Stage]] = {
    Stage.NEW: frozenset({Stage.PLANNING}),
    Stage.PLANNING: frozenset({Stage.DEVELOPING}),
    Stage.DEVELOPING: frozenset({Stage.VERIFYING}),
    Stage.VERIFYING: frozenset({Stage.CHECKPOINTED, Stage.PLANNING}),
    Stage.CHECKPOINTED: frozenset({Stage.PLANNING, Stage.READY_FOR_DELIVERY}),
    Stage.READY_FOR_DELIVERY: frozenset(),
}

#: Conditions from which no further dispatch may happen.
HALTED: frozenset[Condition] = frozenset(
    {Condition.PAUSED, Condition.BLOCKED, Condition.FAILED, Condition.CANCELLED}
)


class TransitionError(RuntimeError):
    """A forbidden transition. Fail loud instead of quietly bending the state."""


class FreezeError(RuntimeError):
    """The candidate binding no longer matches what was frozen."""


def can_transition(current: Stage, target: Stage) -> bool:
    return target in ALLOWED[current]


def begin_iteration(state: RunState, *, reason: str = "") -> RunState:
    """Entry into a new planning round -- idempotent.

    The controller used to call `transition(state, PLANNING)` bluntly. After a
    rejection, however, the run was already in PLANNING (the REPLAN edge), and
    PLANNING -> PLANNING is forbidden: the loop was dead after the first
    rejection, which made the REPLAN branch from handoff §6 unreachable. On top
    of that the double transition would have counted the iteration twice.

    This function is the only permitted way to begin an iteration.
    """
    if state.condition in HALTED:
        raise TransitionError(
            f"start of iteration refused: run is {state.condition.value}"
            + (f" ({state.stop_reason})" if state.stop_reason else "")
        )
    if state.stage is Stage.PLANNING:
        # Already there -- replanning after a rejection: the REPLAN edge has
        # raised `state.iteration` but deliberately not the budget. **Here** is
        # where the iteration is charged, because here is where it begins.
        state.usage.iterations += 1
        state.note(f"iteration {state.iteration} continued in PLANNING"
                   + (f": {reason}" if reason else ""))
        return state

    if state.stage in (Stage.DEVELOPING, Stage.VERIFYING):
        # Conservative recovery. An abort in the developer step -- the most
        # likely case, since it is the LLM step -- used to wedge the run
        # permanently: DEVELOPING -> PLANNING is not a permitted edge, and
        # `unblock` did not help, because it only concerns the condition.
        # The attempt that was started is not booked as progress.
        previous = state.stage
        state.stage = Stage.PLANNING
        # `attempt` is deliberately NOT reset: in a recovery the iteration
        # number stays the same, and a reset counter produced the same receipt
        # id as the failed attempt -- the immutability boundary then blocked
        # the restart. This happened in the real a02 run.
        _thaw(state)
        state.note(
            f"RECOVERY {previous.value} -> PLANNING (aborted iteration "
            f"{state.iteration} is being replanned)"
            + (f": {reason}" if reason else "")
        )
        return state

    state = transition(state, Stage.PLANNING, reason=reason)
    state.usage.iterations += 1
    return state


def transition(state: RunState, target: Stage, *, reason: str = "") -> RunState:
    """Performs a transition, or refuses it traceably."""
    if state.condition in HALTED:
        raise TransitionError(
            f"transition {state.stage} -> {target} refused: run is {state.condition}"
            + (f" ({state.stop_reason})" if state.stop_reason else "")
        )
    if not can_transition(state.stage, target):
        allowed = ", ".join(sorted(s.value for s in ALLOWED[state.stage])) or "none"
        raise TransitionError(
            f"Forbidden transition {state.stage.value} -> {target.value}; allowed: {allowed}"
        )

    previous = state.stage
    state.stage = target
    state.note(f"stage {previous.value} -> {target.value}" + (f": {reason}" if reason else ""))

    # A new planning round raises the iteration number and releases the old
    # boundary. It does **not** charge the iteration budget.
    #
    # O32: it used to. The REPLAN edge after a rejection goes through here, so
    # the budget was charged at rejection time -- before the next iteration
    # existed. If the controller then stopped (loop count reached) or blocked
    # (budget), a slot had been paid for an iteration that never ran. Measured
    # on run `d2c`: `max_iterations=2`, three dispatches (one real iteration),
    # rejected once, and then `iteration budget exhausted (2/2)` without a
    # second iteration ever starting. A run with `max_iterations = N` could
    # complete only `N-1` iterations whenever one of them was rejected.
    #
    # The charge now sits in `begin_iteration`, which is the only permitted
    # way to begin an iteration -- so it is still counted exactly once, but at
    # the moment the work actually starts. The recovery path there charges
    # nothing, because it keeps the same iteration number.
    if target is Stage.PLANNING:
        state.iteration += 1
        state.attempt = 0
        _thaw(state)
    return state


def _thaw(state: RunState) -> None:
    state.frozen_plan_digest = None
    state.frozen_at = None
    state.frozen_binding = None


def freeze(state: RunState, *, plan_digest: str, candidate: Candidate) -> RunState:
    """The persisted boundary before VERIFYING.

    It writes down WHAT is being checked. After the freeze neither the plan nor
    the object under test may change; `assert_binding_intact` verifies that
    before and after QA (handoff §6).
    """
    if state.stage is not Stage.DEVELOPING:
        raise TransitionError(f"Freeze only out of DEVELOPING, not out of {state.stage.value}")
    state.frozen_plan_digest = plan_digest
    state.frozen_binding = candidate.binding()
    state.working_candidate = candidate
    from .contracts import utcnow

    state.frozen_at = utcnow()
    state.note(f"FREEZE plan={plan_digest} binding={state.frozen_binding}")
    return state


def assert_binding_intact(state: RunState, candidate: Candidate) -> None:
    """To be called before AND after QA.

    Catches the case where someone changed the sources between the freeze and
    the verdict -- A05 demands exactly this refusal.
    """
    if state.frozen_binding is None:
        raise FreezeError("No freeze active: there is nothing to check against")
    actual = candidate.binding()
    if actual != state.frozen_binding:
        raise FreezeError(
            "candidate binding violated: frozen was "
            f"'{state.frozen_binding}', found '{actual}'"
        )


def pause(state: RunState, reason: str) -> RunState:
    """Stops further dispatches at a documented safe boundary."""
    if state.condition is Condition.CANCELLED:
        raise TransitionError("A canceled run is not paused")
    state.condition = Condition.PAUSED
    state.stop_reason = reason
    state.note(f"PAUSED: {reason}")
    return state


def resume(state: RunState) -> RunState:
    if state.condition is not Condition.PAUSED:
        raise TransitionError(f"Resume only out of PAUSED, not out of {state.condition.value}")
    state.condition = Condition.ACTIVE
    state.stop_reason = None
    state.note("RESUMED")
    return state


def block(state: RunState, reason: str) -> RunState:
    """An unclear state or a real user decision -- an unambiguous waiting state
    instead of an endless replanning loop (handoff §8).

    An exhausted quota is **marked as such**. It is not a failure but a wait:
    `blocked_kind='usage_limit'` and `retry_after` make it machine-readable, so
    that `hoh resume-quota` can pick it up without guessing at free text.
    """
    from . import quota

    state.condition = Condition.BLOCKED
    state.blocked_reason = reason
    state.stop_reason = reason
    if quota.is_quota_exhausted(reason):
        state.blocked_kind = "usage_limit"
        state.retry_after = quota.next_attempt_at(reason).isoformat()
        state.note(
            f"BLOCKED (quota, earliest retry {state.retry_after}): "
            f"{reason[:200]}"
        )
        return state
    state.blocked_kind = None
    state.retry_after = None
    state.note(f"BLOCKED: {reason}")
    return state


def unblock(state: RunState, resolution: str) -> RunState:
    """Lifts a block after the operator has cleared up its cause.

    `BLOCKED` used to be a dead end: `resume` only permits PAUSED, and there
    was no other way out -- not even once the budget had been raised or the
    binding violation had been investigated. Handoff §8 wants an unambiguous
    waiting state, not a state without an exit; A09 demands conservative
    recovery.

    The resolution is recorded in the history, so that it stays traceable who
    lifted a block, and with what reason.
    """
    if state.condition is not Condition.BLOCKED:
        raise TransitionError(
            f"Unblock only out of BLOCKED, not out of {state.condition.value}"
        )
    if not resolution.strip():
        raise TransitionError("A block is only lifted with a reasoned resolution")
    state.condition = Condition.ACTIVE
    state.note(f"UNBLOCKED: {resolution} (previously: {state.blocked_reason})")
    state.blocked_reason = None
    state.stop_reason = None
    return state


def cancel(state: RunState, reason: str) -> RunState:
    state.condition = Condition.CANCELLED
    state.stop_reason = reason
    state.note(f"CANCELLED: {reason}")
    return state


def fail(state: RunState, reason: str) -> RunState:
    state.condition = Condition.FAILED
    state.stop_reason = reason
    state.note(f"FAILED: {reason}")
    return state


def accept_checkpoint(state: RunState, candidate: Candidate) -> RunState:
    """An accepted checkpoint is an internal HoH state first of all,
    not a delivery (handoff §6)."""
    if state.stage is not Stage.VERIFYING:
        raise TransitionError(
            f"Checkpoint only out of VERIFYING, not out of {state.stage.value}"
        )
    transition(state, Stage.CHECKPOINTED,
               reason=f"candidate {candidate.candidate_id} accepted")
    state.last_accepted_candidate = candidate
    state.usage.loops_without_progress = 0
    _thaw(state)
    return state


#: Prefix with which the controller marks a rejection that has **no check**
#: behind it -- QA that never ran, an exhausted quota, a crashed agent. See
#: `outage=` below.
#:
#: Producer and consumer both go through this constant, which is what makes
#: the wording safe to translate: `controller` builds the reason from it and
#: `reject_candidate` tests it with `startswith`. Written out as a literal in
#: a second place, it would become the kind of prose coupling that carried
#: real behaviour elsewhere in this project without a single test on it.
UNVERIFIED = "unverified, no QA verdict"


def reject_candidate(state: RunState, reason: str) -> RunState:
    """A rejected candidate goes into the next planning round with its finding,
    never into the delivery. `last_accepted_candidate` stays untouched
    (handoff §6)."""
    if state.stage is not Stage.VERIFYING:
        raise TransitionError(
            f"Rejection only out of VERIFYING, not out of {state.stage.value}"
        )
    # An outage is not a failed attempt. The progress counter measures whether
    # the roles are getting anywhere on the substance; if QA never answered
    # there is no information about that -- the candidate is unverified, not
    # failed. Counting it anyway would choke the run on a quota problem while
    # making it look as though the work had stalled on the substance. The same
    # separation as INFRA_EXIT_CODES in the runner: an infrastructure error is
    # never a result.
    if not reason.startswith(UNVERIFIED):
        state.usage.loops_without_progress += 1
    else:
        # O21: the iteration budget still counts this one, and rightly so --
        # the dispatches were paid for. Recording it separately is what lets
        # the exhaustion message say which kind of bound was reached.
        state.usage.outages += 1
    state.note(f"candidate rejected: {reason}")
    # The REPLAN edge. It raises the iteration -- which is why
    # begin_iteration() must not do so a second time.
    transition(state, Stage.PLANNING, reason="replanning after rejection")
    return state
