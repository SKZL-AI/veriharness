"""The processes a role left running when its pane was closed.

Closing a Herdr pane ends the agent and its shell. It does **not** end a
command the agent started with `run_in_background` (measured live,
2026-10-06: the `sleep` stayed alive, orphaned). A surviving writer could still
change the tree after the freeze, so before the freeze HoH finds what the
pane's role left running, ends it, and proves it gone -- or says what is left.

How a process is found, and why it is safe
------------------------------------------

*The mark.* Every role pane is created with `HOH_ROLE_MARK=<mark>` in its
environment (`mint`). Every process the role starts inherits it, attached or
not. A mark is `<label>:<role>:<96 random bits>` and is remembered in this
process: **`scan`, `end` and `marked_left` raise `ValueError` for a mark `mint`
did not return here**, so no fixed or guessable mark is ever acted on and two
runs (or two test runs) cannot take each other's processes.

*A mark alone is not enough.* A shared per-user daemon that a role happened to
start first inherits the mark, detaches, and belongs to the operator's other
work as much as to the role. A process is the role's only if it is **marked
and anchored** to the pane's own shell:

1. Never touched, whatever its mark: this process, its ancestors, everything
   this process started, pid 1 (`excluded`); other users' processes are never
   in the table.
2. The **root** is the pane's own shell: a marked process that leads its own
   session (`sid == pid`) and whose parent is **a Herdr server** (`is_server`:
   named exactly `herdr`, and itself a session leader) that is not marked. It
   is never "the candidate that is left": an orphan is adopted by a
   *subreaper*, not by pid 1, so a detached marked daemon is also a session
   leader with an unmarked parent. **Exactly one root, or the pane's processes
   cannot be told and nothing is signalled.**
3. The **members** are the root, every process in the root's session, and every
   descendant of those by parent pid -- by structure, not by mark: a member
   that scrubbed its environment, or whose environment cannot be read, is
   still a member.
4. After the pane is closed (the root is gone) a process joins the members if it
   was one before, or is a descendant of a live member, or is marked and still
   in the root's session.
5. A marked process that is none of these -- a daemon that detached before the
   release -- is **escaped**: reported, never signalled.
6. A pane that is already gone has no root and none is looked for
   (`marked_left`): its marked leftovers are only named.

*The signal.* One process, never a group, and only the process that had that
pid **and the start time that was seen**. Where `os.pidfd_open` and
`signal.pidfd_send_signal` exist a pidfd is opened for the pid first, the start
time is read after that, and the signal goes through the pidfd (a reused pid
cannot receive it); otherwise the start time is re-read and `kill` follows (a
window of microseconds, `docs/LIMITATIONS.md`). **This module is the only place
that signals.**
"""
from __future__ import annotations

import os
import re
import secrets
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

MARK_ENV = "HOH_ROLE_MARK"
"""The environment variable that carries a pane's mark."""

SERVER_COMM = "herdr"
"""The name of the Herdr server process: the parent of every pane shell."""

#: SIGTERM, then this long, then SIGKILL for what is left, then this long.
TERM_WAIT_S = 5.0
KILL_WAIT_S = 3.0
#: A dying process may start another: how many times the whole sequence runs.
ROUNDS = 3
#: How often the table is looked at while waiting.
_POLL_S = 0.1

#: Marks `mint` returned in this process; nothing else is ever acted on.
_MINTED: set[str] = set()


def mint(label: str, role: str) -> str:
    """A new mark for one pane: `<label>:<role>:<96 random bits>`.

    Not derivable from run and role, so a second run (or a test) with the same
    label and role gets another one. Remembered, so `scan`/`end` accept it.
    """
    mark = f"{label}:{role}:{secrets.token_hex(12)}"
    _MINTED.add(mark)
    return mark


def _require_minted(mark: str) -> None:
    if not isinstance(mark, str) or mark not in _MINTED:
        raise ValueError(
            "refusing to act on a mark this process did not mint: no fixed or "
            "guessable mark is ever scanned or signalled")


@dataclass(frozen=True)
class Proc:
    """One process as the table saw it at one instant."""

    pid: int
    ppid: int
    pgrp: int
    sid: int
    state: str
    start: int
    comm: str
    mark: str | None = None

    @property
    def zombie(self) -> bool:
        return self.state in ("Z", "X")

    @property
    def key(self) -> tuple[int, int]:
        """What identifies the process: its pid **and** its start time."""
        return (self.pid, self.start)


# --- the table: the real Linux /proc -------------------------------------------- #

_STAT_RE = re.compile(r"^(\d+) \((.*)\) (\S) (.*)$", re.DOTALL)


def _parse_stat(text: str) -> tuple[int, str, str, int, int, int, int]:
    """`(pid, comm, state, ppid, pgrp, sid, start)` from a `/proc/<pid>/stat`.

    The name may hold spaces and parentheses, so the line is read from the
    **last** `)` (`proc(5)`: the fields after it are numbered from 3).
    """
    text = text.rstrip("\n")
    lparen = text.index("(")
    rparen = text.rindex(")")
    pid = int(text[:lparen].strip())
    comm = text[lparen + 1:rparen]
    rest = text[rparen + 1:].split()
    # rest[0] is field 3 (state), field 4 ppid, 5 pgrp, 6 session, ...,
    # field 22 starttime is rest[19].
    return (pid, comm, rest[0], int(rest[1]), int(rest[2]), int(rest[3]),
            int(rest[19]))


class ProcTable:
    """This user's processes, read from `/proc`. Raises when `/proc` cannot be read."""

    def __init__(self, root: str = "/proc") -> None:
        self.root = root

    def _read(self, pid: int) -> Proc | None:
        base = f"{self.root}/{pid}"
        try:
            with open(f"{base}/stat", "r", encoding="utf-8", errors="replace") as fh:
                parsed = _parse_stat(fh.read())
        except (FileNotFoundError, ProcessLookupError):
            return None            # gone while it was being read
        except (PermissionError, ValueError, IndexError):
            return None
        _, comm, state, ppid, pgrp, sid, start = parsed
        return Proc(pid=pid, ppid=ppid, pgrp=pgrp, sid=sid, state=state,
                    start=start, comm=comm, mark=self._mark(base))

    @staticmethod
    def _mark(base: str) -> str | None:
        """The mark in the process's environment, or None (none, or unreadable)."""
        try:
            with open(f"{base}/environ", "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        prefix = (MARK_ENV + "=").encode()
        for item in data.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix):].decode("utf-8", errors="replace")
        return None

    def snapshot(self) -> list[Proc]:
        """Every process of this user. **Raises** `OSError` if `/proc` is not
        there or shows no process: a missing table is never an empty one."""
        names = os.listdir(self.root)           # FileNotFoundError if missing
        uid = os.getuid()
        procs: list[Proc] = []
        seen_any = False
        for name in names:
            if not name.isdigit():
                continue
            seen_any = True
            try:
                if os.stat(f"{self.root}/{name}").st_uid != uid:
                    continue
            except OSError:
                continue
            proc = self._read(int(name))
            if proc is not None:
                procs.append(proc)
        if not seen_any:
            raise OSError(f"{self.root} shows no process: it is not mounted or "
                          "not readable, so no table can be told")
        return procs

    def start_time(self, pid: int) -> int | None:
        """The start time of `pid` now, or None if there is no such process."""
        try:
            with open(f"{self.root}/{pid}/stat", "r", encoding="utf-8",
                      errors="replace") as fh:
                return _parse_stat(fh.read())[6]
        except (OSError, ValueError, IndexError):
            return None

    def signal(self, proc: Proc, sig: int) -> bool:
        """Send `sig` to this one process, if it still is the one that was seen.

        Never a group, never a bare pid: the pid must still have the start
        time of `proc`. `True` only if the signal was delivered.
        """
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is not None and pidfd_send is not None:
            try:
                fd = pidfd_open(proc.pid)
            except OSError:
                return False
            try:
                # The pidfd names one process for good: after this, a reused
                # pid cannot receive the signal.
                if self.start_time(proc.pid) != proc.start:
                    return False
                try:
                    pidfd_send(fd, sig)
                except OSError:
                    return False
                return True
            finally:
                os.close(fd)
        if self.start_time(proc.pid) != proc.start:
            return False
        try:
            os.kill(proc.pid, sig)
        except OSError:
            return False
        return True


# --- what is never touched, who is a server, who is left -------------------------- #

def _children(procs: Iterable[Proc]) -> dict[int, list[Proc]]:
    kids: dict[int, list[Proc]] = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p)
    return kids


def excluded(procs: list[Proc], me: int | None = None) -> set[int]:
    """The pids that are never touched, whatever their mark: this process, its
    ancestors, everything this process started, and pid 1."""
    me = os.getpid() if me is None else me
    by_pid = {p.pid: p for p in procs}
    out = {1, me}
    pid = me
    while pid in by_pid and by_pid[pid].ppid not in out and by_pid[pid].ppid > 0:
        pid = by_pid[pid].ppid
        out.add(pid)
    # The ancestors above a link this table cannot see (another user's
    # process) are not in the table and cannot be signalled anyway.
    kids = _children(procs)
    stack = [me]
    while stack:
        for child in kids.get(stack.pop(), []):
            if child.pid not in out:
                out.add(child.pid)
                stack.append(child.pid)
    return out


def is_server(proc: Proc | None) -> bool:
    """A Herdr server: named exactly `herdr` and leading its own session (the
    `herdr` client that starts the server is not a leader)."""
    return proc is not None and proc.comm == SERVER_COMM and proc.sid == proc.pid


def marked_left(procs: list[Proc], mark: str, skip: Iterable[int] = ()) -> list[Proc]:
    """The live processes that still carry `mark`: not a zombie, not in `skip`.

    The look at a pane that is already gone: there is no session to anchor on,
    so what is found is only named.
    """
    _require_minted(mark)
    skipped = set(skip)
    return [p for p in procs
            if p.mark == mark and not p.zombie and p.pid not in skipped]


# --- scan ----------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RoleScan:
    """What `scan` found while the pane was still there.

    `problem` is empty when exactly one root was found. With a problem nothing
    is anchored: `members` is empty and nothing may be signalled. `escaped` is
    the marked processes outside the pane (only named).
    """

    root: Proc | None = None
    members: tuple[Proc, ...] = ()
    escaped: tuple[Proc, ...] = ()
    problem: str = ""
    candidates: tuple[Proc, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.problem and self.root is not None


def _members(procs: list[Proc], mark: str, known: set[tuple[int, int]],
             session: int | None, skip: set[int]) -> dict[int, Proc]:
    """The members among `procs`: structure, never the mark alone.

    A process is a member if it was one before (same pid and start time), or is
    a descendant of a member, or is marked and in the root's session. Zombies
    and skipped pids are never members.
    """
    live = [p for p in procs if not p.zombie and p.pid not in skip]
    members: dict[int, Proc] = {}
    for p in live:
        if p.key in known:
            members[p.pid] = p
        elif session is not None and p.mark == mark and p.sid == session:
            members[p.pid] = p
    kids = _children(live)
    stack = list(members)
    while stack:
        for child in kids.get(stack.pop(), []):
            if child.pid not in members:
                members[child.pid] = child
                stack.append(child.pid)
    return members


def scan(procs: list[Proc], mark: str, skip: Iterable[int] = ()) -> RoleScan:
    """Find the pane's root and members while the pane is alive."""
    _require_minted(mark)
    skipped = set(skip)
    by_pid = {p.pid: p for p in procs}
    candidates = [p for p in procs
                  if p.mark == mark and not p.zombie and p.pid not in skipped]
    if not candidates:
        return RoleScan(problem="no process carries the pane's mark (the mark "
                                "did not reach it, or the processes cannot be seen)")
    roots = [p for p in candidates
             if p.sid == p.pid
             and is_server(by_pid.get(p.ppid))
             and by_pid[p.ppid].mark != mark]
    if len(roots) != 1:
        return RoleScan(
            problem=(f"{len(roots)} candidate roots for the pane's shell (a "
                     "marked session leader directly below an unmarked Herdr "
                     f"server); {len(candidates)} marked processes; the pane's "
                     "processes cannot be told, so nothing is signalled"),
            candidates=tuple(candidates))
    root = roots[0]
    # By structure, never by mark: the root's whole session (one pane is one
    # session), and every descendant of those by parent pid.
    live = [p for p in procs if not p.zombie and p.pid not in skipped]
    members: dict[int, Proc] = {p.pid: p for p in live if p.sid == root.sid}
    members.setdefault(root.pid, root)
    kids = _children(live)
    stack = list(members)
    while stack:
        for child in kids.get(stack.pop(), []):
            if child.pid not in members:
                members[child.pid] = child
                stack.append(child.pid)
    escaped = [p for p in candidates if p.pid not in members]
    return RoleScan(root=root, members=tuple(sorted(members.values(), key=lambda p: p.pid)),
                    escaped=tuple(escaped), candidates=tuple(candidates))


# --- end ------------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Outcome:
    """The result of `end`: the proof, or what is left."""

    gone: tuple[int, ...] = ()
    survivors: tuple[Proc, ...] = ()
    escaped: tuple[Proc, ...] = ()

    @property
    def ok(self) -> bool:
        """Proof: no member alive and nothing marked outside the pane."""
        return not self.survivors and not self.escaped


def end(table, mark: str, found: RoleScan, skip: Iterable[int] = (), *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic) -> Outcome:
    """End what a role left running after its pane was closed, and prove it.

    SIGTERM to each member, up to `TERM_WAIT_S` for the ones told, SIGKILL for
    what is left, up to `KILL_WAIT_S`; up to `ROUNDS` rounds, because a dying
    process may start another. Then **one more snapshot, run through the same
    member computation**: a member alive then (one born during the last round
    too) is a survivor, and a marked process outside the members is escaped.
    `Outcome.gone` lists only the pids a signal of this call reached and that
    are gone. `table.snapshot()` errors propagate: an unreadable table is the
    caller's refusal.
    """
    _require_minted(mark)
    skipped = set(skip)
    known = {p.key for p in found.members}
    session = found.root.sid if found.root is not None else None
    reached: set[int] = set()
    killed: set[tuple[int, int]] = set()

    def members_now() -> dict[int, Proc]:
        return _members(table.snapshot(), mark, known, session, skipped)

    def wait_until_gone(pids: dict[int, Proc], limit: float) -> None:
        deadline = clock() + limit
        while clock() < deadline:
            alive = {p.pid for p in members_now().values()}
            if not any(p.pid in alive for p in pids.values()):
                return
            sleep(_POLL_S)

    for _ in range(ROUNDS):
        alive = members_now()
        fresh = {pid: p for pid, p in alive.items() if p.key not in killed}
        if not fresh:
            break
        told = {}
        for pid, proc in fresh.items():
            if table.signal(proc, signal.SIGTERM):
                reached.add(pid)
                told[pid] = proc
        if told:
            wait_until_gone(told, TERM_WAIT_S)
        left = {pid: p for pid, p in members_now().items()
                if pid in told and p.key == told[pid].key}
        for pid, proc in left.items():
            if table.signal(proc, signal.SIGKILL):
                reached.add(pid)
                killed.add(proc.key)
        if left:
            wait_until_gone(left, KILL_WAIT_S)

    procs = table.snapshot()
    final = _members(procs, mark, known, session, skipped)
    survivors = tuple(sorted(final.values(), key=lambda p: p.pid))
    escaped = tuple(p for p in procs
                    if p.mark == mark and not p.zombie and p.pid not in skipped
                    and p.pid not in final)
    gone = tuple(sorted(pid for pid in reached if pid not in final))
    return Outcome(gone=gone, survivors=survivors, escaped=escaped)
