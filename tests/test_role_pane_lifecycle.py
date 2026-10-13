"""Role pane lifecycle: HoH closes the panes it opened, ends a role's session
before the candidate is frozen, and records every close and refusal.

Three defects, one slice (`specs-draft/role-pane-lifecycle/spec.md`):

* **A role that can still wake is frozen and measured.** Run qaw, 2026-10-05:
  a mutant command was moved to the background, the developer ended its turn
  "waiting for the notification", HoH saw an idle pane, froze the candidate,
  and the notification re-woke the developer *while QA ran*. Closing a pane
  does not even end the commands the agent started (live smoke, 2026-10-06:
  the `sleep` stayed alive, orphaned), so the role's processes are found,
  ended and proven gone as well (`hoh.procs`).
* **`close_own` closed whole tabs** it could not prove were its own. Only the
  pane HoH can prove is its own is closed; a tab only when the snapshot proves
  every pane in it is.
* **Panes lived to the run end and nothing was recorded.** Every close, keep and
  refusal is a line in `pane-lifecycle.jsonl` (`hoh.pane_ledger`), shown by
  `hoh status`. The ledger is a record, not a gate: nothing decides from it.

No test starts Herdr or an agent. A small in-memory `Server` stands in for
Herdr (tabs, panes, agents; `subprocess.run` inside `hoh.dispatchers` and
`herdr.snapshot` are its two doors, and it **fails the test on any call it does
not understand**). `FakeProcs` stands in for the Linux process table
(`procs.ProcTable`): closing a pane ends its shell and agent and orphans what
they started to a **subreaper that is not pid 1**, as on the machine this was
measured on. The last section runs the real `/proc` adapter on real, harmless
`sleep` processes: each of those tests mints marks of its own, starts only its
own processes, and ends only what it started, by the record taken right after
the spawn (pid and start time), or through `Popen`.

Every test writes only under `tmp_path`, and never reads or writes `~/.agents`
(`HOME` is pointed into `tmp_path`). Every test names, in its docstring, the
failure it catches.
"""
from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from hoh import cli, controller, dispatchers, herdr, pane_ledger, procs, roles
from hoh.contracts import Budgets, Condition, Role
from hoh.controller import (
    AnswerMissing,
    Controller,
    DispatchError,
    PaneNotClean,
    RoleNotQuiescent,
    WaitingForApproval,
)
from hoh.dispatchers import HerdrDispatcher, PaneRelease, _Base
from hoh.store import RunStore

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

WS = "w1"
CALLER_TAB, CALLER = "w1:t0", "w1:p0"
#: A screen Herdr shows for a live pane.
SCREEN = "PANE-SENTINEL"
#: Claude Code's empty input box (the screen `_check_restored` reads).
RULE = "─" * 88
EMPTY_BOX = (f"⏺ Done.\n✻ Baked for 2m 3s\n{RULE}\n❯ \n{RULE}\n"
             "  ⏵⏵ auto mode on (shift+tab to cycle)\n")
RESIDUE_PANE = (f"✻ Baked for 0s\n{RULE}\n❯ unsent text typed by an earlier "
                f"process\n{RULE}\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")
OPERATOR_DIR = "/home/someone/operator-work"


def _ok(payload=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stderr="",
                                 stdout=json.dumps(payload if payload is not None
                                                   else {"result": {}}))


def _text(view: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stderr="", stdout=view)


def _error(code: str, message: str = "") -> types.SimpleNamespace:
    """A failed Herdr call, in the form Herdr writes it to stderr."""
    return types.SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(
        {"error": {"code": code, "message": message or code}, "id": "cli"}))


@pytest.fixture(autouse=True)
def _never_the_real_herdr(monkeypatch, tmp_path):
    """No test may reach the real Herdr or the operator's `~/.agents`."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test reached the real Herdr: {args!r}")

    monkeypatch.setattr(herdr, "subprocess", types.SimpleNamespace(
        run=refuse, TimeoutExpired=subprocess.TimeoutExpired))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


# --------------------------------------------------------------------------- #
# The fake process table
# --------------------------------------------------------------------------- #


class FakeProcs:
    """The stand-in for `procs.ProcTable`.

    Every pane has a shell (a session leader below the Herdr server) and an
    agent process (the shell's child), both carrying the mark the dispatcher
    passed to `tab create`. **Closing a pane ends those two and orphans
    whatever they started** to `reaper`: a subreaper that is not pid 1, as on
    the measured machine (a few tests set it to 1). `background` and `daemon`
    start the commands a role leaves running.

    Switches: `stubborn` pids ignore SIGTERM, `immortal` pids ignore
    everything, `dies_after[pid]` exits that many snapshots after SIGTERM,
    `on_death[pid]` runs when a process dies (it may start another),
    `unreadable` makes every snapshot fail, `fail_next` the next n, and
    `fail_after_close` every snapshot from the first pane close on. A signal to
    a pid whose start time changed is refused, like the real table. Time is
    fake: `sleep` advances `clock`.
    """

    SERVER = 4000

    def __init__(self, reaper: int = 1540) -> None:
        self.reaper = reaper
        self.now = 0.0
        self.procs: dict[int, procs.Proc] = {}
        self.signals: list[tuple[int, int]] = []
        self.stubborn: set[int] = set()
        self.immortal: set[int] = set()
        self.dies_after: dict[int, int] = {}
        self.on_death: dict[int, object] = {}
        self.unreadable = False
        self.fail_next = 0
        self.fail_after_close = False
        self.closed_once = False
        self.snapshots = 0
        #: ("close", pane, now) and ("signal", pid, sig, now), in order.
        self.events: list[tuple] = []
        #: Called after a pane's shell and agent are gone (a command that
        #: exits by itself, a pid that is reused).
        self.on_close = None
        self.pane_procs: dict[str, dict] = {}
        self._dying: dict[int, int] = {}
        self._pids = itertools.count(5000)
        self._starts = itertools.count(1000, 7)
        self.add(1, ppid=0, comm="systemd")
        if reaper != 1:
            self.add(reaper, ppid=1, comm="Relay")
        self.add(self.SERVER, ppid=reaper, comm="herdr")
        me = os.getpid()
        self.add(me, ppid=reaper, comm="pytest")

    # -- building the table ------------------------------------------------ #

    def add(self, pid: int | None = None, *, ppid: int, sid: int | None = None,
            comm: str = "bash", mark: str | None = None, state: str = "S") -> procs.Proc:
        if pid is None:
            pid = next(self._pids)
            while pid in self.procs or pid == os.getpid():
                pid = next(self._pids)
        proc = procs.Proc(pid=pid, ppid=ppid, pgrp=pid, sid=pid if sid is None else sid,
                          state=state, start=next(self._starts), comm=comm, mark=mark)
        self.procs[pid] = proc
        return proc

    def replace(self, pid: int, **changes) -> procs.Proc:
        proc = self.procs[pid]
        fields = {f: getattr(proc, f) for f in
                  ("pid", "ppid", "pgrp", "sid", "state", "start", "comm", "mark")}
        fields.update(changes)
        self.procs[pid] = procs.Proc(**fields)
        return self.procs[pid]

    def me(self) -> procs.Proc:
        return self.procs[os.getpid()]

    def pane_started(self, pane: str, mark: str | None) -> None:
        shell = self.add(ppid=self.SERVER, comm="bash", mark=mark)
        agent = self.add(ppid=shell.pid, sid=shell.sid, comm="claude", mark=mark)
        self.pane_procs[pane] = {"shell": shell.pid, "agent": agent.pid,
                                 "session": shell.sid, "mark": mark}

    def background(self, pane: str, kind: str = "attached-in-the-session", *, comm: str = "sleep") -> procs.Proc:
        """A command the role started in `pane` and left running."""
        info = self.pane_procs[pane]
        mark, session = info["mark"], info["session"]
        if kind == "attached-in-the-session":
            return self.add(ppid=info["agent"], sid=session, comm=comm, mark=mark)
        if kind == "marked-attached-in-a-session-of-its-own":
            return self.add(ppid=info["agent"], comm=comm, mark=mark)
        if kind == "scrubbed-but-attached":
            return self.add(ppid=info["agent"], comm=comm, mark=None)
        if kind in ("scrubbed-orphan-in-the-session", "non-dumpable-orphan-in-the-session"):
            return self.add(ppid=self.reaper, sid=session, comm=comm, mark=None)
        raise AssertionError(f"unknown background kind {kind}")

    def daemon(self, pane: str, *, parent: str | int = "subreaper",
               comm: str = "lavish-axi") -> procs.Proc:
        """A marked daemon that detached before the release: a session leader of
        its own, orphaned to a subreaper, to init, or hanging below a live
        unmarked process."""
        ppid = {"subreaper": self.reaper, "init": 1}.get(parent, parent)
        return self.add(ppid=ppid, comm=comm, mark=self.pane_procs[pane]["mark"])

    def descendants(self, pid: int) -> list[int]:
        out, stack = [], [pid]
        while stack:
            cur = stack.pop()
            for p in self.procs.values():
                if p.ppid == cur and p.pid not in out:
                    out.append(p.pid)
                    stack.append(p.pid)
        return out

    # -- what Herdr does to it ---------------------------------------------- #

    def pane_closed(self, pane: str) -> None:
        self.closed_once = True
        self.events.append(("close", pane, self.now))
        info = self.pane_procs.pop(pane, None)
        if not info:
            return
        for pid in (info["agent"], info["shell"]):
            if pid in self.procs:
                self._kill(pid)
        if self.on_close is not None:
            self.on_close(self)

    def _kill(self, pid: int) -> None:
        if pid not in self.procs:
            return
        self.procs.pop(pid)
        self._dying.pop(pid, None)
        for p in list(self.procs.values()):
            if p.ppid == pid:
                self.replace(p.pid, ppid=self.reaper)
        hook = self.on_death.pop(pid, None)
        if hook is not None:
            hook(self)

    # -- the table's interface (what `procs.end` and the dispatcher use) ----- #

    def snapshot(self) -> list[procs.Proc]:
        self.snapshots += 1
        if self.unreadable or (self.fail_after_close and self.closed_once):
            raise OSError("the process table cannot be read")
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("the process table failed once")
        for pid in list(self._dying):
            self._dying[pid] -= 1
            if self._dying[pid] <= 0:
                self._kill(pid)
        return list(self.procs.values())

    def signal(self, proc: procs.Proc, sig: int) -> bool:
        self.signals.append((proc.pid, int(sig)))
        self.events.append(("signal", proc.pid, int(sig), self.now))
        live = self.procs.get(proc.pid)
        if live is None or live.start != proc.start:
            return False
        if proc.pid in self.immortal:
            return True
        if sig == signal.SIGTERM:
            if proc.pid in self.stubborn:
                return True
            if proc.pid in self.dies_after:
                self._dying.setdefault(proc.pid, self.dies_after[proc.pid])
                return True
        elif sig != signal.SIGKILL:
            raise AssertionError(f"a signal other than TERM and KILL: {sig}")
        self._kill(proc.pid)
        return True

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def signalled(self) -> set[int]:
        return {pid for pid, _sig in self.signals}


# --------------------------------------------------------------------------- #
# The in-memory Herdr
# --------------------------------------------------------------------------- #


class Server:
    """The stand-in for Herdr: tabs, panes and agents.

    `snapshot()` answers with the rows Herdr's bundled schema requires
    (`agents`: `name`, `pane_id`, `tab_id`, `agent_status`, `cwd`,
    `foreground_cwd`; `panes`: `pane_id`, `tab_id`, `workspace_id`; `tabs`:
    `tab_id`, `pane_count`). `run(args)` replaces `subprocess.run` inside
    `hoh.dispatchers` and understands exactly the calls the dispatcher makes
    (`tab create`, `agent start`, `agent prompt`, `agent get`, `agent read`,
    `pane read`, `pane close`, `tab close`); **any other call fails the test**.
    The fake always lists one standing pane (`CALLER`), the one `hoh run` itself
    sits in, as a real snapshot does.

    Switches: `fail_snapshot`, `partial_snapshot` (no `panes` list),
    `empty_snapshot`, `hide` (live panes the snapshot does not list),
    `refuse_close` (the close is answered ok and not carried out),
    `after_turn_status[name]` (the status a later snapshot sees once the turn
    is over), `wakes[name]` (a background task that runs on `tick()` while the
    agent still exists), `hooks[name]` (what a role does in a turn),
    `tab_count[tab]` (what a tab row claims; `None` drops the row),
    `start_fails_after_registering`.
    """

    def __init__(self, root: Path, fprocs: FakeProcs) -> None:
        self.root = root
        self.fprocs = fprocs
        self.calls: list[list[str]] = []
        self.panes: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.tab_order: list[str] = []
        self.fail_snapshot = False
        self.partial_snapshot = False
        self.empty_snapshot = False
        self.hide: set[str] = set()
        self.refuse_close = False
        self.after_turn_status: dict[str, str] = {}
        self.wakes: dict[str, object] = {}
        self.hooks: dict[str, object] = {}
        self.tab_count: dict[str, int | None] = {}
        self.start_fails_after_registering = False
        #: Rows the snapshot lists in addition (a second agent with a name).
        self.extra_agent_rows: list[dict] = []
        #: agent name -> the Herdr error code its next prompts answer with.
        self.fail_prompt: dict[str, str] = {}
        #: Called with the pane id when Herdr closes a pane.
        self.on_pane_close: list = []
        self.screen = SCREEN
        #: Agents `agent start` made that no prompt has been sent to: until
        #: then their screen is Claude Code's empty box (the dispatcher waits
        #: for it before a first prompt, `_await_ready`).
        self.unprompted: set[str] = set()
        self.reads = 0
        self._n = itertools.count(1)
        self._add_pane(CALLER, CALLER_TAB, str(root))
        fprocs.pane_started(CALLER, None)

    # -- building the world -------------------------------------------------- #

    def _add_pane(self, pane: str, tab: str, cwd: str) -> None:
        self.panes[pane] = {"pane_id": pane, "tab_id": tab, "workspace_id": WS,
                            "cwd": cwd, "foreground_cwd": cwd}
        if tab not in self.tab_order:
            self.tab_order.append(tab)

    def operator_pane(self, *, tab: str | None = None, cwd: str = OPERATOR_DIR) -> str:
        """A pane the operator opened (optionally next to ours, in `tab`)."""
        n = next(self._n)
        pane = f"{WS}:p{100 + n}"
        self._add_pane(pane, tab or f"{WS}:t{100 + n}", cwd)
        self.fprocs.pane_started(pane, None)
        return pane

    def restored_agent(self, name: str, pane: str, tab: str, *, status: str = "idle",
                       cwd: str) -> None:
        """An agent Herdr restored by itself (the pane and the name binding)."""
        self._add_pane(pane, tab, cwd)
        self.agents[name] = {"name": name, "pane_id": pane, "tab_id": tab,
                             "workspace_id": WS, "agent": "claude",
                             "agent_status": status, "cwd": cwd, "foreground_cwd": cwd}
        self.fprocs.pane_started(pane, None)

    def tab_of(self, pane: str) -> str:
        return self.panes[pane]["tab_id"]

    def pane_ids(self) -> list[str]:
        return list(self.panes)

    def live_agents(self) -> list[str]:
        return list(self.agents)

    def kinds(self) -> list[list[str]]:
        return [c[1:3] for c in self.calls]

    def closes(self) -> list[list[str]]:
        return [c[1:] for c in self.calls if c[1:3] in (["pane", "close"], ["tab", "close"])]

    # -- the snapshot --------------------------------------------------------- #

    def snapshot(self) -> dict:
        self.reads += 1
        if self.fail_snapshot:
            raise herdr.HerdrError("herdr api snapshot: the server did not answer")
        if self.empty_snapshot:
            return {"agents": [], "panes": [], "tabs": []}
        panes = [dict(r) for p, r in self.panes.items() if p not in self.hide]
        agents = [dict(r) for r in self.agents.values() if r["pane_id"] not in self.hide]
        agents += [dict(r) for r in self.extra_agent_rows]
        tabs = []
        for tab in self.tab_order:
            count = sum(1 for r in self.panes.values() if r["tab_id"] == tab)
            if not count:
                continue
            if tab in self.tab_count:
                if self.tab_count[tab] is None:
                    continue
                count = self.tab_count[tab]
            tabs.append({"tab_id": tab, "workspace_id": WS, "pane_count": count})
        snap = {"agents": agents, "panes": panes, "tabs": tabs}
        if self.partial_snapshot:
            del snap["panes"]
        return snap

    # -- time passes ------------------------------------------------------------ #

    def tick(self, *, except_agent: str | None = None) -> None:
        """Background tasks of agents that still exist run (a woken developer)."""
        for name in list(self.wakes):
            if name in self.agents and name != except_agent:
                self.wakes.pop(name)()

    # -- subprocess.run ---------------------------------------------------------- #

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[0] != "herdr":
            raise AssertionError(f"not a Herdr call: {args!r}")
        verb = args[1:3]
        if verb == ["tab", "create"]:
            return self._tab_create(args)
        if verb == ["agent", "start"]:
            return self._agent_start(args)
        if verb == ["agent", "prompt"]:
            return self._agent_prompt(args)
        if verb == ["agent", "get"]:
            row = self.agents.get(args[3])
            if row is None:
                return _error("agent_not_found")
            return _ok({"result": {"agent": dict(row)}})
        if verb == ["agent", "read"]:
            if args[3] not in self.agents:
                return _error("agent_not_found")
            if args[3] in self.unprompted:
                return _text(EMPTY_BOX)
            return _text(self.screen)
        if verb == ["pane", "read"]:
            if args[3] not in self.panes:
                return _error("pane_not_found", f"no pane {args[3]}")
            return _text(self.screen)
        if verb == ["pane", "close"]:
            return self._close(args[3], whole_tab=False)
        if verb == ["tab", "close"]:
            return self._close(args[3], whole_tab=True)
        raise AssertionError(f"the fake Herdr does not understand {args!r}")

    def _tab_create(self, args):
        cwd = args[args.index("--cwd") + 1]
        mark = None
        for i, a in enumerate(args):
            if a == "--env" and args[i + 1].startswith(procs.MARK_ENV + "="):
                mark = args[i + 1].split("=", 1)[1]
        n = next(self._n)
        tab, pane = f"{WS}:t{n}", f"{WS}:p{n}"
        self._add_pane(pane, tab, cwd)
        self.fprocs.pane_started(pane, mark)
        return _ok({"result": {"tab": {"tab_id": tab},
                               "root_pane": {"pane_id": pane, "tab_id": tab}}})

    def _agent_start(self, args):
        name, pane = args[3], args[args.index("--pane") + 1]
        if name in self.agents:
            return _error("agent_name_taken", f"the agent name {name} is taken")
        if pane not in self.panes:
            return _error("pane_not_found")
        cwd = self.panes[pane]["cwd"]
        row = {"name": name, "pane_id": pane, "tab_id": self.panes[pane]["tab_id"],
               "workspace_id": WS, "agent": args[args.index("--kind") + 1],
               "agent_status": "idle", "cwd": cwd, "foreground_cwd": cwd}
        self.agents[name] = row
        self.unprompted.add(name)
        if self.start_fails_after_registering:
            return _error("agent_not_ready", "the agent did not become ready")
        self.tick(except_agent=name)
        return _ok({"result": {"agent": dict(row)}})

    def _agent_prompt(self, args):
        name, text = args[3], args[4]
        row = self.agents.get(name)
        if row is None:
            return _error("agent_not_found")
        self.unprompted.discard(name)
        self.tick(except_agent=name)
        if name in self.fail_prompt:
            return _error(self.fail_prompt[name])
        hook = self.hooks.get(name)
        if hook is not None:
            hook(text)
        row["agent_status"] = self.after_turn_status.get(name, "idle")
        return _ok({"result": {"agent": dict(row)}})

    def _close(self, target: str, *, whole_tab: bool):
        if self.refuse_close:
            return _ok()
        if whole_tab:
            gone = [p for p, r in self.panes.items() if r["tab_id"] == target]
            if not gone:
                return _error("tab_not_found")
        else:
            if target not in self.panes:
                return _error("pane_not_found")
            gone = [target]
        for pane in gone:
            self.panes.pop(pane)
            for name in [n for n, r in self.agents.items() if r["pane_id"] == pane]:
                self.agents.pop(name)
                self.wakes.pop(name, None)
            self.fprocs.pane_closed(pane)
            for hook in self.on_pane_close:
                hook(pane)
        return _ok()


# --------------------------------------------------------------------------- #
# The rig: a real RunStore, a real HerdrDispatcher, a real Controller
# --------------------------------------------------------------------------- #


class Clock:
    """The dispatcher's `time`: every `monotonic()` advances by `step`
    seconds and `sleep` only records, so no test waits a real second."""

    def __init__(self, step: float = 1000.0) -> None:
        self.now = 0.0
        self.step = step
        self.fixed = time.time() - 10.0

    def monotonic(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        pass

    def time(self) -> float:
        return self.fixed


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class Rig:
    """A run store, a Herdr-free `HerdrDispatcher` over the `Server`, and a
    real `Controller`. `profiles` names a harness for every role."""

    def __init__(self, root: Path, monkeypatch, *, reaper: int = 1540,
                 git_repo: bool = False) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.monkeypatch = monkeypatch
        self.repo = root / "repo"
        self.repo.mkdir()
        if git_repo:
            _git(self.repo, "init", "-q")
            _git(self.repo, "config", "user.email", "t@example.invalid")
            _git(self.repo, "config", "user.name", "T")
            (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                              encoding="utf-8")
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-qm", "init")
        self.spec = root / "spec.md"
        self.spec.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
        self.store = RunStore(root / "runs", "r1")
        self.state = Controller.new_state(
            run_id="r1", repo_path=self.repo, project_name="p", spec_path=self.spec,
            budgets=Budgets(max_iterations=5))
        self.store.write_state(self.state)
        self.fprocs = FakeProcs(reaper=reaper)
        self.server = Server(root, self.fprocs)
        d = HerdrDispatcher.__new__(HerdrDispatcher)
        _Base.__init__(d, answers_dir=self.store.dir / "answers")
        d.profiles = {r: "claude" for r in Role}
        d.cwd = str(self.repo)
        d.timeout_ms = 1000
        d.agents = {}
        d.own_panes = []
        d.own_tabs = []
        self.d = d
        monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
            run=self.server.run, TimeoutExpired=subprocess.TimeoutExpired))
        monkeypatch.setattr(herdr, "snapshot", self.server.snapshot)
        monkeypatch.setattr(herdr, "read_agent",
                            lambda target, lines=40: self.server.screen)
        monkeypatch.setattr(dispatchers, "time", Clock())
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(d, "_proc_table", lambda: self.fprocs)
        self.controller = Controller(self.store, d, spec_path=self.spec)

    # -- the world ------------------------------------------------------------ #

    def name(self, role: Role) -> str:
        return self.d._agent_name(role, self.state)

    def pane(self, role: Role) -> str:
        """Start the role's agent in a pane of its own (as its first dispatch
        does) and return the pane id."""
        self.d._ensure_agent(role, self.state)
        return self.d._role_panes()[role]

    def ledger(self) -> list[dict]:
        path = self.store.dir / pane_ledger.LEDGER
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def events(self, event: str, **match) -> list[dict]:
        return [r for r in self.ledger() if r["event"] == event
                and all(r.get(k) == v for k, v in match.items())]

    def summary(self) -> dict:
        return pane_ledger.summary(self.store.dir)

    def release(self, role: Role, **kw) -> PaneRelease:
        return self.d.release_role(role, self.state, **kw)

    def restored(self, role: Role, *, pane: str = "w9:p7", tab: str = "w9:t7",
                 status: str = "idle", cwd: str | None = None) -> str:
        """Herdr restored this run's agent for `role`; HoH adopts it."""
        name = self.name(role)
        self.server.restored_agent(name, pane, tab, status=status,
                                   cwd=cwd or str(self.store.dir))
        return name


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path / "rig", monkeypatch)


# =========================================================================== #
# K1 -- the closing rule: only what HoH can prove is its own (item 1)
# =========================================================================== #


def test_a_role_pane_alone_in_its_tab_is_closed_with_the_tab(rig):
    """Catches: the control of the closing rule broken -- a role pane that is
    alone in its tab (the normal case) is closed with the tab, the ledger says
    so, and HoH forgets the pane and the tab. Also the created pane being
    recorded for its role (O10) and a closed pane staying in `own_panes` (L9)."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    assert rig.d._role_panes()[Role.PLANNER] == pane and pane in rig.d.own_panes

    gone = rig.d.close_own()

    assert rig.server.closes() == [["tab", "close", tab]]
    assert gone == [tab]
    assert pane not in rig.server.panes and rig.name(Role.PLANNER) not in rig.server.agents
    assert rig.d.own_panes == [] and rig.d.own_tabs == [] and rig.d.unconfirmed == []
    assert Role.PLANNER not in rig.d.agents, "its next turn would start a fresh agent"
    assert [r["event"] for r in rig.ledger() if r.get("pane") == pane] == ["closing", "closed"]
    assert rig.events("closed", pane=pane)[0]["reason"] == "run end"
    assert CALLER in rig.server.panes, "the pane `hoh run` sits in is never touched"


def test_a_pane_the_operator_opened_next_to_ours_survives_the_run_end(rig):
    """Catches: `close_own` closing the whole tab and the pane the operator
    opened next to a role pane with it (the defect; O2). The own pane alone is
    closed, the tab that survives because of the operator's pane is said to be
    kept (L10) and is no longer HoH's, and nothing is left unconfirmed."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    operator = rig.server.operator_pane(tab=tab)

    gone = rig.d.close_own()

    assert rig.server.closes() == [["pane", "close", pane]]
    assert operator in rig.server.panes, "the operator's pane survives"
    assert pane not in rig.server.panes
    assert gone == [], "the tab is not gone: the operator's pane holds it"
    assert rig.d.own_tabs == [] and rig.d.unconfirmed == []
    kept = rig.events("kept", tab=tab)
    assert kept and operator in kept[0]["why"], "the tab is said to be kept, and why"


def test_releasing_a_role_next_to_an_operator_pane_closes_only_the_role_pane(rig):
    """Catches: the turn-end release closing the tab of a role pane that has an
    operator's pane next to it. Only the role pane closes; the tab leaves
    `own_tabs` (L11) so the run end does not come back for it."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    operator = rig.server.operator_pane(tab=tab)

    answer = rig.release(Role.PLANNER)

    assert answer.closed is True, answer
    assert rig.server.closes() == [["pane", "close", pane]]
    assert operator in rig.server.panes
    assert tab not in rig.d.own_tabs
    assert rig.d.close_own() == [] and rig.server.closes() == [["pane", "close", pane]]


def test_an_adopted_pane_is_closed_alone_at_the_run_end(rig):
    """Catches: the tab of an **adopted** (Herdr-restored) agent being closed
    whole -- `_reusable_agent` put it into `own_tabs`, a tab HoH did not create
    (the defect) -- and an adopted pane not being HoH's to close at the run end
    (O8). Control: the same pane, alone in its tab, goes with the tab."""
    name = rig.restored(Role.QA)
    operator = rig.server.operator_pane(tab="w9:t7")
    assert rig.d._ensure_agent(Role.QA, rig.state) == name

    rig.d.close_own()

    assert rig.server.closes() == [["pane", "close", "w9:p7"]]
    assert operator in rig.server.panes and name not in rig.server.agents
    assert rig.d.unconfirmed == []

    control = Rig(rig.root.parent / "control", rig.monkeypatch)
    control.restored(Role.QA)
    control.d._ensure_agent(Role.QA, control.state)
    control.d.close_own()
    assert control.server.closes() == [["tab", "close", "w9:t7"]]


def test_an_adopted_pane_is_closed_alone_at_the_turn_end(rig):
    """Catches: the adopted role's pane not being recorded, so its turn end
    cannot release it (O9), and a tab closed whole around an operator's pane.
    The pane **is** closed; the release is not clean, because a pane HoH did
    not create has no mark and what its role left running cannot be told
    (Q12): the developer would not be frozen."""
    name = rig.restored(Role.QA)
    operator = rig.server.operator_pane(tab="w9:t7")
    rig.d._ensure_agent(Role.QA, rig.state)
    assert rig.d._role_panes()[Role.QA] == "w9:p7"

    answer = rig.release(Role.QA)

    assert rig.server.closes() == [["pane", "close", "w9:p7"]]
    assert operator in rig.server.panes and name not in rig.server.agents
    assert answer.closed is False and "mark" in answer.why
    assert rig.events("closed", pane="w9:p7"), "the pane itself is closed and recorded"
    assert rig.events("refused", pane="w9:p7")[0]["why"] == answer.why


def test_a_dead_restored_agents_pane_is_closed_without_its_tab(rig):
    """Catches: the dead-agent branch closing a whole tab by id, swallowing the
    error and recording nothing (the defect; O3, O6, O7). The dead agent's
    **pane** is closed, the operator's pane in its tab survives, the close is
    on record, and a fresh agent follows. Control: a dead agent alone in its
    tab is also closed by `pane close`, never `tab close`."""
    name = rig.restored(Role.PLANNER, status="unknown")
    operator = rig.server.operator_pane(tab="w9:t7")

    assert rig.d._ensure_agent(Role.PLANNER, rig.state) == name

    assert rig.server.closes() == [["pane", "close", "w9:p7"]]
    assert operator in rig.server.panes and "w9:p7" not in rig.server.panes
    assert rig.server.agents[name]["pane_id"] != "w9:p7", "a fresh agent, in a new pane"
    closed = rig.events("closed", pane="w9:p7")
    assert closed and closed[0]["reason"] == "dead restored agent"

    alone = Rig(rig.root.parent / "alone", rig.monkeypatch)
    alone.restored(Role.PLANNER, status="unknown")
    alone.d._ensure_agent(Role.PLANNER, alone.state)
    assert alone.server.closes() == [["pane", "close", "w9:p7"]]


@pytest.mark.parametrize("case", [
    pytest.param("row-counts-two", id="row-counts-two"),
    pytest.param("no-row-for-the-tab", id="no-row-for-the-tab"),
    pytest.param("exact", id="control-exact-count"),
])
def test_a_tab_is_closed_whole_only_when_its_own_row_counts_exactly_our_panes(rig, case):
    """Catches: a whole-tab close on a tab row that does not count exactly the
    panes the snapshot lists for it (O4), or on a snapshot with no row for the
    tab (O5). Then the own pane alone is closed. Control: an exact count
    closes the tab."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    if case == "row-counts-two":
        rig.server.tab_count[tab] = 2
    elif case == "no-row-for-the-tab":
        rig.server.tab_count[tab] = None

    rig.release(Role.PLANNER)

    want = ["tab", "close", tab] if case == "exact" else ["pane", "close", pane]
    assert rig.server.closes() == [want]
    assert pane not in rig.server.panes


def test_a_pane_that_is_already_gone_is_recorded_not_closed_again(rig):
    """Catches: a pane that is gone being closed a second time, or not
    recorded (F6), or a close sent for a pane that is not there. The snapshot
    no longer lists it **and** Herdr answers `pane_not_found`: `closed` with
    `already_gone`, no command, at the turn end and at the run end."""
    planner = rig.pane(Role.PLANNER)
    qa = rig.pane(Role.QA)
    rig.server._close(planner, whole_tab=False)
    rig.server._close(qa, whole_tab=False)

    answer = rig.release(Role.PLANNER)
    rig.d.close_own()

    assert answer.closed is True, answer
    assert rig.server.closes() == [], "no command is sent for a pane that is not there"
    for pane in (planner, qa):
        line = rig.events("closed", pane=pane)
        assert line and line[0]["already_gone"] is True
    assert rig.d.own_panes == [] and rig.d.unconfirmed == []


def test_a_pane_of_ours_in_no_tab_of_ours_is_closed_alone_and_still_reported(rig):
    """Catches: a pane whose tab HoH never recorded being closed by the tab id
    or being forgotten (L17): it is closed alone, and when Herdr does not carry
    the close out it is reported by its **own id** in `unconfirmed`."""
    pane = rig.pane(Role.PLANNER)
    rig.d.own_tabs.clear()
    rig.d.close_own()
    assert rig.server.closes() == [["pane", "close", pane]]
    assert pane not in rig.server.panes

    stuck = Rig(rig.root.parent / "stuck", rig.monkeypatch)
    pane = stuck.pane(Role.PLANNER)
    stuck.d.own_tabs.clear()
    stuck.server.refuse_close = True
    stuck.d.close_own()
    assert stuck.d.unconfirmed == [pane]
    assert pane in stuck.d.own_panes


def test_a_tab_is_closed_whole_only_when_every_pane_in_it_still_is_what_was_recorded(rig):
    """Catches: a whole-tab close that checks the identity of the released pane
    but not of the others in the tab (O13): the second pane's id now names the
    operator's shell (a Herdr restart reused it), so only the released pane
    closes and the operator's survives. Control: with the second pane still the
    agent HoH adopted there, the tab closes whole."""
    planner = rig.restored(Role.PLANNER, pane="w9:p7", tab="w9:t7")
    qa = rig.restored(Role.QA, pane="w9:p8", tab="w9:t7")
    rig.d._ensure_agent(Role.PLANNER, rig.state)
    rig.d._ensure_agent(Role.QA, rig.state)
    # An operator shell now sits in the second pane's id.
    rig.server.agents.pop(qa)
    rig.server.panes["w9:p8"]["cwd"] = rig.server.panes["w9:p8"]["foreground_cwd"] = OPERATOR_DIR

    rig.d.close_own()

    assert ["pane", "close", "w9:p7"] in rig.server.closes()
    assert ["tab", "close", "w9:t7"] not in rig.server.closes()
    assert "w9:p8" in rig.server.panes, "the operator's shell survives"
    assert planner not in rig.server.agents

    control = Rig(rig.root.parent / "control", rig.monkeypatch)
    control.restored(Role.PLANNER, pane="w9:p7", tab="w9:t7")
    control.restored(Role.QA, pane="w9:p8", tab="w9:t7")
    control.d._ensure_agent(Role.PLANNER, control.state)
    control.d._ensure_agent(Role.QA, control.state)
    control.d.close_own()
    assert control.server.closes()[0] == ["tab", "close", "w9:t7"]
    assert control.d.unconfirmed == []


def test_a_pane_whose_agent_exited_but_whose_directory_is_this_runs_is_still_closed(rig):
    """Catches: the live identity check being over-strict (O16): the agent
    exited and left a plain shell in the directory HoH made the pane in -- that
    is still the run's pane and is closed. Control of the next test's refusal."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    rig.server.agents.pop(rig.name(Role.PLANNER))

    answer = rig.release(Role.PLANNER)

    assert answer.closed is True, answer
    assert pane not in rig.server.panes
    assert rig.server.closes() == [["tab", "close", tab]]


def test_a_dead_agents_pane_that_could_not_be_closed_stays_tracked_and_reported(rig):
    """Catches: the dead-agent branch forgetting a pane it failed to close (F14):
    the pane goes into `own_panes`, the agent's name stays recorded with it
    (D2, D4: the identity check must recognise the dead agent's row), and the run
    end -- Herdr cooperating again -- closes it. While it is not closed it is
    in `unconfirmed`. A real Herdr answers `agent_name_taken` to the fresh
    start, so the dispatch fails."""
    name = rig.restored(Role.PLANNER, status="unknown")
    rig.server.refuse_close = True

    with pytest.raises(DispatchError, match="agent_name_taken|name"):
        rig.d._ensure_agent(Role.PLANNER, rig.state)

    assert "w9:p7" in rig.d.own_panes, "proven this run's, so the run end retries it"
    assert name in rig.d._pane_names()["w9:p7"]
    assert rig.events("refused", pane="w9:p7")
    rig.d.close_own()
    assert "w9:p7" in rig.d.unconfirmed or "w9:p7" in rig.d.own_panes

    rig.server.refuse_close = False
    rig.d.close_own()
    assert "w9:p7" not in rig.server.panes and name not in rig.server.agents
    assert rig.d.unconfirmed == []


def test_a_failed_start_leaves_a_pane_the_run_end_can_still_close(rig):
    """Catches: a pane whose `agent start` failed *after* Herdr registered the
    agent being lost -- the identity check did not know the name, because it was
    recorded only after the start (D3, D2). The agent name is recorded with the
    pane **before** the start, so the run end closes the pane."""
    rig.server.start_fails_after_registering = True
    with pytest.raises(DispatchError):
        rig.d._ensure_agent(Role.PLANNER, rig.state)
    pane = rig.d._role_panes()[Role.PLANNER]
    assert pane in rig.server.panes and rig.name(Role.PLANNER) in rig.server.agents

    rig.d.close_own()

    assert pane not in rig.server.panes and rig.server.agents == {}
    assert rig.d.unconfirmed == [] and rig.d.own_panes == []


# =========================================================================== #
# K2 -- fail closed: whatever cannot be proven is kept, and the reason recorded
# =========================================================================== #

ANSWER = '{"ok": true}'


def _boom(*args, **kwargs):
    raise OSError(28, "No space left on device")


def _answers(rig: Rig, role: Role, text: str = ANSWER) -> None:
    """The role answers its next turn the way a real one does: with the file."""
    rig.server.hooks[rig.name(role)] = lambda prompt: rig.d._answer_path(
        role, rig.state).write_text(text, encoding="utf-8")


_SNAPSHOT_FAULTS = {
    "unreadable": lambda s: setattr(s, "fail_snapshot", True),
    "no-pane-list": lambda s: setattr(s, "partial_snapshot", True),
    "all-empty": lambda s: setattr(s, "empty_snapshot", True),
}


@pytest.mark.parametrize("fault", list(_SNAPSHOT_FAULTS))
def test_nothing_is_closed_at_the_run_end_when_the_snapshot_cannot_prove_it(rig, fault):
    """Catches: a close on a snapshot that proves nothing -- unreadable (F1),
    without the pane list (F2), or listing no pane at all, which `hoh run`
    itself sitting in a Herdr pane makes a failed read and never "the pane is
    gone" (F13). Nothing is sent, nothing is booked `already_gone`, the pane
    stays tracked and is reported. Control: once the snapshot is whole again,
    the same call closes it."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    _SNAPSHOT_FAULTS[fault](rig.server)

    rig.d.close_own()

    assert rig.server.closes() == []
    assert not [r for r in rig.ledger() if r.get("already_gone")]
    assert pane in rig.d.own_panes and pane in rig.server.panes
    assert tab in rig.d.unconfirmed or pane in rig.d.unconfirmed
    assert rig.events("refused", pane=pane)

    rig.server.fail_snapshot = rig.server.partial_snapshot = rig.server.empty_snapshot = False
    rig.d.close_own()
    assert rig.server.closes() == [["tab", "close", tab]]
    assert rig.d.unconfirmed == []


@pytest.mark.parametrize("fault", list(_SNAPSHOT_FAULTS))
def test_nothing_is_released_at_the_turn_end_when_the_snapshot_cannot_prove_it(rig, fault):
    """Catches: the turn-end release closing a pane on a snapshot that proves
    nothing (F12 for the unreadable one, F13 for the empty one): `closed` is
    False with its own reason, nothing is sent, the refusal is recorded, and
    nothing is `already_gone` on a read that showed nothing. Control: a whole
    snapshot releases the same pane."""
    pane = rig.pane(Role.PLANNER)
    _SNAPSHOT_FAULTS[fault](rig.server)

    answer = rig.release(Role.PLANNER)

    assert answer.closed is False and answer.why
    assert rig.server.closes() == []
    assert not [r for r in rig.ledger() if r.get("already_gone")]
    assert rig.events("refused", pane=pane, role="planner")[0]["why"] == answer.why

    rig.server.fail_snapshot = rig.server.partial_snapshot = rig.server.empty_snapshot = False
    assert rig.release(Role.PLANNER).closed is True


def test_a_close_that_herdr_does_not_carry_out_stays_unconfirmed_and_is_retried(rig):
    """Catches: a close being confirmed by having asked for it (F3): Herdr
    answers ok and does nothing, a second snapshot still lists the pane, so it
    stays tracked, is reported in `unconfirmed`, is on record as refused (with
    its `closing` line before it), and the next run end retries it."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    rig.server.refuse_close = True

    gone = rig.d.close_own()

    assert gone == [] and rig.server.closes() == [["tab", "close", tab]]
    assert pane in rig.d.own_panes and tab in rig.d.own_tabs
    assert rig.d.unconfirmed == [tab]
    assert [r["event"] for r in rig.ledger() if r.get("pane") == pane] == ["closing", "refused"]
    assert "not confirmed" in rig.events("refused", pane=pane)[0]["why"]
    assert pane in [e["pane"] for e in rig.summary()["refused"]]

    rig.server.refuse_close = False
    assert rig.d.close_own() == [tab]
    assert pane not in rig.server.panes and rig.d.unconfirmed == []
    assert rig.summary()["closed"] == 1 and rig.summary()["refused"] == []


def test_a_close_that_cannot_be_recorded_is_not_attempted(rig, monkeypatch):
    """Catches: a close sent although its `closing` line cannot be written (F4):
    the record comes first or the close does not happen -- at the run end and at
    the turn end. The pane stays tracked and is reported. Control: with the
    ledger writable the same close happens."""
    pane = rig.pane(Role.PLANNER)
    with monkeypatch.context() as broken:
        broken.setattr(pane_ledger, "durable_append", _boom)
        rig.d.close_own()
        answer = rig.release(Role.PLANNER)
    assert rig.server.closes() == []
    assert answer.closed is False and "recorded" in answer.why
    assert pane in rig.d.own_panes and pane in rig.server.panes
    assert rig.d.unconfirmed, "an unattempted close is an unconfirmed one"

    assert rig.d.close_own()
    assert rig.server.closes() and pane not in rig.server.panes


def test_every_close_is_recorded_before_it_is_asked_for(rig, monkeypatch):
    """Catches: the `closing` line written after the command (F5), so a crash
    between the two leaves a close nobody recorded. The ledger is read at the
    instant each close command reaches Herdr: it already ends in the `closing`
    line of exactly that pane -- for a pane close, a tab close and a restored
    pane's replacement."""
    seen: list[tuple[list[str], list[dict]]] = []
    real = rig.server.run

    def watching(args, **kwargs):
        if list(args[1:3]) in (["pane", "close"], ["tab", "close"]):
            seen.append((list(args[1:]), rig.ledger()))
        return real(args, **kwargs)

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=watching, TimeoutExpired=subprocess.TimeoutExpired))
    planner = rig.pane(Role.PLANNER)
    qa = rig.pane(Role.QA)
    planner_tab = rig.server.tab_of(planner)
    rig.server.operator_pane(tab=rig.server.tab_of(qa))
    rig.release(Role.PLANNER)          # a tab close
    rig.release(Role.QA)               # a pane close, next to an operator's pane

    assert [c for c, _ in seen] == [["tab", "close", planner_tab], ["pane", "close", qa]]
    for (command, ledger), pane in zip(seen, (planner, qa)):
        last = ledger[-1]
        assert last["event"] == "closing", (command, ledger)
        assert last["scope"] == command[0] and last["pane"] == pane


@pytest.mark.parametrize("case", [
    pytest.param("not-own", id="not-own"),
    pytest.param("agent-in-another-pane", id="agent-in-another-pane"),
    pytest.param("working", id="working"),
    pytest.param("unknown-status", id="unknown-status"),
    pytest.param("idle-closes", id="idle-closes"),
])
def test_release_refuses_a_pane_that_is_not_ours_or_not_where_we_put_it(rig, case):
    """Catches: a release that closes a pane that is not an own pane (O1), an
    agent that sits in another pane than the one HoH created for it (F7), an
    agent that is working again after its turn ended (F8), and a status that
    does not prove the turn ended (F9). Each is `closed=False`, with its
    reason, recorded, nothing sent. Control (`idle-closes`): an idle agent in
    its own pane is released."""
    pane = rig.pane(Role.PLANNER)
    name = rig.name(Role.PLANNER)
    if case == "not-own":
        rig.d.own_panes.remove(pane)
    elif case == "agent-in-another-pane":
        other = rig.server.operator_pane()
        rig.server.agents[name]["pane_id"] = other
    elif case == "working":
        rig.server.agents[name]["agent_status"] = "working"
    elif case == "unknown-status":
        rig.server.agents[name]["agent_status"] = "unknown"

    answer = rig.release(Role.PLANNER)

    if case == "idle-closes":
        assert answer.closed is True and rig.server.closes()
        return
    assert answer.closed is False and answer.why
    assert rig.server.closes() == []
    assert rig.events("refused", role="planner")[-1]["why"] == answer.why
    if case == "working":
        assert "working" in answer.why
    if case == "agent-in-another-pane":
        assert "another" in answer.why or "not in pane" in answer.why


def test_release_refuses_when_two_agents_carry_the_role_name(rig):
    """Catches: a release acting on one of two agents that carry the role's
    name (F11): it is not known which is the role's. `closed=False`, nothing
    sent. Control: with one agent the release closes."""
    rig.pane(Role.QA)
    name = rig.name(Role.QA)
    rig.server.extra_agent_rows.append(dict(rig.server.agents[name], pane_id="w7:p7"))

    answer = rig.release(Role.QA)

    assert answer.closed is False and "2 agents" in answer.why
    assert rig.server.closes() == []

    rig.server.extra_agent_rows.clear()
    assert rig.release(Role.QA).closed is True


@pytest.mark.parametrize("case", [
    pytest.param("an-operator-pane", id="an-operator-pane"),
    pytest.param("an-unknown-agent", id="an-unknown-agent"),
])
def test_a_recorded_pane_id_that_now_names_another_pane_is_not_closed(rig, case):
    """Catches: the recorded pane id alone being trusted (the live identity,
    O12, O14, O15): Herdr documents that ids are not reused for a running
    server, not across a restart. The id now names an operator's shell in
    another directory, or an agent HoH never started: neither is closed, at the
    turn end or at the run end, and the refusal is recorded. Control: the
    pane that is still what was recorded closes (the other tests)."""
    pane = rig.pane(Role.PLANNER)
    name = rig.name(Role.PLANNER)
    if case == "an-operator-pane":
        rig.server.agents.pop(name)
        rig.server.panes[pane]["cwd"] = rig.server.panes[pane]["foreground_cwd"] = OPERATOR_DIR
    else:
        row = rig.server.agents.pop(name)
        rig.server.agents["captains-research"] = dict(row, name="captains-research")

    answer = rig.release(Role.PLANNER)
    rig.d.close_own()

    assert answer.closed is False
    assert rig.server.closes() == []
    assert pane in rig.server.panes
    assert rig.events("refused", pane=pane)
    assert pane in rig.d.own_panes, "still tracked, so the operator is told"


_CLOSE_SPELLING = re.compile(
    r"""["']\s*(?:pane|tab|workspace)\s*["']\s*,\s*["']\s*close\s*["']"""
    r"""|\b(?:pane|tab|workspace)\s+close\b""")


def _close_sites(source: str, class_name: str = "HerdrDispatcher") -> set[str]:
    """The functions of `class_name` (methods, static and class methods, and
    functions nested in them) whose source spells a Herdr `pane close`, `tab
    close` or `workspace close` command -- in single or double quotes, with any
    spacing. **A tripwire for the literal spelling**: a verb built at run time,
    or a command issued from another class or module, is not caught."""
    tree = ast.parse(source)
    lines = source.splitlines()
    sites: set[str] = set()
    for cls in (n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == class_name):
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
            # Docstrings and comments are not commands.
            code = ast.get_source_segment(source, fn) or body
            stripped = re.sub(r'"""(?:.|\n)*?"""', "", code)
            stripped = re.sub(r"#.*", "", stripped)
            if _CLOSE_SPELLING.search(stripped):
                sites.add(fn.name)
    return sites


def test_only_two_functions_ask_herdr_to_close_something():
    """Catches: a third place that asks Herdr to close a pane, tab or workspace
    (C5, C6; the pin had three: `close_own`, `_reusable_agent`,
    `_replace_restored`). Only `_close_own_pane` (the rules of item 1) and
    `_replace_restored` (its own, stricter proof) spell the command. A **tripwire
    for the literal spelling**, in either quote and with any spacing; code
    review is the other half."""
    source = (Path(dispatchers.__file__)).read_text(encoding="utf-8")
    # P2-17: the dispatcher no longer spells a command at all. The close goes
    # through `self.control.close_pane` / `close_tab`, whose Herdr spelling is
    # `HerdrAgentControl`'s, and the two functions that may call them are
    # pinned by `tests/test_agent_control.py::test_the_dispatcher_closes_only_through_two_functions`.
    assert _close_sites(source) == set()
    control = (Path(dispatchers.__file__).parent / "agentcontrol.py").read_text(encoding="utf-8")
    assert _close_sites(control, "HerdrAgentControl") == {"close_pane", "close_tab"}

    # The scanner itself sees every spelling it claims to.
    for spelling in ("['herdr', 'tab' ,  'close', x]", '["herdr", "pane", "close", x]',
                     "['herdr','workspace','close',x]", '"herdr tab close"'):
        planted = f"class HerdrDispatcher:\n    def third(self, x):\n        run({spelling})\n"
        assert _close_sites(planted) == {"third"}, spelling
    harmless = ("class HerdrDispatcher:\n    def other(self):\n"
                '        """pane close is only prose here"""\n'
                '        return ["herdr", "pane", "read", 1]\n')
    assert _close_sites(harmless) == set()


def _restored_with_residue(rig: Rig, *, keep: bool = False) -> str:
    """Herdr restored this run's QA agent in a tab of its own, and its input box
    holds text an earlier process typed: the first prompt replaces the pane."""
    name = rig.restored(Role.QA)
    rig.server.screen = RESIDUE_PANE
    _answers(rig, Role.QA)
    return name


def test_a_restored_pane_that_is_replaced_is_recorded_like_every_other_close(rig, monkeypatch):
    """Catches: the one stated exception to "one place closes" (C2, C4) being a
    silent second path: `_replace_restored` writes the same ledger lines --
    `closing` before the command, `closed` after the confirmation, each with the
    agent and the reason `restored pane replaced` -- and the fresh agent that
    follows answers."""
    name = _restored_with_residue(rig)
    ledgers: list[list[dict]] = []
    real = rig.server.run

    def watching(args, **kwargs):
        if list(args[1:3]) == ["pane", "close"]:
            ledgers.append(rig.ledger())
        return real(args, **kwargs)

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=watching, TimeoutExpired=subprocess.TimeoutExpired))

    answer = rig.d.dispatch(Role.QA, "p", state=rig.state)

    assert answer == ANSWER
    assert rig.server.closes() == [["pane", "close", "w9:p7"]]
    assert ledgers and ledgers[0][-1]["event"] == "closing"
    assert ledgers[0][-1]["agent"] == name and ledgers[0][-1]["reason"] == "restored pane replaced"
    closed = rig.events("closed", pane="w9:p7")
    assert closed and closed[0]["reason"] == "restored pane replaced" and closed[0]["agent"] == name
    assert rig.server.agents[name]["pane_id"] != "w9:p7", "a fresh agent in a new pane"


def test_a_replacement_that_cannot_be_recorded_is_not_attempted(rig, monkeypatch):
    """Catches: the second close site sending its close although its `closing`
    line cannot be written (C3). `PaneNotClean`, nothing closed, nothing sent,
    the restored agent still there. Control: with a writable ledger the same
    dispatch replaces the pane (the test above)."""
    name = _restored_with_residue(rig)
    monkeypatch.setattr(pane_ledger, "durable_append", _boom)

    with pytest.raises(PaneNotClean, match="recorded"):
        rig.d.dispatch(Role.QA, "p", state=rig.state)

    assert rig.server.closes() == []
    assert name in rig.server.agents and "w9:p7" in rig.server.panes
    assert [c for c in rig.server.calls if c[1:3] == ["agent", "prompt"]] == []


@pytest.mark.parametrize("when", [
    pytest.param("before-close", id="before-close"),
    pytest.param("after-close", id="after-close"),
])
def test_a_snapshot_that_hides_a_live_pane_does_not_make_it_gone(rig, monkeypatch, when):
    """Catches: a snapshot that lists only some of the panes making a live pane
    "gone" -- before a close was asked for (G1: no `already_gone`, nothing
    sent) or as its confirmation (G2: Herdr must also answer `pane_not_found`).
    The pane stays tracked and the refusal says Herdr still answers for it."""
    pane = rig.pane(Role.PLANNER)
    if when == "before-close":
        rig.server.hide.add(pane)
    else:
        rig.server.refuse_close = True
        real = rig.server.run

        def hiding(args, **kwargs):
            reply = real(args, **kwargs)
            if list(args[1:3]) in (["pane", "close"], ["tab", "close"]):
                rig.server.hide.add(pane)
            return reply

        monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
            run=hiding, TimeoutExpired=subprocess.TimeoutExpired))

    rig.d.close_own()

    assert not rig.events("closed", pane=pane)
    assert pane in rig.d.own_panes and pane in rig.server.panes
    why = rig.events("refused", pane=pane)[-1]["why"]
    assert "Herdr" in why and ("answers" in why or "does not say" in why)
    assert (rig.server.closes() == []) == (when == "before-close")


def test_an_operators_shell_in_another_directory_of_the_run_is_not_taken_for_the_pane(rig):
    """Catches: the directory test comparing with every directory of the run
    instead of the one HoH made **this pane** in (D1): an operator's shell that
    took the id and sits in the run's work tree is not the planner's pane (made
    in the planner's arena). Control: a shell below the arena is still the
    pane."""
    arena = rig.root / "arena"
    arena.mkdir()
    rig.d.role_cwd[Role.PLANNER] = str(arena)
    pane = rig.pane(Role.PLANNER)
    rig.server.agents.pop(rig.name(Role.PLANNER))
    rig.server.panes[pane]["cwd"] = rig.server.panes[pane]["foreground_cwd"] = str(rig.repo)

    answer = rig.release(Role.PLANNER)

    assert answer.closed is False and str(arena) in answer.why
    assert rig.server.closes() == [] and pane in rig.server.panes

    rig.server.panes[pane]["cwd"] = rig.server.panes[pane]["foreground_cwd"] = str(arena / "sub")
    assert rig.release(Role.PLANNER).closed is True


# =========================================================================== #
# K3 -- kept-open exceptions: one mechanism, `keep_open` (item 5)
# =========================================================================== #


def _waits_for_an_approval(rig: Rig, role: Role = Role.QA) -> str:
    """The role's agent ends its turn at a permission dialog, with no answer."""
    pane = rig.pane(role)
    rig.server.after_turn_status[rig.name(role)] = "blocked"
    with pytest.raises(WaitingForApproval):
        rig.d.dispatch(role, "p", state=rig.state)
    return pane


def test_a_pane_that_waits_for_an_approval_survives_the_run_end(rig):
    """Catches: the run end closing the pane of a role that waits for an
    approval -- the one window in which the dialog could be answered (K1, K4,
    L16). `dispatch` keeps it itself (the CLI's decision is no longer the only
    protection): nothing is sent, the pane is reported in `unconfirmed`, and
    the ledger says why it was kept."""
    pane = _waits_for_an_approval(rig)
    tab = rig.server.tab_of(pane)

    rig.d.close_own()

    assert rig.server.closes() == []
    assert pane in rig.server.panes and rig.name(Role.QA) in rig.server.agents
    assert rig.d.unconfirmed == [tab]
    kept = rig.events("kept", pane=pane)
    assert kept and kept[0]["why"] == "an approval is waiting in it"
    assert rig.summary()["kept_open"][0]["pane"] == pane


def test_a_pane_that_waits_for_an_approval_is_not_released_and_the_keep_ends_with_the_dialog(rig):
    """Catches: the turn-end release closing a pane that waits for an approval
    (K5), and a keep that never ends (K8): once the operator has answered and a
    later dispatch to the role returns normally, the pane is released like any
    other."""
    pane = _waits_for_an_approval(rig)

    refused = rig.release(Role.QA)

    assert refused.closed is False and "approval is waiting" in refused.why
    assert rig.server.closes() == []

    rig.server.after_turn_status.clear()
    _answers(rig, Role.QA)
    assert rig.d.dispatch(Role.QA, "p again", state=rig.state) == ANSWER
    released = rig.release(Role.QA)
    assert released.closed is True, released
    assert pane not in rig.server.panes


def test_a_pane_that_is_not_proven_free_is_kept_for_inspection(rig, monkeypatch):
    """Catches: a `PaneNotClean` (the dispatcher refused to prompt into a pane
    it could not prove free) leaving the pane to the run end (K2): the pane is
    kept for inspection, the turn end and the run end leave it, and the ledger
    says why."""
    pane = rig.pane(Role.QA)
    monkeypatch.setattr(rig.d, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(
        PaneNotClean("qa dispatch not sent: its input box holds text")))

    with pytest.raises(PaneNotClean):
        rig.d.dispatch(Role.QA, "p", state=rig.state)

    assert rig.events("kept", pane=pane)[0]["why"] == "its state is not proven; kept for inspection"
    assert rig.release(Role.QA).closed is False
    rig.d.close_own()
    assert rig.server.closes() == [] and pane in rig.server.panes


def test_a_dialog_met_by_a_follow_up_keeps_the_pane(rig):
    """Catches: the follow-up path not keeping a pane (K3): a role whose turn
    ended without its answer and that now waits in a dialog is exactly the
    pane the operator has to look at. Control: a follow-up that returns the
    answer lifts nothing and keeps nothing."""
    pane = rig.pane(Role.QA)
    name = rig.name(Role.QA)
    rig.server.agents[name]["agent_status"] = "blocked"
    missing = AnswerMissing(
        "qa ended its turn without the answer", answer_path=rig.d._answer_path(Role.QA, rig.state),
        target=name, prompt_sha256="0" * 64, prompt_bytes=1, prompt_file=None, sent_at=0.0)

    with pytest.raises(WaitingForApproval):
        rig.d.follow_up(Role.QA, rig.state, missing, charge=lambda: None)

    assert rig.events("kept", pane=pane)[0]["why"] == "an approval is waiting in it"
    rig.d.close_own()
    assert rig.server.closes() == [] and pane in rig.server.panes

    control = Rig(rig.root.parent / "control", rig.monkeypatch)
    control.pane(Role.QA)
    control.d._answer_path(Role.QA, control.state).write_text(ANSWER, encoding="utf-8")
    missing = AnswerMissing(
        "m", answer_path=control.d._answer_path(Role.QA, control.state),
        target=control.name(Role.QA), prompt_sha256="0" * 64, prompt_bytes=1,
        prompt_file=None, sent_at=0.0)
    assert control.d.follow_up(Role.QA, control.state, missing, charge=lambda: None) == ANSWER
    assert control.events("kept") == []


def test_keep_open_protects_a_pane_from_every_close(rig):
    """Catches: a keep that only some closes honour (K4, K6, K7): a kept pane
    is refused by the turn-end release, the run end, a direct `_close_own_pane`
    and the dead-agent path, with a `kept` line each time; and `keep_open` for a
    role with no pane does nothing and records nothing."""
    pane = rig.pane(Role.PLANNER)
    rig.d.keep_open(Role.DEVELOPER, "no pane for it")
    assert rig.events("kept") == [], "no pane recorded for the role: nothing kept"

    rig.d.keep_open(Role.PLANNER, "the operator is looking at it")
    assert rig.events("kept", pane=pane)[0]["why"] == "the operator is looking at it"

    assert rig.release(Role.PLANNER).closed is False
    rig.d.close_own()
    assert rig.d._close_own_pane(pane, role=Role.PLANNER, reason="run end") is False
    assert rig.d._close_dead(rig.name(Role.PLANNER), pane, rig.server.tab_of(pane)) is False
    assert rig.server.closes() == [] and pane in rig.server.panes
    assert len(rig.events("kept", pane=pane)) >= 4, "every refusal is on record"


def _hoh_run(rig: Rig, monkeypatch, run_iteration, capsys):
    """`hoh run` driven through `cli.main` with a stand-in Controller whose
    iteration is `run_iteration(state)`; the dispatcher is the rig's real one.
    No Herdr, no agent, no storage root outside the rig. Returns (exit code,
    stderr)."""
    from hoh import integrity

    storage = rig.root / "storage"
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=storage / "ledger"), storage))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot", lambda repo: (_ for _ in ()).throw(
        RuntimeError("not measured in this test")))

    class Standin:
        def __init__(self, *a, **kw):
            pass

        def run_iteration(self, state):
            return run_iteration(state)

        def release_open_seals(self):
            return {}

        def compare_open_seals(self):
            return {}

    monkeypatch.setattr(controller, "Controller", Standin)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: rig.d)
    capsys.readouterr()
    rc = cli.main(["--root", str(rig.root / "runs"), "run", "r1", "--no-herdr",
                   "--iterations", "1"])
    return rc, capsys.readouterr().err


def _outcome(**kw) -> controller.LoopOutcome:
    return controller.LoopOutcome(iteration=1, accepted=False, reason="stand-in", **kw)


def test_the_tabs_stay_open_while_an_approval_waits_on_the_normal_exit(rig, monkeypatch, capsys):
    """Catches (K9): the normal exit of `hoh run` closing the role tabs while a
    role waits for an approval -- the route the blocked dialog takes, because
    `_verify` turns the exception into an outage. Control: the same run
    without the flag closes them."""
    def iteration(state):
        rig.pane(Role.QA)
        return _outcome(waiting_for_approval=True)

    rc, err = _hoh_run(rig, monkeypatch, iteration, capsys)

    assert rc == 0 and "The role tabs stay open" in err
    assert rig.server.closes() == [] and rig.name(Role.QA) in rig.server.agents

    control = Rig(rig.root.parent / "control", monkeypatch)
    rc, err = _hoh_run(control, monkeypatch,
                       lambda state: (control.pane(Role.QA), _outcome())[1], capsys)
    assert control.server.closes() and control.server.agents == {}


@pytest.mark.parametrize("how", [pytest.param("exception", id="exception"),
                                 pytest.param("outcome", id="outcome")])
def test_a_run_that_ends_at_an_approval_closes_nothing(rig, monkeypatch, capsys, how):
    """Catches (K9, K10): either route of a run that ends at an approval --
    the exception path (`WaitingForApproval`) and the normal path (the
    outcome's `waiting_for_approval`) -- closing the tabs. The CLI's own
    decisions are unchanged and now have killing nodes."""
    def iteration(state):
        rig.pane(Role.QA)
        if how == "exception":
            raise WaitingForApproval("qa waits in pane wX:pY")
        return _outcome(waiting_for_approval=True)

    rc, err = _hoh_run(rig, monkeypatch, iteration, capsys)

    assert rc == (1 if how == "exception" else 0)
    assert rig.server.closes() == []
    assert rig.name(Role.QA) in rig.server.agents and "stay open" in err


def test_a_developer_that_cannot_be_ended_keeps_the_tabs_open_by_type(rig, monkeypatch, capsys):
    """Catches (K11): a `RoleNotQuiescent` not being a `PaneNotClean` for the
    CLI, so the exception path closes the tabs of a developer whose pane or
    processes could not be proven gone. By type: the tabs stay open, the
    notice says nothing was sent, and the message names none of the words
    `taxonomy.classify_failure` reads as a provider or approval wait."""
    from hoh import taxonomy

    def iteration(state):
        rig.pane(Role.DEVELOPER)
        raise RoleNotQuiescent("developer: its session could not be ended before "
                               "the candidate was frozen: a process survived")

    rc, err = _hoh_run(rig, monkeypatch, iteration, capsys)

    assert rc == 1 and issubclass(RoleNotQuiescent, PaneNotClean)
    assert rig.server.closes() == []
    assert "so the pane can be inspected; nothing in it was sent" in err
    # Whatever the reason quotes, the first line decides the failure's class.
    dispatcher = types.SimpleNamespace(release_role=lambda role, state: PaneRelease(
        False, "the pane is kept open: waits for an approval; timed out; quota; rate limit"))
    ctrl = Controller.__new__(Controller)
    ctrl.dispatcher = dispatcher
    with pytest.raises(RoleNotQuiescent) as raised:
        ctrl._finish_turn(Role.DEVELOPER, rig.state)
    exc = raised.value
    assert exc.transient is False
    assert not isinstance(exc, WaitingForApproval)
    assert "waits for an approval" in str(exc), "the reason is carried, on its own line"
    assert taxonomy.classify_failure(exc) is taxonomy.FailureClass.UNKNOWN


def test_a_whole_tab_is_not_closed_while_any_pane_in_it_is_kept_open(rig):
    """Catches: a whole-tab close that checks every pane is HoH's own but not
    that none is **kept** (O11; round 1): with two adopted agents in one tab and
    one kept (an approval waiting), releasing the other closed the tab and the
    kept pane with it. Only the released pane closes."""
    rig.restored(Role.PLANNER, pane="w9:p7", tab="w9:t7")
    rig.restored(Role.QA, pane="w9:p8", tab="w9:t7")
    rig.d._ensure_agent(Role.PLANNER, rig.state)
    rig.d._ensure_agent(Role.QA, rig.state)
    rig.d.keep_open(Role.QA, "an approval is waiting in it")

    rig.release(Role.PLANNER)

    assert rig.server.closes() == [["pane", "close", "w9:p7"]]
    assert "w9:p8" in rig.server.panes and rig.name(Role.QA) in rig.server.agents


def test_a_restored_pane_that_is_kept_open_is_not_replaced(rig):
    """Catches: the second close site replacing a restored pane that is kept
    open (C1): `PaneNotClean`, nothing closed, nothing sent, a `kept` line.
    Control: the same pane not kept is replaced (K2's test)."""
    name = _restored_with_residue(rig)
    rig.d._ensure_agent(Role.QA, rig.state)
    rig.d.keep_open(Role.QA, "an approval is waiting in it")

    with pytest.raises(PaneNotClean, match="kept open"):
        rig.d.dispatch(Role.QA, "p", state=rig.state)

    assert rig.server.closes() == [] and name in rig.server.agents
    assert [c for c in rig.server.calls if c[1:3] == ["agent", "prompt"]] == []
    assert len(rig.events("kept", pane="w9:p7")) >= 2, "kept, and refused again at the replacement"


# =========================================================================== #
# K4 -- the turn end, and the developer's quiescence before the freeze (item 4)
# =========================================================================== #


def _plan(state) -> dict:
    """A plan correctly bound to the run."""
    base = state.last_accepted_candidate or state.base_candidate
    return {
        "run_id": state.run_id, "iteration": state.iteration,
        "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
        "spec_digest": state.spec_digest, "objective": "keep add() correct",
        "targets": ["app.py"], "preserve": [],
        "acceptance_checks": [{"check_id": "K1", "description": "the marker exists",
                               "command": "test -f done.txt", "expect_exit": 0,
                               "preserves": False}],
        "out_of_scope": [], "evidence_refs": [], "repair_only": False,
        "repair_reason": None,
    }


def _qa_pass(state) -> dict:
    rid = f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1"
    return {"verdicts": [{"check_id": "K1", "outcome": "PASS", "receipt_id": rid,
                          "reproduction": "pytest", "note": "green"}],
            "open_gaps": [], "summary": "all good"}


def play(rig: Rig, *, develop=None, extras: dict | None = None) -> list[str]:
    """The three roles of one iteration, as hooks of the fake Herdr: each writes
    the file a real role writes. Returns the list that records, in order, the
    role whose turn it is."""
    turns: list[str] = []
    state = rig.state

    extras = extras or {}

    def answering(role: Role, make):
        def hook(prompt: str) -> None:
            turns.append(role.value)
            if role in extras:
                extras[role]()
            rig.d._answer_path(role, state).write_text(json.dumps(make(state)),
                                                        encoding="utf-8")
        return hook

    def developer(prompt: str) -> None:
        turns.append("developer")
        if develop is not None:
            develop()
        if Role.DEVELOPER in extras:
            extras[Role.DEVELOPER]()
        (rig.repo / "done.txt").write_text("ok\n", encoding="utf-8")

    rig.server.hooks[rig.name(Role.PLANNER)] = answering(Role.PLANNER, _plan)
    rig.server.hooks[rig.name(Role.DEVELOPER)] = developer
    rig.server.hooks[rig.name(Role.QA)] = answering(Role.QA, _qa_pass)
    return turns


@pytest.fixture
def git_rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path / "git-rig", monkeypatch, git_repo=True)


def _watch_freeze(rig: Rig, monkeypatch) -> list[list[str]]:
    """The agents Herdr still lists at the instant the candidate is frozen."""
    seen: list[list[str]] = []
    real = controller.snapshot

    def freeze(*args, **kwargs):
        if str(kwargs.get("note", "")).startswith("after developer run"):
            seen.append(rig.server.live_agents())
        return real(*args, **kwargs)

    monkeypatch.setattr(controller, "snapshot", freeze)
    return seen


def test_every_role_pane_is_closed_when_its_turn_is_accepted(git_rig, monkeypatch):
    """Catches: panes living to the run end (L1, L2, L5): in one real iteration
    the planner's pane is gone when its plan is valid, the developer's pane is
    gone **before the freeze** (the freeze sees no developer agent), and QA's
    pane is gone after its verdict -- each with a `closed` ledger line, in that
    order. The operator's pane is untouched."""
    rig = git_rig
    operator = rig.server.operator_pane()
    play(rig)
    frozen_with = _watch_freeze(rig, monkeypatch)

    out = rig.controller.run_iteration(rig.state)

    assert out.accepted, out.reason
    assert frozen_with == [[]], "no role agent is alive when the candidate is frozen"
    assert rig.server.agents == {}
    assert set(rig.server.panes) == {CALLER, operator}
    closed = [r["role"] for r in rig.events("closed") if r.get("role")]
    assert closed == ["planner", "developer", "qa"]
    assert rig.summary()["closed"] == 3 and rig.summary()["unconfirmed"] == []


def test_a_developer_that_wakes_after_its_turn_cannot_touch_the_frozen_candidate(
        git_rig, monkeypatch):
    """Catches the qaw incident (L1): the developer arms a background edit and
    ends its turn; QA's start lets time pass; the developer is re-woken **while
    QA runs** and edits the tree. With the pane closed before the freeze the
    session is gone, the wake never runs, the late file never appears, and the
    iteration is accepted. Control: without the turn-end release the late file
    appears and the binding fails (the defect)."""
    rig = git_rig
    late = rig.repo / "late.txt"
    dev = rig.name(Role.DEVELOPER)
    play(rig, develop=lambda: rig.server.wakes.__setitem__(
        dev, lambda: late.write_text("woken\n", encoding="utf-8")))

    out = rig.controller.run_iteration(rig.state)

    assert out.accepted, out.reason
    assert not late.exists()

    control = Rig(rig.root.parent / "control", monkeypatch, git_repo=True)
    clate = control.repo / "late.txt"
    cdev = control.name(Role.DEVELOPER)
    play(control, develop=lambda: control.server.wakes.__setitem__(
        cdev, lambda: clate.write_text("woken\n", encoding="utf-8")))
    monkeypatch.setattr(control.d, "release_role",
                        lambda role, state, **kw: PaneRelease(True, ""))
    out = control.controller.run_iteration(control.state)
    assert clate.exists(), "without the release the sleeping developer wakes"
    assert not out.accepted


def test_a_developer_that_cannot_be_ended_stops_the_run_before_the_freeze(git_rig, monkeypatch):
    """Catches: a developer that is still `working` after its turn (F8, L3)
    being frozen anyway: `RoleNotQuiescent` is raised inside `_develop`'s `try`,
    so the dispatch is booked failed **before** the freeze; the candidate is not
    frozen, the developer's pane stays for the operator, and the ledger says
    why."""
    rig = git_rig
    play(rig)
    rig.server.after_turn_status[rig.name(Role.DEVELOPER)] = "working"
    frozen_with = _watch_freeze(rig, monkeypatch)

    with pytest.raises(RoleNotQuiescent, match="could not be ended"):
        rig.controller.run_iteration(rig.state)

    assert frozen_with == [], "the candidate was not frozen"
    assert rig.state.working_candidate is None
    assert rig.name(Role.DEVELOPER) in rig.server.agents
    assert "working again" in rig.events("refused", role="developer")[0]["why"]


def test_a_planner_that_cannot_be_released_does_not_stop_the_run(git_rig):
    """Catches: a refusal at the planner's turn end stopping the run (L4): only
    the developer's refusal does -- for the planner and QA it is recorded.
    Control: the iteration is accepted, the planner's pane stays until the run
    end, the other two are closed."""
    rig = git_rig
    play(rig)
    rig.server.after_turn_status[rig.name(Role.PLANNER)] = "working"

    out = rig.controller.run_iteration(rig.state)

    assert out.accepted, out.reason
    assert rig.events("refused", role="planner")
    assert list(rig.server.agents) == [rig.name(Role.PLANNER)]
    rig.server.after_turn_status.clear()
    rig.d.close_own()
    assert rig.server.agents == {} and rig.d.unconfirmed == []


def test_a_schema_repair_goes_to_the_same_pane_and_the_pane_closes_after_it(git_rig):
    """Catches: the pane closed before the answer is valid (L7), so the repair
    needs a fresh agent, or never (L6): the first planner answer breaks the
    schema, the repair prompt goes to the **same** pane, and the pane is closed
    once, after the validated repair."""
    rig = git_rig
    answers = iter(["this is not json", json.dumps(dict(_plan(rig.state), iteration=1))])
    planner = rig.name(Role.PLANNER)
    rig.server.hooks[planner] = lambda prompt: rig.d._answer_path(
        Role.PLANNER, rig.state).write_text(next(answers), encoding="utf-8")

    raw = rig.controller._dispatch_with_repair(Role.PLANNER, "plan it", rig.state)

    assert json.loads(raw)["objective"] == "keep add() correct"
    kinds = rig.server.kinds()
    assert kinds.count(["agent", "start"]) == 1 and kinds.count(["tab", "create"]) == 1
    assert kinds.count(["agent", "prompt"]) == 2
    assert kinds.index(["tab", "close"]) > max(i for i, k in enumerate(kinds)
                                               if k == ["agent", "prompt"])
    assert len(rig.events("closed", role="planner")) == 1
    assert rig.server.agents == {}


def test_a_failed_turn_keeps_its_pane_until_the_run_end(git_rig):
    """Catches: a failed turn's pane being closed by the turn-end release (L12):
    the developer's dispatch fails, the exception propagates, its pane stays
    for the operator -- and the run end closes it. The planner, whose turn
    ended, is already gone."""
    rig = git_rig
    play(rig)
    rig.server.fail_prompt[rig.name(Role.DEVELOPER)] = "agent_not_found"

    with pytest.raises(DispatchError):
        rig.controller.run_iteration(rig.state)

    assert rig.name(Role.PLANNER) not in rig.server.agents
    assert rig.name(Role.DEVELOPER) in rig.server.agents
    rig.d.close_own()
    assert rig.server.agents == {}
    assert rig.events("closed", role="developer")[0]["reason"] == "run end"


def test_a_released_role_gets_a_fresh_pane_for_its_next_turn(rig):
    """Catches: a released role keeping its cached agent (L8), so its next turn
    prompts an agent that is gone: the next dispatch starts a fresh agent in a
    pane of its own (D1: a pane lives for one role turn)."""
    _answers(rig, Role.PLANNER)
    rig.d.dispatch(Role.PLANNER, "first", state=rig.state)
    first = rig.d._role_panes()[Role.PLANNER]
    assert rig.release(Role.PLANNER).closed is True
    assert Role.PLANNER not in rig.d.agents and first not in rig.server.panes

    rig.d.dispatch(Role.PLANNER, "second", state=rig.state)

    second = rig.d._role_panes()[Role.PLANNER]
    assert second != first and second in rig.server.panes
    assert rig.server.kinds().count(["agent", "start"]) == 2


def test_a_pane_left_at_an_input_prompt_after_its_answer_is_still_released(rig):
    """Catches: a role that answered and then sat at its input prompt (Herdr
    reports `blocked`, measured on a real run) being kept (F10): its answer was
    delivered, the dialog is one that appeared after it, and the pane is closed.
    A dialog **during** the turn never reaches release (K3's tests)."""
    _answers(rig, Role.PLANNER)
    rig.server.after_turn_status[rig.name(Role.PLANNER)] = "blocked"
    assert rig.d.dispatch(Role.PLANNER, "p", state=rig.state) == ANSWER
    pane = rig.d._role_panes()[Role.PLANNER]

    answer = rig.release(Role.PLANNER)

    assert answer.closed is True, answer
    assert pane not in rig.server.panes


# =========================================================================== #
# K5 -- the run end, whatever the outcome (item 6)
# =========================================================================== #


@pytest.mark.parametrize("how", [
    pytest.param("accepted", id="accepted"),
    pytest.param("not-accepted", id="not-accepted"),
    pytest.param("cancelled", id="cancelled"),
    pytest.param("failed", id="failed"),
])
def test_the_run_end_closes_every_own_pane_and_nothing_else(rig, monkeypatch, capsys, how):
    """Catches: a run end that leaves HoH's panes standing, or closes more than
    its own (L14, L15): through `cli.main`, for every way an iteration ends,
    every pane HoH opened is closed -- each role's, one of them with an
    operator's pane next to it (only the role pane goes) -- and the operator's
    pane in its own tab, and the pane `hoh run` sits in, survive."""
    apart = rig.server.operator_pane()
    beside = []

    def iteration(state):
        planner = rig.pane(Role.PLANNER)
        beside.append(rig.server.operator_pane(tab=rig.server.tab_of(planner)))
        rig.pane(Role.DEVELOPER)
        rig.pane(Role.QA)
        if how == "failed":
            raise DispatchError("the provider is down")
        if how == "cancelled":
            state.condition = Condition.CANCELLED
        return controller.LoopOutcome(iteration=1, accepted=how == "accepted",
                                      reason="stand-in")

    rc, err = _hoh_run(rig, monkeypatch, iteration, capsys)

    assert rc == (1 if how == "failed" else 0), err
    assert rig.server.agents == {}, "every role pane is closed"
    assert set(rig.server.panes) == {CALLER, apart, *beside}
    assert rig.d.own_panes == [] and rig.d.unconfirmed == []
    assert len(rig.events("closed", reason="run end")) == 3


# =========================================================================== #
# K6 -- the record and `hoh status` (item 7)
# =========================================================================== #


def _status(rig_root: Path, capsys) -> dict:
    capsys.readouterr()
    assert cli.main(["--root", str(rig_root / "runs"), "status", "r1"]) == 0
    return json.loads(capsys.readouterr().out)


def test_hoh_status_shows_what_was_closed_kept_and_refused(rig, capsys):
    """Catches: a close, keep or refusal that leaves no trace in the run (the
    defect, R1, R2, R3, R8, R9, Q21): `hoh status` carries a `panes` summary --
    what was closed, the processes HoH ended, the pane kept for an approval and
    its reason, the pane whose release was refused and why. A later `closed`
    for a pane removes it from the open lists."""
    planner = rig.pane(Role.PLANNER)
    leftover = rig.fprocs.background(planner, "attached-in-the-session")
    assert rig.release(Role.PLANNER).closed is True
    qa = _waits_for_an_approval(rig, Role.QA)
    developer = rig.pane(Role.DEVELOPER)
    rig.server.agents[rig.name(Role.DEVELOPER)]["agent_status"] = "working"
    assert rig.release(Role.DEVELOPER).closed is False

    panes = _status(rig.root, capsys)["panes"]

    assert panes["closed"] == 1 and panes["processes_ended"] == 1
    assert panes["unconfirmed"] == [] and panes["skipped_lines"] == 0
    assert [e["pane"] for e in panes["kept_open"]] == [qa]
    assert panes["kept_open"][0]["why"] == "an approval is waiting in it"
    assert panes["kept_open"][0]["role"] == "qa" and panes["kept_open"][0]["at"]
    assert [e["pane"] for e in panes["refused"]] == [developer]
    assert "working again" in panes["refused"][0]["why"]
    assert leftover.pid in [pid for r in rig.events("ended") for pid in r["pids"]]

    rig.server.agents[rig.name(Role.DEVELOPER)]["agent_status"] = "idle"
    assert rig.release(Role.DEVELOPER).closed is True
    assert _status(rig.root, capsys)["panes"]["refused"] == [], "a later close removes it"


def test_hoh_status_survives_a_missing_torn_or_unreadable_ledger(rig, capsys, tmp_path):
    """Catches: a status that raises, or lies, on the ledger it cannot read (R4,
    R5, R6): a missing ledger is zero and empty; a line that does not parse or has
    an unknown event is counted in `skipped_lines`, never raised; an unreadable
    ledger says so instead of looking empty. A write that fails is `False`, and an
    unknown event a `ValueError`."""
    empty = {"closed": 0, "processes_ended": 0, "kept_open": [], "refused": [],
             "unconfirmed": [], "skipped_lines": 0}
    assert _status(rig.root, capsys)["panes"] == empty

    ledger = rig.store.dir / pane_ledger.LEDGER
    ledger.write_text("\n".join([
        json.dumps({"event": "closed", "pane": "p1", "at": "t"}),
        json.dumps({"event": "ended", "count": 2, "pids": [7, 8]}),
        json.dumps({"event": "ended", "count": 3, "pids": [9, 10, 11]}),
        json.dumps({"event": "ended", "count": "many"}),
        '{"event": "clos',
        json.dumps({"event": "exploded", "pane": "p9"}),
        "[1, 2]",
        json.dumps({"event": "refused", "pane": "p2", "why": "w", "role": "qa", "at": "t"}),
    ]) + "\n", encoding="utf-8")
    panes = _status(rig.root, capsys)["panes"]
    assert panes["closed"] == 1 and panes["processes_ended"] == 5
    assert panes["skipped_lines"] == 3
    assert panes["refused"] == [{"role": "qa", "pane": "p2", "why": "w", "at": "t"}]

    broken = Rig(tmp_path / "broken", rig.monkeypatch)
    (broken.store.dir / pane_ledger.LEDGER).mkdir()
    assert "unreadable" in _status(broken.root, capsys)["panes"]
    assert "closed" not in pane_ledger.summary(broken.store.dir)

    with pytest.raises(ValueError):
        pane_ledger.append(rig.store.dir, "vanished", pane="p")
    assert pane_ledger.append(tmp_path / "no-such-dir", "closed", pane="p") is False
    assert pane_ledger.append(rig.store.dir, "closed", pane="p3") is True
    assert pane_ledger.EVENTS == {"closing", "closed", "kept", "refused", "ended"}


def test_a_close_that_was_asked_for_and_never_confirmed_is_listed_as_unconfirmed(rig, capsys):
    """Catches: a close that was asked for and never confirmed looking like
    nothing happened (R7, R3): a `closing` line with no later `closed` for the
    pane is listed as `unconfirmed`; a confirmed one is not; a tab (a line
    without a pane) is listed by its tab. Also through the dispatcher: a close
    the snapshot cannot confirm is listed until a later run end closes it."""
    pane_ledger.append(rig.store.dir, "closing", pane="p1", role="qa", reason="run end")
    pane_ledger.append(rig.store.dir, "closing", pane="p2", reason="run end")
    pane_ledger.append(rig.store.dir, "closed", pane="p2", reason="run end")
    pane_ledger.append(rig.store.dir, "closing", tab="t1", reason="run end")
    summary = pane_ledger.summary(rig.store.dir)
    assert [e.get("pane") or e.get("tab") for e in summary["unconfirmed"]] == ["p1", "t1"]
    assert summary["unconfirmed"][0]["role"] == "qa" and summary["closed"] == 1

    other = Rig(rig.root.parent / "other", rig.monkeypatch)
    pane = other.pane(Role.PLANNER)
    other.server.fail_snapshot = False
    real = other.server.snapshot
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 3:           # the snapshot that should confirm the close
            raise herdr.HerdrError("the server did not answer")
        return real()

    rig.monkeypatch.setattr(herdr, "snapshot", flaky)
    other.d.close_own()
    assert other.server.closes(), "the close was asked for"
    assert [r["event"] for r in other.ledger() if r.get("pane") == pane][:2] == ["closing", "refused"]
    # The refusal replaced the open `closing` line; with only the `closing` line
    # (a crash between the command and its confirmation) it is unconfirmed:
    lines = [r for r in other.ledger() if r["event"] != "refused"]
    (other.store.dir / pane_ledger.LEDGER).write_text(
        "\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    assert [e["pane"] for e in pane_ledger.summary(other.store.dir)["unconfirmed"]] == [pane]


# =========================================================================== #
# K7 -- the prompts (item 8)
# =========================================================================== #

PREVENTION = ("Do not end your turn while work you started in the background is still "
              "running; wait for it.")


def _dev_plan():
    from hoh.contracts import DevelopmentPlan

    return DevelopmentPlan.model_validate({
        "run_id": "r", "iteration": 1, "base_candidate_id": "b", "spec_digest": "s",
        "objective": "o", "targets": ["t"],
        "acceptance_checks": [{"check_id": "K1", "description": "d", "command": "true",
                               "expect_exit": 0}]})


def test_every_role_prompt_says_long_commands_run_in_the_foreground():
    """Catches: a role that is not told its session ends with its turn (P1, P2,
    P3), a developer that is not told the end of its turn freezes the candidate
    (P4), and the new sentence placed before the existing background sentence
    (P5) or the developer prompt carrying the background sentence it must not.
    Planner and QA: the line right after the existing sentence; developer: the
    first line of `/development-policy`."""
    from hoh.evidence import EvidenceBundle

    qa = roles.qa_prompt(iteration=1, spec_text="spec", plan=_dev_plan(), candidate_path="c",
                         candidate_binding="b", receipts_summary="r")
    planner = roles.planner_prompt(iteration=1, spec_text="spec",
                                   evidence=EvidenceBundle(run_id="r"), repo_path="/x",
                                   base_candidate_id="b", spec_digest="s", run_id="r")
    developer = roles.developer_prompt(iteration=1, attempt=0, spec_text="spec",
                                       plan=_dev_plan(), workspace="/w", warm_start=False,
                                       evidence=EvidenceBundle(run_id="r"))

    assert roles.FOREGROUND == (
        "Run long commands in the foreground and wait for them to finish. Start no "
        "background task, monitor or timer: your session ends when your turn ends, "
        "and whatever is still running then is cut off.")
    assert roles.DEVELOPER_FREEZE == (
        "The end of your turn freezes the candidate; nothing you do after it counts.")
    assert "/planning-policy\n" + PREVENTION + "\n" + roles.FOREGROUND + "\n" in planner
    assert "/assessment-policy\n" + PREVENTION + "\n" + roles.FOREGROUND + "\n" in qa
    assert ("/development-policy\n" + roles.FOREGROUND + " " + roles.DEVELOPER_FREEZE
            + "\nRepair build and runtime blockers first") in developer
    assert PREVENTION not in developer, "the developer prompt still does not carry it"
    assert roles.DEVELOPER_FREEZE not in planner and roles.DEVELOPER_FREEZE not in qa
    for prompt in (planner, qa, developer):
        assert prompt.count(roles.FOREGROUND) == 1


# =========================================================================== #
# K8 -- the role's processes: found, ended, proven gone (item 4b)
# =========================================================================== #
#
# Closing a pane ends the agent and its shell, not the commands the agent
# started in the background (live smoke, 2026-10-06). These tests run on the
# fake process table; the last section runs the real `/proc` adapter.

SIGTERM, SIGKILL = int(signal.SIGTERM), int(signal.SIGKILL)


def _dev(rig: Rig, role: Role = Role.DEVELOPER) -> tuple[str, str]:
    """The role's pane, and the mark its processes carry."""
    pane = rig.pane(role)
    return pane, rig.d._pane_marks()[pane]


def _ended(rig: Rig) -> list[int]:
    return sorted(pid for r in rig.events("ended") for pid in r["pids"])


@pytest.mark.parametrize("shape", [
    pytest.param("attached-in-the-session", id="attached-in-the-session"),
    pytest.param("marked-attached-in-a-session-of-its-own",
                 id="marked-attached-in-a-session-of-its-own"),
    pytest.param("scrubbed-but-attached", id="scrubbed-but-attached"),
    pytest.param("scrubbed-orphan-in-the-session", id="scrubbed-orphan-in-the-session"),
    pytest.param("non-dumpable-orphan-in-the-session",
                 id="non-dumpable-orphan-in-the-session"),
])
def test_a_background_command_that_outlives_the_pane_is_ended_before_the_freeze(rig, shape):
    """Catches the live smoke (Q1, Q2, Q3, Q4, Q15, Q17): a command the role
    left running survives the pane close, orphaned to a subreaper -- and a
    surviving writer could still change the tree after the freeze. It is found
    **by structure, not by mark**, in five shapes (a member that scrubbed its
    environment, or whose environment cannot be read, is still a member), ended
    only after the pane is closed, with SIGTERM, and recorded."""
    pane, mark = _dev(rig)
    cmd = rig.fprocs.background(pane, shape)
    assert cmd.pid in rig.fprocs.procs, "the command is running when the turn ends"

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert cmd.pid not in rig.fprocs.procs
    kinds = [e[0] for e in rig.fprocs.events]
    assert kinds.index("close") < kinds.index("signal"), "ended after the pane is closed"
    assert rig.fprocs.signals == [(cmd.pid, SIGTERM)]
    line = rig.events("ended")[0]
    assert line["pids"] == [cmd.pid] and line["count"] == 1
    assert line["role"] == "developer" and line["pane"] == pane
    assert rig.summary()["processes_ended"] == 1


def test_a_command_that_ignores_sigterm_is_killed(rig):
    """Catches (Q5, S16): SIGKILL not following a SIGTERM that was ignored: the
    command is told SIGTERM first, given the grace period, and only then
    SIGKILLed; the release is clean."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs.stubborn.add(cmd.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert [s for p, s in rig.fprocs.signals if p == cmd.pid] == [SIGTERM, SIGKILL]
    term, kill = [e for e in rig.fprocs.events if e[0] == "signal"]
    assert kill[3] - term[3] >= procs.TERM_WAIT_S - 1e-9, "a real grace period"
    assert cmd.pid not in rig.fprocs.procs


def test_a_command_that_needs_a_moment_after_sigterm_is_not_killed(rig):
    """Catches (S14, xS13): a command that exits a few moments after SIGTERM
    being SIGKILLed anyway, because the wait did not look again. SIGTERM only."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs.dies_after[cmd.pid] = 3

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signals == [(cmd.pid, SIGTERM)]
    assert cmd.pid not in rig.fprocs.procs


def test_a_process_that_starts_another_while_it_dies_is_followed(rig):
    """Catches (Q16, Q22): a dying command that starts another (a restart loop
    in a shell) being ended once and its successor left running: the next round
    finds the one born while the first died -- marked, in the root's session --
    and ends it too."""
    pane, mark = _dev(rig)
    info = rig.fprocs.pane_procs[pane]
    first = rig.fprocs.background(pane)
    born: list[procs.Proc] = []
    rig.fprocs.on_death[first.pid] = lambda fp: born.append(
        fp.add(ppid=fp.reaper, sid=info["session"], comm="sleep", mark=mark))

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert born and born[0].pid not in rig.fprocs.procs
    assert _ended(rig) == sorted([first.pid, born[0].pid])


def test_a_process_born_in_the_last_round_is_a_survivor_not_invisible(rig):
    """Catches (Q25): the survivors being computed from the processes seen in
    earlier rounds, so a process born during the **last** round is neither
    signalled nor named, and the developer is frozen with it running. A chain
    longer than the rounds: the final look takes a fresh snapshot, runs it
    through the same member computation, and names the last one."""
    pane, mark = _dev(rig)
    session = rig.fprocs.pane_procs[pane]["session"]
    chain: list[procs.Proc] = [rig.fprocs.background(pane)]

    def successor(fp):
        nxt = fp.add(ppid=fp.reaper, sid=session, comm="sleep", mark=mark)
        chain.append(nxt)
        if len(chain) < procs.ROUNDS + 3:
            fp.on_death[nxt.pid] = successor

    rig.fprocs.on_death[chain[0].pid] = successor

    answer = rig.release(Role.DEVELOPER)

    last_alive = [p for p in chain if p.pid in rig.fprocs.procs]
    assert len(last_alive) == 1, "exactly the one born in the last round is alive"
    assert answer.closed is False and str(last_alive[0].pid) in answer.why
    assert rig.events("refused", role="developer")[-1]["pids"] == [last_alive[0].pid]
    assert last_alive[0].pid not in rig.fprocs.signalled(), "born too late to be signalled"


def test_a_process_that_cannot_be_killed_is_a_refusal_and_names_its_pid(rig):
    """Catches (Q6): a process that survives SIGKILL being booked gone. The
    pane **is** closed, the release is refused with the pid, the ledger says
    `pids`, and `ended` does not claim what HoH could not end."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs.immortal.add(cmd.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and str(cmd.pid) in answer.why
    assert pane not in rig.server.panes, "the pane is closed"
    refused = rig.events("refused", pane=pane)[-1]
    assert refused["pids"] == [cmd.pid] and refused["why"] == answer.why
    assert rig.events("ended") == []
    assert cmd.pid in rig.fprocs.procs


def _develop_with(rig: Rig, make):
    """The developer's turn starts a command in its pane, as the qaw mutant
    runner did."""
    def start():
        rig.started = make(rig.fprocs, rig.d._role_panes()[Role.DEVELOPER])
    return start


def test_a_developer_with_a_surviving_process_is_not_frozen(git_rig, monkeypatch):
    """Catches (Q6, Q19): the qaw incident's second half -- a developer whose
    background command survives is frozen anyway, and the writer changes the
    tree after the freeze. Through the controller: `RoleNotQuiescent` before the
    freeze, the candidate not frozen, the command named."""
    rig = git_rig
    play(rig, extras={Role.DEVELOPER: _develop_with(rig, lambda fp, pane: (
        lambda cmd: (fp.immortal.add(cmd.pid), cmd)[1])(fp.background(pane)))})
    frozen_with = _watch_freeze(rig, monkeypatch)

    with pytest.raises(RoleNotQuiescent, match="still alive"):
        rig.controller.run_iteration(rig.state)

    assert frozen_with == [] and rig.state.working_candidate is None
    assert str(rig.started.pid) in rig.events("refused", role="developer")[-1]["why"]


def test_a_developer_whose_background_command_is_ended_is_frozen_and_accepted(git_rig):
    """Catches (Q1): the fix refusing too much -- the same iteration with a
    command that **can** be ended is frozen and accepted, the command is gone
    and recorded. (The qaw incident without and with the fix.)"""
    rig = git_rig
    play(rig, extras={Role.DEVELOPER: _develop_with(
        rig, lambda fp, pane: fp.background(pane))})

    out = rig.controller.run_iteration(rig.state)

    assert out.accepted, out.reason
    assert rig.started.pid not in rig.fprocs.procs
    assert rig.started.pid in _ended(rig)


def test_nothing_but_the_roles_own_processes_is_signalled(rig):
    """Catches (Q8, Q8b, Q9, Q39): a signal to anything but the role's own
    processes. In the table: an operator's shell, another pane's command, this
    very process and what it started (carrying the role's mark), a stranger
    with no readable mark outside the session, and pid 1 (carrying the mark and
    sitting in the session): only the role's command is signalled."""
    pane, mark = _dev(rig)
    session = rig.fprocs.pane_procs[pane]["session"]
    cmd = rig.fprocs.background(pane)
    other_pane = rig.pane(Role.PLANNER)
    other = rig.fprocs.background(other_pane)
    operator = rig.server.operator_pane()
    operator_shell = rig.fprocs.pane_procs[operator]["shell"]
    stranger = rig.fprocs.add(ppid=rig.fprocs.reaper, comm="stranger")
    me = os.getpid()
    mine = rig.fprocs.add(ppid=me, sid=session, comm="my-child", mark=mark)
    rig.fprocs.replace(me, mark=mark)
    rig.fprocs.replace(1, mark=mark, sid=session)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signalled() == {cmd.pid}
    for survivor in (other.pid, operator_shell, stranger.pid, mine.pid, me, 1):
        assert survivor in rig.fprocs.procs, survivor


def test_a_reused_pid_is_not_signalled(rig):
    """Catches (Q10): a pid whose start time changed -- the command exited
    between the scan and the signal and an unrelated process took its pid --
    being signalled, or booked a survivor. Only the process that had that pid
    **and the start time that was seen** is ever signalled."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    strangers: list[procs.Proc] = []

    def reused(fp):
        fp.procs.pop(cmd.pid)
        strangers.append(fp.add(cmd.pid, ppid=fp.reaper, comm="stranger"))

    rig.fprocs.on_close = reused

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert strangers and strangers[0].pid in rig.fprocs.procs
    assert rig.fprocs.signals == [] and rig.events("ended") == []


def test_a_process_that_ends_by_itself_before_the_signal_is_not_recorded_as_ended_by_hoh(rig):
    """Catches (Q7): `ended` claiming a command that exited on its own: it is
    gone, HoH did not end it and does not say it did. No signal, no `ended`."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs.on_close = lambda fp: fp._kill(cmd.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signals == [] and rig.events("ended") == []
    assert rig.summary()["processes_ended"] == 0


def test_a_mark_this_process_did_not_mint_is_refused():
    """Catches (Q18, Q23, Q24, S15, S21): a fixed or guessable mark being
    acted on, so two concurrent runs (or test runs) signal each other's
    processes. `scan`, `end` and `marked_left` raise `ValueError` for a mark
    `mint` did not return in this process, and a minted one is random: 96 bits,
    different every time, not derivable from run and role."""
    table = FakeProcs()
    forged = "test-run:developer:aaaa"
    table.pane_started("w1:p9", forged)
    found = procs.scan(table.snapshot(), procs.mint("x", "y"))
    assert found.root is None
    with pytest.raises(ValueError):
        procs.scan(table.snapshot(), forged)
    with pytest.raises(ValueError):
        procs.end(table, forged, found, sleep=table.sleep, clock=table.clock)
    with pytest.raises(ValueError):
        procs.marked_left(table.snapshot(), forged)
    with pytest.raises(ValueError):
        procs.marked_left(table.snapshot(), "")
    assert table.signals == []

    first, second = procs.mint("run", "developer"), procs.mint("run", "developer")
    label, role, nonce = first.rsplit(":", 2)
    assert (label, role) == ("run", "developer") and first != second
    assert re.fullmatch(r"[0-9a-f]{24}", nonce), "96 random bits, not a guessable name"
    procs.scan(table.snapshot(), first)        # a minted mark is accepted


def test_a_zombie_is_gone_and_is_not_signalled(rig):
    """Catches (Q40): a zombie (a process that has exited and waits to be reaped)
    being signalled, or booked a survivor so the developer is never frozen. One
    hangs below the pane's shell, one below a live member (the descent reaches
    it only through that member). Both are gone: not signalled, not survivors;
    the live command is ended."""
    pane, mark = _dev(rig)
    info = rig.fprocs.pane_procs[pane]
    cmd = rig.fprocs.background(pane)
    below_shell = rig.fprocs.add(ppid=info["shell"], sid=info["session"], mark=mark,
                                 state="Z", comm="defunct")
    below_member = rig.fprocs.add(ppid=cmd.pid, sid=info["session"], mark=mark,
                                  state="Z", comm="defunct")

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signalled() == {cmd.pid}
    assert below_shell.pid in rig.fprocs.procs and below_member.pid in rig.fprocs.procs
    assert procs.marked_left(rig.fprocs.snapshot(), mark) == []


@pytest.mark.parametrize("parent", [
    pytest.param("orphaned-to-a-subreaper", id="orphaned-to-a-subreaper"),
    pytest.param("orphaned-to-init", id="orphaned-to-init"),
    pytest.param("below-a-live-unmarked-process", id="below-a-live-unmarked-process"),
])
def test_a_marked_daemon_outside_the_roles_pane_is_reported_and_never_signalled(
        tmp_path, monkeypatch, parent):
    """Catches (Q32, Q33, Q35, Q36, Q37): a shared per-user daemon -- a server
    lavish-axi or chrome-devtools-axi started first -- inherits the mark,
    detaches, and belongs to the operator's other work as much as to the role.
    A mark alone does not qualify a process: this one is a detached marked
    session leader (orphaned to a subreaper, to init, or below a live unmarked
    process). It is named, recorded (`escaped`), **never signalled**, and the
    release is refused -- while the role's own command is still ended."""
    rig = Rig(tmp_path / parent, monkeypatch, reaper=1 if parent == "orphaned-to-init" else 1540)
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    if parent == "below-a-live-unmarked-process":
        operator = rig.server.operator_pane()
        where = rig.fprocs.pane_procs[operator]["shell"]
    else:
        where = {"orphaned-to-a-subreaper": "subreaper", "orphaned-to-init": "init"}[parent]
    daemon = rig.fprocs.daemon(pane, parent=where)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False
    assert str(daemon.pid) in answer.why and "never signalled" in answer.why
    assert daemon.pid in rig.fprocs.procs and daemon.pid not in rig.fprocs.signalled()
    assert cmd.pid not in rig.fprocs.procs, "the role's own command is still ended"
    assert rig.events("refused", pane=pane)[-1]["escaped"] == [daemon.pid]
    assert pane not in rig.server.panes


def test_a_developer_with_an_escaped_marked_process_is_not_frozen(git_rig, monkeypatch):
    """Catches (Q33, Q36): an escaped marked process not failing the release
    closed. Through the controller: the developer's step stops before the
    freeze, the daemon is named and alive, never signalled."""
    rig = git_rig
    play(rig, extras={Role.DEVELOPER: _develop_with(
        rig, lambda fp, pane: fp.daemon(pane))})
    frozen_with = _watch_freeze(rig, monkeypatch)

    with pytest.raises(RoleNotQuiescent, match="never signalled"):
        rig.controller.run_iteration(rig.state)

    assert frozen_with == []
    assert rig.started.pid in rig.fprocs.procs
    assert rig.started.pid not in rig.fprocs.signalled()


def _break_the_root(rig: Rig, pane: str, fault: str) -> None:
    fp = rig.fprocs
    info = fp.pane_procs[pane]
    mark = info["mark"]
    if fault == "not-a-session-leader":
        fp.replace(info["shell"], sid=fp.SERVER)
        fp.replace(info["agent"], sid=fp.SERVER)
    elif fault == "not-a-session-leader-with-a-detached-daemon":
        fp.replace(info["shell"], sid=fp.SERVER)
        fp.replace(info["agent"], sid=fp.SERVER)
        fp.daemon(pane, parent="subreaper")
    elif fault == "a-second-root":
        fp.add(ppid=fp.SERVER, comm="bash", mark=mark)
    elif fault == "the-parent-is-not-named-like-a-herdr-server":
        fp.replace(fp.SERVER, comm="tmux")
    elif fault in ("the-parent-is-named-herdr2", "the-parent-is-named-herdrd",
                   "the-parent-is-named-xherdr"):
        fp.replace(fp.SERVER, comm=fault.rsplit("named-", 1)[1])
    elif fault == "the-parent-leads-no-session":
        fp.replace(fp.SERVER, sid=fp.reaper)
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize("fault", [
    pytest.param("not-a-session-leader", id="not-a-session-leader"),
    pytest.param("not-a-session-leader-with-a-detached-daemon",
                 id="not-a-session-leader-with-a-detached-daemon"),
    pytest.param("a-second-root", id="a-second-root"),
    pytest.param("the-parent-is-not-named-like-a-herdr-server",
                 id="the-parent-is-not-named-like-a-herdr-server"),
    pytest.param("the-parent-is-named-herdr2", id="the-parent-is-named-herdr2"),
    pytest.param("the-parent-is-named-herdrd", id="the-parent-is-named-herdrd"),
    pytest.param("the-parent-is-named-xherdr", id="the-parent-is-named-xherdr"),
    pytest.param("the-parent-leads-no-session", id="the-parent-leads-no-session"),
])
def test_a_pane_root_that_cannot_be_identified_fails_closed(rig, fault):
    """Catches (Q13, Q29, Q30, Q32, S2, S3, S4, S18, S19): the "one pane, one
    session" assumption not being asserted at run time. The root is the pane's
    own shell: a marked session leader directly below a Herdr server (named
    **exactly** `herdr`, itself a session leader), and exactly one. Faults: the
    shell is no session leader (alone, and next to a detached daemon -- which
    must never become the root); two candidates; the parent is not named like a
    server (also `herdr2`, `herdrd`, `xherdr`: an exact name, not `in` or
    `startswith`); the parent leads no session. Nothing is signalled, the pane
    is closed, the release is refused saying the processes cannot be told."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    _break_the_root(rig, pane, fault)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and "cannot be told" in answer.why
    assert rig.fprocs.signals == [], "nothing is signalled on a guess"
    assert cmd.pid in rig.fprocs.procs
    assert pane not in rig.server.panes
    assert rig.events("refused", pane=pane)[-1]["why"] == answer.why


def test_a_herdr_server_the_agent_started_inside_the_pane_is_no_second_root(rig):
    """Catches (Q31): a server the agent started inside the pane -- it inherits
    the mark, leads a session, and has a shell of its own below it -- making a
    second root, so the real pane's processes cannot be told and nothing is
    ended. A root's parent must be an **unmarked** server: the nested shell is a
    member, and everything below it is ended."""
    pane, mark = _dev(rig)
    info = rig.fprocs.pane_procs[pane]
    inner = rig.fprocs.add(ppid=info["shell"], comm="herdr", mark=mark)
    inner_shell = rig.fprocs.add(ppid=inner.pid, comm="bash", mark=mark)
    cmd = rig.fprocs.add(ppid=inner_shell.pid, sid=inner_shell.sid, comm="sleep", mark=mark)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert all(p not in rig.fprocs.procs for p in (inner.pid, inner_shell.pid, cmd.pid))


def test_a_member_with_an_unreadable_environment_that_cannot_be_ended_is_a_survivor(rig):
    """Catches (Q3 and fail closed): a non-dumpable orphan in the pane's session
    (its environment cannot be read) being left alone because nothing says it is
    the role's. Membership is structural: it is signalled, and when it cannot be
    ended it is a survivor -- the developer is not frozen."""
    pane, _ = _dev(rig)
    ghost = rig.fprocs.background(pane, "non-dumpable-orphan-in-the-session")
    rig.fprocs.immortal.add(ghost.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and str(ghost.pid) in answer.why
    assert (ghost.pid, SIGKILL) in rig.fprocs.signals
    assert rig.events("refused", pane=pane)[-1]["pids"] == [ghost.pid]


def test_a_pid_in_the_skip_set_is_never_signalled_even_if_it_was_found():
    """Catches (Q38): `end` signalling a pid that it was told to leave alone
    because it was already among the members it was handed. The skip set wins:
    this process and what it started are never touched, whatever the scan said."""
    table = FakeProcs()
    mark = procs.mint("t", "developer")
    table.pane_started("w1:p9", mark)
    cmd = table.background("w1:p9")
    spared = table.background("w1:p9")
    found = procs.scan(table.snapshot(), mark, procs.excluded(table.snapshot()))
    assert {cmd.pid, spared.pid} <= {p.pid for p in found.members}

    outcome = procs.end(table, mark, found, {spared.pid}, sleep=table.sleep,
                        clock=table.clock)

    assert spared.pid not in table.signalled() and spared.pid in table.procs
    assert cmd.pid in table.signalled() and cmd.pid not in table.procs
    assert [p.pid for p in outcome.survivors] == [], "a skipped pid is not a survivor either"


def test_a_table_that_fails_after_the_close_is_a_refusal(rig):
    """Catches (Q11b, X6): a process table that cannot be read **after** the
    close being read as "no processes": the pane is closed, the command may be
    running, the release is refused with that reason and the developer is not
    frozen."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs.fail_after_close = True

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and "after the close" in answer.why
    assert pane not in rig.server.panes and cmd.pid in rig.fprocs.procs
    assert rig.fprocs.signals == []
    assert rig.events("refused", pane=pane)[-1]["why"] == answer.why


@pytest.mark.parametrize("case", [
    pytest.param("table-unreadable", id="table-unreadable"),
    pytest.param("unreadable-only-before-the-close", id="unreadable-only-before-the-close"),
    pytest.param("no-process-carries-the-mark", id="no-process-carries-the-mark"),
])
def test_processes_that_cannot_be_told_fail_closed(rig, case):
    """Catches (Q11, Q13): a table that cannot be read before the close (always,
    or just once), or a pane whose processes carry no mark (the environment did
    not reach them), being read as "nothing to end". The pane is closed, the
    release is refused with the reason, nothing is signalled."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    if case == "table-unreadable":
        rig.fprocs.unreadable = True
    elif case == "unreadable-only-before-the-close":
        rig.fprocs.fail_next = 1
    else:
        for pid in list(rig.fprocs.procs):
            rig.fprocs.replace(pid, mark=None)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and answer.why
    assert pane not in rig.server.panes and rig.fprocs.signals == []
    assert rig.events("refused", pane=pane)[-1]["why"] == answer.why
    if case != "table-unreadable":
        assert cmd.pid in rig.fprocs.procs


def test_a_survivor_of_a_planner_or_qa_is_recorded_and_does_not_stop_the_run(git_rig):
    """Catches (Q20): the planner's or QA's survivor stopping the run: only the
    developer's refusal does. Their survivors are recorded (`refused`, with the
    pids) and the iteration is accepted."""
    rig = git_rig
    survivors: dict[str, procs.Proc] = {}

    def immortal(role: Role):
        def start():
            fp = rig.fprocs
            cmd = fp.background(rig.d._role_panes()[role])
            fp.immortal.add(cmd.pid)
            survivors[role.value] = cmd
        return start

    play(rig, extras={Role.PLANNER: immortal(Role.PLANNER), Role.QA: immortal(Role.QA)})

    out = rig.controller.run_iteration(rig.state)

    assert out.accepted, out.reason
    for role, cmd in survivors.items():
        assert rig.events("refused", role=role)[-1]["pids"] == [cmd.pid]
    assert set(survivors) == {"planner", "qa"}
    assert rig.events("refused", role="developer") == []


def test_the_run_end_ends_the_processes_of_every_pane_it_closes(rig):
    """Catches (Q14): the run end closing panes and leaving their commands
    running. Every marked pane it closes has its commands ended (recorded);
    a survivor there raises nothing; an adopted pane (no mark) is closed and
    its processes are not looked for."""
    first, _ = _dev(rig, Role.PLANNER)
    second, _ = _dev(rig, Role.QA)
    a = rig.fprocs.background(first)
    b = rig.fprocs.background(second)
    rig.fprocs.immortal.add(b.pid)
    rig.restored(Role.DEVELOPER, pane="w9:p7", tab="w9:t7")
    rig.d._ensure_agent(Role.DEVELOPER, rig.state)

    rig.d.close_own()

    assert a.pid not in rig.fprocs.procs and b.pid in rig.fprocs.procs
    assert _ended(rig) == [a.pid]
    assert rig.events("refused", pane=second)[-1]["pids"] == [b.pid]
    assert rig.events("refused", pane="w9:p7") == [], "an adopted pane has nothing to look for"
    assert rig.server.agents == {} and rig.d.unconfirmed == []


def test_every_role_pane_is_created_with_a_mark_of_its_own(rig):
    """Catches (Q17, Q18): a pane created without the mark in its environment
    (nothing could ever be found), or with a mark that is a function of run and
    role (two runs would take each other's processes). Every `tab create`
    carries `--env HOH_ROLE_MARK=<mark>` next to the existing pane environment;
    the three marks differ; a role released and started again gets a new one;
    the mark is recorded for the pane and is one `mint` returned."""
    panes = {role: rig.pane(role) for role in Role}
    creates = [c for c in rig.server.calls if c[1:3] == ["tab", "create"]]
    assert len(creates) == 3
    marks = []
    for call in creates:
        values = [call[i + 1] for i, a in enumerate(call) if a == "--env"]
        carried = [v for v in values if v.startswith(procs.MARK_ENV + "=")]
        assert len(carried) == 1 and len(values) > 1, "the pane environment is still passed"
        marks.append(carried[0].split("=", 1)[1])
    assert len(set(marks)) == 3
    for role, pane in panes.items():
        mark = rig.d._pane_marks()[pane]
        assert mark in marks
        label, who, nonce = mark.rsplit(":", 2)
        assert label == rig.store.dir.name and who == role.value and len(nonce) == 24
        procs.marked_left(rig.fprocs.snapshot(), mark)      # minted: not refused

    rig.release(Role.PLANNER)
    again = rig.pane(Role.PLANNER)
    assert rig.d._pane_marks()[again] not in marks


def test_a_zombie_is_not_a_marked_leftover_of_a_gone_pane(rig):
    """Catches (S5): a zombie that still carries the mark making a gone pane
    "have processes left", so the release is refused for a process that has
    already exited. `marked_left` ignores it; the gone pane is quiescent."""
    pane, mark = _dev(rig)
    rig.fprocs.add(ppid=rig.fprocs.reaper, mark=mark, state="Z", comm="defunct")
    live = rig.fprocs.background(pane)
    rig.fprocs._kill(live.pid)
    rig.server._close(pane, whole_tab=False)

    assert procs.marked_left(rig.fprocs.snapshot(), mark) == []
    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signals == []


def test_pid_1_is_never_touched_even_when_it_is_not_an_ancestor(rig):
    """Catches (S12, xS5): pid 1 being excluded only as an ancestor of this
    process, so a table where it is not one (a container whose init is another
    process, an ancestor owned by another user) lets it be signalled when it
    carries the mark and sits in the session. `excluded` keeps it by name."""
    me = os.getpid()
    detached = [procs.Proc(pid=1, ppid=0, pgrp=1, sid=1, state="S", start=1, comm="init"),
                procs.Proc(pid=me, ppid=999_999, pgrp=me, sid=me, state="S", start=5,
                           comm="pytest")]
    assert 1 in procs.excluded(detached, me) and me in procs.excluded(detached, me)

    pane, mark = _dev(rig)
    session = rig.fprocs.pane_procs[pane]["session"]
    rig.fprocs.replace(me, ppid=999_999)
    rig.fprocs.replace(1, mark=mark, sid=session)
    cmd = rig.fprocs.background(pane)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert 1 not in rig.fprocs.signalled() and 1 in rig.fprocs.procs
    assert rig.fprocs.signalled() == {cmd.pid}


def test_a_gone_pane_does_not_name_this_process_or_what_it_started(rig):
    """Catches (S20): the look at a pane that is already gone naming this very
    process, or what it started, because they carry the mark -- the test
    runner's own tree must never make a release refuse. Control: without the
    skip set the same table does name them."""
    pane, mark = _dev(rig)
    me = os.getpid()
    kid = rig.fprocs.add(ppid=me, comm="my-child", mark=mark)
    rig.fprocs.replace(me, mark=mark)
    rig.server._close(pane, whole_tab=False)

    table = rig.fprocs.snapshot()
    assert {p.pid for p in procs.marked_left(table, mark)} == {me, kid.pid}, "control"
    assert procs.marked_left(table, mark, procs.excluded(table, me)) == []

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert rig.fprocs.signals == []


@pytest.mark.parametrize("when", [pytest.param("turn-end", id="turn-end"),
                                  pytest.param("run-end", id="run-end")])
@pytest.mark.parametrize("shape", [
    pytest.param("command-in-the-session", id="command-in-the-session"),
    pytest.param("detached-leader-under-a-subreaper", id="detached-leader-under-a-subreaper"),
    pytest.param("detached-leader-under-init", id="detached-leader-under-init"),
])
def test_a_pane_that_is_gone_with_marked_processes_left_is_a_refusal_and_never_touches_them(
        tmp_path, monkeypatch, shape, when):
    """Catches (S1, Q13b, Q41, Q42; the finding of round 4): a pane that is
    already gone has no root and none is looked for. Orphans are adopted by a
    subreaper, not by init, so a detached marked daemon is also a session
    leader with an unmarked parent: v2.2 took it for "the root" and SIGTERMed
    it. Three shapes (a command in the session, a detached leader under a
    subreaper, a detached leader under init), at the turn end and at the run
    end: the leftovers are named, the release is refused, **nothing is
    signalled**. Control: a gone pane with nothing left is quiescent."""
    rig = Rig(tmp_path / f"{shape}-{when}", monkeypatch,
              reaper=1 if shape.endswith("init") else 1540)
    pane, mark = _dev(rig)
    left = (rig.fprocs.background(pane) if shape == "command-in-the-session"
            else rig.fprocs.daemon(pane, parent="init" if shape.endswith("init") else "subreaper"))
    rig.server._close(pane, whole_tab=False)
    assert left.pid in rig.fprocs.procs, "it outlived the pane"

    if when == "turn-end":
        answer = rig.release(Role.DEVELOPER)
        assert answer.closed is False and str(left.pid) in answer.why
        assert "already gone" in answer.why
    else:
        rig.d.close_own()
        assert str(left.pid) in rig.events("refused", pane=pane)[-1]["why"]

    assert rig.fprocs.signals == [], "nothing anchors them, so none is signalled"
    assert left.pid in rig.fprocs.procs
    assert rig.events("closed", pane=pane)[0]["already_gone"] is True
    assert rig.events("refused", pane=pane)[-1]["escaped"] == [left.pid]


# --------------------------------------------------------------------------- #
# The real `/proc` adapter, on real, harmless processes
# --------------------------------------------------------------------------- #
#
# Never Herdr, never an agent. Each test mints marks of its own, starts only its
# own processes, and ends only what it started: by the record taken right after
# the spawn (pid **and** start time, checked by `ProcTable.signal`), through
# `Popen`, or by a mark it minted itself -- never by a start time read from a
# later snapshot, never by a bare pid. They need a mounted `/proc`.

CLK_TCK = os.sysconf("SC_CLK_TCK")
SLEEP = shutil.which("sleep") or "/bin/sleep"
SH = shutil.which("sh") or "/bin/sh"


class Real:
    """The real processes of one test, and the finalizer that ends them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.table = procs.ProcTable()
        self.records: list[procs.Proc] = []
        self.popens: list[subprocess.Popen] = []
        self.marks: list[str] = []

    def mint(self, role: str = "developer") -> str:
        mark = procs.mint("realtest", role)
        self.marks.append(mark)
        return mark

    def lookup(self, pid: int, *, wait: float = 5.0) -> procs.Proc:
        """The record of a process, taken as soon as it is visible."""
        deadline = time.monotonic() + wait
        while True:
            for proc in self.table.snapshot():
                if proc.pid == pid:
                    self.records.append(proc)
                    return proc
            if time.monotonic() > deadline:
                raise AssertionError(f"process {pid} never showed in /proc")
            time.sleep(0.02)

    def settle(self, pid: int, *, comm: str | None = None, leader: bool = False,
               orphaned_from: tuple[int, ...] = (), wait: float = 10.0) -> procs.Proc:
        """The record of a process **once it has become what the test says it is**.

        A pid taken from `$!` or a pid file names a process that may still be
        between `fork` and `exec` (its name is its parent's: `setsid`, `sh`) or
        before its `setsid()`. Pid and start time do not change across those, so
        the first sighting is kept for the finalizer at once; the record that is
        *returned* is taken when `comm` is exactly the one asked for, the process
        leads its own session when `leader`, and, for an orphan, its parent is no
        longer one of `orphaned_from` nor a child of one of them (a subshell that
        has not exited yet). Bounded; on the deadline the failure names the pid
        and what was seen, never a silent pass."""
        deadline = time.monotonic() + wait
        seen = "never listed"
        first = True
        while True:
            rows = {p.pid: p for p in self.table.snapshot()}
            proc = rows.get(pid)
            if proc is not None:
                if first:
                    self.records.append(proc)
                    first = False
                parent = rows.get(proc.ppid)
                parent_in_tree = proc.ppid in orphaned_from or (
                    parent is not None and parent.ppid in orphaned_from)
                seen = (f"comm={proc.comm!r} sid={proc.sid} ppid={proc.ppid} "
                        f"state={proc.state}")
                if ((comm is None or proc.comm == comm)
                        and (not leader or proc.sid == proc.pid)
                        and not (orphaned_from and parent_in_tree)
                        and not proc.zombie):
                    self.records.append(proc)
                    return proc
            if time.monotonic() > deadline:
                want = [f"comm={comm!r}" if comm else "", "session leader" if leader else "",
                        f"orphaned from {orphaned_from}" if orphaned_from else ""]
                raise AssertionError(
                    f"process {pid} did not settle within {wait} s "
                    f"(wanted {', '.join(w for w in want if w) or 'to be listed'}): {seen}")
            time.sleep(0.02)

    def popen(self, argv, **kwargs) -> tuple[subprocess.Popen, procs.Proc]:
        child = subprocess.Popen(argv, **kwargs)
        self.popens.append(child)
        return child, self.lookup(child.pid)

    def pidfile(self, name: str, *, wait: float = 5.0) -> int:
        path = self.tmp / name
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return int(path.read_text().strip())
            time.sleep(0.02)
        raise AssertionError(f"{name} was never written")

    def alive(self, pid: int) -> bool:
        """Not gone and not a zombie."""
        for proc in self.table.snapshot():
            if proc.pid == pid:
                return not proc.zombie
        return False

    def launch_server(self, script: str) -> procs.Proc:
        """A stand-in Herdr server: a `sh` **named `herdr`** (a symlink to `sh`,
        so its name is what the product runs) leading a session of its own,
        detached from this process, so that nothing it starts is "what this
        process started". Returns its record."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "herdr"
        if not link.exists():
            link.symlink_to(SH)
        env = {k: v for k, v in os.environ.items() if k != procs.MARK_ENV}
        launcher = (f"setsid {shlex.quote(str(link))} -c {shlex.quote(script)} "
                    "</dev/null >/dev/null 2>&1 & echo $!")
        out = subprocess.run([SH, "-c", launcher], capture_output=True, text=True,
                             check=True, env=env, timeout=30)
        # `$!` is the pid of the forked `setsid`; it is the server only once it
        # has exec'd the `herdr` symlink (its name until then is `setsid`).
        return self.settle(int(out.stdout.strip()), comm="herdr", leader=True)

    def finish(self) -> None:
        for child in self.popens:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:       # pragma: no cover
                pass
        for record in self.records:
            self.table.signal(record, signal.SIGKILL)
        try:
            for proc in self.table.snapshot():
                if proc.mark in self.marks:
                    self.table.signal(proc, signal.SIGKILL)
        except OSError:                              # pragma: no cover
            pass


@pytest.fixture
def real(tmp_path):
    handle = Real(tmp_path / "real")
    handle.tmp.mkdir()
    try:
        yield handle
    finally:
        handle.finish()


def _pane_script(directory: Path, mark: str, other: str, *, leader: bool = True,
                 daemon: bool = True) -> str:
    """The server's script: a pane shell carrying `mark` (a session leader with
    `setsid`, else not) that starts a `sleep` and waits; and orphans that the
    server's subshells leave behind -- a marked detached `sleep`, another mint's,
    and an unmarked one -- adopted by whatever reaps orphans on this machine."""
    d = shlex.quote(str(directory))
    setsid = "setsid " if leader else ""
    lines = [
        f"HOH_ROLE_MARK={shlex.quote(mark)} {setsid}sh -c "
        f"'echo $$ > \"$0/shell.pid\"; {SLEEP} 120 & echo $! > \"$0/cmd.pid\"; wait' {d} &",
        f"( HOH_ROLE_MARK={shlex.quote(other)} {SLEEP} 122 & echo $! > {d}/other.pid )",
        f"( {SLEEP} 123 & echo $! > {d}/plain.pid )",
    ]
    if daemon:
        lines.insert(1, f"( HOH_ROLE_MARK={shlex.quote(mark)} setsid {SLEEP} 121 & "
                        f"echo $! > {d}/daemon.pid )")
    # The server outlives its pane shell, as the real one does.
    return "\n".join([*lines, "while :; do sleep 1; done"])


def _real_rig(tmp_path, monkeypatch, real: Real, *, leader: bool = True):
    """A rig whose process table is the real one, whose pane's processes are the
    real tree above, and whose fake Herdr ends the real pane shell when it
    closes the pane -- as the real Herdr does (it does not end the commands)."""
    rig = Rig(tmp_path / "rig", monkeypatch)
    pane = rig.pane(Role.DEVELOPER)
    mark = rig.d._pane_marks()[pane]
    real.marks.append(mark)
    other = real.mint("planner")
    monkeypatch.setattr(rig.d, "_proc_table", lambda: real.table)
    server = real.launch_server(_pane_script(real.tmp, mark, other, leader=leader))
    # Each record is taken only once its process has settled: the pid files are
    # written between `fork` and `exec`, so a record taken at once can still
    # carry the parent's name or lack the `setsid()` the test asserts.
    shell = real.settle(real.pidfile("shell.pid"), leader=leader)
    tree = types.SimpleNamespace(
        pane=pane, mark=mark, server=server, shell=shell,
        cmd=real.settle(real.pidfile("cmd.pid"), comm="sleep"),
        daemon=(real.settle(real.pidfile("daemon.pid"), comm="sleep", leader=True,
                            orphaned_from=(server.pid, shell.pid))),
        other=real.settle(real.pidfile("other.pid"), comm="sleep"),
        plain=real.settle(real.pidfile("plain.pid"), comm="sleep"))
    rig.server.on_pane_close.append(lambda closed: real.table.signal(shell, signal.SIGKILL))
    return rig, tree


def test_the_real_process_table_ends_what_a_closed_pane_left_and_only_that(
        tmp_path, monkeypatch, real):
    """Catches (Q9, Q33, S17; the whole of item 4b on real processes): a real
    **pane shape** -- a `sh` named `herdr` leading a session, below it a marked
    shell that leads its own session and starts a `sleep`; next to it a
    **detached marked `sleep` that called `setsid` and was orphaned to whatever
    reaps orphans on this machine** (a subreaper, not pid 1), another mint's
    `sleep`, and an unmarked one. The fake Herdr ends the shell when it closes the
    pane. Only the pane's command is ended; the daemon is named, alive and
    untouched; the others are untouched."""
    rig, tree = _real_rig(tmp_path, monkeypatch, real)
    assert tree.server.comm == "herdr" and tree.server.sid == tree.server.pid
    assert tree.shell.sid == tree.shell.pid and tree.shell.ppid == tree.server.pid
    assert tree.cmd.sid == tree.shell.sid and tree.daemon.sid == tree.daemon.pid
    assert tree.daemon.ppid not in (tree.server.pid, tree.shell.pid), "orphaned past the server"

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and str(tree.daemon.pid) in answer.why
    assert not real.alive(tree.shell.pid) and not real.alive(tree.cmd.pid)
    for survivor in (tree.daemon, tree.other, tree.plain, tree.server):
        assert real.alive(survivor.pid), f"{survivor.comm} {survivor.pid} was touched"
    assert [r["pids"] for r in rig.events("ended")] == [[tree.cmd.pid]]
    assert rig.events("refused", pane=tree.pane)[-1]["escaped"] == [tree.daemon.pid]


def test_a_real_pane_that_is_gone_has_no_root_and_its_marked_leftovers_are_only_named(
        tmp_path, monkeypatch, real):
    """Catches (S2; the finding of round 4, on real processes): with the pane
    gone, the pane's command **and** the detached daemon are orphans adopted by a
    subreaper; the daemon is a marked session leader with an unmarked parent,
    exactly what v2.2 took for "the root" and SIGTERMed. A gone pane gets no
    root: both are named, neither is touched."""
    rig, tree = _real_rig(tmp_path, monkeypatch, real)
    rig.server._close(tree.pane, whole_tab=False)           # Herdr ends the shell
    assert not real.alive(tree.shell.pid) and real.alive(tree.cmd.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and "already gone" in answer.why
    for left in (tree.cmd, tree.daemon):
        assert str(left.pid) in answer.why and real.alive(left.pid)
    assert real.alive(tree.other.pid) and real.alive(tree.plain.pid)
    assert rig.events("ended") == []
    left = procs.marked_left(real.table.snapshot(), tree.mark,
                             procs.excluded(real.table.snapshot()))
    assert {p.pid for p in left} == {tree.cmd.pid, tree.daemon.pid}


def test_a_real_shell_that_is_not_a_session_leader_is_no_root_and_the_daemon_never_becomes_one(
        tmp_path, monkeypatch, real):
    """Catches (Q32, the second shape of the finding): the pane's shell is not a
    session leader, so the marked detached daemon is the **only** marked leader
    with an unmarked parent. v2.2 made it "the one root" and signalled it. The
    root's parent must be a Herdr server: no root, nothing is signalled, the
    pane is closed, the release is refused."""
    rig, tree = _real_rig(tmp_path, monkeypatch, real, leader=False)
    assert tree.shell.sid != tree.shell.pid

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False and "cannot be told" in answer.why
    assert tree.pane not in rig.server.panes
    for survivor in (tree.daemon, tree.cmd, tree.other, tree.plain):
        assert real.alive(survivor.pid), f"{survivor.comm} {survivor.pid} was touched"
    assert rig.events("ended") == []


def _stat_by_regex(pid: int) -> dict:
    """An independent parse of `/proc/<pid>/stat` (a regular expression over
    `proc(5)`'s numbering) and what the kernel itself says: `getsid`, `getpgid`,
    and `/proc/<pid>/status` for the state."""
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^(?P<pid>\d+) \((?P<comm>.*)\) (?P<state>\S) (?P<ppid>\d+) (?P<pgrp>\d+) "
                 r"(?P<sid>\d+) (?:\S+ ){15}(?P<start>\d+) ", text, re.S)
    assert m, text
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    return {
        "comm": m["comm"], "ppid": int(m["ppid"]), "start": int(m["start"]),
        "state": re.search(r"^State:\s+(\S)", status, re.M)[1],
        "sid": os.getsid(pid), "pgrp": os.getpgid(pid),
        "stat_state": m["state"], "stat_sid": int(m["sid"]), "stat_pgrp": int(m["pgrp"]),
        "status_ppid": int(re.search(r"^PPid:\s+(\d+)", status, re.M)[1]),
    }


def _consistent_view(table: procs.ProcTable, pids: list[int], *, timeout: float = 10.0):
    """`(snapshot rows by pid, independent parse by pid)` **as of one instant**:
    wait, bounded, until every child is asleep (`S`; a just-started child is
    `R`), then take the independent parse on **both sides** of the snapshot and
    require them to be equal. Compared at two instants a child that is still
    starting up reads `R` once and `S` the next time."""
    deadline = time.monotonic() + timeout
    seen: dict[int, str] = {}
    while time.monotonic() < deadline:
        before = {pid: _stat_by_regex(pid) for pid in pids}
        rows = {p.pid: p for p in table.snapshot()}
        after = {pid: _stat_by_regex(pid) for pid in pids}
        seen = {pid: info["state"] for pid, info in after.items()}
        if before == after and all(state == "S" for state in seen.values()):
            return rows, after
        time.sleep(0.05)
    raise AssertionError(f"the children did not all settle asleep within {timeout} s: {seen}")


#: Starts the three children of `test_the_real_stat_fields_agree_with_an_independent_parse`
#: from a process of its own, and writes each child's pid and the monotonic
#: clock right after it was started (`CLOCK_MONOTONIC` is the same in every
#: process). The pid file is written last, after its time.
_STARTER = """\
import os, subprocess, sys, time
out, sleep, odd = sys.argv[1:4]


def put(name, text):
    tmp = os.path.join(out, name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, os.path.join(out, name))


for name, argv in (("first", [sleep, "60"]), ("second", [odd, "60"]), ("third", [sleep, "60"])):
    if name == "second":
        time.sleep(1.1)
    kid = subprocess.Popen(argv, process_group=0)
    put(name + ".at", repr(time.monotonic()))
    put(name + ".pid", str(kid.pid))
time.sleep(120)
"""


def test_the_real_stat_fields_agree_with_an_independent_parse(tmp_path, real):
    """Catches (S6-S10, xS4, xS15-xS18): `ppid`, `sid`, `start`, `state` and
    `comm` read from the wrong field of `/proc/<pid>/stat`, or from the first `)`
    of a name that holds one. Real children, each leading a process group of its
    own **inside this process's session**, one whose name holds `) S 9 9`, the
    first and third started a second apart (a start read from the wrong field is
    0 for everyone), compared with an independent parse as of **one instant**;
    the start-time gap is compared with the wall clock between the spawns, not
    with a fixed budget.

    **The same in every regime.** The children are started by an intermediate
    process that leads no session. Started by pytest itself, their `ppid` is
    pytest's pid, which **is** their `sid` whenever pytest leads its own session
    -- as it does under `runner.run_check` with isolation NONE (`os.setsid()` in
    `runner._limits`, then `bash` execs pytest) -- and then `ppid`, `pgrp` and
    `sid` would not be three different numbers and a swap of two fields could not
    show. With the intermediate they are three different numbers whoever leads
    the session above them."""
    link = tmp_path / "bin"
    link.mkdir()
    odd = link / "x) S 9 9"
    odd.symlink_to(SLEEP)
    middle, middle_rec = real.popen([sys.executable, "-c", _STARTER, str(real.tmp), SLEEP, str(odd)])
    session = os.getsid(0)
    assert os.getsid(middle.pid) == session and session != middle.pid, \
        "precondition: the intermediate shares our session and leads none of its own"
    first_rec = real.settle(real.pidfile("first.pid", wait=15), comm="sleep")
    second_rec = real.settle(real.pidfile("second.pid", wait=15), comm="x) S 9 9")
    third_rec = real.settle(real.pidfile("third.pid", wait=15), comm="sleep")
    wall_first = float((real.tmp / "first.at").read_text())
    wall_third = float((real.tmp / "third.at").read_text())
    kids = [first_rec, second_rec, third_rec]

    rows, parsed = _consistent_view(real.table, [k.pid for k in kids])

    for kid in kids:
        got, want = rows[kid.pid], parsed[kid.pid]
        assert got.ppid == want["ppid"] == want["status_ppid"] == middle.pid
        assert got.sid == want["sid"] == want["stat_sid"] == session
        assert got.pgrp == want["pgrp"] == want["stat_pgrp"] == kid.pid
        assert len({got.ppid, got.pgrp, got.sid}) == 3, \
            "ppid, pgrp and sid all differ, so a field read from the wrong place shows"
        assert got.state == want["state"] == "S"
        assert got.comm == want["comm"]
        assert got.start == want["start"] and got.start > 0
    assert rows[second_rec.pid].comm == "x) S 9 9"
    gap_ticks = rows[third_rec.pid].start - rows[first_rec.pid].start
    gap_wall = wall_third - wall_first
    assert abs(gap_ticks / CLK_TCK - gap_wall) <= 1.0
    assert gap_ticks / CLK_TCK >= 0.9, "a start read from the wrong field is the same for all"


def test_a_real_zombie_is_read_as_a_zombie(real):
    """Catches (S8, xS17): a zombie being read as a live process because the
    state is taken from the wrong field. A real child that exited and was not
    waited for is `Z`, `zombie` is True, and it is never "a marked leftover"."""
    child, record = real.popen([SH, "-c", "exit 0"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _stat_by_regex(child.pid)["state"] != "Z":
        time.sleep(0.02)
    assert _stat_by_regex(child.pid)["state"] == "Z", "precondition: a real zombie"

    rows = {p.pid: p for p in real.table.snapshot()}

    assert rows[child.pid].state == "Z" and rows[child.pid].zombie is True
    assert child.pid in {p.pid for p in real.table.snapshot()}
    assert procs.marked_left([dataclasses.replace(rows[child.pid], mark=real.mint())],
                             real.marks[-1]) == []
    child.wait(timeout=5)


def test_the_real_snapshot_holds_this_users_processes_and_nobody_elses(real):
    """Catches (S11, xS1): a snapshot with no user filter, so other users'
    processes -- which HoH must never signal -- are in the table. NOT_RUN where
    pid 1 is this user's (root, or a sandbox whose init this user owns): there is
    no other user's process to leave out."""
    uid = os.getuid()
    rows = real.table.snapshot()
    assert os.getpid() in {p.pid for p in rows}
    for proc in rows:
        try:
            assert os.stat(f"/proc/{proc.pid}").st_uid == uid
        except FileNotFoundError:
            continue
    if os.stat("/proc/1").st_uid == uid:
        pytest.skip("NOT_RUN: pid 1 is this user's, so there is no other user's process "
                    "to leave out of the snapshot")
    assert 1 not in {p.pid for p in rows}


def test_signal_refuses_a_pid_whose_start_time_is_not_the_one_seen(real):
    """Catches (Q26, X1): `ProcTable.signal` without the start-time re-read -- a
    reused pid receiving the signal. `signal(Proc(pid, ..., start + 1))` is
    `False` and the `sleep` lives. Control: with the start time that was seen
    the signal is delivered and the process ends."""
    child, record = real.popen([SLEEP, "60"])

    assert real.table.signal(dataclasses.replace(record, start=record.start + 1),
                             signal.SIGTERM) is False
    assert child.poll() is None, "the sleep lives"

    assert real.table.signal(record, signal.SIGTERM) is True
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_signal_works_without_pidfd_and_still_checks_the_start_time(real, monkeypatch):
    """Catches (Q26, Q21): the fallback path of an interpreter with no
    `os.pidfd_open` (this machine's default `python3`, Anaconda 3.13.5, has none)
    skipping the start-time check, or not working at all: re-read, then `kill`.
    Both halves on a real process."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    child, record = real.popen([SLEEP, "60"])

    assert real.table.signal(dataclasses.replace(record, start=record.start + 1),
                             signal.SIGTERM) is False
    assert child.poll() is None
    assert real.table.signal(record, signal.SIGTERM) is True
    assert child.wait(timeout=5) == -signal.SIGTERM
    assert real.table.signal(record, signal.SIGTERM) is False, "gone: nothing to signal"


def test_a_signal_goes_through_a_pidfd_and_never_through_a_bare_pid(monkeypatch):
    """Catches (Q27, S13, xS3): the signal going to a bare pid where pidfds
    exist, or the pidfd opened **after** the start time is read (a reused pid
    could then receive it). The two pidfd functions are stood in for -- this
    machine's interpreter has none; what is proven is the path **and its order**:
    open the pidfd, read the start time, send. Nothing is signalled through
    `os.kill`."""
    order: list[str] = []
    monkeypatch.setattr(os, "pidfd_open",
                        lambda pid: (order.append("open"), os.open(os.devnull, os.O_RDONLY))[1],
                        raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal",
                        lambda fd, sig: order.append("send"), raising=False)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("a bare pid was signalled"))
    table = procs.ProcTable()
    monkeypatch.setattr(table, "start_time", lambda pid: (order.append("start"), 77)[1])
    target = procs.Proc(pid=999_999, ppid=1, pgrp=1, sid=1, state="S", start=77, comm="x")

    assert table.signal(target, signal.SIGTERM) is True
    assert order == ["open", "start", "send"]

    order.clear()
    assert table.signal(dataclasses.replace(target, start=78), signal.SIGTERM) is False
    assert order == ["open", "start"], "a start time that changed is never sent to"

    order.clear()
    monkeypatch.setattr(os, "pidfd_open", lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
                        raising=False)
    assert table.signal(target, signal.SIGTERM) is False and order == []


def test_a_missing_proc_is_an_error_and_not_an_empty_table(tmp_path, rig):
    """Catches (Q28, X13): a `/proc` that is missing, unmounted or shows no
    process being read as "no processes", so a release finds nothing to end.
    `snapshot` raises; through the dispatcher the scan is a problem with its
    reason, and the release is refused."""
    with pytest.raises(OSError):
        procs.ProcTable(root=str(tmp_path / "no-such-proc")).snapshot()
    (tmp_path / "empty-proc").mkdir()
    with pytest.raises(OSError):
        procs.ProcTable(root=str(tmp_path / "empty-proc")).snapshot()

    pane = rig.pane(Role.DEVELOPER)
    rig.monkeypatch.setattr(rig.d, "_proc_table",
                            lambda: procs.ProcTable(root=str(tmp_path / "no-such-proc")))
    answer = rig.release(Role.DEVELOPER)
    assert answer.closed is False and "process table cannot be read" in answer.why
    assert pane not in rig.server.panes


# =========================================================================== #
# RT -- the gaps the integration reviews rplint-a and rplint-b found
# =========================================================================== #
#
# Each test below passes on the product code as it is: the reviews found the
# code correct and the tests missing. The mutant each one kills is named in its
# docstring; every mutant was alive before it.


@pytest.mark.parametrize("how", [pytest.param("run-end", id="run-end"),
                                 pytest.param("turn-end", id="turn-end")])
def test_a_pane_the_operator_opened_in_the_role_panes_own_directory_is_never_closed_with_its_tab(
        rig, how):
    """Catches (O2): the whole-tab test no longer asking whether **every pane in
    the tab is HoH's own**. The operator's pane sits next to a role pane, in the
    very directory the role pane was made in, with no agent in it: nothing but
    ownership tells it from ours (the identity check passes, `_identity_problem`
    is empty -- the control below), so a tab close would take it. Only the role
    pane is closed, by `close_own` and by the turn-end release alike; no tab
    close is asked for, no `closing` line names the operator's pane, and the
    ledger says that the tab stays and why."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    operator = rig.server.operator_pane(tab=tab, cwd=rig.server.panes[pane]["cwd"])
    assert operator not in {row["pane_id"] for row in rig.server.agents.values()}
    assert rig.d._identity_problem(operator, rig.server.snapshot()) == "", \
        "precondition: its directory passes the identity check, so only ownership refuses it"
    assert operator not in rig.d._own_pane_ids()

    if how == "run-end":
        assert rig.d.close_own() == [], "the tab is not gone: the operator's pane holds it"
    else:
        answer = rig.release(Role.PLANNER)
        assert answer.closed is True, answer

    assert operator in rig.server.panes, "the operator's pane survives"
    assert pane not in rig.server.panes
    assert rig.server.closes() == [["pane", "close", pane]], "the pane alone: no tab close"
    assert [(r["pane"], r["scope"]) for r in rig.events("closing")] == [(pane, "pane")]
    assert operator not in [r.get("pane") for r in rig.ledger() if r["event"] == "closed"]
    kept = rig.events("kept", tab=tab)
    assert kept and operator in kept[0]["why"], "the tab is said to stay open, and why"
    assert rig.d.own_tabs == [] and rig.d.unconfirmed == []


@pytest.mark.parametrize("meanwhile", [
    pytest.param("snapshot-unreadable", id="snapshot-unreadable"),
    pytest.param("agent-working-again", id="agent-working-again"),
])
def test_a_kept_pane_is_refused_as_kept_before_anything_else_is_looked_at(rig, meanwhile):
    """Catches (K5, the turn end's own check of the keep): `release_role` no
    longer asking whether the pane is kept itself and leaving it to the close.
    The close refuses a kept pane too, but only after the snapshot was read and
    the agent's status judged -- so with a snapshot that cannot be read, or an
    agent that is working again, the answer would be that reason instead, with a
    `refused` line where a `kept` line belongs. Here the first and only line of
    the release says `kept`, with the operator's reason."""
    pane = rig.pane(Role.PLANNER)
    rig.d.keep_open(Role.PLANNER, "the operator is looking at it")
    if meanwhile == "snapshot-unreadable":
        rig.server.fail_snapshot = True
    else:
        rig.server.agents[rig.name(Role.PLANNER)]["agent_status"] = "working"

    answer = rig.release(Role.PLANNER)

    assert answer.closed is False
    assert answer.why == "the pane is kept open: the operator is looking at it"
    lines = [r for r in rig.ledger() if r.get("reason") == "turn finished"]
    assert [(r["event"], r["pane"], r["why"]) for r in lines] == [
        ("kept", pane, "the operator is looking at it")]
    assert rig.server.closes() == [] and pane in rig.server.panes


@pytest.mark.parametrize("fault", list(_SNAPSHOT_FAULTS))
def test_a_release_whose_first_look_at_the_snapshot_fails_is_refused_and_does_not_look_again(
        rig, monkeypatch, fault):
    """Catches (F12): `release_role` no longer refusing on a snapshot that proves
    nothing (unreadable, without the pane list, or listing no pane at all) and
    leaving it to the close, which reads the snapshot **again**. Only the first
    look is bad here: a release that goes on after it would find a good
    snapshot at the second and close the pane. The refusal is the first look's:
    one look, a recorded reason, nothing closed. Control: the next release,
    with a whole snapshot, closes the pane."""
    pane = rig.pane(Role.PLANNER)
    whole = rig.server.snapshot
    looks: list[int] = []

    def bad_the_first_time():
        looks.append(1)
        if len(looks) > 1:
            return whole()
        _SNAPSHOT_FAULTS[fault](rig.server)
        try:
            return whole()
        finally:
            rig.server.fail_snapshot = False
            rig.server.partial_snapshot = False
            rig.server.empty_snapshot = False

    monkeypatch.setattr(herdr, "snapshot", bad_the_first_time)

    answer = rig.release(Role.PLANNER)

    assert answer.closed is False and answer.why
    assert len(looks) == 1, "the first look refused: nothing looked again"
    assert rig.server.closes() == [] and pane in rig.server.panes
    assert rig.events("refused", pane=pane, role="planner")[0]["why"] == answer.why

    assert rig.release(Role.PLANNER).closed is True


@pytest.mark.parametrize("key", ["agents", "panes", "tabs"])
def test_a_snapshot_that_lacks_a_list_is_refused_for_that_list(rig, monkeypatch, key):
    """Catches (F2, and the same for `agents` and `tabs`): a key dropped from the
    check that the snapshot carries all three lists. For `panes` the next check
    (no pane row at all) still refuses, so only the **reason** shows it: the
    refusal says which list is missing. For `agents` and `tabs` nothing refuses
    and the close goes ahead on a snapshot that proves nothing. Control: the same
    pane closes on a whole snapshot."""
    pane = rig.pane(Role.PLANNER)
    tab = rig.server.tab_of(pane)
    whole = rig.server.snapshot

    def lacking():
        snapshot = whole()
        del snapshot[key]
        return snapshot

    monkeypatch.setattr(herdr, "snapshot", lacking)

    rig.d.close_own()

    assert rig.server.closes() == [] and pane in rig.server.panes
    assert rig.events("refused", pane=pane)[0]["why"] == f"the Herdr snapshot has no {key} list"

    monkeypatch.setattr(herdr, "snapshot", whole)
    rig.d.close_own()
    assert rig.server.closes() == [["tab", "close", tab]]


def test_a_pane_that_is_gone_with_nothing_left_is_quiescent_after_one_look_at_the_process_table(
        rig, monkeypatch):
    """Catches (Q42, in effect): the release of a pane that is already gone, and
    has no marked process left, going on to `end` as if it had a root. There is
    nothing to anchor and nothing to end, and the one look that said so (the
    scan) is the whole proof: a process table that cannot be read a second time
    must not turn the quiescent release into a refusal. Control: the release
    names no `ended` and no `refused` line."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    rig.fprocs._kill(cmd.pid)
    rig.server._close(pane, whole_tab=False)
    taken = rig.fprocs.snapshot
    looks: list[int] = []

    def once():
        looks.append(1)
        if len(looks) > 1:
            raise OSError("the process table cannot be read a second time")
        return taken()

    monkeypatch.setattr(rig.fprocs, "snapshot", once)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is True, answer
    assert len(looks) == 1
    assert rig.events("closed", pane=pane)[0]["already_gone"] is True
    assert rig.events("ended") == [] and rig.events("refused", pane=pane) == []


def test_a_child_a_member_starts_after_the_scan_is_ended_though_it_has_left_the_session_unmarked():
    """Catches (Q4, the walk down from the members that `end` makes on every
    look): after the scan, the role's command starts a child that scrubbed its
    environment and leads a session of its own -- neither marked, nor in the
    root's session, nor known from the scan; only its parent, a member, says
    whose it is. The walk finds it and ends it. Without the walk it keeps running
    and nothing mentions it."""
    table = FakeProcs()
    mark = procs.mint("t", "developer")
    table.pane_started("w1:p9", mark)
    cmd = table.background("w1:p9")
    found = procs.scan(table.snapshot(), mark, procs.excluded(table.snapshot()))
    assert cmd.pid in {p.pid for p in found.members}
    table.pane_closed("w1:p9")
    late = table.add(ppid=cmd.pid, comm="sleep")
    assert late.mark is None and late.sid == late.pid
    assert late.key not in {p.key for p in found.members}

    outcome = procs.end(table, mark, found, procs.excluded(table.snapshot()),
                        sleep=table.sleep, clock=table.clock)

    assert late.pid in table.signalled() and late.pid not in table.procs
    assert cmd.pid not in table.procs
    assert late.pid in outcome.gone and outcome.survivors == () and outcome.escaped == ()


def test_a_daemon_that_detaches_while_the_pane_closes_is_named_and_never_signalled(rig):
    """Catches (Q35, the final look of `end`): a marked process that was not
    there at the scan -- it detaches as the pane closes -- being neither a
    survivor nor named. Only the look after the last round can see it. It is
    named in the refusal and in the ledger, never signalled, alive, while the
    role's own command is still ended."""
    pane, mark = _dev(rig)
    cmd = rig.fprocs.background(pane)
    detached: list[procs.Proc] = []
    rig.fprocs.on_close = lambda fp: detached.append(
        fp.add(ppid=fp.reaper, comm="late-daemon", mark=mark))

    answer = rig.release(Role.DEVELOPER)

    daemon = detached[0]
    assert answer.closed is False
    assert str(daemon.pid) in answer.why and "never signalled" in answer.why
    assert daemon.pid in rig.fprocs.procs and daemon.pid not in rig.fprocs.signalled()
    assert cmd.pid not in rig.fprocs.procs, "the role's own command is still ended"
    assert rig.events("refused", pane=pane)[-1]["escaped"] == [daemon.pid]


def test_a_marked_process_seen_outside_the_pane_at_the_scan_stays_named_when_it_is_gone_at_the_end(
        rig):
    """Catches (Q35, the scan's own report -- a characterisation, see
    `QUESTIONS.md` Q2): a marked daemon outside the pane when the scan was
    taken, that exits as the pane closes, so the final look no longer finds it.
    The release is still refused and names it: it was seen outside the pane
    while the agent was alive, and nothing shows that it was the role's to end.
    Fail closed; never signalled."""
    pane, _ = _dev(rig)
    cmd = rig.fprocs.background(pane)
    daemon = rig.fprocs.daemon(pane)
    rig.fprocs.on_close = lambda fp: fp._kill(daemon.pid)

    answer = rig.release(Role.DEVELOPER)

    assert answer.closed is False
    assert str(daemon.pid) in answer.why and "never signalled" in answer.why
    assert daemon.pid not in rig.fprocs.procs and daemon.pid not in rig.fprocs.signalled()
    assert cmd.pid not in rig.fprocs.procs
    assert rig.events("refused", pane=pane)[-1]["escaped"] == [daemon.pid]
