"""WorkspaceLease: one writer per worktree, persisted (V3.3 P3-06, slice 1).

Until now a worktree was protected only by the run that was started in it: the
run lock (`RunStore.lock`, `ProjectStore.lock`) makes one controller per *run*,
and nothing says that two runs, two projects or an operator and a run are not
pointed at the same worktree. Both would `git add -A` into it, and the second
`commit_candidate` would sweep up the first one's unverified work -- the failure
`commit_candidate`'s own docstring describes for a main checkout, one level
down. The lease is the record that closes that: one holder per worktree, on
disk, judged the way the leader lease (`hoh.succession`) judges a master.

What it is: a small JSON record in the worktree's own git directory
(`workspace.lease_directory`), replaced atomically and appended to a log, with
the holder's pid, boot, host and process start so that a holder that is gone can
be told from one that is working.

What counts as a lost holder, in order: a lease that was released; a holder on
another boot; a holder on this boot, host and pid namespace whose pid no longer
exists or now belongs to another process; a lease past `expires_at`. A holder
that is alive and within its lease is **never** displaced -- the way past a live
holder is that holder's own `release`. Anything the module cannot read is
**not** "no lease": a lease file that is a link, a FIFO, a directory, larger
than `MAX_RECORD_BYTES`, not UTF-8, not one JSON object with exactly the
fields below, or whose values break a rule, refuses every operation, and nothing
is written.

Slice 1 stops at the lease and the guard in `workspace.commit_candidate`. It
does not start or renew anything by itself, does not decide who asks (the
controller, the launcher and a future wave executor wire it), and does not
create worktrees.

The one environmental seam is the same as the leader lease's: the clock, the
boot id, the host, the pid and how to ask whether a pid lives are injectable,
so a test can say "the machine rebooted" without rebooting it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import socket
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import durable_io
from .succession import boot_id, pid_alive, pid_namespace, pid_start, token_digest, utc_now
from .workspace import WorkspaceError, is_git_repo, lease_directory

SCHEMA = "hoh-workspace-lease/1"
FILENAME = "lease.json"
LOG = "lease.log.jsonl"

#: Default lease length. A holder renews on its own writes; a holder that
#: vanished without trace on another host (its pid cannot be asked) frees the
#: worktree after this long.
DEFAULT_TTL_SECONDS = 1800
#: The longest lease a handle may ask for: a day. A holder that is not asked
#: for can only be waited out, so the wait is bounded.
MAX_TTL_SECONDS = 86400
#: A record is a few hundred bytes. Anything larger is not one of ours.
MAX_RECORD_BYTES = 16384
#: Bounds of the two integers in a record: a generation counts acquisitions of
#: one worktree, a pid is a Linux pid (`pid_max` is at most 2**22, and a larger
#: number would only be an `OverflowError` waiting in `os.kill`).
MAX_GENERATION = 2**40
MAX_PID = 2**22
#: The short lock around read-decide-write is held for microseconds; a holder of
#: it that stays for 0.5 s is not about to let go.
LOCK_ATTEMPTS = 50
LOCK_PAUSE_SECONDS = 0.01

_HOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TEXT = re.compile(r"[\x20-\x7e]{0,512}")
_FIELDS = ("schema", "holder", "generation", "token_sha256", "acquired_at", "renewed_at",
           "expires_at", "holder_pid", "holder_boot_id", "holder_host", "holder_pidns",
           "holder_start", "released_at", "released_reason")


class WorkspaceLeaseError(WorkspaceError):
    """A lease operation that is refused. `reason` is one of `argument`,
    `unreadable`, `oversize`, `malformed`, `schema`, `invalid`, `busy`, `held`,
    `lost`, `elsewhere`."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class WorkspaceHeld(WorkspaceLeaseError):
    """Another live holder has the worktree. Displacing it would be two
    writers; the way past a live holder is that holder's `release`."""


class WorkspaceLeaseLost(WorkspaceLeaseError):
    """This handle does not hold the worktree (any more): never acquired,
    superseded, released, or run out."""


@dataclass(frozen=True)
class Lease:
    """The current holder of one worktree. Replaced, never edited in place."""

    holder: str
    generation: int
    token_sha256: str
    acquired_at: str
    renewed_at: str
    expires_at: str
    holder_pid: int
    holder_boot_id: str
    holder_host: str
    holder_pidns: str
    holder_start: str
    released_at: str | None = None
    released_reason: str = ""

    def to_bytes(self) -> bytes:
        body = {"schema": SCHEMA, **asdict(self)}
        return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _stamp(value: object, what: str) -> datetime:
    """A `YYYY-MM-DDTHH:MM:SSZ` time that is a real calendar time, or `invalid`."""
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise WorkspaceLeaseError("invalid", f"{what} is not a UTC time of the form "
                                             f"YYYY-MM-DDTHH:MM:SSZ: {value!r}")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise WorkspaceLeaseError("invalid", f"{what} is not a calendar time: {value!r}") from exc


def _plus(value: str, seconds: int) -> str:
    return (_stamp(value, "the clock") + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"repeated key {key!r}")
        out[key] = value
    return out


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


def parse(data: bytes) -> Lease:
    """The lease these bytes hold, or a `WorkspaceLeaseError` saying why not."""
    if not isinstance(data, bytes):
        raise WorkspaceLeaseError("malformed", "a lease record is bytes")
    if len(data) > MAX_RECORD_BYTES:
        raise WorkspaceLeaseError("oversize", f"a lease record is at most {MAX_RECORD_BYTES} "
                                              f"bytes, this one has {len(data)}")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:      # UnicodeDecodeError is a ValueError
        raise WorkspaceLeaseError("malformed", f"not a JSON document: {exc}") from exc
    if not isinstance(obj, dict):
        raise WorkspaceLeaseError("malformed", "a lease record is one JSON object")
    if obj.get("schema") != SCHEMA:
        raise WorkspaceLeaseError("schema", f"schema is {obj.get('schema')!r}, not {SCHEMA!r}")
    if set(obj) != set(_FIELDS):
        raise WorkspaceLeaseError("invalid", f"the fields are {sorted(obj)}, not {sorted(_FIELDS)}")

    def text(key: str, pattern: re.Pattern) -> str:
        value = obj[key]
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise WorkspaceLeaseError("invalid", f"{key} is not an acceptable value: {value!r}")
        return value

    def number(key: str, low: int, high: int) -> int:
        value = obj[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise WorkspaceLeaseError("invalid", f"{key} is not an integer from {low} to {high}: "
                                                 f"{value!r}")
        return value

    released = obj["released_at"]
    if released is not None:
        _stamp(released, "released_at")
    for key in ("acquired_at", "renewed_at", "expires_at"):
        _stamp(obj[key], key)
    return Lease(
        holder=text("holder", _HOLDER), generation=number("generation", 1, MAX_GENERATION),
        token_sha256=text("token_sha256", _DIGEST), acquired_at=obj["acquired_at"],
        renewed_at=obj["renewed_at"], expires_at=obj["expires_at"],
        holder_pid=number("holder_pid", 0, MAX_PID), holder_boot_id=text("holder_boot_id", _TEXT),
        holder_host=text("holder_host", _TEXT), holder_pidns=text("holder_pidns", _TEXT),
        holder_start=text("holder_start", _TEXT), released_at=released,
        released_reason=text("released_reason", _TEXT))


def holder_name(identity: str) -> str:
    """The holder name a caller with this identity (a run id) asks under: the identity itself
    when the grammar takes it, else `run-` and the first 32 hex digits of the digest of its
    ASCII form -- stable, so one identity is one holder in every process. A legal run id may
    start with `-` or be longer than 128 characters; that is never a reason to run without a
    lease."""
    text = identity if isinstance(identity, str) else str(identity)
    if _HOLDER.fullmatch(text) is not None:
        return text
    return "run-" + token_digest(ascii(text))[:32]


def _ascii(value: str) -> str:
    """What this process's own record may carry: printable ASCII, bounded."""
    return "".join(c if " " <= c <= "~" else "?" for c in str(value))[:512]


class WorkspaceLease:
    """One handle's claim to be the writer of one worktree.

    `repo` is the root of the worktree. `holder` names who asks (a run id, a
    node id): it is for the record and the log; what makes a handle the holder
    is the secret `acquire` hands it, not the name.
    """

    def __init__(self, repo: Path | str, holder: str, *,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], str] = utc_now,
                 boot: Callable[[], str] = boot_id,
                 host: str | None = None,
                 pid: int | None = None,
                 alive: Callable[[int], bool] = pid_alive,
                 pidns: Callable[[], str] = pid_namespace,
                 start: Callable[[int], str] = pid_start) -> None:
        if not isinstance(holder, str) or _HOLDER.fullmatch(holder) is None:
            raise WorkspaceLeaseError("argument", f"a holder is 1 to 128 characters of letters, "
                                                  f"digits, '.', '_' and '-': {holder!r}")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                or not 1 <= ttl_seconds <= MAX_TTL_SECONDS):
            raise WorkspaceLeaseError("argument", f"ttl_seconds is an integer from 1 to "
                                                  f"{MAX_TTL_SECONDS}: {ttl_seconds!r}")
        try:
            path = Path(repo).expanduser()
            rooted = is_git_repo(path)
        except (TypeError, ValueError, OSError) as exc:      # not a path; a NUL in the name
            raise WorkspaceLeaseError("argument", f"{repo!r} is not a path: {exc}") from exc
        if not rooted:
            raise WorkspaceLeaseError("argument", f"{path} is not the root of a git worktree")
        self.worktree = path.resolve()
        self.directory = lease_directory(self.worktree)
        self.holder = holder
        self.ttl = ttl_seconds
        self.clock = clock
        self.boot = boot
        self.host = host if host is not None else socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self.alive = alive
        self.pidns = pidns
        self.start = start
        #: The secret `acquire` handed out; None for a handle that has not.
        self.token: str | None = None

    # --- files ------------------------------------------------------------

    @property
    def lease_path(self) -> Path:
        return self.directory / FILENAME

    def _now(self) -> str:
        now = self.clock()
        _stamp(now, "the clock")
        return now

    @contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[None]:
        """A short lock around read-decide-write: `flock` on the lease
        directory itself, taken without waiting more than half a second.

        Only `acquire` creates the directory (`create=True`); a verb of a
        handle that holds nothing finds no directory and says it lost, and
        leaves the worktree's git directory exactly as it was.

        A refused lock is `busy`, never a wait without end: whoever holds it is
        inside a read-decide-write of microseconds, and one that is not is
        something to look at, not something to queue behind.
        """
        if create:
            try:
                os.mkdir(self.directory, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WorkspaceLeaseError("unreadable", f"{self.directory} cannot be made: "
                                                        f"{exc}") from exc
        try:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            if not create:
                raise WorkspaceLeaseLost("lost", f"no lease exists for {self.worktree}") from exc
            raise WorkspaceLeaseError("unreadable", f"{self.directory} is not there: {exc}") from exc
        except OSError as exc:
            raise WorkspaceLeaseError("unreadable", f"{self.directory} is not a directory of "
                                                    f"its own: {exc}") from exc
        try:
            for attempt in range(LOCK_ATTEMPTS):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise WorkspaceLeaseError("unreadable", f"{self.directory} cannot be "
                                                                f"locked: {exc}") from exc
                    if attempt == LOCK_ATTEMPTS - 1:
                        raise WorkspaceLeaseError(
                            "busy", f"{self.directory} stayed locked by another process") from exc
                    time.sleep(LOCK_PAUSE_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read(self) -> Lease | None:
        """The lease on disk; None only when there is none. Every file that is
        there and is not a good lease is a `WorkspaceLeaseError`."""
        try:
            # The directory is looked at as itself: a link to another directory
            # is not "the lease directory with nothing in it".
            kind = os.lstat(self.directory)
            if not stat.S_ISDIR(kind.st_mode):
                raise WorkspaceLeaseError("unreadable", f"{self.directory} is not a directory "
                                                        f"of its own")
            st = os.lstat(self.lease_path)
            # The size first: `read_single_link` has no bound of its own.
            if stat.S_ISREG(st.st_mode) and st.st_size > MAX_RECORD_BYTES:
                raise WorkspaceLeaseError("oversize", f"{self.lease_path} is {st.st_size} bytes; "
                                                      f"a lease is at most {MAX_RECORD_BYTES}")
            data = durable_io.read_single_link(self.lease_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceLeaseError("unreadable", f"{self.lease_path} cannot be read: "
                                                    f"{exc}") from exc
        if data is None:
            return None
        return parse(data)

    def _put(self, lease: Lease) -> None:
        durable_io.durable_write(self.lease_path, lease.to_bytes(), mode=0o600)

    def _require_log(self) -> None:
        """Before the lease is written: the log, if there is one, is a regular
        file with one name -- never a link that would carry the line out of
        the directory, never a FIFO that would fail after the lease changed."""
        try:
            st = os.lstat(self.directory / LOG)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceLeaseError("unreadable", f"{self.directory / LOG} cannot be looked "
                                                    f"at: {exc}") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise WorkspaceLeaseError("unreadable", f"{self.directory / LOG} is not a regular "
                                                    f"file with one name")

    def _log(self, event: str, lease: Lease, **extra: object) -> None:
        entry = {"at": self.clock(), "event": event, "holder": lease.holder,
                 "generation": lease.generation, "pid": self.pid, **extra}
        durable_io.durable_append(self.directory / LOG, json.dumps(entry, sort_keys=True) + "\n",
                                  single_link=True, create_mode=0o600)

    # --- judging a lease --------------------------------------------------

    def holder_state(self, lease: Lease | None, now: str) -> tuple[str, str]:
        """`(state, why)` for a lease: NONE, RELEASED, LOST or LIVE."""
        if lease is None:
            return "NONE", "no lease has been written"
        if lease.released_at:
            return "RELEASED", (f"released by {lease.holder!r} at {lease.released_at}: "
                                f"{lease.released_reason}")
        here = self.boot()
        if lease.holder_boot_id and here and lease.holder_boot_id != here:
            return "LOST", (f"holder {lease.holder!r} was on boot {lease.holder_boot_id[:8]}, "
                            f"this is boot {here[:8]}: the machine restarted")
        same_process_space = (
            lease.holder_host == self.host and lease.holder_pid
            and (not lease.holder_boot_id or lease.holder_boot_id == here)
            and lease.holder_pidns and lease.holder_pidns == self.pidns())
        if same_process_space:
            if not self.alive(lease.holder_pid):
                return "LOST", f"holder {lease.holder!r} (pid {lease.holder_pid}) no longer exists"
            now_start = self.start(lease.holder_pid)
            if lease.holder_start and now_start and now_start != lease.holder_start:
                return "LOST", (f"holder {lease.holder!r}: pid {lease.holder_pid} now belongs "
                                f"to another process")
        if _stamp(lease.expires_at, "expires_at") <= _stamp(now, "the clock"):
            return "LOST", f"lease of {lease.holder!r} expired at {lease.expires_at}"
        return "LIVE", (f"held by {lease.holder!r} (generation {lease.generation}, pid "
                        f"{lease.holder_pid}) until {lease.expires_at}")

    def _mine(self, lease: Lease) -> bool:
        return (self.token is not None and token_digest(self.token) == lease.token_sha256
                and lease.holder == self.holder and lease.holder_pid == self.pid)

    # --- the verbs --------------------------------------------------------

    def acquire(self, *, admit: Callable[[Lease | None], None] | None = None) -> Lease:
        """Become the writer of the worktree, or raise.

        A live holder is `WorkspaceHeld`, an unreadable lease is not taken over
        (`WorkspaceLeaseError`), and nothing is written in either case. A
        handle that already holds a live lease gets it back unchanged. Taking
        over a lost or released lease is a new generation, and the log says
        what it took over from.

        `admit`, when given, is called **under the short lock, after the lease
        has been judged free and before anything is written**, with the lease it
        is about to replace (None for the first one). It is how a caller asks
        "may I have this worktree as it stands?" -- who held it last is only
        known here, atomically; a caller that asked before `acquire` could be
        overtaken between the question and the answer. What it raises
        (`WorkspaceError` and its subclasses) is raised unchanged, and the
        record and the log are exactly as they were.
        """
        with self._locked(create=True):
            now = self._now()
            lease = self.read()
            state, why = self.holder_state(lease, now)
            if state == "LIVE":
                assert lease is not None
                if self._mine(lease):
                    return lease
                raise WorkspaceHeld("held", f"cannot take {self.worktree}: {why}")
            if lease is not None and lease.generation >= MAX_GENERATION:
                raise WorkspaceLeaseError(
                    "invalid", f"{self.lease_path} is at generation {lease.generation}, the last "
                               f"one a record may carry; an operator renames the directory away")
            self._require_log()
            if admit is not None:
                admit(lease)
            token = secrets.token_hex(16)
            new = Lease(
                holder=self.holder, generation=(lease.generation + 1 if lease else 1),
                token_sha256=token_digest(token), acquired_at=now, renewed_at=now,
                expires_at=_plus(now, self.ttl), holder_pid=self.pid,
                holder_boot_id=_ascii(self.boot()), holder_host=_ascii(self.host),
                holder_pidns=_ascii(self.pidns()), holder_start=_ascii(self.start(self.pid)))
            self._put(new)
            # From here the handle holds the lease, whether or not the log line
            # below lands: a refusal of the log must not leave a lease on disk
            # that its own holder does not know it has.
            self.token = token
            self._log("acquire", new, previous=why[:300])
            return new

    def check(self, worktree: Path | str | None = None) -> Lease:
        """The lease, if this handle holds the worktree right now; else raise.

        With `worktree`, also that the lease is the lease of **that**
        worktree: a handle for one worktree never authorises a write into
        another (`elsewhere`).
        """
        if worktree is not None:
            asked = Path(worktree).expanduser().resolve()
            if asked != self.worktree:
                raise WorkspaceLeaseError(
                    "elsewhere", f"this lease governs {self.worktree}, not {asked}")
        if self.token is None:
            raise WorkspaceLeaseLost("lost", f"{self.holder!r} never acquired {self.worktree}")
        lease = self.read()
        if lease is None:
            raise WorkspaceLeaseLost("lost", f"no lease exists for {self.worktree}")
        if token_digest(self.token) != lease.token_sha256:
            raise WorkspaceLeaseLost("lost", f"{self.holder!r} no longer holds {self.worktree}: "
                                             f"it is {lease.holder!r}'s, generation {lease.generation}")
        if lease.released_at:
            raise WorkspaceLeaseLost("lost", f"{self.holder!r} released {self.worktree} at "
                                             f"{lease.released_at}")
        if _stamp(lease.expires_at, "expires_at") <= _stamp(self._now(), "the clock"):
            raise WorkspaceLeaseLost("lost", f"the lease of {self.holder!r} on {self.worktree} "
                                             f"ran out at {lease.expires_at}")
        return lease

    def renew(self) -> Lease:
        """Push `expires_at` out by this handle's ttl; only the holder may."""
        with self._locked():
            lease = self.check()
            now = self._now()
            new = replace(lease, renewed_at=now, expires_at=_plus(now, self.ttl))
            self._put(new)
            return new

    def release(self, *, reason: str = "released") -> Lease:
        """Give the worktree up. Only the holder may, so a superseded handle
        cannot clobber the lease that replaced its own."""
        with self._locked():
            lease = self.check()
            self._require_log()
            new = replace(lease, released_at=self._now(), released_reason=_ascii(reason) or "released")
            self._put(new)
            self._log("release", new, reason=_ascii(reason)[:300])
            return new


__all__ = ["DEFAULT_TTL_SECONDS", "FILENAME", "LOG", "MAX_RECORD_BYTES", "MAX_TTL_SECONDS", "SCHEMA",
           "Lease", "WorkspaceHeld", "WorkspaceLease", "WorkspaceLeaseError", "WorkspaceLeaseLost",
           "holder_name", "parse"]
