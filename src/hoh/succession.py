"""Session succession and the leader lease: who may drive a program *now*.

V3.3 P2-07 (session succession) and P2-09 (leader lease), on top of P2-08's
epoch chain (`hoh.epoch`). The plan's design, `04_DURABLE_PROGRAM_SUPERVISOR`
section 7:

    Master N -> durable handoff -> leader lease transfer -> Master N+1
    persist: master_session_epoch, leader_session_id, leader_lease,
             program_generation
    Old master returns: stale epoch -> no authoritative write

The thing this module exists to make true is the last line. `hoh.epoch` could
already say "epoch 5 is held by X"; nothing asked it before writing. A master
that has been replaced -- killed and restarted from an old context, rolled over
deliberately, or simply paused long enough that a successor took over -- could
keep writing, and one more write from a stale master is exactly how duplicate
accepted work appears.

**Who the master is does not matter here, and that is deliberate.** A Claude
Code session, a Codex session, a Herdr pane, a cron job or a Python process may
all be the master; being the master is a *registered status*, not a
description. A session is the master when the epoch chain and this lease say
so, and only then. Conversely, an external harness resuming its own session
(`claude --resume`) makes nobody the master: it gets a process back, not
authority, and the first authoritative write it attempts from its old epoch is
refused.

Three pieces:

* `Lease` -- the current leader, its epoch, the program generation, when the
  lease runs out, and enough about the holder (pid, boot id, host) to tell a
  *lost* holder from a *live* one without waiting out the whole lease.
* `MasterAuthority` -- one session's handle: `acquire` (begins an epoch and
  takes the lease), `check` (raise `StaleMaster` unless this session still
  holds exactly its epoch and a live lease), `renew`, `handoff` to a named
  successor, `release`.
* `stamp` -- puts this session's epoch on a state before it is written, so the
  store's own fence (`ProjectStore.write_state`) can refuse a stale epoch even
  if a caller forgot to `check`.

What counts as a lost holder, in order: a lease that was released; a holder on
another boot (the machine rebooted: DS2); a holder on this boot and host whose
pid no longer exists (killed: DS1); a lease past `expires_at`. A holder that is
alive and within its lease is never displaced -- two live masters is the split
brain this module exists to prevent, and the only way past a live holder is
that holder's own `handoff`.

Nothing is deleted. Every acquisition, handoff and release is appended to
`lease.log.jsonl`; the epoch chain keeps every epoch; `lease.json` is replaced
atomically.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ConfigDict

from .contracts import Strict
from .durable_io import durable_append
from .epoch import EpochStore, MasterEpoch, Reason, authority_problems
from .store import StaleWrite, _atomic_write

SCHEMA = "hoh-lease/1"
FILENAME = "lease.json"
LOG = "lease.log.jsonl"
LOCKNAME = ".authority.lock"

#: Default lease length. Long enough that a master renewing on every write never
#: lapses during an ordinary round; short enough that a master that vanished
#: without trace (on another host, where its pid cannot be asked) is replaced
#: within the hour rather than never.
DEFAULT_TTL_SECONDS = 1800


class LeaseError(RuntimeError):
    """A lease request that is refused for a reason other than staleness."""


class LeaseHeld(LeaseError):
    """Another live session holds the lease. Displacing it would be two
    masters; the way past a live holder is that holder's handoff."""


class HandoverGone(LeaseError):
    """A conditional acquire found the lease on disk is not the handover it
    was asked to take. Nothing was written: no epoch began, no lease changed,
    no log line was appended."""


class StaleMaster(StaleWrite):
    """This session is not (or no longer) the master and may not write.

    A subclass of `StaleWrite` so every store path that already refuses a
    stale writer refuses this one too; a distinct class so a controller can
    report "superseded" rather than "unclassifiable" -- the state is known, it
    just belongs to somebody else now.
    """


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _plus(ts: str, seconds: int) -> str:
    return (_parse(ts) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def boot_id() -> str:
    """This boot's identity, or "" where the platform does not expose one."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def pid_namespace() -> str:
    """This process's pid namespace, or "" where it cannot be read.

    A pid means something only inside its namespace: a master started under
    `unshare --pid` has a small pid that the host's /proc does not know (or
    knows as some other process). Judging such a holder by its pid would call
    a live master dead -- review A, finding P7.
    """
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def pid_start(pid: int) -> str:
    """The start time of `pid` in clock ticks since boot, or "" if unknown.

    Together with the pid it names one process: a pid reused by a later
    process after the holder died has a different start time.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    # Field 22; the command name (field 2) may contain spaces and parentheses.
    fields = stat[stat.rfind(")") + 2:].split()
    return fields[19] if len(fields) > 19 else ""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ExpectedHandover:
    """The one handover a conditional `acquire` may take.

    `master_epoch` is the epoch the handing-over master held, and
    `token_sha256` the digest of its token, as the capsule recorded them before
    the handover. The successor is always the acquiring handle's own session.
    """

    master_epoch: int
    token_sha256: str


class Lease(Strict):
    """The current leader of one program. Frozen: a lease changes by being
    replaced, and the replacement is logged."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = SCHEMA
    leader_session_id: str
    master_epoch: int
    program_generation: int = 1
    acquired_at: str
    renewed_at: str
    expires_at: str
    holder_pid: int = 0
    holder_boot_id: str = ""
    holder_host: str = ""
    #: The holder's pid namespace and process start time: the pid is judged
    #: only in the same namespace, and only as the same process.
    holder_pidns: str = ""
    holder_start: str = ""
    #: sha256 of the token handed to the acquiring session. A CLI command that
    #: acts as this master must present the token; the session id and epoch
    #: are printed by `hoh project authority` and are therefore not a
    #: credential. This guards against a confused or stale session, not
    #: against a hostile process of the same OS user -- which could edit these
    #: files directly, and against which no same-user file protocol can hold.
    token_sha256: str = ""
    #: Set when the holder gave the lease up. A released lease has no leader.
    released_at: str | None = None
    released_reason: str = ""
    #: The session the holder handed over to, if it named one. Only that
    #: session may take a handed-over lease before it expires.
    successor: str | None = None


class MasterAuthority:
    """One session's claim to drive one program.

    `directory` is the program's own directory (for a project,
    `<root>/projects/<id>`); the epoch chain and the lease live there, next to
    the state they govern, so a copy of the directory carries its authority
    history with it.

    Every environmental fact is injectable -- the clock, this boot's id, this
    host, this pid, and how to ask whether a pid is alive -- because DS1 and
    DS2 are exactly about those facts changing, and a test has to be able to
    say "the machine rebooted" without rebooting it. The defaults are the real
    ones.
    """

    def __init__(self, directory: Path | str, session_id: str, *,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], str] = utc_now,
                 boot: Callable[[], str] = boot_id,
                 host: str | None = None,
                 pid: int | None = None,
                 alive: Callable[[int], bool] = pid_alive,
                 serialize: Callable[[], AbstractContextManager] | None = None,
                 pidns: Callable[[], str] = pid_namespace,
                 start: Callable[[int], str] = pid_start) -> None:
        session_id = str(session_id).strip()
        if not session_id:
            raise LeaseError("a master needs a session id; an anonymous master "
                             "cannot be told apart from its successor")
        if ttl_seconds <= 0:
            raise LeaseError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self.dir = Path(directory)
        self.session_id = session_id
        self.ttl = int(ttl_seconds)
        self.clock = clock
        self.boot = boot
        self.host = host if host is not None else socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self.alive = alive
        self.pidns = pidns
        self.start = start
        #: The secret handed out by `acquire`; None for a handle that did not
        #: acquire. `present(token)` sets it on a handle built from a CLI
        #: credential.
        self.token: str | None = None
        self._keepalive_stop: threading.Event | None = None
        #: The program's own short write lock, taken *around* every change of
        #: authority. With it, acquiring and a state write are serialised: a
        #: predecessor's write either lands before the successor reads the
        #: state, or it is checked after the successor's epoch began and is
        #: refused. Lock order is always program lock, then authority lock.
        self.serialize = serialize or nullcontext
        self.epochs = EpochStore(self.dir)
        #: The epoch this handle acquired. None until `acquire` succeeds; a
        #: handle that never acquired has no authority to check.
        self.epoch: int | None = None
        self.program_generation: int = 1

    # --- files ------------------------------------------------------------

    @property
    def lease_path(self) -> Path:
        return self.dir / FILENAME

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """A short-held lock around read-decide-write of the lease.

        Separate from the program's own lock: that one is held for a whole
        drive, and acquiring authority must be possible to *ask* while a drive
        runs -- and to refuse, rather than block, while the holder is live.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.dir / LOCKNAME), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def read_lease(self) -> Lease | None:
        if not self.lease_path.is_file():
            return None
        try:
            return Lease.model_validate_json(self.lease_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LeaseError(f"{self.lease_path} is not a readable lease: {exc}") from exc

    def _write(self, lease: Lease, event: str, **extra: Any) -> None:
        _atomic_write(self.lease_path, lease.model_dump_json(indent=2) + "\n")
        entry = {"at": lease.renewed_at if event == "renew" else self.clock(),
                 "event": event, "session": self.session_id,
                 "epoch": lease.master_epoch, "pid": self.pid,
                 "boot_id": self.boot(), **extra}
        # P2-41b W13: one complete line, file and directory fsynced, a torn
        # tail terminated first; exact under `_locked()`, which every caller
        # holds. A refusal here comes after the lease was written (today's
        # order, pinned and tracked as Q3).
        durable_append(self.dir / LOG, json.dumps(entry, sort_keys=True) + "\n")

    # --- judging a lease --------------------------------------------------

    def holder_state(self, lease: Lease | None, now: str) -> tuple[str, str]:
        """(state, why) for a lease: NONE, RELEASED, LOST or LIVE."""
        if lease is None:
            return "NONE", "no lease has ever been written"
        if lease.released_at:
            return "RELEASED", (f"released by {lease.leader_session_id!r} at "
                                f"{lease.released_at}: {lease.released_reason}")
        here = self.boot()
        if lease.holder_boot_id and here and lease.holder_boot_id != here:
            return "LOST", (f"holder {lease.leader_session_id!r} was on boot "
                            f"{lease.holder_boot_id[:8]}, this is boot {here[:8]}: "
                            f"the machine restarted")
        same_process_space = (
            lease.holder_host == self.host and lease.holder_pid
            and (not lease.holder_boot_id or lease.holder_boot_id == here)
            and lease.holder_pidns and lease.holder_pidns == self.pidns())
        if same_process_space:
            if not self.alive(lease.holder_pid):
                return "LOST", (f"holder {lease.leader_session_id!r} (pid "
                                f"{lease.holder_pid}) no longer exists")
            now_start = self.start(lease.holder_pid)
            if lease.holder_start and now_start and now_start != lease.holder_start:
                return "LOST", (f"holder {lease.leader_session_id!r}: pid "
                                f"{lease.holder_pid} now belongs to another process")
        if lease.expires_at <= now:
            return "LOST", (f"lease of {lease.leader_session_id!r} expired at "
                            f"{lease.expires_at}")
        return "LIVE", (f"held by {lease.leader_session_id!r} (epoch "
                        f"{lease.master_epoch}, pid {lease.holder_pid}) until "
                        f"{lease.expires_at}")

    # --- the four verbs ---------------------------------------------------

    def acquire(self, *, program_generation: int = 1, note: str = "",
                reason: Reason | None = None,
                only_handover: ExpectedHandover | None = None) -> MasterEpoch:
        """Become the master: begin a new epoch and take the lease.

        Always a new epoch, even for a session that held the previous one --
        a restarted process cannot prove it is the same master that stopped,
        and "the same id" is exactly what a stale context would also present.
        The one exception is a handle that already holds a live lease in this
        very process, for which acquiring again is a no-op.

        **Conditional acquire (`only_handover`).** A successor that has verified
        one handover must take *that* handover and nothing else. Between its
        last look and the acquire the world can move (the handover lapses and
        the predecessor takes the program back), and an unconditional acquire
        would then begin an epoch over somebody else's lease. With
        `only_handover` the lease on disk is compared with the expectation
        **under this same lock, immediately before the epoch begins**: it must
        be a released lease whose successor is this session, whose token digest
        and epoch are the expected ones, that has not expired, over an epoch
        chain whose current epoch is the expected one. Anything else raises
        `HandoverGone` with nothing written. The re-entrant no-op above does
        not apply to a conditional acquire.
        """
        if only_handover is not None:
            if not isinstance(only_handover, ExpectedHandover):
                raise LeaseError("only_handover must be an ExpectedHandover, got "
                                 f"{type(only_handover).__name__}")
            if reason is not None and reason is not Reason.SUCCESSION:
                raise LeaseError("a conditional acquire takes a handover: its "
                                 "reason is SUCCESSION")
            if (not isinstance(only_handover.token_sha256, str)
                    or not only_handover.token_sha256
                    or isinstance(only_handover.master_epoch, bool)
                    or not isinstance(only_handover.master_epoch, int)):
                raise LeaseError("a conditional acquire names the epoch and the "
                                 "token digest of the handover it takes")
        with self.serialize(), self._locked():
            now = self.clock()
            lease = self.read_lease()
            state, why = self.holder_state(lease, now)
            current = self.epochs.current()
            if only_handover is not None:
                gone = self._handover_problem(only_handover, lease, current, now)
                if gone:
                    raise HandoverGone(f"the handover is not there to take: {gone}")
            if (state == "LIVE" and lease is not None
                    and lease.leader_session_id == self.session_id
                    and lease.holder_pid == self.pid
                    and self.epoch == lease.master_epoch):
                return current  # type: ignore[return-value]
            if state == "LIVE":
                raise LeaseHeld(f"cannot acquire: {why}")
            if state == "RELEASED" and lease is not None and lease.successor \
                    and lease.successor != self.session_id \
                    and lease.expires_at > now:
                raise LeaseHeld(
                    f"cannot acquire: {lease.leader_session_id!r} handed over to "
                    f"{lease.successor!r}, not to {self.session_id!r}; the handover "
                    f"stands until {lease.expires_at}")
            if current is None:
                reason = Reason.INITIAL
            elif state == "RELEASED" and lease is not None \
                    and lease.successor == self.session_id:
                reason = Reason.SUCCESSION
            elif reason is None or reason is Reason.INITIAL:
                reason = Reason.RECOVERY
            epoch = self.epochs.begin(
                self.session_id, reason=reason, now=now,
                note=(note or why)[:300])
            token = secrets.token_hex(16)
            new = Lease(leader_session_id=self.session_id, master_epoch=epoch.epoch,
                        program_generation=int(program_generation),
                        acquired_at=now, renewed_at=now,
                        expires_at=_plus(now, self.ttl), holder_pid=self.pid,
                        holder_boot_id=self.boot(), holder_host=self.host,
                        holder_pidns=self.pidns(), holder_start=self.start(self.pid),
                        token_sha256=token_digest(token))
            self._write(new, "acquire", reason=reason.value, previous=why)
            self.epoch = epoch.epoch
            self.token = token
            self.program_generation = new.program_generation
            return epoch

    def _handover_problem(self, want: ExpectedHandover, lease: Lease | None,
                          current: MasterEpoch | None, now: str) -> str:
        """Why `lease` is not exactly the handover `want` to this session, or ""."""
        if lease is None:
            return "no lease exists"
        if not lease.released_at:
            return (f"the lease is held by {lease.leader_session_id!r}, not handed "
                    f"over")
        if lease.successor != self.session_id:
            return (f"it was handed to {lease.successor!r}, not to "
                    f"{self.session_id!r}")
        if lease.token_sha256 != want.token_sha256:
            return "it is the handover of another token"
        if lease.master_epoch != want.master_epoch:
            return (f"it is the handover of epoch {lease.master_epoch}, not "
                    f"{want.master_epoch}")
        if lease.expires_at <= now:
            return f"the handover lapsed at {lease.expires_at}"
        if current is None or current.epoch != want.master_epoch:
            return (f"the epoch chain is at "
                    f"{current.epoch if current else None}, not {want.master_epoch}")
        return ""

    def present(self, *, epoch: int, token: str) -> None:
        """Make this handle act as the master that acquired `epoch`, on the
        strength of the token that acquisition handed out (for a CLI command
        issued on the master's behalf). `problems()` then also requires the
        token to match the lease."""
        self.epoch = int(epoch)
        self.token = token

    def problems(self) -> list[str]:
        """Why this session may not write now, or []."""
        if self.epoch is None:
            return [f"{self.session_id!r} never acquired authority over {self.dir}"]
        out = authority_problems(self.epochs, master_id=self.session_id,
                                 claimed_epoch=self.epoch)
        if out:
            return out
        now = self.clock()
        try:
            lease = self.read_lease()
        except LeaseError as exc:
            return [str(exc)]
        if lease is None:
            return ["the epoch is current but no lease exists; authority without a "
                    "lease is not authority"]
        if lease.leader_session_id != self.session_id or lease.master_epoch != self.epoch:
            return [f"the lease names {lease.leader_session_id!r} at epoch "
                    f"{lease.master_epoch}, not {self.session_id!r} at {self.epoch}"]
        if lease.token_sha256 and (self.token is None
                                   or token_digest(self.token) != lease.token_sha256):
            return [f"{self.session_id!r} does not hold the token of epoch {self.epoch}; "
                    f"knowing a session id and an epoch is not being the master"]
        if lease.released_at:
            return [f"this session released its lease at {lease.released_at} "
                    f"({lease.released_reason}); it may no longer write"]
        if lease.expires_at <= now:
            return [f"this session's lease expired at {lease.expires_at}; it must "
                    f"acquire again, which begins a new epoch"]
        return []

    def check(self) -> None:
        """Raise `StaleMaster` unless this session may write right now."""
        problems = self.problems()
        if problems:
            raise StaleMaster("stale master: " + "; ".join(problems))

    def renew(self) -> Lease:
        with self._locked():
            self.check()
            lease = self.read_lease()
            assert lease is not None
            now = self.clock()
            new = lease.model_copy(update={"renewed_at": now,
                                           "expires_at": _plus(now, self.ttl)})
            _atomic_write(self.lease_path, new.model_dump_json(indent=2) + "\n")
            return new

    @contextmanager
    def keepalive(self, *, interval: float | None = None) -> Iterator[None]:
        """Renew the lease in the background for as long as the block runs.

        A master whose only renewal is its next state write loses its lease
        during any dispatch longer than the TTL, and is then displaced while
        still working -- review A, finding P6. The renewal thread stops at the
        first refusal (the master was superseded: the main flow will be refused
        at its next authoritative act) and at the end of the block. A frozen
        process freezes its thread with it, so a frozen master still lapses.
        Only a refusal (`StaleMaster`) ends the thread; I/O errors are retried.
        """
        stop = threading.Event()
        period = interval if interval is not None else max(1.0, self.ttl / 3)

        def run() -> None:
            while not stop.wait(period):
                try:
                    self.renew()
                except StaleMaster:
                    return          # superseded: the main flow is refused next
                except Exception:                     # noqa: BLE001 - retried
                    # Transient -- a full disk, a lock hiccup. One such error
                    # used to end renewal for the rest of the drive and let a
                    # live master lapse (review A2, a); the next period retries.
                    continue

        thread = threading.Thread(target=run, name="hoh-lease-keepalive", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=period + 5)

    def handoff(self, successor: str, *, reason: str,
                expires_in: int | None = None) -> Lease:
        """Durable handoff: release the lease to one named successor.

        After this returns, this session can no longer write -- `check` fails
        on the released lease -- and only `successor` may acquire until the
        lease would have expired.

        `expires_in` bounds how long the handover stands, in seconds. Without
        it the handover stands for this handle's whole ttl, so a successor
        that took the handover and then died would wedge the program for that
        long (1800 s by default); with it the handing-over master can take the
        program back as soon as the window has passed. A bound that is not a
        positive integer is refused, never read as "no bound".
        """
        successor = str(successor).strip()
        if not successor or successor == self.session_id:
            raise LeaseError("a handoff names a successor other than the master itself")
        if not reason.strip():
            raise LeaseError("a handoff says why")
        if expires_in is not None and (isinstance(expires_in, bool)
                                       or not isinstance(expires_in, int)
                                       or expires_in <= 0):
            raise LeaseError(f"expires_in must be a positive number of seconds, "
                             f"got {expires_in!r}")
        with self.serialize(), self._locked():
            self.check()
            lease = self.read_lease()
            assert lease is not None
            now = self.clock()
            new = lease.model_copy(update={"released_at": now,
                                           "released_reason": f"handoff: {reason}",
                                           "successor": successor,
                                           "expires_at": _plus(
                                               now, self.ttl if expires_in is None
                                               else int(expires_in))})
            self._write(new, "handoff", successor=successor, reason=reason)
            return new

    def release(self, *, reason: str) -> Lease:
        """Give the lease up with no successor (e.g. the program is terminal)."""
        with self.serialize(), self._locked():
            self.check()
            lease = self.read_lease()
            assert lease is not None
            now = self.clock()
            new = lease.model_copy(update={"released_at": now,
                                           "released_reason": reason or "released"})
            self._write(new, "release", reason=reason)
            return new

    def stamp(self, state: Any) -> Any:
        """Put this session's epoch on a state about to be written.

        The store fence compares it with what is on disk; `check` first, so a
        stamp is never the thing that lets a stale master through.
        """
        self.check()
        state.master_epoch = self.epoch
        return state


def status(directory: Path | str, *, clock: Callable[[], str] = utc_now) -> dict:
    """A read-only report of who holds a program, for `hoh project authority`."""
    probe = MasterAuthority(directory, "status-reader", clock=clock)
    lease = probe.read_lease()
    state, why = probe.holder_state(lease, clock())
    try:
        chain = probe.epochs.chain()
        fsck: list[str] = []
    except Exception as exc:  # noqa: BLE001 - reported
        chain, fsck = [], [str(exc)]
    return {
        "holder_state": state,
        "why": why,
        "lease": json.loads(lease.model_dump_json()) if lease else None,
        "epochs": [{"epoch": e.epoch, "master_id": e.master_id,
                    "reason": e.reason.value, "began_at": e.began_at} for e in chain],
        "fsck": fsck,
    }
