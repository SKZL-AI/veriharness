"""The capacity broker's first caller: what is held now, and one decision (plan row P3-17, slice 2).

`broker.decide` (slice 1) is a pure judgement: it is handed a registry, a policy, a time, what is wanted and what is
already held. Nothing handed it the last of those. This module is the thin adapter that does: it reads what the
product itself already records -- the run leases of every project under a runs root (`hoh.runlease`) and the open
budget reservations of that root (`hoh.reservation`) -- turns them into `broker.Amount` holds under two fixed,
documented names, and calls `decide`. It is called by `hoh capacity check` (`cli.cmd_capacity_check`), which is the
first place a broker decision is taken on a path production executes.

**Which hold counts against which capacity** (a lease records no capacity, so the attribution is a convention, and
the operator declares the capacities under these names with `hoh capacity set`):

* every **live run lease** is one `sessions` on `machine` / `host.sessions` (`SESSIONS`) -- a run is one
  concurrent session at a time; a lease that is released, lost (its holder's process gone, the machine restarted
  or its ttl run out) or absent holds nothing;
* every **open budget reservation** (`HELD`) holds its `amount` dispatches on `provider` /
  `dispatches.reserved` (`DISPATCHES`, unit `requests`): what is reserved and not yet spent, so the limit the
  operator declares there is "reserved at once", not the budget ceiling (the ceiling is `reservation`'s own, kept
  atomically; the spend is the launcher's and is not read here).

Workspace leases (`hoh.wslease`) are **not** counted: a lease is an exclusivity of one worktree, not an amount of
a capacity; a node that holds one also holds a run lease, so counting both would count a session twice; and the
product does not record which worktrees a project has (the project state carries one `repo_path`, no per-node
worktree), so they cannot be listed read-only.

**Read-only and fail closed.** Nothing is written, locked or created. A hold that cannot be known is not a hold of
zero: a lease file or ledger that cannot be read exactly, a project or lease directory that is a link or a file (a
stray file in `projects/` included), a runs root that does not exist or is not a directory (a link to one is
followed, as `ProjectStore` does), more than `MAX_PROJECTS` project directories, more than `MAX_LEASES` **live**
leases or more than `MAX_SCANNED` lease files, is a `CheckError` -- no decision is taken on an unknown. A lease is
judged in this process's view of the machine (`RunLeases.judge`).

**What is bounded.** A lease file is never removed (`runlease` stamps `released_at` and keeps the file), so the
number of files only grows with the runs a host has ever made. The cap on holds therefore counts what is **live**
(`MAX_LEASES`), and the scan is bounded separately by a generous ceiling on the files read (`MAX_SCANNED`) with its
own named refusal; pruning old leases is `P3-18`'s, and until then the ceiling is a stated precondition, not a hold.

**A run holds a session through its run lease**, which `hoh run` takes, keeps and releases for the run it drives
(`rundrive.drive_lease`, P3-18 slice 2); a run that is started by another route holds none, and a `grant` beside
`machine/host.sessions` says nothing about it.

**Not a reservation.** A `grant` is a judgement over what was recorded at `now`; two callers can each be granted the
same headroom (slice 1's limit stands). Policy ages are the operator's: `policy()` builds all six `(kind, source)`
pairs from two numbers (declared and measured), with defaults stated below.

**The wave's admission (plan row P3-19, slice 2).** `admit_wave` is the second caller: the project loop
(`orchestrator.ProjectController._wave_round`) hands it the nodes a wave would start, and it asks
`scheduler.admit` -- the same registry, the same holds (`live_holds`, one source of truth), the same policy -- with
one `machine/host.sessions` session wanted per node, in the order given. It answers which nodes may be prepared and
launched and why each of the others waits. It is **opt-in by the registry's existence**: no storage state root, or
no `capacity-registry.json` there, is "not configured" and the answer is "every node, as before" with the reason
said (**absent** means `ENOENT` on the record's own name and nothing else: a permission error, a link loop or a path
that is not a directory cannot be known and admits nobody); once the record exists everything that cannot be known (the
state root not initialised, a registry that `capacity.read` refuses, holds that cannot be read, a stale or unknown
capacity) admits **nobody** -- never an over-admission. The state root is the one `hoh capacity set` / `check` use
(`cli.resolve_storage_root`: the launcher's configured root, else `$HOH_STATE_ROOT`, else
`$XDG_STATE_HOME/veriharness`), resolved by the caller (`orchestrator._capacity_admission`). The budget stays the launcher's (`prepare`, `take_hold`, `WaveBudget`): the scheduler is given a
budget that cannot bind (share 1 of 10**9), so a spent budget is still `BUDGET_EXHAUSTED` as before.

**What the wave was admitted against is held (plan row P3-04, capacity chain step C4).** A judgement is not a claim: two
controllers could each be told "one session is free". `admit_wave` therefore takes the admission and the hold in one
locked step (`reservation.CapacityReservations.reserve_atomically`): what is over is ended first (an owner that is gone,
a hold past its ttl, this owner's own earlier wave), the holds are read, the scheduler decides, and one
`machine/host.sessions` session per admitted node is written to `<runs root>/_capacity-reservations.json` before the lock
is let go. `live_holds` counts the open holds of live owners as sessions, one per row and one per lease, **but one run, one session**
(P3-04, capacity chain step C6): the admission names the lease of each node's run in the row it holds (`runs=`, from the
controller's `node.run_id or node.id`, kept under `rundrive.lease_name`), and a session row whose lease is a live lease of the
`hoh run` namespace (`rundrive.LEASE_PROJECT`) is not counted again -- a lease absorbs **at most one** row (so two owners
holding a node of the same name stay two), only the session row (the provider's sessions, the bytes and the inodes of the row are
counted in full), and only the lease of that namespace. Run names and node ids are otherwise project-local, so a row that names
no lease, or a lease of another project, is never merged: a run that has a lease **and** a row that names none counts twice --
over-counted, never under-counted (not the caller's own rows, whose earlier wave is over when it asks). So `hoh capacity check`,
another process, sees what a running wave holds. A ledger or lock that cannot be used admits nobody. The same link keeps the row
while the lease is live (`reservation.CapacityReservations.judge`).
`live_holds` and `check` remain read-only; `admit_wave` is the one writer here.

**Storage is one more capacity of the same admission (plan row P3-39, the remaining two thirds).** Slice 1 gave the broker
a judgement over the admission ledger's bytes and inodes (`broker.storage_facts`); nothing called it. `admit_wave` now
does, when it is handed a `storage` reader: each node of the wave wants, beside its session, the **bytes** one run is
predicted to write (the ledger's own history-windowed estimate of a role run and of its temporary workspace,
`StorageAdmission.estimate`) and the **inodes** it is predicted to create (the same window over the inode counts the product
recorded when earlier dispatches and workspaces closed, `StorageAdmission.estimate_inodes`, and never below the floor
`RUN_INODES`, as the bytes never fall below the policy's default). The scheduler admits the nodes in order, each one's want counted as held for the next, so a wave whose
**combined** reservation would cross the reserve is cut where it would cross it, and a node that can never fit
(`CRITICAL`: the limit is zero) is refused. What is admitted is held in the same locked step as its sessions: one
`machine/storage.bytes` and one `machine/storage.inodes` row per node in `<runs root>/_capacity-reservations.json`, so
another controller, and `hoh capacity show --source`, see what a running wave holds. The figures are the ledger's own
snapshot (`StorageAdmission.status()`): free space above the **reserve** is the limit, the ledger's open reservations (and
the other waves' rows) are the holds, a figure that cannot be read is an absent fact and the wave is refused. The rows are
rows of the same ledger as the sessions' and end as they do (`hoh.reservation`: the owner's next ask, its death, their ttl,
a settle). The ledger's reservation of a run that has started is counted beside the wave's row for the same run:
over-counted, never under-counted.

**The reserve for critical nodes, the provider's state and the record (plan rows P3-17, P3-19 and P3-16, slice 3/3/4).** Three more
things stand in the same locked step, and each is opt-in by what the operator declared:

* *The reserve.* A registry that records `machine/host.sessions.critical` (`RESERVE`, unit `sessions`, declared with `hoh capacity
  set machine host.sessions.critical N sessions`) keeps that many of the host's sessions for **critical nodes** -- a repair node
  (`TaskNode.repair_of`, the QA/integration work a failed gate creates: the class `launcher.qa_reserve` already keeps a budget for) and a
  node whose run already started (`TaskNode.run_id`, the iteration that is under way and must not starve behind fresh work). The
  controller names them (`critical=`). The ordinary nodes are admitted first, as if the reserve were held by someone; the critical nodes
  are then admitted over what is left, so the reserve is never taken by an ordinary node, and a waiting ordinary node does not hold a
  critical one back (`behind` is per class). The reserve is read like every other fact: a stale, future or foreign-unit record admits no
  ordinary node (the broker's own reasons), a missing record is no reserve. It is a number of **sessions**; storage and the providers
  have none.
* *The provider's state.* A node of a launcher whose runs use the provider `P` also wants one `provider/P.sessions` (`sessions`) **when the
  registry declares it**; the capacity source (`hoh.capacitysource`) is **taken here**, over the same registry and the holds just read,
  and what its provider state -- a usage limit, a rate limit, a park the resume loop gave up on, a reading that is incomplete -- holds back
  of that capacity is held against it, so a provider that is `LIMITED` admits no node that needs its session (`providers=`:
  `backpressure.providers_of` of the launcher; `None` = it cannot be told, every declared `<provider>.sessions` counts). The snapshot is
  observed at one clock reading and judged at the next, so a reading older than its bound by then (a controller that was suspended or
  slow between the two) is UNKNOWN: slots that are UNKNOWN admit nobody, a provider reading that is UNKNOWN holds the whole of every
  provider capacity. The rows a wave holds of the provider capacity are counted by the next wave like the sessions' are. A run that is
  already running is not attributed to a provider (a lease records none), so the limit is "reserved by waves", not "in use".
* *The record.* When the caller names a directory and an id (`record_dir=`, `decision_id=`) the admission is written
  (`hoh.capacityrecord`: create-only, bound by its own sha256, read back) **before** its rows are held: its inputs (the policy, what
  each class wants, the holds it was handed, what the provider state held back, the provider and slot facts), the registry's digest and
  the snapshot's, the plan with each node's verdict and reason, the nodes admitted and the rows held. A record that cannot be written,
  read back or counted admits nobody and holds nothing. A crash between the record and the rows leaves a record whose rows were never
  held; the next ask decides again and writes another. The record says what was decided, the ledger what is held.

**Opt-in by the operator's storage policy.** A state root with no `storage-policy.json` is "no storage policy": the wave
is judged on sessions only, as before. Once the file exists everything that cannot be known (a policy that is a link or a
directory, one that `StoragePolicy` refuses, a ledger that cannot be opened or read, a snapshot with a malformed
reservation) admits **nobody**. A grant is still a judgement over one snapshot: the ledger's `reserve()` of each run stays
the authority.
"""
from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import capacityrecord, rundrive
from .broker import (STORAGE_BYTES, STORAGE_INODES, STORAGE_KIND, Amount, BrokerError, Decision, Policy, decide,
                     storage_facts)
from .capacity import KINDS, CapacityError, Registry, parse, render
from .capacity import location as registry_location
from .capacity import read as read_registry
from .reservation import BudgetReservations, CapacityReservations, ReservationError
from .runlease import DIRNAME, LIVE, RunLeaseError, RunLeases
from .scheduler import MAX_BUDGET, Candidate, Plan, admit

SESSIONS = ("machine", "host.sessions", "sessions")
"""`(kind, name, unit)` every live run lease holds one of."""

DISPATCHES = ("provider", "dispatches.reserved", "requests")
"""`(kind, name, unit)` the open budget reservations hold their amounts of."""

DEFAULT_DECLARED_AGE = 7 * 24 * 60 * 60
"""How old a *declared* limit may be before the broker calls it stale: a week. A declared limit is the operator's
statement and changes by the operator's act; re-running `hoh capacity set` refreshes it."""

DEFAULT_MEASURED_AGE = 5 * 60
"""How old a *measured* figure may be: five minutes. A measurement is a reading of something that moves (what a
host can carry, what a provider has left); an old one is worse than none."""

MAX_PROJECTS = 256
"""Directories of `<runs root>/projects` that are read; more is refused (a host does not have more, and an
unbounded scan is not a fact)."""

MAX_LEASES = 1024
"""LIVE run leases that are counted; more is refused (the broker itself looks at at most 1024 holds)."""

MAX_SCANNED = 16384
"""Lease files (`<run>.json`) that are read in total, live or not: files are never removed, so this bounds the scan,
not the holds. More is refused with its own message; pruning is P3-18's."""

RUN_INODES = 50_000
"""The **permanent floor** of the inodes one run is predicted to create (P3-39), exactly as the policy's default estimate is the
floor of the bytes (`storage_pressure.estimate` is `max(default, 1.5 x p95)`): a node wants `max(RUN_INODES, the history's
prediction)`. The history (`estimate_inodes` of `role_run` plus of `tmp_workspace`, written when a dispatch or a workspace closes)
raises the want above the floor on a host whose runs are larger than it; it never lowers it. A run's linked worktree of this
repository is about 44 000 entries, and the run's temporary workspace adds to it, which is why the floor is deliberately above the
one reading there was."""

MAX_POLICY_BYTES = 64 * 1024
"""The largest `storage-policy.json` that is read: the policy is a few dozen numbers; a file of any other size is not one."""

STORAGE_TIMEOUT = 10.0
"""Seconds the wave's admission waits for the storage ledger to answer. The ledger's lock is a blocking `flock` (it was made
for processes that hold it for the length of one observation), so a ledger that is held for ever must not hold the controller
for ever: after this many seconds the storage is "did not answer", which admits nobody. The wait is before the capacity
ledger's lock is taken, so it delays this controller only."""

RESERVE = ("machine", "host.sessions.critical", "sessions")
"""`(kind, name, unit)` of the capacity the operator records to keep sessions for critical nodes: its limit is how many
of `SESSIONS`' limit an ordinary node may not take. No such record is no reserve."""

PROVIDER_SESSIONS = ".sessions"
"""The suffix of the provider capacity a node of that provider wants one of: `<provider>.sessions`, unit `sessions`."""

MAX_PROVIDER_WANTS = 16
"""Provider session capacities one wave wants; more is refused (a node's wants are at most 64 in all)."""

POLICY_FILE = "storage-policy.json"
"""The operator's storage policy, in the state root: its existence is what makes a wave storage-judged (see the module
text), its content is `StoragePolicy`'s."""

_NAMES_PER_DIRECTORY = 2 * MAX_SCANNED
"""Names of one lease directory that are listed (leases, the lock and the parked copies of takeovers)."""

_SUFFIX = ".json"


class CheckError(ValueError):
    """A decision that could not be taken because a fact it needs cannot be known. `reason` is `holds` (a lease,
    a ledger or a directory cannot be read exactly), `invalid` (a bound, a runs root or a policy age breaks a rule)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"capacity check: {message}")
        self.reason = reason


@dataclass(frozen=True)
class Holds:
    """What is held, as read: the runs root, how many project directories and lease files were looked at, the live
    sessions, the reserved dispatches, the sums of the `machine/storage.bytes` and `machine/storage.inodes` rows that
    other owners' waves hold (`storage_bytes`, `storage_inodes`, P3-39), and the `Amount`s `decide` is given.

    `reserved` is the session rows of other owners **counted beyond their lease** (a row whose run holds a live `hoh run`
    lease is counted once, through the lease: C6); before C6 it was every session row of another owner. `absorbed` is the
    difference -- the rows a live lease absorbed -- so `sessions - reserved` is still the live leases and `reserved +
    absorbed` is what the session rows of other owners held."""

    runs_root: str
    projects: int
    lease_files: int
    sessions: int
    dispatches: int
    amounts: tuple[Amount, ...]
    reserved: int = 0
    storage_bytes: int = 0
    storage_inodes: int = 0
    absorbed: int = 0

    def as_dict(self) -> dict:
        return {"runs_root": self.runs_root, "projects": self.projects, "lease_files": self.lease_files,
                "sessions": self.sessions, "dispatches": self.dispatches, "reserved": self.reserved,
                "absorbed": self.absorbed,
                "storage_bytes": self.storage_bytes, "storage_inodes": self.storage_inodes,
                "amounts": [{"kind": a.kind, "name": a.name, "amount": a.amount, "unit": a.unit}
                            for a in self.amounts]}


def policy(declared: int = DEFAULT_DECLARED_AGE, measured: int = DEFAULT_MEASURED_AGE) -> Policy:
    """A broker `Policy` from two ages in seconds -- one for declared limits, one for measured ones, the same for
    every kind. `CheckError` (`invalid`) for an age that is not a whole number from 0 to `broker.MAX_AGE`."""
    try:
        return Policy({(kind, source): age for kind in KINDS
                       for source, age in (("declared", declared), ("measured", measured))})
    except BrokerError as exc:
        raise CheckError("invalid", str(exc)) from exc


def _directory(path: Path, what: str) -> os.stat_result | None:
    """`lstat` of a directory that is a directory of its own; `None` when it is absent; `CheckError` otherwise."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CheckError("holds", f"{what} {path.name!r}: {getattr(exc, 'strerror', None) or type(exc).__name__}") \
            from exc
    if not stat.S_ISDIR(info.st_mode):
        raise CheckError("holds", f"{what} {path.name!r} is not a directory of its own")
    return info


def _names(directory: Path, limit: int, what: str) -> list[str]:
    try:
        with os.scandir(directory) as scan:
            names = []
            for entry in scan:
                names.append(entry.name)
                if len(names) > limit:
                    raise CheckError("invalid", f"{what}: more than {limit}")
    except OSError as exc:
        raise CheckError("holds", f"{what}: {exc.strerror or type(exc).__name__}") from exc
    return sorted(names)


def live_holds(runs_root: str | os.PathLike, **environment) -> Holds:
    """The holds the product records under `runs_root`, read-only (see the module text). `environment` is
    `RunLeases`' injectable facts (clock, boot, host, pid, alive, pidns, start), for a test that has to say "the
    holder's process is gone" without killing one. `CheckError` when anything cannot be known."""
    try:
        root = Path(os.fspath(runs_root)).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise CheckError("holds", f"the runs root cannot be resolved: {type(exc).__name__}") from exc
    if _directory(root, "the runs root") is None:
        raise CheckError("holds", f"the runs root {root.name!r} does not exist (pass --root)")
    projects = 0
    leased = 0                                            # live run leases: one session each, per lease file
    live_runs: set[str] = set()                           # the names of the live leases of the `hoh run` namespace
    found: list[tuple[str, Path, list[str]]] = []
    projects_dir = root / "projects"
    if _directory(projects_dir, "projects") is not None:
        for name in _names(projects_dir, MAX_PROJECTS, "project directories"):
            if _directory(projects_dir / name, "project directory") is None:
                continue
            directory = projects_dir / name / DIRNAME
            if _directory(directory, f"the run leases of project {name!r}: directory") is None:
                continue
            projects += 1
            listed = [n for n in _names(directory, _NAMES_PER_DIRECTORY, "lease directory entries")
                      if n.endswith(_SUFFIX)]            # not `.lock` nor the parked copies `<run>.json.g<N>`
            found.append((name, directory, listed))
    scanned = sum(len(listed) for _, _, listed in found)
    if scanned > MAX_SCANNED:                             # before any file is read
        raise CheckError("invalid", f"lease files: more than {MAX_SCANNED} (released leases are never removed; "
                                    f"prune them before the live holds can be read)")
    for name, directory, listed in found:
        leases = RunLeases(directory, **_lease_facts(environment))
        for file_name in listed:
            try:
                lease = leases.read(file_name[:-len(_SUFFIX)])
                state = None if lease is None else leases.judge(lease)[0]
            except RunLeaseError as exc:
                raise CheckError("holds", f"the run lease {file_name!r} of project {name!r} cannot be "
                                          f"read ({exc.reason}): not a free run") from exc
            if state == LIVE:
                leased += 1
                if name == rundrive.LEASE_PROJECT:
                    live_runs.add(file_name[:-len(_SUFFIX)])
                if leased > MAX_LEASES:
                    raise CheckError("invalid", f"live run leases: more than {MAX_LEASES}")
    try:
        dispatches = sum(row.amount for row in BudgetReservations(root).held())
    except ReservationError as exc:
        raise CheckError("holds", f"the budget reservations cannot be read: {str(exc)[:160]}") from exc
    try:                                                  # the capacity a wave was admitted against, by its owner
        ledger = CapacityReservations(root, **_owner_facts(environment))
        # not the caller's own: when its owner asks, its earlier wave is over (the rule that ends it), so a caller
        # never counts its own reservation against itself -- `hoh capacity check` is another process and does
        reserved = [row for row in ledger.live()
                    if (row.owner_pid, row.owner_thread) != (ledger.pid, ledger.thread)]
    except ReservationError as exc:
        raise CheckError("holds", f"the capacity reservations cannot be read: {str(exc)[:160]}") from exc
    others: dict[tuple[str, str, str], int] = {}
    reserved_sessions = 0
    absorbed: set[str] = set()                            # the leases that already stand for a row: at most one each
    for row in reserved:
        if (row.kind, row.name, row.unit) == SESSIONS:
            if row.lease in live_runs and row.lease not in absorbed:
                absorbed.add(row.lease)                   # one run, one session: the lease counted it
            else:
                reserved_sessions += 1                    # one session per row: run names are project-local
        else:
            key = (row.kind, row.name, row.unit)
            others[key] = others.get(key, 0) + row.amount
    sessions = leased + reserved_sessions
    dispatches_held = dispatches + others.pop(DISPATCHES, 0)   # one Amount per key: a reserved dispatch row joins the budget's
    amounts = (Amount(*SESSIONS[:2], sessions, SESSIONS[2]), Amount(*DISPATCHES[:2], dispatches_held, DISPATCHES[2]),
               *(Amount(kind, name, amount, unit) for (kind, name, unit), amount in sorted(others.items())))
    stored = {key: amount for key, amount in others.items() if key[:2] == (STORAGE_KIND, STORAGE_BYTES)
              or key[:2] == (STORAGE_KIND, STORAGE_INODES)}
    return Holds(str(root), projects, scanned, sessions, dispatches, amounts, reserved_sessions,
                 sum(a for key, a in stored.items() if key[1] == STORAGE_BYTES),
                 sum(a for key, a in stored.items() if key[1] == STORAGE_INODES), len(absorbed))


_LEASE_KEYS = ("clock", "boot", "host", "pid", "alive", "pidns", "start", "lock_wait_seconds")
_OWNER_KEYS = ("clock", "boot", "host", "pid", "alive", "pidns", "start", "thread", "live_threads",
               "ttl_seconds", "lock_timeout")


def _lease_facts(environment: dict) -> dict:
    """The injectable facts `RunLeases` takes, out of the one `environment` a caller passes (a test says "the holder is
    gone" once, for the leases and for the reservations)."""
    return {key: environment[key] for key in _LEASE_KEYS if key in environment}


def _owner_facts(environment: dict) -> dict:
    """The injectable facts `CapacityReservations` takes, out of the same `environment`."""
    return {key: environment[key] for key in _OWNER_KEYS if key in environment}


def check(registry: Registry, wants: Iterable[Amount], *, runs_root: str | os.PathLike, now: str,
          policy_: Policy | None = None, **environment) -> tuple[Decision, Holds]:
    """`(decision, holds)`: `broker.decide` on `registry` at `now` for `wants`, beside what `live_holds` reads under
    `runs_root`. `policy_` defaults to `policy()`. `CheckError` when a hold cannot be known, `BrokerError` where
    `decide` raises. Reads no clock (`now` is the caller's) and writes nothing."""
    holds = live_holds(runs_root, **environment)
    decision = decide(registry, policy() if policy_ is None else policy_, now, wants, holds.amounts)
    return decision, holds


@dataclass(frozen=True)
class Gate:
    """What the registry says about a wave's nodes. `configured` is False only when there is nothing to ask (no
    state root, no registry record): `admitted` is then every node offered and `why` says so. Otherwise
    `admitted` are the node ids that may start (in the order offered), `waits` is `(node id, reason)` for each of
    the others, `why` is one bounded line, `registry` the digest of the record that was judged (`None` when it could
    not be read) and `plan` the scheduler's `Plan.as_dict()` (`None` when no plan was made). `reserved` names the nodes
    the wave holds capacity for (each holds its session and, when storage is judged, its bytes and inodes) and `ended` how
    many earlier rows asking ended. `snapshot` is the digest of what the capacity source said when the wave was judged and
    `record` the file name of the admission record written (`None` when none was asked for or nothing was judged)."""

    configured: bool
    admitted: tuple[str, ...]
    waits: tuple[tuple[str, str], ...]
    why: str
    registry: str | None = None
    plan: dict | None = None
    reserved: tuple[str, ...] = ()
    ended: int = 0
    snapshot: str | None = None
    record: str | None = None


@dataclass(frozen=True)
class StorageReading:
    """What a wave's storage is judged on: the admission ledger's `snapshot` (the dict `StorageAdmission.status()`
    returns; `broker.storage_facts` reads it whole or not at all), the bytes one run is predicted to write
    (`run_bytes`) and the inodes it is predicted to create (`run_inodes`, never below the floor `RUN_INODES`;
    `inodes_measured` says whether it is the ledger's measured history, which then exceeds the floor, or the floor)."""

    snapshot: object
    run_bytes: int
    run_inodes: int
    inodes_measured: bool = False


def storage_reader(state_root: str | os.PathLike, *, factory: Callable | None = None,
                   inodes: int = RUN_INODES) -> Callable[[], StorageReading | None]:
    """A function that reads the storage a wave is judged on, or says there is none to judge by.

    It answers `None` -- the wave is not storage-judged -- only when the state root has **no** `storage-policy.json`
    (`ENOENT` on that name and nothing else). The policy must be a regular file of its own; a link, a directory, a FIFO or
    a name that cannot be examined is `CheckError`, never "no policy". The admission is `factory(state_root)` when one is
    given (the launcher's own injectable seam), else `StorageAdmission.for_state_root` over `StoragePolicy.load` of the
    file; the reading is its `status()` (which, as `hoh storage status` does, renames the stale reservations it finds and
    records the observation), and the bytes of one run are `estimate("role_run") + estimate("tmp_workspace")` -- the ledger's
    own history-windowed figures, what `resumeloop` asks before it resumes a run -- and the inodes of one run are
    `estimate_inodes("role_run") + estimate_inodes("tmp_workspace")`, the same window over the inode counts the product
    recorded when those dispatches and workspaces closed, **never below `inodes`** (the permanent floor `RUN_INODES`, as
    `estimate` keeps `max(default, ...)`): a kind with no record counts 0 toward the history, and a history below the floor
    leaves the want at the floor. Anything the admission raises is
    the caller's to take as "cannot be judged"; so is an admission that does not answer within `STORAGE_TIMEOUT`."""
    def ask(root: Path, policy_file: Path) -> StorageReading:
        from .storage_pressure import StorageAdmission, StoragePolicy

        admission = factory(root) if factory is not None else \
            StorageAdmission.for_state_root(root, StoragePolicy.load(policy_file))
        snapshot = admission.status()
        parts = [admission.estimate_inodes(kind) for kind in ("role_run", "tmp_workspace")]
        history = sum(part for part in parts if part is not None)
        from_history = history > inodes                           # the history's figure, because it is above the floor
        return StorageReading(snapshot, admission.estimate("role_run") + admission.estimate("tmp_workspace"),
                              max(history, inodes), from_history)

    def read() -> StorageReading | None:
        if type(inodes) is not int or inodes < 1:
            raise CheckError("invalid", "the inodes of a run are a whole number from 1")
        root = Path(os.fspath(state_root))
        policy_file = root / POLICY_FILE
        try:
            info = os.lstat(policy_file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CheckError("holds", f"{POLICY_FILE}: {getattr(exc, 'strerror', None) or type(exc).__name__}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CheckError("invalid", f"{POLICY_FILE} is not a file of its own")
        if info.st_size > MAX_POLICY_BYTES:
            raise CheckError("invalid", f"{POLICY_FILE} is larger than {MAX_POLICY_BYTES} bytes")
        box: dict = {}

        def work() -> None:
            try:
                box["reading"] = ask(root, policy_file)
            except BaseException as exc:              # noqa: BLE001 - handed to the caller, which fails closed
                box["error"] = exc

        worker = threading.Thread(target=work, name="storage-reading", daemon=True)
        worker.start()
        worker.join(STORAGE_TIMEOUT)
        if worker.is_alive():                         # a ledger lock that is not let go: the wave waits for no one
            raise CheckError("holds", f"the storage ledger did not answer within {STORAGE_TIMEOUT:g} s")
        if "error" in box:
            raise box["error"]
        return box["reading"]

    return read


def _storage_wants(reading: StorageReading) -> tuple[Amount, ...]:
    """What one node of the wave wants of the storage: its bytes always (so a snapshot that cannot be read cannot be walked
    around by leaving storage out), its inodes unless the snapshot says the filesystem has no inode table to run out of."""
    wants = [Amount(STORAGE_KIND, STORAGE_BYTES, max(1, reading.run_bytes), "bytes")]       # a run is never wanted at 0: CRITICAL (limit 0) refuses it
    snapshot = reading.snapshot
    unmeasured = False
    try:
        unmeasured = snapshot.get("inode_state") == "NOT_MEASURED"        # type: ignore[union-attr]
    except Exception:                                 # noqa: BLE001 - a snapshot that cannot be read has no such fact
        pass
    if not unmeasured:
        wants.append(Amount(STORAGE_KIND, STORAGE_INODES, reading.run_inodes, "items"))
    return tuple(wants)


def _with_storage(registry: Registry, reading: StorageReading) -> tuple[Registry, tuple[Amount, ...]]:
    """`(registry, holds)` with the storage the snapshot states as two more capacities and the ledger's holds on them:
    exactly `broker.decide_with_storage`'s construction, for the scheduler's call. The operator's registry must not
    record a storage name itself (one writer per capacity). `CheckError` when it does or the two do not fit."""
    taken = {(entry.kind, entry.name) for entry in registry.entries}
    if {(STORAGE_KIND, STORAGE_BYTES), (STORAGE_KIND, STORAGE_INODES)} & taken:
        raise CheckError("invalid", "the registry records a storage capacity itself; the storage ledger states it")
    facts = storage_facts(reading.snapshot)
    try:
        return parse(render(registry.entries + facts.entries)), facts.holds
    except CapacityError as exc:
        raise CheckError("invalid", f"the registry and storage do not fit one registry: {exc}") from exc


def _shorten(text: str, root: object) -> str:
    """`text` with the runs root's path written `<runs root>`: a reason is cut at a few hundred characters and a long
    path in front of the cause (a lock held, a ledger unreadable) must not push the cause out."""
    try:
        for spelled in {os.fspath(root), str(Path(os.fspath(root)).expanduser())}:
            if spelled:
                text = "<runs root>".join(text.split(spelled))
    except Exception:                                 # noqa: BLE001 - a root that is not a path is left as it is
        pass
    return text


def _state(reading: StorageReading) -> str:
    """The pressure state the snapshot states, or `unreadable`."""
    try:
        state = reading.snapshot.get("state")         # type: ignore[union-attr]
    except Exception:                                 # noqa: BLE001
        return "unreadable"
    return state if type(state) is str and state.isalpha() and state.isupper() and len(state) <= 16 else "unreadable"


_STORAGE_NAMES = ((STORAGE_KIND, STORAGE_BYTES), (STORAGE_KIND, STORAGE_INODES))


def _explain(placement, narrowed: Iterable = (), reserve: int = 0) -> str:
    """One line for a node that does not start: the scheduler's verdict and reason, and the figures behind it. The line
    is the one that decided: the capacity the verdict rests on, which with storage is not always the first. `narrowed`
    are the provider capacities the provider state held back and `reserve` the sessions kept for critical nodes that an
    ordinary node was judged beside: both are part of why a capacity reads as held."""
    decision = placement.decision
    line = next((l for l in decision.lines if (l.verdict, l.reason) == (decision.verdict, decision.reason)),
                decision.lines[0])
    name = f"{line.kind}/{line.name}"
    head = f"{placement.verdict}/{placement.reason}"
    if (line.kind, line.name) in _STORAGE_NAMES and placement.reason in ("unknown", "stale", "future", "unit"):
        return (f"{head}: {name} is not stated by the storage ledger now (it cannot be read, a reservation in it is "
                f"malformed, the host cannot be observed, or the reading is outside the policy age)")
    if placement.reason == "unknown":
        return f"{head}: {name} is not recorded in the capacity registry (hoh capacity set {line.kind} {line.name} N {line.unit})"
    if placement.reason == "stale":
        return f"{head}: {name} was stated {line.age} s ago, older than the policy age (hoh capacity set refreshes it)"
    if placement.reason == "future":
        return f"{head}: {name} is stamped after now"
    if placement.reason == "unit":
        return f"{head}: {name} is recorded in another unit than {line.unit}"
    if placement.reason == "exceeds":
        return f"{head}: {line.wanted} {line.unit} of {name} is more than its limit {line.limit}"
    if placement.reason == "exhausted":
        held = f"{head}: {line.in_use} of {line.limit} {line.unit} of {name} are held"
        back = next((n for n in narrowed if (n.kind, n.name) == (line.kind, line.name)), None)
        if back is not None:
            held += f"; the provider state holds back {back.held} of them ({back.rule}): {back.by}"
        if reserve and (line.kind, line.name) == SESSIONS[:2]:
            held += f"; {reserve} of them are kept for critical nodes ({RESERVE[0]}/{RESERVE[1]})"
        return held
    return f"{head}: {name}"


def _provider_wants(registry: Registry, providers: Sequence[str] | None) -> tuple[Amount, ...]:
    """One session of each provider capacity `<provider>.sessions` the registry declares, for the providers a wave's
    runs use (`None`: every one declared). A provider whose session capacity nobody declared is not asked for."""
    if providers is not None and (isinstance(providers, (str, bytes)) or not all(type(p) is str for p in providers)):
        raise CheckError("invalid", "the providers of a wave are a list of names")
    wanted = [Amount("provider", entry.name, 1, "sessions") for entry in registry.entries
              if entry.kind == "provider" and entry.name.endswith(PROVIDER_SESSIONS)
              and (providers is None or entry.name[:-len(PROVIDER_SESSIONS)] in providers)]
    if len(wanted) > MAX_PROVIDER_WANTS:
        raise CheckError("invalid", f"a wave wants more than {MAX_PROVIDER_WANTS} provider session capacities")
    return tuple(wanted)


def _inputs(snapshot, wants: Iterable[Amount], *, now: str) -> tuple[tuple[Amount, ...], tuple]:
    """`(holds, narrowed)`: what a decision at `now` over `wants` is handed beside the snapshot's registry -- the snapshot's
    holds and, for each wanted provider capacity the provider state holds back, one more hold of what it holds back --
    and the narrowings themselves (`capacitysource.narrowing`). `CheckError` (`holds`) when the slots are UNKNOWN at `now`
    (`capacitysource.decide`'s rule: no decision is taken on a hold that cannot be known). Pure."""
    from . import capacitysource

    if snapshot.holds is None:
        raise snapshot.holds_error
    stale = next((f for f in capacitysource.facts(snapshot, now=now)
                  if f.family == "slots" and f.status == capacitysource.UNKNOWN), None)
    if stale is not None:
        raise CheckError("holds", f"the holds are UNKNOWN: {stale.why}")
    narrowed = capacitysource.narrowing(snapshot, wants, now=now)
    extra = [Amount(item.kind, item.name, item.held, item.unit) for item in narrowed if item.held]
    return (*snapshot.holds.amounts, *extra), narrowed


def _admit_classes(registry: Registry, policy_: Policy, moment: str, nodes: Sequence[str], critical: Collection[str],
                   ordinary: tuple[Amount, ...], needed: tuple[Amount, ...], holds: tuple[Amount, ...],
                   reserve: int | None) -> Plan:
    """The wave's plan. Without a reserve (`None`) it is one `admit` over every node in the caller's order. With one, the
    ordinary nodes are admitted first beside `reserve` more sessions held by nobody in particular, then the critical nodes
    over what is left (the claims of the first walk are holds of the second), so an ordinary node never takes the reserve
    and a waiting ordinary node does not hold a critical one back; the placements are returned in the caller's order."""
    if reserve is None or not nodes:                  # an empty wave is `admit`'s own refusal, reserve or not
        return admit(registry, policy_, moment, [Candidate(node, needed, 1) for node in nodes], holds, MAX_BUDGET, MAX_BUDGET)
    claimed: dict[tuple[str, str], tuple[int, str]] = {}
    placed = {}
    for ids, wants, kept in (([n for n in nodes if n not in critical], ordinary, reserve),
                             ([n for n in nodes if n in critical], needed, 0)):
        if not ids:
            continue
        beside = tuple(Amount(kind, name, total, unit) for (kind, name), (total, unit) in claimed.items())
        kept_back = (Amount(*SESSIONS[:2], kept, SESSIONS[2]),) if kept else ()
        walk = admit(registry, policy_, moment, [Candidate(node, wants, 1) for node in ids], (*holds, *beside, *kept_back),
                     MAX_BUDGET, MAX_BUDGET)
        for placement in walk.placements:
            placed[placement.node] = placement
            if placement.verdict == "admit":
                for want in wants:
                    total, _ = claimed.get((want.kind, want.name), (0, want.unit))
                    claimed[(want.kind, want.name)] = (total + want.amount, want.unit)
    placements = tuple(placed[node] for node in nodes)
    return Plan(registry=registry.digest, now=moment, budget_ceiling=MAX_BUDGET, budget_left=MAX_BUDGET,
                budget_after=MAX_BUDGET - sum(1 for p in placements if p.verdict == "admit"), placements=placements)


def _listed(amounts: Iterable[Amount]) -> list[list]:
    return [[a.kind, a.name, a.amount, a.unit] for a in amounts]


def admit_wave(node_ids: Iterable[str], *, runs_root: str | os.PathLike, state_root: str | os.PathLike | None,
               now: str | Callable[[], str], policy_: Policy | None = None,
               storage: Callable[[], StorageReading | None] | None = None, critical: Iterable[str] = (),
               providers: Sequence[str] | None = None, record_dir: str | os.PathLike | None = None,
               decision_id: str | None = None, width: int | None = None, ready: int | None = None,
               runs: Mapping[str, str] | None = None, **environment) -> Gate:
    """Which of the nodes `node_ids` (the wave's candidates, in priority order) may start at `now`? See the module
    text. **Does not raise**: every failure of its own work -- a `state_root` or `runs_root` that is not a path, a
    record that cannot be examined, anything the readers raise -- is `Gate(configured=True, admitted=())` with every
    node waiting and the exception's type in the reason; only a **missing** record (`ENOENT`) or no state root at
    all is "not configured". Reads no clock itself: `now` is the caller's, a time or a function that gives one, which is
    called **after** the registry and the holds were read (`broker.decide`: a fact written after `now` is `future`, so a
    concurrent `hoh capacity set` must not be judged by an earlier clock) -- once for the moment the capacity source's
    snapshot is observed and once more for the moment it is judged and the decision is taken, so a reading that is older
    than its bound by then is UNKNOWN and admits nobody. Writes only the reservation ledger of
    `runs_root` (the nodes it admits are held there in the same locked step), the admission record when it is asked for
    and, when it judges storage, what the admission ledger records when it is read (`StorageAdmission.status()`: the
    observation, the pressure state, the stale reservations it renames); nothing at all when no registry is declared.

    `runs` (C6) is a mapping of node id to the id of the node's run (`node.run_id or node.id`): the rows held for a node name the
    lease of that run (`rundrive.lease_name`), which keeps them while the lease is live and lets `live_holds` count the run's
    session once. A node it does not name holds rows that name no lease; `runs` that is not such a mapping is a bad request
    (`CheckError(invalid)`): nobody is admitted.

    `critical` are the ids among `node_ids` that may take the reserve (see the module text; ids that are not offered are
    ignored), `providers` the providers the runs use (`None`: every declared provider session capacity counts).
    `record_dir` and `decision_id` are given together: the admission is then written as `capacity-<decision_id>.json` in that
    directory **before** its rows are held, and an admission that cannot be recorded admits nobody; `width` and `ready` (the
    controller's wave width and how many nodes were ready) are quoted in it.

    `storage` (P3-39) is a reader (`storage_reader`): called once, **before** the reservation lock is taken (a read that
    waits must not hold every other controller's admission), after the state root was found initialised. A
    `StorageReading` makes every node want its bytes and inodes beside its session, and what is admitted holds them;
    `None` from the reader (no storage policy in the state root) or no reader at all is the wave as it was. A reader
    that raises is "cannot be judged": nobody is admitted. The capacity ledger's holds are still read under the lock, so a
    reading older than the decision cannot hide a row another controller has written; **but the reading's own figures can
    be old**: its `reserved_bytes` (the admission ledger's open reservations) can miss a `reserve()` that lands while the
    caller waits for the lock, and its free space is a moment old. The window is seconds wide (the lock wait is bounded by
    the ledger's 5 s, the read by `STORAGE_TIMEOUT`), and the run's own `reserve()` stays the authority."""
    try:
        nodes = tuple(node_ids)
        if any(type(node) is not str for node in nodes):
            raise TypeError("a node id is a string")
    except Exception as exc:                          # noqa: BLE001 - nothing to wait for: a bad request
        why = f"capacity cannot be judged ({type(exc).__name__}): the nodes of the wave cannot be listed"
        return Gate(True, (), (), why)
    try:
        if state_root is None:
            return Gate(False, nodes, (), "no storage state root is resolved: the wave is not capacity-gated")
        path = registry_location(state_root)
        try:
            os.lstat(path)                            # the record itself, no link followed
        except FileNotFoundError:                     # the one answer that is "nobody declared limits"
            return Gate(False, nodes, (), f"no capacity registry at {path.name} in the state root: the wave is "
                                          f"not capacity-gated (hoh capacity set declares one)")
        from . import capacitysource
        from .storage_registry import StorageRegistry

        StorageRegistry(state_root)._require_init()
        named = (critical,) if isinstance(critical, (str, bytes)) else tuple(critical)
        if isinstance(critical, (str, bytes)) or not all(type(c) is str for c in named):
            raise CheckError("invalid", "the critical nodes of a wave are a list of node ids")
        flagged = frozenset(named) & set(nodes)
        if runs is not None and (not isinstance(runs, Mapping) or not all(
                type(k) is str and type(v) is str for k, v in runs.items())):
            raise CheckError("invalid", "the runs of a wave are a map of node ids to run ids")
        links = {} if runs is None else {node: rundrive.lease_name(run) for node, run in runs.items()}
        if (record_dir is None) != (decision_id is None):
            raise CheckError("invalid", "a record needs both its directory and its decision id")
        for what, figure in (("width", width), ("ready", ready)):
            if figure is not None and (type(figure) is not int or not 0 <= figure <= 10 ** 6):
                raise CheckError("invalid", f"the {what} of a wave is a whole number or nothing")
        reading = None if storage is None else storage()      # before the lock: it may wait (STORAGE_TIMEOUT)
        judged: dict = {}

        def decide_now() -> list[tuple[str, str, str, int, str, str | None]]:
            """Runs under the reservation lock, after what is over was ended: the registry, the holds (the ledger as
            just written), the capacity source's snapshot of them and one admission over them and the storage read
            before; what it admits is held by the caller in the same locked write, and the admission is recorded first."""
            registry = read_registry(path)
            used = policy() if policy_ is None else policy_
            observed = now() if callable(now) else now        # after the registry was read, not before
            snapshot = capacitysource.take(registry, runs_root=runs_root, now=observed, policy=used, **environment)
            reserve_entry = next((e for e in registry.entries if (e.kind, e.name) == RESERVE[:2]), None)
            needed = (Amount(*SESSIONS[:2], 1, SESSIONS[2]),)
            judged_on, ledger = registry, ()
            if reading is not None:
                judged_on, ledger = _with_storage(registry, reading)
                needed += _storage_wants(reading)
            needed += _provider_wants(registry, providers)
            ordinary = needed if reserve_entry is None else needed + (Amount(*RESERVE[:2], 0, RESERVE[2]),)
            moment = now() if callable(now) else now          # after every read: the snapshot is judged, and the decision taken, now
            given, narrowed = _inputs(snapshot, tuple({(w.kind, w.name): w for w in (*ordinary, *needed)}.values()),
                                      now=moment)
            amounts = (*given, *ledger)
            plan = _admit_classes(judged_on, used, moment, nodes, flagged, ordinary, needed, amounts,
                                  None if reserve_entry is None else reserve_entry.limit)
            rows = [(node, want.kind, want.name, want.amount, want.unit) for node in plan.admitted
                    for want in (needed if node in flagged else ordinary) if want.amount > 0]
            facts = [fact.as_dict() for fact in capacitysource.facts(snapshot, now=moment)]
            digest = capacityrecord.snapshot_digest(registry.digest, snapshot.taken_at, moment, facts)
            judged.update(registry=registry, reading=reading, plan=plan, snapshot=digest, narrowed=narrowed,
                          reserve=0 if reserve_entry is None else reserve_entry.limit)
            if record_dir is not None:
                judged["record"], _ = capacityrecord.write_record(record_dir, decision_id, {
                    "schema": capacityrecord.SCHEMA, "decision_id": decision_id, "decided_at": moment, "observed_at": observed,
                    "registry": registry.digest, "snapshot": digest, "policy": [list(row) for row in used.rows],
                    "width": width, "ready": ready,
                    "offered": [{"node": node, "critical": node in flagged} for node in nodes],
                    "wants": {"ordinary": _listed(ordinary), "critical": _listed(needed)},
                    "reserve": None if reserve_entry is None else {"kind": RESERVE[0], "name": RESERVE[1],
                                                                    "limit": reserve_entry.limit,
                                                                    "as_of": reserve_entry.as_of},
                    "holds": _listed(amounts), "narrowed": [n.as_dict() for n in narrowed],
                    "facts": [fact for fact in facts if fact["family"] in ("provider", "slots")],
                    "plan": plan.as_dict(),
                    "outcome": {"admitted": list(plan.admitted),
                                "waiting": [[p.node, p.verdict, p.reason] for p in plan.placements if p.verdict != "admit"]},
                    "held": [list(row) for row in rows]})
            return [(*row, links.get(row[0])) for row in rows]

        created, ended = CapacityReservations(runs_root, **_owner_facts(environment)).reserve_atomically(decide_now)
        plan = judged["plan"]
    except Exception as exc:                          # noqa: BLE001 - fail closed: nothing is admitted
        why = f"capacity cannot be judged ({type(exc).__name__}): {_shorten(str(exc), runs_root)[:220]}"
        return Gate(True, (), tuple((node, why) for node in nodes), why[:300])
    waits = tuple((p.node, _explain(p, judged["narrowed"], 0 if p.node in flagged else judged["reserve"]))
                  for p in plan.placements if p.verdict != "admit")
    admitted = plan.admitted
    digest = judged["registry"].digest                # the operator's record, not the registry plus the storage
    if admitted:
        why = f"capacity: {len(admitted)} of {len(nodes)} node(s) admitted on registry {digest[:12]}"
        if judged["reading"] is not None:
            why += f"; storage {_state(judged['reading'])}: {judged['reading'].run_bytes} bytes and " \
                   f"{judged['reading'].run_inodes} inodes held per node" \
                   + ("" if judged["reading"].inodes_measured else " (inodes: the RUN_INODES floor)")
    else:
        why = ("capacity: no node of the wave may start now: "
               + "; ".join(f"{node}: {reason}" for node, reason in waits))[:400]
    return Gate(True, admitted, waits, why, digest, plan.as_dict(),
                tuple(dict.fromkeys(row.run_id for row in created)), len(ended), judged["snapshot"],
                judged.get("record"))
