"""Durable STORAGE_PRESSURE park of a run (STORAGE-B3, P2-40).

STORAGE-B2 (`hoh.storage_pressure`) decides whether a write-heavy operation
may start. This module is what acts on a refusal: a run whose next role
dispatch was refused parks at a durable boundary and is resumed by its own
consumer (`hoh resume-storage`, `hoh.resumeloop.resume_storage`) once a role
run would be admitted again -- it never crashes into ENOSPC, never spins and
never waits on a person unless owned optimisation cannot recover the space.
The pressure may be caused entirely by foreign data; then the run parks and
waits, and nothing here touches that data.

The park follows the quota park's invariant (`Controller._arm_quota_timer`,
`hoh.resumeloop`):

    a parked state is never durable without its wake-up.

So `park_for_storage_pressure` arms a `STORAGE_RECHECK` timer **first** and
persists the parked state second; a crash in between leaves an armed timer
and an unparked state, which the resume loop refuses harmlessly
(ALREADY_RUNNING). If the timer cannot be armed the run is **not** parked:
it stops (PAUSED) with ``stop_reason = "storage pressure: the recheck timer
could not be armed"`` and is reported NEEDS_YOU. There is no ``retry_after``
fallback for this class.

What this module deliberately does not do:

* It removes nothing -- no file, no directory, owned or foreign.
* It reads, lists and sizes nothing outside a run's own directory and its
  arena. `measure_owned_bytes` walks exactly the paths it is given with
  ``os.scandir`` and ``DirEntry.stat(follow_symlinks=False)`` and follows no
  link; the controller gives it the run directory and the run's arena only.
* It decides nothing about admission: the refusal comes from
  ``StorageAdmission.reserve()``, the resume check from
  ``StorageAdmission.would_admit()``.
"""
from __future__ import annotations

import os
import re
import secrets
import stat as stat_mod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import stages
from .contracts import Condition
from .stages import violation_unresolved
from .timers import TimerKind, TimerStore, in_seconds

#: The blocked kind this module writes and `hoh resume-storage` owns.
STORAGE_CLASS = "storage_pressure"

#: The stop reason of a park whose wake-up could not be armed (Revision 2,
#: item 3). The launcher maps it to STORAGE_UNAVAILABLE (NEEDS_YOU).
ARM_FAILED = "storage pressure: the recheck timer could not be armed"

#: The reason prefix of a dispatch refused because admission itself failed
#: (an unwritable ledger, an unclaimed boot ...): fail closed, NEEDS_YOU.
ADMISSION_UNAVAILABLE = "storage admission unavailable"

#: What a run reports after `ParkPolicy.needs_you_after` rechecks.
NEEDS_YOU_REASON = "owned optimisation could not recover the reserve"

#: Refusal reasons of `StorageAdmission.reserve()` that are about capacity --
#: free space, the reserve, an unobservable host -- and can recover by
#: themselves, so a park with a recheck is the answer. Every other refusal (a
#: pid namespace that differs, an unclaimed boot, a malformed reservation
#: file, a missing namespace record) needs a person: the run stops NEEDS_YOU
#: as "storage admission unavailable" instead of parking.
CAPACITY_REASONS = ("state CRITICAL", "the reservation would cross below the reserve",
                    "host unobservable")


def is_capacity_refusal(refusal: Any) -> bool:
    """Whether a refusal is about capacity (park) rather than the ledger (stop)."""
    return str(getattr(refusal, "reason", "")).startswith(CAPACITY_REASONS)


@dataclass(frozen=True)
class ParkPolicy:
    """How often a storage park is looked at again, and when a person is told.

    Kept out of `StoragePolicy` on purpose: that model refuses unknown keys
    and is STORAGE-B2's contract, which this run does not change.
    """

    recheck_interval_s: int = 600
    needs_you_after: int = 6

    def __post_init__(self) -> None:
        if not isinstance(self.recheck_interval_s, int) or self.recheck_interval_s <= 0:
            raise ValueError(f"recheck_interval_s must be a positive int, "
                             f"got {self.recheck_interval_s!r}")
        if not isinstance(self.needs_you_after, int) or self.needs_you_after <= 0:
            raise ValueError(f"needs_you_after must be a positive int, "
                             f"got {self.needs_you_after!r}")


class StorageParked(Exception):
    """Raised inside the controller after a storage refusal was handled.

    Not a `DispatchError`: the QA path converts those into an outage, and a
    storage park is neither an outage nor a verdict. `run_iteration` turns it
    into a `LoopOutcome`, so no further role is started in that iteration.
    ``parked`` is False when the run was stopped NEEDS_YOU instead.
    """

    def __init__(self, reason: str, *, parked: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.parked = parked


# --------------------------------------------------------------------------- #
# Recheck bookkeeping, carried by the durable timers themselves
# --------------------------------------------------------------------------- #

_RECHECK = re.compile(r"^storage park (?P<park>\S+) recheck (?P<n>\d+): ")


def recheck_reason(park_id: str, recheck: int, detail: str) -> str:
    """The reason a `STORAGE_RECHECK` timer carries: which park, which recheck.

    The recheck count lives in the durable timer, not in a process's memory,
    so a resume loop restarted between two rechecks counts on correctly.
    """
    return f"storage park {park_id} recheck {recheck}: {detail}"[:400]


def parse_recheck(reason: str) -> tuple[str, int] | None:
    """(park id, recheck count) of a timer armed by this module, or None."""
    m = _RECHECK.match(reason or "")
    if m is None:
        return None
    return m.group("park"), int(m.group("n"))


def _timer_stamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"now must be timezone-aware, got naive {moment!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def refusal_numbers(refusal: Any) -> str:
    """The refusal's numbers in one line: state, effective free, reserve,
    estimate, reserved -- what a person reading the parked state needs."""
    state = getattr(getattr(refusal, "state", None), "value", getattr(refusal, "state", "?"))
    free = getattr(refusal, "effective_free", None)
    return (f"state={state} effective_free={'unknown' if free is None else f'{free} B'} "
            f"reserve={getattr(refusal, 'reserve', '?')} B "
            f"estimate={getattr(refusal, 'estimate', '?')} B "
            f"reserved={getattr(refusal, 'reserved', '?')} B")


# --------------------------------------------------------------------------- #
# The park
# --------------------------------------------------------------------------- #


def stop_needs_you(state: Any, store: Any, reason: str) -> None:
    """Stop the run for a person, never parked: PAUSED with ``reason`` as the
    stop reason, a NEEDS_YOU note, persisted."""
    state.condition = Condition.PAUSED
    state.stop_reason = reason
    state.note(f"NEEDS_YOU: {reason}")
    store.write_state(state)


def park_for_storage_pressure(state: Any, refusal: Any, *, store: Any, governor: Any,
                              policy: ParkPolicy, now: datetime, steward: Any = None) -> bool:
    """Park a run whose dispatch `StorageAdmission.reserve()` refused.

    Arms a `STORAGE_RECHECK` timer (in the runs root, where the resume loop
    reads run timers, with the run id and the governing contract's programme
    id and epoch) **first**, then persists the parked state:
    ``blocked_kind = "storage_pressure"``, ``blocked_reason`` with the
    refusal's numbers, ``retry_after = now + policy.recheck_interval_s``.
    Returns True.

    If the timer cannot be armed, nothing is parked: the run stops NEEDS_YOU
    with ``stop_reason = ARM_FAILED`` and False is returned.

    **A held run is never parked.** A run held by a capability violation
    (`stages.violation_unresolved`, read before anything is overwritten) is
    not a wait: parking it would overwrite its `blocked_kind` and
    `blocked_reason` with ``storage_pressure``, and `hoh resume-storage`
    would then lift the hold. It is re-blocked by `stages.block`, which keeps
    the kind (and, for a legacy hold, adopts it), clears `retry_after`, and
    arms no timer; the reason it already has is kept (a run with none gets one
    that names the violation). False is returned and a person's
    `hoh unblock` is the only way on.

    With a `steward` (STORAGE-D, Revisions 6-6b), ``steward.on_event(
    "admission_refused")`` is called **after** the park is durable (timer
    first, then the parked state) -- or after the NEEDS_YOU stop, flagged
    ``arm_failed`` -- and only records the event (one bounded append). Its
    answer or any exception it raises is noted in the run's detail; the park
    never fails because of the steward. Without a steward nothing changes.
    """
    detail = f"{getattr(refusal, 'reason', refusal)}; {refusal_numbers(refusal)}"
    if violation_unresolved(state):
        stages.block(state, state.blocked_reason
                     or "capability violation: the run is held and a dispatch "
                        "was refused for storage pressure")
        state.note(f"storage pressure: {detail}; the run is held by a capability "
                   "violation, so it is not parked and no recheck is armed: only "
                   "`hoh unblock` lifts the hold")
        store.write_state(state)
        _tell_steward(steward, state, store, arm_failed=True)
        return False
    due = in_seconds(policy.recheck_interval_s, now=now)
    park_id = f"{_timer_stamp(now).replace(':', '')}-{secrets.token_hex(3)}"
    try:
        TimerStore(Path(store.dir).parent).arm_new(
            kind=TimerKind.STORAGE_RECHECK, due_at=due,
            reason=recheck_reason(park_id, 0, detail),
            program_id=(governor.contract.contract_id if governor else ""),
            program_epoch=(governor.contract.program_epoch if governor else 0),
            run_id=state.run_id)
    except Exception as exc:                  # noqa: BLE001 - classified, never silent
        state.note(f"storage pressure: arming the recheck timer failed: {exc}"[:400])
        stop_needs_you(state, store, ARM_FAILED)
        _tell_steward(steward, state, store, arm_failed=True)
        return False
    state.condition = Condition.BLOCKED
    state.blocked_kind = STORAGE_CLASS
    state.blocked_reason = f"storage pressure: {detail}"[:600]
    state.stop_reason = state.blocked_reason
    state.retry_after = due
    state.note(f"BLOCKED (storage pressure, recheck at {due}, park {park_id}): "
               f"{refusal_numbers(refusal)}")
    store.write_state(state)
    _tell_steward(steward, state, store, arm_failed=False)
    return True


def _tell_steward(steward: Any, state: Any, store: Any, *, arm_failed: bool) -> None:
    """Record ``admission_refused`` with the project's steward after the park
    (or the NEEDS_YOU stop) is durable. Isolated: whatever the steward
    answers or raises is noted and persisted, never propagated."""
    if steward is None:
        return
    try:
        answer = (steward.on_event("admission_refused", arm_failed=True) if arm_failed
                  else steward.on_event("admission_refused"))
        note = answer.get("note", "") if isinstance(answer, dict) else ""
    except Exception as exc:                  # noqa: BLE001 - the park never fails here
        note = f"storage steward: recording admission_refused failed: {type(exc).__name__}: {exc}"
    if note:
        state.note(note[:400])
        try:
            store.write_state(state)
        except Exception:                     # noqa: BLE001 - the park itself is durable
            pass


# --------------------------------------------------------------------------- #
# Sizing what a dispatch wrote -- owned directories only, no links followed
# --------------------------------------------------------------------------- #


def _walk_bytes(directory: str) -> int:
    """The apparent bytes of the regular files below `directory`. Iterative (an explicit stack, P3-39): a tree
    nested deeper than the interpreter's recursion limit -- a persisted pytest basetemp inside an arena can be --
    is measured, not aborted with a ``RecursionError`` that blocks the run that sized it."""
    total = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue                         # an unreadable directory counts 0, as before
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_mod.S_ISLNK(st.st_mode):
                continue                     # a link is never followed or counted
            if stat_mod.S_ISDIR(st.st_mode):
                pending.append(entry.path)
            elif stat_mod.S_ISREG(st.st_mode):
                total += st.st_size
    return total


#: The most entries ``measure_owned_inodes`` walks before it says the count is unmeasurable (P3-39): a walk without an end is not a measurement.
MAX_INODE_WALK = 5_000_000


def measure_owned_inodes(paths: Iterable[Path | str]) -> int | None:
    """The number of directory entries (files, directories, links) below ``paths`` -- the inodes a run's owned trees hold (P3-39).

    The same discipline as ``measure_owned_bytes``: only the given directories are walked, with ``os.scandir`` and
    ``DirEntry.is_dir(follow_symlinks=False)``; a link is counted as the entry it is (it takes an inode) and never
    followed; a given path that is itself a link, or not a directory, or that is missing counts 0. **Fail closed:** more
    than ``MAX_INODE_WALK`` entries, a directory that cannot be listed (a permission error, a race) and a walk that
    raises are all ``None`` -- unmeasurable, which records nothing -- never a partial count mistaken for the whole.
    The walk is iterative, so a deep tree cannot exhaust the stack."""
    total = 0
    try:
        for path in paths:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat_mod.S_ISDIR(st.st_mode):
                continue
            pending = [os.fspath(path)]
            while pending:
                directory = pending.pop()
                try:
                    entries = list(os.scandir(directory))
                except OSError:
                    return None                      # an unlistable directory is a count nobody has: unmeasurable
                total += len(entries)
                if total > MAX_INODE_WALK:
                    return None
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except OSError:
                        return None
    except Exception:                        # noqa: BLE001 - a walk that cannot be completed is unmeasurable
        return None
    return total


def measure_owned_bytes(paths: Iterable[Path | str]) -> int:
    """The apparent size of the regular files under ``paths``.

    Walks only the given directories (the run directory and its arena), with
    ``os.scandir`` and ``DirEntry.stat(follow_symlinks=False)``: a symbolic
    link -- to a file or to a directory, inside or outside -- is neither
    followed nor counted, so nothing outside the owned directories is read.
    A given path that is itself a link, or not a directory, counts 0.
    """
    total = 0
    for path in paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat_mod.S_ISDIR(st.st_mode):
            continue
        total += _walk_bytes(os.fspath(path))
    return total
