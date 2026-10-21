"""The run leases of a directory, listed and archived (plan row P3-18, slice 3).

`hoh.runlease` is one lease and its protocol; `hoh.rundrive` is the driver that holds one. This module is for the
operator and the readers of a whole directory: **`snapshot`** (every lease of a directory, judged now, read-only) and
**`archive_ended`** (the leases that ended long ago, with their parked copies, moved aside -- never removed). It is
reached from `hoh project leases [--archive]` (`rundrive.listing` / `rundrive.archive`).

**Why it is not in `runlease.py`.** That module is reviewed to remove nothing and to rename nothing
(`tests/test_runlease.py`, "what the module may do"); an archive is a rename. Here the rename is one named call, classified
in `tests/test_durable_writers.py`, and it happens only after the manifest is durable.

**Fail closed.** A lease file that cannot be read exactly is a row of state `UNREADABLE` in a listing and is left where it
is by an archive; so is a run one of whose parked copies cannot be read exactly (it is not a copy of that run and that
generation). A directory that cannot be listed, holds more than `MAX_SNAPSHOT` lease files or is a link or a file is a
`RunLeaseError`. Nothing is made by a listing; an archive makes nothing when there is nothing to move.

**Nothing is removed.** A file is renamed into a directory made for this pass, `<lease dir>-archive/<stamp>/`, in the
same file system (a rename that crosses one is refused, never turned into a copy and a removal). The manifest (name, run,
generation, size, sha256 of every file the pass will move) is written and synced before the first rename; every moved file
is read back and compared with its digest; `COMPLETE.json` is written after the last. A pass directory with a manifest and
no `COMPLETE.json` is a pass that stopped; the next pass moves what is left into a directory of its own and touches that
one no more.

**The lock is held for the budget, not for the directory.** A pass plans without the lock (reads runs in name order until
`MAX_ARCHIVE_FILES` files are planned) and takes the lock only to read the planned runs again, write the manifest and rename;
so a directory above the listing's `MAX_SNAPSHOT` ceiling -- the state the archive is for -- is archived 512 files at a time and
a concurrent `hoh run`'s `acquire` waits for at most those. The clock is this host's: like `RunLeases.judge`, a pass trusts it,
and a clock ahead by more than `older_than_seconds` plus a ttl would archive the record of a holder that is still renewing
(its next `renew` is then a `StaleRunLease`, visible, and the run stops iterating); the default age is a week.

**What an archive costs a run.** The parked copies move first and the current record last, so a pass that stops half way
leaves every run it did not finish with its current record -- hence its generation -- in place. A run whose whole history was
archived begins again at generation 1 if it is driven again; the manifest of its archive has the generations it had, and the
fence does not rest on the number: a grant is fenced by its whole identity (holder, generation, acquired_at, pid, boot, host,
pid namespace, start time, master epoch), so the holder of an archived grant cannot renew or release a later one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import durable_io
from .runlease import (
    LOST, MAX_BYTES, RELEASED, RunLease, RunLeaseError, RunLeases, _moment, _refuse, _spell, parse, render,
)

UNREADABLE = "UNREADABLE"
"""What `snapshot` says of a lease file that cannot be read exactly: not a free run, and not a judgement either."""

MAX_SNAPSHOT = 16384
"""The most lease files `snapshot` and `archive_ended` look at (the broker's own ceiling, `capacitycheck.MAX_SCANNED`)."""

MIN_ARCHIVE_AGE_SECONDS = 3600
"""A lease that ended less than an hour ago is never archived, whatever a caller asks."""

MAX_ARCHIVE_AGE_SECONDS = 3650 * 86400
"""The oldest `older_than_seconds` a caller may ask (ten years: a number a calendar can subtract)."""

MAX_ARCHIVE_NAMES = 1 << 20
"""The most names of a lease directory an archive pass lists (it reads only until its plan is full). Above the listing's
own `MAX_SNAPSHOT` ceiling an archive still works -- that is exactly the state it is for -- up to this many names; more is
refused."""

MAX_ARCHIVE_FILES = 512
"""The most files one `archive_ended` moves: the short lock is held while they move, and a pass that held it for seconds
would refuse the `hoh run` waiting for it (`RunLeaseBusy`). What is left is left for the next pass."""

ARCHIVE_SUFFIX = "-archive"
"""The directory next to the lease directory that the passes go to: `<project dir>/run-leases-archive/<stamp>/`."""

MANIFEST = "MANIFEST.json"
COMPLETE = "COMPLETE.json"
ARCHIVE_SCHEMA = "veriharness.run-lease-archive/1"

_PARKED = re.compile(r"(.+)\.json\.g([0-9]{1,10})")


@dataclass(frozen=True)
class LeaseRow:
    """One lease file as `snapshot` read it: `state` is `RunLeases.judge`'s answer (`LIVE`, `LOST`, `RELEASED`) or
    `UNREADABLE` (then `lease` is None and `why` is the refusal); `parked` counts the copies of replaced records next to it."""

    run_id: str
    state: str
    why: str
    lease: RunLease | None
    parked: int

    @property
    def reclaimable(self) -> bool:
        """A lease nobody holds: the next `acquire` takes it over (and parks the record it replaces). A LIVE lease is
        not, and neither is one that cannot be read."""
        return self.state in (LOST, RELEASED)


@dataclass(frozen=True)
class ArchiveResult:
    """What one `archive_ended` pass did. `directory` is None when nothing was moved; `moved` are the file names
    (current records and parked copies) now under it; `runs` the runs they belong to; `skipped` is
    `(run_id, why)` for a run whose files were left where they are; `deferred` are runs left for the next pass."""

    directory: Path | None
    moved: tuple[str, ...]
    runs: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    deferred: tuple[str, ...]


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _listing(leases: RunLeases, *, archive: bool = False) -> tuple[list[str], dict[str, list[int]]]:
    """`(runs with a record file, parked generations per run)` -- both empty for a directory that does not exist; a
    directory that is a link or a file, or holds more files than `MAX_SNAPSHOT` allows, is refused (for an `archive`
    listing the bound is `MAX_ARCHIVE_NAMES` names and there is none on the files: the listing reads no file). Names that
    are neither a record (`<run>.json`), a parked copy (`<run>.json.g<N>`) nor the lock are not looked at."""
    try:
        status = os.lstat(leases.dir)
    except FileNotFoundError:
        return [], {}
    except OSError as exc:
        raise RunLeaseError("unreadable", f"{leases.dir}: {exc}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise RunLeaseError("unreadable", f"{leases.dir} is not a directory of its own")
    try:
        names = sorted(os.listdir(leases.dir))
    except OSError as exc:
        raise RunLeaseError("unreadable", f"{leases.dir}: {exc}") from exc
    ceiling = MAX_ARCHIVE_NAMES if archive else 2 * MAX_SNAPSHOT
    if len(names) > ceiling:
        raise RunLeaseError("invalid", f"{leases.dir} holds {len(names)} names; at most {ceiling} are read")
    current: list[str] = []
    parked: dict[str, list[int]] = {}
    for name in names:
        hit = _PARKED.fullmatch(name)
        if hit is not None:
            parked.setdefault(hit.group(1), []).append(int(hit.group(2)))
        elif name.endswith(".json"):
            current.append(name[:-len(".json")])
    if len(current) > MAX_SNAPSHOT and not archive:
        raise RunLeaseError("invalid", f"{len(current)} lease files; at most {MAX_SNAPSHOT} are read "
                                       f"(archive the ended ones)")
    return current, parked


def snapshot(leases: RunLeases) -> list[LeaseRow]:
    """Every lease of the directory, judged now, sorted by run id. Read-only: nothing is created, locked or written. A
    file that cannot be read exactly is a row of state `UNREADABLE` (never skipped, never free); a directory that cannot
    be listed is a `RunLeaseError`."""
    names, parked = _listing(leases)
    now = leases._now()
    rows: list[LeaseRow] = []
    for name in names:
        count = len(parked.get(name, ()))
        try:
            lease = leases.read(name)
        except RunLeaseError as exc:
            rows.append(LeaseRow(name, UNREADABLE, f"{exc.reason}: {exc}"[:300], None, count))
            continue
        if lease is None:
            rows.append(LeaseRow(name, UNREADABLE, "the record vanished while it was read", None, count))
            continue
        state, why = leases._judge(lease, now)
        rows.append(LeaseRow(name, state, why, lease, count))
    return rows


def _parked_bytes(leases: RunLeases, run_id: str, generation: int) -> bytes:
    """The bytes of `<run_id>.json.g<generation>`, which must be a record of that very run and generation."""
    path = leases.dir / f"{run_id}.json.g{generation}"
    try:
        size = os.lstat(path).st_size
        if size > MAX_BYTES:
            raise RunLeaseError("oversize", f"{path} is {size} bytes; at most {MAX_BYTES}")
        data = durable_io.read_single_link(path)
    except OSError as exc:
        raise RunLeaseError("unreadable", f"{path}: {exc}") from exc
    if data is None:
        raise RunLeaseError("unreadable", f"{path} vanished while it was read")
    copy = parse(data)
    if copy.run_id != run_id or copy.generation != generation:
        raise _refuse(f"{path} holds generation {copy.generation} of run {copy.run_id!r}")
    return data


def _run_entries(leases: RunLeases, name: str, parked: dict[str, list[int]], now: str,
                 cutoff: str) -> tuple[list[dict] | None, str | None]:
    """`(entries, None)` for a run whose current lease ended at or before `cutoff`: one manifest entry per parked copy
    (ascending) and the current record last; `(None, None)` for a run that is not eligible (LIVE, too young, no record);
    `(None, why)` for a run that must stay as it is (its record or one of its parked copies cannot be read exactly)."""
    try:
        lease = leases.read(name)
    except RunLeaseError as exc:
        return None, f"{exc.reason}: its record cannot be read"
    if lease is None:
        return None, None
    state, _why = leases._judge(lease, now)
    # A LIVE lease is never old enough: it is not released and its expiry is after `now`, hence after the cutoff. (A
    # holder that died before its expiry is LOST, and ended -- for this purpose -- at its expiry.)
    ended = lease.released_at if lease.released_at is not None else lease.expires_at
    if ended > cutoff:
        return None, None
    entries: list[dict] = []
    try:
        for generation in sorted(parked.get(name, ())):
            data = _parked_bytes(leases, name, generation)
            entries.append({"name": f"{name}.json.g{generation}", "kind": "parked", "run_id": name,
                            "generation": generation, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    except RunLeaseError as exc:
        return None, f"{exc.reason}: a parked copy cannot be read exactly"
    data = render(lease)
    entries.append({"name": f"{name}.json", "kind": "current", "run_id": name, "state": state,
                    "generation": lease.generation, "ended_at": ended, "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()})
    return entries, None


def _cutoff(now: str, older_than_seconds: int) -> str:
    try:
        return _spell(_moment(now, "the clock's time") - timedelta(seconds=older_than_seconds))
    except OverflowError as exc:
        raise _refuse(f"{now!r} minus {older_than_seconds} s is before the calendar") from exc


def archive_ended(leases: RunLeases, *, older_than_seconds: int) -> ArchiveResult:
    """Move the leases that ended at least `older_than_seconds` ago, with their parked copies, out of the directory and
    under `<directory>-archive/<stamp>/`, with a manifest. **Nothing is removed** (see the module text).

    A lease is archived only if it is `RELEASED` (ended when it was released) or `LOST` (ended when it expired) and its
    end is not later than the cutoff; a `LIVE` one, a record that cannot be read and a run whose parked copies cannot be
    read exactly are left where they are (the last two are in `skipped`).

    **Two phases, so the short lock is held for the budget and not for the directory.** (1) *Without the lock*, a plan: the
    names are listed (at most `MAX_ARCHIVE_NAMES`; no bound on the lease files, so a directory above the listing's
    `MAX_SNAPSHOT` ceiling -- the state an archive is for -- can be archived) and runs are read in name order until the plan
    holds `MAX_ARCHIVE_FILES` files; the first eligible run that does not fit is `deferred` (more may follow: run the pass
    again) and ends the scan, as does a plan with `MAX_ARCHIVE_FILES` skipped runs; a run with more files than a pass
    moves is `skipped`. (2) *Under the lock* (so no `acquire`, `renew` or `release` interleaves), only the planned runs
    are read again and judged again -- a run that is no longer eligible (it was taken over meanwhile) is `skipped`, never
    moved --, the manifest is written, the files are renamed. One pass, no retry: a failure raises with what had moved,
    and the files that did not move are where they were."""
    if type(older_than_seconds) is not int or not (
            MIN_ARCHIVE_AGE_SECONDS <= older_than_seconds <= MAX_ARCHIVE_AGE_SECONDS):
        raise _refuse(f"older_than_seconds {older_than_seconds!r} is not an integer from "
                      f"{MIN_ARCHIVE_AGE_SECONDS} to {MAX_ARCHIVE_AGE_SECONDS}")
    names, parked = _listing(leases, archive=True)        # nothing is made, locked or written until there is a plan
    cutoff = _cutoff(leases._now(), older_than_seconds)
    now_plan = leases._now()
    planned: list[str] = []
    skipped: list[tuple[str, str]] = []
    deferred: list[str] = []
    budget = MAX_ARCHIVE_FILES
    for name in names:
        if budget <= 0 or len(skipped) >= MAX_ARCHIVE_FILES:
            break
        entries, why = _run_entries(leases, name, parked, now_plan, cutoff)
        if why is not None:
            skipped.append((name, why))
        elif entries is None:
            continue
        elif len(entries) > MAX_ARCHIVE_FILES:
            skipped.append((name, f"{len(entries)} files; a pass moves at most {MAX_ARCHIVE_FILES}"))
        elif len(entries) > budget:
            deferred.append(name)
            break
        else:
            budget -= len(entries)
            planned.append(name)
    if not planned:
        return ArchiveResult(None, (), (), tuple(skipped), tuple(deferred))
    with leases._locked():
        names, parked = _listing(leases, archive=True)
        now = leases._now()
        cutoff = _cutoff(now, older_than_seconds)
        plan: list[tuple[str, list[dict]]] = []
        for name in planned:
            entries, why = _run_entries(leases, name, parked, now, cutoff)
            if entries is None:
                skipped.append((name, why or "no longer eligible: the lease changed since the plan"))
            else:
                plan.append((name, entries))
        if not plan:
            return ArchiveResult(None, (), (), tuple(skipped), tuple(deferred))
        root = leases.dir.with_name(leases.dir.name + ARCHIVE_SUFFIX)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                raise RunLeaseError("unreadable", f"{root} is not a directory of its own")
            target = root / (now.replace(":", "").replace("-", "") + "-" + secrets.token_hex(3))
            target.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RunLeaseError("conflict", f"{exc.filename} exists") from exc
        except OSError as exc:
            if isinstance(exc, durable_io.DiskFullError):
                raise
            raise RunLeaseError("unreadable", f"{root}: {exc}") from exc
        manifest = {"schema": ARCHIVE_SCHEMA, "archived_at": now, "older_than_seconds": older_than_seconds,
                    "source": str(leases.dir), "files": [e for _run, entries in plan for e in entries]}
        try:
            durable_io.durable_create(target / MANIFEST, json.dumps(manifest, sort_keys=True, indent=1) + "\n",
                                      mode=0o600)
        except OSError as exc:
            if isinstance(exc, durable_io.DiskFullError):
                raise
            raise RunLeaseError("unreadable", f"{target / MANIFEST}: {exc}") from exc
        moved: list[str] = []
        try:
            for _run, entries in plan:
                for entry in entries:
                    os.rename(leases.dir / entry["name"], target / entry["name"])
                    moved.append(entry["name"])
                    if hashlib.sha256((target / entry["name"]).read_bytes()).hexdigest() != entry["sha256"]:
                        raise RunLeaseError("conflict", f"{entry['name']} reads differently after it was moved")
            _sync_directory(leases.dir)
            _sync_directory(target)
            durable_io.durable_create(
                target / COMPLETE, json.dumps({"schema": ARCHIVE_SCHEMA, "moved": len(moved)}, sort_keys=True,
                                              indent=1) + "\n", mode=0o600)
        except (OSError, RunLeaseError) as exc:
            if isinstance(exc, durable_io.DiskFullError):
                raise
            raise RunLeaseError(getattr(exc, "reason", "unreadable"),
                                f"archive {target}: {exc}; {len(moved)} file(s) were moved (see {MANIFEST})") from exc
        return ArchiveResult(target, tuple(moved), tuple(run for run, _e in plan), tuple(skipped), tuple(deferred))
