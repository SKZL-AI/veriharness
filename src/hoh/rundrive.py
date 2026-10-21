"""A run lease on the production path: `hoh run` holds the lease of the run it drives (plan row P3-18, slice 2).

Slice 1 (`hoh.runlease`) is the record and its protocol, and nothing called it. This module is its first caller. A
`hoh run` that holds the run's process lock (`RunStore.process_marker`) takes the run's lease before the run body
writes anything of the run, renews it from a keepalive thread for as long as it drives, and gives it up when it ends
-- on every way out that unwinds, a refusal and an exception included. A process that does not unwind (a kill) leaves
a lease whose holder's pid is gone, which `RunLeases.judge` reads as LOST at once; a frozen one lets it lapse after
`LEASE_TTL`. A lease that is released, lost or run out is taken over only by `RunLeases.acquire` (the next
generation, the replaced record parked): this module never writes a lease record by hand.

**Where the leases are.** `<runs root>/projects/hoh-runs/run-leases/<name>.json`, one namespace for every run: the
launcher starts runs without `--project`, so a run does not know its project, and a directory per name would multiply
the entries of `projects/` that `capacitycheck.live_holds` (which reads exactly this directory) refuses beyond a bound.
`lease_name` is the run id when the lease grammar takes it, else `run-` and the first 32 hex digits of the id's digest.

**What a refusal says.** A live lease of another holder is refused with the words of the process lock's own refusal
(`Run <id> is already being executed ...`), which `launcher.HohRunLauncher._launch` reads as IN_FLIGHT; it is reached when
the process lock is free, i.e. the holder is in another host or pid namespace that shares the root. Anything else
that keeps the lease from being read or written (`run lease refused (<reason>)`) refuses too: a lease that cannot be
read exactly is not a free run. In both cases no run state was written (the process marker `process.json` is the lock's
own protocol and says nothing about the lease).

**The keepalive is bounded.** One daemon thread, one attempt per period (a third of the ttl). It ends when the run
ends (`close`), at the first refusal of a renewal (`StaleRunLease`: the lease is not this run's any more) and after
`MAX_RENEW_FAILURES` renewals in a row failed (three periods are one ttl: the lease has lapsed anyway); the last two
set `lost`, which `cli._run_in` reads before it starts another iteration. The counter lives in memory because its loss
coincides with the thread's; the persisted bound is the record's `expires_at`, which only a successful renewal moves.

A refusal at the start leaves nothing of this process behind: an `acquire` that raised after it wrote (the bytes in
place, the directory entry in doubt) is given back at once, and a keepalive thread that cannot be started releases the
lease and refuses the run. `close` never raises, whether or not the thread ever ran.

`refusal_for_resume` is the read-only half for `hoh resume`: it creates nothing and answers why a run that another
driver holds must not be touched.

**Slice 3: the readers of the project loop.** `consult` is the one question every reader asks of a run -- "what does its
lease say?" -- answered `NONE` (no lease was ever written: a run that predates leases, or one `hoh run` never drove),
`LIVE` (a driver holds it: the run is alive, whatever its process lock says, because the holder may live in a host or
a pid namespace that shares the root), `LOST` / `RELEASED` (nobody holds it: **reclaimable**, taken over by the next
`hoh run`'s `acquire`, which parks the record it replaces -- this module and its readers never write a lease), or
`UNREADABLE` (a record that cannot be read exactly is not a free run). `consult_nodes` asks it for the RUNNING nodes of a
project state; `listing` and `archive` are what `hoh project leases` prints and does. The lease of a driver started by a
master carries that master's epoch (`drive_lease(master_epoch=...)`), and every reader says it.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from . import leaseadmin, runlease
from .controller import keepalive_period
from .projectstore import ProjectStore
from .leaseadmin import UNREADABLE, ArchiveResult, LeaseRow
from .runlease import LIVE, LOST, NONE, RELEASED, RunLease, RunLeaseError, RunLeaseHeld, RunLeases, StaleRunLease
from .succession import token_digest

LEASE_PROJECT = "hoh-runs"
"""The one namespace under `<runs root>/projects/` whose `run-leases` directory holds every run's lease."""

LEASE_TTL = 300
"""Seconds a holder may be silent: a killed holder is LOST at once by its pid, this bounds a frozen or foreign one."""

MAX_RENEW_FAILURES = 3
"""Renewals in a row that may fail before the keepalive gives the lease up as lost."""

JOIN_SECONDS = 5.0
"""The longest the end of a run waits for its keepalive thread; the thread is a daemon and finds the lease released."""

THREAD_NAME = "hoh-run-lease-keepalive"


class DriveRefused(RuntimeError):
    """`hoh run` may not drive this run. The text is the operator's; `reason` is `held` or a `RunLeaseError.reason`."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def lease_name(run_id: str) -> str:
    """The name a run's lease is kept under: the id itself when the lease grammar takes it, else `run-` and the
    first 32 hex digits of its digest -- stable, so one run is one lease in every process. A legal run id may start
    with `-` or be longer than 64 characters; that is never a reason to run without a lease."""
    try:
        return runlease._run_id(run_id)
    except RunLeaseError:
        return "run-" + token_digest(run_id)[:32]


def _leases(root, **environment) -> RunLeases:
    return ProjectStore(root, LEASE_PROJECT).run_leases(**environment)


class DriveLease:
    """The lease of the run being driven, kept alive while it runs. Holds nothing until `acquire`."""

    def __init__(self, leases: RunLeases, name: str, *, ttl_seconds: int, period: float,
                 master_epoch: int | None = None) -> None:
        self.leases = leases
        self.name = name
        self.ttl_seconds = ttl_seconds
        self.period = period
        self.master_epoch = master_epoch
        self.lease: RunLease | None = None
        #: Why the keepalive gave the lease up, or None.
        self.lost: str | None = None
        #: Renewals in a row that failed (reset by a success).
        self.failures = 0
        self.released: RunLease | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._keepalive, name=THREAD_NAME, daemon=True)

    def acquire(self, run_id: str) -> None:
        holder = f"hoh-run.{self.leases.pid}"
        try:
            # `master_epoch` only when there is one: a handle that was built without it asks as it always did.
            epoch = {} if self.master_epoch is None else {"master_epoch": self.master_epoch}
            self.lease = self.leases.acquire(self.name, holder, ttl_seconds=self.ttl_seconds, **epoch)
        except RunLeaseHeld as exc:
            raise DriveRefused("held", f"Run {run_id} is already being executed ({exc}); "
                                       "no run state was written") from exc
        except (RunLeaseError, OSError) as exc:       # a full disk, a file system that refuses
            reason = exc.reason if isinstance(exc, RunLeaseError) else "unreadable"
            raise DriveRefused(reason, _refused(reason, exc) + self._give_back(holder)) from exc

    def _give_back(self, holder: str) -> str:
        """The record an `acquire` that raised may have written (`DurableWriteNotSynced`: the bytes are in
        place, only the directory entry is in doubt) is this process's own LIVE lease, and a refused run must
        not leave it standing for a ttl -- the next try would be refused against itself. Released here when it
        is ours; `""` when there is nothing of ours, else the sentence that says it stays."""
        try:
            record = self.leases.read(self.name)
        except Exception:                             # noqa: BLE001 - a record that cannot be read is not ours
            return ""
        if (record is None or record.released_at is not None or record.holder != holder
                or record.holder_pid != self.leases.pid or record.holder_host != self.leases.host):
            return ""
        try:
            self.leases.release(record, reason="acquire did not finish")
        except Exception as exc:                      # noqa: BLE001 - said, never retried
            return f" (a lease of this process stays until it lapses at {record.expires_at}: {exc})"
        return ""

    def start(self) -> None:
        self._thread.start()

    def _keepalive(self) -> None:
        while not self._stop.wait(self.period):
            try:
                self.lease = self.leases.renew(self.lease)
            except StaleRunLease as exc:
                self.lost = f"the lease is not this run's any more: {exc}"[:300]
                return
            except Exception as exc:                  # noqa: BLE001 - counted, and the bound is visible
                self.failures += 1
                if self.failures >= MAX_RENEW_FAILURES:
                    self.lost = (f"{self.failures} renewals in a row failed, the last with "
                                 f"{type(exc).__name__}: {exc}")[:300]
                    return
            else:
                self.failures = 0

    def close(self, reason: str) -> None:
        """Stop renewing, then give the run up. Never raises (it runs while an exception may be on its way out);
        a lease that is not this handle's any more is left exactly as it is."""
        if self.lease is None:
            return
        self._stop.set()
        if self._thread.is_alive():                   # one that never started (or has ended) is not joined
            self._thread.join(timeout=JOIN_SECONDS)
        try:
            self.released = self.leases.release(self.lease, reason=reason)
        except Exception as exc:                      # noqa: BLE001 - said once, never retried
            print(f"run lease {self.name}: not released ({exc}); it lapses at {self.lease.expires_at}",
                  file=sys.stderr)


def _refused(reason: str, exc: BaseException) -> str:
    return (f"run lease refused ({reason}): {exc}; no run state was written. Look at the record, then move it "
            "aside (a versioned rename) to go on")


@contextmanager
def drive_lease(root, run_id: str, *, ttl_seconds: int = LEASE_TTL, leases: RunLeases | None = None,
                master_epoch: int | None = None) -> Iterator[DriveLease]:
    """Hold the lease of `run_id` for the block: taken before it, kept alive in it, released after it whichever
    way it ends. `DriveRefused` (nothing held) when another holder has it or the record cannot be read.
    `master_epoch` is the epoch of the master that started this driver (`hoh run --master-epoch`), recorded in the
    lease; None for a driver no master started."""
    if leases is None:
        leases = _leases(root)
    drive = DriveLease(leases, lease_name(run_id), ttl_seconds=ttl_seconds,
                       period=keepalive_period(ttl_seconds), master_epoch=master_epoch)
    reason = "run ended"
    try:
        drive.acquire(run_id)
        try:
            drive.start()
        except RuntimeError as exc:                   # no thread can be made: the lease could not be kept alive
            raise DriveRefused("keepalive", f"run lease refused (keepalive): the keepalive thread could not "
                                            f"be started ({exc}); no run state was written") from exc
        yield drive
    except BaseException:
        reason = "run raised an exception"
        raise
    finally:
        drive.close(reason)


def refusal_for_resume(root, run_id: str, **environment) -> str | None:
    """Why `hoh resume` must not touch `run_id` now, or None. Read-only: an absent directory or record is no lease
    (every run that predates this slice), a LIVE lease is another driver, a record that cannot be read is not a free run."""
    leases = _leases(root, **environment)
    name = lease_name(run_id)
    try:
        lease = leases.read(name)
        if lease is None:
            return None
        state, why = leases.judge(lease)
    except RunLeaseError as exc:
        return _refused(exc.reason, exc)
    if state == LIVE:
        return (f"Run {run_id} is already being executed ({why}); 'hoh resume' does not touch a run "
                "another driver holds")
    return None


@dataclass(frozen=True)
class RunLeaseFact:
    """What the lease of one run says, for a reader. `state` is `NONE`, `LIVE`, `LOST`, `RELEASED` or `UNREADABLE`;
    `why` is one bounded line (it names the master epoch when the lease carries one)."""

    state: str
    why: str
    lease: RunLease | None = None

    @property
    def alive(self) -> bool:
        return self.state == LIVE

    @property
    def reclaimable(self) -> bool:
        """Nobody holds the run: the next `acquire` takes it over. A run with no lease at all is not "reclaimable"
        -- there is nothing to reclaim -- and a record that cannot be read is not free."""
        return self.state in (LOST, RELEASED)


def _epoch_note(lease: RunLease | None) -> str:
    return "" if lease is None or lease.master_epoch is None else f"; started under master epoch {lease.master_epoch}"


def consult(root, run_id: str, **environment) -> RunLeaseFact:
    """What the lease of `run_id` says now. Read-only: nothing is created, locked or written; an absent directory
    or record is `NONE`. A record that cannot be read exactly (or a lease directory that is not a directory of its
    own) is `UNREADABLE`, never `NONE`. `environment` is `RunLeases`' injectable facts (a test says "the holder is
    gone" without killing one)."""
    try:
        leases = _leases(root, **environment)
        lease = leases.read(lease_name(run_id))
        if lease is None:
            return RunLeaseFact(NONE, "no lease has ever been written for this run")
        state, why = leases.judge(lease)
    except RunLeaseError as exc:
        return RunLeaseFact(UNREADABLE, f"{exc.reason}: {exc}"[:300])
    except (OSError, ValueError) as exc:             # a runs root the file system or the store refuses
        return RunLeaseFact(UNREADABLE, f"unreadable: {exc}"[:300])
    return RunLeaseFact(state, (why + _epoch_note(lease))[:300], lease)


def consult_nodes(root, state, **environment) -> dict[str, tuple[str, str]]:
    """`{node id: (state, why)}` for the RUNNING nodes of a project state (`hoh.project.ProjectState`): the facts
    `projectstore.resume_decision` takes. A node's run is `node.run_id` or else its id, as the launcher has it."""
    out: dict[str, tuple[str, str]] = {}
    for node in state.nodes:
        if node.lifecycle.value == "RUNNING":
            fact = consult(root, node.run_id or node.id, **environment)
            out[node.id] = (fact.state, fact.why)
    return out


def listing(root, **environment) -> list[LeaseRow]:
    """The leases `hoh run` keeps under `root`, judged now (`leaseadmin.snapshot`); nothing is created."""
    return leaseadmin.snapshot(_leases(root, **environment))


def archive(root, older_than_seconds: int, **environment) -> ArchiveResult:
    """Move the leases that ended at least `older_than_seconds` ago aside (`leaseadmin.archive_ended`)."""
    return leaseadmin.archive_ended(_leases(root, **environment), older_than_seconds=older_than_seconds)
