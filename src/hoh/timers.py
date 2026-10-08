"""Durable timers: a wait that survives the process that started it.

V3.3 P2-04. The rule the plan states is a prohibition, and it is the reason
this module exists at all:

    No multi-hour sleep while holding the program/controller lock.

A controller that sleeps for six hours waiting for a provider quota to reset
is indistinguishable, from the outside, from a controller that has hung -- and
it holds the lock the whole time, so nothing else may touch the program
either. HoH already refuses that for the one case it met first: an exhausted
quota blocks the run, writes `retry_after` into its state, and releases
everything (`hoh.quota`). This module is that idea as a general object, so
that *every* wait is a record on disk rather than a process in memory:

* a timer is written down, with what it is for and when it is due;
* whoever is running later reads it -- the same session, its successor, a
  cron, a plugin -- and the wake-up is therefore **replaceable**;
* a terminal program disarms its timers, so a finished program cannot be
  woken by something it armed while it was alive.

What this module deliberately does not do: fire timers. Deciding *when*
something is due is a question about durable state; doing the work is a
question about an agent, a provider and a lock. `hoh.sentinel` reads the due
ones and says what should happen; the controller acts. Keeping the two apart
is what makes a timer testable without a running programme.

Epochs. A timer carries the program epoch, the session epoch and the
generation it was armed under. A timer armed by a master session that has
since been replaced is **stale**, and firing it would let a superseded leader
act through a note it left behind. `due()` refuses those rather than
returning them, and says so.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from . import durable_io
from .contracts import Strict, utcnow

SCHEMA = "hoh-timer/1"


class TimerKind(StrEnum):
    """What a timer is waiting for. The kind decides what the sentinel
    proposes, so an unknown kind is a refusal rather than a default."""

    #: A provider said "come back later"; `hoh.quota` writes the time.
    PROVIDER_RETRY = "provider_retry"
    #: A review the policy requires again after an interval.
    REVIEW_DUE = "review_due"
    #: A run parked on an external condition that has to be looked at again.
    EXTERNAL_WAIT = "external_wait"
    #: A heartbeat: nothing is wrong, but something should look at the program
    #: again, because a program nobody looks at is how a stuck one survives.
    HEARTBEAT = "heartbeat"
    #: A run parked because storage admission refused its next role dispatch
    #: (STORAGE-B3, P2-40); `hoh resume-storage` looks at free space again.
    STORAGE_RECHECK = "storage_recheck"


class TimerStatus(StrEnum):
    ARMED = "armed"
    FIRED = "fired"
    DISARMED = "disarmed"


class Timer(Strict):
    """One durable wait.

    `due_at` is UTC and stored as text, like every other time in this project:
    a timestamp that needs a timezone to be read is a timestamp that will be
    read wrong once.
    """

    schema_version: str = SCHEMA
    timer_id: str
    due_at: str
    kind: TimerKind
    #: Which program armed it, and under which epoch, session and generation.
    #: A timer whose epoch no longer matches is not this program's timer.
    program_id: str = ""
    program_epoch: int = 0
    session_epoch: int = 0
    generation: int = 0
    run_id: str | None = None
    node_id: str | None = None
    status: TimerStatus = TimerStatus.ARMED
    #: Why it was armed, in the words of whoever armed it. Read by a human
    #: looking at a program that is waiting and wondering what for.
    reason: str = ""
    armed_at: str = Field(default_factory=utcnow)
    fired_at: str | None = None
    disarmed_at: str | None = None
    disarmed_reason: str = ""

    def is_due(self, now: str | None = None) -> bool:
        return self.status is TimerStatus.ARMED and self.due_at <= (now or utcnow())


_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def check_time(text: str) -> str:
    """UTC, to the second, with the Z. Refused rather than reinterpreted: a
    local timestamp that reads as UTC is a wait of the wrong length, and the
    error shows up hours later as "why did nothing happen"."""
    if not isinstance(text, str) or not _TIME.match(text):
        raise ValueError(f"not a UTC timestamp of the form 2026-09-22T18:00:00Z: {text!r}")
    return text


def in_seconds(seconds: float, *, now: datetime | None = None) -> str:
    """A due time this many seconds from now, in the stored form."""
    base = now or datetime.now(UTC)
    return datetime.fromtimestamp(base.timestamp() + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TimerStore:
    """The timers of one root, in one file, replaced atomically.

    One file rather than one per timer: the question asked of this store is
    almost always "what is due", which is a question about the set. The file
    is rewritten whole, through a temporary file and a rename, so a reader
    never sees a half-written set -- the same discipline `hoh.store` uses for
    run state.

    Nothing is ever removed. A timer that has fired or been disarmed keeps its
    row and its timestamps, because "this program waited for the quota four
    times last week" is evidence, and a store that forgets cannot be asked.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.path = self.root / "timers.json"

    def read(self) -> list[Timer]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TimerStoreError(f"{self.path} is not readable as timers: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("timers"), list):
            raise TimerStoreError(f"{self.path} does not hold a timer set")
        try:
            return [Timer.model_validate(t) for t in raw["timers"]]
        except ValueError as exc:
            # One malformed row is an unreadable store, reported as such --
            # not a pydantic error escaping into every caller (review A3, F4).
            raise TimerStoreError(f"{self.path} holds an invalid timer: {exc}") from exc

    def write(self, timers: list[Timer]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema_version": SCHEMA, "written_at": utcnow(),
             "timers": [json.loads(t.model_dump_json()) for t in timers]},
            indent=2) + "\n"
        # 0o600, as the file had when `mkstemp` created it (P2-41).
        durable_io.durable_write(self.path, payload, mode=0o600)

    @contextmanager
    def _mutating(self) -> Iterator[None]:
        """An exclusive lock around one read-modify-write of the timer set.

        Without it two writers -- a master arming a wait, the resume loop
        consuming another -- each read the set, change their row and write the
        whole set back, and the later write silently drops the earlier row:
        the one thing this store promises never happens (review A2, N1).
        """
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.root / ".timers.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def arm(self, timer: Timer) -> Timer:
        """Adds a timer. A timer_id that is already present is refused: two
        different waits under one id is how one of them silently disappears."""
        with self._mutating():
            timers = self.read()
            if any(t.timer_id == timer.timer_id for t in timers):
                raise TimerStoreError(f"timer {timer.timer_id} already exists")
            check_time(timer.due_at)
            timers.append(timer)
            self.write(timers)
            return timer

    def arm_new(self, *, kind: TimerKind, due_at: str, reason: str = "",
                program_id: str = "", program_epoch: int = 0, session_epoch: int = 0,
                generation: int = 0, run_id: str | None = None,
                node_id: str | None = None) -> Timer:
        """The usual way to arm one: the id carries its random part first, so
        it stays unambiguous where names are truncated (house rules)."""
        return self.arm(Timer(
            timer_id=f"{uuid.uuid4().hex[:8]}-{kind.value}", due_at=check_time(due_at),
            kind=kind, reason=reason, program_id=program_id, program_epoch=program_epoch,
            session_epoch=session_epoch, generation=generation, run_id=run_id,
            node_id=node_id))

    def _replace(self, timer_id: str, change) -> Timer:
        with self._mutating():
            timers = self.read()
            for i, t in enumerate(timers):
                if t.timer_id == timer_id:
                    timers[i] = change(t)
                    self.write(timers)
                    return timers[i]
        raise TimerStoreError(f"no timer {timer_id}")

    def fire(self, timer_id: str) -> Timer:
        return self._replace(timer_id, lambda t: t.model_copy(update={
            "status": TimerStatus.FIRED, "fired_at": utcnow()}))

    def disarm(self, timer_id: str, reason: str) -> Timer:
        return self._replace(timer_id, lambda t: t.model_copy(update={
            "status": TimerStatus.DISARMED, "disarmed_at": utcnow(),
            "disarmed_reason": reason}))

    def disarm_all(self, reason: str, *, program_id: str | None = None,
                   run_id: str | None = None) -> list[Timer]:
        """What a terminal program does. The plan says it plainly: a terminal
        program disarms all pending recovery. Without this, a finished or
        cancelled program is still woken by what it armed while alive, and the
        wake-up looks exactly like a live program asking for work."""
        with self._mutating():
            timers, changed = self.read(), []
            for i, t in enumerate(timers):
                if t.status is not TimerStatus.ARMED:
                    continue
                if program_id is not None and t.program_id != program_id:
                    continue
                if run_id is not None and t.run_id != run_id:
                    continue
                timers[i] = t.model_copy(update={"status": TimerStatus.DISARMED,
                                                 "disarmed_at": utcnow(),
                                                 "disarmed_reason": reason})
                changed.append(timers[i])
            if changed:
                self.write(timers)
            return changed

    def due(self, *, now: str | None = None, program_epoch: int | None = None,
            session_epoch: int | None = None) -> tuple[list[Timer], list[tuple[Timer, str]]]:
        """(due now, refused with a reason).

        Refused rather than returned: a timer armed under an epoch that has
        been superseded belongs to a leader that is no longer the leader, and
        acting on it is exactly the "stale master writes" failure the plan
        names. The caller sees them, so a refusal is visible rather than a
        timer that quietly never fires.
        """
        moment = now or utcnow()
        ready, refused = [], []
        for t in self.read():
            if t.status is not TimerStatus.ARMED:
                continue
            if not t.is_due(moment):
                continue
            if program_epoch is not None and t.program_epoch > program_epoch:
                refused.append((t, f"armed under program epoch {t.program_epoch}, "
                                   f"this program is at {program_epoch}"))
            elif program_epoch is not None and t.program_epoch < program_epoch:
                refused.append((t, f"armed under program epoch {t.program_epoch}, "
                                   f"superseded by {program_epoch}"))
            elif session_epoch is not None and t.session_epoch < session_epoch:
                refused.append((t, f"armed by session epoch {t.session_epoch}, "
                                   f"superseded by {session_epoch}"))
            else:
                ready.append(t)
        return sorted(ready, key=lambda t: (t.due_at, t.timer_id)), refused

    def armed(self) -> Iterator[Timer]:
        return (t for t in self.read() if t.status is TimerStatus.ARMED)


class TimerStoreError(RuntimeError):
    """The store could not do what was asked, and did not guess instead."""
