"""The project's dispatch activity log, and the pure reading of it (V3.3 P2-15).

`hoh.programlogic` states the rule -- COMMAND -> ACTIVITY -> EVENT -> STATE
REDUCER -> MATERIALIZED STATE, "program decisions must be reconstructable
without depending on a living chat" -- and ships the pure reducer. This module
is where the *product's own* orchestration meets it.

The one activity a project controller performs that costs something and cannot
be repeated blindly is the **dispatch of a node**: launching its run. The
controller records it as events in `ProjectState.activity_events`, in the same
atomic write as the lifecycle transition that goes with it:

    STARTED    persisted together with the node becoming RUNNING, before the launch
    SUCCEEDED  the run was accepted
    FAILED     the run ended any other way (rejected, parked, not started, ...)
    ABANDONED  a person (or the rejection limit) settled the node for good

`in_flight_disagreements` then answers, by `programlogic.reduce` alone, the
question every resuming session asks first -- *is a dispatch still open?* -- and
compares it with what the node's lifecycle claims. The two are written in one
write, so they agree on every state the controller produces; where they do not
(a restored backup, a hand edit, a transition added later that forgot to close
its activity) the decision is not guessed: `resume_decision` blocks and the
controller halts AMBIGUOUS. An activity is never inferred complete from silence.

A node with no logged dispatch at all (a state written before the log existed)
is judged by its lifecycle alone, exactly as before.

Nothing here reads a clock to decide. `recorded_at` is stamped when an event is
appended and is never read by the reducer.
"""

from __future__ import annotations

from typing import Any

from .contracts import utcnow
from .programlogic import ActivityStatus, Event, EventKind, MaterializedState, reduce
from .project import Lifecycle, ProjectState, digest_obj

#: The activity name carried in every event payload.
DISPATCH = "dispatch"


class ActivityLogError(ValueError):
    """The recorded events cannot be folded: a gap, a duplicate sequence, or an
    event naming an activity with no idempotency key on record. The log is not
    repaired or skipped -- a history with a missing event is a history that did
    not happen."""


def dispatch_key(node_id: str) -> str:
    """One idempotency key per node: a retry of the same node's dispatch is the
    same activity, with its attempts carried over."""
    return f"{DISPATCH}:{node_id}"


def _activity_id(node_id: str) -> str:
    return f"{DISPATCH}/{node_id}"


def materialize(state: ProjectState) -> MaterializedState:
    """Fold the state's activity events. The only call of `reduce` the product
    makes; a damaged log raises `ActivityLogError` rather than being skipped."""
    try:
        return reduce(state.activity_events)
    except ValueError as exc:
        raise ActivityLogError(f"activity log of project {state.project_id}: {exc}") from exc


def _open_nodes(folded: MaterializedState) -> set[str]:
    return {key.split(":", 1)[1] for key in folded.open_keys if key.startswith(DISPATCH + ":")}


def _logged_nodes(folded: MaterializedState) -> set[str]:
    return {a.idempotency_key.split(":", 1)[1] for a in folded.activities.values()
            if a.idempotency_key.startswith(DISPATCH + ":")}


def append(state: ProjectState, kind: EventKind, node_id: str, **payload: Any) -> Event:
    """Append one event for `node_id`'s dispatch. The sequence continues the log;
    `recorded_at` is for humans and evidence, never for the reducer."""
    sequence = (state.activity_events[-1].sequence + 1) if state.activity_events else 1
    event = Event(
        event_id=f"{_activity_id(node_id)}/{sequence}", sequence=sequence, kind=kind,
        activity_id=_activity_id(node_id),
        payload={"name": DISPATCH, "idempotency_key": dispatch_key(node_id),
                 "node": node_id, **payload},
        recorded_at=utcnow(),
    )
    state.activity_events.append(event)
    return event


def start(state: ProjectState, node_id: str) -> Event:
    """The dispatch begins. Called where the node becomes RUNNING, before the
    state is persisted, so the open activity and the RUNNING lifecycle are one
    write."""
    return append(state, EventKind.STARTED, node_id)


def is_open(state: ProjectState, node_id: str) -> bool:
    return node_id in _open_nodes(materialize(state))


def close(state: ProjectState, node_id: str, kind: EventKind, **payload: Any) -> Event | None:
    """Close `node_id`'s dispatch if -- and only if -- the log says it is open.
    A state without a logged dispatch (written before the log, or an
    already-accepted run found on resume) records nothing and invents nothing."""
    if not is_open(state, node_id):
        return None
    return append(state, kind, node_id, **payload)


def abandon(state: ProjectState, node_id: str, reason: str) -> Event | None:
    """Record that `node_id` was settled for good, if the log knows its dispatch.
    Closes the dispatch when it is open; a node the log never heard of records
    nothing."""
    if _activity_id(node_id) not in materialize(state).activities:
        return None
    return append(state, EventKind.ABANDONED, node_id, reason=reason[:300])


def outcome_digest(**facts: Any) -> str:
    """Bind what an accepted run produced without keeping it."""
    return digest_obj(facts)


def in_flight_disagreements(state: ProjectState) -> list[str]:
    """Where the reduced activity log and the node lifecycles disagree about a
    dispatch being in flight. Empty on every state the controller writes.

    * a dispatch the log holds open on a node that is not RUNNING (or is not in
      the state at all);
    * a node RUNNING that the log knows a dispatch for, which the log has closed.

    A RUNNING node the log has never heard of is not a disagreement: that is a
    state from before the log, and it is evaluated against its run record.
    Raises `ActivityLogError` for a log that cannot be folded.
    """
    folded = materialize(state)
    open_ = _open_nodes(folded)
    logged = _logged_nodes(folded)
    by_id = {n.id: n for n in state.nodes}
    problems: list[str] = []
    for node_id in sorted(open_):
        node = by_id.get(node_id)
        if node is None:
            problems.append(f"the log holds a dispatch of {node_id!r} open and the state has no such node")
        elif node.lifecycle is not Lifecycle.RUNNING:
            problems.append(f"the log holds the dispatch of {node_id} open but the node is "
                            f"{node.lifecycle.value}, not RUNNING")
    for node in state.nodes:
        if node.lifecycle is Lifecycle.RUNNING and node.id in logged and node.id not in open_:
            problems.append(f"node {node.id} is RUNNING but the log shows its dispatch finished "
                            f"({folded.activities[_activity_id(node.id)].status.value})")
    return problems


__all__ = [
    "ActivityLogError", "ActivityStatus", "DISPATCH", "abandon", "append", "close", "dispatch_key",
    "in_flight_disagreements", "is_open", "materialize", "outcome_digest", "start",
]
