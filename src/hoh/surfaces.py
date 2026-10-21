"""The terminal surfaces a run's own tooling opens: recorded when opened, closed
when the run is over (increment I10a, plan row P5-39, Amendment A6).

What it does, said exactly. `open_surface` opens one Herdr tab for a run and
records it in the run's own `owned-surfaces.jsonl` (an `OwnedAgentRegistry`, used
unchanged, role `program:<role>`). `listing` shows that record and asks nobody
anything. `reap` closes, for a run that is **over**, only the tabs the record
owns, and only after a fail-closed proof for each one (steps 1-10 of the spec,
in the order of the function). `reap_after_run` is the call at the end of
`hoh run`, after the process marker is released.

Why a file of its own. `HerdrDispatcher.close_own` closes every pane of its
`owned-agents.jsonl` at the end of a run; a surface recorded there would be
closed with it, among them the tab `hoh run` itself executes in. The surfaces
therefore go to `owned-surfaces.jsonl`, never to `owned-agents.jsonl`.

"Over" is narrow on purpose (`run_over`): condition FAILED or CANCELLED, or
stage READY_FOR_DELIVERY with condition ACTIVE, and no live process holds the
run. It is not `storage_tiers.run_finished` (which counts a checkpoint, and a
blocked run at a checkpoint, as finished) and it never reads `blocked_kind`.

What a close does. Closing a Herdr tab hangs up the shell in it and, with it,
whatever runs in that shell's foreground. This module sends no signal and closes
a pane only when nothing in it can be running: a pane with an agent when every
agent in it is `idle` or `done`; a plain shell when the process table (every
process of every user, from the world-readable `stat` of `/proc/<pid>`) shows
the pane's shell alone and asleep. Whatever cannot be read, found, told apart or
proven keeps the surface open (`kept`), with its reason.

What this is not (stated, not built):

* It does not close the pane of the process that reaps. A run-host tab is closed
  by a later `hoh surface reap --close` from another pane; Herdr's behaviour on
  a self-closed pane is NOT_RUN. Inside Herdr with its own tab unknown, nothing
  is closed (`refused`).
* It has one terminal rule of its own, "the run is over"; a caller may pass another,
  asked once per surface (`over_for`, and `workspace_of` for a surface whose close is the
  close of its Herdr workspace): `surfacebook.Rule` does, for reviewer and drafter
  surfaces (their strand is merged or released), for run workspaces (every run of their
  runs root has ended) and for orchestrator sessions (never). There is no deadline.
* It removes no file and signals no process. A run-host of a run that stops at a
  checkpoint stays open until the run is delivered, failed or cancelled.
* An agent pane is judged by the agent's state, not by its processes: a command
  the agent left in the background is not looked at (it survives a pane close,
  measured live, `procs.py`). A plain shell's foreground child is looked at.
* A busy host can make the process table unreadable as a whole (the read is
  repeated five times with a 20 ms pause) and the pane is then kept until the
  next reap; a `/proc` mounted with `hidepid` keeps every plain shell. The scan
  reads the pid, parent, group, session, state and name of other users'
  processes and never their command line or environment.
* A window between the snapshots / the process table and the close exists (a
  command started in the shell in between is hung up with it). A stale record
  (Herdr restarted and reused an id) is held only by the tab, directory and
  process checks.
* `hoh surface open` and `hoh surface reap` need `HERDR_ENV=1`: `make_control` calls
  `herdr.require_herdr`, so a process without it (a timer, a steward) gets exit 2 and
  nothing is sent -- it fails closed. The "outside Herdr" branch of `own_location`
  (nothing to protect) is reached only by a caller that brings its own control; the
  tests do. A `hoh run` spawned by the launcher carries `HERDR_ENV=1` whether or not
  Herdr is around (`launcher.py`), so without pane ids it is inside Herdr with its own
  tab unknown and closes nothing.
* The end-of-run notes go to stderr and nowhere else, and a stderr that cannot be written
  (a broken pipe, a closed descriptor, None) costs the note, never the exit code.
* Two reapers of the same run (the end of `hoh run` and a monitor) may race: the one
  whose close fails because the other closed the pane records nothing a second time and
  is `released`, never `refused`. What holds: the check of the pane's `closed` line and
  the `closing` / `closed` line that follows it are one step under the run's ledger gate
  (`pane_ledger.gate`, an exclusive `flock` on the run directory, never held across a
  call to the runtime), so reaps that **overlap in time** write one `closed` line per
  close and the pane's last line is not a stale `closing`. A reaper that cannot get the
  gate in 5 s sends nothing and keeps the surface (`kept`, "another reaper holds the
  ledger gate"; the next reap decides); a `closed` line is still written when the gate
  is lost after the close, because the close has happened. Not claimed: a reaper that
  *starts* after the winner's `closed` line (written in an earlier second) but before
  the record is released still writes its own `closed ... already_gone`, and a record
  that cannot be released repeats that at every later reap (stated, not built).
* The gate has a liveness cost. Where `flock` fails (a file system without it; a 9p /
  drvfs state root is suspected, NOT_RUN) or the run directory is a symbolic link, the
  gate is never had: every reap answers `kept` for the `closing` step and nothing in the
  product closes the surface. The way out is the operator's: close the tab by hand in
  Herdr (`hoh surface list` shows it as owned; the next reap finds the pane gone and
  releases the record, which needs no gate). A check's scratch below a run directory
  (`runner._scratch_locks` holds a shared `flock` on its owner-only ancestors) would
  hold the gate; the assumption is that scratch lives in the registry's scratch root.
* A mount point is not a tab's directory. `open` refuses `--cwd` at a mount point
  (`/proc/self/mountinfo` read as bytes, or `os.path.ismount`; a table that cannot be
  read refuses), because every pane anywhere below the mount would pass the identity
  check; a subdirectory of a mount is fine. `reap` refuses a surface whose recorded
  directory is a mount point, the home directory or an ancestor, or a system directory
  (an old record, or one written by an opener that does not run `open`'s check), and
  closes nothing for it. `/media`, `/srv`, `/boot` and `/mnt/c/Users` stay accepted.
* A reviewer tab that has no run needs the review opener (strand `i5`, `hoh review
  start`): not built here, and that opener does not run this module's `open` check, so
  `reap`'s check of the recorded directory is its backstop.
* Live behaviour on a real Herdr is NOT_RUN: the tests use a fake runtime, a
  fake process table, and a directory shaped like `/proc`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import herdr, pane_ledger, procs, storage_tiers
from .agentcontrol import (HerdrAgentControl, OwnedAgentControl, OwnedAgentRegistry,
                           ProgramAgentRole, Surface)
from .contracts import Condition, Role, Stage
from .controller import DispatchError
from .store import RunStore

SURFACES_FILE = "owned-surfaces.jsonl"

#: The top-level system directories that are never a tab's directory: every
#: pane would be at or below one of them, so the identity check proves nothing.
SYSTEM_DIRS = ("/", "/usr", "/etc", "/var", "/opt", "/bin", "/lib", "/lib32", "/lib64",
               "/libx32", "/sbin", "/home", "/root", "/mnt", "/proc", "/sys", "/dev",
               "/tmp")

#: Where the kernel lists the mounts of this process (a seam for the tests, read at call time).
MOUNTINFO = "/proc/self/mountinfo"

IDLE_STATUSES = frozenset({"idle", "done"})
SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "nu"})

#: How often a process table that changes under the reader is read again.
READ_TRIES = 5
RETRY_PAUSE_S = 0.02

_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PIPELINE_ROLES = frozenset(r.value for r in Role)


def registry_for(run_dir) -> OwnedAgentRegistry:
    """The run's surfaces record: `<run dir>/owned-surfaces.jsonl`."""
    run_dir = Path(run_dir)
    return OwnedAgentRegistry(run_dir / SURFACES_FILE, run_id=run_dir.name)


# --------------------------------------------------------------------------- #
# Seams
# --------------------------------------------------------------------------- #


def herdr_transport(argv, purpose, text=False):
    """Run one Herdr command (no shell, 30 s): its JSON, or stdout as text."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"{purpose}: herdr did not answer within 30 s",
                            transient=True) from exc
    except OSError as exc:
        raise DispatchError(f"{purpose}: herdr could not be run: {exc}",
                            transient=True) from exc
    if done.returncode != 0:
        raise DispatchError(f"{purpose}: herdr exit {done.returncode}: "
                            f"{(done.stderr or '').strip()[:300]}")
    if text:
        return done.stdout
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise DispatchError(f"{purpose}: herdr returned no JSON: {exc}") from exc


def make_control():
    """The Herdr agent control; raises `HerdrUnavailable` unless `HERDR_ENV=1`
    (`herdr.require_herdr`): outside Herdr `open` and `reap` exit 2."""
    herdr.require_herdr()
    return HerdrAgentControl(herdr_transport)


def make_proc_view():
    return ProcView(root="/proc")


def own_location() -> tuple[str, str, bool]:
    """(pane id, tab id, inside Herdr) of the process that asks. Inside Herdr is
    `HERDR_ENV=1` or either id set: a launcher-spawned `hoh run` carries
    `HERDR_ENV=1` whether or not Herdr is around."""
    pane = os.environ.get("HERDR_PANE_ID", "")
    tab = os.environ.get("HERDR_TAB_ID", "")
    return pane, tab, os.environ.get("HERDR_ENV") == "1" or bool(pane or tab)


# --------------------------------------------------------------------------- #
# The process table: every process of every user, metadata only
# --------------------------------------------------------------------------- #


def _unescape_mount_field(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


class ProcView:
    """A read-only scan of a `/proc`-shaped directory.

    Unlike `procs.ProcTable` it keeps the processes of **every** user: right for
    "is anything running in this pane?", where a root-owned `sudo` in the
    shell's session must count. It reads pid, parent, group, session, state and
    name from `stat` and nothing else of a process, except `pane_of`, which
    reads the environment of a candidate for a pane's shell.
    """

    def __init__(self, root: str = "/proc", mounts: str | None = None) -> None:
        self.root = str(root)
        self.mounts = mounts if mounts is not None else "/proc/self/mounts"

    def _hidden(self) -> str:
        """Why the table cannot be trusted to show other users' processes, or ''."""
        try:
            table = Path(self.mounts).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (f"the mount table cannot be read ({type(exc).__name__}), so a "
                    "hidepid mount of the process table cannot be ruled out")
        wanted = os.path.normpath(self.root)
        found: list[str] = []
        for line in table.splitlines():
            parts = line.split()
            if (len(parts) >= 4 and parts[2] == "proc"
                    and os.path.normpath(_unescape_mount_field(parts[1])) == wanted):
                found = parts
        if not found:
            return f"the mount table shows no proc mount at {self.root}"
        for option in found[3].split(","):
            if option.startswith("hidepid=") and option[len("hidepid="):] not in ("0", "off"):
                return (f"{self.root} is mounted with {option}: other users' processes "
                        "may be hidden")
        return ""

    def snapshot(self) -> list[procs.Proc]:
        """Every process, whoever owns it. Raises `OSError` when the table cannot
        be told whole: hidden by the mount, no process, an unreadable or
        unparsable `stat`, or a process that keeps vanishing while it is read."""
        hidden = self._hidden()
        if hidden:
            raise OSError(hidden)
        last = ""
        for attempt in range(READ_TRIES):
            if attempt:
                time.sleep(RETRY_PAUSE_S)
            names = [n for n in os.listdir(self.root) if n.isdigit()]
            if not names:
                raise OSError(f"{self.root} shows no process: it is not mounted or "
                              "not readable, so no table can be told")
            found: list[procs.Proc] = []
            for name in names:
                try:
                    text = Path(f"{self.root}/{name}/stat").read_text(
                        encoding="utf-8", errors="replace")
                except (FileNotFoundError, ProcessLookupError):
                    last = f"process {name} vanished while the table was read"
                    break
                except OSError as exc:
                    raise OSError(f"the stat of process {name} cannot be read: "
                                  f"{type(exc).__name__}") from exc
                try:
                    _, comm, state, ppid, pgrp, sid, start = procs._parse_stat(text)
                except (ValueError, IndexError) as exc:
                    raise OSError(f"the stat of process {name} cannot be parsed") from exc
                found.append(procs.Proc(pid=int(name), ppid=ppid, pgrp=pgrp, sid=sid,
                                        state=state, start=start, comm=comm))
            else:
                return found
        raise OSError(last or "the process table kept changing while it was read")

    def pane_of(self, pid: int) -> str | None:
        """The `HERDR_PANE_ID` of the process's environment; None when unreadable."""
        try:
            data = Path(f"{self.root}/{pid}/environ").read_bytes()
        except OSError:
            return None
        prefix = b"HERDR_PANE_ID="
        for item in data.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix):].decode("utf-8", errors="replace")
        return None


@dataclass
class ProcRead:
    """One read of the process table: the records, or why there are none."""

    view: object = None
    procs: list = field(default_factory=list)
    error: str = ""


def read_proc_table() -> ProcRead:
    """One read of `make_proc_view().snapshot()`; never raises."""
    view = None
    try:
        view = make_proc_view()
        return ProcRead(view, list(view.snapshot()))
    except Exception as exc:                      # noqa: BLE001 - the table is then unknown
        return ProcRead(view, [], f"{type(exc).__name__}: {exc}")


def shell_problem(table: ProcRead, pane_id: str) -> str:
    """'' when the pane's plain shell is proven alone and asleep, else why not.

    Exactly one process is the pane's shell (it leads its own session, it is a
    child of a Herdr server, and its environment names this pane), it is named
    like a shell, asleep, and no other live process is in its session, in its
    process group, or below it or them by parent.
    """
    if table.error:
        return f"the process table cannot be read whole ({table.error})"
    if not table.procs:
        return "the process table shows no process"
    by_pid = {p.pid: p for p in table.procs}
    roots = [p for p in table.procs
             if p.sid == p.pid and not p.zombie and procs.is_server(by_pid.get(p.ppid))
             and table.view.pane_of(p.pid) == pane_id]
    if len(roots) != 1:
        return (f"{len(roots)} processes claim the pane as their own shell"
                if roots else "no process was found that is the pane's shell")
    root = roots[0]
    if root.comm not in SHELLS:
        return f"the pane's first process is {root.comm!r}, not a shell"
    if root.state != "S":
        return f"the shell (pid {root.pid}) is not asleep (state {root.state})"
    seeds = {p.pid for p in table.procs
              if (p.sid == root.sid or p.pgrp == root.pgrp) and p.pid != root.pid}
    members = set(seeds)
    kids: dict[int, list[int]] = {}
    for p in table.procs:
        kids.setdefault(p.ppid, []).append(p.pid)
    stack = [root.pid, *seeds]
    while stack:
        for child in kids.get(stack.pop(), []):
            if child != root.pid and child not in members:
                members.add(child)
                stack.append(child)
    live = sorted((by_pid[pid] for pid in members if not by_pid[pid].zombie),
                  key=lambda p: p.pid)
    if live:
        named = ", ".join(f"{p.comm} (pid {p.pid})" for p in live[:5])
        more = f" and {len(live) - 5} more" if len(live) > 5 else ""
        return f"other processes run in or below the shell (pid {root.pid}): {named}{more}"
    return ""


# --------------------------------------------------------------------------- #
# Is the run over?
# --------------------------------------------------------------------------- #


def run_over(run_dir) -> tuple[bool, str]:
    """Over = condition FAILED or CANCELLED, or stage READY_FOR_DELIVERY with
    condition ACTIVE, **and** `storage_tiers.run_finished` (no live process).
    Decided on stage, condition and the process marker; never on `blocked_kind`.
    A state that cannot be read is not over."""
    run_dir = Path(run_dir)
    try:
        state = RunStore._parse_state((run_dir / "state.json").read_text(encoding="utf-8"))
    except Exception as exc:                      # noqa: BLE001 - unparseable proves nothing
        return False, f"run state unreadable: {exc}"
    if not (state.condition in (Condition.FAILED, Condition.CANCELLED)
            or (state.stage is Stage.READY_FOR_DELIVERY and state.condition is Condition.ACTIVE)):
        return False, (f"run {state.run_id} is {state.condition}/{state.stage}: "
                       "not failed, cancelled or ready for delivery")
    return storage_tiers.run_finished(run_dir)


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #


def mount_points() -> set[str]:
    """Every mount point of `MOUNTINFO` (field 5 of each line), as the path spells it.

    Read as bytes, the octal escapes (`\\040`) undone on the bytes and the result decoded
    with `os.fsdecode`: the same spelling `os.path.realpath` gives the directory, so a
    mount point with a byte that is not UTF-8 still matches. `OSError` when the table
    cannot be read or lists no mount."""
    data = Path(MOUNTINFO).read_bytes()
    found: set[str] = set()
    for line in data.splitlines():
        fields = line.split(b" ")
        if len(fields) < 5 or not fields[4]:
            continue
        raw = re.sub(rb"\\([0-7]{3})",
                     lambda m: bytes([int(m.group(1), 8) & 0xFF]), fields[4])
        found.add(os.path.normpath(os.fsdecode(raw)))
    if not found:
        raise OSError(f"{MOUNTINFO} lists no mount")
    return found


def _mount_table() -> tuple[set[str], str]:
    """(the mount points, why not): never raises. A table that cannot be read rules
    nothing out, so its callers refuse."""
    try:
        return mount_points(), ""
    except Exception as exc:                      # noqa: BLE001 - unreadable proves nothing
        return set(), (f"the mount table cannot be read ({type(exc).__name__}), so a "
                       "mount root cannot be ruled out")


def _breadth_problem(cwd, table=None) -> str:
    """Why `cwd` (absolute) is too broad to be a tab's directory, or ''. Every pane at
    or below it would pass the identity check, so the check would prove nothing: the
    file system root or a top-level system directory, the home directory or one of its
    ancestors, a mount point. A directory *below* a mount point, a directory that only
    contains one and a sibling that shares a prefix are not refused. `table` is a
    `_mount_table()` result (read now when None); an unreadable one refuses."""
    real = os.path.realpath(cwd)
    top_level = set(SYSTEM_DIRS) | {os.path.realpath(d) for d in SYSTEM_DIRS}
    if real in top_level or os.path.normpath(cwd) in top_level:
        return (f"{cwd} is the file system root or a top-level system directory: every "
                "pane would be at or below it, so the identity check would prove nothing")
    expanded = os.path.expanduser("~")
    if expanded != "~":
        home = os.path.realpath(expanded)
        if real == home or home.startswith(real + os.sep):
            return (f"{cwd} is the home directory or one of its ancestors: every pane "
                    "would be at or below it, so the identity check would prove nothing")
    mounts, why = table if table is not None else _mount_table()
    if why:
        return why
    if real in mounts or os.path.normpath(cwd) in mounts or os.path.ismount(real):
        return (f"{cwd} is a mount point: every pane anywhere below the mount would pass "
                "the identity check, so it proves nothing")
    return ""


def _cwd_problem(cwd) -> str:
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return "the directory is not an absolute path"
    if not os.path.isdir(cwd):
        return "the directory does not exist"
    return _breadth_problem(cwd)


def _open_problem(role, label, cwd) -> str:
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        return "the role must be a lowercase slug ([a-z][a-z0-9-]{0,31})"
    if role in _PIPELINE_ROLES:
        return f"{role!r} is a pipeline role, not a program role"
    if (not isinstance(label, str) or not label or len(label) > 80 or label.startswith("-")
            or any(ord(c) < 32 or ord(c) == 127 for c in label)):
        return ("the label must be 1 to 80 characters, not start with '-' and hold no "
                "control character")
    return _cwd_problem(cwd)


def open_surface(run_dir, *, role: str, label: str, cwd: str, program: str,
                 control=None, env=None) -> Surface:
    """Open one tab for the run and record it as the run's own. Every refusal is
    checked before any control is made (`ValueError`, nothing created). `env` is the
    environment the tab's shell starts with (`herdr tab create --env`)."""
    run_dir = Path(run_dir)
    problem = _open_problem(role, label, cwd)
    if problem:
        raise ValueError(problem)
    owned = OwnedAgentControl(control if control is not None else make_control(),
                              registry_for(run_dir))
    with owned.acting_as(ProgramAgentRole.program(role), program=program, run_id=run_dir.name):
        return owned.open_surface(cwd=cwd, label=label, env=env,
                                  purpose=f"open a surface for run {run_dir.name}")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def listing(run_dir) -> list[dict]:
    """The record's view: every surface owned or released. Asks Herdr and the
    process table nothing. An unreadable record is one `problem` entry."""
    run_dir = Path(run_dir)
    registry = registry_for(run_dir)
    agents = registry.agents()
    if registry.problem:
        return [{"run_id": registry.run_id, "problem": registry.problem}]
    over, why = run_over(run_dir)
    return [{"run_id": registry.run_id, "pane_id": a.pane_id, "tab_id": a.tab_id,
             "role": a.role.key, "cwd": a.cwd, "state": a.state, "run_over": over,
             "why": why} for a in agents]


# --------------------------------------------------------------------------- #
# reap
# --------------------------------------------------------------------------- #


def _snapshot_problem(snap, need_pane: bool = True) -> str:
    if not isinstance(snap, dict):
        return "the runtime's snapshot is not an object"
    for key in ("agents", "panes", "tabs"):
        if not isinstance(snap.get(key), list):
            return f"the runtime's snapshot has no list {key!r}"
        if not all(isinstance(row, dict) for row in snap[key]):
            return f"the runtime's snapshot has a row of {key!r} that is not an object"
    if need_pane and not snap["panes"]:
        return "the runtime's snapshot lists no pane (the process that asks sits in one)"
    return ""


def _says_gone(adapter, pane_id: str) -> bool:
    """Herdr itself answers `pane_not_found` for the pane."""
    try:
        adapter.pane_screen(pane_id, source=None, purpose=f"is pane {pane_id} gone")
    except DispatchError as exc:
        return "pane_not_found" in str(exc)
    except Exception:                             # noqa: BLE001 - any other answer proves nothing
        return False
    return False


def _at_or_below(path, root) -> bool:
    """`path` is absolute and equals or lies below `root`, both resolved."""
    if not (isinstance(path, str) and os.path.isabs(path) and isinstance(root, str) and root):
        return False
    try:
        target, base = Path(path).resolve(), Path(root).resolve()
        return target == base or target.is_relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return False


def _identity_problem(panes, cwd, tab_id) -> str:
    """Why the pane is not where it was opened, or ''. `panes` are the snapshot's
    rows for the one pane. Not a proof (a foreign shell in the same tab and
    directory passes) and fail-closed: a shell that left the directory is refused."""
    if len(panes) != 1:
        return f"the snapshot lists the pane {len(panes)} times"
    if not cwd:
        return "no directory was recorded when the surface was opened"
    for row in panes:
        if tab_id and str(row.get("tab_id") or "") != tab_id:
            return (f"the pane is in tab {row.get('tab_id')!r}, not in the tab "
                    f"{tab_id!r} it was opened in")
        reported = [str(row.get(k) or "") for k in ("foreground_cwd", "cwd")]
        reported = [r for r in reported if r]
        if not reported:
            return "the snapshot reports no directory for the pane"
        for path in reported:
            if not _at_or_below(path, cwd):
                return f"the pane is at {path!r}, not at or below {cwd!r} where it was opened"
    return ""


def _pane_lines(run_dir, pane_id: str) -> list[dict]:
    """The pane's lines in the pane ledger, oldest first ([] when none or unreadable)."""
    try:
        text = (Path(run_dir) / pane_ledger.LEDGER).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    lines = []
    for raw in text.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and str(rec.get("pane") or "") == pane_id:
            lines.append(rec)
    return lines


def _latest_line(run_dir, pane_id: str) -> dict | None:
    """The pane's latest line in the pane ledger, or None (none, or unreadable)."""
    lines = _pane_lines(run_dir, pane_id)
    return lines[-1] if lines else None


def _closed_since(run_dir, pane_id: str, since: str) -> bool:
    """The ledger holds a `closed` line of the pane written at or after `since` (the
    start of this reap, in the ledger's own second-resolution UTC form): another reaper
    closed the pane while this one was working. An older `closed` line (a pane id that
    Herdr gave out again) does not count."""
    return any(rec.get("event") == "closed" and str(rec.get("at") or "") >= since
               for rec in _pane_lines(run_dir, pane_id))


def _already_recorded(run_dir, pane_id: str, event: str, why: str) -> bool:
    """The pane's latest ledger line is this event with this reason."""
    latest = _latest_line(run_dir, pane_id)
    return latest is not None and latest.get("event") == event and latest.get("why") == why


def _closing_unless_closed(run_dir, s, since: str, scope: str, reason: str = "run over") -> str:
    """Under the run's ledger gate: the check for a `closed` line of the pane at or after
    `since` and the `closing` line are one step, so a reaper that overlaps this one
    cannot land its `closed` between them. Returns `closed` (another reaper's line is
    there: nothing written), `gate` (the gate is not held: nothing written), `failed`
    (the ledger cannot be appended to) or `written` (the `closing` line is there)."""
    with pane_ledger.gate(run_dir) as held:
        if not held:
            return "gate"
        if _closed_since(run_dir, s.pane_id, since):
            return "closed"
        wrote = pane_ledger.append(run_dir, "closing", pane=s.pane_id, tab=s.tab_id,
                                   role=s.role.key, via="surface", reason=reason,
                                   scope=scope)
        return "written" if wrote else "failed"


def _closed_once(run_dir, s, since: str, **fields) -> bool:
    """Append the pane's `closed` line unless one at or after `since` is there already
    (the check and the append are one step under the gate). The close has happened, so a
    gate that cannot be had does not stop the line: without it the pane's latest line
    would stay a stale `closing`. `True` when a line was written."""
    with pane_ledger.gate(run_dir):
        if _closed_since(run_dir, s.pane_id, since):
            return False
        return pane_ledger.append(run_dir, "closed", pane=s.pane_id, tab=s.tab_id,
                                  role=s.role.key, via="surface", **fields)


def _gone_now(adapter, pane_id: str) -> bool:
    """The pane is gone by both proofs, read now: a fresh snapshot does not list it and
    Herdr answers `pane_not_found`. Used after a close that failed: a reaper that
    lost a race (the end of `hoh run` and a monitor reap together) finds the pane
    closed by the winner, not a close that failed."""
    try:
        snap = adapter.snapshot()
        if _snapshot_problem(snap, need_pane=False):
            return False
    except Exception:                             # noqa: BLE001 - unreadable proves nothing
        return False
    listed = {str(p.get("pane_id") or "") for p in snap["panes"]}
    return pane_id not in listed and _says_gone(adapter, pane_id)


def reap(run_dir, *, close: bool = False, self_pane: str = "", self_tab: str = "",
         self_inside: bool = False, over_for=None, workspace_of=None,
         group_of=None, refuse_for=None) -> list[dict]:
    """For every surface the record owns now, say what happens to it (a dry run)
    or close it (`close`), after the fail-closed proof of the spec. Raises only
    for a Herdr that cannot be reached (`HerdrError` / `DispatchError` from
    making the control).

    `over_for` is the terminal rule, `(run_dir, surface) -> (over, why)`, asked **once
    per surface**; by default one answer for the whole directory, `run_over(run_dir)`.
    A rule that raises or answers something else says "not over". A surface that is
    not over is `pending` (with the rule's reason; with `close` a `kept` line of the
    pane ledger, once per reason), and when none is over neither Herdr nor the process
    table is asked. `workspace_of(run_dir, surface)` names the Herdr workspace whose
    close is the surface's close ("" for a tab or a pane): the workspace must hold that
    one pane and no other, and `close_workspace` closes it.

    `group_of(run_dir, surface)` answers `([(pane id, workspace id), ...], problem)`: the other
    surfaces recorded together with this one by the same creation (a `hoh worktree` call makes a
    target workspace and a linked one, and Herdr's close of the target ends the linked one with
    it), and a non-empty `problem` when a member of the group is not in the record at all. A
    surface with a group closes only if **every** member passes every proof of this reap or is
    gone already: otherwise it is `kept` (a member that is kept) or `refused` (a member that is
    refused, or a group with an unrecorded member), with the member named.

    `refuse_for(run_dir, surface)` answers a reason ('' for none) for a surface that is not over
    and must not read as a surface that merely waits: it is then `refused` (with a `refused` line
    of the pane ledger under `close`), not `pending`. It is asked only of a surface the rule says
    is not over. The identity check of the recorded agent name runs only under a rule (`over_for`):
    the default path is the old reaper, statement for statement."""
    run_dir = Path(run_dir)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    registry = registry_for(run_dir)
    owned = registry.owned()
    if registry.problem:
        return [{"run_id": registry.run_id, "pane_id": "", "tab_id": "", "role": "",
                 "outcome": "refused",
                 "why": f"the surfaces record cannot be read: {registry.problem}"}]
    if not owned:
        return []
    if over_for is None:
        run_verdict = run_over(run_dir)
        verdicts = {s.pane_id: run_verdict for s in owned}
    else:
        verdicts = {}
        for s in owned:
            try:
                verdict = over_for(run_dir, s)
                if not (isinstance(verdict, tuple) and len(verdict) == 2
                        and isinstance(verdict[0], bool) and isinstance(verdict[1], str)):
                    raise TypeError("a terminal rule answers (bool, str)")
                verdicts[s.pane_id] = verdict
            except Exception as exc:                  # noqa: BLE001 - a rule that fails proves nothing
                verdicts[s.pane_id] = (False, f"the terminal rule failed: {type(exc).__name__}")

    def entry(s, outcome, why=""):
        return {"run_id": registry.run_id, "pane_id": s.pane_id, "tab_id": s.tab_id,
                "role": s.role.key, "outcome": outcome, "why": why}

    def record(event, s, **fields):
        return pane_ledger.append(
            run_dir, event, pane=s.pane_id, tab=s.tab_id,
            role=s.role.key, via="surface", **fields)

    def note(s, event, why):
        """A `kept` / `refused` outcome; its ledger line (only with `close`) only
        when it differs from the pane's latest line."""
        if (close and not (event in ("kept", "refused")
                                     and _already_recorded(run_dir, s.pane_id, event, why))):
            record(event, s, why=why)
        return entry(s, event, why)

    def pending(s):
        why = verdicts[s.pane_id][1]
        if over_for is None:
            return entry(s, "pending", f"the run is not over: {why}")
        odd = ""
        if refuse_for is not None:
            try:
                odd = str(refuse_for(run_dir, s) or "")
            except Exception:                         # noqa: BLE001 - the ordinary answer stands
                odd = ""
        if odd:
            return note(s, "refused", odd)
        if close and not _already_recorded(run_dir, s.pane_id, "kept", why):
            record("kept", s, why=why)
        return entry(s, "pending", why)

    if not any(v[0] for v in verdicts.values()):
        return [pending(s) for s in owned]

    adapter = OwnedAgentControl(make_control(), registry)
    try:
        snap = adapter.snapshot()
        snap_problem = _snapshot_problem(snap)
    except Exception as exc:                      # noqa: BLE001 - an unreadable snapshot closes nothing
        snap, snap_problem = None, f"the runtime's snapshot cannot be read: {exc}"
    if snap_problem:
        return [note(s, "refused", snap_problem) for s in owned]
    pane_rows, tab_rows_all, agent_rows = snap["panes"], snap["tabs"], snap["agents"]
    snapshot_panes = {str(p.get("pane_id") or "") for p in pane_rows}
    owned_panes = {a.pane_id for a in owned}

    protected = {self_tab} if self_tab else set()
    for row in pane_rows:
        if self_pane and str(row.get("pane_id") or "") == self_pane and row.get("tab_id"):
            protected.add(str(row["tab_id"]))
    own_unknown = self_inside and not protected

    view_box: list = [None]
    mount_box: list = [None]
    results: list[dict] = []
    deferred: list = []

    def close_one(s, ws):
        """The close of one surface whose proofs have passed (or its `would_close` line)."""
        end_why = "the run is over" if over_for is None else verdicts[s.pane_id][1]
        if not close:
            results.append(entry(s, "would_close", f"{end_why} and nothing can be running in "
                                                   "the pane"))
            return
        scope = "workspace" if ws else ("tab" if s.tab_id else "pane")
        if _closed_since(run_dir, s.pane_id, started):
            # Another reaper finished while this one read the process table (its `closed`
            # line is written before it releases the record): nothing is asked of the
            # runtime and no `closing` line is left without its `closed`. Only a real
            # `closed` line says so -- a record that cannot be read now is not "released".
            results.append(entry(s, "released", "another reaper closed the pane first"))
            return
        registry.owned()                  # the record, read again: it may have become unreadable
        if registry.problem:
            results.append(note(s, "refused", f"the surfaces record cannot be read: "
                                              f"{registry.problem}"))
            return
        step = _closing_unless_closed(run_dir, s, started, scope,
                                      "run over" if over_for is None else end_why)
        if step == "closed":
            results.append(entry(s, "released", "another reaper closed the pane first"))
            return
        if step == "gate":
            results.append(note(s, "kept", "another reaper holds the ledger gate: the "
                                           "close is left to the next reap"))
            return
        if step == "failed":
            results.append(entry(s, "refused", "the pane lifecycle ledger cannot be "
                                               "appended to: the close is not attempted"))
            return
        try:
            if ws:
                adapter.close_workspace(ws, purpose=f"close the workspace {ws} of surface "
                                                    f"{s.pane_id}: {end_why}")
            elif s.tab_id:
                adapter.close_tab(s.tab_id, purpose=f"close the tab of surface {s.pane_id}: "
                                                    f"{end_why}")
            else:
                adapter.close_pane(s.pane_id, purpose=f"close surface {s.pane_id}: {end_why}")
        except Exception as exc:                  # noqa: BLE001 - the close did not happen, or not by us
            if _gone_now(adapter, s.pane_id):
                # Another reaper closed it first: its `closed` line stands (one is written
                # only if none of the pane's `closed` lines is from this reap), and no
                # `refused` line is appended after it.
                _closed_once(run_dir, s, started, reason="closed by another reaper",
                             already_gone=True)
                try:
                    registry.release(s.pane_id, reason="gone")
                except Exception:                 # noqa: BLE001 - the next reap finds it gone again
                    pass
                results.append(entry(s, "released", "another reaper closed the pane first"))
                return
            results.append(note(s, "refused", f"the close failed: {exc}"))
            return
        try:
            after = adapter.snapshot()
            after_problem = _snapshot_problem(after, need_pane=False)
        except Exception as exc:                  # noqa: BLE001
            after, after_problem = None, f"the snapshot after the close cannot be read: {exc}"
        if after_problem:
            results.append(note(s, "refused", f"the close is not proven: {after_problem}"))
            return
        listed = {str(p.get("pane_id") or "") for p in after["panes"]}
        if s.pane_id in listed or not _says_gone(adapter, s.pane_id):
            results.append(note(s, "refused", "the close did not take: the runtime still "
                                              "knows the pane"))
            return
        _closed_once(run_dir, s, started, reason="run over" if over_for is None else end_why)
        try:
            registry.release(s.pane_id, reason="closed")
        except Exception:                         # noqa: BLE001 - the next reap finds the pane gone
            pass
        results.append(entry(s, "closed", f"{end_why}; the pane is gone"))


    for s in owned:
        if not verdicts[s.pane_id][0]:
            results.append(pending(s))
            continue
        if (self_pane and s.pane_id == self_pane) or (s.tab_id and s.tab_id in protected):
            results.append(note(s, "kept", "reap from another pane: this is the pane or "
                                           "tab of the process that reaps"))
            continue
        if own_unknown:
            results.append(note(s, "refused", "its own tab is unknown: this process runs "
                                              "inside Herdr and neither HERDR_TAB_ID nor "
                                              "a snapshot row for HERDR_PANE_ID names it"))
            continue
        if s.pane_id not in snapshot_panes:
            if _says_gone(adapter, s.pane_id):
                if not close:
                    results.append(entry(s, "would_release", "the pane is already gone"))
                    continue
                # (another reaper that closed it during this reap has written its own line)
                _closed_once(run_dir, s, started, reason="pane already gone",
                             already_gone=True)
                try:
                    registry.release(s.pane_id, reason="gone")
                except Exception:                 # noqa: BLE001 - the next reap finds it gone again
                    pass
                results.append(entry(s, "released", "the pane is already gone"))
            else:
                results.append(note(s, "refused", "the snapshot does not list the pane, but "
                                                  "Herdr does not say it is gone: a snapshot "
                                                  "that lists only some panes does not make "
                                                  "a live pane gone"))
            continue
        panes = [r for r in pane_rows if str(r.get("pane_id") or "") == s.pane_id]
        if isinstance(s.cwd, str) and os.path.isabs(s.cwd):
            if mount_box[0] is None:          # read once for the whole reap
                mount_box[0] = _mount_table()
            broad = _breadth_problem(s.cwd, mount_box[0])
            if broad:
                results.append(note(s, "refused", "the directory the surface was recorded "
                                                  f"with is too broad: {broad}"))
                continue
        problem = _identity_problem(panes, s.cwd, s.tab_id)
        if problem:
            results.append(note(s, "refused", problem))
            continue
        mine = [a for a in agent_rows if str(a.get("pane_id") or "") == s.pane_id]
        named = [str(a.get("name") or "") for a in mine]
        if over_for is not None and s.name and any(n != s.name for n in named):
            results.append(note(s, "refused", f"the agent in the pane is named {named[0]!r}, "
                                              f"not {s.name!r} as recorded: the pane is not "
                                              "the one that was registered"))
            continue
        busy = [str(a.get("agent_status") or "") or "empty" for a in mine
                if str(a.get("agent_status") or "") not in IDLE_STATUSES]
        if busy:
            results.append(note(s, "kept", f"an agent in the pane is {busy[0]}, not idle "
                                           "or done"))
            continue
        if not mine:
            if view_box[0] is None:
                view_box[0] = read_proc_table()
            not_idle = shell_problem(view_box[0], s.pane_id)
            if not_idle:
                results.append(note(s, "kept", f"the shell is not proven idle: {not_idle}"))
                continue
        if s.tab_id:
            in_tab = [str(p.get("pane_id") or "") for p in pane_rows
                      if str(p.get("tab_id") or "") == s.tab_id]
            strangers = [p for p in in_tab if p not in owned_panes]
            if strangers:
                results.append(note(s, "refused", f"tab {s.tab_id} holds a pane the record "
                                                  f"does not own: {', '.join(strangers)}"))
                continue
            tab_rows = [t for t in tab_rows_all if str(t.get("tab_id") or "") == s.tab_id]
            count = tab_rows[0].get("pane_count") if len(tab_rows) == 1 else None
            if not (isinstance(count, int) and not isinstance(count, bool)
                    and count == len(in_tab)):
                results.append(note(s, "refused", f"the snapshot's row for tab {s.tab_id} "
                                                  f"({len(tab_rows)} rows, pane_count "
                                                  f"{count!r}) does not account for the "
                                                  f"{len(in_tab)} panes listed in it"))
                continue
            if len(in_tab) != 1:
                results.append(note(s, "refused", f"tab {s.tab_id} holds {len(in_tab)} panes: "
                                                  "a close of the tab would end panes that "
                                                  "were not examined"))
                continue
        ws = ""
        if workspace_of is not None:
            try:
                ws = str(workspace_of(run_dir, s) or "")
            except Exception as exc:              # noqa: BLE001 - a surface whose scope is unknown stays
                results.append(note(s, "refused", "the workspace of the surface cannot be "
                                                  f"determined: {type(exc).__name__}"))
                continue
        if ws:
            in_ws = [str(r.get("pane_id") or "") for r in pane_rows
                     if str(r.get("workspace_id") or "") == ws]
            if s.pane_id not in in_ws:
                results.append(note(s, "refused", f"the pane is not in workspace {ws}, where "
                                                  "it was recorded"))
                continue
            strangers = [p for p in in_ws if p not in owned_panes]
            if strangers:
                results.append(note(s, "refused", f"workspace {ws} holds a pane the record "
                                                  f"does not own: {', '.join(strangers)}"))
                continue
            if len(in_ws) != 1:
                results.append(note(s, "refused", f"workspace {ws} holds {len(in_ws)} panes: "
                                                  "a close of the workspace would end panes "
                                                  "that were not examined"))
                continue
        if ws and group_of is not None:
            try:
                members, group_problem = group_of(run_dir, s)
            except Exception as exc:              # noqa: BLE001 - a group that is unknown stays
                results.append(note(s, "refused", "the group of the workspace cannot be "
                                                  f"determined: {type(exc).__name__}"))
                continue
            if group_problem:
                results.append(note(s, "refused", group_problem))
                continue
            if members:                           # judged once every surface has been examined
                deferred.append((s, ws, members))
                continue
        close_one(s, ws)
    if deferred:
        resolved = {r["pane_id"]: r for r in results}
        waiting = {d[0].pane_id for d in deferred}
        for s, ws, members in deferred:
            blockers = []
            for member, member_ws in members:
                if member in owned_panes:
                    done = resolved.get(member)
                    if done is not None and done["outcome"] not in ("would_release", "released"):
                        blockers.append((member, member_ws, done["outcome"], done["why"]))
                    elif done is None and member not in waiting:
                        blockers.append((member, member_ws, "kept", "it was not examined"))
                elif member in snapshot_panes:    # no longer ours and still there: not examined
                    blockers.append((member, member_ws, "kept",
                                     "it is not in the record any more and Herdr still lists it"))
            if blockers:
                member, member_ws, outcome, why = blockers[0]
                results.append(note(s, "refused" if any(b[2] == "refused" for b in blockers)
                                    else "kept",
                                    f"workspace {ws} was created together with workspace "
                                    f"{member_ws} (pane {member}) and a close of one ends both: "
                                    f"that one is {outcome}: {why}"))
                continue
            close_one(s, ws)
        position = {a.pane_id: i for i, a in enumerate(owned)}      # the order of the record
        results.sort(key=lambda r: position.get(r["pane_id"], len(position)))
    return results


def _note(text: str) -> None:
    """One line on stderr and nowhere else; it never raises and it leaves nothing in a
    buffer. A stderr that is broken (a pipe whose reader is gone, a closed descriptor),
    replaced by something whose write raises, or gone (`sys.stderr` is None --
    `print(..., file=None)` would write to stdout, where the run's JSON payload is) loses
    the note, never the run's exit code.

    Why `os.write` on the stream's own descriptor and not `stream.write`: a failed write
    or flush leaves the text in the stream's buffer, and the interpreter flushes
    `sys.stderr` once more when it exits -- that flush fails on the broken pipe and the
    process exits 120 in place of the run's code. A note written straight to the
    descriptor is never buffered, so there is nothing to flush. A stream with no
    descriptor (a test's capture, a closed file) is written normally: it buffers nothing
    that a pipe could refuse."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        try:
            fd = stream.fileno()
        except Exception:                     # noqa: BLE001 - no descriptor: not a pipe
            fd = None
        if fd is None:
            stream.write(text + "\n")
            stream.flush()
            return
        try:
            stream.flush()                    # keep the order with what is already written
        except Exception:                     # noqa: BLE001 - that is not this note
            pass
        encoding = getattr(stream, "encoding", None) or "utf-8"
        os.write(fd, (text + "\n").encode(encoding, "replace"))
    except Exception:                         # noqa: BLE001 - a note is not worth a traceback
        pass


def reap_after_run(run_dir) -> None:
    """The end of `hoh run`, after the process marker is released. Never raises,
    never changes the exit code, never writes to stdout, whatever `sys.stderr` is;
    a run with no record does nothing at all."""
    try:
        if not os.path.lexists(Path(run_dir) / SURFACES_FILE):
            return
        pane, tab, inside = own_location()
        results = reap(Path(run_dir), close=True, self_pane=pane, self_tab=tab,
                       self_inside=inside)
        for r in results:
            if r["outcome"] not in ("kept", "refused"):
                continue
            _note(f"surface {r['pane_id'] or r['run_id']} stays: {r['why']}")
    except Exception as exc:                  # noqa: BLE001 - the end of a run must not fail here
        try:
            text = f"surfaces: not reaped ({type(exc).__name__}: {exc})"
        except Exception:                     # noqa: BLE001 - an exception whose text cannot be made
            text = "surfaces: not reaped"
        _note(text)
