"""Durable writes that survive a full disk: one primitive instead of nine.

V3.3 amendment A1, requirement P2-41 (STORAGE-B1). A long unattended run that
reaches a full disk must stop cleanly. What it must never do is leave a
durable record half-written, half-replaced, or replaced by an empty file --
that turns a storage incident into lost state, and the P2 exit line says
"no lost state".

`durable_write` is the one way to do it here:

1. **Pre-check.** `os.statvfs` on the directory *before anything is created*;
   too little room for the payload plus a reserve is a `DiskFullError` and the
   directory is untouched.
2. **Temp beside the target.** `.<name>.tmp.<pid>.<random>`, opened with
   `O_CREAT | O_EXCL | O_WRONLY`, so this call owns a fresh inode.
3. **Write in a loop.** A short write is progress; a zero-byte write is a
   failure, never a silent truncation.
4. **Mode, fsync, replace, fsync the directory.** The final mode is set
   explicitly with `fchmod`, so it does not depend on the umask.

On any failure before the replace the target is untouched -- old bytes, or
absent if it was absent -- and the temp file this call created is abandoned
through `_abandon_own_temp`, the one removal in this module. ENOSPC, EDQUOT
and EFBIG (a file-size limit, i.e. a real short write) surface as
`DiskFullError`; every other error is re-raised as it was.

A failure *after* the replace -- the directory fsync -- is different: the new
bytes are already in place, only the rename may not survive a power failure.
That is `DurableWriteNotSynced`, never `DiskFullError`, whatever its errno.

*(As written on 2026-10-01; superseded by the next sections.)*
Known remaining work. This change migrates `store._atomic_write` and
`timers.TimerStore.write` only. These writers still carry their own
"write beside, fsync, replace" helper and are not yet routed through
`durable_write`:

* `projectstore.py`
* `epoch.py`
* `telemetry.py`
* `succession.py`
* `trust.py`
* `goalbook.py`
* `migrations.py`
* `store.RunStore._park` -- its `open(..., "xb")` fallback
* `store.RunStore.backup`

(`projectstore.py` and `succession.py` already call `store._atomic_write` for
some of their records; those calls go through `durable_write` now, the rest of
their writes do not.) P2-41 is therefore **not PROVEN** by this change alone;
the register carries a cap until the writers above are migrated.

Migrated by P2-41b H-a (2026-10). Two siblings of `durable_write` were added:
`durable_create` (publish under a name that must not exist, by `os.link`,
never replacing) and `durable_append` (one complete line, torn-tail guard).
Through the three of them now go W1-W15: `epoch.EpochStore._write` (W1) and
`._preserve_previous` (W2), `goalbook.Goalbook.write` (W3) and `._park` (W4),
`telemetry.TelemetryLog.rewrite_sorted` (W5) and `.append` (W6),
`migrations._atomic_write` (W7) and `_preserve` (W8),
`storage_pressure._atomic_write` (W9), `trust.register` (backup W10, config
W11), `projectstore.ProjectStore._park` (W12, a copy now, no longer a move),
`succession.MasterAuthority._write` (the log append, W13), the fallback of
`store.RunStore._park` (W14) and `store.RunStore.backup` (W15).

Still remaining:

* `amendment.py`: `park_and_amend`
* `approval_policy.py`: `RoleApprovalPolicy.write_role_files`
* `governance.py`: `Governor.decide`
* `governance.py`: `bind`
* `governance.py`: `record_override`
* `launcher.py`: `HohRunLauncher.prepare`
* `cli.py`: `_cmd_run`
* `cli.py`: `_integrity_close`
* `dispatchers.py`: `HarnessDispatcher.dispatch`
* `dispatchers.py`: `_append_audit`
* `dispatchers.py`: `_write_leaf`
* `store.py`: `RunStore.restore`

P2-41 is not PROVEN while these remain.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

#: Errors that mean "there is no room for these bytes". EFBIG is what a write
#: past RLIMIT_FSIZE returns once SIGXFSZ is ignored -- a real short write.
_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG})


class DiskFullError(OSError):
    """There was no room to write a durable record; the target is untouched.

    An `OSError` with `errno.ENOSPC`, so callers that already handle OSError
    keep working. `free_bytes` is None when the space was not measured (the
    filesystem refused a write rather than the pre-check refusing the call).
    """

    def __init__(self, path: Path | str, needed_bytes: int,
                 free_bytes: int | None = None, detail: str = "") -> None:
        free = "unknown" if free_bytes is None else str(free_bytes)
        msg = f"no room for a durable write: need {needed_bytes} bytes, free {free}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(errno.ENOSPC, msg, str(path))
        self.path = Path(path)
        self.needed_bytes = needed_bytes
        self.free_bytes = free_bytes


class DurableWriteNotSynced(OSError):
    """The new bytes are in place, but the rename may not be durable.

    Raised when the directory fsync after `os.replace` fails (an ENOSPC there
    included). The target already holds the new payload; what is uncertain is
    only whether the directory entry survives a power failure. Carries the
    errno of the failed directory fsync; the original error is `__cause__`.
    """

    def __init__(self, path: Path | str, cause: OSError) -> None:
        msg = ("new bytes are in place, but the rename may not be durable: "
               f"directory fsync failed ({cause.strerror or cause})")
        super().__init__(cause.errno, msg, str(path))
        self.path = Path(path)


class UnsafeRecordFile(OSError):
    """A record file that is a symbolic link, is not a regular file, or has more
    than one name. An `OSError`, so a caller that already handles one keeps
    working; `why` says which."""

    def __init__(self, path: Path | str, why: str, err: int = errno.EMLINK) -> None:
        super().__init__(err, why, str(path))
        self.path = Path(path)
        self.why = why


def _require_single_link(fd: int, path: Path | str) -> None:
    """On the descriptor, not on the name: a regular file with exactly one name, or
    close it and raise `UnsafeRecordFile`.

    A second name may lie outside the run, where an append would write to a file
    that is not ours, and the count cannot tell the two cases apart.
    """
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeRecordFile(path, "is not a regular file", errno.EINVAL)
        if st.st_nlink != 1:
            raise UnsafeRecordFile(path, f"has {st.st_nlink} names, not one")
    except BaseException:
        os.close(fd)
        raise


def read_single_link(path: Path | str) -> bytes | None:
    """The bytes of one of HoH's own record files; None when it does not exist.

    For a ledger a same-user process may try to redirect. The last component is
    opened with `O_NOFOLLOW` (a symbolic link, dangling or not, is refused),
    `O_NONBLOCK` (a FIFO cannot hang the open) and `O_CLOEXEC`; then the file
    must be regular with exactly one name (`_require_single_link`). A symbolic
    link, a non-regular file or a second name is `UnsafeRecordFile`; any other
    error is raised as it is. Directories above the file are not checked: the
    run directory's own identity is the store's concern.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeRecordFile(path, "is a symbolic link", errno.ELOOP) from exc
        raise
    _require_single_link(fd, path)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def durable_write(path: Path, payload: bytes | str, *, mode: int | None = 0o644,
                  min_free_bytes: int = 1 << 20) -> None:
    """Replace `path` with `payload`, or leave it exactly as it was.

    Does not create directories: `path.parent` must exist. Raises
    `DiskFullError` when the pre-check finds too little room or the
    filesystem runs out of it; any other failure before the replace is
    re-raised unchanged. A failed directory fsync after the replace raises
    `DurableWriteNotSynced`.

    `mode=None` creates the temp file with 0o666 and sets no mode, so the
    umask decides, exactly as `open(..., "w")` does.
    """
    path = Path(path)
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    parent = path.parent

    # Before anything is created: a refusal here leaves the directory as it was.
    st = os.statvfs(parent)
    free = st.f_bavail * st.f_frsize
    needed = len(data) + min_free_bytes
    if free < needed:
        raise DiskFullError(path, needed, free, "refused before writing")

    tmp_path = parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                     0o666 if mode is None else 0o600)
    except OSError as exc:
        # Nothing was created, so there is nothing to abandon.
        if exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise

    try:
        _write_all(fd, data, path)
        if mode is not None:
            os.fchmod(fd, mode)
        os.fsync(fd)
        # The fd stays open across the replace so that an abandon after a
        # failed replace can still prove, by inode, which file it created.
        os.replace(tmp_path, path)
    except BaseException as exc:
        _abandon_own_temp(fd, tmp_path)
        if isinstance(exc, OSError) and not isinstance(exc, DiskFullError) \
                and exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise
    os.close(fd)

    # After the replace the target holds the new bytes; nothing is abandoned
    # from here on. What can still fail is the durability of the rename.
    try:
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise DurableWriteNotSynced(path, exc) from exc


def _write_all(fd: int, data: bytes, path: Path) -> None:
    """The write loop: a short write is progress, a zero-byte write fails."""
    view = memoryview(data)
    done = 0
    while done < len(view):
        n = os.write(fd, view[done:])
        if n <= 0:
            raise DiskFullError(path, len(data), None,
                                f"zero-byte write after {done} of {len(data)} bytes")
        done += n


def _precheck(path: Path, needed: int, when: str) -> None:
    """Free-space pre-check, `f_bavail * f_frsize`, before anything is created."""
    st = os.statvfs(path.parent)
    free = st.f_bavail * st.f_frsize
    if free < needed:
        raise DiskFullError(path, needed, free, when)


def _fsync_parent(path: Path) -> None:
    """fsync `path`'s directory; a failure is `DurableWriteNotSynced`."""
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise DurableWriteNotSynced(path, exc) from exc


def durable_create(path: Path, payload: bytes | str, *, mode: int | None = 0o644,
                   min_free_bytes: int = 1 << 20) -> bool:
    """Publish `payload` under `path`, which must not exist. Never replaces.

    Does not create directories. An existing name (even a dangling symlink)
    is `FileExistsError` before anything is created and before the space
    pre-check. Then the steps of `durable_write` -- pre-check, temp beside
    the target with `O_CREAT | O_EXCL`, write loop, mode (`mode=None`: the
    umask decides), fsync -- and the publish with `os.link(tmp, path)`, which
    fails with `FileExistsError` if a file appeared at `path` meanwhile, so it
    can never replace one. A filesystem without hard links makes `os.link`
    fail; that error is re-raised (fail closed: there is no fallback to
    `os.replace` or to writing at the final name).

    Any failure before or at the link abandons the temp file and leaves
    `path` as it was; space errors surface as `DiskFullError`. After the link
    the temp *name* is dropped through `_abandon_own_temp` (the bytes live on
    under `path`), then the directory is fsynced; a failure there is
    `DurableWriteNotSynced` with the file in place.

    Returns True when the temp name was dropped, False when it could not be:
    the published file is whole either way, and the temp name left is only a
    second link to the same inode (no extra bytes).
    """
    path = Path(path)
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if os.path.lexists(path):
        raise FileExistsError(errno.EEXIST,
                              "a durable create never replaces an existing file", str(path))
    _precheck(path, len(data) + min_free_bytes, "refused before writing")

    tmp_path = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                     0o666 if mode is None else 0o600)
    except OSError as exc:
        if exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise

    try:
        _write_all(fd, data, path)
        if mode is not None:
            os.fchmod(fd, mode)
        os.fsync(fd)
        os.link(tmp_path, path)
    except BaseException as exc:
        _abandon_own_temp(fd, tmp_path)
        if isinstance(exc, OSError) and not isinstance(exc, DiskFullError) \
                and exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise

    # Published: `path` and the temp name are two links to one inode. Dropping
    # the temp name removes no bytes. `_abandon_own_temp` closes `fd`.
    dropped = _abandon_own_temp(fd, tmp_path)
    _fsync_parent(path)
    return dropped


def durable_append(path: Path, line: bytes | str, *,
                   min_free_bytes: int = 1 << 20, single_link: bool = False) -> None:
    """Append one complete line to `path`, or raise.

    With `single_link` the file is opened with `O_NOFOLLOW` and then checked
    on the descriptor (`_require_single_link`): a symbolic
    link, a file that is not regular and a file with more than one name are
    `UnsafeRecordFile` before a byte is written (the seal ledger, whose
    appends must never reach a file outside the run). Without it nothing
    changes for the other callers (telemetry, the succession log).

    Does not create directories (a missing file is created, mode as
    `open(path, "a")` gives it). A newline is added if missing. The
    free-space pre-check runs before the file is opened, so a refusal leaves
    it untouched (not even created). If the file ends in a torn tail (no
    final newline, left by an earlier failed append), the line is prefixed
    with a newline in the same write, so it is never glued onto the fragment.
    Exactly one `os.write`: a short count is `DiskFullError` and is not
    retried; ENOSPC/EDQUOT/EFBIG are `DiskFullError`. Then fsync the file and
    its directory; a directory-fsync failure is `DurableWriteNotSynced` (the
    line is in place). Nothing is truncated; earlier lines are never touched.

    Precondition: the torn-tail check and the write are two system calls, so
    the guard is exact only for a single appender or under the caller's lock.
    With concurrent appenders each record is still one `O_APPEND` write and
    never interleaves inside, but a record racing a torn fragment can still
    be glued onto it.
    """
    path = Path(path)
    data = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if not data.endswith(b"\n"):
        data += b"\n"
    _precheck(path, len(data) + 1 + min_free_bytes, "refused before opening")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
    if single_link:
        flags |= os.O_NOFOLLOW          # (a FIFO opens O_RDWR without waiting; the check refuses it)
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as exc:
        if single_link and exc.errno == errno.ELOOP:
            raise UnsafeRecordFile(path, "is a symbolic link", errno.ELOOP) from exc
        if exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise
    if single_link:
        _require_single_link(fd, path)
    try:
        size = os.fstat(fd).st_size
        if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        try:
            n = os.write(fd, data)
            if n != len(data):
                raise DiskFullError(path, len(data), None,
                                    f"short append: {n} of {len(data)}")
            os.fsync(fd)
        except OSError as exc:
            if not isinstance(exc, DiskFullError) and exc.errno in _SPACE_ERRNOS:
                raise DiskFullError(path, len(data), None,
                                    os.strerror(exc.errno)) from exc
            raise
    finally:
        os.close(fd)
    _fsync_parent(path)


def _abandon_own_temp(fd: int, tmp_path: Path) -> bool:
    """Abort a failed write: remove the temp file this call created, if it is
    still the one at `tmp_path`. Closes `fd`. Never raises.

    Why this is not a deletion of data: the file was created a moment earlier
    by the same `durable_write` call with `O_CREAT | O_EXCL`, so it did not
    exist before, and it has only ever held this call's partial bytes -- the
    record those bytes were meant to become is still intact at the target.
    Removing it aborts a write; leaving it would litter the record directory
    with half-written copies that a full disk can least afford.

    The check: `os.fstat(fd)` (the inode this call created) is compared with
    `os.stat(name, dir_fd=..., follow_symlinks=False)` (what the directory
    entry names now); only on the same device and inode is the entry unlinked,
    relative to the same directory fd. A different file swapped in at that name
    is kept.

    Residual race, stated plainly: between that comparison and the unlink, a
    process with write access to the same directory could swap the entry, and
    the unlink would then remove what it put there. The check narrows this to
    that window; it does not close it. This function is only ever called on
    VeriHarness's own record directories, where no other writer is expected.

    It is also called after a successful `durable_create` link. There the
    temp name is a second link to the inode just published under the target
    name, so removing that name removes no bytes; it only drops the extra
    name. It stays the only removal in this module.

    Returns True when the temp file was removed.
    """
    removed = False
    dir_fd = None
    try:
        name = tmp_path.name
        dir_fd = os.open(tmp_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        own = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (own.st_dev, own.st_ino) == (current.st_dev, current.st_ino):
            os.unlink(name, dir_fd=dir_fd)
            removed = True
    except Exception:
        # Abandoning must never hide the error that caused it.
        pass
    finally:
        for f in (fd, dir_fd):
            if f is not None:
                try:
                    os.close(f)
                except OSError:
                    pass
    return removed
