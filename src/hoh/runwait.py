"""`hoh wait RUN | --report PATH | --pid N`: block until a run is at rest, a review report is
final, or a process has ended, from what the product (or the kernel) already writes.

One read-only command answers "is this run finished, or waiting, and for whom?" once,
instead of a hand-written shell loop on `state.json` and a second one on the process.
It looks, decides, and otherwise sleeps and looks again, bounded by a timeout; it prints
one JSON line and the exit code names the outcome. It writes nothing: no state, no lock
file, no marker, no parked copy, no log. It starts no process and calls no Herdr. It
reads no report unless it is asked to wait for one (`--report`), and then only through the
merge gate's reader.

A look at a run asks the process first and then reads the state: `RunStore.running_process()` (the
lock, the one definition of "something executes this run"), then `process.json`, then
`state.json` parsed in memory (`RunStore._parse_state`, never `read_state`, which parks
and rewrites a state that needs a migration). When the probe finds nothing executing the
run, every write that process made happened before the probe, so the state read next is
final.

Phases of a run: `running`, `finishing` (a process still executes a run whose state is at
rest), `accepted` (a checkpointed run that carries its accepted candidate), `failed`,
`cancelled`, `blocked` (a person), `parked` (time, and a resume loop owns the kind, has
not given up and, for storage pressure, has a timer to wake it), `held` (no loop will lift
it; a person looks), `paused`, `idle` (nothing executes the run and it is not finished)
and `died` (the process that executed it was killed).

The report wait (`--report PATH`, slice 1b of the row P5-43; clauses C2 and C8): a report is
final when, at one look, the file is a regular file (read by `mergegate._read_report`: never
blocked on, bounded, not followed below the resolved path), has not been modified for the
stable age (`mergegate.DEFAULT_MIN_AGE`, read at the call) and ends with a well-formed footer
(`mergegate.parse_footer`). The content is judged by `mergegate.judge_footer` against the
footer's own CANDIDATE, BASE and SPEC_SHA256: only COMPLETENESS, VERDICT and BLOCKING_OPEN
count here. Looks: `missing`, `unreadable`, `unstable`, `no_footer` (the writer may be slow:
the wait goes on), `invalid`, `partial`, `no` and `yes`. Exit 0 of a report wait is not a
merge approval.

The process wait (`--pid N`, slice 1c; clause C3): the first look reads `/proc/N/stat` and
keeps `(N, start time)`; the wait ends when the process is gone, when the number belongs to
another start time, or when the process is a zombie (or dead) that runs nothing. A pid that
is not there at the first look is `gone`, not `ended`. Only `<proc>/<pid>/stat` and
`<proc>/sys/kernel/random/boot_id` are read: no command line, no name, no signal.

What this is not (slices 1, 1b and 1c of the row P5-43; the rest is tracked, not built
here): it keeps no durable list of what it reported (C7: the state is durable, so a run that
finished before the wait started, or before a restart of the wait, is answered at the first
look), and is not a trigger of the reaper (C9). It does not wait for several targets, and it
does not resume, unblock, cancel or close anything. The row stays MISSING.

Limits:
  L1  `idle` is a verdict about the moment of the look: a relaunch that has not yet taken
      the process lock reads `idle` against the previous process's `ended` marker
      (`process_ended` is in the answer so that a caller can tell an old end from a new
      one).
  L2  A run executed without `hoh run` (no `process.json`) has process `none`: the first
      checkpoint ends the wait even if the driver goes on.
  L3  A hung process that still holds its lock is `live`: the run stays `running` until
      the timeout.
  L4  `parked`, `held` and `blocked` are the product's own classification
      (`resumption.WAITING_KINDS`, `resumption.capability_blocked`, and the kinds the two
      resume loops consume); a waiting kind that no loop consumes reads `held`.
  L5  The lock semantics assume a local file system with working `flock`.
  L6  Nothing here has run against a live Herdr session or a run in flight (NOT_RUN).
  L7  `parked` says a loop will lift the block, not that one is running; and lifting is not
      running: a lifted park reads `idle` until someone runs `hoh run` again.
  L8  For a quota park, `parked` does not include the loop's check of the park's routing
      receipt and the master's standing (`dispatchrecovery.verify_binding`); the answer
      says `binding: "unchecked"` and `hoh resume-quota` is the authority. The attempt
      limit is decided here: a park the loop gave up on is `held`.
  L9  `caveat` names what the phase alone does not say, from a closed set (null when
      there is nothing to say): `parked_without_timer` and `timer_store_unreadable`
      (a storage park the storage loop cannot wake: it has no `retry_after` fallback,
      so the phase is `held`; the look reads `timers.json` and the answer is final only
      when the state still says parked after that read), `checkpointed_without_candidate`
      (a state at CHECKPOINTED or READY_FOR_DELIVERY with no accepted candidate is not
      called accepted: `held`; the product never writes it), and `accepted_process_died`
      (accepted, but the process that executed the run was killed: the post-acceptance
      close may not have run; the exit stays 0, the state is the truth). Whether the
      storage loop will take a due timer (epoch, capacity) is not decided here. The
      second state read is not preceded by a second probe of the process: the process
      answer is the one the look began with, so a run relaunched in between reads as in
      L1 (`idle` against the previous `ended` marker); no new case follows from it.
  L10 The stable age trusts the file's modification time, as the gate does: a writer that
      sets the time into the past (`touch -d`, `cp -p`, `rsync -t`) makes a growing file
      read stable. The merge gate re-reads the report at merge time with the same rule;
      this command does not add a second line of defence.
  L11 `--report` follows symbolic links in the path it is given (the path is resolved
      first, as `hoh merge-gate check` does) and resolves it again at every look; a link
      re-pointed between looks is followed, and the answer names the file the last look
      read. The gate re-reads at merge time.
  L12 A report is read whole (at most 8 MiB) at every look.
  L13 The footer is judged against its own values; its binding to a candidate, a base and
      a specification is the gate's (`hoh merge-gate check`).
  L14 `--pid` needs a readable Linux `/proc`. A pid that was reused before the first look
      cannot be told from the process meant (there is no way to name a start time); the
      first look names the process. The start time is in clock ticks since boot; the boot
      id is reported, not compared (the waiter does not outlive a reboot). A thread id is
      accepted and ends with the thread. A zombie group leader whose other threads still
      run (stat field 20 above 1) is alive. A process in another pid namespace, or hidden
      by `hidepid`, is `gone` or a refusal, never `ended`.
  L15 `ended` says the process the first look saw is not running; it says nothing about
      how it ended.
  L16 Nothing here ran against a live reviewer, a live Herdr or a live orchestrator
      rotation (NOT_RUN).
  L17 A report that is final is final at the first look, whatever it is a report of (a
      stale complete report at the same path: use a fresh name, or compare
      `report_sha256` with the digest you expect, or ask `hoh merge-gate check`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from time import time as _wall_time
from typing import Any, Callable

from .contracts import Condition, RunState, Stage, recorded_original
from .store import RunStore, StoreError

SCHEMA = "hoh-wait/1"

DEFAULT_TIMEOUT = 3600.0
DEFAULT_POLL = 10.0
MIN_POLL = 1.0

#: The exit code of a refusal, or of a failure of the command itself (no JSON line).
REFUSED = 2

#: The closed set of `caveat` values (L9).
CAVEATS = ("parked_without_timer", "timer_store_unreadable",
           "checkpointed_without_candidate", "accepted_process_died")

#: Bounds that keep the answer one short line whatever the run directory holds.
REASON_LIMIT = 400
MARKER_LIMIT = 64

#: outcome -> exit code
EXIT = {"accepted": 0, "reached": 0, "failed": 1, "cancelled": 1,
        "blocked": 3, "parked": 4, "paused": 5, "idle": 6, "died": 7, "held": 8,
        "timeout": 124,
        # the report wait (C2, C8) and the process wait (C3); the run wait's eleven are above
        "report_yes": 0, "report_no": 1, "report_partial": 8, "report_invalid": 8,
        "ended": 0, "gone": 9}

TERMINAL_PHASES = frozenset({"accepted", "failed", "cancelled"})
SETTLED_PHASES = TERMINAL_PHASES | frozenset(
    {"blocked", "parked", "held", "paused", "idle", "died"})


class WaitRefused(Exception):
    """Nothing could be waited on: a bad `--until`, `--poll` or `--timeout`, a missing or
    unreadable state."""


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Until:
    kind: str              # settled | terminal | stage | condition
    name: str | None       # the stage or condition asked for
    text: str              # as given (`settled` when none)


def parse_until(text: str | None) -> Until:
    """`settled` (default), `terminal`, `stage=NAME`, `condition=NAME`. Exact names."""
    if text is None:
        return Until("settled", None, "settled")
    if text in ("settled", "terminal"):
        return Until(text, None, text)
    for kind, enum in (("stage", Stage), ("condition", Condition)):
        prefix = kind + "="
        if text.startswith(prefix):
            name = text[len(prefix):]
            if name in {member.value for member in enum}:
                return Until(kind, name, text)
            raise WaitRefused(
                f"--until {text!r}: {name!r} is not a {kind}; one of "
                f"{', '.join(member.value for member in enum)}")
    raise WaitRefused(
        f"--until {text!r}: one of settled, terminal, stage=NAME, condition=NAME "
        f"(exact, upper-case names; settled: accepted, failed, cancelled, blocked, "
        f"parked, held, paused, idle or died; terminal: accepted, failed or cancelled)")


def _check_bounds(timeout: float, poll: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"timeout must be a finite number of seconds, 0 or more; got {timeout!r}")
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) \
            or not math.isfinite(poll) or poll < MIN_POLL:
        raise ValueError(f"poll must be a finite number of seconds, at least {MIN_POLL:g}; got {poll!r}")


def _number(text: str, what: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{what} must be a number of seconds, got {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{what} must be finite, got {text!r}")
    return value


def timeout_arg(text: str) -> float:
    """argparse type of `--timeout`: finite, 0 or more."""
    value = _number(text, "--timeout")
    if value < 0:
        raise argparse.ArgumentTypeError(f"--timeout must be 0 or more, got {text!r}")
    return value


def poll_arg(text: str) -> float:
    """argparse type of `--poll`: finite, at least `MIN_POLL`."""
    value = _number(text, "--poll")
    if value < MIN_POLL:
        raise argparse.ArgumentTypeError(
            f"--poll must be at least {MIN_POLL:g} second, got {text!r}")
    return value


# --------------------------------------------------------------------------- #
# One look
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Observation:
    stage: str
    condition: str
    iteration: int
    blocked_kind: str | None
    blocked_reason: str | None
    retry_after: str | None
    stop_reason: str | None
    accepted_candidate: str | None
    state_phase: str
    process: str
    process_since: str | None = None
    process_ended: str | None = None
    resume_attempts: int | None = None
    binding: str | None = None
    caveat: str | None = None


@dataclass(frozen=True)
class WaitResult:
    run_id: str
    until: Until
    outcome: str
    matched: bool
    phase: str
    obs: Observation
    waited_s: float
    polls: int

    @property
    def exit_code(self) -> int:
        return EXIT[self.outcome]


def _short(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:MARKER_LIMIT]


def process_state(store: RunStore) -> tuple[str, str | None, str | None]:
    """`(process, since, ended)`: `live` (the lock is held), `died` (the lock is free, the
    marker says `running` and a second probe is still free), `ended` (the lock is free and
    the marker says `ended`) or `none`. The lock is probed first; nothing is written."""
    live = store.running_process() is not None
    marker = store._marker()
    if not isinstance(marker, dict):
        marker = {}
    since = marker["since"][:MARKER_LIMIT] if isinstance(marker.get("since"), str) else None
    ended = marker["ended"][:MARKER_LIMIT] if isinstance(marker.get("ended"), str) else None
    if live:
        return "live", since, None
    if marker.get("state") == "running":
        # A run that starts between the two probes holds its lock: it is live, not dead.
        if store.running_process() is not None:
            return "live", since, None
        return "died", since, None
    if marker.get("state") == "ended":
        return "ended", since, ended
    return "none", None, None


def classify_state(state: RunState) -> str:
    """The phase of the state alone. The condition decides before the stage."""
    from .quota import MAX_RESUME_ATTEMPTS
    from .resumeloop import QUOTA_CLASSES, STORAGE_CLASSES
    from .resumption import WAITING_KINDS, capability_blocked

    if state.condition is Condition.FAILED:
        return "failed"
    if state.condition is Condition.CANCELLED:
        return "cancelled"
    if state.condition is Condition.BLOCKED:
        if state.blocked_kind in WAITING_KINDS and not capability_blocked(state):
            if state.blocked_kind in QUOTA_CLASSES:
                return "held" if state.resume_attempts >= MAX_RESUME_ATTEMPTS else "parked"
            return "parked" if state.blocked_kind in STORAGE_CLASSES else "held"
        return "blocked"
    if state.condition is Condition.PAUSED:
        return "paused"
    if state.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
        # Accepted means the acceptance left its candidate; a final stage without one is
        # a state the product never writes, and a person looks at it.
        return "accepted" if state.last_accepted_candidate is not None else "held"
    return "running"


def _read_state(store: RunStore) -> RunState:
    """The state, parsed in memory (never `read_state`, which may rewrite it)."""
    try:
        raw = store.state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WaitRefused(f"no state for run {store.run_id} at {store.state_path}") from None
    except (OSError, UnicodeError) as exc:
        raise WaitRefused(f"state of run {store.run_id} is unreadable: {type(exc).__name__}: {exc}") from None
    try:
        return RunStore._parse_state(raw)
    except StoreError:
        raise
    except ValueError as exc:                          # pydantic's and json's errors are ValueErrors
        raise WaitRefused(
            f"state of run {store.run_id} is damaged or schema-foreign: {type(exc).__name__}: {exc}") from None


def storage_timer(store: RunStore) -> str:
    """`armed` (an armed STORAGE_RECHECK timer names the run), `none` or `unreadable`: the
    wake-up a storage park depends on. A read of `timers.json` in the runs root, nothing else."""
    from .timers import TimerKind, TimerStatus, TimerStore, TimerStoreError

    try:
        timers = TimerStore(store.root).read()
    except TimerStoreError:
        return "unreadable"
    armed = [t for t in timers if t.run_id == store.run_id and t.status is TimerStatus.ARMED
             and t.kind is TimerKind.STORAGE_RECHECK]
    return "armed" if armed else "none"


def observe_run(store: RunStore) -> Observation:
    """One look: the process first, then the state, parsed in memory (never rewritten)."""
    from .resumeloop import QUOTA_CLASSES, STORAGE_CLASSES

    process, since, ended = process_state(store)
    state = _read_state(store)
    state_phase = classify_state(state)
    caveat = None
    if state_phase == "parked" and state.blocked_kind in STORAGE_CLASSES:
        timer = storage_timer(store)
        if timer != "armed":
            # The storage loop un-parks a run first and consumes its timer after, so a park
            # without a timer is told only if the state, read again, still says parked.
            state = _read_state(store)
            state_phase = classify_state(state)
            if state_phase == "parked" and state.blocked_kind in STORAGE_CLASSES:
                state_phase = "held"
                caveat = "parked_without_timer" if timer == "none" else "timer_store_unreadable"
    candidate = state.last_accepted_candidate
    if state_phase == "held" and state.condition is Condition.ACTIVE:
        caveat = "checkpointed_without_candidate"
    elif state_phase == "accepted" and process == "died":
        caveat = "accepted_process_died"
    # What an earlier block left behind is not reported: only a run that is BLOCKED now has one.
    blocked = state.condition is Condition.BLOCKED
    reason = state.blocked_reason if blocked else None
    return Observation(
        stage=state.stage.value,
        condition=state.condition.value,
        iteration=state.iteration,
        blocked_kind=_short(state.blocked_kind) if blocked else None,
        blocked_reason=recorded_original(reason)[:REASON_LIMIT] if reason else None,
        retry_after=_short(state.retry_after) if blocked else None,
        stop_reason=recorded_original(state.stop_reason)[:REASON_LIMIT] if state.stop_reason else None,
        accepted_candidate=candidate.candidate_id if candidate is not None else None,
        state_phase=state_phase,
        process=process,
        process_since=since,
        process_ended=ended,
        resume_attempts=state.resume_attempts if blocked else None,
        binding="unchecked" if state_phase == "parked" and state.blocked_kind in QUOTA_CLASSES else None,
        caveat=caveat)


def phase_of(obs: Observation) -> str:
    """The phase of the run: the state phase qualified by the process."""
    if obs.process == "live":
        return "finishing" if obs.state_phase != "running" else "running"
    if obs.process == "died":
        return "died" if obs.state_phase == "running" else obs.state_phase
    if obs.process == "ended":
        return "idle" if obs.state_phase == "running" else obs.state_phase
    return obs.state_phase


# --------------------------------------------------------------------------- #
# The wait
# --------------------------------------------------------------------------- #


def decide(until: Until, obs: Observation, phase: str) -> tuple[str, bool] | None:
    """`(outcome, matched)` when the look ends the wait, else None. First rule wins."""
    if until.kind == "stage" and obs.stage == until.name:
        return "reached", True
    if until.kind == "condition" and obs.condition == until.name:
        return "reached", True
    if until.kind == "terminal" and phase in TERMINAL_PHASES:
        return phase, True
    if until.kind == "settled" and phase in SETTLED_PHASES:
        return phase, True
    # A wait never sits on a run that nothing executes, whatever was asked for.
    if phase in ("idle", "died"):
        return phase, False
    # A stage or condition the run can no longer reach is not waited for.
    if until.kind in ("stage", "condition") and obs.condition in ("FAILED", "CANCELLED"):
        return obs.condition.lower(), False
    return None


def wait(store: RunStore, until: Until, *, timeout: float = DEFAULT_TIMEOUT,
         poll: float = DEFAULT_POLL, sleep: Callable[[float], Any] | None = None,
         monotonic: Callable[[], float] | None = None,
         observe: Callable[[RunStore], Observation] | None = None) -> WaitResult:
    """Look, decide, and otherwise sleep `min(poll, time left)` and look again until
    `timeout` is spent. `poll < 1`, a non-finite or a negative value is a `ValueError`
    before any sleep: nobody can ask for the tight loop this command replaces."""
    _check_bounds(timeout, poll)
    if sleep is None:
        sleep = time.sleep                  # looked up on the module at the call
    if monotonic is None:
        monotonic = time.monotonic
    if observe is None:
        observe = observe_run
    start = monotonic()
    polls = 0
    while True:
        obs = observe(store)
        polls += 1
        phase = phase_of(obs)
        waited = monotonic() - start
        verdict = decide(until, obs, phase)
        if verdict is not None:
            outcome, matched = verdict
            return WaitResult(store.run_id, until, outcome, matched, phase, obs, waited, polls)
        remaining = timeout - waited
        if remaining <= 0:
            return WaitResult(store.run_id, until, "timeout", False, phase, obs, waited, polls)
        sleep(min(poll, remaining))


def render(result: WaitResult) -> str:
    """The answer: one line, ASCII only, fixed key order."""
    obs = result.obs
    return json.dumps({
        "schema": SCHEMA,
        "run_id": result.run_id,
        "until": result.until.text,
        "outcome": result.outcome,
        "matched": result.matched,
        "exit_code": result.exit_code,
        "phase": result.phase,
        "stage": obs.stage,
        "condition": obs.condition,
        "iteration": obs.iteration,
        "process": obs.process,
        "process_since": obs.process_since,
        "process_ended": obs.process_ended,
        "blocked_kind": obs.blocked_kind,
        "blocked_reason": obs.blocked_reason,
        "retry_after": obs.retry_after,
        "resume_attempts": obs.resume_attempts,
        "binding": obs.binding,
        "caveat": obs.caveat,
        "stop_reason": obs.stop_reason,
        "accepted_candidate": obs.accepted_candidate,
        "waited_s": round(result.waited_s, 3),
        "polls": result.polls,
    })


# --------------------------------------------------------------------------- #
# The shared loop of the report wait and the process wait
# --------------------------------------------------------------------------- #


def _loop(look: Callable[[], Any], decide_look: Callable[[Any], tuple[str, bool] | None], *,
          timeout: float, poll: float, sleep: Callable[[float], Any] | None,
          monotonic: Callable[[], float] | None) -> tuple[Any, str, bool, float, int]:
    """The loop of `wait`, for a target that is not a run: bounds checked first, a look, a
    decision, `sleep(min(poll, time left))`, injected `sleep` and `monotonic`.
    Returns `(last look, outcome, matched, waited, polls)`."""
    _check_bounds(timeout, poll)
    if sleep is None:
        sleep = time.sleep                  # looked up on the module at the call
    if monotonic is None:
        monotonic = time.monotonic
    start = monotonic()
    polls = 0
    while True:
        seen = look()
        polls += 1
        waited = monotonic() - start
        verdict = decide_look(seen)
        if verdict is not None:
            return seen, verdict[0], verdict[1], waited, polls
        remaining = timeout - waited
        if remaining <= 0:
            return seen, "timeout", False, waited, polls
        sleep(min(poll, remaining))


def _wall_clock() -> float:
    return getattr(time, "time", _wall_time)()


# --------------------------------------------------------------------------- #
# The report wait (C2, C8)
# --------------------------------------------------------------------------- #

#: look status -> (outcome, matched) for a look that ends the wait; the others go on.
_REPORT_FINAL = {"yes": ("report_yes", True), "no": ("report_no", True),
                 "partial": ("report_partial", False),
                 "invalid": ("report_invalid", False), "unreadable": ("report_invalid", False)}
_REPORT_PROBLEM_STATUS = {"REPORT_MISSING": "missing", "REPORT_UNREADABLE": "unreadable",
                          "REPORT_UNSTABLE": "unstable"}


@dataclass(frozen=True)
class ReportLook:
    """One look at a report. `path` is the file the look read (the path as resolved at that
    look), `sha256` that file's digest. The footer fields are None unless a footer was parsed."""
    path: str
    status: str            # missing | unreadable | unstable | no_footer | invalid | partial | no | yes
    code: str | None       # the gate's code for the look (None for yes)
    verdict: str | None = None
    candidate: str | None = None
    base: str | None = None
    spec_sha256: str | None = None
    blocking_open: str | None = None
    completeness: str | None = None
    sha256: str | None = None
    size: int | None = None
    age_s: float | None = None


@dataclass(frozen=True)
class ReportResult:
    outcome: str
    matched: bool
    look: ReportLook
    min_age: float
    waited_s: float
    polls: int

    @property
    def exit_code(self) -> int:
        return EXIT[self.outcome]


def _check_min_age(min_age: Any) -> None:
    if isinstance(min_age, bool) or not isinstance(min_age, (int, float)) \
            or not math.isfinite(min_age) or min_age < 0:
        raise ValueError(f"min_age must be a finite number of seconds, 0 or more; got {min_age!r}")


def look_report(path: str, *, now: float, min_age: float) -> ReportLook:
    """One look: the gate's reader (`mergegate._read_report`), then the gate's footer grammar
    and judgement against the footer's own values. The path is resolved by the reader at
    this look. Nothing is written."""
    from . import mergegate

    row = mergegate._read_report(path, now=now, min_age=min_age)
    mtime_ns = row["mtime_ns"]
    age = round(now - mtime_ns / 1e9, 3) if isinstance(mtime_ns, int) else None
    where = dict(path=str(row["report"]), sha256=row["sha256"], size=row["size"], age_s=age)
    if row["problems"]:
        code = row["problems"][0][0]
        return ReportLook(status=_REPORT_PROBLEM_STATUS.get(code, "unreadable"), code=code, **where)
    footer, bad = mergegate.parse_footer(row["data"].decode("utf-8", "replace"))
    if footer is None:
        code = bad[0]
        return ReportLook(status="no_footer" if code == "FOOTER_INCOMPLETE" else "invalid",
                          code=code, **where)
    judged = [code for code, _detail in mergegate.judge_footer(
        footer, candidate=footer["CANDIDATE"], base=footer["BASE"], spec_sha256=footer["SPEC_SHA256"])]
    if "INCOMPLETE" in judged:
        status = "partial"
    else:
        status = "no" if judged else "yes"
    return ReportLook(status=status, code=judged[0] if judged else None,
                      verdict=_short(footer["VERDICT"]), candidate=_short(footer["CANDIDATE"]),
                      base=_short(footer["BASE"]), spec_sha256=_short(footer["SPEC_SHA256"]),
                      blocking_open=_short(footer["BLOCKING_OPEN"]),
                      completeness=_short(footer["COMPLETENESS"]), **where)


def wait_report(path: str, *, timeout: float = DEFAULT_TIMEOUT, poll: float = DEFAULT_POLL,
                min_age: float | None = None, sleep: Callable[[float], Any] | None = None,
                monotonic: Callable[[], float] | None = None,
                now: Callable[[], float] | None = None) -> ReportResult:
    """Wait until the report at `path` is final (see the module docstring), the gate's stable
    age read at this call when `min_age` is None. A final report is answered at the first
    look with no sleep. A bad bound, a bad `min_age` or an empty path is refused before any read."""
    from . import mergegate

    _check_bounds(timeout, poll)
    if min_age is None:
        min_age = mergegate.DEFAULT_MIN_AGE
    _check_min_age(min_age)
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
        raise WaitRefused("--report: a path is needed")
    given = os.fspath(path)
    if not isinstance(given, str):
        raise WaitRefused("--report: the path must be text")
    clock = now if now is not None else _wall_clock

    def look() -> ReportLook:
        return look_report(given, now=clock(), min_age=min_age)

    def decide_look(seen: ReportLook) -> tuple[str, bool] | None:
        return _REPORT_FINAL.get(seen.status)

    seen, outcome, matched, waited, polls = _loop(
        look, decide_look, timeout=timeout, poll=poll, sleep=sleep, monotonic=monotonic)
    return ReportResult(outcome, matched, seen, float(min_age), waited, polls)


def render_report(result: ReportResult) -> str:
    """The answer of a report wait: one line, ASCII only, fixed key order."""
    seen = result.look
    return json.dumps({
        "schema": SCHEMA,
        "target": "report",
        "report": seen.path[:REASON_LIMIT],
        "outcome": result.outcome,
        "matched": result.matched,
        "exit_code": result.exit_code,
        "status": seen.status,
        "code": seen.code,
        "verdict": seen.verdict,
        "candidate": seen.candidate,
        "base": seen.base,
        "spec_sha256": seen.spec_sha256,
        "blocking_open": seen.blocking_open,
        "completeness": seen.completeness,
        "report_sha256": seen.sha256,
        "size": seen.size,
        "age_s": seen.age_s,
        "min_age_s": result.min_age,
        "waited_s": round(result.waited_s, 3),
        "polls": result.polls,
    })


# --------------------------------------------------------------------------- #
# The process wait (C3)
# --------------------------------------------------------------------------- #

#: The proc root, read at the call so that tests point it at a synthetic one.
PROC_ROOT = "/proc"
PID_LIMIT = 2 ** 31


def pid_arg(text: str) -> int:
    """argparse type of `--pid`: a positive decimal number in canonical form (digits only, no
    sign, no leading zero, no space) below 2**31."""
    if not isinstance(text, str) or not text.isascii() or not text.isdecimal() or text[0] == "0":
        raise argparse.ArgumentTypeError(
            f"--pid must be a positive decimal number without sign or leading zero, got {text!r}")
    value = int(text)
    if value >= PID_LIMIT:
        raise argparse.ArgumentTypeError(f"--pid must be below {PID_LIMIT}, got {text!r}")
    return value


@dataclass(frozen=True)
class ProcLook:
    """One process as its stat line shows it."""
    pid: int
    start: int             # field 22, clock ticks since boot
    state: str             # field 3
    threads: int           # field 20

    @property
    def runs_nothing(self) -> bool:
        """`X` or `x` (dead), or `Z` with no other thread running: nobody has collected it, it
        runs nothing. A zombie group leader whose other threads run is alive (L14)."""
        return self.state in ("X", "x") or (self.state == "Z" and self.threads <= 1)


def read_pid(pid: int, root: str | None = None) -> ProcLook | None:
    """`<root>/<pid>/stat` read as a stat line, or None when there is no such process.
    Raises `WaitRefused` when the file cannot be read other than by absence or is not a
    stat line. Nothing else of the process is read."""
    from .procs import _parse_stat

    base = PROC_ROOT if root is None else root
    try:
        text = Path(base, str(pid), "stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError, ProcessLookupError):
        return None
    except OSError as exc:
        raise WaitRefused(
            f"the stat line of process {pid} cannot be read: {type(exc).__name__}: {exc}") from None
    try:
        _, _, state, _, _, _, start = _parse_stat(text)
        rest = text[text.rindex(")") + 1:].split()
        threads = int(rest[17])
        if len(state) != 1 or start < 0 or threads < 0:
            raise ValueError("not a stat line")
    except (ValueError, IndexError):
        raise WaitRefused(f"the stat line of process {pid} cannot be read as a stat line") from None
    return ProcLook(pid, start, state, threads)


def read_boot_id(root: str | None = None) -> str:
    """The boot id of the proc file system. A start time means nothing without one, so a
    root that has none is a refusal, not an empty answer."""
    base = PROC_ROOT if root is None else root
    try:
        text = Path(base, "sys", "kernel", "random", "boot_id").read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise WaitRefused(f"the boot id cannot be read ({type(exc).__name__}: {exc}): "
                          f"no usable proc file system") from None
    if not text:
        raise WaitRefused("the boot id is empty: no usable proc file system")
    return text


@dataclass(frozen=True)
class PidResult:
    pid: int
    outcome: str           # ended | gone | timeout
    matched: bool
    how: str | None        # exited | replaced | zombie | None
    start_ticks: int | None
    boot_id: str
    waited_s: float
    polls: int

    @property
    def exit_code(self) -> int:
        return EXIT[self.outcome]


def wait_pid(pid: int, *, timeout: float = DEFAULT_TIMEOUT, poll: float = DEFAULT_POLL,
             sleep: Callable[[float], Any] | None = None,
             monotonic: Callable[[], float] | None = None) -> PidResult:
    """Wait until the process the first look finds under `pid` is gone, has another start
    time, or runs nothing (a zombie). Nothing is signalled, no command line is read."""
    _check_bounds(timeout, poll)
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < PID_LIMIT:
        raise WaitRefused(f"--pid: {pid!r} is not a process id (a positive number below {PID_LIMIT})")
    if pid == os.getpid():
        raise WaitRefused(f"--pid {pid} is this process: the wait would sit until its timeout")
    boot_id = read_boot_id()
    seen_start: list[int] = []         # the start time the first look found
    how: list[str] = []

    def decide_look(seen: ProcLook | None) -> tuple[str, bool] | None:
        if not seen_start:             # the first look
            if seen is None:
                return "gone", False
            seen_start.append(seen.start)
            if seen.runs_nothing:
                how.append("zombie")
                return "ended", True
            return None
        if seen is None:
            how.append("exited")
        elif seen.start != seen_start[0]:
            how.append("replaced")
        elif seen.runs_nothing:
            how.append("zombie")
        else:
            return None
        return "ended", True

    _, outcome, matched, waited, polls = _loop(
        lambda: read_pid(pid), decide_look, timeout=timeout, poll=poll, sleep=sleep,
        monotonic=monotonic)
    return PidResult(pid, outcome, matched, how[0] if how else None,
                     seen_start[0] if seen_start else None, boot_id, waited, polls)


def render_pid(result: PidResult) -> str:
    """The answer of a process wait: one line, ASCII only, fixed key order."""
    return json.dumps({
        "schema": SCHEMA,
        "target": "pid",
        "pid": result.pid,
        "outcome": result.outcome,
        "matched": result.matched,
        "exit_code": result.exit_code,
        "how": result.how,
        "start_ticks": result.start_ticks,
        "boot_id": result.boot_id[:MARKER_LIMIT],
        "waited_s": round(result.waited_s, 3),
        "polls": result.polls,
    })
