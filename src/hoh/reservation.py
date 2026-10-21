"""Budget reservation: a hold on the shared dispatch budget that is taken
atomically before a run is started and released after it has been dispatched.

V3.3 P3-04. The shared budget (`HohRunLauncher.dispatch_budget`) is a ceiling
that is authorised outside the system, and the spend is recorded inside it,
one `usage.dispatches` per run state. Until now nothing stood between the two:
`prepare` read what was spent and handed the whole remainder to the run it was
about to start, so two starts that overlapped -- two nodes prepared before
either was dispatched, two orchestrators over one root -- were each granted
the same remainder, and together they could spend twice the ceiling. The state
ownership ADR names this (rule 4) and the parallelism baseline measures it.

A reservation is the missing record between the two. Under one exclusive lock
a start reads the spend, reads the holds that are still open, and is granted
what neither has used; the grant is written down before the lock is released.
The next start therefore sees it, whichever process it is in.

What a hold counts for. A run holding `amount` is committed to
`max(amount, what it has spent)`: while it runs its own dispatches are already
in the spend, so counting both would charge it twice, and a run that spent
past its grant (it cannot, but a state file can be edited) must not shrink
what is committed. Everything else counts at what it has spent.

What this module does not do: read run states (the caller passes a function
that returns the spend per run, so the layout of a run directory stays the
launcher's business), start anything, or decide when a hold ends (the caller
settles it). Nothing is ever removed: a settled or superseded hold keeps its
row, because "this run held nine dispatches for forty minutes" is evidence and
a ledger that forgets cannot be asked (the same rule `hoh.timers` keeps).

A start may also keep a reserve (`keep`): the grant leaves at least that much of
the ceiling neither spent nor held, so that the phase that follows the one that
is starting -- QA, integration -- cannot find the budget gone (V3.3 P3-23). A hold
is ended by the id it was granted under (`settle_holds`), so settling a wave
again later can never end a newer hold of the same run (V3.3 P3-22).

Fail closed. A ledger that cannot be read, a lock that cannot be taken, a file
that is not a regular file, a disk too full to record the hold: each is a
refusal. A hold that cannot be known is not a hold of zero.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationError

from . import durable_io
from .contracts import Strict, utcnow
from .leaseadmin import UNREADABLE
from .runlease import LIVE
from .store import RESERVED_PREFIXES, RESERVED_RUN_IDS

SCHEMA = "hoh-budget-reservations/1"

#: Names the runs root keeps for itself (`store.RESERVED_PREFIXES`): never a
#: run id, so no run directory can ever collide with them.
LEDGER_NAME = "_budget-reservations.json"
LOCK_NAME = "_budget-reservations.lock"

#: The most bytes of ledger that are read. A ledger above it is refused rather
#: than parsed: a file that large is not this module's, and reading it whole
#: would put its size in memory.
MAX_LEDGER_BYTES = 16 * 1024 * 1024

#: How long a start waits for the lock before it is refused. The lock is held
#: for one read and one small write, so a wait this long means a holder that is
#: stuck, and waiting on it for ever would stop the whole project silently.
LOCK_TIMEOUT_SECONDS = 10.0

#: A run id as the runs root names it (`store._SAFE`). `\Z`, not `$`: `$` also
#: matches before a final newline.
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,255}\Z")

#: A reservation id as `reserve` writes it: eight hex digits, `-`, the run id.
_RESERVATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,300}\Z")


class ReservationError(RuntimeError):
    """The reservation could not be made, settled or read. Always a refusal."""


class NothingToReserve(ReservationError):
    """The budget is fully spent or held; the numbers are in the message."""

    #: True only for `ReserveKept`: there is budget, and it is the kept reserve.
    reserve_kept = False


class ReserveKept(NothingToReserve):
    """Something is left, and all of it is the reserve the caller keeps: a start
    that may use the reserve (`keep=0`) would be granted it."""

    reserve_kept = True


class HoldStatus(StrEnum):
    HELD = "HELD"
    SETTLED = "SETTLED"
    SUPERSEDED = "SUPERSEDED"


class Reservation(Strict):
    """One row of the ledger."""

    reservation_id: str
    run_id: str
    amount: int = Field(ge=1, strict=True)
    status: HoldStatus = HoldStatus.HELD
    reserved_at: str
    ceiling: int = Field(ge=1, strict=True)
    ended_at: str | None = None
    reason: str = ""


def check_run_id(run_id: object) -> str:
    """The run id as given, or `ReservationError`. Not stripped or folded.

    The names the runs root keeps for itself are not run ids (`RunStore` refuses
    them), so a hold cannot be keyed by one either.
    """
    if (not isinstance(run_id, str) or not _RUN_ID.match(run_id)
            or run_id.startswith(RESERVED_PREFIXES) or run_id in RESERVED_RUN_IDS):
        raise ReservationError(f"not a run id: {run_id!r}")
    return run_id


def _check_count(value: object, what: str, *, minimum: int) -> int:
    # `bool` is an `int`; `True` is not a budget.
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReservationError(f"{what} must be an integer of at least {minimum}, "
                               f"not {value!r}")
    return value


class BudgetReservations:
    """The holds on one runs root, in one file, replaced atomically."""

    def __init__(self, root: Path | str, *,
                 lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.root = Path(root).expanduser()
        self.path = self.root / LEDGER_NAME
        self.lock_path = self.root / LOCK_NAME
        self.lock_timeout = float(lock_timeout)

    # -- reading ------------------------------------------------------------ #

    def read(self) -> list[Reservation]:
        """Every row, oldest first. A missing ledger is no rows; a ledger that
        is not exactly what this module wrote is `ReservationError`."""
        try:
            raw = durable_io.read_single_link(self.path)
        except OSError as exc:
            raise ReservationError(f"{self.path} cannot be read: {exc}") from exc
        if raw is None:
            return []
        if len(raw) > MAX_LEDGER_BYTES:
            raise ReservationError(
                f"{self.path} is {len(raw)} bytes, above the {MAX_LEDGER_BYTES} "
                f"that are read")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ReservationError(f"{self.path} is not JSON: {exc}") from exc
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA
                or not isinstance(data.get("reservations"), list)):
            raise ReservationError(f"{self.path} does not hold a reservation ledger")
        # The model strips whitespace, so the run id is checked as written.
        for raw_row in data["reservations"]:
            check_run_id(raw_row.get("run_id") if isinstance(raw_row, dict) else None)
        try:
            return [Reservation.model_validate(r) for r in data["reservations"]]
        except ValidationError as exc:
            raise ReservationError(f"{self.path} holds an invalid row: {exc}") from exc

    def held(self) -> list[Reservation]:
        """The holds that are open now. Read without the lock: the ledger is
        replaced whole, so a reader sees one version or the next."""
        return [r for r in self.read() if r.status is HoldStatus.HELD]

    # -- writing ------------------------------------------------------------ #

    def _write(self, rows: list[Reservation]) -> None:
        payload = json.dumps(
            {"schema_version": SCHEMA, "written_at": utcnow(),
             "reservations": [json.loads(r.model_dump_json()) for r in rows]},
            indent=2) + "\n"
        try:
            durable_io.durable_write(self.path, payload, mode=0o600)
        except OSError as exc:
            raise ReservationError(f"the hold could not be recorded: {exc}") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """The exclusive lock around one read-modify-write of the ledger.

        Taken without blocking and retried until `lock_timeout`, so a stuck
        holder is a refusal and not a project that has silently stopped.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path,
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise ReservationError(f"{self.lock_path} cannot be opened: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ReservationError(f"{self.lock_path} is not a regular file")
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ReservationError(
                            f"{self.lock_path} is held by another process and was "
                            f"not released within {self.lock_timeout:g} s") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- the operations ----------------------------------------------------- #

    def reserve(self, run_id: str, *, ceiling: int,
                spent: Callable[[], Mapping[str, int]],
                want: int | None = None, keep: int = 0) -> Reservation:
        """Holds budget for `run_id`, or refuses.

        `ceiling` is the shared budget. `spent` returns the dispatches each run
        has spent, keyed by run id; it is called under the lock, so what it
        returns and the holds it is combined with are one moment. `want` is the
        most to hold; None holds everything that is left. The grant is
        `min(want, left - keep)`, and `NothingToReserve` when nothing is left.

        `keep` is the reserve this start may not touch: the grant leaves at
        least `keep` of the ceiling neither spent nor held. When something is
        left and all of it is within `keep`, the refusal is `ReserveKept` (a
        `NothingToReserve`): the same start with `keep=0` would be granted it.
        The test is made under the lock, with the spend and the holds it is
        combined with.

        A run id holds at most one reservation: reserving again supersedes the
        earlier hold (kept, marked SUPERSEDED), so a start that was interrupted
        and is made again does not strand what it held the first time.
        """
        run_id = check_run_id(run_id)
        ceiling = _check_count(ceiling, "the ceiling", minimum=1)
        if want is not None:
            want = _check_count(want, "the amount wanted", minimum=1)
        keep = _check_count(keep, "the reserve kept", minimum=0)
        with self._locked():
            rows = self.read()
            left, total_spent, headroom = self._left(
                rows, run_id, ceiling, self._spent(spent))
            if left <= 0:
                raise NothingToReserve(
                    f"nothing is left to reserve: {total_spent} spent and {headroom} "
                    f"held of {ceiling}")
            if left <= keep:
                raise ReserveKept(
                    f"{left} of {ceiling} is left and {keep} of it is kept in reserve: "
                    f"{total_spent} spent and {headroom} held")
            grant = left - keep if want is None else min(want, left - keep)
            now = utcnow()
            rows = [r.model_copy(update={"status": HoldStatus.SUPERSEDED, "ended_at": now,
                                         "reason": f"superseded by a new hold of {run_id}"})
                    if r.status is HoldStatus.HELD and r.run_id == run_id else r
                    for r in rows]
            hold = Reservation(
                reservation_id=f"{secrets.token_hex(4)}-{run_id}", run_id=run_id,
                amount=grant, reserved_at=now, ceiling=ceiling)
            rows.append(hold)
            self._write(rows)
            return hold

    @staticmethod
    def _left(rows: list[Reservation], run_id: str, ceiling: int,
              spent_by_run: dict[str, int]) -> tuple[int, int, int]:
        """`(left, spent, held)` for a start of `run_id`: the ceiling less every
        run's spend less the open holds of the OTHER runs beyond what each has
        spent. The run's own earlier hold is not counted (a new one supersedes
        it), its own spend is. A run with two open rows (a ledger edited by
        hand) counts at the larger, which can only make the grant smaller."""
        total_spent = sum(spent_by_run.values())
        held: dict[str, int] = {}
        for row in rows:
            if row.status is HoldStatus.HELD and row.run_id != run_id:
                held[row.run_id] = max(held.get(row.run_id, 0), row.amount)
        headroom = sum(max(amount - spent_by_run.get(rid, 0), 0)
                       for rid, amount in held.items())
        return ceiling - total_spent - headroom, total_spent, headroom

    def left(self, run_id: str, *, ceiling: int,
             spent: Callable[[], Mapping[str, int]]) -> int:
        """What a start of `run_id` would be granted now, read **without** the
        lock: an advisory answer for a caller that wants to refuse early, before
        it creates anything. `reserve` is the authority and asks again under the
        lock."""
        run_id = check_run_id(run_id)
        ceiling = _check_count(ceiling, "the ceiling", minimum=1)
        return self._left(self.read(), run_id, ceiling, self._spent(spent))[0]

    def settle(self, run_id: str, reason: str) -> int:
        """Ends the open hold of `run_id`: its dispatches are in the run's own
        state from here, and nothing is held for it. Returns how many holds it
        ended -- 0 when there was none, which is not an error (a run that was
        resumed never held anything), and the ledger is then not written."""
        run_id = check_run_id(run_id)
        if not os.path.lexists(self.path):
            return 0                            # nothing was ever held under this root
        with self._locked():
            rows = self.read()
            if not any(r.status is HoldStatus.HELD and r.run_id == run_id for r in rows):
                return 0
            now, ended = utcnow(), 0
            out = []
            for r in rows:
                if r.status is HoldStatus.HELD and r.run_id == run_id:
                    r = r.model_copy(update={"status": HoldStatus.SETTLED,
                                             "ended_at": now, "reason": str(reason)[:200]})
                    ended += 1
                out.append(r)
            self._write(out)
            return ended

    def settle_holds(self, reservation_ids: Sequence[str], reason: str, *,
                     expect: Mapping[str, tuple[str, int]] | None = None
                     ) -> dict[str, HoldStatus]:
        """Ends the open holds with exactly these reservation ids, in one write.

        Returns each id's status **as it was found**: HELD means this call ended
        it; SETTLED and SUPERSEDED were not open and are left as they are. By id,
        never by run id: a run id that a later hold has taken again is not the
        hold the caller means. Everything is checked before anything is written,
        so an id the ledger does not hold (or is not an id, or is named twice,
        or a ledger that is not there) refuses the whole call and ends nothing.
        No ids end nothing, read nothing and take no lock.

        `expect` maps each id to the `(run id, amount)` the caller believes that
        hold has -- what a wave record says -- and is checked against the row
        under the lock: a record whose ids were swapped or edited is refused,
        not settled as another run's hold.
        """
        if isinstance(reservation_ids, (str, bytes)):
            raise ReservationError("reservation ids are a list, not one string")
        ids = list(reservation_ids)
        for rid in ids:
            if not isinstance(rid, str) or _RESERVATION_ID.match(rid) is None:
                raise ReservationError(f"not a reservation id: {rid!r}")
        if len(set(ids)) != len(ids):
            raise ReservationError("a reservation id is named twice")
        if expect is not None:
            if (not isinstance(expect, Mapping) or set(expect) != set(ids)
                    or not all(isinstance(v, tuple) and len(v) == 2 for v in expect.values())):
                raise ReservationError("expect names exactly the ids, each as (run id, amount)")
        if not ids:
            return {}
        if not os.path.lexists(self.path):
            raise ReservationError(f"{self.path} does not exist: no hold is known here")
        with self._locked():
            rows = self.read()
            by_id = {r.reservation_id: r for r in rows}
            missing = [rid for rid in ids if rid not in by_id]
            if missing:
                raise ReservationError(
                    f"the ledger holds no reservation {missing[0]!r}"
                    + (f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""))
            if expect is not None:
                for rid in ids:
                    row, (want_run, want_amount) = by_id[rid], expect[rid]
                    if row.run_id != want_run:
                        raise ReservationError(
                            f"the hold {rid!r} is of run {row.run_id!r}, not {want_run!r}")
                    if row.amount != want_amount:
                        raise ReservationError(
                            f"the hold {rid!r} is {row.amount}, not {want_amount}")
            found = {rid: by_id[rid].status for rid in ids}
            if HoldStatus.HELD not in found.values():
                return found
            now, why = utcnow(), str(reason)[:200]
            self._write([r.model_copy(update={"status": HoldStatus.SETTLED, "ended_at": now,
                                              "reason": why})
                         if r.reservation_id in found and r.status is HoldStatus.HELD else r
                         for r in rows])
            return found

    @staticmethod
    def _spent(spent: Callable[[], Mapping[str, int]]) -> dict[str, int]:
        mapping = spent()
        if not isinstance(mapping, Mapping):
            raise ReservationError("the spend is not a mapping of run id to dispatches")
        out: dict[str, int] = {}
        for run_id, n in mapping.items():
            if not isinstance(run_id, str):
                raise ReservationError(f"the spend names a run {run_id!r}")
            out[run_id] = _check_count(n, f"the spend of {run_id}", minimum=0)
        return out


# --------------------------------------------------------------------------- #
# Capacity reservations (plan row P3-04, capacity chain step C4)
# --------------------------------------------------------------------------- #
#
# `BudgetReservations` holds *dispatches* of the shared budget. This is the other half of the row: the
# **capacity** a wave was admitted against (`capacitycheck.admit_wave`: one `machine/host.sessions` session per
# admitted node) is held, atomically with the decision, until its owner ends it. Two controllers that ask
# the registry for the same remaining sessions can no longer both be told yes: the second read of the
# holds happens under the same lock as the first write.
#
# What ends a hold, and why every path terminates (the contract of this section):
#
# * `SUPERSEDED` -- the owner (this process, this thread) asks again. A controller runs one wave at a time, so
#   its previous wave is over when it asks for the next; the row stays, marked.
# * `LOST` -- the owner's process is gone (another boot, the pid no longer exists or is another process, the
#   owning thread has ended): judged exactly as a run lease is (`RunLeases._judge`).
# * `EXPIRED` -- `expires_at` has passed: the ceiling for an owner whose process cannot be judged (another
#   host or pid namespace) and for a hold nobody ended. `CAPACITY_TTL_SECONDS` (a day, the run lease's own ceiling).
# * `SETTLED` -- the wave is over: `ProjectController._report_wave_end` ends the controller's own open rows
#   (`settle_owned`, owner-scoped: node ids are project-local), and the recovery of a crash-left wave ends the gone
#   controller's (its pid and process identity). `settle(run_id, reason)` is the primitive for a caller that knows the
#   project. A round that ends before its executor starts leaves its rows to the next ask, the owner's death or the ttl.
#
# Lease-aware (capacity chain step C6): a row may name the **run lease** of the run it was taken for (`CapacityHold.lease`,
# written by `capacitycheck.admit_wave` from the controller's run ids). While that lease is `LIVE` -- `rundrive.consult`,
# read-only, in this object's own view of the machine -- the row is `HELD` whatever its owner's death and its ttl say, so a
# run that outlives its controller, or a wave that runs past the ttl, keeps what it was admitted against until its lease
# ends. A lease that cannot be read exactly is not a free run: it suspends the owner-gone rule (the ttl still ends the row)
# and the ask that would have ended the row for its owner's death is refused, naming the file. A lease that is absent,
# released or lost says nothing and the rules above apply. `settle_owned(keep_live=True)`, which the recovery of a
# crash-left wave uses, leaves such rows open.
#
# The limit, exactly: a hold is released when its *owner* is gone or past its ttl, **unless its run's lease is live**.
# The windows that stay (the register's `equivalent` names them): L1 the owner's own next ask supersedes its earlier
# rows although a run of that wave is alive; L2 a row is settled by the wave's end or the recovery before its run has
# taken its lease (the launch is between the dispatch and `hoh run` taking it); L3 a run that takes no run lease (only
# `hoh run` takes one) is held by its row alone; the link is by the lease's name, not its generation. The retention is
# bounded by the run's life: a retained row ends, by the rules it always had, at the first ask after its lease does.
#
# Each transition is made once, under the lock, and persisted in the same write that decided; a terminal row is
# never looked at again (a further ask over a ledger with nothing to end writes nothing). The lock is taken
# without blocking and retried for `CAPACITY_LOCK_TIMEOUT_SECONDS`; a holder that does not let go is a refusal
# (`ReservationError`), never a wait without an end. A ledger or archive that cannot be read or written is a
# refusal: the caller admits nobody. Terminal rows are kept; when more than `CAPACITY_ARCHIVE_ROWS` rows are
# terminal they are copied, whole, to a versioned file beside the ledger before the ledger is rewritten with
# the open holds only, so the file a decision reads does not grow with the host's history.

CAPACITY_LEDGER_NAME = "_capacity-reservations.json"
CAPACITY_LOCK_NAME = "_capacity-reservations.lock"
CAPACITY_SCHEMA = "hoh-capacity-reservations/1"
CAPACITY_TTL_SECONDS = 24 * 60 * 60
CAPACITY_LOCK_TIMEOUT_SECONDS = 5.0
CAPACITY_ARCHIVE_ROWS = 1024
MAX_CAPACITY_BYTES = 16 * 1024 * 1024
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class CapacityStatus(StrEnum):
    HELD = "HELD"
    SUPERSEDED = "SUPERSEDED"
    LOST = "LOST"
    EXPIRED = "EXPIRED"
    SETTLED = "SETTLED"


class CapacityHold(Strict):
    """One row of the capacity ledger: `amount` `unit`s of the capacity `kind`/`name` held for run `run_id` by one
    owner (a process and a thread), from `reserved_at` until `expires_at` at the latest."""

    reservation_id: str
    run_id: str
    kind: str
    name: str
    amount: int = Field(ge=1, strict=True)
    unit: str
    status: CapacityStatus = CapacityStatus.HELD
    owner_pid: int = Field(ge=1, strict=True)
    owner_thread: int = Field(ge=0, strict=True)
    owner_boot: str
    owner_host: str
    owner_pidns: str
    owner_start: str
    reserved_at: str
    expires_at: str
    ended_at: str | None = None
    reason: str = ""
    lease: str | None = None
    """The name of the run lease (`rundrive.lease_name`) of the run this hold was taken for, or None. The ledger spells
    the key only when it is set (`_row_json`), so a ledger nothing links to a run is what it was."""


def check_lease_name(name: object) -> str:
    """The lease name as given, or `ReservationError`: the run lease's own name grammar (at most 64 characters, not a
    name the runs root keeps for itself). Not stripped or folded."""
    from . import runlease

    try:
        return runlease._run_id(name)
    except runlease.RunLeaseError as exc:
        raise ReservationError(f"not a run lease name: {name!r}") from exc


def _row_json(row: CapacityHold) -> dict:
    """A row as the ledger spells it: without `lease` when it names none."""
    return json.loads(row.model_dump_json(exclude={"lease"}) if row.lease is None else row.model_dump_json())


class CapacityReservations:
    """The capacity holds of one runs root, in one file, replaced atomically.

    Every environmental fact is injectable (clock, boot id, host, pid, how to ask whether a pid is alive, its
    namespace and start time, the thread, the live threads), because a lost owner is exactly a change of those
    facts and a test has to say "the process is gone" without ending one. The defaults are the real ones
    (`hoh.succession`)."""

    def __init__(self, root: Path | str, *, lock_timeout: float | None = None, clock=None, boot=None,
                 host: str | None = None, pid: int | None = None, alive=None, pidns=None, start=None,
                 thread: int | None = None, live_threads=None, ttl_seconds: int = CAPACITY_TTL_SECONDS) -> None:
        import socket

        from . import succession as _s

        self.root = Path(root).expanduser()
        self.path = self.root / CAPACITY_LEDGER_NAME
        self.lock_path = self.root / CAPACITY_LOCK_NAME
        self._lock_timeout = lock_timeout
        self.clock = clock or _s.utc_now
        self.boot = boot or _s.boot_id
        self.host = host if host is not None else socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self.alive = alive or _s.pid_alive
        self.pidns = pidns or _s.pid_namespace
        self.start = start or _s.pid_start
        self.thread = thread if thread is not None else threading.get_ident()
        self.live_threads = live_threads or (lambda: {t.ident for t in threading.enumerate()})
        self.ttl = _check_count(ttl_seconds, "the ttl", minimum=1)

    # -- reading ------------------------------------------------------------ #

    def read(self) -> list[CapacityHold]:
        """Every row, oldest first. A missing ledger is no rows; a ledger that is not exactly what this module
        wrote (a link, a directory, a FIFO, a second name, oversize, not JSON, a wrong row) is `ReservationError`."""
        try:
            raw = durable_io.read_single_link(self.path)
        except OSError as exc:
            raise ReservationError(f"{self.path} cannot be read: {exc}") from exc
        if raw is None:
            return []
        if len(raw) > MAX_CAPACITY_BYTES:
            raise ReservationError(f"{self.path} is {len(raw)} bytes, above the {MAX_CAPACITY_BYTES} that are read")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ReservationError(f"{self.path} is not JSON: {exc}") from exc
        if (not isinstance(data, dict) or data.get("schema_version") != CAPACITY_SCHEMA
                or not isinstance(data.get("holds"), list)):
            raise ReservationError(f"{self.path} does not hold a capacity ledger")
        for raw_row in data["holds"]:
            check_run_id(raw_row.get("run_id") if isinstance(raw_row, dict) else None)
            if isinstance(raw_row, dict) and raw_row.get("lease") is not None:
                check_lease_name(raw_row["lease"])
        try:
            return [CapacityHold.model_validate(r) for r in data["holds"]]
        except ValidationError as exc:
            raise ReservationError(f"{self.path} holds an invalid row: {exc}") from exc

    def _run_lease_state(self, name: str, seen: dict | None):
        """What the run lease `name` says now (`rundrive.consult`: read-only, nothing is created), judged in this
        object's own view of the machine (its clock, boot id, host, process facts). Asked once per name for the
        operation that shares `seen`."""
        if seen is not None and name in seen:
            return seen[name]
        from . import rundrive

        fact = rundrive.consult(self.root, name, clock=self.clock, boot=self.boot, host=self.host, pid=self.pid,
                                alive=self.alive, pidns=self.pidns, start=self.start)
        if seen is not None:
            seen[name] = fact
        return fact

    def _owner_lost(self, row: CapacityHold) -> str | None:
        """Why the owner of the row is gone, or None: another boot; this host and pid namespace and the pid gone or
        another process (or, for this process, the owning thread gone). The run lease's order."""
        here = str(self.boot() or "")
        if row.owner_boot and here and row.owner_boot != here:
            return "the machine restarted since the hold was taken"
        if (row.owner_host == self.host and row.owner_pidns and row.owner_pidns == str(self.pidns() or "")):
            if not self.alive(row.owner_pid):
                return f"the owner (pid {row.owner_pid}) no longer exists"
            started = str(self.start(row.owner_pid) or "")
            if row.owner_start and started and started != row.owner_start:
                return f"pid {row.owner_pid} now belongs to another process"
            if row.owner_pid == self.pid and row.owner_thread not in self.live_threads():
                return "the owning thread has ended"
        return None

    def _lease_says(self, row: CapacityHold, seen: dict | None) -> str | None:
        """The state of the run lease of a row that names one (`LIVE`, `UNREADABLE`, ...), else None."""
        return None if row.lease is None else self._run_lease_state(row.lease, seen).state

    def judge(self, row: CapacityHold, now: str, *, seen: dict | None = None) -> tuple[CapacityStatus, str]:
        """`(status, why)` of a row at `now`: its own terminal status, or `LOST` / `EXPIRED` / `HELD` (live). A row
        that names a run lease asks it, after the terminal-status check: **`LIVE` keeps the row `HELD` whatever the
        owner and the ttl say**; a lease that cannot be read suspends the owner-gone rule (the ttl still ends the
        row); an absent, released or lost lease says nothing. Then the run lease's order: the owner (`_owner_lost`),
        the ttl. `seen` is the lease facts of one operation (a name is asked once); judging writes nothing."""
        if row.status is not CapacityStatus.HELD:
            return row.status, row.reason or "ended"
        state = self._lease_says(row, seen)
        if state == LIVE:
            return CapacityStatus.HELD, f"the run lease {row.lease} is live"
        if state != UNREADABLE:
            lost = self._owner_lost(row)
            if lost is not None:
                return CapacityStatus.LOST, lost
        if row.expires_at <= now:
            return CapacityStatus.EXPIRED, f"the hold ran past {row.expires_at}"
        return CapacityStatus.HELD, "held"

    def live(self) -> list[CapacityHold]:
        """The holds that count now: open and judged live. Read without the lock and **without writing**: a row whose
        owner is gone is not counted even before somebody ends it."""
        now, seen = self.clock(), {}
        return [r for r in self.read() if r.status is CapacityStatus.HELD
                and self.judge(r, now, seen=seen)[0] is CapacityStatus.HELD]

    # -- writing ------------------------------------------------------------ #

    def _write(self, rows: list[CapacityHold]) -> None:
        payload = json.dumps(
            {"schema_version": CAPACITY_SCHEMA, "written_at": self.clock(),
             "holds": [_row_json(r) for r in rows]}, indent=2) + "\n"
        try:
            durable_io.durable_write(self.path, payload, mode=0o600)
        except OSError as exc:
            raise ReservationError(f"the capacity ledger could not be written: {exc}") from exc

    def _archive(self, terminal: list[CapacityHold]) -> None:
        """The terminal rows, whole, to a new versioned file beside the ledger (never replaced, never removed)."""
        stamp = self.clock().replace(":", "").replace("-", "")
        payload = json.dumps(
            {"schema_version": CAPACITY_SCHEMA, "archived_at": self.clock(),
             "holds": [_row_json(r) for r in terminal]}, indent=2) + "\n"
        for _ in range(5):                                   # a name that exists is another archive: a new token
            target = self.root / f"_capacity-reservations.{stamp}-{secrets.token_hex(3)}.json"
            try:
                durable_io.durable_create(target, payload, mode=0o600)
                return
            except FileExistsError:
                continue
            except OSError as exc:
                raise ReservationError(f"the capacity ledger could not be archived: {exc}") from exc
        raise ReservationError("the capacity ledger could not be archived: no free archive name")

    @property
    def lock_timeout(self) -> float:
        """How long the lock is waited for: the module's bound unless the caller gave one."""
        return CAPACITY_LOCK_TIMEOUT_SECONDS if self._lock_timeout is None else float(self._lock_timeout)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """The exclusive lock around one read-modify-write of the ledger: `BudgetReservations`' own routine (one
        flock rule in this module: not blocking, retried until `lock_timeout`, a link or a non-file refused) run on
        this ledger's lock file. It reads only `root`, `lock_path` and `lock_timeout`."""
        with BudgetReservations._locked(self):           # type: ignore[arg-type]
            yield

    # -- the operations ----------------------------------------------------- #

    def reserve_atomically(self, decide: Callable[[], Sequence[tuple]]
                           ) -> tuple[list[CapacityHold], list[CapacityHold]]:
        """`(created, ended)`: under the lock, end what is over (a lost or expired owner's rows; this owner's earlier
        rows, which its asking again supersedes), persist that, call `decide()` -- which reads the holds and the
        registry and returns `(run_id, kind, name, amount, unit)` -- or a six-tuple whose last element is the name of
        the run lease of the run, or None -- for each capacity to hold -- and write those rows.

        A row whose run lease cannot be read, and which only the owner's death would have ended, is not a free
        run: the ask is a `ReservationError` naming the lease, before anything is written.

        `decide` runs with the lock held and sees the ledger as just written, so two callers are one after the
        other: the second counts what the first held. Whatever `decide` raises is raised after the ended rows were
        persisted and nothing was held. Nothing is written when there was nothing to end and nothing to hold."""
        with self._locked():
            rows = self.read()
            now = self.clock()
            ended: list[CapacityHold] = []
            seen: dict = {}
            unreadable: list[CapacityHold] = []
            for index, row in enumerate(rows):
                if row.status is not CapacityStatus.HELD:
                    continue
                status, why = self.judge(row, now, seen=seen)
                if status is CapacityStatus.HELD and row.owner_pid == self.pid and row.owner_thread == self.thread:
                    status, why = CapacityStatus.SUPERSEDED, "the owner asked again: its earlier wave is over"
                if status is CapacityStatus.HELD and self._lease_says(row, seen) == UNREADABLE \
                        and self._owner_lost(row) is not None:
                    unreadable.append(row)
                if status is not CapacityStatus.HELD:
                    rows[index] = row.model_copy(update={"status": status, "ended_at": now, "reason": why})
                    ended.append(rows[index])
            if unreadable:
                first = unreadable[0]
                raise ReservationError(
                    f"capacity cannot be judged: the run lease {first.lease!r} of the hold {first.reservation_id} "
                    f"cannot be read ({seen[first.lease].why}); move the lease file aside (a versioned rename) "
                    f"after looking at it")
            if ended:
                self._write(rows)
            wanted = [self._wanted(item) for item in decide()]
            if not wanted:
                return [], ended
            identity = dict(owner_pid=self.pid, owner_thread=self.thread, owner_boot=str(self.boot() or ""),
                            owner_host=self.host, owner_pidns=str(self.pidns() or ""),
                            owner_start=str(self.start(self.pid) or ""))
            from datetime import UTC, datetime, timedelta
            moment = datetime.strptime(now, _TIME_FORMAT).replace(tzinfo=UTC)
            expires = (moment + timedelta(seconds=self.ttl)).strftime(_TIME_FORMAT)
            created = [CapacityHold(reservation_id=f"{secrets.token_hex(4)}-{check_run_id(run_id)}", run_id=run_id,
                                    kind=kind, name=name, amount=amount, unit=unit, reserved_at=now,
                                    expires_at=expires, lease=lease, **identity)
                       for run_id, kind, name, amount, unit, lease in wanted]
            terminal = [r for r in rows if r.status is not CapacityStatus.HELD]
            if len(terminal) > CAPACITY_ARCHIVE_ROWS:
                self._archive(terminal)
                rows = [r for r in rows if r.status is CapacityStatus.HELD]
            self._write(rows + created)
            return created, ended

    @staticmethod
    def _wanted(item: object) -> tuple:
        """One thing `decide` wants held, as `(run_id, kind, name, amount, unit, lease)`: the five-tuple of before (no
        lease) or a six-tuple whose last element is a lease name or None. Anything else is `ReservationError`."""
        if not isinstance(item, (tuple, list)) or len(item) not in (5, 6):
            raise ReservationError(f"a capacity to hold is (run id, kind, name, amount, unit[, lease]): {item!r}")
        lease = item[5] if len(item) == 6 else None
        return (*item[:5], None if lease is None else check_lease_name(lease))

    def settle(self, run_id: str, reason: str) -> int:
        """Ends the open holds of `run_id` (`SETTLED`): the settle path of a wave calls it when the run is over.
        Returns how many it ended; 0 -- and no write -- when there was none."""
        run_id = check_run_id(run_id)
        if not os.path.lexists(self.path):
            return 0
        with self._locked():
            rows = self.read()
            if not any(r.status is CapacityStatus.HELD and r.run_id == run_id for r in rows):
                return 0
            now, count = self.clock(), 0
            for index, row in enumerate(rows):
                if row.status is CapacityStatus.HELD and row.run_id == run_id:
                    rows[index] = row.model_copy(update={"status": CapacityStatus.SETTLED, "ended_at": now,
                                                         "reason": str(reason)[:200]})
                    count += 1
            self._write(rows)
            return count

    def settle_owned(self, reason: str, *, run_ids: Sequence[str] | None = None, keep: Sequence[str] = (),
                     pid: int | None = None, thread: int | None = None, identity: str | None = None,
                     keep_live: bool = False) -> int:
        """Ends (`SETTLED`) the open holds of **one owner** -- by default this object's own `(pid, thread)`, the
        controller whose wave is over. Returns how many it ended; 0 -- and no write, no lock, no file -- when there
        was none or no ledger exists, so a second call over the same rows is a no-op.

        `run_ids` narrows it to those runs (node ids are project-local: a run id alone never names a row, so
        `settle` is not what a wave's end calls); `keep` leaves those runs' rows open (a run that may still be alive:
        its launch came back IN_FLIGHT or raised). A recovery names a *gone* controller: `pid`, `thread` (None = any
        thread of that process) and `identity`, the `<boot id>:<start time>` token of the process as its outcome
        recorded it -- a row whose owner has another identity is a reused pid's and is left. `keep_live` leaves the rows
        whose run lease is `LIVE` -- or cannot be read, which is not a free run -- open (a run that outlives its
        controller keeps what it was admitted against); the rows of every other lease, and of none, are ended. An
        unreadable ledger is `ReservationError`: nothing is ended and the caller says so."""
        who = self.pid if pid is None else pid
        if pid is None and thread is None:
            thread = self.thread
        wanted = None if run_ids is None else {check_run_id(r) for r in run_ids}
        kept = {check_run_id(r) for r in keep}
        if not os.path.lexists(self.path):
            return 0
        seen: dict = {}

        def mine(row: CapacityHold) -> bool:
            return (row.status is CapacityStatus.HELD and row.owner_pid == who
                    and (thread is None or row.owner_thread == thread)
                    and (wanted is None or row.run_id in wanted) and row.run_id not in kept
                    and (identity is None or not (row.owner_boot or row.owner_start)
                         or f"{row.owner_boot}:{row.owner_start}" == identity)
                    and not (keep_live and self._lease_says(row, seen) in (LIVE, UNREADABLE)))
        with self._locked():
            rows = self.read()
            if not any(mine(r) for r in rows):
                return 0
            now, count = self.clock(), 0
            for index, row in enumerate(rows):
                if mine(row):
                    rows[index] = row.model_copy(update={"status": CapacityStatus.SETTLED, "ended_at": now,
                                                         "reason": str(reason)[:200]})
                    count += 1
            self._write(rows)
            return count
