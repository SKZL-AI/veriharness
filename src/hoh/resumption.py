"""Resumption: which parked branch a due timer should wake, and why not.

V3.3 O211. P2-04 made a wait durable (`hoh.timers`), P2-05 made the due ones
visible (`hoh.sentinel`), P2-24 arms a real review timer when the contract
parks a branch. What none of them does is the step that makes the loop
autonomous:

    nothing consumes a due timer to resume the parked branch.

The P2-20 run is the live instance: parked with `retry_after` armed, and it
stayed parked, because the orchestrator -- not the system -- would have had to
restart it. A visible timer is not autonomous resumption.

This module is the *decision* half of that loop, as a pure function over
durable state: timers, what the run state says about each run, the current
programme epoch, the kill list, and `now`. It decides what should resume and
why everything else should not. It never resumes anything itself -- no
subprocess, no dispatch, no provider, no sleep -- and the only write it makes
is `consume`, through a `TimerStore` the caller hands in, so that one due
timer is consumed exactly once.

Every refusal is its own verdict, and every refusal carries a reason. A
resumption loop that collapses them either retries a killed run forever or
silently skips a due one; a refusal nobody can read is indistinguishable from
a bug.

Deliberate choices beyond the spec, all of which fail closed:

* A timer epoch *ahead* of `program_epoch` is SUPERSEDED_EPOCH: then the
  caller is the stale party, and a stale caller must not act either.
* A run record at another program epoch is SUPERSEDED_EPOCH: a current timer
  must not resume a run that belongs to a plan which has since moved.
* A run with no integer `program_epoch` is NEEDS_OVERRIDE: which plan it
  belongs to has to be decided, and guessing is how the wrong plan resumes.
* A BLOCKED run whose `blocked_kind` is missing or not one of the waiting
  kinds is NEEDS_OVERRIDE: an unclassified block may be a finding, and only a
  recorded override may clear a finding. Such a run never resumes on its own.
* An unreadable `due_at` or `retry_after` is NEEDS_OVERRIDE: a time that
  cannot be read cannot be said to have passed.
* A run whose own `retry_after` is later than `now` keeps a due timer
  NOT_DUE: the run's later promise (e.g. a quota reset) outranks an earlier
  note, or the resume fails straight away and loops.
* CANCELLED and FAILED count as terminal, and an unknown condition such as
  PAUSED is RUN_NOT_PARKED: only a condition known to be a wait resumes.

Supervisor order: `next_wakeup` only reports *future* due times, so a timer
that is already due is not a wake-up time. A supervisor calls
`due_resumptions` first, acts on its RESUMEs, and only then sleeps until
`next_wakeup` -- sleeping on None before deciding would miss due work.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .taxonomy import FailureClass, classify_failure
from .timers import Timer, TimerStatus, TimerStore, TimerStoreError, check_time


class Verdict(StrEnum):
    """One decision about one timer. Only the first is a resume."""

    RESUME = "RESUME"
    NOT_DUE = "NOT_DUE"
    SUPERSEDED_EPOCH = "SUPERSEDED_EPOCH"
    SUPERSEDED_TIMER = "SUPERSEDED_TIMER"
    RUN_GONE = "RUN_GONE"
    RUN_NOT_PARKED = "RUN_NOT_PARKED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    KILLED = "KILLED"
    NEEDS_OVERRIDE = "NEEDS_OVERRIDE"


class ResumptionError(TimerStoreError):
    """`consume` refused: the decision was not a resume, or the store no
    longer holds that timer armed. Raised rather than firing anyway, because a
    second fire is a branch resumed twice."""


@dataclass(frozen=True)
class Resumption:
    """One decision about one parked run.

    A refusal (any verdict but RESUME) must say why. One without a reason is
    refused at construction: "not resumed" with no reason cannot be told apart
    from a resumption loop that is broken.
    """

    run_id: str | None
    timer_id: str
    verdict: Verdict
    why: str
    due_at: str
    program_epoch: int
    timer_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        if not isinstance(self.why, str):
            raise TypeError(f"why must be text, got {type(self.why).__name__}")
        if self.verdict is not Verdict.RESUME and not self.why.strip():
            raise ValueError(
                f"a {self.verdict.value} decision for timer {self.timer_id!r} "
                f"must say why it refuses")

    @property
    def resumes(self) -> bool:
        return self.verdict is Verdict.RESUME


#: Conditions a run can take on that end it. A timer for such a run has
#: nothing left to resume. `CANCELLED` and `FAILED` are how `RunState` spells
#: them (`hoh.contracts.Condition`); `ACCEPTED` and `STOPPED` are how the plan
#: does.
TERMINAL = frozenset({"ACCEPTED", "STOPPED", "CANCELLED", "FAILED"})

#: `blocked_kind` values that mean "waiting for a time", not "waiting for a
#: decision": a contract park, a question collected for the digest, a
#: provider usage limit, and a storage-pressure park (STORAGE-B3). A BLOCKED
#: run with one of these, and no capability violation behind it, is parked in
#: the sense this module resumes -- by the consumer that owns its class.
WAITING_KINDS = frozenset({"parked_by_contract", "collected_for_digest", "usage_limit",
                           "storage_pressure"})


def normalise_now(now: datetime | str) -> str:
    """`now` in the one form `check_time` accepts: UTC, to the second, with Z.

    A naive datetime is refused rather than assumed to be UTC -- the same
    reason `check_time` refuses a local timestamp: the wait comes out the
    wrong length and nobody notices for hours.
    """
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(f"now must be timezone-aware, got naive {now!r}")
        return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return check_time(now)


def _parse(text: str) -> datetime:
    return datetime.strptime(check_time(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _field(run: Any, name: str) -> Any:
    if isinstance(run, Mapping):
        return run.get(name)
    return getattr(run, name, None)


def capability_blocked(run: Any) -> bool:
    """Whether the run state records a capability violation.

    `blocked_kind` alone cannot say so. `controller._block` writes the
    violation as `blocked_reason='capability violation: ...'` with no kind,
    and `governance.block_under_contract` then fills that empty kind with
    `parked_by_contract` and arms a retry -- which is exactly how the P2-20
    run looked, and why `hoh run` refused it when its timer came due. So the
    reason is read as well as the kind, and it is read by the project's one
    failure classifier, `hoh.taxonomy.classify_failure`, rather than by a
    private string match that could drift from it. The coupling to the
    controller's wording remains; `tests/test_resumption.py` pins it so that
    a reworded reason fails a test instead of silently resuming.
    """
    if str(_field(run, "condition") or "").upper() != "BLOCKED":
        return False
    kind = str(_field(run, "blocked_kind") or "").strip().lower()
    if kind == "capability_violation":
        return True
    reason = str(_field(run, "blocked_reason") or "")
    return classify_failure(None, detail=reason) is FailureClass.CAPABILITY_VIOLATION


def _newest(timers: Iterable[Timer], program_epoch: int) -> dict[str, str]:
    """run_id -> timer_id of the one live timer that may speak for that run.

    Contenders are ARMED timers at exactly `program_epoch`; a timer from
    another epoch is refused for its epoch and must not out-rank a current
    one. Newest is (program_epoch, generation, armed_at) -- what the timer
    says about when it was armed, never the order it was passed in or its id.
    Only an exact tie on all three falls back to the later `due_at` and then
    the larger `timer_id`, so that even then exactly one timer wins.
    """
    best: dict[str, Timer] = {}
    for t in timers:
        if t.run_id is None or t.status is not TimerStatus.ARMED:
            continue
        if t.program_epoch != program_epoch:
            continue
        key = (t.program_epoch, t.generation, t.armed_at, t.due_at, t.timer_id)
        cur = best.get(t.run_id)
        if cur is None or key > (cur.program_epoch, cur.generation, cur.armed_at,
                                 cur.due_at, cur.timer_id):
            best[t.run_id] = t
    return {run_id: t.timer_id for run_id, t in best.items()}


def _decide(t: Timer, *, runs: Mapping[str, Any], now: str, program_epoch: int,
            killed: frozenset[str], winners: Mapping[str, str]) -> tuple[Verdict, str]:
    """The rules, in fail-closed precedence. The first that applies decides."""
    # 1. The kill switch survives the resumption loop, whatever the timer says.
    if t.run_id is not None and t.run_id in killed:
        return Verdict.KILLED, f"run {t.run_id} is on the kill list; it is never resumed"

    # 2. A timer from another programme generation. Behind: the plan it was
    #    armed under may no longer hold. Ahead: this caller is the stale one.
    if t.program_epoch < program_epoch:
        return (Verdict.SUPERSEDED_EPOCH,
                f"armed under program epoch {t.program_epoch}, superseded by {program_epoch}")
    if t.program_epoch > program_epoch:
        return (Verdict.SUPERSEDED_EPOCH,
                (f"armed under program epoch {t.program_epoch}, ahead of this "
                 f"caller's epoch {program_epoch}; the caller is stale, not the timer"))

    # 3. Not live, or not the one live timer for its run.
    if t.status is not TimerStatus.ARMED:
        return (Verdict.SUPERSEDED_TIMER,
                f"timer is {t.status.value}, not armed; it has already been consumed or withdrawn")
    if t.run_id is not None and winners.get(t.run_id) != t.timer_id:
        return (Verdict.SUPERSEDED_TIMER,
                f"a newer timer {winners.get(t.run_id)} speaks for run {t.run_id}")

    # 4. Absence is not permission.
    if t.run_id is None:
        return Verdict.RUN_GONE, "timer names no run, so there is no branch to resume"
    if t.run_id not in runs:
        return Verdict.RUN_GONE, f"run {t.run_id} is not in the durable run state"
    run = runs[t.run_id]
    condition = str(_field(run, "condition") or "").upper()

    # 5. Terminal: nothing left to resume.
    if condition in TERMINAL:
        return Verdict.RUN_NOT_PARKED, f"run {t.run_id} is {condition}, which is terminal"

    # 6. Running: resuming would duplicate accepted work.
    if condition == "ACTIVE":
        return (Verdict.ALREADY_RUNNING,
                f"run {t.run_id} is ACTIVE; resuming it would duplicate its work")

    # The run record must belong to this programme generation as well.
    run_epoch = _field(run, "program_epoch")
    if not isinstance(run_epoch, int) or isinstance(run_epoch, bool):
        return (Verdict.NEEDS_OVERRIDE,
                (f"run {t.run_id} records no program epoch ({run_epoch!r}); "
                 f"which plan it belongs to has to be decided, not guessed"))
    if run_epoch != program_epoch:
        return (Verdict.SUPERSEDED_EPOCH,
                (f"run {t.run_id} belongs to program epoch {run_epoch}, "
                 f"this program is at {program_epoch}"))

    # 7. A finding is not a wait. Clearing it takes a recorded override.
    if capability_blocked(run):
        return (Verdict.NEEDS_OVERRIDE,
                (f"run {t.run_id} is BLOCKED on a capability violation "
                 f"({_field(run, 'blocked_reason')!r}); clearing it takes `hoh unblock`, "
                 f"not a timer"))
    if condition == "BLOCKED":
        kind = _field(run, "blocked_kind")
        if kind not in WAITING_KINDS:
            return (Verdict.NEEDS_OVERRIDE,
                    (f"run {t.run_id} is BLOCKED ({kind or 'unclassified'}); "
                     f"that is waiting for a decision, not for a time"))
    elif condition != "PARKED":
        return (Verdict.RUN_NOT_PARKED,
                f"run {t.run_id} is {condition or 'in no recorded condition'}, not parked")

    # 8. Not yet. The boundary second counts as due, like `Timer.is_due`.
    if t.due_at > now:
        return Verdict.NOT_DUE, f"due at {t.due_at}, now is {now}"
    retry_after = _field(run, "retry_after")
    if retry_after:
        try:
            check_time(retry_after)
        except ValueError:
            return (Verdict.NEEDS_OVERRIDE,
                    f"run {t.run_id} has an unreadable retry_after {retry_after!r}")
        if retry_after > now:
            return (Verdict.NOT_DUE,
                    f"the timer is due, but run {t.run_id} says retry after {retry_after}")

    # 9. A due timer, a genuinely parked run, the current epoch.
    return Verdict.RESUME, f"due at {t.due_at}; run {t.run_id} is parked at epoch {program_epoch}"


def due_resumptions(timers: Sequence[Timer], *, runs: Mapping[str, Any],
                    now: datetime | str, program_epoch: int,
                    killed: Iterable[str] = ()) -> list[Resumption]:
    """One decision per timer, refusals included, ordered by (due_at, timer_id).

    `runs` maps run_id to what the durable state says about the run: a
    mapping or an object with `condition` (PARKED, BLOCKED, ACTIVE, ACCEPTED,
    STOPPED; CANCELLED and FAILED count as terminal), `program_epoch`,
    `retry_after`, and optionally `blocked_kind` and `blocked_reason`.

    Due is ``timer.due_at <= now``, the rule `Timer.is_due` uses on the
    `check_time` form: the boundary is **inclusive**, so a timer due at
    12:00:00Z resumes at 12:00:00Z and is NOT_DUE at 11:59:59Z.

    Where several rules apply, the first in this fail-closed order decides:
    KILLED, SUPERSEDED_EPOCH (timer epoch behind or ahead), SUPERSEDED_TIMER
    (not armed, or not the newest armed timer for its run), RUN_GONE,
    RUN_NOT_PARKED (terminal), ALREADY_RUNNING, then the run's own epoch
    (SUPERSEDED_EPOCH), NEEDS_OVERRIDE (a capability violation, or a BLOCKED
    run not waiting for a time), NOT_DUE, and finally RESUME.
    """
    moment = normalise_now(now)
    kill = frozenset(killed)
    winners = _newest(timers, program_epoch)
    out = []
    for t in timers:
        try:
            check_time(t.due_at)
        except ValueError:
            verdict, why = (Verdict.NEEDS_OVERRIDE,
                            (f"timer due_at {t.due_at!r} is not a UTC timestamp; "
                             f"when it is due cannot be read"))
        else:
            verdict, why = _decide(t, runs=runs, now=moment, program_epoch=program_epoch,
                                   killed=kill, winners=winners)
        out.append(Resumption(run_id=t.run_id, timer_id=t.timer_id, verdict=verdict,
                              why=why, due_at=t.due_at, program_epoch=program_epoch,
                              timer_epoch=t.program_epoch))
    return sorted(out, key=lambda r: (r.due_at, r.timer_id))


def next_wakeup(timers: Sequence[Timer], *, now: datetime | str,
                program_epoch: int) -> datetime | None:
    """When a supervisor next has something to look at, or None.

    The earliest `due_at` strictly after `now`, among ARMED timers at exactly
    `program_epoch` that no newer timer for the same run supersedes. A timer
    already due is not a wake-up time -- it is work for `due_resumptions` now.
    A superseded timer must not keep a supervisor awake. Returned as an aware
    UTC datetime.
    """
    moment = normalise_now(now)
    winners = _newest(timers, program_epoch)
    live = []
    for t in timers:
        if t.status is not TimerStatus.ARMED or t.program_epoch != program_epoch:
            continue
        if t.run_id is not None and winners.get(t.run_id) != t.timer_id:
            continue
        try:
            check_time(t.due_at)
        except ValueError:
            continue
        if t.due_at > moment:
            live.append(t.due_at)
    return _parse(min(live)) if live else None


def consume(store: TimerStore, resumption: Resumption, *, now: datetime | str) -> Timer:
    """Fire the timer behind a RESUME decision, exactly once.

    Refuses (ResumptionError, a TimerStoreError) a decision that is not
    RESUME, a timer the store does not hold, one it holds in any state but
    ARMED -- which is what makes a second `consume` of the same decision raise
    instead of firing again, the normal case after a crash-and-restart -- one
    that no longer matches the decision (run or epoch), and one that is not
    yet due at `now`. A refusal leaves the store untouched.
    """
    if resumption.verdict is not Verdict.RESUME:
        raise ResumptionError(
            f"refusing to consume timer {resumption.timer_id}: the decision was "
            f"{resumption.verdict.value} ({resumption.why})")
    moment = normalise_now(now)
    held = [t for t in store.read() if t.timer_id == resumption.timer_id]
    if not held:
        raise ResumptionError(f"the store holds no timer {resumption.timer_id}")
    t = held[0]
    if t.status is not TimerStatus.ARMED:
        raise ResumptionError(
            f"timer {t.timer_id} is {t.status.value}, not armed; it was already "
            f"consumed or withdrawn (fired_at={t.fired_at}, disarmed_at={t.disarmed_at})")
    if t.run_id != resumption.run_id or t.program_epoch != resumption.timer_epoch:
        raise ResumptionError(
            f"timer {t.timer_id} no longer matches the decision: store has run "
            f"{t.run_id} at epoch {t.program_epoch}, decision has run "
            f"{resumption.run_id} at epoch {resumption.timer_epoch}")
    if not t.is_due(moment):
        raise ResumptionError(f"timer {t.timer_id} is due at {t.due_at}, not at {moment}")
    return store.fire(t.timer_id)
