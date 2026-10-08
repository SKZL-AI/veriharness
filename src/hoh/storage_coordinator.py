"""The global storage coordinator (STORAGE-E, P3-40, implemented early).

The operator's directive (A1, section D) asks for two levels of storage
management on a machine that several VeriHarness projects -- and several
sessions of one project -- share:

* a **global storage coordinator** (this module) that observes host and global
  pressure through the admission ledger (`StorageAdmission.status()`, STORAGE-B2),
  owns no project files and coordinates capacity; and
* a **project storage steward** per project (STORAGE-D) that acts only on that
  project's registered namespace.

On pressure the coordinator **asks the project stewards for candidate plans**
(`ProjectSteward.candidate_plan`); it never builds such a list itself, never
scans, lists or sizes a directory, and never cleans a machine path. Every item
a steward proposes is validated with `StorageRegistry.owner_of` (STORAGE-A):
only `OWNED` by the steward's **own** project is accepted. One item that is not
(another project's, foreign, `UNKNOWN`, `PROTECTED` such as the registry
itself) makes the whole plan `untrusted` for the round, and an untrusted plan
counts zero bytes. Ownership is only ever `owner_of`'s answer -- there is no
path prefix or string comparison anywhere in this module.

**The coordinator performs no storage action.** It returns decisions
(`CoordinatorDecision.acted` is always False); the stewards act on their own
namespace (STORAGE-D, not here). The only thing this module writes is its own
per-project **driver lease**::

    <state_root>/coordinator/<project_id>/.lock            flock target (no content)
    <state_root>/coordinator/<project_id>/.vh-owner.json   marker, written first
    <state_root>/coordinator/<project_id>/DRIVER.json      the driver lease
    <state_root>/coordinator/<project_id>/DRIVER.json.v<N>.<UTC>   superseded, kept

Every read-modify-write of `DRIVER.json` happens under `fcntl.flock` on
`.lock`. A new `DRIVER.json` is published whole and never over an existing
one (`_create_exclusive`): an `O_EXCL` temporary file beside it, written
whole and fsynced, then ``renameat2(RENAME_NOREPLACE)`` onto the name --
no link fallback, so no second name is left; where ``renameat2`` is not
available the write fails (``OS_ERROR``), and a temporary file a failed
write leaves stays (nothing removes it, nothing reads it as a record). A
torn write therefore never leaves a malformed record. Later updates are
atomic (temp file, fsync, `os.replace`). A stale lease (another
boot, an absent pid, a reused pid with another start time, a holder whose
session lease the registry no longer has) is superseded by a versioned rename
under the same lock, never removed; a lease whose liveness cannot be judged
is **unknown** and refuses takeover (see `_driver_liveness` for the order of
the rules). A released lease (`release_driver`, `close_session`) is a
`DRIVER.json` marked `released` that keeps the sequence, so the next holder
continues at `sequence + 1`. `.vh-owner.json` is a
**marker**, not an ownership record: under the registry's rules only
`projects/<p>/<kind>/<s>/` records count, so `coordinator/<p>/` stays
`UNKNOWN` to `owner_of` -- which is safe, because no storage action can ever
take an `UNKNOWN` path. The `.lock` file is created to take the lock and
carries no content; the marker is the first record written into the
directory.

Sessions of one project share the project's registry and are serialised by
the registry's session leases (`acquire_session` delegates to
`StorageRegistry.open_session`, always for the coordinator's own pid from the
injected `getpid`) plus the coordinator's driver lease: one driving session
per project, and its sequence number lives only in that project's own
`DRIVER.json`. A second session whose driver lease is live elsewhere is
refused -- not queued silently, not allowed to take over.

Every refusal is a `StorageCoordinatorError` with an explicit `reason`
(STORAGE-D Revision 11 item 1): `LIVE_HOLDER` only where a live other driver
was found (also after an ``EEXIST`` race on `DRIVER.json`, decided by
re-reading the record), `MALFORMED` for an unreadable driver record,
`OS_ERROR` for an operating-system error (the lock, the directory, a write),
and the default `REFUSED` everywhere else -- never `LIVE_HOLDER` by default.
A caller may read a refusal as "busy" only for `LIVE_HOLDER`.

`share()` is **advisory**: it reports a per-project byte budget of the
admission headroom over the registry's registered projects (so every
coordinator instance over one state root reports the same shares);
`StorageAdmission.reserve` stays the authority and enforcement belongs to
P3-39's Capacity Broker.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import math
import os
import secrets
import stat
from collections import Counter
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, runtime_checkable

from hoh import archive_bundle as _ab
from hoh import storage_registry as _sr
from hoh.storage_pressure import PressureState, StorageAdmission
from hoh.storage_registry import (
    OWNED,
    PROTECTED,
    UNKNOWN,
    SessionLease,
    StorageOwnershipError,
    StorageRegistry,
)

DRIVER_SCHEMA = "veriharness.coordinator-driver/1"
MARKER_SCHEMA = "veriharness.coordinator-marker/1"

NONE_NEEDED = "NONE_NEEDED"
SUFFICIENT = "SUFFICIENT"
NEEDS_YOU = "NEEDS_YOU"
NEEDS_YOU_PREFIX = "NEEDS_YOU: owned optimisation cannot restore the reserve"

_COORDINATOR = "coordinator"
_LOCK = ".lock"
_MARKER = ".vh-owner.json"
_DRIVER = "DRIVER.json"

# The three answers of `_driver_liveness`.
LIVE = "LIVE"
DEAD = "DEAD"
LIVENESS_UNKNOWN = "UNKNOWN"


#: Refusal reasons (`StorageCoordinatorError.reason`, Revision 11 item 1).
#: `LIVE_HOLDER` is the registry's own constant: a proven live other holder.
LIVE_HOLDER = _sr.LIVE_HOLDER
REFUSED = "REFUSED"
MALFORMED = "MALFORMED"
OS_ERROR = "OS_ERROR"
#: `_create_exclusive` found the name taken (decided by `_advance_driver`).
EXISTS = "EXISTS"


class StorageCoordinatorError(RuntimeError):
    """A coordinator operation was refused (fail closed).

    `reason` is explicit, never read from the message: `LIVE_HOLDER` only at
    the sites that found a live other holder; the default `REFUSED` is never
    `LIVE_HOLDER`, so every other site is fail-safe without naming one."""

    def __init__(self, *args: object, reason: str = REFUSED) -> None:
        super().__init__(*args)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanItem:
    """One item a steward proposes to relieve: a path it claims its project
    owns, the bytes it claims relieving it frees, and its classification
    (e.g. `VERIFIED_DUPLICATE`, `REPRODUCIBLE_SCRATCH`). The coordinator never
    sizes the path; `bytes` is the steward's claim."""

    path: str | os.PathLike[str]
    bytes: int
    classification: str


@dataclass(frozen=True)
class CandidatePlan:
    """A steward's candidate plan for one round: every item is claimed to be
    owned by `project_id`. `items` may be any sequence of `PlanItem` that is
    not a string or a mapping (a tuple, a list); the coordinator freezes it
    to a tuple when it judges the plan, and an accepted plan holds that
    tuple with every item's classification kept (for STORAGE-D)."""

    project_id: str
    items: Sequence[PlanItem] = ()

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.items)


@dataclass(frozen=True)
class RejectedItem:
    """An item (or a whole steward) the coordinator rejected, and why.
    `ownership_status` and `owner_project_id` are `owner_of`'s answer (None
    when the steward failed before any item could be judged)."""

    project_id: str
    path: str
    reason: str
    ownership_status: str | None = None
    owner_project_id: str | None = None


@dataclass(frozen=True)
class CoordinatorDecision:
    """The answer of `on_pressure`. The coordinator never acts: `acted` is
    always False.

    `accepted` holds the trusted plans ordered by reclaimable bytes,
    descending; `untrusted` names the projects whose plan was rejected this
    round (they count zero bytes). `shortfall_bytes` is what the reserve is
    missing (`reserve + open reservations - effective free`, at least 0;
    None when the free space is unobservable). `verdict` is `NONE_NEEDED`
    (state better than OPTIMIZE, no steward asked), `SUFFICIENT` or
    `NEEDS_YOU`.
    """

    state: str
    accepted: tuple[CandidatePlan, ...]
    rejected: tuple[RejectedItem, ...]
    untrusted: tuple[str, ...]
    reclaimable_bytes: int
    shortfall_bytes: int | None
    restores_reserve: bool
    verdict: str
    message: str
    acted: bool = False


@runtime_checkable
class ProjectSteward(Protocol):
    """A project's storage steward (STORAGE-D) as the coordinator sees it.

    `candidate_plan(pressure)` receives a copy of the admission status and
    returns the items the steward proposes to relieve, all of which it claims
    are owned by `project_id`. The coordinator validates every item and never
    asks a steward to act.
    """

    project_id: str

    def candidate_plan(self, pressure: Mapping[str, Any]) -> CandidatePlan: ...


# --------------------------------------------------------------------------- #
# The coordinator
# --------------------------------------------------------------------------- #


def _check_project_id(project_id: object) -> str:
    """The registry's id rule (`storage_registry._ID`), before any path use."""
    try:
        return _sr._check_id(project_id, "project id")  # type: ignore[arg-type]
    except StorageOwnershipError as exc:
        raise StorageCoordinatorError(str(exc)) from exc


def _is_byte_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class StorageCoordinator:
    """Global coordinator over one registry (`StorageRegistry`) and one
    admission ledger (`StorageAdmission`), with one steward per project.

    The liveness readers of the driver lease (`pid_exists(pid) -> bool`,
    `pid_start_reader(pid) -> int | None`, field 22 of `/proc/<pid>/stat`, and
    `boot_id_reader() -> str`) are injectable; by default they are the
    registry's own readers, so session leases and driver leases are judged by
    the same readers. `getpid() -> int` (default `os.getpid`) is the only
    source of the pid a session lease is opened for: no method takes a pid.
    """

    def __init__(
        self,
        registry: StorageRegistry,
        admission: StorageAdmission,
        stewards: Iterable[ProjectSteward],
        *,
        weights: Mapping[str, float] | None = None,
        pid_exists: Callable[[int], bool] | None = None,
        pid_start_reader: Callable[[int], int | None] | None = None,
        boot_id_reader: Callable[[], str] | None = None,
        getpid: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.admission = admission
        self._stewards: dict[str, ProjectSteward] = {}
        for steward in stewards:
            project_id = _check_project_id(getattr(steward, "project_id", None))
            if not callable(getattr(steward, "candidate_plan", None)):
                raise StorageCoordinatorError(
                    f"steward of {project_id!r} has no candidate_plan")
            if project_id in self._stewards:
                raise StorageCoordinatorError(
                    f"two stewards for project {project_id!r}; refusing")
            self._require_registered(project_id, "a steward")
            self._stewards[project_id] = steward
        self._weights: dict[str, Fraction] = {}
        for project_id, weight in (weights or {}).items():
            _check_project_id(project_id)
            self._require_registered(project_id, "a weight")
            if (
                not isinstance(weight, (int, float)) or isinstance(weight, bool)
                or not math.isfinite(weight) or weight <= 0
            ):
                raise StorageCoordinatorError(
                    f"weight of {project_id!r} must be a positive finite number, got {weight!r}"
                )
            self._weights[project_id] = Fraction(weight)
        self._pid_exists = pid_exists or getattr(registry, "_pid_exists", _sr._pid_exists)
        self._pid_start_reader = pid_start_reader or getattr(
            registry, "_pid_start_reader", _sr._pid_start_time)
        self._boot_id_reader = boot_id_reader or getattr(
            registry, "_boot_id_reader", _sr._read_boot_id)
        self._getpid: Callable[[], int] = getpid or os.getpid
        self._sessions: dict[tuple[str, str], SessionLease] = {}

    def _require_registered(self, project_id: str, what: str) -> None:
        """A project the registry does not know gets no steward and no weight."""
        if self.registry._project_record(project_id) is None:
            raise StorageCoordinatorError(
                f"{what} for project {project_id!r}, which the registry does not know; refusing"
            )

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(self._stewards)

    # ------------------------------------------------------------------ observe

    def observe(self) -> dict[str, Any]:
        """The admission's `status()` plus `project_reservations`: the open
        reservations grouped by project (every steward's project present,
        an empty list if it holds none). Read only."""
        status = dict(self.admission.status())
        grouped: dict[str, list[dict[str, Any]]] = {p: [] for p in self._stewards}
        for reservation in status.get("open_reservations") or []:
            grouped.setdefault(str(reservation.get("project_id")), []).append(reservation)
        status["project_reservations"] = grouped
        return status

    # ------------------------------------------------------------------ pressure

    def on_pressure(self) -> CoordinatorDecision:
        """Ask the stewards for plans when the state is OPTIMIZE or worse,
        validate every item with `owner_of`, and decide. Never acts.

        Below OPTIMIZE no steward is asked (`NONE_NEEDED`). Otherwise every
        steward is asked once (a registered project without a steward is not
        asked). A steward that raises, returns no `CandidatePlan`, returns a
        plan for another project, or whose items are not a sequence (a
        string, a mapping, an iterator) is untrusted. An item is accepted only
        if `owner_of(item.path)` is `OWNED` **by the steward's own project**,
        its byte claim is a non-negative int, and its resolved path is
        neither equal to, inside, nor an ancestor of the resolved path of
        another item of the same plan; one rejected item makes the whole plan
        untrusted and it counts zero bytes this round.

        `restores_reserve` is whether the accepted plans' bytes cover the
        shortfall. With unobservable free space (`effective_free_bytes` None)
        the shortfall is unknown and the answer fails closed: not restored.
        Not restored means `NEEDS_YOU`, with a message that starts with
        `NEEDS_YOU: owned optimisation cannot restore the reserve` and says by
        how much.
        """
        status = self.admission.status()
        state = PressureState(status["state"])
        if state.severity < PressureState.OPTIMIZE.severity:
            return CoordinatorDecision(
                state=state.value, accepted=(), rejected=(), untrusted=(),
                reclaimable_bytes=0, shortfall_bytes=0, restores_reserve=True,
                verdict=NONE_NEEDED,
                message=f"state {state.value}: no optimisation needed, no steward asked",
            )

        accepted: list[CandidatePlan] = []
        rejected: list[RejectedItem] = []
        untrusted: list[str] = []
        for project_id, steward in self._stewards.items():
            plan, refusals = self._judge(project_id, steward, status)
            if refusals:
                rejected.extend(refusals)
                untrusted.append(project_id)
            elif plan is not None:
                accepted.append(plan)
        accepted.sort(key=lambda p: (-p.total_bytes, p.project_id))
        reclaimable = sum(p.total_bytes for p in accepted)

        effective = status.get("effective_free_bytes")
        shortfall: int | None
        if isinstance(effective, int) and not isinstance(effective, bool):
            shortfall = max(
                0, int(status["reserve_bytes"]) + int(status["reserved_bytes"]) - effective
            )
            restores = reclaimable >= shortfall
        else:
            shortfall = None
            restores = False

        if restores:
            verdict = SUFFICIENT
            message = (f"state {state.value}: owned plans of {reclaimable} B cover the "
                       f"shortfall of {shortfall} B")
        else:
            verdict = NEEDS_YOU
            if shortfall is None:
                message = (f"{NEEDS_YOU_PREFIX}: missing an unknown number of bytes "
                           f"(free space unobservable; reclaimable {reclaimable} B)")
            else:
                message = (f"{NEEDS_YOU_PREFIX}: missing {shortfall - reclaimable} bytes "
                           f"(shortfall {shortfall} B, reclaimable {reclaimable} B)")
        if untrusted:
            message += f"; untrusted plans: {', '.join(untrusted)}"
        return CoordinatorDecision(
            state=state.value, accepted=tuple(accepted), rejected=tuple(rejected),
            untrusted=tuple(untrusted), reclaimable_bytes=reclaimable,
            shortfall_bytes=shortfall, restores_reserve=restores, verdict=verdict,
            message=message,
        )

    def _judge(self, project_id: str, steward: ProjectSteward, status: Mapping[str, Any]
               ) -> tuple[CandidatePlan | None, list[RejectedItem]]:
        """One steward's plan, judged item by item (`owner_of` only). The
        accepted plan holds the items frozen to a tuple, classifications kept."""
        try:
            plan = steward.candidate_plan(copy.deepcopy(dict(status)))
        except Exception as exc:                # a failing steward proves nothing
            return None, [RejectedItem(project_id, "", f"the steward failed: {exc!r}")]
        if not isinstance(plan, CandidatePlan):
            return None, [RejectedItem(project_id, "", "the steward returned no CandidatePlan")]
        if plan.project_id != project_id:
            return None, [RejectedItem(
                project_id, "",
                f"the plan names project {plan.project_id!r}, not the steward's {project_id!r}",
            )]
        raw = plan.items
        if isinstance(raw, (str, bytes, bytearray, Mapping)) or not isinstance(raw, Sequence):
            return None, [RejectedItem(
                project_id, "", f"the plan's items are not a sequence of PlanItem: "
                                f"{type(raw).__name__}")]
        try:
            items = tuple(raw)
        except Exception as exc:                # an unreadable sequence proves nothing
            return None, [RejectedItem(project_id, "", f"the plan's items failed: {exc!r}")]

        refusals: list[RejectedItem] = []
        judged: list[tuple[str, _sr.Ownership, str]] = []
        for item in items:
            if not isinstance(item, PlanItem):
                refusals.append(RejectedItem(project_id, repr(item), "not a PlanItem"))
                continue
            shown = os.fspath(item.path) if isinstance(item.path, (str, os.PathLike)) \
                else repr(item.path)
            try:
                ownership = self.registry.owner_of(item.path)
            except Exception as exc:            # an unanswerable path is UNKNOWN
                refusals.append(RejectedItem(project_id, str(shown),
                                             f"UNKNOWN: owner_of failed: {exc!r}", UNKNOWN))
                continue
            judged.append((str(shown), ownership, self._refusal(project_id, item, ownership)))

        # Nesting is judged on the resolved paths of every judged item, so the
        # outcome does not depend on the order of the items.
        counts = Counter(o.resolved for _, o, _ in judged if o.resolved is not None)
        ancestors: set[Path] = set()
        for resolved in counts:
            ancestors.update(resolved.parents)
        for shown, ownership, refusal in judged:
            if not refusal and ownership.resolved is not None:
                refusal = _nesting_refusal(ownership.resolved, counts, ancestors)
            if refusal:
                refusals.append(RejectedItem(project_id, shown, refusal,
                                             ownership.status, ownership.project_id))
        if refusals:
            return None, refusals
        return CandidatePlan(project_id=project_id, items=items), []

    @staticmethod
    def _refusal(project_id: str, item: PlanItem, ownership: _sr.Ownership) -> str:
        """Why this item is not acceptable for `project_id` on its own, or ""
        if it is (nesting within the plan is judged separately)."""
        if ownership.status == PROTECTED:
            return f"PROTECTED (the registry's own layout): {ownership.reason}"
        if ownership.status != OWNED:
            return f"{ownership.status}: {ownership.reason or 'ownership not proven'}"
        if ownership.project_id != project_id:
            return (f"OWNED by project {ownership.project_id!r}, not by the steward's "
                    f"project {project_id!r}")
        if not _is_byte_count(item.bytes):
            return f"the byte claim {item.bytes!r} is not a non-negative int"
        if ownership.resolved is None:
            return "owner_of gave no resolved path"
        return ""

    # ------------------------------------------------------------------ share

    def share(self, project_id: str) -> int:
        """An **advisory** per-project byte budget: the admission headroom
        (`effective free - open reservations - reserve`, from `status()`),
        floored at 0 and split by the configured weights (default equal) over
        the registry's registered projects -- not over the stewards of this
        instance, so every coordinator over one state root and ledger reports
        the same shares, and a registered project without a steward gets a
        share too; rounded down.

        Reported, not enforced: `StorageAdmission.reserve` stays the
        authority, and enforcement of shares belongs to P3-39's Capacity
        Broker. An unknown free space gives 0. A project the registry does
        not know raises.
        """
        _check_project_id(project_id)
        projects = self.registry._registered_projects()
        if project_id not in projects:
            raise StorageCoordinatorError(
                f"project {project_id!r} is not registered; it has no share")
        headroom = self.admission.status().get("headroom_bytes")
        if not isinstance(headroom, int) or isinstance(headroom, bool) or headroom <= 0:
            return 0
        weights = {p: self._weights.get(p, Fraction(1)) for p in projects}
        return math.floor(Fraction(headroom) * weights[project_id] / sum(weights.values()))

    # ------------------------------------------------------------------ sessions

    def acquire_session(self, project_id: str, session_id: str,
                        boot_id: str | None = None) -> SessionLease:
        """The registry's session lease (`StorageRegistry.open_session`) for
        this coordinator's own pid (the injected `getpid`), kept in memory for
        `coordinate`. `boot_id` defaults to the boot-id reader. A live lease
        held by another process is refused by the registry
        (`StorageOwnershipError`), never taken over; a stale one is superseded
        by the registry's versioned rename."""
        _check_project_id(project_id)
        if boot_id is None:
            try:
                boot_id = self._boot_id_reader()
            except Exception as exc:
                raise StorageCoordinatorError(f"the boot id is unreadable: {exc!r}") from exc
        lease = self.registry.open_session(project_id, session_id, pid=self._getpid(),
                                           boot_id=boot_id)
        held = self._sessions.get((project_id, session_id))
        if lease.token is None and held is not None and held == lease:
            return held                         # the same live holder; keep its token
        self._sessions[(project_id, session_id)] = lease
        return lease

    def coordinate(self, project_id: str, session_id: str, fn: Callable[[], Any]
                   ) -> tuple[int, Any]:
        """Run `fn` as the project's driving session; returns `(sequence,
        fn())`.

        Refused (`StorageCoordinatorError`) unless this coordinator holds the
        session's live registry lease (from `acquire_session`) and the
        project's driver lease is free (released), dead or already this
        session's. A driver lease held live by another session, or one whose
        liveness is unknown, is refused at once -- not queued, not taken
        over, `DRIVER.json` untouched. A dead one is superseded by a versioned
        rename (kept) with its sequence carried forward. The sequence grows by
        exactly one per call and is persisted in `DRIVER.json` before the
        lock is released; `fn` runs after that, outside the lock, so another
        session is refused rather than blocked.
        """
        _check_project_id(project_id)
        lease = self._sessions.get((project_id, session_id))
        if lease is None:
            raise StorageCoordinatorError(
                f"session {session_id!r} of project {project_id!r} holds no lease from "
                f"this coordinator (acquire_session first)"
            )
        if not self.registry.lease_is_live(lease) \
                or self.registry._read_lease(project_id, session_id) != lease:
            raise StorageCoordinatorError(
                f"the lease of session {session_id!r} of project {project_id!r} is not live "
                f"or no longer the one on record"
            )
        directory = self._project_dir(project_id)
        with self._locked(directory, project_id):
            sequence = self._advance_driver(directory, project_id, lease)
        return sequence, fn()

    def release_driver(self, lease: SessionLease) -> Path:
        """Release the project's driver lease held by `lease`'s session;
        returns where the held record is kept.

        Refused (`StorageCoordinatorError`) unless `lease` is a session lease
        this coordinator holds (from `acquire_session`, with its token) and
        `DRIVER.json` names exactly this lease (session id, pid, pid start
        time, boot id) and is not already released. Under the lock the held
        record is kept by a versioned rename (`DRIVER.json.v<superseded+1>.<UTC>`)
        and a new `DRIVER.json` marked `released` is written atomically; it
        keeps `sequence` and `superseded`, so the next holder continues at
        `sequence + 1`. Nothing is removed.
        """
        project_id = self._require_held(lease)
        directory = self.registry.state_root / _COORDINATOR / project_id
        if not os.path.lexists(directory / _DRIVER):
            raise StorageCoordinatorError(
                f"project {project_id!r} has no driver lease to release")
        directory = self._project_dir(project_id)
        with self._locked(directory, project_id):
            path = directory / _DRIVER
            if not os.path.lexists(path):
                raise StorageCoordinatorError(
                    f"project {project_id!r} has no driver lease to release")
            current = _read_driver(path, project_id)
            if current is None:
                raise StorageCoordinatorError(
                    f"{path} is unreadable or malformed; refusing to release it")
            if current.get("released") is True:
                raise StorageCoordinatorError(
                    f"the driver lease of project {project_id!r} is already released")
            if not _names_lease(current, lease):
                raise StorageCoordinatorError(
                    f"the driver lease of project {project_id!r} is held by session "
                    f"{current['session_id']!r} (pid {current['pid']}), not by this lease; "
                    f"refusing"
                )
            kept = _sr._rename_versioned(path, str(current["superseded"] + 1))
            _write_atomic(path, {**current, "released": True, "released_utc": _sr._utc_iso()})
            return kept

    def close_session(self, lease: SessionLease) -> Path:
        """End a session this coordinator holds: release the driver lease if
        this session holds it (`release_driver`), then close the session's
        registry lease (`StorageRegistry.close_session`, which keeps it as
        `LEASE.json.v<N>.<UTC>`) and drop it from memory. Returns where the
        registry kept the lease. Refused for a lease this coordinator does
        not hold."""
        project_id = self._require_held(lease)
        path = self.registry.state_root / _COORDINATOR / project_id / _DRIVER
        if os.path.lexists(path):
            current = _read_driver(path, project_id)
            if current is not None and current.get("released") is not True \
                    and _names_lease(current, lease):
                self.release_driver(lease)      # re-checks the record under the lock
        try:
            kept = self.registry.close_session(project_id, lease.session_id, lease)
        except StorageOwnershipError as exc:
            raise StorageCoordinatorError(str(exc)) from exc
        finally:
            self._sessions.pop((project_id, lease.session_id), None)
        return kept

    def _require_held(self, lease: object) -> str:
        """The project id of `lease` if this coordinator holds exactly it
        (equal, and with the same token), else refused."""
        if not isinstance(lease, SessionLease):
            raise StorageCoordinatorError(f"not a SessionLease: {lease!r}")
        project_id = _check_project_id(lease.project_id)
        held = self._sessions.get((project_id, lease.session_id))
        if held is None or held != lease or not isinstance(lease.token, str) \
                or not lease.token or held.token != lease.token:
            raise StorageCoordinatorError(
                f"session {lease.session_id!r} of project {project_id!r}: this lease is not "
                f"one this coordinator holds; refusing"
            )
        return project_id

    # ------------------------------------------------------------------ driver lease

    def _project_dir(self, project_id: str) -> Path:
        """`<state_root>/coordinator/<project_id>/`, created as plain
        directories (a symlink or a non-directory is refused)."""
        try:
            self.registry._require_init()
        except StorageOwnershipError as exc:
            raise StorageCoordinatorError(str(exc)) from exc
        base = self.registry.state_root / _COORDINATOR
        directory = base / project_id
        for path in (base, directory):
            try:
                os.mkdir(path, 0o755)
            except FileExistsError:
                pass
            except OSError as exc:
                raise StorageCoordinatorError(f"cannot create {path}: {exc}",
                                              reason=OS_ERROR) from exc
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise StorageCoordinatorError(f"cannot read {path}: {exc}",
                                              reason=OS_ERROR) from exc
            if not stat.S_ISDIR(st.st_mode):
                raise StorageCoordinatorError(f"{path} is not a plain directory; refusing")
        return directory

    @contextmanager
    def _locked(self, directory: Path, project_id: str) -> Iterator[None]:
        """`flock` on `coordinator/<p>/.lock`; the marker is written first."""
        try:
            fd = os.open(directory / _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        except OSError as exc:
            raise StorageCoordinatorError(f"cannot open the lock in {directory}: {exc}",
                                          reason=OS_ERROR) from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise StorageCoordinatorError(f"cannot lock {directory / _LOCK}: {exc}",
                                              reason=OS_ERROR) from exc
            try:
                self._ensure_marker(directory, project_id)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_marker(self, directory: Path, project_id: str) -> None:
        """Write `.vh-owner.json` (a marker only; `owner_of` keeps answering
        UNKNOWN for this directory) before any other record. Caller holds the
        lock. A marker that names something else is refused, never adopted."""
        marker = directory / _MARKER
        if os.path.lexists(marker):
            record = _sr._read_record(marker)
            if not record or record.get("schema") != MARKER_SCHEMA \
                    or record.get("owner") != _sr.OWNER_NAME \
                    or record.get("project_id") != project_id \
                    or record.get("kind") != _COORDINATOR:
                raise StorageCoordinatorError(
                    f"{marker} is not this coordinator's marker for {project_id!r}; refusing"
                )
            return
        if os.path.lexists(directory / _DRIVER):
            raise StorageCoordinatorError(
                f"{directory / _DRIVER} exists without a marker; refusing to adopt it"
            )
        _write_atomic(marker, {
            "schema": MARKER_SCHEMA,
            "owner": _sr.OWNER_NAME,
            "project_id": project_id,
            "kind": _COORDINATOR,
            "created_utc": _sr._utc_iso(),
            "note": "a marker only: not an ownership record; owner_of answers UNKNOWN here",
        })

    def _driver_liveness(self, project_id: str, record: Mapping[str, Any]) -> tuple[str, str]:
        """`(LIVE | DEAD | UNKNOWN, reason)` for a held driver record. The
        first rule that applies decides:

        1. the recorded boot id is not the current one -> DEAD;
        2. the pid does not exist -> DEAD;
        3. the pid exists but its start time cannot be read (None, not an
           int, or the reader raises) -> UNKNOWN;
        4. the start time differs (a reused pid) -> DEAD;
        5. the start time is equal and the holder session's `LEASE.json` is
           on record and names the same pid, start time and boot id -> LIVE;
        6. the start time is equal and that `LEASE.json` is positively
           absent (`lstat` says it does not exist) -> DEAD: the registry has
           closed the holder's session;
        7. the start time is equal and `LEASE.json` names another holder ->
           UNKNOWN.

        A `LEASE.json` that exists but cannot be read, and any reader that
        raises, is UNKNOWN. UNKNOWN refuses takeover, as a malformed record
        does.
        """
        pid = record["pid"]
        try:
            boot = self._boot_id_reader()
        except Exception as exc:
            return LIVENESS_UNKNOWN, f"the boot id is unreadable ({exc!r})"
        if boot != record["boot_id"]:
            return DEAD, "recorded on another boot"
        try:
            exists = self._pid_exists(pid)
        except Exception as exc:
            return LIVENESS_UNKNOWN, f"whether pid {pid} exists is unreadable ({exc!r})"
        if not exists:
            return DEAD, f"pid {pid} does not exist"
        try:
            start = self._pid_start_reader(pid)
        except Exception as exc:
            return LIVENESS_UNKNOWN, f"the start time of pid {pid} is unreadable ({exc!r})"
        if not isinstance(start, int) or isinstance(start, bool):
            return LIVENESS_UNKNOWN, f"the start time of pid {pid} is unreadable ({start!r})"
        if start != record["pid_start"]:
            return DEAD, f"pid {pid} was reused (start time {start}, not {record['pid_start']})"
        session_id = record["session_id"]
        lease_path = self.registry._lease_path(project_id, session_id)
        try:
            os.lstat(lease_path)
        except FileNotFoundError:
            return DEAD, f"the registry holds no lease of session {session_id!r}"
        except OSError as exc:
            return LIVENESS_UNKNOWN, f"the lease of session {session_id!r} is unreadable ({exc})"
        on_record = self.registry._read_lease(project_id, session_id)
        if on_record is None:
            return LIVENESS_UNKNOWN, f"the lease of session {session_id!r} is unreadable"
        if (on_record.pid, on_record.pid_start, on_record.boot_id) == (
                pid, record["pid_start"], record["boot_id"]):
            return LIVE, f"pid {pid} holds session {session_id!r}"
        return LIVENESS_UNKNOWN, (f"the lease of session {session_id!r} names pid "
                                  f"{on_record.pid}, not the driver's pid {pid}")

    def _advance_driver(self, directory: Path, project_id: str, lease: SessionLease) -> int:
        """Take or keep the driver lease and hand out the next sequence.
        Caller holds the lock."""
        path = directory / _DRIVER
        holder = {"session_id": lease.session_id, "pid": lease.pid,
                  "pid_start": lease.pid_start, "boot_id": lease.boot_id}
        if not os.path.lexists(path):
            record = {"schema": DRIVER_SCHEMA, "project_id": project_id, **holder,
                      "since_utc": _sr._utc_iso(), "sequence": 1, "superseded": 0}
            self._publish_driver(path, project_id, record, lease)
            return 1
        current = _read_driver(path, project_id)
        if current is None:
            raise StorageCoordinatorError(
                f"{path} is unreadable or malformed; it cannot be proven dead, refusing",
                reason=MALFORMED,
            )
        if current.get("released") is True:
            # The release kept the held record as v<superseded+1>.
            version = current["superseded"] + 2
        else:
            liveness, reason = self._driver_liveness(project_id, current)
            if liveness == LIVE:
                if not _names_lease(current, lease):
                    raise StorageCoordinatorError(
                        f"project {project_id!r} is driven by live session "
                        f"{current['session_id']!r} (pid {current['pid']}); refusing session "
                        f"{lease.session_id!r}",
                        reason=LIVE_HOLDER,
                    )
                record = {**current, "sequence": current["sequence"] + 1}
                _write_atomic(path, record)
                return record["sequence"]
            if liveness != DEAD:
                raise StorageCoordinatorError(
                    f"the driver lease of project {project_id!r} (session "
                    f"{current['session_id']!r}, pid {current['pid']}) cannot be proven dead: "
                    f"{reason}; refusing session {lease.session_id!r}"
                )
            version = current["superseded"] + 1
        _sr._rename_versioned(path, str(version))
        record = {"schema": DRIVER_SCHEMA, "project_id": project_id, **holder,
                  "since_utc": _sr._utc_iso(), "sequence": current["sequence"] + 1,
                  "superseded": version}
        self._publish_driver(path, project_id, record, lease)
        return record["sequence"]

    def _publish_driver(self, path: Path, project_id: str, record: dict[str, Any],
                        lease: SessionLease) -> None:
        """`_create_exclusive`, with an ``EEXIST`` race decided here (Revision
        11 item 1): the record that took the name is re-read, and the refusal
        is `LIVE_HOLDER` only if that record names a live driver other than
        this lease; a released, dead, unknown, malformed or own record is a
        refusal of another reason. Caller holds the lock."""
        try:
            _create_exclusive(path, record)
        except StorageCoordinatorError as exc:
            if exc.reason != EXISTS:
                raise
            current = _read_driver(path, project_id)
            if current is not None and current.get("released") is not True \
                    and not _names_lease(current, lease):
                liveness, why = self._driver_liveness(project_id, current)
                if liveness == LIVE:
                    raise StorageCoordinatorError(
                        f"{path} appeared concurrently and names live session "
                        f"{current['session_id']!r} (pid {current['pid']}); refusing session "
                        f"{lease.session_id!r}",
                        reason=LIVE_HOLDER,
                    ) from exc
                state = f"{liveness}: {why}"
            else:
                state = ("unreadable or malformed" if current is None
                         else "released" if current.get("released") is True
                         else "naming this lease")
            raise StorageCoordinatorError(
                f"{path} appeared concurrently ({state}); refusing", reason=EXISTS) from exc


def _nesting_refusal(resolved: Path, counts: Mapping[Path, int], ancestors: set[Path]) -> str:
    """Why `resolved` overlaps another item of the same plan, or "". Only
    resolved paths are compared, never the given ones."""
    if counts.get(resolved, 0) > 1:
        return f"the resolved path {resolved} appears more than once in the plan"
    for parent in resolved.parents:
        if parent in counts:
            return f"the resolved path {resolved} lies inside another item of the plan ({parent})"
    if resolved in ancestors:
        return f"the resolved path {resolved} contains another item of the plan"
    return ""


def _names_lease(record: Mapping[str, Any], lease: SessionLease) -> bool:
    """Whether a driver record names exactly this session lease."""
    return (record.get("session_id"), record.get("pid"), record.get("pid_start"),
            record.get("boot_id")) == (lease.session_id, lease.pid, lease.pid_start,
                                       lease.boot_id)


# --------------------------------------------------------------------------- #
# Record helpers (the coordinator's own directory only)
# --------------------------------------------------------------------------- #


def _write_atomic(path: Path, record: dict[str, Any]) -> None:
    """Temp file, fsync, `os.replace`, fsync of the directory."""
    _sr._atomic_write_json(path, record)


def _write_whole(fd: int, data: bytes) -> None:
    """Write all of `data` to `fd`, looping over short writes (the test seam
    of a write that fails part-way)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "a write wrote nothing")
        view = view[written:]


def _create_exclusive(path: Path, record: dict[str, Any]) -> None:
    """Publish a new record under `path`, whole, never over an existing one
    (Revision 11 item 1): an ``O_EXCL|O_NOFOLLOW`` temporary file
    ``.<name>.tmp-<hex>`` beside it, written whole (`_write_whole`) and
    fsynced, then ``renameat2(RENAME_NOREPLACE)`` onto the name through
    `archive_bundle._renameat2` -- no link fallback, so the temporary name is
    never left as a second link -- then fsync of the directory.

    A name already taken is refused (reason `EXISTS`, "appeared
    concurrently"); ``renameat2`` unavailable or unsupported, and every other
    operating-system error, is `OS_ERROR`. A temporary file that could not be
    completed or published stays where it is: nothing removes it, and it is
    never read as a record (records are read by their name only)."""
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if os.path.lexists(path):
        raise StorageCoordinatorError(f"{path} appeared concurrently; refusing",
                                      reason=EXISTS)
    temp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o644)
    except OSError as exc:
        raise StorageCoordinatorError(f"cannot create {temp}: {exc}", reason=OS_ERROR) from exc
    try:
        try:
            _write_whole(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StorageCoordinatorError(
            f"cannot write {temp}: {exc}; the temporary file stays, {path.name} is not written",
            reason=OS_ERROR) from exc
    func = _ab._renameat2()
    if func is None:
        raise StorageCoordinatorError(
            f"renameat2 is not available: {path} cannot be published without replacing; "
            f"{temp.name} stays", reason=OS_ERROR)
    rc = func(_ab._AT_FDCWD, os.fsencode(temp), _ab._AT_FDCWD, os.fsencode(path),
              _ab._RENAME_NOREPLACE)
    if rc != 0:
        err = _ab.ctypes.get_errno()
        if err == errno.EEXIST:
            raise StorageCoordinatorError(f"{path} appeared concurrently; refusing "
                                          f"({temp.name} stays)", reason=EXISTS)
        raise StorageCoordinatorError(
            f"cannot publish {path}: {os.strerror(err)}; {temp.name} stays", reason=OS_ERROR)
    try:
        _sr._fsync_dir(path.parent)
    except OSError as exc:
        raise StorageCoordinatorError(f"cannot fsync {path.parent}: {exc}",
                                      reason=OS_ERROR) from exc


def _read_driver(path: Path, project_id: str) -> dict[str, Any] | None:
    """A well-formed driver record of `project_id` (held, or marked
    `released` with the holder fields of the lease it ended), or None."""
    record = _sr._read_record(path)
    if not record or record.get("schema") != DRIVER_SCHEMA \
            or record.get("project_id") != project_id:
        return None
    ints = ("pid", "pid_start", "sequence", "superseded")
    if not all(isinstance(record.get(k), int) and not isinstance(record.get(k), bool)
               for k in ints):
        return None
    if record["sequence"] < 0 or record["superseded"] < 0:
        return None
    if not isinstance(record.get("session_id"), str) or not isinstance(record.get("boot_id"), str):
        return None
    if not _sr._ID.match(record["session_id"]):  # it is used in a path (the holder's lease)
        return None
    if record.get("released", False) not in (True, False):
        return None
    return record


__all__ = [
    "EXISTS",
    "LIVE_HOLDER",
    "MALFORMED",
    "NEEDS_YOU",
    "NEEDS_YOU_PREFIX",
    "NONE_NEEDED",
    "OS_ERROR",
    "REFUSED",
    "SUFFICIENT",
    "CandidatePlan",
    "CoordinatorDecision",
    "PlanItem",
    "ProjectSteward",
    "RejectedItem",
    "StorageCoordinator",
    "StorageCoordinatorError",
]
