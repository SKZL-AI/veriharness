"""Storage pressure observation and reservation-based admission (STORAGE-B2, P2-39).

On 2026-09-25 VeriHarness wrote until the host volume under the WSL disk was
full. The guest-side headroom check measured a 512 MB floor, refused nothing
and could not see the host. The operator's policy (directive 2026-09-30,
sections B and C) replaces it with two rules this module implements:

1. **Admission by pessimistic reservation.** A write-heavy operation is
   admitted only when ``effective_free - open reservations - estimate`` still
   leaves ``policy.reserve`` (default 30 GiB). Current free space above some
   large threshold is *not* the test: a job that fits is admitted in NORMAL,
   OPTIMIZE and PRESSURE alike; only CRITICAL refuses everything.
2. **States with hysteresis.** NORMAL / OPTIMIZE / PRESSURE / CRITICAL; a
   worse state applies as soon as a boundary is crossed, a better one only
   once the boundary is cleared by ``policy.hysteresis`` (free inodes by
   ``policy.inode_hysteresis``). The last state is persisted, so a restarted
   process does not oscillate.

   **Inodes are a second axis next to bytes (P2-42).** On 2026-10-05 pytest
   trees of 15.5 M entries exhausted the 67 M inodes of the root filesystem
   while 100 GB were free; the one inode rule, an absolute floor of
   ``min_inodes_free`` that only the very last 100 000 inodes could trip, said
   NORMAL until then. When the filesystem reports its inode total
   (``f_files``), the share of free inodes is classified like free bytes
   (``inode_normal_ratio`` / ``inode_pressure_ratio`` / ``inode_critical_ratio``,
   cleared by ``inode_ratio_hysteresis``), and the state is the worse of the
   two axes. A filesystem that reports ``f_files == 0`` has no fixed inode
   table (btrfs, for one): there the inode axis is not measured at all -- a
   zero ``f_favail`` there is not an emergency. Without ``f_files`` (an
   observer that cannot say) only the absolute floor applies, as before.

   **This is a decision, not a measurement of every filesystem.** The pin
   answered CRITICAL for ``f_files == 0``; the reading "no fixed table" was
   checked here only with an injected ``statvfs`` (btrfs reports it so, ext4
   never does). An observer that reads 0 by mistake switches the inode axis
   off; the state file records ``total_inodes`` and ``status()`` reports
   ``inode_state: NOT_MEASURED`` rather than a quiet NORMAL, and the byte axis
   is untouched.

What this module deliberately does **not** do:

* It never reads, lists or sizes a directory to observe space. Observers call
  ``os.statvfs`` / ``os.stat`` / ``os.path.ismount`` on a path and nothing
  else -- they observe filesystems, not files. Foreign data may lower free
  space; VeriHarness only observes it, and the only consequence is a lower
  admission headroom.
* It never removes anything. Closed and stale reservation files are renamed
  with a versioned suffix (``.closed.v<N>.<UTC>``, ``.stale.v<N>.<UTC>``) and
  stay in the ledger. A malformed reservation file is never renamed or
  removed by the module: it refuses admission until an operator renames it
  (versioned).
* It never reads ``/proc/1``. The only ``/proc`` reads are the process's own
  pid-namespace link, ``/proc/<pid>/stat`` (start time) and the boot id.
* It does not park or resume runs itself: ``hoh.storage_park`` (STORAGE-B3,
  P2-40) acts on a refusal, and host adapters beyond the ``HostObserver``
  protocol and ``StatvfsHostObserver`` are STORAGE-F.

Who claims a boot (STORAGE-B3, Revisions 8 and 9)
-------------------------------------------------
The ledger records which pid namespace a boot belongs to, and ``reserve()`` /
``would_admit()`` refuse a caller from any other namespace ("reserve outside
the sandbox"). They never record it themselves: a new boot is claimed only by
an explicit ``claim_boot()``, and until then both refuse ("boot not
claimed"). ``claim_boot()`` is idempotent for the namespace that holds the
claim and refused for any other; it refuses, recording nothing, when the
caller's own namespace link cannot be read, and ledger creation refuses for
the same reason -- ``null`` is never recorded. Creation records the creator's
namespace in the record's ``history`` and leaves the boot unclaimed.

Only the host entry points call ``claim_boot()``, each before its first
``reserve()`` or ``would_admit()`` of a process: ``hoh run`` (the
controller), ``hoh resume-storage`` (the timer unit, which after a reboot may
reach the ledger first) and the launcher's spawn path. On a new boot there is
no reference namespace, so ``claim_boot()`` cannot itself decide that its
caller is not sandboxed. What holds that property is ordering and the mount
table: every role of the previous boot is dead, the claim precedes any role
start of this boot, and the strict sandbox (``hoh.sandbox``) binds only
system directories, the read-only candidate and its scratch -- a sandboxed
check cannot reach the state root's ledger at all. Developer roles in Herdr
panes share the controller's namespace. Several controllers and projects
share one ledger; the first host entry point of a boot claims it, the others
find their own namespace already recorded.

The ledger
----------
One ledger per filesystem, shared by every process on the machine:
``<state_root>/ledger/<f_fsid of the guest path>/`` (see
``StorageAdmission.for_state_root``). Every admission decision -- read the
open reservations, load the tracker state, compute, write the new
reservation -- happens under one exclusive ``flock`` on the ledger directory.
Layout::

    namespace.json                            pid-namespace record and boot claim
    reservations/<id>.json                    one per open reservation
    reservations/<id>.json.closed.v1.<UTC>    released (renamed, kept)
    reservations/<id>.json.stale.v1.<UTC>     owner dead (renamed, kept)
    history.jsonl                             one line per release
    host_last.json                            last successful host reading
    state.json                                the tracker's last state
    observations.json                         the last observations (slope)
"""

from __future__ import annotations

import enum
import fcntl
import json
import math
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .durable_io import durable_write

GiB = 1024 ** 3

#: The operation kinds the directive names. An estimate is kept per kind.
OP_KINDS = (
    "role_run",
    "reviewer_pair",
    "mutation_review",
    "durability_evidence",
    "clone",
    "archive_staging",
    "rehydration",
    "regeneration",
    "tmp_workspace",
)

#: Conservative per-kind defaults. Reviewer scratch was the largest consumer on
#: 2026-09-25 (52 of ~74 GiB), hence the larger figures for review kinds.
DEFAULT_ESTIMATES: dict[str, int] = {
    "role_run": 2 * GiB,
    "reviewer_pair": 4 * GiB,
    "mutation_review": 4 * GiB,
    "durability_evidence": 2 * GiB,
    "clone": 2 * GiB,
    "archive_staging": 10 * GiB,
    "rehydration": 10 * GiB,
    "regeneration": 5 * GiB,
    "tmp_workspace": 1 * GiB,
}

#: How many observations are kept for the naive ``seconds_to_reserve`` slope.
_OBSERVATIONS_KEPT = 32
_SLOPE_POINTS = 8

_NAMESPACE_RECORD = "namespace.json"


class StoragePolicyError(ValueError):
    """A policy (or a per-machine override file) was refused."""


class StorageLedgerError(RuntimeError):
    """The ledger was asked for something it cannot do (e.g. double release)."""


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Fixed-width UTC ISO form with microseconds, so strings sort like times."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _compact(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class StoragePolicy(BaseModel):
    """Thresholds of the storage lifecycle, all byte values in bytes.

    Defaults (GiB): ``normal_at=80``, ``pressure_below=50``, ``reserve=30``,
    ``critical_below=15``, ``hysteresis=5``; ``min_inodes_free=100_000`` with
    ``inode_hysteresis=10_000``; and, as shares of the filesystem's inodes
    that are free, ``inode_normal_ratio=0.20``, ``inode_pressure_ratio=0.10``,
    ``inode_critical_ratio=0.03`` with ``inode_ratio_hysteresis=0.02``. The ordering
    ``critical_below < reserve < pressure_below < normal_at`` (and
    ``inode_critical_ratio < inode_pressure_ratio < inode_normal_ratio``) is enforced;
    unknown keys are refused. ``defaults`` holds the per-kind default
    estimates; an override may name some kinds, the rest keep their default,
    and an unknown kind is refused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    normal_at: StrictInt = Field(80 * GiB, gt=0)
    pressure_below: StrictInt = Field(50 * GiB, gt=0)
    reserve: StrictInt = Field(30 * GiB, gt=0)
    critical_below: StrictInt = Field(15 * GiB, ge=0)
    hysteresis: StrictInt = Field(5 * GiB, ge=0)
    min_inodes_free: StrictInt = Field(100_000, ge=0)
    inode_hysteresis: StrictInt = Field(10_000, ge=0)
    inode_normal_ratio: float = Field(0.20, gt=0, le=1)
    inode_pressure_ratio: float = Field(0.10, gt=0, le=1)
    inode_critical_ratio: float = Field(0.03, ge=0, le=1)
    inode_ratio_hysteresis: float = Field(0.02, ge=0, le=1)
    safety_factor: float = Field(1.5, ge=1.0)
    history_window: StrictInt = Field(20, ge=1)
    host_max_age_s: float = Field(900.0, gt=0)
    host_paths: tuple[str, ...] = ()
    defaults: dict[str, StrictInt] = Field(default_factory=lambda: dict(DEFAULT_ESTIMATES))

    @field_validator("defaults", mode="before")
    @classmethod
    def _merge_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        unknown = sorted(set(value) - set(OP_KINDS))
        if unknown:
            raise ValueError(
                f"unknown operation kind(s) in defaults: {unknown}; known: {list(OP_KINDS)}"
            )
        return {**DEFAULT_ESTIMATES, **value}

    @field_validator("defaults")
    @classmethod
    def _non_negative_defaults(cls, value: dict[str, int]) -> dict[str, int]:
        negative = sorted(k for k, v in value.items() if v < 0)
        if negative:
            raise ValueError(f"negative default estimate(s) for: {negative}")
        return value

    @model_validator(mode="after")
    def _ordering(self) -> "StoragePolicy":
        if not (self.critical_below < self.reserve < self.pressure_below < self.normal_at):
            raise ValueError(
                "inconsistent ordering, required critical_below < reserve < "
                f"pressure_below < normal_at; got critical_below={self.critical_below}, "
                f"reserve={self.reserve}, pressure_below={self.pressure_below}, "
                f"normal_at={self.normal_at}"
            )
        if not (self.inode_critical_ratio < self.inode_pressure_ratio < self.inode_normal_ratio):
            raise ValueError(
                "inconsistent inode ordering, required inode_critical_ratio < "
                "inode_pressure_ratio < inode_normal_ratio; got "
                f"{self.inode_critical_ratio}, {self.inode_pressure_ratio}, "
                f"{self.inode_normal_ratio}"
            )
        return self

    @classmethod
    def load(cls, path: Path | str) -> "StoragePolicy":
        """Read a per-machine JSON file of overrides; refusal raises."""
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoragePolicyError(f"storage policy {source} cannot be read: {exc}") from exc
        if not isinstance(data, dict):
            raise StoragePolicyError(f"storage policy {source} must be a JSON object")
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise StoragePolicyError(f"storage policy {source} refused: {exc}") from exc


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Observation:
    """One reading of free space. Bytes and inodes, never file contents.

    ``host_unknown`` is True when a host path is configured but could not be
    observed; ``effective_free_bytes`` is then the guest figure only, and the
    state tracker caps the state at PRESSURE. During admission the ledger may
    fill ``host_free_bytes`` with the term derived from the last persisted host
    reading (``host_unknown`` stays True and ``host_note`` says so).
    """

    guest_free_bytes: int
    guest_free_inodes: int
    host_free_bytes: int | None = None
    host_note: str = ""
    observed_utc: str = field(default_factory=lambda: _iso(_utcnow()))
    host_unknown: bool = False
    #: ``f_files``: the filesystem's inode total. None: not reported (only the
    #: absolute floor applies). 0: no fixed inode table (the inode axis is off).
    guest_total_inodes: int | None = None

    @property
    def effective_free_bytes(self) -> int:
        if self.host_free_bytes is None:
            return self.guest_free_bytes
        return min(self.guest_free_bytes, self.host_free_bytes)


class HostObserver(Protocol):
    """Free bytes of the host volume under the guest, or None plus a note.

    ``configured`` says whether a host is supposed to be observable at all;
    an observer without the attribute is treated as configured, so a ``None``
    from it reads as *unknown*, never as *no host*.
    """

    configured: bool

    def observe(self) -> tuple[int | None, str]: ...


class NoHost:
    """The default: no host is configured, the guest figure stands alone."""

    configured = False

    def observe(self) -> tuple[int | None, str]:
        return None, "no host configured"


class StatvfsHostObserver:
    """The minimum free space over configured host-side mount points.

    A configured path counts as observed only if it is a mount point
    (``ismount``) **and** its filesystem differs from the guest's: both
    ``f_fsid`` and ``st_dev`` differ from those of ``guest_path``. An
    unmounted host drive under WSL is a plain guest directory; reading its
    ``statvfs`` would report the guest's own free space as the host's.

    Any configured path that cannot be observed makes the whole reading
    unknown (``None``): an unobservable host never reads as plenty. Without a
    ``guest_path`` the filesystem comparison cannot be made, so every path is
    unobservable; ``GuestObserver`` binds its own path when it is given an
    observer without one. ``ismount``, ``stat`` and ``statvfs`` are
    injectable (they are applied to the host paths and to the guest path).
    """

    def __init__(
        self,
        paths: Sequence[Path | str],
        *,
        guest_path: Path | str | None = None,
        statvfs: Callable[[Any], Any] = os.statvfs,
        stat: Callable[[Any], Any] = os.stat,
        ismount: Callable[[Any], bool] = os.path.ismount,
    ) -> None:
        self.paths = tuple(Path(p) for p in paths)
        self.configured = bool(self.paths)
        self.guest_path = Path(guest_path) if guest_path is not None else None
        self._statvfs = statvfs
        self._stat = stat
        self._ismount = ismount

    def observe(self) -> tuple[int | None, str]:
        if not self.paths:
            return None, "no host paths configured"
        if self.guest_path is None:
            return None, "host paths not observable: guest filesystem unknown"
        try:
            guest_fsid = self._statvfs(self.guest_path).f_fsid
            guest_dev = self._stat(self.guest_path).st_dev
        except OSError as exc:
            return None, f"host paths not observable: guest {self.guest_path}: {exc}"
        values: list[int] = []
        for path in self.paths:
            try:
                if not self._ismount(path):
                    return None, f"host path {path} not observable: not a mount point"
                st = self._statvfs(path)
                dev = self._stat(path).st_dev
            except OSError as exc:
                return None, f"host path {path} not observable: {exc}"
            if st.f_fsid == guest_fsid or dev == guest_dev:
                return None, (
                    f"host path {path} not observable: on the guest's filesystem "
                    f"(fsid {st.f_fsid} vs {guest_fsid}, st_dev {dev} vs {guest_dev})"
                )
            values.append(int(st.f_bavail) * int(st.f_frsize))
        low = min(range(len(values)), key=values.__getitem__)
        return values[low], f"host minimum at {self.paths[low]}"


def _total_inodes(st: Any) -> int | None:
    """``f_files`` of a ``statvfs`` answer as an int, or None when it is not
    reported (or not an int): an unreadable total switches the ratio axis off,
    it never invents one."""
    total = getattr(st, "f_files", None)
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    return None


class GuestObserver:
    """Observes the guest filesystem holding ``path``, plus the host.

    Only ``os.statvfs`` (``f_bavail * f_frsize``, ``f_favail``, ``f_files``) is used: no
    directory is read, listed or sized.
    """

    def __init__(
        self,
        path: Path | str,
        host: HostObserver | None = None,
        *,
        statvfs: Callable[[Any], Any] = os.statvfs,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = Path(path)
        self.host: HostObserver = host if host is not None else NoHost()
        if isinstance(self.host, StatvfsHostObserver) and self.host.guest_path is None:
            self.host.guest_path = self.path
        self._statvfs = statvfs
        self._clock = clock

    @classmethod
    def from_policy(cls, path: Path | str, policy: StoragePolicy, **kw: Any) -> "GuestObserver":
        host: HostObserver = (
            StatvfsHostObserver(policy.host_paths, guest_path=path)
            if policy.host_paths
            else NoHost()
        )
        return cls(path, host, **kw)

    def observe(self) -> Observation:
        st = self._statvfs(self.path)
        host_free, note = self.host.observe()
        configured = getattr(self.host, "configured", True)
        return Observation(
            guest_free_bytes=int(st.f_bavail) * int(st.f_frsize),
            guest_free_inodes=int(st.f_favail),
            guest_total_inodes=_total_inodes(st),
            host_free_bytes=host_free,
            host_note=note,
            observed_utc=_iso(self._clock()),
            host_unknown=bool(configured and host_free is None),
        )


# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #


class PressureState(str, enum.Enum):
    NORMAL = "NORMAL"
    OPTIMIZE = "OPTIMIZE"
    PRESSURE = "PRESSURE"
    CRITICAL = "CRITICAL"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {
    PressureState.NORMAL: 0,
    PressureState.OPTIMIZE: 1,
    PressureState.PRESSURE: 2,
    PressureState.CRITICAL: 3,
}


def _atomic_write(path: Path, payload: str) -> None:
    """Write, fsync, rename over the target, fsync the directory.

    Through `durable_io.durable_write` (P2-41b W9): on ENOSPC or EIO the file
    keeps its bytes, the error is raised and no temp file is left. `mode=None`:
    the umask decides, as it did before.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_write(path, payload, mode=None)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class StateTracker:
    """NORMAL / OPTIMIZE / PRESSURE / CRITICAL with hysteresis.

    Raw state from effective free space: ``>= normal_at`` NORMAL, below
    ``critical_below`` CRITICAL, below ``pressure_below`` PRESSURE, otherwise
    OPTIMIZE; fewer than ``min_inodes_free`` free inodes is CRITICAL. A worse
    state applies at once; a better one only when the boundary is cleared by
    ``hysteresis`` (49 GiB PRESSURE, 52 GiB still PRESSURE, 56 GiB OPTIMIZE).
    Leaving CRITICAL also requires ``min_inodes_free + inode_hysteresis`` free
    inodes. An unknown host caps the state at PRESSURE. With ``state_file``
    the last state is persisted atomically and read back by ``load()`` (at
    construction unless ``load=False``; ``StorageAdmission`` loads it under its
    ledger lock instead).
    """

    def __init__(
        self,
        policy: StoragePolicy,
        state_file: Path | str | None = None,
        *,
        load: bool = True,
    ) -> None:
        self.policy = policy
        self.state_file = Path(state_file) if state_file is not None else None
        self.last: PressureState | None = None
        if load:
            self.load()

    def load(self) -> PressureState | None:
        """Read the persisted state (None when there is none)."""
        if self.state_file is None:
            return self.last
        data = _read_json(self.state_file)
        if isinstance(data, dict) and data.get("state") in PressureState.__members__:
            self.last = PressureState(data["state"])
        else:
            self.last = None
        return self.last

    def inode_axis(
        self, free_inodes: int, total_inodes: int | None = None, min_inodes: int | None = None
    ) -> PressureState:
        """The state of the inode axis alone. ``total_inodes`` 0 (no fixed
        inode table) is NORMAL; None (not reported) judges the absolute floor
        only; otherwise the floor and the share of free inodes decide."""
        p = self.policy
        floor = p.min_inodes_free if min_inodes is None else min_inodes
        if total_inodes == 0:
            return PressureState.NORMAL
        if free_inodes < floor:
            return PressureState.CRITICAL
        if total_inodes is None:
            return PressureState.NORMAL
        share = free_inodes / total_inodes
        if share < p.inode_critical_ratio:
            return PressureState.CRITICAL
        if share < p.inode_pressure_ratio:
            return PressureState.PRESSURE
        if share < p.inode_normal_ratio:
            return PressureState.OPTIMIZE
        return PressureState.NORMAL

    def classify(
        self,
        free_bytes: int,
        free_inodes: int,
        min_inodes: int | None = None,
        total_inodes: int | None = None,
    ) -> PressureState:
        p = self.policy
        by_inodes = self.inode_axis(free_inodes, total_inodes, min_inodes)
        if free_bytes < p.critical_below:
            by_bytes = PressureState.CRITICAL
        elif free_bytes < p.pressure_below:
            by_bytes = PressureState.PRESSURE
        elif free_bytes < p.normal_at:
            by_bytes = PressureState.OPTIMIZE
        else:
            by_bytes = PressureState.NORMAL
        return by_bytes if by_bytes.severity >= by_inodes.severity else by_inodes

    def update(self, obs: Observation) -> PressureState:
        free = obs.effective_free_bytes
        inodes = obs.guest_free_inodes
        total = obs.guest_total_inodes
        raw = self.classify(free, inodes, None, total)
        prev = self.last
        if prev is None or raw.severity >= prev.severity:
            state = raw
        else:
            floor = self.policy.min_inodes_free
            if prev is PressureState.CRITICAL:
                floor += self.policy.inode_hysteresis
            slack = int(total * self.policy.inode_ratio_hysteresis) if total else 0
            cleared = self.classify(free - self.policy.hysteresis, inodes - slack, floor, total)
            state = cleared if cleared.severity < prev.severity else prev
        if obs.host_unknown and state.severity < PressureState.PRESSURE.severity:
            state = PressureState.PRESSURE
        self.last = state
        if self.state_file is not None:
            _atomic_write(
                self.state_file,
                json.dumps(
                    {
                        "state": state.value,
                        "effective_free_bytes": free,
                        "free_inodes": inodes,
                        "total_inodes": total,
                        "host_unknown": obs.host_unknown,
                        "observed_utc": obs.observed_utc,
                    },
                    indent=2,
                ),
            )
        return state


# --------------------------------------------------------------------------- #
# Liveness of a reservation's owner
# --------------------------------------------------------------------------- #


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_start_time(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat`` (start time in clock ticks after boot)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name (field 2) may contain spaces and parentheses; the
    # remaining fields start after the last ')', with field 3 at index 0.
    rest = raw[raw.rfind(")") + 2:].split()
    try:
        return int(rest[22 - 3])
    except (IndexError, ValueError):
        return None


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot"


def pid_namespace() -> str | None:
    """The calling process's own pid-namespace link (``/proc/self/ns/pid``),
    e.g. ``pid:[4026532229]``; None when it cannot be read. Never ``/proc/1``."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def ledger_key(path: Path | str) -> str:
    """The ledger directory name for the filesystem holding ``path``: its
    ``f_fsid`` (stable across boots). A filesystem reporting fsid 0 falls back
    to its device number, so zero-fsid filesystems do not share a ledger."""
    fsid = os.statvfs(path).f_fsid
    if fsid:
        return f"fsid-{fsid:016x}"
    return f"dev-{os.stat(path).st_dev:x}"


@contextmanager
def _flock(directory: Path) -> Iterator[None]:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reservation:
    id: str
    project_id: str
    op_kind: str
    estimate: int
    pid: int
    pid_start_time: int | None
    boot_id: str
    opened_utc: str
    path: Path
    pid_ns: str | None = None

    admitted = True

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "op_kind": self.op_kind,
            "estimate": self.estimate,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "pid_ns": self.pid_ns,
            "boot_id": self.boot_id,
            "opened_utc": self.opened_utc,
        }

    @classmethod
    def from_file(cls, path: Path) -> "Reservation | None":
        """The reservation in ``path``, or None when the file cannot be read
        or does not hold a well-formed reservation (malformed)."""
        data = _read_json(path)
        if not isinstance(data, dict):
            return None

        def is_int(v: Any) -> bool:
            return isinstance(v, int) and not isinstance(v, bool)

        try:
            ok = (
                isinstance(data["id"], str)
                and isinstance(data["project_id"], str)
                and data["op_kind"] in OP_KINDS
                and is_int(data["estimate"]) and data["estimate"] >= 0
                and is_int(data["pid"]) and data["pid"] > 0
                and (data.get("pid_start_time") is None or is_int(data["pid_start_time"]))
                and (data.get("pid_ns") is None or isinstance(data["pid_ns"], str))
                and isinstance(data["boot_id"], str)
                and isinstance(data["opened_utc"], str)
            )
            if not ok:
                return None
            _parse(data["opened_utc"])
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            id=data["id"],
            project_id=data["project_id"],
            op_kind=data["op_kind"],
            estimate=data["estimate"],
            pid=data["pid"],
            pid_start_time=data.get("pid_start_time"),
            boot_id=data["boot_id"],
            opened_utc=data["opened_utc"],
            path=path,
            pid_ns=data.get("pid_ns"),
        )


@dataclass(frozen=True)
class Refusal:
    """Why an operation was not admitted, with the numbers."""

    project_id: str
    op_kind: str
    reason: str
    state: PressureState
    effective_free: int | None
    reserved: int
    estimate: int
    reserve: int
    host_note: str = ""

    admitted = False

    @property
    def headroom_after(self) -> int | None:
        if self.effective_free is None:
            return None
        return self.effective_free - self.reserved - self.estimate - self.reserve

    @property
    def message(self) -> str:
        free = "unknown" if self.effective_free is None else f"{self.effective_free} B"
        return (
            f"refused {self.op_kind} for {self.project_id}: {self.reason}; "
            f"state={self.state.value} effective_free={free} reserved={self.reserved} B "
            f"estimate={self.estimate} B reserve={self.reserve} B "
            f"headroom_after={self.headroom_after}"
            + (f" ({self.host_note})" if self.host_note else "")
        )

    def __str__(self) -> str:
        return self.message


@dataclass
class _Scan:
    """The reservation files of the ledger, judged. ``counted`` includes the
    ``foreign`` ones (another pid namespace: cannot be judged, fail safe)."""

    counted: list[Reservation] = field(default_factory=list)
    foreign: list[Reservation] = field(default_factory=list)
    stale: list[Reservation] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


class StorageAdmission:
    """The reservation ledger: admit a write-heavy operation only if its
    pessimistic estimate leaves ``policy.reserve`` free.

    The canonical constructor is ``for_state_root``: every VeriHarness process
    on a machine uses ``<state_root>/ledger/`` and therefore the same ledger
    for the same filesystem. **Constructing a second ``ledger_root`` on the
    same filesystem is a convention violation**: two ledgers cannot see each
    other's reservations, so both could take the last space. Nothing here
    enforces that machine-wide yet; that is tracked for P3-40 (global storage
    coordinator).

    Admission: refused in CRITICAL; otherwise admitted iff
    ``effective_free - sum(open reservations' estimates) - estimate >=
    policy.reserve``, where the reserved ``estimate`` is
    ``max(estimate_bytes, estimate(op_kind))`` -- a caller cannot undercut the
    history. "Remaining" is the full estimate until release. The whole
    read-compute-write sequence (including loading and writing the tracker
    state) runs under one exclusive ``flock`` on the ledger directory;
    ``_before_commit`` runs after the open reservations were read and before
    the new one is written (a test seam for the race).

    Also refused, fail closed: while any reservation file is malformed (the
    file is named; only an operator's versioned rename clears it); when the
    ledger's namespace record is missing; when the caller's own pid namespace
    cannot be read; while the current boot is not claimed ("boot not
    claimed" -- only ``claim_boot()`` claims, ``reserve()`` never records a
    namespace); when the caller's pid namespace differs from the one that
    claimed the current boot ("reserve outside the sandbox").

    ``would_admit()`` is the same decision under the same lock, writing no
    reservation and no history (it may persist the observation and the
    tracker state, which are observations).

    Unobservable host: the host term is the last persisted successful host
    reading if it is younger than ``policy.host_max_age_s``, minus the
    estimates of reservations opened at or after it (open, closed or stale)
    and minus the recorded actuals of reservations released at or after it.
    Older or absent: refused. The admission formula is otherwise unchanged,
    so open reservations are still subtracted from the result as well --
    deliberately conservative.

    Liveness of a reservation's owner, judged in this order: a previous boot
    is stale; a reservation recorded in another pid namespace (or none) cannot
    be judged and is counted (``foreign_namespace`` in ``status()``); a dead
    pid, or a pid whose start time differs from the recorded one (reused), is
    stale. A start time that cannot be read -- when recording or when judging
    -- never makes a reservation stale. Stale files are renamed with a
    ``.stale.v<N>.<UTC>`` suffix, never removed. All probes are injectable.
    """

    def __init__(
        self,
        policy: StoragePolicy,
        observer: Any,
        ledger_root: Path | str,
        *,
        pid_alive: Callable[[int], bool] = pid_alive,
        proc_start_time: Callable[[int], int | None] = proc_start_time,
        boot_id: Callable[[], str] = boot_id,
        pid_namespace: Callable[[], str | None] = pid_namespace,
        getpid: Callable[[], int] = os.getpid,
        clock: Callable[[], datetime] = _utcnow,
        _before_commit: Callable[[], None] = lambda: None,
    ) -> None:
        self.policy = policy
        self.observer = observer
        self.ledger_root = Path(ledger_root)
        self.ledger = self.ledger_root / ledger_key(observer.path)
        self.reservations_dir = self.ledger / "reservations"
        self.history_path = self.ledger / "history.jsonl"
        self.host_path = self.ledger / "host_last.json"
        self.observations_path = self.ledger / "observations.json"
        self.namespace_path = self.ledger / _NAMESPACE_RECORD
        self.tracker = StateTracker(policy, self.ledger / "state.json", load=False)
        self._pid_alive = pid_alive
        self._proc_start_time = proc_start_time
        self._boot_id = boot_id
        self._pid_namespace = pid_namespace
        self._getpid = getpid
        self._clock = clock
        self._before_commit = _before_commit
        self._create_ledger()

    @classmethod
    def for_state_root(
        cls,
        state_root: Path | str,
        policy: StoragePolicy,
        observer: Any = None,
        **kw: Any,
    ) -> "StorageAdmission":
        """The canonical ledger: ``<state_root>/ledger/<f_fsid>/``.

        Without an observer, the guest is the state root's filesystem and the
        host paths come from ``policy.host_paths``.
        """
        root = Path(state_root)
        root.mkdir(parents=True, exist_ok=True)
        if observer is None:
            observer = GuestObserver.from_policy(root, policy)
        return cls(policy, observer, root / "ledger", **kw)

    # -- creation and locking ------------------------------------------------ #

    def _create_ledger(self) -> None:
        """Create the ledger with an exclusive ``mkdir`` and write its
        namespace record under the ledger lock. Creation is serialised by a
        lock on ``ledger_root``, which every constructor takes, so no caller
        sees the new directory before its record exists. An existing ledger
        is never given a record here (a missing one refuses admission).

        Creation records the creator's namespace in the record's history and
        leaves the boot unclaimed (only ``claim_boot()`` claims). It is
        refused -- ``StorageLedgerError``, nothing created -- when the
        creator's own namespace link cannot be read: ``null`` is never
        recorded."""
        self.ledger_root.mkdir(parents=True, exist_ok=True)
        with _flock(self.ledger_root):
            creator_ns: str | None = None
            if not os.path.lexists(self.ledger):
                creator_ns = self._pid_namespace()
                if creator_ns is None:
                    raise StorageLedgerError(
                        f"ledger creation refused at {self.ledger}: the creator's pid "
                        "namespace cannot be read (/proc/self/ns/pid); a null namespace "
                        "is never recorded"
                    )
            try:
                os.mkdir(self.ledger)
            except FileExistsError:
                created = False
            else:
                created = True
            self.reservations_dir.mkdir(exist_ok=True)
            if created:
                assert creator_ns is not None
                with self._locked():
                    self._write_namespace_record(
                        self._boot_id(), creator_ns, None, claimed=False, event="created"
                    )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _flock(self.ledger):
            yield

    # -- the namespace record ------------------------------------------------- #

    def _read_namespace_record(self) -> dict[str, Any] | None:
        data = _read_json(self.namespace_path)
        if (
            isinstance(data, dict)
            and isinstance(data.get("boot_id"), str)
            and (data.get("pid_ns") is None or isinstance(data.get("pid_ns"), str))
            and "pid_ns" in data
        ):
            return data
        return None

    def _write_namespace_record(
        self,
        boot: str,
        ns: str,
        previous: dict[str, Any] | None,
        *,
        claimed: bool,
        event: str,
    ) -> None:
        """Atomically (re)write the record. The previous record is kept inside
        the new one (``previous``) and every event is appended to
        ``history``, so nothing is lost. ``ns`` is never None."""
        if not isinstance(ns, str) or not ns:
            raise StorageLedgerError("refusing to record a null pid namespace")
        earlier: list[Any] = []
        history: list[Any] = []
        if previous is not None:
            earlier = list(previous.get("previous", []))
            earlier.append(
                {k: v for k, v in previous.items() if k not in ("previous", "history")}
            )
            history = list(previous.get("history", []))
        now = _iso(self._clock())
        history.append(
            {"event": event, "boot_id": boot, "pid_ns": ns, "utc": now, "pid": self._getpid()}
        )
        _atomic_write(
            self.namespace_path,
            json.dumps(
                {
                    "boot_id": boot,
                    "pid_ns": ns,
                    "claimed": claimed,
                    "recorded_utc": now,
                    "recorded_by_pid": self._getpid(),
                    "history": history,
                    "previous": earlier,
                },
                indent=2,
            ),
        )

    def _claimed_for(self, record: dict[str, Any], boot: str) -> bool:
        return record.get("claimed") is True and record.get("boot_id") == boot

    def _namespace_refusal(self, own_ns: str | None) -> str | None:
        """Under the lock, for ``reserve()`` and ``would_admit()``: compare
        only -- never record. Returns a refusal reason or None."""
        if own_ns is None:
            return "reserve outside the sandbox: the caller's pid namespace cannot be read"
        record = self._read_namespace_record()
        if record is None:
            return (
                f"ledger namespace record missing ({self.namespace_path.name} absent or "
                "unreadable); an operator must restore it"
            )
        boot = self._boot_id()
        if not self._claimed_for(record, boot):
            return (
                f"boot not claimed: boot {boot} has no claim in the ledger (last record: "
                f"boot {record.get('boot_id')}, claimed={record.get('claimed') is True}); "
                "a host entry point (hoh run, hoh resume-storage, the launcher) claims it"
            )
        if record["pid_ns"] != own_ns:
            return (
                f"reserve outside the sandbox: caller pid namespace {own_ns} differs from "
                f"the ledger's {record['pid_ns']} for this boot"
            )
        return None

    def claim_boot(self) -> dict[str, Any]:
        """Claim the current boot for the caller's pid namespace.

        Called only by host entry points, before their first ``reserve()`` or
        ``would_admit()`` (see the module docstring for why that ordering is
        what keeps a sandbox from claiming). Under the ledger lock:

        * refused (``StorageLedgerError``, nothing recorded) when the
          caller's own namespace link cannot be read, or the namespace record
          is missing;
        * idempotent when this boot is already claimed by the same namespace;
        * refused when this boot is already claimed by another namespace;
        * otherwise the claim ``{boot_id, pid_ns}`` is recorded, the previous
          record kept in ``previous`` and the event appended to ``history``.

        Returns the record in force afterwards.
        """
        with self._locked():
            own_ns = self._pid_namespace()
            if own_ns is None:
                raise StorageLedgerError(
                    "claim_boot refused: the caller's pid namespace cannot be read "
                    "(/proc/self/ns/pid); nothing is recorded"
                )
            record = self._read_namespace_record()
            if record is None:
                raise StorageLedgerError(
                    f"claim_boot refused: ledger namespace record missing "
                    f"({self.namespace_path.name} absent or unreadable); an operator "
                    "must restore it"
                )
            boot = self._boot_id()
            if self._claimed_for(record, boot):
                if record["pid_ns"] == own_ns:
                    return record
                raise StorageLedgerError(
                    f"claim_boot refused: boot {boot} is already claimed by pid "
                    f"namespace {record['pid_ns']}; the caller is in {own_ns}"
                )
            self._write_namespace_record(boot, own_ns, record, claimed=True, event="claimed")
            claimed = self._read_namespace_record()
            if claimed is None:
                raise StorageLedgerError("claim_boot: the namespace record cannot be read back")
            return claimed

    # -- reading the ledger --------------------------------------------------- #

    def _history(self) -> tuple[list[dict[str, Any]], int]:
        """(well-formed records, number of malformed lines skipped). A torn
        line is skipped; the records after it are kept."""
        try:
            lines = self.history_path.read_text(encoding="utf-8", errors="replace").split("\n")
        except FileNotFoundError:
            return [], 0
        out: list[dict[str, Any]] = []
        malformed = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ok = (
                    isinstance(entry, dict)
                    and isinstance(entry.get("op_kind"), str)
                    and isinstance(entry.get("actual"), int)
                    and not isinstance(entry.get("actual"), bool)
                    and isinstance(entry.get("closed_utc"), str)
                )
                if ok:
                    _parse(entry["closed_utc"])
            except (ValueError, TypeError):
                ok = False
            if ok:
                out.append(entry)
            else:
                malformed += 1
        return out, malformed

    def _is_stale(self, res: Reservation, own_ns: str | None) -> bool | None:
        """True stale, False live, None foreign (cannot be judged; counted).
        A previous boot is judged first, before any namespace check."""
        if res.boot_id != self._boot_id():
            return True
        if res.pid_ns is None or own_ns is None or res.pid_ns != own_ns:
            return None
        if not self._pid_alive(res.pid):
            return True
        if res.pid_start_time is None:
            return False
        current = self._proc_start_time(res.pid)
        if current is None:
            return False
        return current != res.pid_start_time

    def _versioned(self, path: Path, tag: str) -> Path:
        prefix = f"{path.name}.{tag}.v"
        n = len(list(path.parent.glob(prefix + "*"))) + 1
        while True:
            target = path.with_name(f"{prefix}{n}.{_compact(self._clock())}")
            if not target.exists():
                return target
            n += 1

    def _scan(self, *, rename: bool = True) -> _Scan:
        """Judge every open reservation file. Newly stale files are renamed,
        never removed; malformed files are left untouched. With
        ``rename=False`` (the dry run of ``would_admit``) a stale file is
        judged and not counted exactly the same, but left where it is.
        Call under the lock."""
        own_ns = self._pid_namespace()
        scan = _Scan()
        for path in sorted(self.reservations_dir.glob("*.json")):
            res = Reservation.from_file(path)
            if res is None:
                scan.malformed.append(path.name)
                continue
            verdict = self._is_stale(res, own_ns)
            if verdict is True:
                if not rename:
                    scan.stale.append(res)
                    continue
                target = self._versioned(path, "stale")
                os.replace(path, target)
                scan.stale.append(replace(res, path=target))
            else:
                scan.counted.append(res)
                if verdict is None:
                    scan.foreign.append(res)
        return scan

    def _stale_files(self) -> list[Reservation]:
        out = []
        for path in sorted(self.reservations_dir.glob("*.json.stale.v*")):
            res = Reservation.from_file(path)
            if res is not None:
                out.append(res)
        return out

    def _closed_files(self) -> list[Reservation]:
        out = []
        for path in sorted(self.reservations_dir.glob("*.json.closed.v*")):
            res = Reservation.from_file(path)
            if res is not None:
                out.append(res)
        return out

    # -- observation --------------------------------------------------------- #

    def _record_observation(self, obs: Observation, effective: int | None, now: datetime) -> None:
        # Only a real, observed host reading is persisted -- never a derived
        # term and never a reading of an unobservable host.
        if not obs.host_unknown and obs.host_free_bytes is not None:
            _atomic_write(
                self.host_path,
                json.dumps(
                    {
                        "host_free_bytes": obs.host_free_bytes,
                        "host_note": obs.host_note,
                        "observed_utc": obs.observed_utc,
                        "recorded_utc": _iso(now),
                    },
                    indent=2,
                ),
            )
        if effective is None:
            return
        points = _read_json(self.observations_path)
        if not isinstance(points, list):
            points = []
        points.append({"t": _iso(now), "effective_free_bytes": effective})
        _atomic_write(self.observations_path, json.dumps(points[-_OBSERVATIONS_KEPT:], indent=2))

    def _host_term(self, now: datetime) -> tuple[int | None, str]:
        """The derived host term for an unobservable host (see class doc)."""
        last = _read_json(self.host_path)
        try:
            recorded = _parse(last["recorded_utc"])
            last_free = int(last["host_free_bytes"])
        except (TypeError, KeyError, ValueError):
            return None, "host unobservable and no readable persisted host observation"
        age = (now - recorded).total_seconds()
        if age >= self.policy.host_max_age_s:
            return None, (
                f"host unobservable and the last host observation is {age:.0f} s old "
                f"(limit {self.policy.host_max_age_s:.0f} s)"
            )
        opened = sum(
            r.estimate
            for r in [
                *(Reservation.from_file(p) for p in self.reservations_dir.glob("*.json")),
                *self._stale_files(),
                *self._closed_files(),
            ]
            if r is not None and _parse(r.opened_utc) >= recorded
        )
        released = sum(
            h["actual"] for h in self._history()[0] if _parse(h["closed_utc"]) >= recorded
        )
        term = last_free - opened - released
        return term, (
            f"host unobservable; derived from observation recorded {last['recorded_utc']}: "
            f"{last['host_free_bytes']} - opened {opened} - released {released}"
        )

    def _assess(self, now: datetime) -> tuple[Observation, int | None, PressureState]:
        """Load the persisted state, observe, derive the host term if needed,
        update and persist the state. Call under the lock."""
        self.tracker.load()
        obs = self.observer.observe()
        effective: int | None = obs.effective_free_bytes
        if obs.host_unknown:
            term, note = self._host_term(now)
            if term is None:
                effective = None
                obs = replace(obs, host_note=f"{obs.host_note}; {note}".strip("; "))
            else:
                obs = replace(obs, host_free_bytes=term,
                              host_note=f"{obs.host_note}; {note}".strip("; "))
                effective = obs.effective_free_bytes
        state = self.tracker.update(obs)
        self._record_observation(obs, effective, now)
        return obs, effective, state

    # -- public interface ---------------------------------------------------- #

    def estimate(self, op_kind: str) -> int:
        """``max(defaults[kind], safety_factor x p95 of the last
        history_window actuals of that kind)`` -- windowed, so it does not
        ratchet on an old outlier."""
        if op_kind not in OP_KINDS:
            raise ValueError(f"unknown operation kind {op_kind!r}; known: {list(OP_KINDS)}")
        default = self.policy.defaults[op_kind]
        actuals = [
            h["actual"] for h in self._history()[0] if h["op_kind"] == op_kind
        ][-self.policy.history_window:]
        if not actuals:
            return default
        ordered = sorted(actuals)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        return max(default, math.ceil(self.policy.safety_factor * p95))

    def _check_request(self, project_id: str, op_kind: str, estimate_bytes: int | None) -> None:
        if op_kind not in OP_KINDS:
            raise ValueError(f"unknown operation kind {op_kind!r}; known: {list(OP_KINDS)}")
        if not project_id:
            raise ValueError("project_id must not be empty")
        if estimate_bytes is not None and (
            not isinstance(estimate_bytes, int)
            or isinstance(estimate_bytes, bool)
            or estimate_bytes < 0
        ):
            raise ValueError(f"estimate_bytes must be a non-negative int, got {estimate_bytes!r}")

    def _decide(
        self,
        project_id: str,
        op_kind: str,
        estimate_bytes: int | None,
        now: datetime,
        *,
        rename: bool,
    ) -> tuple[Refusal | None, int, str | None]:
        """The admission decision, shared by ``reserve()`` and
        ``would_admit()``. Call under the lock. Returns (refusal or None,
        the estimate that would be reserved, the caller's namespace)."""
        own_ns = self._pid_namespace()
        ns_problem = self._namespace_refusal(own_ns)
        scan = self._scan(rename=rename)
        reserved = sum(r.estimate for r in scan.counted)
        estimate = self.estimate(op_kind)
        if estimate_bytes is not None:
            estimate = max(estimate_bytes, estimate)
        obs, effective, state = self._assess(now)

        def refuse(reason: str) -> Refusal:
            return Refusal(
                project_id=project_id,
                op_kind=op_kind,
                reason=reason,
                state=state,
                effective_free=effective,
                reserved=reserved,
                estimate=estimate,
                reserve=self.policy.reserve,
                host_note=obs.host_note,
            )

        decision: Refusal | None
        if ns_problem is not None:
            decision = refuse(ns_problem)
        elif scan.malformed:
            decision = refuse(
                "malformed reservation file(s) in the ledger, fail closed until an "
                f"operator renames them (versioned): {', '.join(scan.malformed)}"
            )
        elif state is PressureState.CRITICAL:
            decision = refuse("state CRITICAL refuses every write-heavy operation")
        elif effective is None:
            decision = refuse("host unobservable without a fresh persisted host observation")
        elif effective - reserved - estimate < self.policy.reserve:
            decision = refuse("the reservation would cross below the reserve")
        else:
            decision = None
        return decision, estimate, own_ns

    def reserve(
        self,
        project_id: str,
        op_kind: str,
        *,
        estimate_bytes: int | None = None,
    ) -> Reservation | Refusal:
        """Admit or refuse. The reserved amount is
        ``max(estimate_bytes, estimate(op_kind))``: a caller may reserve more
        than the history suggests, never less."""
        self._check_request(project_id, op_kind, estimate_bytes)
        with self._locked():
            now = self._clock()
            decision, estimate, own_ns = self._decide(
                project_id, op_kind, estimate_bytes, now, rename=True
            )
            self._before_commit()
            if decision is not None:
                return decision

            pid = self._getpid()
            rid = f"{_compact(now)}-{pid}-{secrets.token_hex(4)}"
            res = Reservation(
                id=rid,
                project_id=project_id,
                op_kind=op_kind,
                estimate=estimate,
                pid=pid,
                pid_start_time=self._proc_start_time(pid),
                boot_id=self._boot_id(),
                opened_utc=_iso(now),
                path=self.reservations_dir / f"{rid}.json",
                pid_ns=own_ns,
            )
            _atomic_write(res.path, json.dumps(res.to_json(), indent=2))
            return res

    def would_admit(self, op_kind: str, *, estimate_bytes: int | None = None) -> bool:
        """Whether ``reserve(..., op_kind)`` would be admitted now: the same
        computation under the same lock, with the same namespace and "boot
        not claimed" rules -- but writing no reservation file, no history line
        and renaming no stale file. It may persist the tracker state, the
        last observation and the last host reading, which are observations."""
        self._check_request("would-admit", op_kind, estimate_bytes)
        with self._locked():
            decision, _, _ = self._decide(
                "would-admit", op_kind, estimate_bytes, self._clock(), rename=False
            )
            return decision is None

    def _append_history(self, entry: dict[str, Any]) -> None:
        """One ``os.write`` of a complete line on an ``O_APPEND`` descriptor.
        If the file does not end with a newline (a torn line, e.g. after
        ENOSPC), the newline is put in front of the record in the same write,
        so the record never glues onto the torn fragment. Call under the lock."""
        line = (json.dumps(entry) + "\n").encode("utf-8")
        fd = os.open(self.history_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short write to {self.history_path}: {written} of {len(line)}")
            os.fsync(fd)
        finally:
            os.close(fd)

    def release(self, reservation: Reservation, actual_bytes: int) -> Path:
        """Close a reservation: append the record to ``history.jsonl``, then
        rename its file to a versioned closed name. Returns the closed file.
        If the history cannot be written the reservation stays open (counted).

        A reservation already judged stale is still recorded (the bytes were
        really written); its stale file stays as it is. A second release --
        of an open reservation or of a stale one already recorded in the
        history -- is refused (``StorageLedgerError``)."""
        if (
            not isinstance(actual_bytes, int)
            or isinstance(actual_bytes, bool)
            or actual_bytes < 0
        ):
            raise ValueError(f"actual_bytes must be a non-negative int, got {actual_bytes!r}")
        with self._locked():
            now = self._clock()
            path = self.reservations_dir / f"{reservation.id}.json"
            stale_target: Path | None = None
            if not path.exists():
                stale = [r for r in self._stale_files() if r.id == reservation.id]
                if not stale:
                    raise StorageLedgerError(
                        f"reservation {reservation.id} is not open (already released?)"
                    )
                if any(h.get("id") == reservation.id for h in self._history()[0]):
                    raise StorageLedgerError(
                        f"reservation {reservation.id} is stale and was already released "
                        "once (its history record exists)"
                    )
                stale_target = stale[-1].path
            self._append_history(
                {
                    "op_kind": reservation.op_kind,
                    "project_id": reservation.project_id,
                    "estimate": reservation.estimate,
                    "actual": actual_bytes,
                    "opened_utc": reservation.opened_utc,
                    "closed_utc": _iso(now),
                    "id": reservation.id,
                }
            )
            if stale_target is not None:
                return stale_target
            target = self._versioned(path, "closed")
            os.replace(path, target)
            return target

    def stale_reservations(self) -> list[Reservation]:
        """Every stale reservation in the ledger (renaming newly found ones).
        They are not counted by admission; their files are kept."""
        with self._locked():
            self._scan()
            return self._stale_files()

    def open_reservations(self) -> list[Reservation]:
        """The counted reservations (live and foreign-namespace ones)."""
        with self._locked():
            return self._scan().counted

    def observe(self) -> tuple[Observation, PressureState]:
        """Observe now, persist the reading and update the state (the
        persisted state is loaded under the lock first)."""
        with self._locked():
            obs, _, state = self._assess(self._clock())
            return obs, state

    def status(self) -> dict[str, Any]:
        """A report: state, effective free, reserve, open reservations,
        headroom, a naive ``seconds_to_reserve`` forecast, and what the
        ledger could not judge (malformed files, foreign-namespace
        reservations, malformed history lines)."""
        with self._locked():
            now = self._clock()
            scan = self._scan()
            obs, effective, state = self._assess(now)
            reserved = sum(r.estimate for r in scan.counted)
            headroom = None if effective is None else effective - reserved - self.policy.reserve
            return {
                "state": state.value,
                "effective_free_bytes": effective,
                "guest_free_bytes": obs.guest_free_bytes,
                "guest_free_inodes": obs.guest_free_inodes,
                "guest_total_inodes": obs.guest_total_inodes,
                "inode_free_ratio": (
                    obs.guest_free_inodes / obs.guest_total_inodes
                    if obs.guest_total_inodes else None
                ),
                # The raw axis, without hysteresis. NOT_MEASURED where the axis is
                # off (f_files == 0): an operator must see that, not a quiet NORMAL.
                "inode_state": (
                    "NOT_MEASURED" if obs.guest_total_inodes == 0
                    else self.tracker.inode_axis(
                        obs.guest_free_inodes, obs.guest_total_inodes
                    ).value
                ),
                "host_free_bytes": obs.host_free_bytes,
                "host_unknown": obs.host_unknown,
                "host_note": obs.host_note,
                "reserve_bytes": self.policy.reserve,
                "reserved_bytes": reserved,
                "open_reservations": [r.to_json() for r in scan.counted],
                "foreign_namespace": [
                    {"file": r.path.name, **r.to_json()} for r in scan.foreign
                ],
                "malformed_reservations": list(scan.malformed),
                "history_malformed_lines": self._history()[1],
                "namespace_record": self._read_namespace_record(),
                "headroom_bytes": headroom,
                "seconds_to_reserve": self._seconds_to_reserve(effective),
                "observed_utc": obs.observed_utc,
                "ledger": str(self.ledger),
            }

    def _seconds_to_reserve(self, effective: int | None) -> float | None:
        """Least-squares slope over the last observations; a forecast only
        when free space is falling. Time until effective free reaches the
        reserve (0 when already there)."""
        if effective is None:
            return None
        points = _read_json(self.observations_path)
        if not isinstance(points, list):
            return None
        pts = [
            (_parse(p["t"]).timestamp(), float(p["effective_free_bytes"]))
            for p in points[-_SLOPE_POINTS:]
            if isinstance(p, dict) and "t" in p and "effective_free_bytes" in p
        ]
        if len(pts) < 2:
            return None
        mean_t = sum(t for t, _ in pts) / len(pts)
        mean_y = sum(y for _, y in pts) / len(pts)
        var = sum((t - mean_t) ** 2 for t, _ in pts)
        if var == 0:
            return None
        slope = sum((t - mean_t) * (y - mean_y) for t, y in pts) / var
        if slope >= 0:
            return None
        return max(0.0, (effective - self.policy.reserve) / -slope)
