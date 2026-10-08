"""Program logic against activities (V3.3 P2-15).

The plan's durable execution rule:

    COMMAND -> ACTIVITY -> EVENT -> STATE REDUCER -> MATERIALIZED STATE

    Program decisions must be reconstructable without depending on a living chat.

That holds only if the two halves stay apart:

* **Program logic decides.** `reduce` is a pure function of the events so far:
  no clock, no randomness, no filesystem, no environment, no network, no
  provider. Replayed over the same events it produces the identical
  `MaterializedState`, and `state_digest` makes any divergence visible as a
  digest difference rather than as a subtle behavioural one.
* **An activity acts.** `run_activity` is the only place a side effect may
  happen. It calls a caller-supplied `perform()` at most once, takes timestamps
  only from an injected `now`, and the only way its outcome reaches the state is
  as recorded `Event`s.

**The reducer never reads `Event.recorded_at`** -- not to decide, not to order,
not to copy into the state. A reducer that branches on a timestamp replays
differently the moment the same events are replayed at a different time, which
is exactly the failure the rule exists to prevent. `recorded_at` (and the
`started_at`/`ended_at` an activity run carries) is for humans and for evidence;
folded activities leave both empty and `state_digest` excludes them.

`purity_problems` turns the rule into a check over this module's own AST, in
the spirit of the gateway's MCP7: starting from `reduce` and following the
module-level helpers it calls, it reports every name a pure reducer may not
reach.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sys
import textwrap
from enum import StrEnum
from types import ModuleType
from typing import Any, Callable, Iterable

from pydantic import ConfigDict, Field, field_validator

from hoh.contracts import Strict


class ActivityStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class EventKind(StrEnum):
    SCHEDULED = "SCHEDULED"   # an activity was asked for; nothing happened yet
    STARTED = "STARTED"       # one attempt began; this is what counts attempts
    SUCCEEDED = "SUCCEEDED"   # payload.result_digest binds what came back
    FAILED = "FAILED"         # payload.failure says why
    ABANDONED = "ABANDONED"   # attempts reached max_attempts; no more retries
    SKIPPED = "SKIPPED"       # key already done; perform was not called


OPEN_STATUSES = (ActivityStatus.PENDING, ActivityStatus.RUNNING)
FAILED_STATUSES = (ActivityStatus.FAILED, ActivityStatus.ABANDONED)


class Activity(Strict):
    """A named, identified unit of effect. Retry is the point of an activity,
    and a retry without an identity is a second effect: an empty
    `idempotency_key` is refused at construction."""

    activity_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    idempotency_key: str
    status: ActivityStatus = ActivityStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    started_at: str | None = None
    ended_at: str | None = None
    result_digest: str | None = None
    failure: str | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _key_present(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("an activity needs a non-empty idempotency_key")
        return value


class Event(Strict):
    """The append-only record and the reducer's only input. `recorded_at` is
    carried for humans and evidence; the reducer never reads it."""

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    kind: EventKind
    activity_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    recorded_at: str


class MaterializedState(Strict):
    """What the events so far add up to. Frozen: a state is a fold, not a
    thing to be edited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    activities: dict[str, Activity]
    completed: tuple[str, ...]
    failed: tuple[str, ...]
    open_keys: tuple[str, ...]
    state_digest: str


# --------------------------------------------------------------------------- #
# The state reducer -- pure. Everything reduce() reaches is checked by
# purity_problems(); keep it free of clocks, randomness, I/O and environment.
# --------------------------------------------------------------------------- #

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ordered(events: Iterable[Event]) -> list[Event]:
    """Sort by sequence; a duplicate or a gap raises rather than being skipped.
    A reducer that tolerates a missing event reconstructs a history that did
    not happen."""
    ordered = sorted(events, key=lambda e: e.sequence)
    for before, after in zip(ordered, ordered[1:]):
        if after.sequence == before.sequence:
            raise ValueError(f"duplicate event sequence {after.sequence}")
        if after.sequence != before.sequence + 1:
            raise ValueError(
                f"gap in event sequence: {before.sequence} is followed by {after.sequence}")
    return ordered


def _key_of(event: Event, key_by_id: dict[str, str]) -> str:
    key = event.payload.get("idempotency_key")
    if key:
        return str(key)
    if event.activity_id in key_by_id:
        return key_by_id[event.activity_id]
    raise ValueError(
        f"event {event.event_id} names activity {event.activity_id!r} "
        "with no idempotency_key on record")


def _fold(entries: dict[str, Activity], key_by_id: dict[str, str],
          event: Event) -> None:
    key = _key_of(event, key_by_id)
    key_by_id[event.activity_id] = key
    payload = event.payload
    current = entries.get(key)
    if current is None:
        current = Activity(
            activity_id=event.activity_id,
            name=str(payload.get("name") or event.activity_id),
            idempotency_key=key,
            max_attempts=int(payload.get("max_attempts", 3)),
        )
    # The later sequence wins: the entry now answers to this activity id, and
    # attempts carry over, which is what makes a retry a retry.
    changes: dict[str, Any] = {"activity_id": event.activity_id}
    if "name" in payload:
        changes["name"] = str(payload["name"])
    if "max_attempts" in payload:
        changes["max_attempts"] = int(payload["max_attempts"])

    kind = event.kind
    if kind == EventKind.SCHEDULED:
        changes.update(status=ActivityStatus.PENDING)
    elif kind == EventKind.STARTED:
        changes.update(status=ActivityStatus.RUNNING, attempts=current.attempts + 1)
    elif kind == EventKind.SUCCEEDED:
        changes.update(status=ActivityStatus.SUCCEEDED, failure=None,
                       result_digest=payload.get("result_digest"))
    elif kind == EventKind.FAILED:
        changes.update(status=ActivityStatus.FAILED,
                       failure=str(payload.get("failure") or "failed"))
    elif kind == EventKind.ABANDONED:
        changes.update(status=ActivityStatus.ABANDONED)
    elif kind == EventKind.SKIPPED:
        # An explicit record that the key is already done. It completes an
        # entry that has no history here, and leaves one that has alone.
        if key not in entries:
            changes.update(status=ActivityStatus.SUCCEEDED)
    else:  # pragma: no cover - EventKind is closed
        raise ValueError(f"unknown event kind {kind!r}")
    entries[key] = current.model_copy(update=changes)


def _state_digest(sequence: int, activities: dict[str, Activity],
                  completed: tuple[str, ...], failed: tuple[str, ...],
                  open_keys: tuple[str, ...]) -> str:
    content = {
        "sequence": sequence,
        "activities": {
            aid: act.model_dump(mode="json", exclude={"started_at", "ended_at"})
            for aid, act in sorted(activities.items())
        },
        "completed": list(completed),
        "failed": list(failed),
        "open_keys": list(open_keys),
    }
    return _sha256(_canonical(content))


def reduce(events: Iterable[Event], *, upto: int | None = None) -> MaterializedState:
    """Fold events, in sequence order, into the materialized state.

    Pure and deterministic: the same events give the same state and the same
    `state_digest`, whenever and wherever they are replayed. `upto` folds only
    events with `sequence <= upto`, so every earlier state is reconstructable.
    `recorded_at` is never read.
    """
    ordered = _ordered(events)
    if upto is not None:
        ordered = [e for e in ordered if e.sequence <= upto]

    entries: dict[str, Activity] = {}
    key_by_id: dict[str, str] = {}
    for event in ordered:
        _fold(entries, key_by_id, event)

    activities = {act.activity_id: act for act in entries.values()}
    completed = tuple(sorted(k for k, a in entries.items()
                             if a.status == ActivityStatus.SUCCEEDED))
    failed = tuple(sorted(k for k, a in entries.items() if a.status in FAILED_STATUSES))
    open_keys = tuple(sorted(k for k, a in entries.items() if a.status in OPEN_STATUSES))
    sequence = ordered[-1].sequence if ordered else 0
    return MaterializedState(
        sequence=sequence, activities=activities, completed=completed,
        failed=failed, open_keys=open_keys,
        state_digest=_state_digest(sequence, activities, completed, failed, open_keys),
    )


# --------------------------------------------------------------------------- #
# The activity -- the only place an effect happens
# --------------------------------------------------------------------------- #

def result_digest(result: Any) -> str:
    """Bind what came back without keeping it: sha256 over a canonical form."""
    return _sha256(_canonical(result))


def run_activity(
    activity: Activity,
    *,
    perform: Callable[[], Any],
    now: Callable[[], str] | str,
    seen_keys: Iterable[str] = (),
    first_sequence: int = 1,
) -> tuple[Activity, list[Event]]:
    """Run one attempt of `activity` and record what happened as events.

    * A key already in `seen_keys` is not performed again: `perform` is not
      called and a SKIPPED event says the activity was already done.
    * Otherwise `perform()` is called exactly once. Its return value is
      digested into `result_digest`, never stored.
    * `perform` raising is a FAILED attempt, not an exception the caller sees.
      When `attempts` reaches `max_attempts` the activity is ABANDONED.

    Timestamps come only from the injected `now` (a callable or a fixed
    string). Events are numbered from `first_sequence`, so a caller can append
    them to an existing log and fold them with `reduce`.
    """
    clock = now if callable(now) else (lambda: str(now))
    seq = first_sequence
    events: list[Event] = []
    identity = {"name": activity.name, "idempotency_key": activity.idempotency_key,
                "max_attempts": activity.max_attempts}

    def record(kind: EventKind, **payload: Any) -> str:
        nonlocal seq
        stamp = clock()
        events.append(Event(
            event_id=f"{activity.activity_id}/{seq}", sequence=seq, kind=kind,
            activity_id=activity.activity_id, payload={**identity, **payload},
            recorded_at=stamp,
        ))
        seq += 1
        return stamp

    if activity.idempotency_key in set(seen_keys):
        stamp = record(EventKind.SKIPPED, reason="already done")
        done = activity.model_copy(update={"status": ActivityStatus.SUCCEEDED,
                                           "ended_at": stamp})
        return done, events

    if activity.status in (ActivityStatus.SUCCEEDED, ActivityStatus.ABANDONED) \
            or activity.attempts >= activity.max_attempts:
        raise ValueError(
            f"activity {activity.activity_id!r} is {activity.status} after "
            f"{activity.attempts}/{activity.max_attempts} attempts; not run again")

    attempt = activity.attempts + 1
    started = record(EventKind.STARTED, attempt=attempt)
    try:
        result = perform()
    except Exception as exc:  # the attempt failed; the programme does not
        failure = f"{type(exc).__name__}: {exc}"
        ended = record(EventKind.FAILED, attempt=attempt, failure=failure)
        status = ActivityStatus.FAILED
        if attempt >= activity.max_attempts:
            ended = record(EventKind.ABANDONED, attempt=attempt)
            status = ActivityStatus.ABANDONED
        return activity.model_copy(update={
            "status": status, "attempts": attempt, "started_at": started,
            "ended_at": ended, "failure": failure, "result_digest": None,
        }), events

    digest_ = result_digest(result)
    ended = record(EventKind.SUCCEEDED, attempt=attempt, result_digest=digest_)
    return activity.model_copy(update={
        "status": ActivityStatus.SUCCEEDED, "attempts": attempt,
        "started_at": started, "ended_at": ended, "failure": None,
        "result_digest": digest_,
    }), events


# --------------------------------------------------------------------------- #
# The rule as a check
# --------------------------------------------------------------------------- #

FORBIDDEN_NAMES = frozenset({
    "time", "random", "open", "subprocess", "requests", "urllib", "perform",
    "socket", "secrets", "input",
})
FORBIDDEN_ATTRIBUTES = frozenset({
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
    ("date", "today"), ("os", "environ"), ("os", "getenv"), ("os", "urandom"),
})


def _dotted(node: ast.AST) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def _function_findings(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []
    for node in ast.walk(fn):
        where = f"{fn.name} (line {getattr(node, 'lineno', '?')})"
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{where}: uses {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr == "recorded_at":
                found.append(f"{where}: reads recorded_at")
            chain = _dotted(node)
            for i in range(len(chain) - 1):
                if (chain[i], chain[i + 1]) in FORBIDDEN_ATTRIBUTES:
                    found.append(f"{where}: uses {chain[i]}.{chain[i + 1]}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            for mod in mods:
                root = mod.split(".")[0]
                if root in FORBIDDEN_NAMES or root in ("os", "datetime"):
                    found.append(f"{where}: imports {mod}")
    return found


def _called_names(fn: ast.AST) -> set[str]:
    return {node.func.id for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def purity_problems(module: ModuleType | Callable[..., Any] | str | None = None,
                    *, entry: str = "reduce") -> list[str]:
    """Report every forbidden name reachable from `entry` in `module`,
    including any read of `recorded_at`.

    `module` defaults to this module. It may also be a function (its module is
    parsed and the function is the entry), or source text. The walk starts at
    `entry` and follows the module-level functions it calls, transitively.
    """
    if module is None:
        module = sys.modules[__name__]
    if isinstance(module, str):
        source = module
    elif isinstance(module, ModuleType):
        source = inspect.getsource(module)
    else:
        entry = module.__name__
        owner = inspect.getmodule(module)
        source = inspect.getsource(owner) if owner is not None \
            else inspect.getsource(module)
    tree = ast.parse(textwrap.dedent(source))
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if entry not in functions:
        return [f"entry function {entry!r} not found; nothing was checked"]

    problems: list[str] = []
    seen: set[str] = set()
    todo = [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        fn = functions[name]
        problems.extend(_function_findings(fn))
        todo.extend(sorted(n for n in _called_names(fn) if n in functions))
    return problems
