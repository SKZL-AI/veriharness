"""Run leases: one durable, fenced claim per run (plan row P3-18, slice 1).

The master lease (`hoh.succession`) says who may drive a *project*. A run -- one node's execution -- has
today only the kernel's answer: `RunStore.process_marker` holds `.process.lock` for the run's lifetime, and
"is some process executing this run" is whatever that flock says. That answer is complete for one process on
one run directory and silent about everything a parallel scheduler has to know: *who* was granted the run,
*since when*, for how long it is allowed to be silent, and whether the grant that a returning process holds is
still the current one. A frozen process keeps its flock for ever; a run directory under `runs/` cannot be
asked from the project; and nothing stops a holder that was replaced from carrying on as if it were not.

This module is that record: one file per run, `<project dir>/run-leases/<run_id>.json`, with

* a **holder** (a session name) and the identity of its process (pid, start time, boot id, host, pid
  namespace), so a *lost* holder is told from a *live* one without waiting out the lease;
* a **generation** that only goes up: taking over a lease that was released or lost begins the next one, and
  a holder of an older generation can neither renew nor release (`StaleRunLease`) -- it is fenced, however
  fresh the object it holds;
* a **ttl**: a holder that is silent for longer than its ttl is `LOST` whatever its process says.

Judging a lease uses the same order as the master lease (`MasterAuthority.holder_state`): released; another
boot; a holder in this host and pid namespace whose pid is gone, or is now another process; past expiry;
otherwise live. A live holder is never displaced.

**What slice 1 was.** The record, a strict parse, a fail-closed read, and `acquire` / `renew` / `release`
under one short lock. `hoh.rundrive` is its caller (slice 2: `hoh run` takes, keeps and gives up the lease of the
run it drives). **Slice 3** adds the master epoch to the record; the listing of a directory's leases and the archive of
the old ended ones (moved aside, never removed -- this module renames nothing) are `hoh.leaseadmin`'s. The reclaim of
a lease stays what it was: a LOST or RELEASED lease is taken over by `acquire` and by nothing else. It is a record and a
protocol, not a trust boundary: a process of the same user can edit the file, and no same-user file protocol can stop that.

**The master epoch.** A `hoh run` that was started by a master says so (`--master-session`, `--master-epoch`); its
lease then carries that epoch and is spelled `SCHEMA_EPOCH` (`veriharness.run-lease/2`, one more key). A lease with no
epoch is spelled exactly as it always was (`SCHEMA`, `/1`) and every record written before this slice parses as before.
The epoch is part of the grant's identity (`_current`): a renewal never changes it.

**Fail closed.** A lease file that cannot be read exactly (`RunLeaseError`, a stable `reason`) is not "free":
`acquire` refuses over it, `renew` and `release` refuse, and `read` raises. The only absent lease is a name that
does not exist.

**Nothing is deleted.** A takeover first copies the replaced record to `<run_id>.json.g<generation>` and only
then replaces the current one; a copy that cannot be made refuses the takeover and leaves the lease. A lease
that ended stays, `released_at` set; the lock file stays.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import durable_io
from .store import RESERVED_RUN_IDS
from .succession import boot_id, pid_alive, pid_namespace, pid_start, utc_now

SCHEMA = "veriharness.run-lease/1"
"""The record of a lease that names no master epoch: the spelling every record had before slice 3."""

SCHEMA_EPOCH = "veriharness.run-lease/2"
"""The record of a lease that names the epoch of the master that started its holder: `/1` and one key, `master_epoch`."""

MAX_EPOCH = 10 ** 9
"""A master epoch is an integer from 1 to this."""

DIRNAME = "run-leases"
"""Directory under a project's own directory (`ProjectStore.run_leases`)."""

LOCKNAME = ".lock"
"""The short lock around every read-decide-write; created empty, never removed."""

MAX_BYTES = 4096
"""The largest record `read` or `parse` looks at (a real record is under a kilobyte)."""

MAX_TTL_SECONDS = 86400
"""A holder may be silent for at most a day; a lease is an integer ttl from 1 to this."""

MAX_GENERATION = 10 ** 9
"""A generation is an integer from 1 to this; taking over the last one is refused."""

MAX_LOCK_WAIT_SECONDS = 60
"""The longest `lock_wait_seconds` a handle may be built with."""

LOCK_WAIT_SECONDS = 5.0
"""How long `acquire`, `renew` and `release` wait for the short lock before `RunLeaseBusy`."""

LIVE, LOST, RELEASED, NONE = "LIVE", "LOST", "RELEASED", "NONE"
"""What `judge` and `status` answer."""

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TEXT = re.compile(r"[\x20-\x7e]{0,256}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FIELDS = ("run_id", "holder", "generation", "ttl_seconds", "acquired_at", "renewed_at", "expires_at",
           "holder_pid", "holder_boot", "holder_host", "holder_pidns", "holder_start",
           "released_at", "released_reason")


class RunLeaseError(ValueError):
    """A request or a record that is refused. `reason` is one of: `unreadable` (the file system or the
    record-file rules refuse the file or a directory), `oversize`, `malformed` (not one JSON object of ASCII
    text), `schema` (wrong or missing schema or keys), `invalid` (a field or a request breaks a rule),
    `noncanonical` (valid content in a spelling `render` would not produce), `conflict` (a parked copy that
    differs), `clock` (the clock reads earlier than the record's last renewal), `held` (a live holder),
    `stale` (the caller is no longer the holder), `busy` (the short lock was not free in time)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"run lease: {message}")
        self.reason = reason


class RunLeaseHeld(RunLeaseError):
    """A live holder has the run. The way past it is its own `release`, or its loss."""

    def __init__(self, message: str) -> None:
        super().__init__("held", message)


class StaleRunLease(RunLeaseError):
    """The caller's lease is not the current grant (another generation, another holder, released, or run
    out): it may not renew or release. Nothing was written."""

    def __init__(self, message: str) -> None:
        super().__init__("stale", message)


class RunLeaseBusy(RunLeaseError):
    """The short lock around the lease directory was not free within `lock_wait_seconds`."""

    def __init__(self, message: str) -> None:
        super().__init__("busy", message)


def _refuse(message: str) -> RunLeaseError:
    return RunLeaseError("invalid", message)


def _moment(value: object, what: str) -> datetime:
    if type(value) is not str or _TIME.fullmatch(value) is None:
        raise _refuse(f"{what} {value!r} is not YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, _FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise _refuse(f"{what} {value!r} is not a calendar time") from exc


def _spell(moment: datetime) -> str:
    # Not strftime: `%Y` is not zero-padded for years below 1000 on this platform.
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z")


def _plus(value: str, seconds: int) -> str:
    try:
        return _spell(_moment(value, "time") + timedelta(seconds=seconds))
    except OverflowError as exc:
        raise _refuse(f"{value!r} plus {seconds} s is beyond the calendar") from exc


def _name(value: object, what: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise _refuse(f"{what} {value!r} is not [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    return value


def _run_id(value: object) -> str:
    """A run id: the name grammar, and not a name the runs root keeps for itself."""
    if _name(value, "run_id") in RESERVED_RUN_IDS:
        raise _refuse(f"run_id {value!r} is a name the runs root keeps for itself")
    return value


def _ttl(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TTL_SECONDS:
        raise _refuse(f"ttl_seconds {value!r} is not an integer from 1 to {MAX_TTL_SECONDS}")
    return value


def _epoch(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_EPOCH:
        raise _refuse(f"master_epoch {value!r} is not an integer from 1 to {MAX_EPOCH}")
    return value


def _text(value: object, what: str) -> str:
    if type(value) is not str or _TEXT.fullmatch(value) is None:
        raise _refuse(f"{what} {value!r} is not printable ASCII of at most 256 characters")
    return value


def _ascii(value: object) -> str:
    """Environment text as the record's grammar wants it: escaped to printable ASCII, at most 256 characters."""
    return str(value).encode("unicode_escape").decode("ascii")[:256]


@dataclass(frozen=True)
class RunLease:
    """The grant of one run to one holder. Frozen: a lease changes by being replaced.

    `generation` counts the grants of this run (1 is the first); `renewed_at` / `expires_at` move on renewal
    and `expires_at` is always `renewed_at` plus `ttl_seconds`; `released_at` and `released_reason` are both set
    when the holder gave the run up and both empty otherwise. `master_epoch` is the epoch of the master that
    started the holder (slice 3), or None for a holder no master started.
    """

    run_id: str
    holder: str
    generation: int
    ttl_seconds: int
    acquired_at: str
    renewed_at: str
    expires_at: str
    holder_pid: int
    holder_boot: str
    holder_host: str
    holder_pidns: str
    holder_start: str
    released_at: str | None = None
    released_reason: str = ""
    master_epoch: int | None = None

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        _name(self.holder, "holder")
        if type(self.generation) is not int or not 1 <= self.generation <= MAX_GENERATION:
            raise _refuse(f"generation {self.generation!r} is not an integer from 1 to {MAX_GENERATION}")
        _ttl(self.ttl_seconds)
        acquired = _moment(self.acquired_at, "acquired_at")
        renewed = _moment(self.renewed_at, "renewed_at")
        if renewed < acquired:
            raise _refuse("renewed_at is earlier than acquired_at")
        if _moment(self.expires_at, "expires_at") != renewed + timedelta(seconds=self.ttl_seconds):
            raise _refuse("expires_at is not renewed_at plus ttl_seconds")
        if type(self.holder_pid) is not int or not 1 <= self.holder_pid <= 2 ** 31 - 1:
            raise _refuse(f"holder_pid {self.holder_pid!r} is not a process id")
        for what in ("holder_boot", "holder_host", "holder_pidns", "holder_start"):
            _text(getattr(self, what), what)
        if self.released_at is None:
            if self.released_reason != "":
                raise _refuse("released_reason is set on a lease that was not released")
        else:
            if _moment(self.released_at, "released_at") < acquired:
                raise _refuse("released_at is earlier than acquired_at")
            if not _text(self.released_reason, "released_reason").strip():
                raise _refuse("a released lease says why")
        if self.master_epoch is not None:
            _epoch(self.master_epoch)

    def as_dict(self) -> dict:
        document = {name: getattr(self, name) for name in _FIELDS}
        if self.master_epoch is not None:
            document["master_epoch"] = self.master_epoch
        return document


def render(lease: RunLease) -> bytes:
    """The one record for this lease: ASCII JSON, sorted keys, indent 1, one final newline. `SCHEMA` for a lease
    that names no master epoch, `SCHEMA_EPOCH` (and the key) for one that does."""
    if type(lease) is not RunLease:
        raise _refuse(f"{lease!r} is not a RunLease")
    document = {"schema": SCHEMA if lease.master_epoch is None else SCHEMA_EPOCH, **lease.as_dict()}
    return (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice in one object")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def parse(data: bytes) -> RunLease:
    """The lease a record's bytes describe, or a `RunLeaseError`. Pure: no file is touched.

    Accepted only if the bytes are exactly what `render` produces for the lease they describe.
    """
    if type(data) is not bytes:
        raise RunLeaseError("malformed", "a record is bytes")
    if len(data) > MAX_BYTES:
        raise RunLeaseError("oversize", f"{len(data)} bytes; at most {MAX_BYTES}")
    if not data.isascii():
        raise RunLeaseError("malformed", "a record is ASCII text")
    try:
        document = json.loads(data.decode("ascii"), object_pairs_hook=_no_duplicate_keys,
                              parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise RunLeaseError("malformed", f"not JSON: {exc}") from exc
    if type(document) is not dict:
        raise RunLeaseError("malformed", "a record is one JSON object")
    keys = _FIELDS + ("master_epoch",) if document.get("schema") == SCHEMA_EPOCH else _FIELDS
    if set(document) != {"schema", *keys} or document["schema"] not in (SCHEMA, SCHEMA_EPOCH):
        raise RunLeaseError("schema", f"a record is an object with exactly the keys schema ({SCHEMA}) "
                                      f"and {', '.join(_FIELDS)}, or schema ({SCHEMA_EPOCH}) and master_epoch "
                                      f"besides")
    lease = RunLease(**{name: document[name] for name in keys})
    if render(lease) != data:
        raise RunLeaseError("noncanonical", "valid content, but not the bytes render() produces for it")
    return lease


class RunLeases:
    """The run leases in one directory (for a project: `ProjectStore.run_leases()`).

    Every environmental fact is injectable -- the clock, this boot's id, this host, this pid, how to ask
    whether a pid is alive, its namespace and its start time -- because a lost holder is exactly a change of
    those facts, and a test has to be able to say "the machine rebooted" without rebooting it. The defaults
    are the real ones (`hoh.succession`).
    """

    def __init__(self, directory: str | os.PathLike, *,
                 clock: Callable[[], str] = utc_now,
                 boot: Callable[[], str] = boot_id,
                 host: str | None = None,
                 pid: int | None = None,
                 alive: Callable[[int], bool] = pid_alive,
                 pidns: Callable[[], str] = pid_namespace,
                 start: Callable[[int], str] = pid_start,
                 lock_wait_seconds: float = LOCK_WAIT_SECONDS) -> None:
        if type(lock_wait_seconds) not in (int, float) or not 0 <= lock_wait_seconds <= MAX_LOCK_WAIT_SECONDS:
            raise _refuse(f"lock_wait_seconds {lock_wait_seconds!r} is not a number from 0 to "
                          f"{MAX_LOCK_WAIT_SECONDS}")
        self.dir = Path(directory)
        if "\x00" in str(self.dir):
            raise _refuse(f"directory {str(self.dir)!r} has a NUL in it")
        self.clock = clock
        self.boot = boot
        if host is None:
            host = socket.gethostname()
        self.host = _ascii(host)
        self.pid = pid if pid is not None else os.getpid()
        self.alive = alive
        self.pidns = pidns
        self.start = start
        self.lock_wait_seconds = lock_wait_seconds

    # --- files ----------------------------------------------------------------------------------

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{_run_id(run_id)}.json"

    def _now(self) -> str:
        value = self.clock()
        _moment(value, "the clock's time")
        return value

    def _identity(self) -> dict:
        """This process as a lease records it, asked of the environment **before** the directory or the lock
        is made: a pid that is not a pid is refused, and an injected `boot`, `pidns` or `start` that raises
        raises here (its exception is the caller's), with nothing created."""
        pid = self.pid
        if type(pid) is not int or not 1 <= pid <= 2 ** 31 - 1:
            raise _refuse(f"holder_pid {pid!r} is not a process id")
        return dict(holder_pid=pid, holder_boot=_ascii(self.boot()), holder_host=self.host,
                    holder_pidns=_ascii(self.pidns()), holder_start=_ascii(self.start(pid)))

    def _directory(self) -> None:
        """The directory exists and is a directory of its own (not a link, not a file), or `unreadable`."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            status = os.lstat(self.dir)
        except OSError as exc:
            if isinstance(exc, durable_io.DiskFullError):
                raise
            raise RunLeaseError("unreadable", f"{self.dir}: {exc}") from exc
        if not stat.S_ISDIR(status.st_mode):
            raise RunLeaseError("unreadable", f"{self.dir} is not a directory of its own")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """The short lock around read-decide-write: `.lock` in the directory, taken without blocking and
        retried for `lock_wait_seconds`. Held for microseconds, never across a run."""
        self._directory()
        lock = self.dir / LOCKNAME
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            try:
                fd = os.open(lock, flags)
            except FileNotFoundError:
                try:
                    durable_io.durable_create(lock, b"", mode=0o600)
                except FileExistsError:
                    pass
                fd = os.open(lock, flags)
        except OSError as exc:
            if isinstance(exc, durable_io.DiskFullError):
                raise
            raise RunLeaseError("unreadable", f"{lock}: {exc}") from exc
        try:
            status = durable_io.settled_stat(fd)    # a creator of the lock may be between its link and its drop
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise RunLeaseError("unreadable", f"{lock} is not a regular file with one name")
            deadline = time.monotonic() + self.lock_wait_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RunLeaseBusy(f"{lock} was not free within {self.lock_wait_seconds} s") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def read(self, run_id: str) -> RunLease | None:
        """The current lease of `run_id`; None only when no such file exists. Every other failure is a
        `RunLeaseError` -- a record that cannot be read is not a free run."""
        path = self._path(run_id)
        try:
            directory = os.lstat(self.dir)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RunLeaseError("unreadable", f"{self.dir}: {exc}") from exc
        if not stat.S_ISDIR(directory.st_mode):
            raise RunLeaseError("unreadable", f"{self.dir} is not a directory of its own")
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RunLeaseError("unreadable", f"{path}: {exc}") from exc
        if status.st_size > MAX_BYTES:
            raise RunLeaseError("oversize", f"{path} is {status.st_size} bytes; at most {MAX_BYTES}")
        try:
            data = durable_io.read_single_link(path)
        except OSError as exc:
            raise RunLeaseError("unreadable", f"{path}: {exc}") from exc
        if data is None:
            raise RunLeaseError("unreadable", f"{path} vanished while it was read")
        lease = parse(data)
        if lease.run_id != run_id:
            raise _refuse(f"{path} holds the lease of run {lease.run_id!r}, not {run_id!r}")
        return lease

    def _write(self, lease: RunLease) -> None:
        data = render(lease)
        parse(data)
        durable_io.durable_write(self._path(lease.run_id), data, mode=0o600)

    def _park(self, lease: RunLease) -> None:
        """Copy the record about to be replaced to `<run_id>.json.g<generation>`; an existing copy must be
        the same bytes (a takeover that died after parking, retried)."""
        target = self.dir / f"{lease.run_id}.json.g{lease.generation}"
        data = render(lease)
        try:
            durable_io.durable_create(target, data, mode=0o600)
        except FileExistsError:
            try:
                size = os.lstat(target).st_size
                if size > MAX_BYTES:        # as `read`: a copy longer than a record is not one, and is not read
                    raise RunLeaseError("oversize", f"{target} is {size} bytes; at most {MAX_BYTES}")
                existing = durable_io.read_single_link(target)
            except OSError as exc:
                raise RunLeaseError("unreadable", f"{target}: {exc}") from exc
            if existing != data:
                raise RunLeaseError("conflict", f"{target} exists and is not the record being replaced")

    # --- judging --------------------------------------------------------------------------------

    def judge(self, lease: RunLease) -> tuple[str, str]:
        """`(LIVE | LOST | RELEASED, why)` for a lease, now, in this process's view of the machine."""
        if type(lease) is not RunLease:
            raise _refuse(f"{lease!r} is not a RunLease")
        return self._judge(lease, self._now())

    def _judge(self, lease: RunLease, now: str) -> tuple[str, str]:
        if lease.released_at is not None:
            return RELEASED, (f"released by {lease.holder!r} at {lease.released_at}: "
                              f"{lease.released_reason}")
        here = _ascii(self.boot())
        # A boot id names one machine's boot. Another machine has another boot id whatever its state, so a holder in
        # another host is not "lost on a reboot" by comparing it with this machine's (P3-18 slice 3, review B1); its
        # own ttl and renewals decide, below.
        if lease.holder_host == self.host and lease.holder_boot and here and lease.holder_boot != here:
            return LOST, (f"holder {lease.holder!r} was on boot {lease.holder_boot[:8]}, this is boot "
                          f"{here[:8]}: the machine restarted")
        same_process_space = (lease.holder_host == self.host and lease.holder_pid
                              and (not lease.holder_boot or lease.holder_boot == here)
                              and lease.holder_pidns and lease.holder_pidns == _ascii(self.pidns()))
        if same_process_space:
            if not self.alive(lease.holder_pid):
                return LOST, f"holder {lease.holder!r} (pid {lease.holder_pid}) no longer exists"
            started = _ascii(self.start(lease.holder_pid))
            if lease.holder_start and started and started != lease.holder_start:
                return LOST, (f"holder {lease.holder!r}: pid {lease.holder_pid} now belongs to another "
                              f"process")
        if lease.expires_at <= now:
            return LOST, f"lease of {lease.holder!r} expired at {lease.expires_at}"
        return LIVE, (f"held by {lease.holder!r} (generation {lease.generation}, pid {lease.holder_pid}) "
                      f"until {lease.expires_at}")

    def status(self, run_id: str) -> tuple[str, str]:
        """`(NONE | LIVE | LOST | RELEASED, why)` for a run; `RunLeaseError` when its record cannot be read."""
        lease = self.read(run_id)
        if lease is None:
            return NONE, "no lease has ever been written for this run"
        return self.judge(lease)

    # --- the protocol ---------------------------------------------------------------------------

    def acquire(self, run_id: str, holder: str, *, ttl_seconds: int, master_epoch: int | None = None) -> RunLease:
        """Take the run for `holder`: generation 1 for a run nobody held, otherwise the next generation over
        a lease that is released or lost. A live holder, including this very holder, is `RunLeaseHeld`; a
        record that cannot be read is refused, never taken over. `master_epoch` is the epoch of the master that
        started `holder` (None: none did); it is part of the grant."""
        _run_id(run_id)
        _name(holder, "holder")
        _ttl(ttl_seconds)
        if master_epoch is not None:
            _epoch(master_epoch)
        _plus(self._now(), ttl_seconds)   # a clock that is not a time, or at the calendar's end, is refused
                                          # before the directory or the lock exists
        identity = self._identity()       # and so is an environment that cannot describe this process
        with self._locked():
            now = self._now()
            current = self.read(run_id)
            generation = 1
            replaced = None
            if current is not None:
                state, why = self._judge(current, now)
                if state == LIVE:
                    raise RunLeaseHeld(f"cannot acquire {run_id!r}: {why}")
                generation = current.generation + 1
                if generation > MAX_GENERATION:
                    raise _refuse(f"generation {generation} is beyond {MAX_GENERATION}")
                replaced = current
            # The new lease is built, and so validated, before anything is parked: a refusal here (a
            # pid that is not a pid, a time beyond the calendar) leaves no copy of the old record behind.
            lease = RunLease(
                run_id=run_id, holder=holder, generation=generation, ttl_seconds=ttl_seconds,
                acquired_at=now, renewed_at=now, expires_at=_plus(now, ttl_seconds),
                master_epoch=master_epoch, **identity)
            if replaced is not None:
                self._park(replaced)
            self._write(lease)
            return lease

    def _current(self, lease: RunLease) -> RunLease:
        """The lease on disk, which must be the very grant `lease` is (same run, holder, generation and
        process, not released), or `StaleRunLease`."""
        if type(lease) is not RunLease:
            raise _refuse(f"{lease!r} is not a RunLease")
        current = self.read(lease.run_id)
        if current is None:
            raise StaleRunLease(f"run {lease.run_id!r} has no lease on disk")
        same = all(getattr(current, name) == getattr(lease, name) for name in (
            "holder", "generation", "ttl_seconds", "acquired_at", "holder_pid", "holder_boot",
            "holder_host", "holder_pidns", "holder_start", "master_epoch"))
        if not same:
            raise StaleRunLease(
                f"run {lease.run_id!r} is held by {current.holder!r} at generation {current.generation}, "
                f"not by {lease.holder!r} at generation {lease.generation}")
        if current.released_at is not None:
            raise StaleRunLease(f"run {lease.run_id!r} was released at {current.released_at}")
        return current

    def _require_a_record(self, lease: RunLease) -> RunLease:
        """What `renew` and `release` decide first, before the directory or the lock is made and before the
        clock is read: the argument is a `RunLease` and the run has a record. A refusal here creates nothing."""
        if type(lease) is not RunLease:
            raise _refuse(f"{lease!r} is not a RunLease")
        record = self.read(lease.run_id)
        if record is None:
            raise StaleRunLease(f"run {lease.run_id!r} has no lease on disk")
        return record

    def renew(self, lease: RunLease) -> RunLease:
        """Extend the lease by its ttl from now. Only the holder of the current grant may, and only before it
        has run out: a lease that expired must be acquired again, which begins the next generation."""
        record = self._require_a_record(lease)
        _plus(self._now(), record.ttl_seconds)    # the clock, and the sum, are refused before the lock
        with self._locked():
            current = self._current(lease)
            now = self._now()
            if now < current.renewed_at:
                raise RunLeaseError("clock", f"the clock reads {now}, before the last renewal at "
                                             f"{current.renewed_at}")
            if current.expires_at <= now:
                raise StaleRunLease(f"the lease of run {lease.run_id!r} expired at {current.expires_at}; "
                                    f"acquire it again")
            renewed = replace(current, renewed_at=now, expires_at=_plus(now, current.ttl_seconds))
            self._write(renewed)
            return renewed

    def release(self, lease: RunLease, *, reason: str) -> RunLease:
        """Give the run up, with a reason. Only the holder of the current grant may (also after the lease
        ran out, if nobody took it over). The record stays, `released_at` set."""
        _text(reason, "reason")
        if not reason.strip():
            raise _refuse("a release says why")
        self._require_a_record(lease)
        self._now()                               # a release adds no seconds: only the time is checked
        with self._locked():
            current = self._current(lease)
            now = self._now()
            if now < current.renewed_at:
                raise RunLeaseError("clock", f"the clock reads {now}, before the last renewal at "
                                             f"{current.renewed_at}")
            released = replace(current, released_at=now, released_reason=reason)
            self._write(released)
            return released
