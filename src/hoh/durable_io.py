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

Migrated by P2-41 closure S1 (2026-10). Three helpers were added for a tree
rather than a file: `require_room` (the space pre-check for a directory, creates
nothing), `fsync_tree` and `durable_publish_tree` (a complete staging tree,
fsynced, published by one rename, or merged file by file with the named files
last). Through them and the three siblings now go: `amendment.park_and_amend`
(the superseded text is parked by `durable_create` before the new text is
written), `approval_policy.RoleApprovalPolicy.write_role_files`,
`governance.Governor.decide`, `governance.bind` (witness before pin) and
`governance.record_override`, and `store.RunStore.restore` (extracted into a
staging directory, read back, then published).

Migrated by P2-41 closure S3 (2026-10). `launcher.HohRunLauncher.prepare` (the
repair specification, by `durable_create`), `cli._cmd_run` (the four pre-run
records) and `cli._integrity_close` (the integrity record, by `durable_write`;
a record that cannot be written sets the earlier one aside under a versioned
name, so a stale verdict is never read as the current one). A merged restore
(`durable_publish_tree`, `merge=True`) now keeps the empty directories of the
backup, creates the missing ones owner-only and gives them the backup's modes
only after the last replace, and refuses a directory it cannot write into
before it replaces anything.

Migrated by P2-41 closure S2 (2026-10). `dispatchers._write_leaf` (the versioned
write of prompt files and markers), `dispatchers._append_audit` (the
prompt-binding audit file) and the recorded answer of `HarnessDispatcher.dispatch`
go through the three functions. Those writers act relative to a directory
descriptor opened with `O_NOFOLLOW`, so the three functions gained a `dir_fd`
form: with a descriptor `path` must be a bare name, and every step (the space
check, the temp file, the publish, the existence check, the directory fsync) is
relative to the descriptor, which stays open and belongs to the caller. Without
a descriptor nothing changes. `durable_write` also takes a `before_replace` hook,
called once the temp file is written and fsynced and immediately before the
replace, for a caller that must touch the old target only when the new bytes are
safe (the version link of `_write_leaf`); `durable_append` takes a `create_mode`.

No writer remains in the list of the register's `equivalent`: the writer scan of `tests/test_durable_writers.py` has no DEFERRED and no MIGRATE_LATER row, and fails when a raw write call appears that is not classified there.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
import time
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


#: How many times `read_single_link` opens a name that was replaced between its open and its `fstat`, and how long
#: it waits for the second name of a record that `durable_create` has just linked. Both are properties of the writers
#: in this module, not guesses: `durable_write` replaces the name by `os.replace`, which leaves the inode a reader
#: already opened with no name at all (`st_nlink == 0`) -- the record is whole, only the name moved on -- and
#: `durable_create` publishes by `os.link` and drops its temp name afterwards, so for a moment the whole record has
#: two names (measured on this disk: median 60 us, worst 30 ms). A reader that refused either was refusing a
#: well-formed record (0.1 % to 0.4 % of the reads against a replacing writer). Each is bounded; past its bound the read
#: is refused as before, so a record that really has two names (a hard link made on purpose) costs the grace and
#: is then `UnsafeRecordFile`, and nothing is retried across calls. The pause between reopens is not decoration:
#: the retries are correlated, not independent -- a writer thread that is mid-cycle meets the reader's next open too
#: (reads that needed three or more opens were thousands of times more frequent than independent attempts allow,
#: and one run met eight replacements in a row with immediate reopens). One pause of a millisecond lets the cycle
#: finish; sixteen attempts are the bound.
RECORD_REOPEN_ATTEMPTS = 16
_REOPEN_PAUSE_S = 0.001
SECOND_NAME_GRACE_S = 0.25
_SECOND_NAME_POLL_S = 0.001


def _settled_stat(fd: int) -> os.stat_result:
    """`os.fstat(fd)`, re-taken for up to `SECOND_NAME_GRACE_S` while the file has exactly two names.

    Two names are what a record has while `durable_create` has linked it and not yet dropped its temp name; a
    third, or a count that stays at two, is not that and is returned as it is."""
    st = os.fstat(fd)
    if st.st_nlink != 2 or not stat.S_ISREG(st.st_mode):
        return st
    deadline = time.monotonic() + SECOND_NAME_GRACE_S
    while st.st_nlink == 2 and time.monotonic() < deadline:
        time.sleep(_SECOND_NAME_POLL_S)
        st = os.fstat(fd)
    return st


def read_single_link(path: Path | str) -> bytes | None:
    """The bytes of one of HoH's own record files; None when it does not exist.

    For a ledger a same-user process may try to redirect. The last component is
    opened with `O_NOFOLLOW` (a symbolic link, dangling or not, is refused),
    `O_NONBLOCK` (a FIFO cannot hang the open) and `O_CLOEXEC`; then the file
    must be regular with exactly one name, on the descriptor. A symbolic
    link, a non-regular file or a second name is `UnsafeRecordFile`; any other
    error is raised as it is. Directories above the file are not checked: the
    run directory's own identity is the store's concern.

    **A read that races a writer of this module returns a whole record or refuses a
    real inconsistency; it is not refused spuriously within the stated bounds** (a name
    replaced `RECORD_REOPEN_ATTEMPTS` times in a row is refused, and a record that stays at
    two names costs `SECOND_NAME_GRACE_S` per read before it is refused). A name replaced by `durable_write`
    after the open leaves the descriptor on an inode with no name (`st_nlink == 0`):
    that inode still holds a complete earlier record, but the name now holds a newer
    one, so the name is opened again (at most `RECORD_REOPEN_ATTEMPTS` opens, a millisecond
    apart; the last one that still finds no name is refused). A record `durable_create` has just
    linked has two names for an instant: the descriptor is looked at again for up to
    `SECOND_NAME_GRACE_S`, and two names after that are refused. The counters are
    local to the call; nothing is persisted and nothing outlives it.
    """
    for attempt in range(RECORD_REOPEN_ATTEMPTS):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeRecordFile(path, "is a symbolic link", errno.ELOOP) from exc
            raise
        try:
            st = _settled_stat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafeRecordFile(path, "is not a regular file", errno.EINVAL)
            replaced = st.st_nlink == 0 and attempt + 1 < RECORD_REOPEN_ATTEMPTS
            if not replaced and st.st_nlink != 1:
                raise UnsafeRecordFile(path, f"has {st.st_nlink} names, not one")
        except BaseException:
            os.close(fd)
            raise
        if not replaced:
            break
        os.close(fd)                              # replaced under us: the name holds a newer record
        time.sleep(_REOPEN_PAUSE_S)               # let the writer finish its cycle
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


def _place(path: Path | str, dir_fd: int | None) -> tuple[Path, dict, Path]:
    """Where a primitive writes: `(path, keywords, parent)`.

    Without a descriptor the path form: `keywords` is empty, `parent` is
    `path.parent`, and every `os.*` call is made exactly as it always was. With
    a descriptor `path` must be a bare name (no separator, not `.`, `..` or
    empty): the kernel would resolve anything else from the descriptor and could
    leave the directory that was checked, so it is `ValueError` before anything
    is touched. `keywords` is then `{"dir_fd": dir_fd}` and `parent` is `.`.
    """
    raw = os.fspath(path)
    path = Path(raw)
    if dir_fd is None:
        return path, {}, path.parent
    if not raw or raw in (".", "..") or "/" in raw or "\0" in raw:
        raise ValueError(f"with dir_fd the path must be a bare name, not {raw!r}")
    return path, {"dir_fd": dir_fd}, Path(".")


def _temp_name(path: Path, parent: Path) -> Path:
    return parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"


def _lexists(path: Path, keywords: dict) -> bool:
    """`os.path.lexists(path)`, or, with a descriptor, a no-follow `stat` of the
    bare name relative to it."""
    if not keywords:
        return os.path.lexists(path)
    try:
        os.stat(path, follow_symlinks=False, **keywords)
    except FileNotFoundError:
        return False
    return True


def durable_write(path: Path, payload: bytes | str, *, mode: int | None = 0o644,
                  min_free_bytes: int = 1 << 20, dir_fd: int | None = None,
                  before_replace=None) -> None:
    """Replace `path` with `payload`, or leave it exactly as it was.

    Does not create directories: `path.parent` must exist. Raises
    `DiskFullError` when the pre-check finds too little room or the
    filesystem runs out of it; any other failure before the replace is
    re-raised unchanged. A failed directory fsync after the replace raises
    `DurableWriteNotSynced`.

    `mode=None` creates the temp file with 0o666 and sets no mode, so the
    umask decides, exactly as `open(..., "w")` does.

    `dir_fd`: an open directory descriptor, owned by the caller and never
    closed here. `path` is then a bare name inside that directory and every
    step (the space check, the temp file, the replace, the directory fsync) is
    relative to the descriptor, so a directory swapped for a link after the
    caller checked it cannot redirect the write. `ValueError` for a name that
    is not bare.

    `before_replace`: called with no arguments after the temp file is written
    and fsynced and immediately before the replace, for a caller that must do
    something to the old target only when the new bytes are safe. It is a
    failure before the replace like any other: the temp file is abandoned, the
    target is untouched and the exception propagates unchanged (an `OSError`
    with a space errno becomes `DiskFullError`). It is not called when the
    space pre-check refuses.
    """
    path, kw, parent = _place(path, dir_fd)
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)

    # Before anything is created: a refusal here leaves the directory as it was.
    st = os.statvfs(dir_fd if dir_fd is not None else parent)
    free = st.f_bavail * st.f_frsize
    needed = len(data) + min_free_bytes
    if free < needed:
        raise DiskFullError(path, needed, free, "refused before writing")

    tmp_path = _temp_name(path, parent)
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                     0o666 if mode is None else 0o600, **kw)
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
        if before_replace is not None:
            before_replace()
        # The fd stays open across the replace so that an abandon after a
        # failed replace can still prove, by inode, which file it created.
        if dir_fd is None:
            os.replace(tmp_path, path)
        else:
            os.replace(tmp_path, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException as exc:
        _abandon_own_temp(fd, tmp_path, dir_fd=dir_fd)
        if isinstance(exc, OSError) and not isinstance(exc, DiskFullError) \
                and exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise
    os.close(fd)

    # After the replace the target holds the new bytes; nothing is abandoned
    # from here on. What can still fail is the durability of the rename.
    _fsync_parent(path, dir_fd=dir_fd, parent=parent)


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


def _precheck(path: Path, needed: int, when: str, *, dir_fd: int | None = None) -> None:
    """Free-space pre-check, `f_bavail * f_frsize`, before anything is created.
    With a descriptor the filesystem of that descriptor is asked."""
    st = os.statvfs(dir_fd if dir_fd is not None else path.parent)
    free = st.f_bavail * st.f_frsize
    if free < needed:
        raise DiskFullError(path, needed, free, when)


def _fsync_parent(path: Path, *, dir_fd: int | None = None,
                  parent: Path | None = None) -> None:
    """fsync `path`'s directory (the descriptor itself, when one is given); a
    failure is `DurableWriteNotSynced`."""
    try:
        if dir_fd is not None:
            os.fsync(dir_fd)
            return
        d = os.open(path.parent if parent is None else parent,
                    os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    except OSError as exc:
        raise DurableWriteNotSynced(path, exc) from exc


def durable_create(path: Path, payload: bytes | str, *, mode: int | None = 0o644,
                   min_free_bytes: int = 1 << 20, dir_fd: int | None = None) -> bool:
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

    `dir_fd`: as for `durable_write` -- a bare name inside the caller's open
    directory, every step relative to it (the existence check is a no-follow
    `stat` of the name).
    """
    path, kw, parent = _place(path, dir_fd)
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if _lexists(path, kw):
        raise FileExistsError(errno.EEXIST,
                              "a durable create never replaces an existing file", str(path))
    _precheck(path, len(data) + min_free_bytes, "refused before writing", **kw)

    tmp_path = _temp_name(path, parent)
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                     0o666 if mode is None else 0o600, **kw)
    except OSError as exc:
        if exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise

    try:
        _write_all(fd, data, path)
        if mode is not None:
            os.fchmod(fd, mode)
        os.fsync(fd)
        if dir_fd is None:
            os.link(tmp_path, path)
        else:
            os.link(tmp_path, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException as exc:
        _abandon_own_temp(fd, tmp_path, dir_fd=dir_fd)
        if isinstance(exc, OSError) and not isinstance(exc, DiskFullError) \
                and exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(path, len(data), None, os.strerror(exc.errno)) from exc
        raise

    # Published: `path` and the temp name are two links to one inode. Dropping
    # the temp name removes no bytes. `_abandon_own_temp` closes `fd`.
    dropped = _abandon_own_temp(fd, tmp_path, dir_fd=dir_fd)
    _fsync_parent(path, dir_fd=dir_fd, parent=parent)
    return dropped


def durable_append(path: Path, line: bytes | str, *,
                   min_free_bytes: int = 1 << 20, single_link: bool = False,
                   dir_fd: int | None = None, create_mode: int = 0o666) -> None:
    """Append one complete line to `path`, or raise.

    With `single_link` the file is opened with `O_NOFOLLOW` and then checked
    on the descriptor (`_require_single_link`): a symbolic
    link, a file that is not regular and a file with more than one name are
    `UnsafeRecordFile` before a byte is written (the seal ledger, whose
    appends must never reach a file outside the run). Without it nothing
    changes for the other callers (telemetry, the succession log).

    Does not create directories (a missing file is created with `create_mode`
    before the umask, `0o666` as `open(path, "a")` gives it). A newline is
    added if missing. The
    free-space pre-check runs before the file is opened, so a refusal leaves
    it untouched (not even created). If the file ends in a torn tail (no
    final newline, left by an earlier failed append), the line is prefixed
    with a newline in the same write, so it is never glued onto the fragment.
    Exactly one `os.write`: a short count is `DiskFullError` and is not
    retried; ENOSPC/EDQUOT/EFBIG are `DiskFullError`. Then fsync the file and
    its directory; a directory-fsync failure is `DurableWriteNotSynced` (the
    line is in place). Nothing is truncated; earlier lines are never touched.

    `dir_fd`: as for `durable_write` -- a bare name inside the caller's open
    directory, the file opened relative to it.

    Precondition: the torn-tail check and the write are two system calls, so
    the guard is exact only for a single appender or under the caller's lock.
    With concurrent appenders each record is still one `O_APPEND` write and
    never interleaves inside, but a record racing a torn fragment can still
    be glued onto it.
    """
    path, kw, parent = _place(path, dir_fd)
    data = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if not data.endswith(b"\n"):
        data += b"\n"
    _precheck(path, len(data) + 1 + min_free_bytes, "refused before opening", **kw)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
    if single_link:
        flags |= os.O_NOFOLLOW          # (a FIFO opens O_RDWR without waiting; the check refuses it)
    try:
        fd = os.open(path, flags, create_mode, **kw)
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
    _fsync_parent(path, dir_fd=dir_fd, parent=parent)


def require_room(directory: Path | int, needed_bytes: int, *,
                 min_free_bytes: int = 1 << 20) -> None:
    """Refuse, before anything is created, when `directory` has too little room.

    `f_bavail * f_frsize` of `os.statvfs(directory)` against `needed_bytes`
    plus a reserve: the formula of `durable_write`'s pre-check, for a caller
    that is about to write a tree. A refusal is `DiskFullError`; nothing is
    created either way. `directory` may be an open directory descriptor; the
    refusal then names `/proc/self/fd/<n>`.
    """
    st = os.statvfs(directory)
    free = st.f_bavail * st.f_frsize
    needed = needed_bytes + min_free_bytes
    if free < needed:
        where = f"/proc/self/fd/{directory}" if isinstance(directory, int) else directory
        raise DiskFullError(where, needed, free, "refused before writing")


def _raise_walk_error(exc: OSError) -> None:
    """`os.walk` skips a directory it cannot read unless `onerror` raises."""
    raise exc


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _depth(path: Path, root: Path) -> int:
    return len(path.relative_to(root).parts)


def fsync_tree(root: Path) -> None:
    """Make a tree that was just written durable: every regular file, then
    every directory, deepest first, `root` last. Writes nothing.

    The walk raises on a directory it cannot read, so a tree that cannot be
    read back completely is an error, not a smaller tree. Only a regular file
    (by `lstat`, and again on the descriptor) is opened -- with `O_NOFOLLOW`
    and `O_NONBLOCK`; a symbolic link, FIFO, socket or device is skipped
    (opening a FIFO for reading blocks for ever, and an archive may hold one).
    A space errno from any fsync is `DiskFullError` (delayed allocation
    reports ENOSPC there); any other error is raised as it is.
    """
    root = Path(root)
    try:
        files: list[Path] = []
        dirs: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise_walk_error):
            here = Path(dirpath)
            dirs.append(here)
            for name in (*dirnames, *filenames):
                entry = here / name
                if stat.S_ISREG(os.lstat(entry).st_mode):
                    files.append(entry)
        for entry in files:
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fsync(fd)
            finally:
                os.close(fd)
        for here in sorted(dirs, key=lambda d: (-_depth(d, root), str(d))):
            _fsync_dir(here)
    except OSError as exc:
        if not isinstance(exc, DiskFullError) and exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(root, 0, None, os.strerror(exc.errno)) from exc
        raise


def _staged_entries(staging: Path) -> list[Path]:
    """Relative names of everything in `staging` that is not a real directory.

    A symbolic link to a directory is listed by `os.walk` with the directories
    and not descended into; it is a name to move here, never a tree to follow.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(staging, onerror=_raise_walk_error):
        here = Path(dirpath)
        for name in dirnames:
            if (here / name).is_symlink():
                found.append((here / name).relative_to(staging))
        for name in filenames:
            found.append((here / name).relative_to(staging))
    return found


def _staged_directories(staging: Path) -> list[Path]:
    """Relative names of every real directory in `staging`, empty ones included,
    parents before children. A symbolic link to a directory is not one (it is a
    name to move, see `_staged_entries`). The walk raises on a directory it
    cannot read."""
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(staging, onerror=_raise_walk_error):
        here = Path(dirpath)
        for name in dirnames:
            if not (here / name).is_symlink():
                found.append((here / name).relative_to(staging))
    return sorted(found, key=lambda r: (len(r.parts), r.as_posix()))


def _nearest_existing(path: Path) -> Path:
    while not os.path.lexists(path):
        path = path.parent
    return path


def _check_merge_destination(target: Path, rel: Path) -> None:
    """Refuse, before the first replace, a destination that a replace could not
    or must not use: a link or a non-directory among the parents, or a
    directory where a file is to go."""
    here = target
    for part in rel.parent.parts:
        here = here / part
        if here.is_symlink() or (os.path.lexists(here) and not here.is_dir()):
            raise NotADirectoryError(errno.ENOTDIR, "a link or a file where a "
                                     "directory is needed", str(here))
        if not os.path.lexists(here):
            return
    dest = target / rel
    if dest.is_dir() and not dest.is_symlink():
        raise IsADirectoryError(errno.EISDIR, "a directory where a file is to "
                                "be replaced", str(dest))


def durable_publish_tree(staging: Path, target: Path, *, merge: bool = False,
                         last: tuple[str, ...] = ()) -> None:
    """Publish a complete staging tree at `target`, whole or not at all.

    `staging` is a finished directory beside `target` (the same filesystem)
    that the caller has written; a link or a non-directory is
    `NotADirectoryError`. It is made durable first (`fsync_tree`). Then:

    * `target` absent, or an empty directory (not a link): one `os.rename`,
      then the parent is fsynced (`DurableWriteNotSynced` on failure, the tree
      in place).
    * `target` is a non-empty directory, a link or a file and `merge` is False:
      `FileExistsError`, nothing touched.
    * `merge` and a non-empty directory: every staged file is replaced onto its
      name in `target`, one by one, the relative names in `last` after all the
      others, so a crash between two replaces pairs old state with at most
      newer receipts. Files of `target` the tree does not hold stay. Every
      destination is checked first (`NotADirectoryError` for `target` or a
      parent that is a link or not a directory, `IsADirectoryError` for a
      directory in the way, `NotADirectoryError` for a staged directory over a
      link or a non-directory); a failed check leaves `target` untouched. So
      does the writability check: every existing directory that a replace or a
      `mkdir` will write into must allow it (`PermissionError`), or a live
      directory of mode 0o500 would fail a replace half way through the tree.
      The staging tree is made owner-rwx first, because a rename out of a
      directory needs write permission on it and the extraction gave the
      directories the backup's modes. Missing directories (empty ones of the
      backup included) are created owner-only before the first replace; only
      after the last replace do the directories created here get the modes the
      staging tree had, deepest first, so a directory of mode 0o500 holds all
      its files by then. A directory that already exists keeps its own mode.
      Afterwards every directory from each file's parent up to `target`, and
      every one created, is fsynced, deepest first (`DurableWriteNotSynced` on
      failure).

    This function removes nothing: after a merge `staging` is a skeleton of
    empty directories.
    """
    staging = Path(staging)
    target = Path(target)
    if staging.is_symlink() or not staging.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "the staging tree is a link or "
                                 "not a directory", str(staging))
    fsync_tree(staging)

    if not os.path.lexists(target) or (
            not target.is_symlink() and target.is_dir() and not os.listdir(target)):
        os.rename(staging, target)
        _fsync_parent(target)
        return
    if not merge:
        raise FileExistsError(errno.EEXIST, "a published tree never replaces a "
                              "non-empty target", str(target))
    if target.is_symlink() or not target.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "the target is a link or not a "
                                 "directory", str(target))

    names = sorted(_staged_entries(staging), key=lambda r: r.as_posix())
    tail = [Path(n) for n in last]
    ordered = [r for r in names if r not in tail] + [r for r in tail if r in names]
    for rel in ordered:
        _check_merge_destination(target, rel)

    directories = _staged_directories(staging)
    for rel in directories:
        here = target
        for part in rel.parts:
            here = here / part
            if not os.path.lexists(here):
                break
            if here.is_symlink() or not here.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "a link or a file where "
                                         "a staged directory is needed", str(here))

    # Writability, before anything changes: a replace writes into the parent of
    # its file, a mkdir into the parent of the directory it creates.
    written_into: set[Path] = set()
    for rel in ordered:
        written_into.add(_nearest_existing(target / rel.parent))
    for rel in directories:
        if not os.path.lexists(target / rel):
            written_into.add(_nearest_existing(target / rel.parent))
    for here in sorted(written_into, key=str):
        if not os.access(here, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, "a directory the merge must "
                                  "write into does not allow it", str(here))

    # The staging tree becomes owner-rwx (parents first), the modes it had are
    # kept for the directories this merge creates.
    modes: dict[Path, int] = {}
    for rel in (Path("."), *directories):
        here = staging / rel
        mode = stat.S_IMODE(os.lstat(here).st_mode)
        modes[rel] = mode
        if mode & 0o700 != 0o700:
            os.chmod(here, mode | 0o700)

    created: list[Path] = []
    try:
        for rel in directories:
            if not os.path.lexists(target / rel):
                os.mkdir(target / rel, 0o700)
                created.append(rel)
        for rel in ordered:
            os.makedirs(target / rel.parent, exist_ok=True)
    except OSError as exc:
        if exc.errno in _SPACE_ERRNOS:
            raise DiskFullError(target, 0, None, os.strerror(exc.errno)) from exc
        raise
    for rel in ordered:
        os.replace(staging / rel, target / rel)
    for rel in sorted(created, key=lambda r: (-len(r.parts), r.as_posix())):
        os.chmod(target / rel, modes[rel])

    touched = {target}
    for rel in ordered:
        here = target / rel.parent
        while here != target:
            touched.add(here)
            here = here.parent
    for rel in created:
        here = target / rel
        while here != target:
            touched.add(here)
            here = here.parent
    try:
        for here in sorted(touched, key=lambda d: (-_depth(d, target), str(d))):
            _fsync_dir(here)
    except OSError as exc:
        raise DurableWriteNotSynced(target, exc) from exc


def _abandon_own_temp(fd: int, tmp_path: Path, *, dir_fd: int | None = None) -> bool:
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
    is kept. With `dir_fd` (the caller's own, already checked directory) the
    parent of the temp is not opened by path at all: the comparison and the
    unlink are relative to that descriptor, which is left open.

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
    opened = None
    try:
        name = tmp_path.name
        if dir_fd is None:
            opened = dir_fd = os.open(tmp_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        own = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (own.st_dev, own.st_ino) == (current.st_dev, current.st_ino):
            os.unlink(name, dir_fd=dir_fd)
            removed = True
    except Exception:
        # Abandoning must never hide the error that caused it.
        pass
    finally:
        for f in (fd, opened):
            if f is not None:
                try:
                    os.close(f)
                except OSError:
                    pass
    return removed
