"""The resume loop: the consumer that O211 said did not exist.

`hoh.resumption` decides, as a pure function, which parked branch a due timer
should wake and why everything else should not. O211 stayed open because
nothing *acted* on that decision:

    There is no supervisor loop, no CLI command, no controller path that calls
    `due_resumptions` and acts on a `RESUME`.

This module is that consumer. It is called by `hoh resume-quota` -- by hand,
from the systemd timer in `ops/`, or from a Herdr action -- and it does, in one
pass, for every run and every project under a root:

    read durable timers and durable state
    -> `due_resumptions` decides (kill list, epoch, parked-ness, due time)
    -> only the blocked class this consumer owns is resumed
    -> the timer is consumed exactly once, under a lock
    -> the branch is unblocked, with the reason on the record
    -> for a project with a registered driver: a *new governed master*
       acquires authority (a new epoch) and drives it on

Three rules decide what it may touch, and each is a refusal first:

* **Only its own class.** `resume-quota` owns `usage_limit` and the other
  automatic waits of `hoh.failures.AUTOMATIC_KINDS` (`QUOTA_CLASSES`), and
  nothing else. A branch parked by the autonomy contract, an operator-required
  failure (an expired credential never wakes on a timer), or a question collected for the
  digest, is also a timed wait -- and it is somebody else's: resuming it here
  would answer a question nobody answered. Such a decision is reported as
  `CLASS_NOT_OWNED` and its timer stays armed for its own consumer.
* **A finding is not a wait.** A capability violation never resumes on a
  timer; `hoh.resumption` already says NEEDS_OVERRIDE, and this module never
  second-guesses a refusal. It does not rely on that alone: every place below
  that lifts a run's block (the timer, the `retry_after` fallback, the storage
  recheck) first asks `stages.violation_unresolved` and refuses a held run,
  whatever decided it was due and whatever `classes` the caller passed.
* **Authority before action.** A project is driven only by a session that
  acquired the lease; a live master elsewhere means this pass does nothing to
  that project (its own loop will see its own timer), and a stale epoch cannot
  write because the store refuses it.

* **A park is resumed only if it is the park the receipts say it is (P2-43c).**
  Before a provider wait of `QUOTA_CLASSES` is lifted -- by a timer, by the
  `retry_after` fallback, and at the project level before a node whose run is
  still parked is resumed -- `hoh.dispatchrecovery.verify_binding` recomputes
  the park's id from the live state, the pinned contract and the declared
  master and compares it with the receipt. A stale or foreign park, a master the
  project's epoch chain never held, a park already resumed (its claim) and a
  governed park without its receipt are `refused`, state and timer untouched; a
  live master holding the project means `held_by_live_master` (a consumer does
  not take work a live master has); a park of slice a (no receipt, no master)
  is resumed as before and says `LEGACY_UNBOUND`. A resume writes its claim
  after the lift. When the attempts run out the loop appends a
  `RETRIES_EXHAUSTED` escalation (once per park) and reports `given_up`.

* **Runs of one account wake together (P3-08).** Before a run of
  `QUOTA_CLASSES` is lifted -- by a timer or by the `retry_after` fallback, at
  the run level and, for a project node, after its run's park has verified --
  `hoh.health.ProviderHealthSentinel` is asked. When other runs of the same
  provider, account and model state a limit that is still pending and whose
  park still verifies (a governed run's only when it is deferred to a live,
  current master), the run or node is `held_for_provider` until the latest such
  reset: state, timer, receipts and project untouched -- no master is acquired
  for it, said in the record, and the decision is taken again on the next pass.
  The refusals above come first and are not softened; a sentinel that cannot
  read says so (`provider_health`) and the run is lifted as before.

* **A declared fallback does not wait for the reset (P3-41 slice 3).** A run of
  `QUOTA_CLASSES` whose park verifies and whose stated reset has not come is looked at once more
  when its pinned routing policy (`<run>/routing.json`) declares a fallback for the parked role:
  `routing.record_park_failover` decides from the verified health of the other runs and this
  park, and records the decision *before* anything is lifted. The run is then lifted exactly as
  a due one is (one resume attempt, a claim, the unparked state before anything else), its timer
  is withdrawn, and the next `hoh run` builds its dispatcher on the fallback until the record's
  `return_at`. Where no declared fallback is healthy, or anything cannot be verified, the park
  waits for its reset as before (the record says `failover_not_decided` and why): a provider stop
  is never a halt. A failure that is not a usage limit (a rate limit, an outage, a network failure)
  fails over only when at least `routing.MIN_FAILOVER_WAIT_SECONDS` of its wait are left: a blip waits
  and spends no record. At the project level a node whose run was lifted this way is lifted with it.

**The crash invariant**, the same on both sides of a wait:

    a parked state is never durable without its wake-up;
    a wake-up may be durable without a parked state.

Parking arms the timer *before* persisting the parked state
(`ProjectController._settle`); resuming persists the unparked state *before*
consuming the timer (here). A crash anywhere therefore leaves at worst an
orphan timer, which `due_resumptions` refuses (RUN_NOT_PARKED /
ALREADY_RUNNING) -- never a parked branch that nothing will wake. And should a
parked `usage_limit` branch ever exist with no armed timer anyway (a timer
disarmed by hand, a store restored from backup), its own `retry_after` is the
supported fallback: it is resumed as `resumed_without_timer`, at the run level
and the project level alike.

Legacy: a run blocked on `usage_limit` before timers were armed for quota
waits has no timer. It is resumed exactly as `hoh resume-quota` always did --
by its own `retry_after` -- and reported as `resumed_without_timer`, so the
two paths stay distinguishable in the output.

**Storage pressure (STORAGE-B3, P2-40)** is a second owned class with its own
consumer, `resume_storage` (`hoh resume-storage`); the quota functions keep
`QUOTA_CLASSES` and report a `storage_pressure` wait `CLASS_NOT_OWNED`, and
`resume_storage` touches nothing but `storage_pressure`. It resumes a parked
run or project node only when a due `STORAGE_RECHECK` timer speaks for it
*and* the observed state is better than CRITICAL *and* a `role_run` together with
the iteration's temporary workspace would be admitted now
(`StorageAdmission.would_admit`, `_capacity`); otherwise it arms a new timer
first, disarms the superseded one by id, and leaves the branch parked. There
is no `retry_after` fallback for this class: a storage park without an armed
timer is reported, never resumed.
"""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import dispatchrecovery
from .contracts import Condition
from .failures import AUTOMATIC_KINDS
from .resumption import Resumption, ResumptionError, Verdict, consume, due_resumptions, normalise_now
from .timers import TimerStatus, TimerStore, TimerStoreError

#: The class `hoh resume-quota` owns. Everything else is reported, not touched.
QUOTA_CLASSES = frozenset({"usage_limit"}) | AUTOMATIC_KINDS

#: The class `hoh resume-storage` owns (STORAGE-B3). Never resumed by the
#: quota functions, which keep `QUOTA_CLASSES` as their default.
STORAGE_CLASSES = frozenset({"storage_pressure"})

#: File in a project directory that names how to build its launcher and gates.
DRIVER_FILE = "driver.json"


def _now_str(now: datetime | str | None) -> str:
    return normalise_now(now if now is not None else datetime.now(UTC))


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def run_epoch(run_dir: Path) -> int | None:
    """The programme epoch a run belongs to: its pinned contract's, 0 without
    one, None when a pinned contract cannot be read (then nothing guesses)."""
    from .governance import ContractRefused, bind

    try:
        governor = bind(run_dir)
    except ContractRefused:
        return None
    return 0 if governor is None else int(governor.contract.program_epoch)


def run_view(state: Any, program_epoch: int | None) -> dict:
    return {"condition": state.condition.value, "blocked_kind": state.blocked_kind,
            "blocked_reason": state.blocked_reason,
            "retry_after": _timer_form(state.retry_after),
            "program_epoch": program_epoch}


def _timer_form(stamp: str | None) -> str | None:
    """The launcher's timer form (rounded up to the whole second, O237), except
    that an unreadable or naive stamp comes back unchanged: resumption refuses it."""
    if not stamp:
        return None
    # Imported here, not at the top: the launcher pulls in the orchestrator.
    from .launcher import _timer_form as timer_form

    return timer_form(stamp) or stamp


def resume_runs(root: Path | str, *, now: datetime | str | None = None,
                classes: frozenset[str] = QUOTA_CLASSES,
                only: str | None = None, killed: frozenset[str] = frozenset()
                ) -> list[dict]:
    """One pass over the runs under `root`. Returns one record per decision."""
    from . import quota, stages
    from .store import LockBusy, RunStore, StoreError, list_runs

    from .health import ProviderHealthSentinel

    root = Path(root).expanduser()
    moment = _now_str(now)
    when = _moment(now)
    sentinel = ProviderHealthSentinel(root, now=when)
    timers_store = TimerStore(root)
    out: list[dict] = []
    ids = [only] if only else list(list_runs(root))
    with _flock(root / ".resume.lock"):
        try:
            timers = timers_store.read()
        except TimerStoreError as exc:
            # Reported and survived: one unreadable store must not stop the
            # pass for every project behind it (review A4, G4). Runs without
            # a readable timer store are left to the legacy retry_after path.
            out.append({"scope": "runs", "target": str(root), "action": "skipped",
                        "why": f"timer store unreadable: {exc}"[:200]})
            timers = []
        for rid in ids:
            store = RunStore(root, rid)
            try:
                with store.lock():
                    st = store.read_state()
                    epoch = run_epoch(store.dir)
                    armed = [t for t in timers if t.run_id == rid
                             and t.status is TimerStatus.ARMED]
                    if armed and epoch is None:
                        out.append({"scope": "run", "target": rid, "action": "refused",
                                    "verdict": Verdict.NEEDS_OVERRIDE.value,
                                    "why": "the run's pinned contract cannot be read, so "
                                           "which programme epoch it belongs to is "
                                           "unknown; nothing is resumed on a guess"})
                        continue
                    decisions = due_resumptions(
                        armed, runs={rid: run_view(st, epoch)}, now=moment,
                        program_epoch=epoch if epoch is not None else 0,
                        killed=killed)
                    def failover(timer_id, store=store, st=st):
                        try:
                            return _failover_run(store, st, timer_id=timer_id,
                                                 timers_store=timers_store, when=when,
                                                 stages=stages, quota=quota)
                        except Exception as exc:          # noqa: BLE001 - said, not raised
                            # a defect in the failover must not stop the pass for the runs
                            # behind this one (review A4, G4); the park simply waits
                            return {"failover_not_decided": f"internal error ({type(exc).__name__})"}
                    for d in decisions:
                        rec = {"scope": "run", "target": rid, "timer_id": d.timer_id,
                               "verdict": d.verdict.value, "why": d.why}
                        if d.resumes and st.blocked_kind not in classes:
                            rec["action"] = "class_not_owned"
                            rec["why"] = (f"{st.blocked_kind!r} is not a class this consumer "
                                          f"owns ({sorted(classes)}); the timer stays armed")
                        elif d.resumes:
                            rec.update(_resume_run(store, st, d, timers_store, moment,
                                                   stages, quota, sentinel))
                        else:
                            rec["action"] = "refused"
                            if d.verdict is Verdict.NOT_DUE and st.blocked_kind in classes:
                                rec.update(failover(d.timer_id))
                        out.append(rec)
                    if not armed and st.condition is Condition.BLOCKED \
                            and st.blocked_kind in classes:
                        if rid in killed:
                            # Said, not silent (review A6, 2).
                            out.append({"scope": "run", "target": rid, "timer_id": None,
                                        "verdict": "KILLED", "action": "refused",
                                        "why": f"run {rid} is on the kill list; the "
                                               "retry_after fallback never resumes it"})
                        else:
                            out.append(_legacy_run(store, st, rid, stages, quota,
                                                   sentinel, failover))
            except (LockBusy, StoreError) as exc:
                out.append({"scope": "run", "target": rid, "action": "skipped",
                            "why": str(exc)[:160]})
    return out


def _hold_refusal(st, rec: dict, by: str) -> dict:
    """Refuse a run held by a capability violation, in `due_resumptions`' words.

    Every automatic way back asks `stages.violation_unresolved` itself, so no
    path depends on the class filter or on rule 7 of `due_resumptions` alone.
    The timer, if any, stays armed and the state is not written.
    """
    first = (st.blocked_reason or st.stop_reason or "").strip().splitlines()
    rec.update(verdict=Verdict.NEEDS_OVERRIDE.value, action="refused",
               why=f"run {st.run_id} is held by a capability violation "
                   f"({first[0][:160] if first else 'no reason recorded'}); clearing "
                   f"it takes `hoh unblock`, not {by}")
    return rec


def _check_park(store, st):
    """The binding check of a provider wait; None for a block of another class
    (not a park this module's receipts describe)."""
    if st.blocked_kind not in QUOTA_CLASSES:
        return None
    return dispatchrecovery.verify_binding(store.dir, st)


def _binding_fields(check) -> dict:
    if check is None:
        return {}
    out = {"binding": check.verdict.value}
    if check.park_id:
        out["park_id"] = check.park_id
    return out


def _barred(check) -> dict | None:
    """The result record of a park that must not be resumed now, or None."""
    if check is None or check.resumes:
        return None
    action = ("held_by_live_master"
              if check.verdict is dispatchrecovery.ParkVerdict.DEFERRED else "refused")
    return {"action": action, "why": check.reason, **_binding_fields(check)}


def _binding_note(check) -> str:
    if check is None:
        return ""
    if check.verdict is dispatchrecovery.ParkVerdict.LEGACY_UNBOUND:
        return " (legacy park: no routing receipt)"
    return f" (park {(check.park_id or '')[:12]} verified: {check.reason})"


def _exhausted(store, st, check, rec: dict) -> dict:
    """`given_up`, with the escalation receipt of a park a receipt describes."""
    rec.update(action="given_up",
               why=f"{st.resume_attempts} unsuccessful resume attempts; "
                   "'hoh unblock' is the captain's decision",
               **_binding_fields(check))
    if check is not None and check.record is not None:
        try:
            esc = dispatchrecovery.escalate_exhaustion(store.dir, check)
            rec["escalation"] = {"escalation": esc.escalation, "park_id": esc.park_id}
        except Exception as exc:                          # noqa: BLE001 - said, not raised
            rec["escalation"] = f"NOT written ({type(exc).__name__})"
    return rec


def _provider_hold(sentinel, run_id: str) -> tuple[dict | None, dict]:
    """(the result record of a run the sentinel says to hold, or None; the fields a
    run that is let through carries).

    A sentinel that could not read says so in the record of the run it lets
    through (`provider_health`), so a pass that consulted nothing is not
    mistaken for one that consulted and found nothing."""
    if sentinel is None:
        return None, {}
    verdict = sentinel.consult(run_id)
    if verdict.action.value == "HOLD":
        return {"action": "held_for_provider", "why": verdict.reason,
                "until": verdict.not_before, "evidence": list(verdict.evidence)}, {}
    if not verdict.available:
        return None, {"provider_health": f"not consulted: {verdict.reason}"[:200]}
    return None, {}


def _claim(store, st, check) -> dict:
    """The receipt of the resume, written after the park was lifted."""
    if check is None or check.record is None:
        return {}
    try:
        dispatchrecovery.claim_park(store.dir, check, resumer="hoh resume-quota (runs)",
                                    attempt=st.resume_attempts)
    except Exception as exc:                              # noqa: BLE001 - said, not raised
        return {"claim": f"NOT written ({type(exc).__name__})"}
    return {"claim": "written"}


def _pinned_policy(run_dir: Path):
    """`(policy, why)`: the routing policy a run is bound to (`<run>/routing.json`), or None and
    why not -- no pin is the empty string, anything else is a pin that cannot be applied."""
    from . import routing
    from .cli import RoutingPinRefused, _routing_decide

    try:
        policy, _pin = _routing_decide(run_dir, None)
    except (RoutingPinRefused, routing.RoutingRefused) as exc:
        return None, f"the routing policy of the run cannot be read: {str(exc)[:160]}"
    return policy, ""


def _failover_run(store, st, *, timer_id, timers_store, when, stages, quota) -> dict:
    """P3-41 slice 3: a verified, not yet due provider wait of a run whose pinned policy declares
    a fallback for the parked role is lifted now, on that fallback. Returns the fields of the
    result record: `{}` for a run this does not apply to (no pinned policy, a policy without
    fallbacks, a hold), `failover_not_decided` and the reason for a run that waits for its reset,
    or the resume (`action: resumed_failover`).

    The order is the crash invariant's: the decision is recorded first (create-only, and counted
    even if the lift never happens), the unparked state is persisted next, the timer is withdrawn
    last. One lift is one resume attempt (`MAX_RESUME_ATTEMPTS` bounds the unproductive ones)."""
    from . import routing

    if stages.violation_unresolved(st):
        return {}
    policy, why = _pinned_policy(store.dir)
    if policy is None:
        return {"failover_not_decided": why} if why else {}
    if not policy.fallbacks:
        return {}
    if st.resume_attempts >= quota.MAX_RESUME_ATTEMPTS:
        return {"failover_not_decided": f"{st.resume_attempts} resume attempts are used"}
    check = _check_park(store, st)
    barred = _barred(check)
    if barred is not None or check is None or check.record is None:
        return {"failover_not_decided": (barred or {}).get("why")
                or "no receipt describes this park"}
    try:
        made = routing.record_park_failover(policy, run_dir=store.dir, receipt=check.record,
                                            now=when)
    except routing.RoutingRefused as exc:
        return {"failover_not_decided": str(exc)[:200], **_binding_fields(check)}
    st.resume_attempts += 1
    stages.unblock(st, f"resumed by the resume loop on the declared fallback "
                       f"{made.route.provider}/{made.route.model} (park failover {made.number}; "
                       f"the declared route again from {made.return_at}), attempt "
                       f"{st.resume_attempts}{_binding_note(check)}")
    store.write_state(st)
    rec = {"action": "resumed_failover", "attempt": st.resume_attempts,
           "route": {"provider": made.route.provider, "model": made.route.model},
           "failover_record": routing.PARK_FAILOVER_FILE.format(n=made.number),
           "return_at": made.return_at,
           "next_step": f"hoh --root {store.root} run {store.run_id}",
           **_binding_fields(check), **_claim(store, st, check)}
    if timer_id is not None:
        try:
            timers_store.disarm(timer_id, f"the run was resumed on a declared fallback "
                                          f"(park failover {made.number})")
        except (TimerStoreError, OSError) as exc:
            rec["timer"] = f"left armed: {exc}"
    return rec


def _resume_run(store, st, d: Resumption, timers_store, moment, stages, quota,
                sentinel=None) -> dict:
    if stages.violation_unresolved(st):
        return _hold_refusal(st, {}, "a timer")
    check = _check_park(store, st)
    barred = _barred(check)
    if barred is not None:
        return barred
    if st.resume_attempts >= quota.MAX_RESUME_ATTEMPTS:
        return _exhausted(store, st, check, {})
    held, said = _provider_hold(sentinel, store.run_id)
    if held is not None:
        return {**held, **_binding_fields(check)}
    st.resume_attempts += 1
    stages.unblock(st, f"resumed by the resume loop: timer {d.timer_id} due "
                       f"{d.due_at}, attempt {st.resume_attempts}{_binding_note(check)}")
    store.write_state(st)
    rec = {"action": "resumed", "attempt": st.resume_attempts,
           "next_step": f"hoh --root {store.root} run {store.run_id}",
           **said, **_binding_fields(check), **_claim(store, st, check)}
    try:
        consume(timers_store, d, now=moment)
    except ResumptionError as exc:
        # The branch is already resumed; the timer stays armed as an orphan,
        # which every later pass refuses (the run is no longer parked).
        rec["timer"] = f"left armed: {exc}"
    return rec


def _legacy_run(store, st, rid, stages, quota, sentinel=None, failover=None) -> dict:
    rec = {"scope": "run", "target": rid, "timer_id": None,
           "verdict": "LEGACY_RETRY_AFTER"}
    if stages.violation_unresolved(st):
        # The one resume path no timer rule covers: a legacy state (kind
        # `usage_limit`, a reason that names the violation) or a caller that
        # widened `classes` must not lift a hold with its retry_after.
        return _hold_refusal(st, rec, "a retry_after")
    check = _check_park(store, st)
    barred = _barred(check)
    if barred is not None:
        rec.update(barred)
        return rec
    if st.resume_attempts >= quota.MAX_RESUME_ATTEMPTS:
        return _exhausted(store, st, check, rec)
    if not quota.is_due(st.retry_after):
        rec.update(action="waiting", why=f"retry after {st.retry_after}")
        if failover is not None:
            rec.update(failover(None))
        return rec
    held, said = _provider_hold(sentinel, rid)
    if held is not None:
        rec.update(held, **_binding_fields(check))
        return rec
    st.resume_attempts += 1
    stages.unblock(st, f"quota retry {st.resume_attempts} (retry_after, no timer)"
                       f"{_binding_note(check)}")
    store.write_state(st)
    rec.update(action="resumed_without_timer", attempt=st.resume_attempts,
               next_step=f"hoh --root {store.root} run {rid}",
               **said, **_binding_fields(check), **_claim(store, st, check))
    return rec


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

def node_view(node: Any, program_generation: int) -> dict:
    """A project node in the shape `due_resumptions` reads for a run.

    A faithful translation, not an approximation: RUNNING is ACTIVE (resuming
    it would duplicate work), MERGED and ABANDONED are terminal, BLOCKED
    carries its own `blocked_kind`/`retry_after`, and anything else is not
    parked.
    """
    from .project import Lifecycle

    lc = node.lifecycle
    condition = {Lifecycle.RUNNING: "ACTIVE", Lifecycle.BLOCKED: "BLOCKED",
                 Lifecycle.MERGED: "STOPPED", Lifecycle.ABANDONED: "STOPPED"
                 }.get(lc, lc.value)
    return {"condition": condition, "blocked_kind": node.blocked_kind,
            "blocked_reason": node.note, "retry_after": node.retry_after,
            "program_epoch": program_generation}


def _run_park_gate(root: Path, node: Any) -> dict | None:
    """The refusal or deferral of a node whose run is still parked on a park that
    does not verify, or None when the node may be resumed.

    Checked before the master is acquired, so a refused park spends no epoch and
    drives nothing. Fail-closed: the only cases that proceed are the ones with
    nothing to check -- a name no run can have, no state file, a run that is not
    BLOCKED any more (the run pass lifted it) and a park of another class. A
    state that exists and cannot be read **here** (damaged, or of an older schema
    that only a locked `RunStore.read_state` migrates) is a refusal: the node
    stays BLOCKED and the next pass, after the run pass has read the state under
    its lock, decides. The read is a plain parse of `state.json`: this gate
    writes nothing, not even a migration.
    """
    from .contracts import RunState
    from .store import RunStore, StoreError

    try:
        store = RunStore(root, node.run_id or node.id)
    except StoreError:
        return None                       # not a name a run can have
    if not os.path.lexists(store.state_path):
        return None                       # no run record: a project-only park
    try:
        st = RunState.model_validate_json(store.state_path.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001 - fail closed
        return {"action": "refused", "binding": "REFUSED",
                "why": f"the state of run {store.run_id} cannot be read here "
                       f"({type(exc).__name__}); the node is not resumed on a guess -- "
                       "the run pass reads it under its lock, and the next pass decides"}
    if st.condition is not Condition.BLOCKED:
        return None
    return _barred(_check_park(store, st))


DriveFn = Callable[[Any, Any], Any]


def load_driver(project_dir: Path) -> Callable[[Any], tuple[Any, Any]] | None:
    """The launcher/gates factory registered for a project, or None.

    `driver.json`: {"factory": "module:callable", "path": "<dir for sys.path>"}.
    The factory is called with the `ProjectStore` and returns
    `(launcher, gates)`. It is operator configuration, stored next to the state
    it drives, and it runs with the operator's own permissions -- the same
    trust as the checkout it names.
    """
    f = project_dir / DRIVER_FILE
    if not f.is_file():
        return None
    spec = json.loads(f.read_text(encoding="utf-8"))
    extra = spec.get("path")
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)
    module, _, attr = str(spec["factory"]).partition(":")
    return getattr(importlib.import_module(module), attr)


def resume_projects(root: Path | str, *, now: datetime | str | None = None,
                    classes: frozenset[str] = QUOTA_CLASSES, drive: bool = False,
                    session_id: str | None = None,
                    killed: frozenset[str] = frozenset(),
                    authority_factory: Callable[..., Any] | None = None) -> list[dict]:
    """One pass over the projects under `root`."""
    from .health import ProviderHealthSentinel
    from .projectstore import list_projects

    root = Path(root).expanduser()
    moment = _now_str(now)
    sentinel = ProviderHealthSentinel(root, now=_moment(now))
    out: list[dict] = []
    for pid in list_projects(root):
        # One project's damage -- an unreadable lease, a driver that raises --
        # must not stop the pass for the others (review A, P3).
        try:
            out.extend(_resume_one_project(
                root, pid, moment=moment, classes=classes, drive=drive,
                session_id=session_id, killed=killed,
                authority_factory=authority_factory, sentinel=sentinel))
        except Exception as exc:                          # noqa: BLE001 - reported
            out.append({"scope": "project", "target": pid, "action": "skipped",
                        "why": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def _failover_lifted(root: Path, node: Any) -> bool:
    """P3-41 slice 3: is this parked node's run one that the resume loop already lifted onto a
    declared fallback -- the run is no longer parked, the newest park failover record names the
    run's newest park, and that park was claimed? The node's own timer is not due yet; the run
    is on its way, and the node goes with it. Reads only; anything unreadable is False."""
    from . import routing
    from .health import _state_of
    from .store import RunStore

    try:
        store = RunStore(root, node.run_id or node.id)
        latest = routing.latest_park_failover(store.dir)
        if latest is None:
            return False
        st = _state_of(store)
        if st is None or st.condition is Condition.BLOCKED:
            return False
        records, _ = dispatchrecovery.read_records(store.dir)
        parks = [r for r in records if isinstance(r, dispatchrecovery.RoutingDecision)]
        return (bool(parks) and parks[-1].park_id == latest["park_id"]
                and os.path.lexists(dispatchrecovery.claim_path(store.dir, latest["park_id"])))
    except Exception:                                    # noqa: BLE001 - unreadable: not lifted
        return False


def _resume_one_project(root: Path, pid: str, *, moment: str, classes, drive,
                        session_id, killed, authority_factory, sentinel=None) -> list[dict]:
    from .orchestrator import disarm_stale_waits, node_timer_key, write_lock
    from .projectstore import ProjectStore
    from .store import LockBusy, StoreError
    from .succession import LeaseError, LeaseHeld, MasterAuthority, StaleMaster

    out: list[dict] = []
    if True:                                   # keeps the per-project body's shape
        store = ProjectStore(root, pid)
        try:
            state = store.read_state()
        except StoreError as exc:
            out.append({"scope": "project", "target": pid, "action": "skipped",
                        "why": str(exc)[:160]})
            return out
        timers_store = TimerStore(store.dir)
        # Only armed timers are asked about: a fired or disarmed timer is
        # history, and reporting it as "superseded" on every pass forever
        # would bury the decisions that matter.
        timers = [t for t in timers_store.read() if t.status is TimerStatus.ARMED]
        views = {node_timer_key(n.id): node_view(n, state.program_generation)
                 for n in state.nodes}
        decisions = due_resumptions(timers, runs=views, now=moment,
                                    program_epoch=state.program_generation,
                                    killed=killed)
        timed = {t.run_id for t in timers}
        orphans = [n for n in state.nodes
                   if n.lifecycle.value == "BLOCKED" and n.blocked_kind in classes
                   and node_timer_key(n.id) not in timed and n.id not in killed]
        if not decisions and not orphans:
            return out
        # a node not due whose run was already lifted onto a declared fallback goes with it
        lifted = {n.id for n in state.nodes
                  if n.lifecycle.value == "BLOCKED" and n.blocked_kind in classes
                  and n.id not in killed and node_timer_key(n.id) in timed
                  and _failover_lifted(root, n)}
        by_key = {node_timer_key(n.id): n for n in state.nodes}
        todo: list[Resumption] = []
        fallback: list[str] = []
        for n in orphans:
            rec = {"scope": "project", "target": pid, "node": n.id, "timer_id": None,
                   "verdict": "PARKED_WITHOUT_TIMER"}
            if n.retry_after and n.retry_after <= moment:
                barred = _run_park_gate(root, n)
                held, said = ((None, {}) if barred is not None
                              else _provider_hold(sentinel, n.run_id or n.id))
                if barred is not None:
                    rec.update(barred)
                elif held is not None:
                    rec.update(held)
                else:
                    rec.update(action="pending", why=f"no armed timer; retry_after "
                               f"{n.retry_after} has passed (fallback)", **said)
                    fallback.append(n.id)
            else:
                rec.update(action="waiting", why=f"no armed timer; retry_after "
                           f"{n.retry_after or 'missing'}")
            out.append(rec)
        for d in decisions:
            node = by_key.get(d.run_id or "")
            rec = {"scope": "project", "target": pid, "node": node.id if node else None,
                   "timer_id": d.timer_id, "verdict": d.verdict.value, "why": d.why}
            if d.resumes and node is not None and node.blocked_kind not in classes:
                rec["action"] = "class_not_owned"
                rec["why"] = (f"{node.blocked_kind!r} is not a class this consumer owns "
                              f"({sorted(classes)}); the timer stays armed")
            elif d.resumes:
                barred = _run_park_gate(root, node) if node is not None else None
                held, said = ((None, {}) if barred is not None or node is None
                              else _provider_hold(sentinel, node.run_id or node.id))
                if barred is not None:
                    rec.update(barred)
                elif held is not None:
                    rec.update(held)
                else:
                    rec["action"] = "pending"
                    rec.update(said)
                    todo.append(d)
            elif d.verdict is Verdict.NOT_DUE and node is not None and node.id in lifted:
                rec.update(verdict="LIFTED_BY_FAILOVER", action="pending",
                           why=f"run {node.run_id or node.id} was resumed on a declared "
                               "fallback before its reset; the node goes with it")
                fallback.append(node.id)
            else:
                rec["action"] = "refused"
            out.append(rec)
        if not todo and not fallback:
            return out

        sid = session_id or f"resume-{moment.replace(':', '')}-{os.getpid()}"
        make = authority_factory or MasterAuthority
        authority = make(store.dir, sid, serialize=lambda: write_lock(store))
        pending = [r for r in out if r.get("target") == pid and r.get("action") == "pending"
                   and r.get("timer_id") is not None
                   and r.get("verdict") != "LIFTED_BY_FAILOVER"]
        pending_fb = [r for r in out if r.get("target") == pid
                      and r.get("action") == "pending"
                      and (r.get("timer_id") is None
                           or r.get("verdict") == "LIFTED_BY_FAILOVER")]
        try:
            authority.acquire(program_generation=state.program_generation,
                              note="resume loop: a due provider wait")
        except (LeaseHeld, LockBusy, LeaseError) as exc:
            for r in pending + pending_fb:
                r.update(action="held_by_live_master"
                         if isinstance(exc, (LeaseHeld, LockBusy)) else "refused",
                         why=str(exc)[:200])
            return out
        handed_over = False
        try:
            from .project import Lifecycle

            try:
                with write_lock(store):
                    st = store.read_state()
                    to_consume = []
                    for d, r in list(zip(todo, pending)) + [(None, r) for r in pending_fb]:
                        node = st.node(r["node"])
                        if node is None or node.blocked_kind not in classes \
                                or node.lifecycle is not Lifecycle.BLOCKED:
                            r.update(action="refused", why="the node changed under the pass")
                            continue
                        node.lifecycle = Lifecycle.READY
                        failed_over = r.get("verdict") == "LIFTED_BY_FAILOVER"
                        node.note = (f"resumed by {sid} at {moment}: "
                                     + (f"timer {d.timer_id} (due {d.due_at})" if d
                                        else "its run was resumed on a declared fallback "
                                             f"(retry_after {node.retry_after})" if failed_over
                                        else f"retry_after {node.retry_after}, no timer"))
                        node.blocked_kind = None
                        node.retry_after = None
                        r.update(action=("resumed" if d else "resumed_failover" if failed_over
                                         else "resumed_without_timer"),
                                 master=sid, epoch=authority.epoch)
                        if d is not None:
                            to_consume.append((d, r))
                    # Unparked state first, then the timers: the crash invariant.
                    authority.stamp(st)
                    store.write_state(st)
                    try:
                        authority.renew()
                    except Exception:                 # noqa: BLE001 - write is durable
                        pass
                    for d, r in to_consume:
                        try:
                            consume(timers_store, d, now=moment)
                        except ResumptionError as exc:
                            r["timer"] = f"left armed: {exc}"
                    # Any other timer of a node that is no longer parked goes too.
                    try:
                        disarm_stale_waits(store.dir, st)
                    except TimerStoreError:
                        pass                          # the state write stands (review A4, S5)
            except (LockBusy, StaleMaster) as exc:
                for r in pending + pending_fb:
                    if r["action"] == "pending":
                        r.update(action="refused", why=str(exc)[:200])
                return out
            if not drive:
                return out
            try:
                factory = load_driver(store.dir)
            except Exception as exc:                      # noqa: BLE001 - reported
                out.append({"scope": "project", "target": pid, "action": "not_driven",
                            "why": f"driver cannot be loaded: {type(exc).__name__}: "
                                   f"{exc}"[:200]})
                return out
            if factory is None:
                out.append({"scope": "project", "target": pid, "action": "not_driven",
                            "why": f"no {DRIVER_FILE}: the branch is READY and waits for a "
                                   "driver; nothing here guesses how to launch it"})
                return out
            handed_over = True
            result = drive_governed(store, authority, factory)
            out.append({"scope": "project", "target": pid, "action": "drove",
                        "master": sid, "epoch": authority.epoch,
                        "halt": result.halt.value, "why": result.reason[:300]})
        finally:
            # Every exit after the acquisition releases, unless the drive owns
            # the lease (it releases itself): a lease left behind reads LOST and
            # would be "recovered" as though its holder had died (review A2, N2).
            if not handed_over:
                _release(authority, "resume pass ended without driving")
    return out


def _release(authority, reason: str) -> None:
    """Release, whatever gets in the way: a failed release must not abort a
    pass that other projects are waiting in (review A2, b)."""
    try:
        authority.release(reason=reason)
    except Exception:                                     # noqa: BLE001 - best effort
        pass


def drive_governed(store, authority, factory, **controller_kw):
    """Drive one project under an acquired authority, and end it orderly.

    Every orderly end releases the lease, whatever the halt -- so that a lease
    found LOST later means exactly one thing: its holder died without saying
    so (killed, crashed, rebooted away, or frozen past its lease). That is the
    only state `recover_projects` acts on. A superseded master has nothing to
    release; its successor holds the lease.
    """
    from .orchestrator import HaltClass, ProjectController, Result

    try:
        launcher, gates = factory(store)
        # P3-20 slice 3: the routing the project declares binds the runs this launcher starts.
        # A project that declares a policy (or is pinned to one) and whose launcher cannot be
        # bound to it halts here, by name, before anything is started.
        from .launcher import ROUTING_PIN_FILE, ROUTING_POLICY_FILE, unbound_preflight_refusal

        project_dir = getattr(store, "dir", None)         # a store without one declares nothing
        bind_routing = getattr(launcher, "bind_routing", None)
        if project_dir is None:
            pass
        elif callable(bind_routing):
            bind_routing(project_dir)
        elif any(os.path.lexists(project_dir / name)
                 for name in (ROUTING_POLICY_FILE, ROUTING_PIN_FILE)):
            declares = os.path.lexists(project_dir / ROUTING_POLICY_FILE)
            raise ValueError(
                f"routing policy refused: this project "
                f"{'declares a routing policy' if declares else 'is bound to a routing policy'}"
                f" ({ROUTING_POLICY_FILE if declares else ROUTING_PIN_FILE}) and its launcher "
                f"({type(launcher).__name__}) cannot route the runs it starts")
        # P3-11 slice 4: the same for the execution preflight. A launcher that binds is bound by
        # `bind_routing` above (the default, the project's requirement or its waiver); one that
        # cannot bind runs on no report unless the project recorded a waiver -- else it halts
        # here, by name, before anything is started.
        if project_dir is not None:
            refusal = unbound_preflight_refusal(launcher, project_dir)
            if refusal is not None:
                raise ValueError(refusal)
        # P2-43b: the runs this master launches are bound to it.
        bind_master = getattr(launcher, "bind_master", None)
        if callable(bind_master) and authority.epoch is not None:
            # A session id that has the shape of a key is refused by every
            # `hoh run` it would be passed to (exit 2, no verdict); say so once,
            # here, as the drive's own halt reason.
            dispatchrecovery.check_session_id(authority.session_id)
            bind_master(authority.session_id, authority.epoch)
        result = ProjectController(store, launcher, gates, authority=authority,
                                   **controller_kw).run()
    except Exception as exc:                              # noqa: BLE001 - classified
        # A driver or launcher that raises is a failure this process observed
        # and survives, not a death: the lease is released like any orderly
        # halt, so the next tick does not read it LOST and burn another epoch
        # on the same exception (review A, P2).
        result = Result(HaltClass.AMBIGUOUS,
                        f"the drive raised {type(exc).__name__}: {exc}"[:400])
    if result.halt is not HaltClass.SUPERSEDED:
        try:
            ws = store.read_state().write_seq
        except Exception:                                 # noqa: BLE001 - best effort
            ws = -1
        _release(authority, f"{DRIVE_END}{result.halt.value} @ws={ws}")
    return result


#: The prefix of every orderly drive release, followed by `<HALT> @ws=<n>`.
DRIVE_END = "orderly end of drive: "


def last_drive_end(reason: str) -> tuple[str, int] | None:
    """(halt, write_seq) of a release written by `drive_governed`, or None."""
    if not reason.startswith(DRIVE_END) or " @ws=" not in reason:
        return None
    halt, _, ws = reason[len(DRIVE_END):].partition(" @ws=")
    try:
        return halt, int(ws)
    except ValueError:
        return None


def recover_projects(root: Path | str, *, session_id: str | None = None,
                     authority_factory: Callable[..., Any] | None = None) -> list[dict]:
    """Replace a master that died, and pick up work nobody is driving.

    The automatic half of DS1 and DS2, plus the case of finding #9: a role run
    that outlived its master. What is read, per project that is not closed and
    registers a driver:

    * the lease: LIVE is never displaced; LOST (holder killed, rebooted away,
      expired) is a death; RELEASED is an orderly end;
    * the verdict of every RUNNING node, and of every READY node that has a
      run -- read-only, through the driver's own launcher, before anything is
      acquired.

    Then: any node IN_FLIGHT -> wait (`waiting_on_run`), no epoch spent. A LOST
    lease -> recover, once; a master that died deserves one successor even if
    the state is unclear. A RELEASED lease -> recover only if there is decisive
    work nobody drives: a RUNNING node whose run has a verdict (accepted,
    rejected, blocked on the provider, never started), or a READY node whose
    run has already accepted. A RELEASED project whose only open work is
    unclear (UNDETERMINED) or waits for a human or a timer is left alone, so an
    orderly halt is never re-driven on every tick. The release reason matters
    in one way only: a release written by a drive carries the write_seq it left
    behind, and a project whose state has not changed since is left alone (and
    reported as such) -- whatever halted that drive would halt the next one.
    Any other release, and any change of state, lets recovery look again, so
    another acquirer cannot strand the project (review A3 F1/F3, A4 G1).

    The driver factory is called before acquiring, to read verdicts; factories
    must therefore be free of side effects at construction.
    """
    from .projectstore import list_projects

    root = Path(root).expanduser()
    out: list[dict] = []
    for pid in list_projects(root):
        try:
            rec = _recover_one(root, pid, session_id=session_id,
                               authority_factory=authority_factory)
        except Exception as exc:                          # noqa: BLE001 - reported
            rec = {"scope": "project", "target": pid, "action": "skipped",
                   "why": f"{type(exc).__name__}: {exc}"[:200]}
        if rec is not None:
            out.append(rec)
    return out


#: Verdicts that settle a RUNNING node when read: driving on them makes progress.
DECISIVE = frozenset({"ACCEPTED", "REJECTED", "PROVIDER_UNAVAILABLE", "NOT_STARTED",
                      "NEEDS_APPROVAL", "BUDGET_EXHAUSTED"})


def _recover_one(root: Path, pid: str, *, session_id, authority_factory) -> dict | None:
    from .orchestrator import RunVerdict, write_lock
    from .project import Lifecycle
    from .projectstore import ProjectStore
    from .store import LockBusy, StoreError
    from .succession import LeaseError, LeaseHeld, MasterAuthority, utc_now

    store = ProjectStore(root, pid)
    try:
        state = store.read_state()
    except StoreError:
        return None
    if state.rc_closed() or not (store.dir / DRIVER_FILE).is_file():
        return None
    make = authority_factory or MasterAuthority
    probe = make(store.dir, "recovery-probe")
    try:
        lease = probe.read_lease()
        holder, why = probe.holder_state(lease, utc_now())
    except LeaseError as exc:
        return {"scope": "project", "target": pid, "action": "refused",
                "why": str(exc)[:200]}
    if holder not in ("LOST", "RELEASED"):
        return None
    candidates = [n for n in state.nodes if n.lifecycle is Lifecycle.RUNNING
                  or (n.lifecycle is Lifecycle.READY and n.run_id)]
    if holder == "RELEASED" and not candidates:
        return None
    try:
        factory = load_driver(store.dir)
    except Exception as exc:                              # noqa: BLE001 - reported
        # Before acquiring: a driver that cannot load must not cost an epoch
        # on every tick, and must not stop the pass for the other projects.
        return {"scope": "project", "target": pid, "action": "refused",
                "why": f"driver cannot be loaded: {type(exc).__name__}: {exc}"[:200]}
    launcher, _ = factory(store)
    verdicts = {n.id: (n, launcher.evaluate(n)) for n in candidates}
    for n, v in verdicts.values():
        if v.verdict is RunVerdict.IN_FLIGHT:
            return {"scope": "project", "target": pid, "action": "waiting_on_run",
                    "node": n.id, "why": v.detail[:200]}
    if holder == "RELEASED":
        # Progress guard (review A4, G1): a drive that ended orderly and left
        # the project exactly as it found it will end the same way again --
        # whatever halted it (an earlier obstacle, an external action, a
        # blocked dependency) is still there. Re-drive only after the state
        # changed, or when the last drive ended waiting for a run that has now
        # finished (checked above: nothing is IN_FLIGHT any more).
        ended = last_drive_end(lease.released_reason) if lease is not None else None
        if ended is not None and ended[1] == state.write_seq \
                and ended[0] != "RUN_IN_FLIGHT":
            # Said, not silent (review A5, M3): the project waits for a change
            # of state -- a person's command, a timer -- or a manual drive.
            return {"scope": "project", "target": pid, "action": "left_alone",
                    "why": f"the last drive ended {ended[0]} at write_seq {ended[1]} and "
                           "nothing has changed since; re-driving would end the same way"}
        decisive = [n.id for n, v in verdicts.values()
                    if (n.lifecycle is Lifecycle.RUNNING and v.verdict.value in DECISIVE)
                    or (n.lifecycle is Lifecycle.READY
                        and v.verdict is RunVerdict.ACCEPTED)]
        if not decisive:
            return None
        why = f"released, and nobody drives decisive work on {', '.join(decisive)}"
    sid = session_id or f"recover-{utc_now().replace(':', '')}-{os.getpid()}"
    authority = make(store.dir, sid, serialize=lambda: write_lock(store))
    try:
        authority.acquire(program_generation=state.program_generation,
                          note=f"recovery: {why}")
    except LeaseHeld as exc:
        return {"scope": "project", "target": pid, "action": "held_by_live_master",
                "why": str(exc)[:200]}
    except LockBusy as exc:
        # Not a live master: the program lock is held -- possibly by a master
        # frozen inside a short critical section such as a merge -- and the
        # next tick tries again (review A2, N3).
        return {"scope": "project", "target": pid, "action": "program_lock_busy",
                "why": str(exc)[:200]}
    except LeaseError as exc:
        return {"scope": "project", "target": pid, "action": "refused",
                "why": str(exc)[:200]}
    result = drive_governed(store, authority, factory)
    return {"scope": "project", "target": pid, "action": "recovered",
            "master": sid, "epoch": authority.epoch, "previous": why[:200],
            "halt": result.halt.value, "why": result.reason[:300]}


# --------------------------------------------------------------------------- #
# Storage pressure (STORAGE-B3, P2-40): its own class, its own consumer
# --------------------------------------------------------------------------- #

def _moment(now: datetime | str | None) -> datetime:
    """`now` as an aware datetime (the timer form for a string)."""
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, str):
        return datetime.strptime(normalise_now(now), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    normalise_now(now)                       # refuses a naive datetime
    return now.astimezone(UTC)


def _capacity(admission) -> tuple[bool, str, str]:
    """(what a resumed iteration asks for first would be admitted now, state,
    effective free) -- observed through the admission, under its lock; nothing
    is reserved or recorded.

    What a resumed iteration asks for first is its temporary workspace
    (``tmp_workspace``, P2-42) and, while that reservation is held, the planner's
    ``role_run``: the **sum** of the two estimates, not the ``role_run`` alone.
    Asking for the ``role_run`` alone unparked a run in the band of free space
    between the two, where the iteration was parked again at the planner, charged
    again, and the escalation count of the park started over (an unpark / re-park
    loop). A run of a project that holds no workspace reservation is held back by
    that one workspace estimate more than it needs: conservative, never a loop."""
    from .storage_pressure import PressureState

    obs, pstate = admission.observe()
    free = f"{obs.effective_free_bytes} B"
    need = admission.estimate("role_run") + admission.estimate("tmp_workspace")
    ok = pstate is not PressureState.CRITICAL and admission.would_admit(
        "role_run", estimate_bytes=need)
    return ok, pstate.value, free


def _rearm(timers_store: TimerStore, old, *, moment: datetime, park_policy,
           still: str) -> tuple[Any, int]:
    """Arm the next STORAGE_RECHECK for the same branch **first**, then disarm
    the superseded timer by its id. Returns (new timer, its recheck count)."""
    from .storage_park import parse_recheck, recheck_reason
    from .timers import TimerKind, in_seconds

    parsed = parse_recheck(old.reason)
    park_id, count = parsed if parsed is not None else (old.timer_id, 0)
    new = timers_store.arm_new(
        kind=TimerKind.STORAGE_RECHECK,
        due_at=in_seconds(park_policy.recheck_interval_s, now=moment),
        reason=recheck_reason(park_id, count + 1, f"recheck: {still}"),
        program_id=old.program_id, program_epoch=old.program_epoch,
        session_epoch=old.session_epoch, generation=old.generation,
        run_id=old.run_id, node_id=old.node_id)
    timers_store.disarm(old.timer_id, f"storage recheck: superseded by {new.timer_id}")
    return new, count + 1


def _still_parked(rec: dict, timers_store, timer, *, moment, park_policy, state_name,
                  free) -> Any | None:
    """Re-arm and fill the record; returns the new timer or None on failure."""
    from .storage_park import NEEDS_YOU_REASON

    still = f"still {state_name}, {free}"
    try:
        new, count = _rearm(timers_store, timer, moment=moment, park_policy=park_policy,
                            still=still)
    except TimerStoreError as exc:
        # The old timer stays armed (it was never disarmed): the park keeps
        # its wake-up and the next pass tries again. A person is told.
        rec.update(action="rearm_failed", verdict="NEEDS_YOU",
                   why=f"recheck: {still}; the next recheck timer could not be armed "
                       f"({exc}); the current one stays armed"[:300])
        return None
    rec.update(action="rearmed", recheck=count, new_timer_id=new.timer_id,
               next_due=new.due_at, why=f"recheck: {still}")
    if count >= park_policy.needs_you_after:
        rec.update(verdict="NEEDS_YOU", needs_you=NEEDS_YOU_REASON,
                   why=f"recheck: {still}; {NEEDS_YOU_REASON} after {count} rechecks; "
                       "the branch stays parked and nothing is cleaned")
    return new


def resume_storage(root: Path | str, *, admission=None, state_root: Path | str | None = None,
                   observer=None, policy=None, park_policy=None,
                   now: datetime | str | None = None, only: str | None = None,
                   killed: frozenset[str] = frozenset(), projects: bool = True,
                   authority_factory: Callable[..., Any] | None = None) -> list[dict]:
    """One pass of `hoh resume-storage` over the runs and projects under `root`.

    `admission` is a `StorageAdmission`; without one it is built with
    `StorageAdmission.for_state_root(state_root, policy, observer)`. The boot
    is claimed first (`claim_boot`, a host entry point: after a reboot this
    pass may reach the ledger before any controller). A refused claim resumes
    nothing and is reported NEEDS_YOU.

    For every run -- and every project node -- BLOCKED on `storage_pressure`
    whose `STORAGE_RECHECK` timer is due (`due_resumptions` decides epoch,
    kill list, parked-ness and due time):

    * state better than CRITICAL **and** `would_admit` of a `role_run` held
      together with the iteration's temporary workspace (the sum of their two
      estimates, `_capacity`): the unparked state is persisted, then the timer
      consumed (`resumed`);
    * otherwise a new timer is armed first, the superseded one disarmed by
      id, and the branch stays parked (`rearmed`, ``recheck: still <state>,
      <free>``); from `park_policy.needs_you_after` rechecks on (counted in
      the durable timers) the record also says NEEDS_YOU with
      "owned optimisation could not recover the reserve".

    A storage park without an armed timer is reported (`parked_without_timer`,
    NEEDS_YOU) and never resumed through `retry_after`. Nothing is cleaned.
    """
    from . import stages
    from .storage_park import ParkPolicy, STORAGE_CLASS
    from .storage_pressure import StorageAdmission, StorageLedgerError, StoragePolicy
    from .store import LockBusy, RunStore, StoreError, list_runs
    from .timers import TimerKind

    root = Path(root).expanduser()
    moment = _moment(now)
    stamp = _now_str(moment)
    park_policy = park_policy or ParkPolicy()
    out: list[dict] = []
    if admission is None:
        if state_root is None:
            raise ValueError("resume_storage needs an admission or a state_root")
        admission = StorageAdmission.for_state_root(state_root, policy or StoragePolicy(),
                                                    observer)
    try:
        admission.claim_boot()
    except StorageLedgerError as exc:
        out.append({"scope": "storage", "target": str(getattr(admission, "ledger", "")),
                    "action": "refused", "verdict": "NEEDS_YOU",
                    "why": f"the boot cannot be claimed, nothing is resumed: {exc}"[:300]})
        return out

    timers_store = TimerStore(root)
    ids = [only] if only else list(list_runs(root))
    with _flock(root / ".resume.lock"):
        try:
            timers = timers_store.read()
        except TimerStoreError as exc:
            out.append({"scope": "runs", "target": str(root), "action": "skipped",
                        "why": f"timer store unreadable: {exc}"[:200]})
            timers = None
        for rid in ids if timers is not None else []:
            store = RunStore(root, rid)
            try:
                with store.lock():
                    st = store.read_state()
                    if st.condition is not Condition.BLOCKED or st.blocked_kind != STORAGE_CLASS:
                        continue
                    armed = [t for t in timers if t.run_id == rid
                             and t.status is TimerStatus.ARMED
                             and t.kind is TimerKind.STORAGE_RECHECK]
                    if not armed:
                        out.append({"scope": "run", "target": rid, "timer_id": None,
                                    "verdict": "NEEDS_YOU", "action": "parked_without_timer",
                                    "why": "parked on storage pressure with no armed "
                                           "STORAGE_RECHECK timer; this class has no "
                                           "retry_after fallback, a person resumes it"})
                        continue
                    epoch = run_epoch(store.dir)
                    if epoch is None:
                        out.append({"scope": "run", "target": rid, "action": "refused",
                                    "verdict": Verdict.NEEDS_OVERRIDE.value,
                                    "why": "the run's pinned contract cannot be read; "
                                           "nothing is resumed on a guess"})
                        continue
                    decisions = due_resumptions(armed, runs={rid: run_view(st, epoch)},
                                                now=stamp, program_epoch=epoch, killed=killed)
                    for d in decisions:
                        rec = {"scope": "run", "target": rid, "timer_id": d.timer_id,
                               "verdict": d.verdict.value, "why": d.why}
                        out.append(rec)
                        if not d.resumes:
                            rec["action"] = "refused"
                            continue
                        if stages.violation_unresolved(st):
                            _hold_refusal(st, rec, "a storage recheck")
                            continue
                        ok, state_name, free = _capacity(admission)
                        if ok:
                            stages.unblock(st, f"resumed by resume-storage: timer {d.timer_id} "
                                               f"due {d.due_at}; state {state_name}, {free}, "
                                               "a role run would be admitted")
                            st.blocked_kind = None
                            st.retry_after = None
                            store.write_state(st)          # unparked first ...
                            rec.update(action="resumed", state=state_name, free=free,
                                       next_step=f"hoh --root {store.root} run {rid}")
                            try:
                                consume(timers_store, d, now=stamp)   # ... then the timer
                            except ResumptionError as exc:
                                rec["timer"] = f"left armed: {exc}"
                            continue
                        timer = next(t for t in armed if t.timer_id == d.timer_id)
                        new = _still_parked(rec, timers_store, timer, moment=moment,
                                            park_policy=park_policy, state_name=state_name,
                                            free=free)
                        if new is not None:
                            st.retry_after = new.due_at
                            st.note(f"recheck: still {state_name}, {free}; next recheck "
                                    f"{new.due_at} (timer {new.timer_id})"
                                    + (f"; NEEDS_YOU: {rec['needs_you']}"
                                       if rec.get("needs_you") else ""))
                            store.write_state(st)          # the new wake-up exists already
            except (LockBusy, StoreError) as exc:
                out.append({"scope": "run", "target": rid, "action": "skipped",
                            "why": str(exc)[:160]})
    if projects and not only:
        from .projectstore import list_projects

        for pid in list_projects(root):
            try:
                out.extend(_storage_one_project(
                    root, pid, admission=admission, moment=moment, stamp=stamp,
                    park_policy=park_policy, killed=killed,
                    authority_factory=authority_factory))
            except Exception as exc:                      # noqa: BLE001 - reported
                out.append({"scope": "project", "target": pid, "action": "skipped",
                            "why": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def _storage_one_project(root: Path, pid: str, *, admission, moment: datetime, stamp: str,
                         park_policy, killed, authority_factory) -> list[dict]:
    """The project half of `resume_storage`: nodes BLOCKED on storage_pressure."""
    from .orchestrator import node_timer_key, write_lock
    from .project import Lifecycle
    from .projectstore import ProjectStore
    from .storage_park import STORAGE_CLASS
    from .store import LockBusy, StoreError
    from .succession import LeaseError, LeaseHeld, MasterAuthority, StaleMaster
    from .timers import TimerKind

    out: list[dict] = []
    store = ProjectStore(root, pid)
    try:
        state = store.read_state()
    except StoreError as exc:
        return [{"scope": "project", "target": pid, "action": "skipped",
                 "why": str(exc)[:160]}]
    parked = {node_timer_key(n.id): n for n in state.nodes
              if n.lifecycle is Lifecycle.BLOCKED and n.blocked_kind == STORAGE_CLASS}
    if not parked:
        return out
    timers_store = TimerStore(store.dir)
    timers = [t for t in timers_store.read() if t.status is TimerStatus.ARMED
              and t.kind is TimerKind.STORAGE_RECHECK and t.run_id in parked]
    timed = {t.run_id for t in timers}
    for key, n in parked.items():
        if key not in timed:
            out.append({"scope": "project", "target": pid, "node": n.id, "timer_id": None,
                        "verdict": "NEEDS_YOU", "action": "parked_without_timer",
                        "why": "parked on storage pressure with no armed STORAGE_RECHECK "
                               "timer; no retry_after fallback, a person resumes it"})
    views = {key: node_view(n, state.program_generation) for key, n in parked.items()}
    decisions = due_resumptions(timers, runs=views, now=stamp,
                                program_epoch=state.program_generation, killed=killed)
    todo: list[tuple[Resumption, dict]] = []
    for d in decisions:
        node = parked.get(d.run_id or "")
        rec = {"scope": "project", "target": pid, "node": node.id if node else None,
               "timer_id": d.timer_id, "verdict": d.verdict.value, "why": d.why}
        out.append(rec)
        if not d.resumes or node is None:
            rec["action"] = "refused"
            continue
        ok, state_name, free = _capacity(admission)
        if ok:
            rec.update(action="pending", state=state_name, free=free)
            todo.append((d, rec))
            continue
        timer = next(t for t in timers if t.timer_id == d.timer_id)
        _still_parked(rec, timers_store, timer, moment=moment, park_policy=park_policy,
                      state_name=state_name, free=free)
    if not todo:
        return out

    sid = f"resume-storage-{stamp.replace(':', '')}-{os.getpid()}"
    make = authority_factory or MasterAuthority
    authority = make(store.dir, sid, serialize=lambda: write_lock(store))
    try:
        authority.acquire(program_generation=state.program_generation,
                          note="resume-storage: capacity returned for a storage wait")
    except (LeaseHeld, LockBusy, LeaseError) as exc:
        for _, r in todo:
            r.update(action="held_by_live_master"
                     if isinstance(exc, (LeaseHeld, LockBusy)) else "refused",
                     why=str(exc)[:200])
        return out
    try:
        with write_lock(store):
            st = store.read_state()
            to_consume = []
            for d, r in todo:
                node = st.node(r["node"])
                if node is None or node.blocked_kind != STORAGE_CLASS \
                        or node.lifecycle is not Lifecycle.BLOCKED:
                    r.update(action="refused", why="the node changed under the pass")
                    continue
                node.lifecycle = Lifecycle.READY
                node.note = (f"resumed by {sid} at {stamp}: storage recheck timer "
                             f"{d.timer_id} (due {d.due_at}), {r['state']}, {r['free']}")
                node.blocked_kind = None
                node.retry_after = None
                r.update(action="resumed", master=sid, epoch=authority.epoch)
                to_consume.append((d, r))
            # Unparked state first, then the timers: the crash invariant.
            authority.stamp(st)
            store.write_state(st)
            for d, r in to_consume:
                try:
                    consume(timers_store, d, now=stamp)
                except ResumptionError as exc:
                    r["timer"] = f"left armed: {exc}"
    except (LockBusy, StaleMaster) as exc:
        for _, r in todo:
            if r["action"] == "pending":
                r.update(action="refused", why=str(exc)[:200])
    finally:
        _release(authority, "resume-storage pass ended")
    return out

