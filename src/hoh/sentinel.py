"""The sentinel: what is due, and what should happen about it.

V3.3 P2-05, and the never-stuck invariant of P2-06.

The sentinel is the replaceable half of a durable wait. `hoh.timers` writes
down that something should happen later; the sentinel is what looks, later,
and says what. It is deliberately a **function of state, not a daemon**: it
takes a store and a moment and returns proposals. A cron, a plugin, the next
session or a test can be the thing that calls it, which is what "the wake-up
process is replaceable" means -- and a proposal that nobody executed is still
visible in the store as a timer that is still armed.

It never dispatches, never takes a lock and never sleeps. Those belong to the
controller, and a sentinel that did them would be a second controller with no
lock discipline of its own.

## Never-stuck

The plan's invariant, stated as a prohibition:

    Every non-terminal object must have
        next_action OR retry_at OR explicit_human_owner
    A state with none of these is a control-plane defect.

`stuck()` is that sentence as a measurement over the objects this project
actually has: runs, task nodes and timers. It is not a style check. Each
finding names the object and which of the three ways out is missing, because
"the program is stuck" is not actionable and "run d7 is BLOCKED with no
retry_at and no owner" is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import utcnow
from .timers import Timer, TimerKind, TimerStatus, TimerStore


@dataclass(frozen=True)
class Proposal:
    """One due timer and what the sentinel says should happen about it.

    `action` is a name, not a call. The sentinel proposes; the controller
    decides whether the program may act, which is where the lock and the
    policy live.
    """

    timer: Timer
    action: str
    why: str


#: What each kind asks for when it comes due. An unknown kind has no entry and
#: is reported as a defect rather than given a default action: a timer nobody
#: can act on is a wait that never ends, and it should be loud.
ACTIONS = {
    TimerKind.PROVIDER_RETRY: ("probe_provider_and_resume",
                               "the provider said to come back; probe it once and resume "
                               "the run exactly once if it answers"),
    TimerKind.REVIEW_DUE: ("request_review",
                           "a review the policy requires again is due"),
    TimerKind.EXTERNAL_WAIT: ("recheck_external",
                              "an external condition the program parked on should be "
                              "looked at again"),
    TimerKind.HEARTBEAT: ("reconcile",
                          "nothing is known to be wrong; reconcile the program so that "
                          "a stuck one cannot survive by being unobserved"),
    TimerKind.STORAGE_RECHECK: ("recheck_storage_and_resume",
                                "a run parked on storage pressure; `hoh resume-storage` "
                                "observes free space again and resumes the run only if a "
                                "role run would be admitted now"),
}


@dataclass
class Sweep:
    """What one look at the store found."""

    at: str
    due: list[Proposal] = field(default_factory=list)
    refused: list[tuple[Timer, str]] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.due and not self.refused and not self.defects


def sweep(store: TimerStore, *, now: str | None = None, program_epoch: int | None = None,
          session_epoch: int | None = None) -> Sweep:
    """Everything due at `now`, as proposals, plus what was refused and why."""
    moment = now or utcnow()
    ready, refused = store.due(now=moment, program_epoch=program_epoch,
                               session_epoch=session_epoch)
    out = Sweep(at=moment, refused=refused)
    for timer in ready:
        known = ACTIONS.get(timer.kind)
        if known is None:                       # pragma: no cover - guarded by the enum
            out.defects.append(f"timer {timer.timer_id}: no action for kind {timer.kind}")
            continue
        action, why = known
        out.due.append(Proposal(timer=timer, action=action, why=why))
    return out


# --- never-stuck ---------------------------------------------------------------

#: The run conditions that are an end. Anything else is non-terminal, and a
#: non-terminal object owes one of the three ways out.
TERMINAL_CONDITIONS = {"DONE", "FAILED", "CANCELLED", "SUPERSEDED", "ABANDONED"}


def _name(value) -> str:
    """The name of an enum member, or the string itself.

    Both shapes reach here: the controller passes `RunState` with enum fields,
    a test or a resumed session passes what it read out of JSON. Reading only
    `.value` made a plain string look like an empty one, which turned an
    active run into a defect and a blocked node into a healthy one -- caught
    by the first two cases of `tests/test_timers.py`.
    """
    return str(getattr(value, "value", value) or "")


def _ways_out(*, next_action: str | None, retry_at: str | None,
              human_owner: str | None) -> list[str]:
    return [name for name, value in (("next_action", next_action), ("retry_at", retry_at),
                                     ("explicit_human_owner", human_owner)) if value]


@dataclass(frozen=True)
class Stuck:
    """One object with no way out, named so it can be repaired."""

    what: str
    object_id: str
    state: str
    detail: str = ""

    def __str__(self) -> str:
        return (f"{self.what} {self.object_id} is {self.state} with no next_action, "
                f"no retry_at and no human owner"
                + (f" ({self.detail})" if self.detail else ""))


def stuck_run(run_state, *, timers: list[Timer] | None = None) -> list[Stuck]:
    """Is this run waiting for something that will happen?

    A run's `retry_after` counts, and so does an armed timer that names it:
    both are a written-down promise that something will look again. A blocked
    run with neither is the defect the plan calls a control-plane defect.
    """
    condition = _name(getattr(run_state, "condition", ""))
    if condition.upper() in TERMINAL_CONDITIONS:
        return []
    armed = [t for t in (timers or [])
             if t.status is TimerStatus.ARMED and t.run_id == getattr(run_state, "run_id", None)]
    ways = _ways_out(
        next_action=("timer " + armed[0].timer_id) if armed else None,
        retry_at=getattr(run_state, "retry_after", None),
        human_owner=getattr(run_state, "blocked_reason", None)
        if getattr(run_state, "blocked_kind", None) == "human" else None)
    if ways:
        return []
    stage = _name(getattr(run_state, "stage", None))
    blocked = getattr(run_state, "blocked_kind", None)
    if not blocked and stage and condition.upper() == "ACTIVE":
        return []            # an active run's next action is the loop itself
    return [Stuck(what="run", object_id=str(getattr(run_state, "run_id", "?")),
                  state=f"{condition}/{stage}",
                  detail=f"blocked_kind={blocked!r}" if blocked else "")]


def stuck_nodes(project_state, *, timers: list[Timer] | None = None) -> list[Stuck]:
    """Task nodes that are neither settled nor waiting for anything.

    A BLOCKED node with no note naming who owns it and no timer is the same
    defect one level down: the program has work it will never pick up, and
    nothing says so.
    """
    out, armed = [], {t.node_id for t in (timers or []) if t.status is TimerStatus.ARMED}
    for node in getattr(project_state, "nodes", []) or []:
        if getattr(node, "settled", False):
            continue
        lifecycle = _name(getattr(node, "lifecycle", None))
        if lifecycle.upper() != "BLOCKED":
            continue            # READY and RUNNING have the frontier as their next action
        ways = _ways_out(next_action=("timer" if node.id in armed else None),
                         retry_at=None, human_owner=(node.note or None))
        if not ways:
            out.append(Stuck(what="node", object_id=node.id, state=lifecycle,
                             detail="blocked with no owner, no timer and no note"))
    return out


def stuck_timers(timers: list[Timer], *, now: str | None = None,
                 late_after_seconds: int = 24 * 3600) -> list[Stuck]:
    """Timers that came due and were never acted on.

    This is the sentinel watching itself: an armed timer long past its due
    time means nobody swept, and a program whose waits are never read is stuck
    in the way that is hardest to see -- everything looks fine, and nothing
    moves.
    """
    from datetime import UTC, datetime

    moment = now or utcnow()
    limit = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    out = []
    for t in timers:
        if t.status is not TimerStatus.ARMED:
            continue
        due = datetime.strptime(t.due_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        if limit - due > late_after_seconds:
            out.append(Stuck(what="timer", object_id=t.timer_id, state="armed",
                             detail=f"due at {t.due_at}, still armed at {moment}"))
    return out
