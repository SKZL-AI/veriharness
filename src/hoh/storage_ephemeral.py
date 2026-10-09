"""Registered ephemeral temporary roots (P2-42, amendment A3).

*Temporary execution state is not durable evidence.* Every temporary workspace
VeriHarness creates for a process, a test, a run, a reviewer, a sandbox or a
verification lives under a **registered temporary root** of its project -- the
registry's scratch kind ``tmp``, ``<state_root>/projects/<p>/tmp/<session>/`` --
with the identity of its creator and an active lease for the creator's
process lifetime, is admitted like any other write-heavy operation, and is
cleaned by the operation that created it.

What this module owns (P2-42 and nothing of P5-35)
--------------------------------------------------

* `EphemeralRoots.create` / `.workspace` -- the one way a workspace comes to
  exist. In this order, failing closed at every step:

  1. the arguments are valid (`ValueError`, nothing created);
  2. the session holds a live registry lease **and it is this process's**
     (the lease's pid is ``getpid()``): another process's session is refused;
  3. the write is *admitted* (`StorageAdmission.reserve`, operation kind
     ``tmp_workspace``); a refusal raises `TempAdmissionRefused`, an admission that
     cannot answer `TempAdmissionUnavailable`, and nothing is created, not even the root;
  4. the root is registered (`StorageRegistry.scratch_dir(p, s, "tmp")`: the
     ownership record and index entry are written before the path is
     returned);
  5. the workspace directory is created empty and its **record** is written
     beside it, ``<root>/.<workspace>.vh-ws.json`` (state ``REGISTERED``, then
     ``ACTIVE``), *before* the caller can put anything in it. The record names
     the project, session, run, creator, purpose, the creator's process lease
     (pid, start time, boot id) and the workspace's inode;
  6. a failure after the directory exists removes it again (it is the
     creator's own, empty), discards the record this call wrote for it, and
     releases the reservation.

* `EphemeralRoots.release` -- the creator's own cleanup of the workspace this
  process created. This is the first of the two removals amendment A3 allows
  besides ``VERIFIED_RELOCATION`` (the second, the steward's reclaim of a proven
  orphan, is P5-35 and is not here). It calls no `rmtree` on a tree nobody
  checked: the root is opened without following links and must be the directory
  it was registered as (a root renamed away and replaced, or overmounted, is
  refused -- its missing name is not "gone"), the workspace is compared with the
  inode the record registered *by name relative to that descriptor* and again on
  the descriptor it opens, and then **every directory at every depth** is opened
  relative to its parent without following links and checked before anything
  below it is listed or removed -- same device **and the same mount**
  (``mnt_id`` from ``/proc/self/fdinfo/<fd>``: a bind mount of a directory of
  the same filesystem has the same ``st_dev``, so the device alone would walk
  straight into foreign data), listable, and the object the listing named. A
  mount identity that cannot be read fails closed. This walk runs to the end
  **before the first entry is removed**; it counts the bytes (so nothing outside
  the verified tree is ever measured) and records a **snapshot** -- for every
  directory its inode, for everything else its device and inode. The removal
  then goes entry by entry with ``unlinkat``/``rmdir`` relative to the directory
  descriptors, never by path, and **acts on what the walk saw and nothing
  else**: each child is opened relative to its parent without following links
  and must be the same inode, device and mount as in the snapshot before it is
  entered; each file must be the same inode before it is unlinked; a name the
  walk did not list, or one that changed between directory and file, is refused.
  Any such mismatch stops the removal there and leaves the rest -- a same-uid
  process that renames a foreign directory into a listed (or a new) name never
  gets it entered or emptied. A workspace that is not the registered inode, or
  has a mount, an unlistable directory, an unreadable mount identity or a
  nesting deeper than the interpreter can walk (``RecursionError``) anywhere
  inside, is left untouched and the record says so. Hard links are unlinked,
  never written through: the other name keeps its content; a file bind-mounted
  over a file is counted at its size and not removed (``unlink`` of a mount
  point fails). **Bytes booked:** 0 when nothing was verified; otherwise what the
  walk counted, also when the removal stopped or an entry stays (it may still be
  there). The window left is one name: a same-uid process that replaces *that
  name* between the last check on it and its ``unlinkat``/``rmdir`` makes the
  creator remove that one name (a file's name, or an empty directory) -- never a
  directory it did not verify, since nothing is entered without its identity
  check. The removal calls are confined to `_remove_tree`, `_clean_workspace`
  and `_discard_own_record` (``test_ephemeral_module_has_one_confined_removal``
  scans for them).

* `read_record`, `record_path`, `creator_state` -- what a later reclaim
  (P5-35) and the tests read: the record beside a workspace, and whether the
  process lease in it is proven dead.

* `RunSession` / `name_for_run` / `project_for_run` -- what ``hoh run`` does with the above (the
  controller wiring): one registry session per run process, named from the run
  id; the project's ``tmp`` root for it registered before the run starts; the
  `EphemeralRoots` the run's controller creates every acceptance-check scratch
  and the iteration's workspace from; the lease ended on every way out of the
  run. A project that is not registered has no root (`cli._open_ephemeral`
  says so and the runner's sibling scratch is used, as before).

Lifecycle
---------

Stored in the record: ``REGISTERED -> ACTIVE -> RELEASED``. A workspace whose
creator died before releasing it stays ``ACTIVE`` or ``REGISTERED`` with a dead
creator (`creator_state`): nothing in this module judges, promotes or removes
it. P5-35 adds the judgements (orphan candidate, proven orphan), the reclaim
and the record fields it needs.

**Trust boundary.** A record is proof only inside a root the registry created
and indexed; a process of the same uid that can write the state root can edit a
record, as it could edit ``SCRATCH_INDEX.json``. This module does not defend
against that and neither does the registry.

Nothing here reads an mtime, lists a directory outside its own roots or touches
a path it did not create.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from hoh.durable_io import durable_create, durable_write
from hoh.storage_registry import StorageOwnershipError, _read_record

TMP_KIND = "tmp"
RECORD_SCHEMA = "veriharness.ephemeral-workspace/1"

REGISTERED = "REGISTERED"
ACTIVE = "ACTIVE"
RELEASED = "RELEASED"
STORED_STATES = (REGISTERED, ACTIVE, RELEASED)

LIVE = "LIVE"
DEAD = "DEAD"
UNKNOWN = "UNKNOWN"

PURPOSES = frozenset({"process", "check", "test", "run", "reviewer", "sandbox",
                      "verification", "mutation", "extract"})

OP_KIND = "tmp_workspace"
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
_REC_SUFFIX = ".vh-ws.json"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class EphemeralError(RuntimeError):
    """A workspace operation that cannot be done safely; nothing was changed
    beyond what the message says."""


class TempAdmissionRefused(EphemeralError):
    """The admission refused the reservation of a temporary workspace
    (`refusal` is the admission's own answer); nothing was created."""

    def __init__(self, refusal: Any) -> None:
        super().__init__(f"temporary workspace not admitted: {refusal}")
        self.refusal = refusal


class TempAdmissionUnavailable(EphemeralError):
    """The admission could not answer (it raised `cause`); nothing was created."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"temporary workspace not admitted: the admission cannot answer "
                         f"({type(cause).__name__}: {cause})")
        self.cause = cause


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def record_path(workspace: os.PathLike[str] | str) -> Path:
    """Where the record of `workspace` lives: beside it, hidden, in the root."""
    workspace = Path(workspace)
    return workspace.parent / f".{workspace.name}{_REC_SUFFIX}"


def read_record(workspace: os.PathLike[str] | str) -> dict[str, Any] | None:
    """The record beside `workspace` as a dict, or None (missing, a symlink,
    not a regular file, unreadable, malformed JSON, another schema). Values are
    not judged here."""
    record = _read_record(record_path(workspace))
    if not record or record.get("schema") != RECORD_SCHEMA:
        return None
    return record


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def creator_state(registry, record: dict[str, Any]) -> tuple[str, str]:
    """``(LIVE | DEAD | UNKNOWN, why)`` for the process lease a record holds.

    DEAD only when it is proven: another boot, a pid that does not exist, or a
    pid whose start time differs from the recorded one (a reused pid is a
    different process). Anything the readers cannot say -- an unreadable start
    time, a reader that raises, a record without the three fields -- is
    UNKNOWN, which is never taken for dead. (`StorageRegistry.lease_is_live`
    answers "not live" on a failing reader because it only supersedes
    leases; deleting something needs the stronger answer.)"""
    pid, start, boot = record.get("pid"), record.get("pid_start"), record.get("boot_id")
    if not (_is_int(pid) and _is_int(start) and isinstance(boot, str) and boot):
        return UNKNOWN, "the record holds no usable process lease"
    try:
        if registry._boot_id_reader() != boot:
            return DEAD, "the record is from another boot"
        if not registry._pid_exists(pid):
            return DEAD, f"pid {pid} does not exist"
        now = registry._pid_start_reader(pid)
    except Exception as exc:                    # a reader that fails proves nothing
        return UNKNOWN, f"the process table cannot be read ({type(exc).__name__}: {exc})"
    if not _is_int(now):
        return UNKNOWN, f"the start time of pid {pid} cannot be read"
    if now == start:
        return LIVE, f"pid {pid} is the recorded process"
    return DEAD, f"pid {pid} is another process now (start time {now}, recorded {start})"


def _mount_id(fd: int) -> int | None:
    """The ``mnt_id`` of the mount an open descriptor lies on, from
    ``/proc/self/fdinfo/<fd>``; None when it cannot be read. (Two directories of
    one filesystem have one ``st_dev`` even when a bind mount puts the second on
    another mount; ``mnt_id`` tells them apart.)"""
    try:
        with open(f"/proc/self/fdinfo/{fd}", "rb") as handle:
            text = handle.read(4096).decode("ascii", "replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            try:
                return int(line.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _identity(fd: int, path: str) -> tuple[int, int, int | None]:
    """``(st_dev, st_ino, mnt_id)`` of the open directory `fd` (named `path`
    for messages). A seam: a test reports another mount or device for one
    directory."""
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, _mount_id(fd)


def _entries(fd: int, path: str) -> list[tuple[str, bool]]:
    """``(name, is_directory)`` of every entry of the open directory `fd`, links
    not followed. A seam: a test makes one directory unlistable."""
    with os.scandir(fd) as listing:
        return [(item.name, item.is_dir(follow_symlinks=False)) for item in listing]


class _Refused(Exception):
    """A directory below the workspace that must not be entered or removed."""


def _open_child(name: str, parent_fd: int, path: str, dev: int, mnt: int) -> tuple[int, int]:
    """Open the directory `name` of `parent_fd` (no symlink followed) and check
    it is on the workspace's device **and mount**; ``(descriptor, st_ino)``, or
    `_Refused`."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _Refused(f"{path} cannot be opened ({exc})") from exc
    try:
        child_dev, child_ino, child_mnt = _identity(fd, path)
        if child_dev != dev:
            raise _Refused(f"{path} lies on another device (a mount inside)")
        if child_mnt is None:
            raise _Refused(f"the mount of {path} cannot be read (failing closed)")
        if child_mnt != mnt:
            raise _Refused(f"{path} lies on another mount (a bind mount inside)")
    except BaseException:
        os.close(fd)
        raise
    return fd, child_ino


def _list(fd: int, path: str) -> list[tuple[str, bool]]:
    try:
        return _entries(fd, path)
    except OSError as exc:
        raise _Refused(f"{path} cannot be listed ({exc})") from exc


# What the walk saw, per directory: ``{name: ("d", st_ino, <the directory's own snapshot>)}`` for a
# directory and ``{name: ("f", st_dev, st_ino)}`` for everything else (device and mount of a directory
# are checked against the workspace's on every open, so its inode is all that has to be remembered).
_Snapshot = dict


def _verify_tree(fd: int, path: str, dev: int, mnt: int) -> tuple[int, _Snapshot]:
    """Walk every directory below `fd` -- opening each relative to its parent and
    checking device and mount before it is listed -- and return the apparent
    bytes of its regular files with a **snapshot** of what was seen (the removal
    acts on nothing else); `_Refused` for the first directory that is not fully
    the workspace's. Nothing is removed."""
    total = 0
    snapshot: _Snapshot = {}
    for name, is_dir in _list(fd, path):
        if is_dir:
            child, ino = _open_child(name, fd, f"{path}/{name}", dev, mnt)
            try:
                sub_total, sub_snapshot = _verify_tree(child, f"{path}/{name}", dev, mnt)
            finally:
                os.close(child)
            total += sub_total
            snapshot[name] = ("d", ino, sub_snapshot)
        else:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            snapshot[name] = ("f", info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode):
                occupied = info.st_blocks * 512
                # a sparse (or compressed) file is booked at what it occupies, not at its
                # apparent size: a hole of a terabyte is not a terabyte written
                total += occupied if (info.st_size >= 4096 and occupied < info.st_size) \
                    else info.st_size
    return total, snapshot


def _remove_tree(fd: int, path: str, dev: int, mnt: int, snapshot: _Snapshot) -> list[str]:
    """Remove every entry below the open directory `fd` with ``unlinkat`` and
    ``rmdir`` relative to the directory descriptors. **Only what the walk saw, and
    only if it is still that:** a name the walk did not list, a directory that
    is now another inode (or another mount or device), a file that is now another
    inode, an entry that changed between directory and non-directory, raises
    `_Refused` and aborts the whole cleanup -- a same-uid process that renamed a
    foreign directory (or anything) into a name never gets it entered or
    emptied. Returns what could not be removed (an OS error on one entry)."""
    failed: list[str] = []
    for name, is_dir in _list(fd, path):
        seen = snapshot.get(name)
        if seen is None or seen[0] != ("d" if is_dir else "f"):
            raise _Refused(f"{path}/{name} is not what the walk verified (it appeared, or changed "
                           f"between directory and file, after the walk)")
        try:
            if is_dir:
                child, ino = _open_child(name, fd, f"{path}/{name}", dev, mnt)
                try:
                    if ino != seen[1]:
                        raise _Refused(f"{path}/{name} is not the directory the walk verified "
                                       f"(another inode now)")
                    failed += _remove_tree(child, f"{path}/{name}", dev, mnt, seen[2])
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=fd)
            else:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (seen[1], seen[2]):
                    raise _Refused(f"{path}/{name} is not the file the walk verified "
                                   f"(another inode now)")
                os.unlink(name, dir_fd=fd)
        except OSError as exc:
            failed.append(f"{path}/{name} ({exc.strerror or exc})")
    return failed


def _clean_workspace(path: Path, st_dev: int, st_ino: int, *, remove: bool,
                     root: tuple[int, int] | None = None) -> tuple[bool, str, int]:
    """The creator's cleanup of the workspace `path` it registered as
    ``(st_dev, st_ino)`` in the root registered as ``root = (st_dev, st_ino)``:
    ``(removed, note, bytes)``. With ``remove`` false only the verification and
    the measure are done (a workspace that is kept).

    Only the directory this process created -- in the root it was created in
    (**not** a root renamed away and replaced, or overmounted: then the name is
    missing for a reason that is not "gone"), still the recorded inode, a real
    directory (not a symlink), the workspace and every directory below it on one
    device **and one mount** and listable -- and then only what the walk saw.
    Anything else is left untouched and said. **Bytes:** 0 when nothing was
    verified (the walk refused, or the root or the workspace is not the
    registered one); otherwise the bytes the walk counted -- also when the
    removal stopped or an entry stays (they may still be there, an upper bound),
    and for a kept workspace. Nothing outside the verified tree is listed or
    measured. See the module docstring."""
    try:
        root_fd = os.open(path.parent, _DIR_FLAGS)
    except OSError as exc:
        return False, f"cannot open the root ({exc}); left alone", 0
    try:
        if root is not None:
            root_info = os.fstat(root_fd)
            if (root_info.st_dev, root_info.st_ino) != tuple(root):
                return False, "the root is not the directory registered for it (renamed " \
                              "away, replaced or overmounted); left alone", 0
        try:
            info = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True, "already gone", 0
        except OSError as exc:
            return False, f"cannot stat the workspace ({exc}); left alone", 0
        if not stat.S_ISDIR(info.st_mode):
            return False, "not a directory any more; left alone", 0
        if (info.st_dev, info.st_ino) != (st_dev, st_ino):
            return False, "no longer the inode registered for it; left alone", 0
        try:
            fd = os.open(path.name, _DIR_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            return False, f"cannot open the workspace ({exc}); left alone", 0
        try:
            dev, ino, mnt = _identity(fd, os.fspath(path))
            _, _, root_mnt = _identity(root_fd, os.fspath(path.parent))
            if (dev, ino) != (st_dev, st_ino):
                return False, "no longer the inode registered for it; left alone", 0
            if mnt is None or root_mnt is None:
                return False, "the mount of the workspace cannot be read (failing closed); " \
                              "left alone", 0
            if mnt != root_mnt:
                return False, "the workspace lies on another mount than its root (a bind " \
                              "mount); left alone", 0
            try:
                nbytes, snapshot = _verify_tree(fd, os.fspath(path), dev, mnt)
            except _Refused as exc:
                return False, f"{exc}; left alone", 0
            except RecursionError:
                return False, "the tree is nested too deeply to verify; left alone", 0
            if not remove:
                return False, "kept", nbytes
            try:
                failed = _remove_tree(fd, os.fspath(path), dev, mnt, snapshot)
            except _Refused as exc:         # a change after the walk: the removal's own check
                return False, f"{exc}; the removal stopped there (entries before it may be " \
                              f"gone)", nbytes
            except RecursionError:
                return False, "the tree is nested too deeply; the removal stopped there " \
                              "(entries before it may be gone)", nbytes
        finally:
            os.close(fd)
        if failed:
            return False, f"some entries could not be removed; it stays ({failed[0]})", nbytes
        try:
            os.rmdir(path.name, dir_fd=root_fd)
        except OSError as exc:
            return False, f"the emptied workspace could not be removed ({exc}); it stays", nbytes
        return True, "removed by its creator", nbytes
    finally:
        os.close(root_fd)


def _discard_own_record(path: Path, workspace_id: str) -> None:
    """Remove the record this very call wrote for a workspace whose registration
    failed (the directory is already gone): a regular file, the right schema and
    workspace, never a symlink or someone else's record. Best effort."""
    record = record_path(path)
    existing = _read_record(record)
    if existing and existing.get("schema") == RECORD_SCHEMA \
            and existing.get("workspace_id") == workspace_id:
        try:
            os.unlink(record)
        except OSError:
            pass


@dataclass
class Workspace:
    """One registered workspace. `path` is the only thing a caller uses."""

    path: Path
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str | None
    record: Path
    st_dev: int
    st_ino: int
    root_ident: tuple[int, int] | None = None   # (st_dev, st_ino) of the root it lives in
    reservation: Any = field(default=None, repr=False)
    released: bool = False
    note: str = ""


class EphemeralRoots:
    """The ephemeral temporary root of one session of one project.

    `registry` is a `StorageRegistry`; the session must already hold a live
    lease of this process (`registry.open_session`). `admission` is the
    project's `StorageAdmission` (or anything with ``reserve``/``release``);
    without one nothing is admitted or measured and the writes are not
    reserved -- a run entry point always passes one.
    """

    def __init__(self, registry, project_id: str, session_id: str, *, admission=None,
                 getpid: Callable[[], int] = os.getpid) -> None:
        self.registry = registry
        self.project_id = project_id
        self.session_id = session_id
        self.admission = admission
        self._getpid = getpid

    # -- create ------------------------------------------------------------ #

    def create(self, *, creator: str, purpose: str, run_id: str | None = None,
               estimate_bytes: int | None = None, name: str = "ws") -> Workspace:
        """Register and create one workspace; see the module docstring."""
        if not isinstance(creator, str) or not creator.strip():
            raise ValueError("creator must be a non-empty string")
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}; known: {sorted(PURPOSES)}")
        stem = run_id if run_id is not None else name
        if not isinstance(stem, str) or not _PREFIX.match(stem):
            raise ValueError(f"{'run_id' if run_id is not None else 'name'} {stem!r} is not a "
                             f"short lower-case id")
        registry = self.registry
        lease = registry._read_lease(self.project_id, self.session_id)
        if lease is None or not registry.lease_is_live(lease):
            raise StorageOwnershipError(
                f"session {self.session_id!r} of project {self.project_id!r} holds no live "
                f"lease; no workspace is created")
        if lease.pid != self._getpid():
            raise StorageOwnershipError(
                f"the lease of session {self.session_id!r} is pid {lease.pid}'s, not this "
                f"process's ({self._getpid()}); no workspace is created")
        reservation = None
        if self.admission is not None:
            try:
                answer = self.admission.reserve(self.project_id, OP_KIND,
                                                estimate_bytes=estimate_bytes)
            except Exception as exc:                # the ledger cannot answer: nothing is created
                raise TempAdmissionUnavailable(exc) from exc
            if not getattr(answer, "admitted", False):
                raise TempAdmissionRefused(answer)
            reservation = answer
        path: Path | None = None
        ident: tuple[int, int] | None = None
        root_ident: tuple[int, int] | None = None
        try:
            root = Path(registry.scratch_dir(self.project_id, self.session_id, TMP_KIND))
            root_st = os.lstat(root)
            root_ident = (root_st.st_dev, root_st.st_ino)
            path = Path(tempfile.mkdtemp(prefix=f"{stem}-", dir=root))
            st = os.lstat(path)
            ident = (st.st_dev, st.st_ino)
            record = {
                "schema": RECORD_SCHEMA,
                "project_id": self.project_id,
                "session_id": self.session_id,
                "workspace_id": path.name,
                "run_id": run_id,
                "creator": creator,
                "purpose": purpose,
                "pid": lease.pid,
                "pid_start": lease.pid_start,
                "boot_id": lease.boot_id,
                "st_dev": st.st_dev,
                "st_ino": st.st_ino,
                "created_utc": _utc_iso(),
                "state": REGISTERED,
                "reservation_id": getattr(reservation, "id", None),
            }
            durable_create(record_path(path), _dump(record), mode=0o600)
            record["state"] = ACTIVE
            durable_write(record_path(path), _dump(record), mode=0o600)
        except BaseException:
            # The directory is this call's own and still empty: take it back, so
            # a failed registration leaves no workspace without a record.
            if path is not None and ident is not None:
                if _clean_workspace(path, *ident, remove=True, root=root_ident)[0]:
                    _discard_own_record(path, path.name)
            self._release_reservation(reservation, 0)
            raise
        return Workspace(path=path, workspace_id=path.name, project_id=self.project_id,
                         session_id=self.session_id, run_id=run_id, record=record_path(path),
                         st_dev=ident[0], st_ino=ident[1], root_ident=root_ident,
                         reservation=reservation)

    @contextmanager
    def workspace(self, *, keep: bool = False, **create: Any) -> Iterator[Workspace]:
        """`create`, then `release` on the way out -- also when the body raises."""
        ws = self.create(**create)
        try:
            yield ws
        finally:
            self.release(ws, keep=keep)

    # -- release ----------------------------------------------------------- #

    def release(self, ws: Workspace, *, keep: bool = False) -> dict[str, Any]:
        """The creator is done: clean the workspace (unless `keep`), record
        ``RELEASED``, release the reservation. Returns what happened."""
        if ws.released:
            return {"removed": False, "note": "already released"}
        ws.released = True
        removed, note, measured = _clean_workspace(ws.path, ws.st_dev, ws.st_ino,
                                                   remove=not keep, root=ws.root_ident)
        ws.note = note
        try:
            record = _read_record(ws.record)
            if record and record.get("schema") == RECORD_SCHEMA \
                    and record.get("workspace_id") == ws.workspace_id:
                record.update(state=RELEASED, released_utc=_utc_iso(), removed=removed,
                              release_note=note)
                durable_write(ws.record, _dump(record), mode=0o600)
            else:
                note += "; its record is not the one written at creation (left as it is)"
        except OSError as exc:
            note += f"; the record could not be updated ({exc})"
        ws.note = note
        self._release_reservation(ws.reservation, measured)
        return {"removed": removed, "note": note}

    def _release_reservation(self, reservation: Any, actual: int) -> None:
        if reservation is None or self.admission is None:
            return
        try:
            self.admission.release(reservation, actual)
        except Exception:                       # a ledger problem never hides the real one
            pass


def _dump(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


#: What a mapped name ends in (`name_for_run`): a run id that already ends so is mapped too, so
#: no run id can be taken for another run id's mapped name.
_MAPPED = re.compile(r"-[0-9a-f]{12}$")


def name_for_run(run_id: str) -> str:
    """The registry session id and the workspace name stem of a run: the run id
    itself when it already is a short lower-case id that does not end in ``-`` and
    twelve hex digits, otherwise its lower-case form (anything outside
    ``[a-z0-9._-]`` becomes ``-``, at most 35 characters) followed by ``-`` and the
    first twelve hex digits of the SHA-256 of the real run id. Two different run ids
    therefore have two names unless their SHA-256 agree in 48 bits (a mapped name
    always ends as a mapped name does and a plain one never does, so a literal id
    cannot be another id's mapped form). Always a valid session id (<= 64) and
    workspace stem (<= 48)."""
    if _PREFIX.match(run_id) and not _MAPPED.search(run_id):
        return run_id
    safe = re.sub(r"[^a-z0-9._-]", "-", run_id.lower()).lstrip("._-")[:35] or "run"
    return f"{safe}-{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:12]}"


def project_for_run(registry, project_name: str, repo_path) -> tuple[str | None, str]:
    """``(project_id, how)`` of the registered project a run belongs to, or
    ``(None, why)``; ``how`` is ``name`` or ``repository``.

    By name first -- the run's ``project_name`` is a registered project id, the
    assumption the steward hook and the admission already make -- **and then the
    repository is compared** when both the run's directory and the project's root
    are repository roots: the same resolved ``git rev-parse --git-common-dir``
    (a linked worktree of the project's repository has it, a separate clone or an
    unrelated repository has not); a different one is no match (a directory that
    merely carries the project's name does not bind it). A project registered on a
    plain directory, or a run in one, is compared by name alone (the registry
    compares plain directories by path only: stated limit L1b).

    Then by repository: a run the launcher starts (`hoh start` without
    ``--project``) carries its worktree's directory name, not the project's id, so
    the one registered project whose repository has the run's git common directory
    owns it -- the registry's own rule for "one root, one project"
    (`register_project`). Nothing is proven when git cannot be read: the reason
    says so (it is not "not a repository"), and the run keeps the sibling scratch."""
    projects = registry._registered_projects()
    if not projects:
        return None, "no project is registered"
    try:
        here = registry._repo_identity(Path(repo_path).resolve(strict=True))
        unreadable = ""
    except Exception as exc:                   # noqa: BLE001 - nothing proven, said as what it is
        here, unreadable = None, f"{type(exc).__name__}: {exc}"
    if project_name in projects:
        if unreadable:
            return None, (f"the run's repository identity cannot be read ({unreadable}), so it "
                          f"is not confirmed as the registered project {project_name!r}")
        record = registry._project_record(project_name)
        try:
            theirs = registry._repo_identity(Path(record["project_root"]))
        except Exception as exc:               # noqa: BLE001
            return None, (f"the repository identity of the registered project {project_name!r} "
                          f"cannot be read ({type(exc).__name__}: {exc})")
        if here is not None and theirs is not None and here != theirs:
            return None, (f"the run's repository is not the repository of the registered "
                          f"project {project_name!r} (another git common directory)")
        return project_name, "name"
    if unreadable:
        return None, (f"no registered project has this name and the run's repository identity "
                      f"cannot be read ({unreadable})")
    if here is None:
        return None, "no registered project has this name and the run's directory is not a repository root"
    found, skipped = [], 0
    for candidate in projects:
        record = registry._project_record(candidate)
        try:
            theirs = registry._repo_identity(Path(record["project_root"]))
        except Exception:                      # noqa: BLE001 - this project's repository is unreadable
            skipped += 1
            continue
        if theirs is not None and theirs == here:
            found.append(candidate)
    if len(found) == 1:
        return found[0], "repository"
    if found:
        return None, f"the run's repository belongs to several registered projects ({', '.join(found)})"
    return None, ("no registered project has this name or this repository"
                  + (f" ({skipped} registered project(s) have a repository that cannot be read)"
                     if skipped else ""))


class RunSession:
    """The registered session of one ``hoh run`` process (P2-42 wiring): the
    registry lease of this process for the run's session id, the project's
    ``tmp`` root for that session (registered before the run starts) and the
    `EphemeralRoots` the run's controller creates every workspace from.

    `begin` raises (nothing is left open) when the lease or the root cannot be
    had -- the caller refuses to start; it never runs on without. `close`
    ends the lease and reports a failure instead of raising: a run that has
    done its work does not fail because its lease could not be closed."""

    def __init__(self, registry, project_id: str, session_id: str, lease, roots: EphemeralRoots,
                 root: Path) -> None:
        self.registry = registry
        self.project_id = project_id
        self.session_id = session_id
        self.lease = lease
        self.roots = roots
        self.root = root

    @classmethod
    def begin(cls, registry, project_id: str, run_id: str, *, admission=None) -> "RunSession":
        session_id = name_for_run(run_id)
        lease = registry.open_session(project_id, session_id, pid=os.getpid(),
                                      boot_id=registry._boot_id_reader())
        try:
            root = Path(registry.scratch_dir(project_id, session_id, TMP_KIND))
        except BaseException:
            try:
                registry.close_session(project_id, session_id, lease)
            except Exception:                    # the first failure is the one that is said
                pass
            raise
        return cls(registry, project_id, session_id, lease,
                   EphemeralRoots(registry, project_id, session_id, admission=admission), root)

    def close(self) -> str:
        """End the lease; ``""`` when it ended, otherwise why not."""
        try:
            self.registry.close_session(self.project_id, self.session_id, self.lease)
        except Exception as exc:                  # noqa: BLE001 - said, never raised
            return f"{type(exc).__name__}: {exc}"
        return ""


__all__ = [
    "ACTIVE", "DEAD", "EphemeralError", "EphemeralRoots", "LIVE", "OP_KIND", "PURPOSES",
    "RECORD_SCHEMA", "REGISTERED", "RELEASED", "RunSession", "STORED_STATES", "TMP_KIND",
    "TempAdmissionRefused", "TempAdmissionUnavailable", "UNKNOWN", "Workspace",
    "creator_state", "name_for_run",
    "project_for_run", "read_record", "record_path",
]
