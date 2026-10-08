"""WSL and native-Ubuntu storage host adapters (STORAGE-F, P5-32 and P5-33).

On WSL the binding storage constraint can be the **host** volume under the
distribution's virtual disk, not the guest filesystem: on 2026-09-25 the VHDX
grew to 977 GiB of its 1 TiB while the guest still showed space, and freeing
guest blocks does not shrink a non-sparse VHDX. The operator's directive (A1,
sections M and N) asks for read-only observation and an **operator-gated**
state, which this module implements:

* ``detect_host_profile`` -- ``"wsl2"``, ``"native-linux"`` or ``"unknown"``
  from ``/proc/version`` and ``WSL_DISTRO_NAME`` (both injectable).
* ``WslHostAdapter`` -- free bytes of the Windows volume mount, allocated
  bytes of the *configured* VHDX (never discovered by scanning), and
  ``reclaim_assessment``: ``WSL_HOST_RECLAIM_REQUIRED`` when the VHDX holds at
  least ``reclaimable_threshold`` more than the guest uses **and** the host is
  below ``host_floor``. ``host_observer()`` hands STORAGE-B2 its own
  ``StatvfsHostObserver``, so B2's "configured path cannot be observed ->
  host_unknown" logic applies unchanged.
* ``NativeLinuxAdapter`` -- a native host is observed through the guest:
  ``host_observer()`` is ``None`` (configure STORAGE-B2's ``NoHost``), and
  there is no host-reclaim state.
* ``pre_restart_inventory`` / ``post_restart_checks`` -- the read-only
  integrity checks around an operator-performed shutdown, compaction or
  restart (``docs/STORAGE_HOST_RUNBOOK.md``).

What this module deliberately does **not** do:

* It never shuts WSL down, compacts, makes sparse, resizes or moves a disk.
  ``WSL_HOST_RECLAIM_REQUIRED`` is a report for the operator; ``runbook()``
  returns the operator's steps as text and never executes them. No method of
  either adapter runs a command.
* Its only process call is the one ``git fsck --connectivity-only`` inside
  ``post_restart_checks``; nothing here calls ``wsl.exe``, ``powershell.exe``
  or any Windows binary.
* It never removes a file or a directory. The inventory is written with a
  fresh temp name and ``os.replace`` onto a fresh name; a crash leaves the
  temp file behind rather than losing anything.
* It reads only what the storage registry (STORAGE-A) attributes to the
  calling project: a root the registry does not report ``OWNED`` or
  ``PROTECTED`` for that project (or as its registered project root) is
  ``NOT_CHECKED`` and fails the result; a child of a runs base the registry
  reports ``UNKNOWN`` is listed by name as ``UNKNOWN_SKIPPED`` and never
  opened **by this module**; the registry's classification reads
  ``<child>/state.json`` (a regular file, ``lstat``-checked) to decide
  ownership, and nothing else below the child is read.
* It trusts an inventory only as the newest of its project, taken in the boot
  before the current one: an inventory taken in the current boot, one with a
  newer sibling in the project's receipts, or one dated after the current
  wall clock is refused. A runs-base child the inventory does not list whose
  ``lstat`` ctime is newer than the inventory fails (metadata only, nothing
  opened); only the registry's owned ``_arenas/`` is exempt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hoh import storage_registry as sr
from hoh.storage_pressure import GiB, StatvfsHostObserver, StoragePolicy
from hoh.storage_registry import (
    OWNED,
    PROTECTED,
    UNKNOWN,
    SessionLease,
    StorageRegistry,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROFILE_WSL2 = "wsl2"
PROFILE_NATIVE = "native-linux"
PROFILE_UNKNOWN = "unknown"

WSL_HOST_RECLAIM_REQUIRED = "WSL_HOST_RECLAIM_REQUIRED"
HOST_OK = "HOST_OK"
HOST_UNKNOWN = "HOST_UNKNOWN"

NATIVE_OBSERVE_NOTE = "native: the guest is the host"
NATIVE_RECLAIM_NOTE = "no host-reclaim state on a native host"

#: Verdicts of one post-restart check. Only FAIL, NOT_CHECKED and NO_INVENTORY
#: fail the overall result; UNKNOWN_SKIPPED is a listing, never a failure.
PASS = "PASS"
FAIL = "FAIL"
NOT_CHECKED = "NOT_CHECKED"
UNKNOWN_SKIPPED = "UNKNOWN_SKIPPED"
NO_INVENTORY = "NO_INVENTORY"
_FAILING = frozenset({FAIL, NOT_CHECKED, NO_INVENTORY})

INVENTORY_SCHEMA = "veriharness.storage-host-inventory/2"
_INVENTORY_PREFIX = "pre-restart-inventory."
_RUN_STATE = "state.json"
_OWNER_FILE = "OWNER.json"
_BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"

#: The runbook shipped with the repository (``docs/`` beside ``src/``).
DEFAULT_RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "STORAGE_HOST_RUNBOOK.md"


class StorageHostError(RuntimeError):
    """A host-adapter operation was refused (e.g. an inventory without a
    live, matching session lease)."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _read_boot_id() -> str:
    """The kernel's id of the current boot (``/proc/sys/kernel/random/boot_id``)."""
    return Path(_BOOT_ID_FILE).read_text(encoding="utf-8").strip()


def _boot_of(reader: Callable[[], str]) -> tuple[str | None, str]:
    """(boot id, "") or (None, why): a reader that fails proves nothing."""
    try:
        value = reader()
    except Exception as exc:                    # an unreadable boot is a refusal
        return None, f"the boot id cannot be read: {type(exc).__name__}: {exc}"
    if not isinstance(value, str) or not value.strip():
        return None, f"the boot id reader returned {value!r}"
    return value.strip(), ""


def _is_ns(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --------------------------------------------------------------------------- #
# Profile detection
# --------------------------------------------------------------------------- #


def _read_proc_version() -> str:
    return Path("/proc/version").read_text(encoding="utf-8")


def detect_host_profile(
    *,
    proc_version: str | Callable[[], str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """``"wsl2"``, ``"native-linux"`` or ``"unknown"``.

    ``proc_version`` is the text of ``/proc/version`` or a reader returning it
    (default: reads the real file); ``env`` defaults to ``os.environ``. WSL
    when ``WSL_DISTRO_NAME`` is set or the kernel string names Microsoft or
    WSL; native when the kernel string reads as Linux without either;
    unknown when it cannot be read or is empty or unrecognised.
    """
    environ = os.environ if env is None else env
    if environ.get("WSL_DISTRO_NAME"):
        return PROFILE_WSL2
    if proc_version is None:
        proc_version = _read_proc_version
    try:
        text = proc_version() if callable(proc_version) else proc_version
    except OSError:
        return PROFILE_UNKNOWN
    if not isinstance(text, str) or not text.strip():
        return PROFILE_UNKNOWN
    lowered = text.lower()
    if "microsoft" in lowered or "wsl" in lowered:
        return PROFILE_WSL2
    if lowered.startswith("linux version"):
        return PROFILE_NATIVE
    return PROFILE_UNKNOWN


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HostConfig:
    """What the operator configures for the host adapters (bytes in bytes).

    ``vhdx_path`` is configured, never discovered: e.g.
    ``/mnt/c/Users/<user>/AppData/Local/wsl/<guid>/ext4.vhdx``; it must lie
    under ``windows_mount``. ``guest_path`` is the guest filesystem the
    Windows mount is compared against (a mount on the guest's own filesystem
    is not the host). ``workspace_volume`` is an optional mount point a
    native host reports separately.
    """

    windows_mount: str = "/mnt/c"
    vhdx_path: str | None = None
    reclaimable_threshold: int = 50 * GiB
    host_floor: int = 100 * GiB
    guest_path: str = "/"
    workspace_volume: str | None = None
    runbook_path: str = str(DEFAULT_RUNBOOK)

    def __post_init__(self) -> None:
        for name in ("reclaimable_threshold", "host_floor"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        for name in ("windows_mount", "guest_path"):
            if not os.path.isabs(getattr(self, name)):
                raise ValueError(f"{name} must be an absolute path, got {getattr(self, name)!r}")
        for name in ("vhdx_path", "workspace_volume"):
            value = getattr(self, name)
            if value is not None and not os.path.isabs(value):
                raise ValueError(f"{name} must be an absolute path or None, got {value!r}")


def _check_used(guest_used_bytes: int | None) -> None:
    if guest_used_bytes is None:
        return
    if (
        not isinstance(guest_used_bytes, int)
        or isinstance(guest_used_bytes, bool)
        or guest_used_bytes < 0
    ):
        raise ValueError(f"guest_used_bytes must be a non-negative int or None, "
                         f"got {guest_used_bytes!r}")


# --------------------------------------------------------------------------- #
# WSL
# --------------------------------------------------------------------------- #


class WslHostAdapter:
    """Read-only observer of the Windows side of a WSL 2 distribution.

    Implements STORAGE-B2's ``HostObserver`` (``configured``, ``observe()``)
    for reports; the admission input is ``host_observer()``. All probes
    (``statvfs``, ``stat``, ``ismount``) are injectable and are the only
    calls made on host paths. **No method runs a command**: the operator
    performs every host action (``runbook()``).
    """

    configured = True

    def __init__(
        self,
        config: HostConfig | None = None,
        *,
        statvfs: Callable[[Any], Any] = os.statvfs,
        stat: Callable[[Any], Any] = os.stat,
        ismount: Callable[[Any], bool] = os.path.ismount,
    ) -> None:
        self.config = config if config is not None else HostConfig()
        self._statvfs = statvfs
        self._stat = stat
        self._ismount = ismount

    def _observer(self, paths: Sequence[str], guest_path: Path | str | None
                  ) -> StatvfsHostObserver:
        return StatvfsHostObserver(
            paths, guest_path=guest_path, statvfs=self._statvfs, stat=self._stat,
            ismount=self._ismount,
        )

    def observe(self) -> tuple[int | None, str]:
        """Free bytes of the Windows volume mount (``f_bavail * f_frsize``),
        or ``None`` and a note when it cannot be observed: not a mount point,
        unreadable, or on the guest's own filesystem. ``None`` means "the host
        could not be observed", never "there is no host"."""
        free, note = self._observer([self.config.windows_mount],
                                    self.config.guest_path).observe()
        if free is None:
            return None, note
        return free, f"windows volume {self.config.windows_mount}"

    def host_observer(self, policy: StoragePolicy | None = None,
                      guest_path: Path | str | None = None) -> StatvfsHostObserver:
        """STORAGE-B2's ``StatvfsHostObserver`` over the Windows mount plus
        ``policy.host_paths``, with this adapter's probes. ``guest_path``
        ``None`` lets ``GuestObserver`` bind its own path."""
        paths: list[str] = [self.config.windows_mount]
        for extra in (policy.host_paths if policy is not None else ()):
            if str(extra) not in paths:
                paths.append(str(extra))
        return self._observer(paths, guest_path)

    def _vhdx(self) -> tuple[int | None, str]:
        path = self.config.vhdx_path
        if path is None:
            return None, "no VHDX path configured"
        mount = Path(self.config.windows_mount)
        if not Path(os.path.normpath(path)).is_relative_to(mount):
            return None, f"VHDX path {path} does not lie under the Windows mount {mount}"
        try:
            st = self._stat(path)
            allocated = int(st.st_blocks) * 512
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            return None, f"VHDX {path} cannot be read: {exc}"
        return allocated, f"VHDX {path} allocates {allocated} B"

    def vhdx_allocation(self) -> int | None:
        """Allocated bytes of the configured VHDX (``st_blocks * 512``), or
        ``None`` when unconfigured, outside the Windows mount or unreadable."""
        return self._vhdx()[0]

    def guest_used_bytes(self, path: Path | str | None = None) -> int | None:
        """Bytes in use on the guest filesystem holding ``path`` (default
        ``config.guest_path``): ``(f_blocks - f_bfree) * f_frsize``, or None."""
        try:
            st = self._statvfs(path if path is not None else self.config.guest_path)
            return (int(st.f_blocks) - int(st.f_bfree)) * int(st.f_frsize)
        except (OSError, AttributeError, TypeError, ValueError):
            return None

    def reclaim_assessment(self, guest_used_bytes: int | None) -> dict[str, Any]:
        """``WSL_HOST_RECLAIM_REQUIRED`` iff ``vhdx_allocation -
        guest_used_bytes >= reclaimable_threshold`` **and** host free ``<
        host_floor``; ``HOST_OK`` otherwise; ``HOST_UNKNOWN`` when any input
        is unknown. Names the numbers and the runbook. Runs nothing."""
        _check_used(guest_used_bytes)
        allocation, vhdx_note = self._vhdx()
        host_free, host_note = self.observe()
        cfg = self.config
        result: dict[str, Any] = {
            "vhdx_path": cfg.vhdx_path,
            "vhdx_allocation_bytes": allocation,
            "guest_used_bytes": guest_used_bytes,
            "reclaimable_bytes": None,
            "host_free_bytes": host_free,
            "reclaimable_threshold": cfg.reclaimable_threshold,
            "host_floor": cfg.host_floor,
            "windows_mount": cfg.windows_mount,
            "runbook": cfg.runbook_path,
            "operator_only": True,
        }
        missing = [name for name, value in (
            ("VHDX allocation", allocation), ("host free space", host_free),
            ("guest used bytes", guest_used_bytes),
        ) if value is None]
        if missing:
            result["state"] = HOST_UNKNOWN
            result["note"] = (f"unknown: {', '.join(missing)} ({vhdx_note}; {host_note}); "
                              "no host-reclaim decision without them")
            return result
        assert allocation is not None and host_free is not None and guest_used_bytes is not None
        reclaimable = allocation - guest_used_bytes
        result["reclaimable_bytes"] = reclaimable
        if reclaimable >= cfg.reclaimable_threshold and host_free < cfg.host_floor:
            result["state"] = WSL_HOST_RECLAIM_REQUIRED
            result["note"] = (
                f"the VHDX holds {reclaimable} B more than the guest uses (threshold "
                f"{cfg.reclaimable_threshold} B) and the host has {host_free} B free (floor "
                f"{cfg.host_floor} B); the operator reclaims it per {cfg.runbook_path} -- "
                "VeriHarness never does"
            )
        else:
            result["state"] = HOST_OK
            result["note"] = (
                f"reclaimable {reclaimable} B (threshold {cfg.reclaimable_threshold} B), "
                f"host free {host_free} B (floor {cfg.host_floor} B)"
            )
        return result

    def runbook(self) -> str:
        """The operator's steps as text, read from ``config.runbook_path``.
        Never executes them; an unreadable runbook raises, it is not
        replaced by a guess."""
        try:
            return Path(self.config.runbook_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageHostError(
                f"the host runbook {self.config.runbook_path} cannot be read: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# Native Linux
# --------------------------------------------------------------------------- #


class NativeLinuxAdapter:
    """A native Ubuntu host: the guest observation is the host observation.

    ``host_observer()`` returns ``None``, meaning "configure no host path"
    (STORAGE-B2's ``NoHost``): a native host is observed through the guest,
    so its free space is the guest's own ``statvfs``. This is not "the host
    could not be observed" -- that is ``WslHostAdapter.observe()`` returning
    ``None``. There is no host-reclaim state.
    """

    configured = False

    def __init__(
        self,
        config: HostConfig | None = None,
        *,
        statvfs: Callable[[Any], Any] = os.statvfs,
        ismount: Callable[[Any], bool] = os.path.ismount,
    ) -> None:
        self.config = config if config is not None else HostConfig()
        self._statvfs = statvfs
        self._ismount = ismount

    def observe(self) -> tuple[int | None, str]:
        return None, NATIVE_OBSERVE_NOTE

    def host_observer(self, policy: StoragePolicy | None = None,
                      guest_path: Path | str | None = None) -> None:
        """``None``: configure ``NoHost``; the guest is observed directly."""
        return None

    def reclaim_assessment(self, guest_used_bytes: int | None = None) -> dict[str, Any]:
        _check_used(guest_used_bytes)
        return {"state": HOST_OK, "note": NATIVE_RECLAIM_NOTE,
                "guest_used_bytes": guest_used_bytes}

    def workspace_volume(self) -> tuple[int | None, str]:
        """Free bytes of the configured workspace volume, reported separately
        from the guest: ``(None, note)`` when unconfigured, not a mount point
        or unreadable."""
        path = self.config.workspace_volume
        if path is None:
            return None, "no workspace volume configured"
        try:
            if not self._ismount(path):
                return None, f"workspace volume {path} is not a mount point"
            st = self._statvfs(path)
            return int(st.f_bavail) * int(st.f_frsize), f"workspace volume {path}"
        except OSError as exc:
            return None, f"workspace volume {path} not observable: {exc}"


def adapter_for_profile(
    profile: str, config: HostConfig | None = None, **probes: Any
) -> WslHostAdapter | NativeLinuxAdapter:
    """The adapter for a detected profile; ``unknown`` is refused, never
    guessed."""
    if profile == PROFILE_WSL2:
        return WslHostAdapter(config, **probes)
    if profile == PROFILE_NATIVE:
        return NativeLinuxAdapter(config, **{k: v for k, v in probes.items()
                                             if k in ("statvfs", "ismount")})
    raise StorageHostError(f"host profile {profile!r} has no adapter; refusing to guess")


# --------------------------------------------------------------------------- #
# Restart integrity: inventory and checks
# --------------------------------------------------------------------------- #


def _ours(own: sr.Ownership, project_id: str | None) -> bool:
    return own.status in (OWNED, PROTECTED) and project_id is not None \
        and own.project_id == project_id


def _read_owned(registry: StorageRegistry, path: Path, project_id: str
                ) -> tuple[bytes | None, str]:
    """The bytes of ``path`` if the registry reports it ``OWNED`` by the
    project (read through ``Ownership.resolved``), else ``None`` and why."""
    own = registry.owner_of(path)
    if own.status != OWNED or own.project_id != project_id or own.resolved is None:
        return None, f"{path} is {own.status} ({own.reason}); not read"
    try:
        return own.resolved.read_bytes(), ""
    except OSError as exc:
        return None, f"{path} cannot be read: {exc}"


def _parse_run_state(raw: bytes, run_id: str) -> str:
    """'' if ``raw`` is a run state for ``run_id`` (the product's own
    parser), else why not."""
    from hoh.store import RunStore

    try:
        state = RunStore._parse_state(raw.decode("utf-8"))
    except Exception as exc:                    # unparseable is the finding
        return f"state.json does not parse: {type(exc).__name__}: {str(exc)[:200]}"
    if state.run_id != run_id:
        return f"state.json names run {state.run_id!r}, not {run_id!r}"
    return ""


def _owned_by(own: sr.Ownership, project_id: str | None) -> bool:
    return own.status == OWNED and project_id is not None and own.project_id == project_id


def _newer_than(child: Path, created_ns: int) -> tuple[bool, str]:
    """(changed after ``created_ns``?, detail) from ``lstat`` alone: the
    child's ctime. A child that cannot be ``lstat``-ed counts as changed."""
    try:
        ctime_ns = os.lstat(child).st_ctime_ns
    except OSError as exc:
        return True, f"cannot be lstat-ed ({exc})"
    if ctime_ns > created_ns:
        return True, (f"changed after it (ctime {(ctime_ns - created_ns) / 1e9:.3f} s after "
                      "the inventory)")
    return False, ""


def _is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _children(base: Path) -> list[str]:
    with os.scandir(base) as entries:
        return sorted(entry.name for entry in entries)


def _write_new(directory: Path, name: str, payload: str) -> Path:
    """Write ``payload`` to a fresh file in ``directory``: exclusive temp
    name, fsync, ``os.replace`` onto a name that does not exist, fsync of
    the directory. Nothing is overwritten and nothing is removed."""
    target = directory / name
    while os.path.lexists(target):
        target = directory / f"{Path(name).stem}.{secrets.token_hex(3)}{Path(name).suffix}"
    tmp = directory / f".{target.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return target


def _require_lease(registry: StorageRegistry, project_id: str, session: SessionLease) -> None:
    """Refuse unless ``session`` is the live lease on record for its session,
    held by its holder (the token matches the recorded digest), for
    ``project_id``. ``scratch_dir`` checks only the record, not the token,
    so this is checked here explicitly."""
    if not isinstance(session, SessionLease):
        raise StorageHostError(f"session must be a SessionLease, got {type(session).__name__}")
    if session.project_id != project_id:
        raise StorageHostError(
            f"the lease belongs to project {session.project_id!r}, not {project_id!r}"
        )
    if not isinstance(session.token, str) or not session.token:
        raise StorageHostError("the lease carries no token: only its holder may write")
    if not registry.lease_is_live(session):
        raise StorageHostError(f"the lease of session {session.session_id!r} is not live")
    recorded = registry._read_lease(project_id, session.session_id)
    if recorded is None or recorded != session:
        raise StorageHostError(
            f"the lease given is not the one on record for session {session.session_id!r}"
        )
    record = sr._read_record(registry._lease_path(project_id, session.session_id)) or {}
    digest = record.get("token_sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, hashlib.sha256(session.token.encode("utf-8")).hexdigest()
    ):
        raise StorageHostError(
            f"the token does not match the lease of session {session.session_id!r}"
        )


def pre_restart_inventory(
    registry: StorageRegistry,
    project_id: str,
    session: SessionLease,
    runs_bases: Iterable[Path | str],
    *,
    boot_id: Callable[[], str] = _read_boot_id,
    clock: Callable[[], int] = time.time_ns,
) -> Path:
    """Record, per runs base, the run directories the registry reports
    ``OWNED`` by ``project_id`` with the SHA-256 of each ``state.json``.

    The record names the current boot (``boot_id``, default
    ``/proc/sys/kernel/random/boot_id``) and its creation time
    (``created_utc``, and ``created_ns`` from ``clock`` for comparisons),
    taken **before** the runs bases are listed, so anything that changes
    while the inventory is taken counts as newer than it.

    Refused (``StorageHostError``) unless ``session`` is a live lease that
    equals the registry's record and carries the holder's token, of
    ``project_id``, and unless every runs base is ``OWNED`` or ``PROTECTED``
    for the project. Written atomically into the session's registered
    ``receipts`` scratch (``registry.scratch_dir(project_id,
    session.session_id, "receipts")``); the lease is re-checked right before
    the write. Returns the inventory path. Changes nothing else.
    """
    _require_lease(registry, project_id, session)
    boot, why = _boot_of(boot_id)
    if boot is None:
        raise StorageHostError(f"cannot inventory without the current boot: {why}")
    created_ns = clock()
    if not _is_ns(created_ns):
        raise StorageHostError(f"the clock returned {created_ns!r}, not nanoseconds")
    created_utc = datetime.fromtimestamp(created_ns / 1e9, timezone.utc).isoformat(
        timespec="microseconds")
    bases: list[dict[str, Any]] = []
    for raw in runs_bases:
        base = Path(raw)
        own = registry.owner_of(base)
        if not _ours(own, project_id) or own.resolved is None:
            raise StorageHostError(
                f"runs base {base} is {own.status} for project {own.project_id!r} "
                f"({own.reason}); it is not {project_id!r}'s to inventory"
            )
        runs: dict[str, dict[str, Any]] = {}
        for name in _children(own.resolved):
            child = own.resolved / name
            if registry.owner_of(child).status != OWNED:
                continue
            if not _is_dir(child):
                continue
            if not os.path.lexists(child / _RUN_STATE):
                continue                        # e.g. _arenas/: not a run
            raw_state, why = _read_owned(registry, child / _RUN_STATE, project_id)
            if raw_state is None:
                raise StorageHostError(f"cannot inventory {child}: {why}")
            runs[name] = {"state_sha256": hashlib.sha256(raw_state).hexdigest(),
                          "state_bytes": len(raw_state)}
        bases.append({"path": str(own.resolved), "runs": runs})
    directory = registry.scratch_dir(project_id, session.session_id, "receipts")
    _require_lease(registry, project_id, session)
    record = {
        "schema": INVENTORY_SCHEMA,
        "project_id": project_id,
        "session_id": session.session_id,
        "boot_id": boot,
        "created_utc": created_utc,
        "created_ns": created_ns,
        "runs_bases": bases,
    }
    return _write_new(directory, f"{_INVENTORY_PREFIX}{_utc_stamp()}.json",
                      json.dumps(record, indent=2, sort_keys=True) + "\n")


def _valid_inventory(record: Any, project_id: str | None) -> bool:
    return (
        isinstance(record, dict)
        and record.get("schema") == INVENTORY_SCHEMA
        and record.get("project_id") == project_id
        and isinstance(record.get("boot_id"), str) and bool(record["boot_id"])
        and _is_ns(record.get("created_ns"))
        and isinstance(record.get("runs_bases"), list)
        and all(isinstance(b, dict) and isinstance(b.get("path"), str)
                and isinstance(b.get("runs"), dict) for b in record["runs_bases"])
    )


def _load_inventory(registry: StorageRegistry, inventory: Path | str
                    ) -> tuple[dict[str, Any] | None, str | None, Path | None, str]:
    """(record, owning project, resolved path, problem). The inventory counts
    only as a ``receipts`` file the registry reports ``OWNED``."""
    own = registry.owner_of(inventory)
    if own.status != OWNED or own.kind != "receipts" or own.resolved is None:
        return None, None, None, (f"inventory {inventory} is {own.status} ({own.reason}), "
                                  "not an owned receipts file")
    try:
        record = json.loads(own.resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, own.project_id, own.resolved, f"inventory {inventory} cannot be read: {exc}"
    if not _valid_inventory(record, own.project_id):
        return None, own.project_id, own.resolved, (
            f"inventory {inventory} is not a valid {INVENTORY_SCHEMA}")
    return record, own.project_id, own.resolved, ""


def _newer_inventories(registry: StorageRegistry, project_id: str, own_path: Path,
                       created_ns: int) -> tuple[list[str], list[str]]:
    """(inventories of ``project_id`` at least as new as ``created_ns``,
    problems). Looks only in the project's ``receipts`` scratch and reads only
    files the registry reports ``OWNED`` there; a receipt named like an
    inventory that cannot be read or is not valid is a problem (it might be
    the newer one)."""
    kind_dir = registry.state_root / sr._PROJECTS / project_id / "receipts"
    newer: list[str] = []
    problems: list[str] = []
    try:
        sessions = _children(kind_dir)
    except OSError as exc:
        return [], [f"{kind_dir} cannot be listed: {exc}"]
    for session_name in sessions:
        session_dir = kind_dir / session_name
        if not _is_dir(session_dir):
            continue
        try:
            names = _children(session_dir)
        except OSError as exc:
            problems.append(f"{session_dir} cannot be listed: {exc}")
            continue
        for name in names:
            if not (name.startswith(_INVENTORY_PREFIX) and name.endswith(".json")):
                continue
            own = registry.owner_of(session_dir / name)
            if (own.status != OWNED or own.kind != "receipts" or own.project_id != project_id
                    or own.resolved is None):
                continue                        # not a registered receipt: not read
            if own.resolved == own_path:
                continue
            try:
                other = json.loads(own.resolved.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"{own.resolved} cannot be read: {exc}")
                continue
            if not _valid_inventory(other, project_id):
                problems.append(f"{own.resolved} is not a valid {INVENTORY_SCHEMA}")
                continue
            if other["created_ns"] >= created_ns:
                newer.append(str(own.resolved))
    return newer, problems


def _inventory_freshness(registry: StorageRegistry, project_id: str, path: Path,
                         record: dict[str, Any], current_boot: str | None, boot_why: str,
                         now_ns: int) -> str:
    """'' if the inventory may vouch for this restart, else why not: it must
    be from a boot other than the current one, not dated after the current
    wall clock, and the newest inventory of its project."""
    if current_boot is None:
        return f"cannot judge the inventory's boot: {boot_why}"
    if record["boot_id"] == current_boot:
        return (f"taken in the current boot {current_boot}: an inventory must predate the "
                "restart it vouches for")
    if now_ns < record["created_ns"]:
        return (f"dated {(record['created_ns'] - now_ns) / 1e9:.3f} s after the current wall "
                "clock: the clock stepped back across the restart, ctimes cannot be compared")
    newer, problems = _newer_inventories(registry, project_id, path, record["created_ns"])
    if problems:
        return "cannot tell whether a newer inventory exists: " + "; ".join(problems)
    if newer:
        return f"a newer inventory of project {project_id!r} exists: {', '.join(newer)}"
    return ""


def _env_for_git() -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k not in sr._GIT_REDIRECTS and not k.startswith(sr._GIT_CONFIG_INJECTION)
    }
    env.update(sr._GIT_FIXED_ENV)
    return env


def post_restart_checks(
    registry: StorageRegistry,
    state_root: Path | str,
    project_roots: Iterable[Path | str],
    runs_bases: Iterable[Path | str],
    *,
    inventory: Path | str | None = None,
    project_id: str | None = None,
    git_fsck: Callable[[Path], tuple[bool, str]] | None = None,
    boot_id: Callable[[], str] = _read_boot_id,
    clock: Callable[[], int] = time.time_ns,
) -> dict[str, Any]:
    """Read-only checks after an operator-performed restart or compaction.

    Every root the caller asks to check -- ``state_root``, each of
    ``project_roots`` and ``runs_bases`` -- is read only when it is
    attributable to the calling project: the registry's state root itself,
    the project's registered project root, or a path ``owner_of`` reports
    ``OWNED`` or ``PROTECTED`` for that project. Anything else is
    ``NOT_CHECKED`` and fails the result. The calling project is
    ``project_id`` or, if not given, the project owning ``inventory``.

    * state root: ``OWNER.json`` exists and is a JSON object;
    * project root holding ``.git``: ``git fsck --connectivity-only`` passes
      (``git_fsck`` is injectable; the default is the module's only process
      call);
    * runs base: its children are listed by name; a child the registry
      reports ``OWNED`` for the project is opened and its ``state.json``
      must parse (with ``run_id`` equal to the name); any other child is
      ``UNKNOWN_SKIPPED`` (never opened by this module; failing only under
      the ctime rule below);
    * with ``inventory`` (from ``pre_restart_inventory``): every inventoried
      run that is no longer ``OWNED`` or whose ``state.json`` no longer
      parses is ``FAIL``, never ``UNKNOWN_SKIPPED``; a runs base the
      inventory does not cover, and an inventoried base not asked for, fail.
      Without an inventory the result carries ``NO_INVENTORY`` and cannot
      pass while ``runs_bases`` is not empty.
    * the inventory is refused (``FAIL``) when it was taken in the current
      boot (``boot_id``), is dated after the current wall clock (``clock``),
      or a newer inventory of the project exists in its receipts; its age is
      reported as ``inventory_age_seconds``.
    * a child of an asked runs base that the inventory does not list and
      whose ``lstat`` ctime is newer than the inventory is ``FAIL`` -- judged
      on metadata only, nothing below it is opened, not even by the
      registry's classification; only the registry's owned ``_arenas/`` is
      exempt. Run directories, ``timers.json`` and ``.resume.lock`` are
      judged alike: nothing may write a runs base between the inventory and
      the checks (the runbook disables the timers and pauses the runs).

    A child the registry reports ``UNKNOWN`` is never opened **by this
    module**; the registry's classification reads ``<child>/state.json`` (a
    regular file, ``lstat``-checked) to decide ownership.

    Returns ``{"result": "PASS"|"FAIL", "checks": [...], ...}``; changes
    nothing on disk.
    """

    def _default_git_fsck(repo: Path) -> tuple[bool, str]:
        env = _env_for_git()
        env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), "fsck", "--connectivity-only", "--no-dangling",
                 "--no-progress"],
                capture_output=True, text=True, env=env, timeout=600, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"git fsck could not run: {exc}"
        output = (done.stdout + done.stderr).strip()
        return done.returncode == 0, f"exit {done.returncode}: {output[-1500:]}"

    fsck = git_fsck if git_fsck is not None else _default_git_fsck
    checks: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    def add(check: str, target: Path | str, status: str, detail: str = "") -> None:
        checks.append({"check": check, "path": str(target), "status": status,
                       "detail": detail})

    # -- the inventory and the calling project ------------------------------ #
    record: dict[str, Any] | None = None
    caller = project_id
    age_seconds: float | None = None
    current_boot, boot_why = _boot_of(boot_id)
    if inventory is not None:
        record, inv_project, inv_path, problem = _load_inventory(registry, inventory)
        if record is None:
            add("inventory", inventory, FAIL, problem)
        elif caller is not None and inv_project != caller:
            add("inventory", inventory, FAIL,
                f"the inventory belongs to project {inv_project!r}, not {caller!r}")
            record = None
        else:
            assert inv_project is not None and inv_path is not None
            caller = inv_project
            now_ns = clock()
            age_seconds = (now_ns - record["created_ns"]) / 1e9
            stale = _inventory_freshness(registry, caller, inv_path, record, current_boot,
                                         boot_why, now_ns)
            if stale:
                add("inventory", inventory, FAIL, f"inventory refused: {stale}")
            else:
                add("inventory", inventory, PASS,
                    f"inventory of project {caller!r} from boot {record['boot_id']}, "
                    f"taken {age_seconds:.0f} s ago")
    runs_bases = [Path(b) for b in runs_bases]
    if inventory is None and runs_bases:
        add("inventory", "-", NO_INVENTORY,
            "no pre-restart inventory: runs bases cannot pass without one")

    registered_root: Path | None = None
    if caller is not None:
        project = registry._project_record(caller)
        if project is not None:
            registered_root = Path(project["project_root"])

    def attributable(path: Path, *, state: bool = False) -> tuple[Path | None, str]:
        """The resolved path if the caller may read it, else (None, why)."""
        own = registry.owner_of(path)
        if state:
            if own.status == PROTECTED and own.resolved == registry.state_root:
                return own.resolved, ""
            return None, (f"not the registry's state root {registry.state_root} "
                          f"({own.status}: {own.reason})")
        if caller is None:
            return None, "the calling project is unknown (no project_id, no inventory)"
        try:
            real = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None, "does not exist or cannot be resolved"
        if registered_root is not None and real == registered_root:
            return real, ""
        if _ours(own, caller) and own.resolved is not None:
            return own.resolved, ""
        return None, (f"not owned by project {caller!r}: {own.status} for "
                      f"{own.project_id!r} ({own.reason})")

    # -- state root ---------------------------------------------------------- #
    root, why = attributable(Path(state_root), state=True)
    if root is None:
        add("state_root", state_root, NOT_CHECKED, why)
    else:
        owner = root / _OWNER_FILE
        try:
            is_file = stat.S_ISREG(os.lstat(owner).st_mode)
            data = json.loads(owner.read_text(encoding="utf-8")) if is_file else None
        except (OSError, ValueError) as exc:
            add("state_root", owner, FAIL, f"{_OWNER_FILE} cannot be read: {exc}")
        else:
            if isinstance(data, dict):
                add("state_root", owner, PASS, f"{_OWNER_FILE} exists ({data.get('schema')})")
            else:
                add("state_root", owner, FAIL, f"{_OWNER_FILE} missing or not a JSON object")

    # -- project roots ------------------------------------------------------- #
    for raw in project_roots:
        root, why = attributable(Path(raw))
        if root is None:
            add("project_root", raw, NOT_CHECKED, why)
            continue
        if not os.path.lexists(root / ".git"):
            add("project_root", root, PASS, "no .git: not a repository, nothing to fsck")
            continue
        ok, detail = fsck(root)
        add("git_fsck", root, PASS if ok else FAIL, detail)

    # -- runs bases ---------------------------------------------------------- #
    inventoried: dict[Path, dict[str, Any]] = {}
    created_ns: int | None = record["created_ns"] if record is not None else None
    if record is not None:
        for entry in record["runs_bases"]:
            inventoried[Path(entry["path"])] = entry["runs"]
    asked: set[Path] = set()
    for base_raw in runs_bases:
        base, why = attributable(base_raw)
        if base is None:
            add("runs_base", base_raw, NOT_CHECKED, why)
            continue
        asked.add(base)
        runs = inventoried.get(base)
        if record is not None and runs is None:
            add("runs_base", base, FAIL, "the inventory does not cover this runs base")
        assert caller is not None
        try:
            names = _children(base)
        except OSError as exc:
            add("runs_base", base, FAIL, f"cannot be listed: {exc}")
            continue
        for run_id in sorted(set(runs or {}) - set(names)):
            add("run", base / run_id, FAIL, "inventoried run is missing")
        for name in names:
            child = base / name
            in_inventory = runs is not None and name in runs
            if not in_inventory and created_ns is not None:
                newer, detail = _newer_than(child, created_ns)
                if newer and not (name == sr._RUNS_BASE_ARENAS
                                  and _owned_by(registry.owner_of(child), caller)):
                    add("run", child, FAIL,
                        f"not in the inventory and {detail}; judged on metadata, not opened")
                    continue
            own = registry.owner_of(child)
            if not (own.status == OWNED and own.project_id == caller):
                if in_inventory:
                    add("run", child, FAIL,
                        f"inventoried run is no longer OWNED ({own.status}: {own.reason})")
                else:
                    skipped.append({"runs_base": str(base), "name": name,
                                    "status": own.status})
                    add("run", child, UNKNOWN_SKIPPED, f"{own.status}; not opened by this module")
                continue
            if not in_inventory and (
                    not _is_dir(child) or not os.path.lexists(child / _RUN_STATE)):
                add("run", child, PASS, "owned, not a run directory: nothing to parse")
                continue
            raw_state, why = _read_owned(registry, child / _RUN_STATE, caller)
            if raw_state is None:
                add("run", child, FAIL, why)
                continue
            problem = _parse_run_state(raw_state, name)
            if problem:
                add("run", child, FAIL, problem)
                continue
            detail = "state.json parses"
            if in_inventory:
                assert runs is not None
                if hashlib.sha256(raw_state).hexdigest() != runs[name].get("state_sha256"):
                    detail += "; changed since the inventory"
                else:
                    detail += "; unchanged since the inventory"
            add("run", child, PASS, detail)
    for base in sorted(set(inventoried) - asked):
        add("runs_base", base, NOT_CHECKED, "inventoried runs base not asked to check")

    failing = [c for c in checks if c["status"] in _FAILING]
    return {
        "result": FAIL if failing else PASS,
        "project_id": caller,
        "inventory": str(inventory) if inventory is not None else NO_INVENTORY,
        "inventory_age_seconds": age_seconds,
        "inventory_boot_id": record["boot_id"] if record is not None else None,
        "current_boot_id": current_boot,
        "checks": checks,
        "unknown_skipped": skipped,
        "failing": failing,
    }


__all__ = [
    "FAIL",
    "HOST_OK",
    "HOST_UNKNOWN",
    "INVENTORY_SCHEMA",
    "NOT_CHECKED",
    "NO_INVENTORY",
    "PASS",
    "PROFILE_NATIVE",
    "PROFILE_UNKNOWN",
    "PROFILE_WSL2",
    "UNKNOWN",
    "UNKNOWN_SKIPPED",
    "WSL_HOST_RECLAIM_REQUIRED",
    "HostConfig",
    "NativeLinuxAdapter",
    "StorageHostError",
    "WslHostAdapter",
    "adapter_for_profile",
    "detect_host_profile",
    "post_restart_checks",
    "pre_restart_inventory",
]
