"""I10a (P5-39): the tabs a run's tooling opens are recorded when opened and closed
when the run is over -- `hoh surface open | list | reap` and the end of `hoh run`.

Every test runs with a stub `herdr` first on `PATH` that logs its argv and fails,
with `subprocess.run` and the `hoh.herdr` functions replaced by something that
fails the test, and with the stub's log asserted empty at the end (autouse). The
runtime is `FakeAgentControl` (tests/test_agent_control.py); the process table is
`FakeProcs` (a Herdr server and the shells below it) or, in R27 and R28, the real
`ProcView` over a directory shaped like `/proc` under `tmp_path`. No test starts
Herdr or an agent or reads the real `/proc`; every test writes only under
`tmp_path`. Each test names the failure it catches.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hoh import cli, herdr, pane_ledger, procs, storage_tiers, surfaces
from hoh.agentcontrol import ProgramAgentRole, Surface
from hoh.contracts import Condition, RunState, Stage
from hoh.controller import DispatchError
from hoh.durable_io import durable_append
from hoh.store import RunStore

from test_agent_control import FakeAgentControl

REAL_MAKE_CONTROL = surfaces.make_control
CLOSE_VERBS = {"close_tab", "close_pane"}
BLOCKED_KIND = "capability_violation"


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    """No test reaches Herdr: a stub `herdr` first on PATH logs and fails, the
    process-spawning and `hoh.herdr` functions fail the test, and the stub's log
    must be empty at the end."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "stub-herdr.log"
    stub = bin_dir / "herdr"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def refuse(*a, **k):
        pytest.fail(f"a test reached the outside world: {a!r} {k!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    for name in ("_cli", "snapshot", "agents", "find_agent", "liveness", "agent_status",
                 "read_agent", "worktree_create", "worktree_list"):
        monkeypatch.setattr(herdr, name, refuse)
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", \
        "the stub herdr was called"


class FakeProcs:
    """The process table: a Herdr server and the shells below it."""

    SERVER = 100

    def __init__(self) -> None:
        self.table: list[procs.Proc] = [self._proc(self.SERVER, 1, self.SERVER, self.SERVER,
                                                   "S", "herdr")]
        self.env: dict[int, str] = {}
        self.error: Exception | None = None
        self.asked = 0
        self._next = 200

    @staticmethod
    def _proc(pid, ppid, pgrp, sid, state, comm):
        return procs.Proc(pid=pid, ppid=ppid, pgrp=pgrp, sid=sid, state=state,
                          start=pid, comm=comm)

    def add(self, pid, ppid, pgrp, sid, state="S", comm="bash", pane=None) -> procs.Proc:
        proc = self._proc(pid, ppid, pgrp, sid, state, comm)
        self.table.append(proc)
        if pane is not None:
            self.env[pid] = pane
        return proc

    def add_shell(self, pane, *, comm="bash", state="S", parent=None) -> procs.Proc:
        pid, self._next = self._next, self._next + 10
        return self.add(pid, self.SERVER if parent is None else parent, pid, pid, state,
                        comm, pane)

    def shell_of(self, pane) -> procs.Proc:
        return next(p for p in self.table if self.env.get(p.pid) == pane)

    def snapshot(self):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return list(self.table)

    def pane_of(self, pid):
        return self.env.get(pid)


class World:
    """Runs under a root, the fake runtime, the fake process table, and `hoh`."""

    def __init__(self, tmp_path, monkeypatch, capsys) -> None:
        self.tmp_path, self.capsys = tmp_path, capsys
        self.root = tmp_path / "runs"
        self.root.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.fake = FakeAgentControl()
        self.procs = FakeProcs()
        self.controls = 0
        self.views = 0
        self.control_error: Exception | None = None
        self._n = 0
        monkeypatch.setattr(surfaces, "make_control", self._make_control)
        monkeypatch.setattr(surfaces, "make_proc_view", self._make_view)

    def _make_control(self):
        self.controls += 1
        if self.control_error is not None:
            raise self.control_error
        return self.fake

    def _make_view(self):
        self.views += 1
        return self.procs

    # -- runs ---------------------------------------------------------------- #
    def run_dir(self, run) -> Path:
        return self.root / run

    def set_state(self, run, stage=Stage.DEVELOPING, condition=Condition.ACTIVE, kind=None):
        store = RunStore(self.root, run)
        store.ensure()
        state = RunState(run_id=run, repo_path=str(self.work), project_name="p",
                         spec_path=str(self.work / "spec.md"), spec_digest="d",
                         policy_digest="d", profile_digest="d", stage=stage,
                         condition=condition, blocked_kind=kind)
        store.state_path.write_text(state.model_dump_json(), encoding="utf-8")
        return store

    def run(self, name=None, **kw) -> str:
        self._n += 1
        run = name or f"run-{self._n}"
        self.set_state(run, **kw)
        return run

    def finish(self, run) -> None:
        self.set_state(run, Stage.READY_FOR_DELIVERY, Condition.ACTIVE)

    # -- hoh ----------------------------------------------------------------- #
    def hoh(self, *argv):
        rc = cli.main(["--root", str(self.root), *argv])
        cap = self.capsys.readouterr()
        return rc, cap.out, cap.err

    def open(self, run, label="host", cwd=None, role="run-host") -> dict:
        rc, out, err = self.hoh("surface", "open", run, f"--label={label}",
                                f"--cwd={cwd or self.work}", f"--role={role}")
        assert rc == 0, err
        info = json.loads(out)
        self.procs.add_shell(info["pane_id"])
        return info

    def reap(self, *args, close=True):
        argv = ["surface", "reap", *args, *(["--close"] if close else [])]
        rc, out, err = self.hoh(*argv)
        return rc, (json.loads(out)["surfaces"] if out.strip() else []), err

    def one(self, run, **kw) -> dict:
        rc, rows, err = self.reap(run, **kw)
        assert len(rows) == 1, (rows, err)
        return rows[0]

    def reset(self) -> None:
        self.fake.calls.clear()
        self.controls = self.views = self.procs.asked = 0

    def verbs(self) -> list[str]:
        return self.fake.verbs()

    def closes(self) -> list[str]:
        return [v for v in self.verbs() if v in CLOSE_VERBS]

    # -- the runtime --------------------------------------------------------- #
    def add_agent(self, pane, status, name=None):
        row = self.fake.panes[pane]
        self.fake.agents[name or f"agent-{pane}"] = {
            "name": name or f"agent-{pane}", "pane_id": pane, "tab_id": row["tab_id"],
            "agent": "claude", "agent_status": status, "cwd": row["cwd"],
            "foreground_cwd": row["foreground_cwd"], "workspace_id": "w1"}

    def foreign_pane(self, tab=None, pane=None) -> str:
        self._n += 1
        pane = pane or f"w1:pF{self._n}"
        self.fake.panes[pane] = {"pane_id": pane, "tab_id": tab or f"w1:tF{self._n}",
                                 "workspace_id": "w1", "cwd": str(self.work),
                                 "foreground_cwd": str(self.work)}
        return pane

    # -- the records --------------------------------------------------------- #
    def ledger(self, run) -> list[dict]:
        path = self.run_dir(run) / pane_ledger.LEDGER
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def events(self, run) -> list[str]:
        return [rec["event"] for rec in self.ledger(run)]

    def registry(self, run):
        return surfaces.registry_for(self.run_dir(run))

    def states(self, run) -> list[str]:
        return [a.state for a in self.registry(run).agents()]

    def end_of_run(self, run, rc=0, *, finish=None):
        """`cli.cmd_run` with `_cmd_run` stubbed: the stub checks the marker is
        held and leaves the run as `finish` says."""
        marker_seen = []

        def stub(args):
            marker_seen.append(RunStore(args.root, args.run_id).running_process())
            if finish is not None:
                self.set_state(run, *finish)
            return rc

        self._stub = stub
        return marker_seen


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def run_cmd_run(world, monkeypatch, run, rc=0, finish=None):
    marker = world.end_of_run(run, rc, finish=finish)
    monkeypatch.setattr(cli, "_cmd_run", world._stub)
    args = argparse.Namespace(root=str(world.root), run_id=run)
    code = cli.cmd_run(args)
    return code, marker


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #


def test_open_records_the_tab_as_the_runs_own_in_its_own_file(world):
    """Catches: a surface recorded in the dispatcher's `owned-agents.jsonl` (which
    `close_own` closes with the run) or not recorded at all."""
    run = world.run()
    rc, out, err = world.hoh("surface", "open", run, "--label", "my host",
                             "--cwd", str(world.work))
    assert rc == 0, err
    info = json.loads(out)
    assert info["run_id"] == run and info["role"] == "program:run-host"
    assert info["registry"] == str(world.run_dir(run) / "owned-surfaces.jsonl")
    assert world.fake.calls == [("open_surface", str(world.work), "my host", {}, None)]
    assert world.fake.purposes == [("open_surface", f"open a surface for run {run}")]
    lines = [json.loads(x) for x in
             (world.run_dir(run) / "owned-surfaces.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert (rec["event"], rec["role"], rec["pane_id"], rec["tab_id"], rec["cwd"],
            rec["run_id"], rec["program"], rec["backend"]) == (
        "surface", "program:run-host", info["pane_id"], info["tab_id"], str(world.work),
        run, "p", "fake")
    assert not (world.run_dir(run) / "owned-agents.jsonl").exists()
    rc, out, err = world.hoh("surface", "list", run)
    row = json.loads(out)["surfaces"][0]
    assert rc == 0 and row["state"] == "owned" and row["run_over"] is False
    assert row["pane_id"] == info["pane_id"] and row["role"] == "program:run-host"


def test_open_refuses_what_it_cannot_own_and_creates_nothing(world, tmp_path):
    """Catches: any refusal of `surface open` that still makes the control, creates
    a tab or leaves a record; a label, role or directory that is not checked."""
    run = world.run("r-open")
    over = world.run("r-over")
    world.finish(over)
    home = Path(os.environ["HOME"])
    below_home = home / "project"
    below_home.mkdir()
    long_ok = "x" * 80

    def refused(run_id, *, role="run-host", label="host", cwd=None):
        world.reset()
        rc, out, err = world.hoh("surface", "open", run_id, f"--role={role}",
                                 f"--label={label}", f"--cwd={cwd or world.work}")
        assert (rc, out) == (2, ""), (run_id, role, label, cwd, err)
        assert "surface open refused" in err, err
        assert world.fake.calls == [] and world.controls == 0
        assert not (world.run_dir(run) / "owned-surfaces.jsonl").exists()
        assert not (world.run_dir(over) / "owned-surfaces.jsonl").exists()

    refused("no-such-run")
    refused("../x")
    refused(over)
    refused(run, role="planner")
    refused(run, role="Host")
    refused(run, role="host\n")
    refused(run, label="")
    refused(run, label="-x")
    refused(run, label="a\nb")
    refused(run, label="x" * 81)
    refused(run, cwd="work")
    refused(run, cwd=str(tmp_path / "missing"))
    refused(run, cwd="/")
    refused(run, cwd=str(home))
    refused(run, cwd=str(home.parent))
    for top in surfaces.SYSTEM_DIRS:
        if os.path.isdir(top):
            refused(run, cwd=top)
    world.reset()
    for label, cwd in ((long_ok, world.work), ("host", below_home)):
        rc, out, err = world.hoh("surface", "open", run, f"--label={label}", f"--cwd={cwd}")
        assert rc == 0, err
    assert world.verbs() == ["open_surface", "open_surface"]


def test_open_outside_herdr_refuses_and_starts_nothing(world, monkeypatch):
    """Catches: `surface open` outside Herdr that guesses a tab, records one or
    reaches the herdr binary (the autouse stub's log must stay empty)."""
    run = world.run()
    monkeypatch.setattr(surfaces, "make_control", REAL_MAKE_CONTROL)
    rc, out, err = world.hoh("surface", "open", run, "--label", "host",
                             "--cwd", str(world.work))
    assert (rc, out) == (2, "")
    assert "surface open failed" in err and "HERDR_ENV" in err
    assert not (world.run_dir(run) / "owned-surfaces.jsonl").exists()
    assert world.fake.calls == []


def test_a_tab_that_cannot_be_recorded_is_closed_again(world, tmp_path):
    """Catches: a tab left open that no record owns when the record cannot be
    appended."""
    run = world.run()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    os.symlink(target, world.run_dir(run) / "owned-surfaces.jsonl")
    rc, out, err = world.hoh("surface", "open", run, "--label", "host",
                             "--cwd", str(world.work))
    assert (rc, out) == (2, "") and "surface open failed" in err
    assert world.verbs()[0] == "open_surface" and world.verbs().count("close_tab") == 1
    assert set(world.fake.panes) == {FakeAgentControl.CALLER}
    assert target.read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------- #
# reap: what is never closed
# --------------------------------------------------------------------------- #


def test_nothing_is_closed_while_the_run_is_not_over(world):
    """Catches: a reap that closes a surface of a run that is active, blocked or
    still held by a live process; any call to Herdr or the process table then."""
    active = world.run("r-active")
    blocked = world.run("r-blocked", stage=Stage.READY_FOR_DELIVERY, condition=Condition.BLOCKED)
    held = world.run("r-held")
    panes = {r: world.open(r)["pane_id"] for r in (active, blocked, held)}
    world.finish(held)
    (world.run_dir(held) / "process.json").write_text(
        json.dumps({"pid": 1, "state": "running"}), encoding="utf-8")
    for run in (active, blocked, held):
        for close in (False, True):
            world.reset()
            rc, rows, err = world.reap(run, close=close)
            assert rc == 0 and [r["outcome"] for r in rows] == ["pending"], rows
            assert world.fake.calls == [] and world.controls == 0
            assert world.procs.asked == 0 and world.views == 0
            assert not (world.run_dir(run) / pane_ledger.LEDGER).exists()
            assert world.states(run) == ["owned"]
    assert all(p in world.fake.panes for p in panes.values())


def test_a_foreign_tab_is_never_closed_whatever_it_is_called(world):
    """Catches: a reaper that walks the snapshot instead of the record, or matches
    tabs by label or directory."""
    run = world.run("r-mine")
    other = world.run("r-other-over")
    world.finish(other)
    info = world.open(run, label="same label")
    world.finish(run)
    foreign = world.foreign_pane()
    world.add_agent(foreign, "idle", name="foreign-agent")
    world.reset()
    rc, rows, err = world.reap(other)
    assert (rc, rows) == (0, []) and world.fake.calls == [] and world.controls == 0
    rc, rows, err = world.reap()
    assert rc == 0 and [r["outcome"] for r in rows] == ["closed"], rows
    assert foreign in world.fake.panes and "foreign-agent" in world.fake.agents
    assert world.verbs().count("close_tab") == 1 and info["pane_id"] not in world.fake.panes


def test_the_pane_of_the_process_that_reaps_is_never_closed(world, monkeypatch):
    """Catches: a run-host closed by the process running in it, or a tab that
    holds the reaper's pane closed because only its pane id was compared."""
    run = world.run("r-self")
    host, reviewer = world.open(run, "host"), world.open(run, "reviewer", role="reviewer")
    world.finish(run)
    monkeypatch.setenv("HERDR_PANE_ID", host["pane_id"])
    rc, rows, err = world.reap(run)
    out = {r["pane_id"]: r["outcome"] for r in rows}
    assert out == {host["pane_id"]: "kept", reviewer["pane_id"]: "closed"}, rows
    assert host["pane_id"] in world.fake.panes
    assert [r["event"] for r in world.ledger(run) if r["pane"] == host["pane_id"]] == ["kept"]
    monkeypatch.delenv("HERDR_PANE_ID")

    for how in ("pane", "tab", "both"):
        case = world.run(f"r-self-{how}")
        info = world.open(case, "host")
        stranger = world.foreign_pane(tab=info["tab_id"], pane=f"w1:pS-{how}")
        world.finish(case)
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        monkeypatch.delenv("HERDR_TAB_ID", raising=False)
        if how in ("pane", "both"):
            monkeypatch.setenv("HERDR_PANE_ID", stranger)
        if how in ("tab", "both"):
            monkeypatch.setenv("HERDR_TAB_ID", info["tab_id"])
        world.reset()
        row = world.one(case)
        assert row["outcome"] == "kept", (how, row)
        assert world.closes() == [] and info["pane_id"] in world.fake.panes


def test_an_agent_that_is_not_idle_keeps_the_tab(world):
    """Catches: a tab closed under a working, blocked or unknown agent; `done`
    treated as busy; the process table consulted for an agent's pane."""
    for status in ("working", "blocked", "unknown", ""):
        run = world.run(f"r-agent-{status or 'empty'}")
        info = world.open(run)
        world.add_agent(info["pane_id"], status)
        world.finish(run)
        world.reset()
        row = world.one(run)
        assert row["outcome"] == "kept", (status, row)
        assert world.closes() == [] and world.states(run) == ["owned"]
    for status in ("done", "idle"):
        run = world.run(f"r-agent-{status}")
        info = world.open(run)
        world.add_agent(info["pane_id"], status)
        world.finish(run)
        world.reset()
        assert world.one(run)["outcome"] == "closed", status
        assert world.procs.asked == 0 and world.views == 0


def test_a_pane_that_is_not_where_it_was_opened_stays(world):
    """Catches: an identity check that compares a string prefix, does not resolve
    the path, looks at one of the two directory keys, or ignores the tab."""
    work = str(world.work)
    cases = {
        "both keys elsewhere": {"cwd": "/elsewhere", "foreground_cwd": "/elsewhere"},
        "same-text sibling": {"cwd": work + "-other", "foreground_cwd": work + "-other"},
        "leaves by dotdot": {"cwd": work + "/../elsewhere",
                             "foreground_cwd": work + "/../elsewhere"},
        "only cwd": {"cwd": "/elsewhere"},
        "only foreground_cwd": {"foreground_cwd": "/elsewhere"},
        "relative": {"cwd": "work", "foreground_cwd": "work"},
        "another tab": {"tab_id": "w1:tOther"},
    }
    for name, change in cases.items():
        run = world.run("r-id-" + name.replace(" ", "-"))
        info = world.open(run)
        world.fake.panes[info["pane_id"]].update(change)
        world.finish(run)
        world.reset()
        row = world.one(run)
        assert row["outcome"] == "refused", (name, row)
        assert world.closes() == [] and world.states(run) == ["owned"], name
    run = world.run("r-id-sub")
    info = world.open(run)
    world.fake.panes[info["pane_id"]].update(cwd=work + "/sub", foreground_cwd=work + "/sub")
    world.finish(run)
    assert world.one(run)["outcome"] == "closed"


def test_a_plain_shell_that_is_not_proven_idle_is_kept(world):
    """Catches: a plain shell closed without a proof that nothing runs in it (a
    foreground child, a descendant that left the session, two claimants, no
    claimant, a parent that is no Herdr server, no shell, not asleep, an unreadable
    process table)."""
    def fresh(name):
        run = world.run("r-shell-" + name)
        info = world.open(run)
        world.finish(run)
        return run, info["pane_id"], world.procs.shell_of(info["pane_id"])

    def kept(name, expect, mutate):
        run, pane, shell = fresh(name)
        mutate(pane, shell)
        world.reset()
        row = world.one(run)
        assert row["outcome"] == "kept" and "not proven idle" in row["why"], (name, row)
        assert expect in row["why"], (name, row["why"])
        assert world.closes() == [] and world.states(run) == ["owned"]

    kept("child", "sleep", lambda p, s: world.procs.add(900, s.pid, s.pgrp, s.sid, "S", "sleep"))
    kept("left-session", "make", lambda p, s: (
        world.procs.add(901, s.pid, 901, 901, "S", "sudo"),
        world.procs.add(902, 901, 902, 902, "S", "make")))
    kept("two-claimants", "2 processes", lambda p, s: world.procs.add_shell(p))
    kept("no-claimant", "no process", lambda p, s: world.procs.env.pop(s.pid))
    kept("not-a-server", "no process was found",
         lambda p, s: world.procs.table.__setitem__(
             world.procs.table.index(s),
             procs.Proc(pid=s.pid, ppid=1, pgrp=s.pgrp, sid=s.sid, state="S",
                        start=s.start, comm="bash")))
    kept("not-a-shell", "python3", lambda p, s: world.procs.table.__setitem__(
        world.procs.table.index(s),
        procs.Proc(pid=s.pid, ppid=s.ppid, pgrp=s.pgrp, sid=s.sid, state="S",
                   start=s.start, comm="python3")))
    kept("running", "not asleep", lambda p, s: world.procs.table.__setitem__(
        world.procs.table.index(s),
        procs.Proc(pid=s.pid, ppid=s.ppid, pgrp=s.pgrp, sid=s.sid, state="R",
                   start=s.start, comm="bash")))
    kept("table", "cannot be read", lambda p, s: setattr(
        world.procs, "error", OSError("boom")))
    world.procs.error = None
    run = world.run("r-shell-idle")
    world.open(run)
    world.finish(run)
    world.reset()
    assert world.one(run)["outcome"] == "closed" and world.procs.asked == 1


# --------------------------------------------------------------------------- #
# reap: the proof and the close
# --------------------------------------------------------------------------- #


def test_an_over_runs_owned_tab_is_closed_and_the_ledger_and_registry_say_so(world):
    """Catches: a close not recorded (ledger or registry), a released surface
    walked again, or a ledger line that does not say it came from a surface."""
    run = world.run("r-close")
    info = world.open(run)
    world.finish(run)
    world.reset()
    rc, rows, err = world.reap(run)
    assert rc == 0 and [r["outcome"] for r in rows] == ["closed"], rows
    assert world.verbs().count("close_tab") == 1
    assert ("close_tab", info["tab_id"]) in [c[:2] for c in world.fake.calls]
    ledger = world.ledger(run)
    assert [r["event"] for r in ledger] == ["closing", "closed"]
    assert all(r["role"] == "program:run-host" and r["via"] == "surface"
               and r["pane"] == info["pane_id"] for r in ledger)
    assert world.states(run) == ["released"]
    rc, out, err = world.hoh("status", run)
    assert rc == 0 and json.loads(out)["panes"]["closed"] == 1
    # the same id, given to a foreign pane: a second reap sends nothing
    world.foreign_pane(tab="w1:tNew", pane=info["pane_id"])
    world.reset()
    rc, rows, err = world.reap(run)
    assert (rc, rows) == (0, []) and world.fake.calls == []
    assert info["pane_id"] in world.fake.panes


def test_a_dry_run_changes_nothing(world):
    """Catches: a dry run that closes, releases or writes a ledger line."""
    run = world.run("r-dry")
    info = world.open(run)
    gone = world.run("r-dry-gone")
    gone_info = world.open(gone)
    world.finish(run)
    world.finish(gone)
    world.fake.panes.pop(gone_info["pane_id"])
    before = {r: (world.run_dir(r) / "owned-surfaces.jsonl").read_bytes() for r in (run, gone)}
    world.reset()
    rc, rows, err = world.reap(close=False)
    assert rc == 0
    assert {r["pane_id"]: r["outcome"] for r in rows} == {
        info["pane_id"]: "would_close", gone_info["pane_id"]: "would_release"}
    assert world.closes() == []
    for r in (run, gone):
        assert (world.run_dir(r) / "owned-surfaces.jsonl").read_bytes() == before[r]
        assert not (world.run_dir(r) / pane_ledger.LEDGER).exists()


def test_an_owned_tab_that_holds_a_stranger_pane_stays(world):
    """Catches: a tab closed over a pane the record does not own, or a tab with two
    owned panes closed after only one was examined."""
    run = world.run("r-stranger")
    info = world.open(run)
    stranger = world.foreign_pane(tab=info["tab_id"])
    world.finish(run)
    world.reset()
    rc, rows, err = world.reap(run)
    assert rc == 1 and [r["outcome"] for r in rows] == ["refused"]
    assert stranger in rows[0]["why"]
    assert world.closes() == [] and world.events(run) == ["refused"]
    assert world.states(run) == ["owned"]

    two = world.run("r-two-owned")
    first = world.open(two, "a")
    extra = world.fake.open_surface(cwd=str(world.work), label="b")
    world.fake.panes[extra.pane_id]["tab_id"] = first["tab_id"]
    world.procs.add_shell(extra.pane_id)
    world.registry(two).record_surface(
        ProgramAgentRole.program("run-host"), Surface(extra.pane_id, first["tab_id"]),
        program="p", backend="fake", cwd=str(world.work))
    world.finish(two)
    world.reset()
    rc, rows, err = world.reap(two)
    assert rc == 1 and [r["outcome"] for r in rows] == ["refused", "refused"], rows
    assert all("2 panes" in r["why"] for r in rows)
    assert world.closes() == []


def test_a_tab_row_that_does_not_account_for_its_panes_stays(world, monkeypatch):
    """Catches: a close on a snapshot whose `tabs` row is missing, doubled or
    disagrees with the panes listed."""
    def tweak(fn):
        original = world.fake.snapshot

        def snapshot():
            snap = original()
            fn(snap)
            return snap
        monkeypatch.setattr(world.fake, "snapshot", snapshot)

    cases = {
        "count over panes": lambda snap, tab: [t.update(pane_count=2) for t in snap["tabs"]
                                               if t["tab_id"] == tab],
        "no row": lambda snap, tab: snap.update(tabs=[t for t in snap["tabs"]
                                                      if t["tab_id"] != tab]),
        "two rows": lambda snap, tab: snap["tabs"].extend(
            [dict(t) for t in snap["tabs"] if t["tab_id"] == tab]),
    }
    for name, fn in cases.items():
        run = world.run("r-rows-" + name.replace(" ", "-"))
        info = world.open(run)
        world.finish(run)
        original = world.fake.snapshot
        tweak(lambda snap, fn=fn, tab=info["tab_id"]: fn(snap, tab))
        world.reset()
        row = world.one(run)
        monkeypatch.setattr(world.fake, "snapshot", original)
        assert row["outcome"] == "refused", (name, row)
        assert world.closes() == [], name


def test_an_unreadable_or_empty_snapshot_closes_nothing(world, monkeypatch):
    """Catches: a close on a snapshot that is an error, not an object, lacks a list
    or lists no pane; Herdr unreachable reported as success."""
    run = world.run("r-snap")
    info = world.open(run)
    world.finish(run)

    def with_snapshot(fn):
        monkeypatch.setattr(world.fake, "snapshot", fn)
        world.reset()
        row = world.one(run)
        assert row["outcome"] == "refused", row
        assert world.closes() == [] and world.states(run) == ["owned"]

    def raising():
        raise DispatchError("herdr is busy")

    with_snapshot(raising)
    with_snapshot(lambda: ["not", "an", "object"])
    with_snapshot(lambda: {"panes": [{"pane_id": "x"}], "agents": []})
    with_snapshot(lambda: {"panes": [], "agents": [], "tabs": []})
    rc, rows, err = world.reap(run)
    assert rc == 1
    world.control_error = herdr.HerdrUnavailable("herdr CLI not on the PATH")
    rc, out, err = world.hoh("surface", "reap", run, "--close")
    assert rc == 2 and "herdr CLI not on the PATH" in err
    assert "closed" not in world.events(run) and info["pane_id"] in world.fake.panes


def test_a_pane_that_is_already_gone_is_released_and_never_closed(world):
    """Catches: a close sent to a pane that is gone, or a pane released because a
    snapshot happened not to list it."""
    run = world.run("r-gone")
    info = world.open(run)
    world.finish(run)
    world.fake.panes.pop(info["pane_id"])
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "released" and world.closes() == []
    closed = [r for r in world.ledger(run) if r["event"] == "closed"]
    assert len(closed) == 1 and closed[0]["already_gone"] is True
    assert world.states(run) == ["released"]

    other = world.run("r-gone-other")
    info = world.open(other)
    world.finish(other)
    world.fake.panes.pop(info["pane_id"])
    world.fake.fail["pane_screen"] = DispatchError("herdr timed out")
    world.reset()
    row = world.one(other)
    assert row["outcome"] == "refused" and world.closes() == []
    assert world.states(other) == ["owned"]


def test_a_close_that_does_not_take_is_not_recorded_as_closed(world, monkeypatch):
    """Catches: a close recorded as done because the command was accepted, or
    because a second snapshot omitted the pane while Herdr still answers for it."""
    run = world.run("r-take")
    info = world.open(run)
    world.finish(run)
    real_close_tab = world.fake.close_tab
    monkeypatch.setattr(world.fake, "close_tab",
                        lambda tab, *, purpose="": world.fake._log("close_tab", tab))
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "refused"
    assert world.events(run) == ["closing", "refused"] and world.states(run) == ["owned"]
    monkeypatch.setattr(world.fake, "close_tab", real_close_tab)
    row = world.one(run)
    assert row["outcome"] == "closed"
    assert world.events(run) == ["closing", "refused", "closing", "closed"]
    assert world.states(run) == ["released"]

    other = world.run("r-take-hidden")
    info = world.open(other)
    world.finish(other)
    hidden: set[str] = set()
    original = world.fake.snapshot

    def snapshot():
        snap = original()
        snap["panes"] = [p for p in snap["panes"] if p["pane_id"] not in hidden]
        return snap

    def close_tab(tab, *, purpose=""):
        world.fake._log("close_tab", tab)
        hidden.add(info["pane_id"])

    monkeypatch.setattr(world.fake, "snapshot", snapshot)
    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(other)
    assert row["outcome"] == "refused" and world.states(other) == ["owned"]
    assert world.events(other) == ["closing", "refused"]


def test_a_close_that_cannot_be_recorded_is_not_attempted(world):
    """Catches: a close sent although the `closing` ledger line could not be
    written first."""
    run = world.run("r-ledger")
    world.open(run)
    world.finish(run)
    (world.run_dir(run) / pane_ledger.LEDGER).mkdir()
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "refused" and "ledger" in row["why"]
    assert world.closes() == [] and world.states(run) == ["owned"]


def test_a_registry_that_cannot_be_read_owns_nothing(world, tmp_path):
    """Catches: a reaper that treats an unreadable record as an empty one, or
    reaches Herdr on its account."""
    run = world.run("r-link")
    world.finish(run)
    target = tmp_path / "other-record.jsonl"
    target.write_text("", encoding="utf-8")
    os.symlink(target, world.run_dir(run) / "owned-surfaces.jsonl")
    world.reset()
    rc, rows, err = world.reap(run)
    assert rc == 1 and [r["outcome"] for r in rows] == ["refused"], rows
    assert world.fake.calls == [] and world.controls == 0
    rc, out, err = world.hoh("surface", "list", run)
    assert rc == 0 and "problem" in json.loads(out)["surfaces"][0]


def test_reap_without_a_run_looks_only_at_runs_that_have_a_record(world, monkeypatch):
    """Catches: a reap over the whole root that reads runs with no record, closes a
    run that is not over, or accepts an unsafe run id."""
    over, active = world.run("r-all-over"), world.run("r-all-active")
    a, b = world.open(over), world.open(active)
    world.finish(over)
    plain = world.root / "r-all-corrupt"
    plain.mkdir()
    (plain / "state.json").write_text("{ not json", encoding="utf-8")
    seen = []
    real = surfaces.run_over
    monkeypatch.setattr(surfaces, "run_over", lambda d: (seen.append(Path(d).name), real(d))[1])
    world.reset()
    rc, rows, err = world.reap()
    assert rc == 0, err
    assert {r["pane_id"]: r["outcome"] for r in rows} == {
        a["pane_id"]: "closed", b["pane_id"]: "pending"}
    assert "r-all-corrupt" not in seen
    for sub in ("list", "reap"):
        rc, out, err = world.hoh("surface", sub, "../x")
        assert rc == 2 and out == "", (sub, err)


def test_list_asks_herdr_nothing_and_shows_owned_and_released(world):
    """Catches: a `list` that makes a control, asks the process table, or hides a
    released surface."""
    run = world.run("r-list")
    info = world.open(run)
    world.finish(run)
    world.reset()
    rc, out, err = world.hoh("surface", "list")
    row = json.loads(out)["surfaces"][0]
    assert rc == 0 and row["state"] == "owned" and row["run_over"] is True
    assert world.fake.calls == [] and world.controls == 0 and world.views == 0
    world.reap(run)
    world.reset()
    rc, out, err = world.hoh("surface", "list", run)
    row = json.loads(out)["surfaces"][0]
    assert row["state"] == "released" and row["run_over"] is True
    assert row["pane_id"] == info["pane_id"] and world.fake.calls == []


def test_the_dispatchers_close_own_does_not_close_a_surface(world):
    """Catches: a surface recorded where `close_own` finds it (the dispatcher's
    `owned-agents.jsonl`): it would be closed with the run's role tabs. The
    dispatcher has a registry of its own on that file and a role pane recorded in
    it, so its recall is live: a test that built it without a registry saw nothing
    whatever file held the surface (a vacuous guard)."""
    from hoh.agentcontrol import OwnedAgentRegistry
    from hoh.contracts import Role
    from hoh.dispatchers import HerdrDispatcher

    run = world.run("r-dispatch")
    info = world.open(run)
    run_dir = world.run_dir(run)
    answers = run_dir / "answers"
    answers.mkdir()
    own_file = run_dir / "owned-agents.jsonl"
    registry = OwnedAgentRegistry(own_file, run_id=run_dir.name)
    role_tab = world.fake.open_surface(cwd=str(world.work), label="planner")
    registry.record_surface(ProgramAgentRole.pipeline(Role.PLANNER), role_tab,
                            program="p", backend="fake", cwd=str(world.work))
    dispatcher = HerdrDispatcher(answers_dir=answers, profiles={r: "claude" for r in Role},
                                 cwd=world.work, timeout_ms=1000, control=world.fake,
                                 registry=registry)
    world.reset()
    dispatcher.close_own()
    closes = [c for c in world.fake.calls if c[0] in CLOSE_VERBS]
    # the fake's `close_tab` closes the panes of the tab one by one, so its own pane
    # shows up as a `close_pane` too; nothing else may be closed
    assert [c for c in closes if c[0] == "close_tab"] == [("close_tab", role_tab.tab_id)]
    assert {c[1] for c in closes if c[0] == "close_pane"} <= {role_tab.pane_id}, closes
    assert role_tab.pane_id not in world.fake.panes
    assert info["pane_id"] in world.fake.panes and world.states(run) == ["owned"]
    assert info["pane_id"] not in own_file.read_text(encoding="utf-8")
    assert surfaces.registry_for(run_dir).path.name == "owned-surfaces.jsonl"


def test_reap_outside_herdr_exits_2_and_closes_nothing(world, monkeypatch):
    """Catches: `hoh surface reap` outside Herdr that closes, or records, anything.
    The "outside Herdr" branch of `own_location` is unreachable through the product:
    `make_control` demands `HERDR_ENV=1`, so the command fails closed with exit 2
    before it matters (the real `make_control`, `HERDR_ENV` unset)."""
    run = world.run("r-outside")
    info = world.open(run)
    world.finish(run)
    monkeypatch.setattr(surfaces, "make_control", REAL_MAKE_CONTROL)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    world.reset()
    for close in (False, True):
        rc, rows, err = world.reap(run, close=close)
        assert (rc, rows) == (2, []), (close, rc, rows, err)
        assert "HERDR_ENV" in err
    assert world.fake.calls == [] and info["pane_id"] in world.fake.panes
    assert world.states(run) == ["owned"] and world.ledger(run) == []


def test_the_module_removes_nothing_and_signals_nothing():
    """Catches: any call in `hoh/surfaces.py` that removes, renames, truncates,
    signals or opens a file for writing, or an import of `shutil` / `signal`."""
    forbidden = {"remove", "unlink", "rmdir", "rmtree", "truncate", "rename", "replace",
                 "kill", "killpg", "terminate", "write_text", "write_bytes", "system",
                 "Popen", "open"}
    source = Path(surfaces.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name in forbidden:
                found.append((name, node.lineno))
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names
                      if a.name.split(".")[0] in ("shutil", "signal")]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in (
                "shutil", "signal"):
            found.append((node.module, node.lineno))
    assert found == []
    assert source.isascii()


def test_a_surface_without_a_tab_id_is_closed_as_a_pane(world):
    """Catches: a surface the adapter gave no tab id closed with `close_tab("")`
    or not closed at all."""
    run = world.run("r-pane")
    fake_surface = world.fake.open_surface(cwd=str(world.work), label="x")
    world.procs.add_shell(fake_surface.pane_id)
    registry = world.registry(run)
    registry.record_surface(ProgramAgentRole.program("run-host"),
                            Surface(pane_id=fake_surface.pane_id, tab_id=None),
                            program="p", backend="fake", cwd=str(world.work))
    world.finish(run)
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "closed" and row["tab_id"] == ""
    assert "close_tab" not in world.verbs() and world.verbs().count("close_pane") == 1


def test_a_checkpointed_run_is_not_over_and_its_tabs_stay(world, monkeypatch):
    """Catches: tabs closed at a checkpoint, where the run goes on (CHECKPOINTED ->
    PLANNING) and `surface open` refuses the run a reviewer is opened for."""
    run = world.run("r-check", stage=Stage.CHECKPOINTED)
    info = world.open(run, "reviewer", role="reviewer")
    world.add_agent(info["pane_id"], "idle")
    world.reset()
    rc, rows, err = world.reap(run)
    assert rc == 0 and [r["outcome"] for r in rows] == ["pending"], rows
    assert world.fake.calls == []
    rc, out, err = world.hoh("surface", "list", run)
    assert json.loads(out)["surfaces"][0]["run_over"] is False
    code, marker = run_cmd_run(world, monkeypatch, run, rc=0)
    cap = world.capsys.readouterr()
    assert code == 0 and (cap.out, cap.err) == ("", "")
    assert world.fake.calls == [] and info["pane_id"] in world.fake.panes


def test_each_terminal_fact_of_a_run_closes_its_tabs(world):
    """Catches: a terminal fact of a run that does not end its tabs, or a stage
    that decides alone (a FAILED run at any stage is over)."""
    facts = {
        "ready": (Stage.READY_FOR_DELIVERY, Condition.ACTIVE),
        "failed-planning": (Stage.PLANNING, Condition.FAILED),
        "cancelled-verifying": (Stage.VERIFYING, Condition.CANCELLED),
        "failed-checkpointed": (Stage.CHECKPOINTED, Condition.FAILED),
    }
    for name, (stage, condition) in facts.items():
        run = world.run("r-fact-" + name)
        info = world.open(run)
        world.set_state(run, stage, condition)
        world.reset()
        row = world.one(run)
        assert row["outcome"] == "closed", (name, row)
        assert info["pane_id"] not in world.fake.panes


def test_inside_herdr_with_an_unknown_own_tab_nothing_is_closed(world, monkeypatch):
    """Catches: a reaper inside Herdr that does not know its own tab and closes
    anyway (it could close the tab it runs in)."""
    def fresh(name):
        run = world.run("r-loc-" + name)
        info = world.open(run)
        world.finish(run)
        world.reset()
        return run, info

    monkeypatch.setenv("HERDR_ENV", "1")
    run, info = fresh("env-only")
    row = world.one(run)
    assert row["outcome"] == "refused" and "its own tab is unknown" in row["why"]
    assert world.closes() == []
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p99")
    run, info = fresh("unlisted")
    row = world.one(run)
    assert row["outcome"] == "refused" and "its own tab is unknown" in row["why"]
    assert world.closes() == []
    monkeypatch.setenv("HERDR_PANE_ID", FakeAgentControl.CALLER)
    run, info = fresh("listed")
    assert world.one(run)["outcome"] == "closed"
    monkeypatch.delenv("HERDR_ENV")
    monkeypatch.delenv("HERDR_PANE_ID")
    run, info = fresh("outside")
    assert world.one(run)["outcome"] == "closed"


def test_a_surface_that_stays_is_recorded_once_until_the_reason_changes(world):
    """Catches: a monitor that asks every minute appending a ledger line every
    minute for a surface that stays."""
    run = world.run("r-once")
    info = world.open(run)
    world.add_agent(info["pane_id"], "working")
    world.finish(run)
    for _ in range(5):
        assert world.one(run)["outcome"] == "kept"
    assert world.events(run) == ["kept"]
    world.fake.agents[f"agent-{info['pane_id']}"]["agent_status"] = "blocked"
    for _ in range(5):
        assert world.one(run)["outcome"] == "kept"
    assert world.events(run) == ["kept", "kept"]

    other = world.run("r-once-refused")
    other_info = world.open(other)
    world.foreign_pane(tab=other_info["tab_id"])
    world.finish(other)
    for _ in range(5):
        assert world.one(other)["outcome"] == "refused"
    assert world.events(other) == ["refused"]


def test_the_end_of_run_notes_go_to_stderr_and_stdout_stays_the_runs(world, monkeypatch):
    """Catches: end-of-run notes on stdout, where the run's JSON payload is, or an
    exit code changed by the reap."""
    run = world.run("r-notes")
    kept, refused = world.open(run, "a"), world.open(run, "b")
    world.add_agent(kept["pane_id"], "working")
    world.foreign_pane(tab=refused["tab_id"])
    code, marker = run_cmd_run(world, monkeypatch, run, rc=7, finish=(
        Stage.READY_FOR_DELIVERY, Condition.ACTIVE))
    cap = world.capsys.readouterr()
    assert code == 7 and cap.out == ""
    assert f"surface {kept['pane_id']} stays:" in cap.err
    assert f"surface {refused['pane_id']} stays:" in cap.err


def test_the_end_of_a_run_is_decided_by_stage_and_condition_never_by_blocked_kind(world):
    """Catches: a run that is blocked or paused counted as over because
    `run_finished` says so, or an ACTIVE run with a stale `blocked_kind` held."""
    pending = {
        "check-blocked": (Stage.CHECKPOINTED, Condition.BLOCKED, BLOCKED_KIND),
        "ready-blocked": (Stage.READY_FOR_DELIVERY, Condition.BLOCKED, BLOCKED_KIND),
        "ready-paused": (Stage.READY_FOR_DELIVERY, Condition.PAUSED, None),
        "check-active-stale": (Stage.CHECKPOINTED, Condition.ACTIVE, BLOCKED_KIND),
    }
    closed = {
        "ready-active-stale": (Stage.READY_FOR_DELIVERY, Condition.ACTIVE, BLOCKED_KIND),
        "failed": (Stage.DEVELOPING, Condition.FAILED, BLOCKED_KIND),
        "cancelled": (Stage.PLANNING, Condition.CANCELLED, BLOCKED_KIND),
    }
    for name, (stage, condition, kind) in {**pending, **closed}.items():
        run = world.run("r-end-" + name)
        info = world.open(run)
        world.set_state(run, stage, condition, kind)
        if name in ("check-blocked", "ready-blocked"):
            assert storage_tiers.run_finished(world.run_dir(run))[0] is True, name
        world.reset()
        row = world.one(run)
        want = "pending" if name in pending else "closed"
        assert row["outcome"] == want, (name, row)
        assert (info["pane_id"] in world.fake.panes) == (name in pending), name


# --------------------------------------------------------------------------- #
# the process table
# --------------------------------------------------------------------------- #


def fake_proc(base: Path, entries: list[dict], mounts: str | None = "default") -> surfaces.ProcView:
    """A directory shaped like `/proc` and a `ProcView` over it."""
    base.mkdir()
    for e in entries:
        d = base / str(e["pid"])
        d.mkdir()
        if e.get("stat") is not None:
            text = e["stat"]
        else:
            text = (f"{e['pid']} ({e.get('comm', 'bash')}) {e.get('state', 'S')} "
                    f"{e.get('ppid', 1)} {e.get('pgrp', e['pid'])} {e.get('sid', e['pid'])} "
                    + " ".join(["0"] * 15) + f" {e['pid']} 0 0\n")
        if e.get("stat") != "":
            (d / "stat").write_text(text, encoding="utf-8")
        if e.get("pane"):
            (d / "environ").write_bytes(f"A=1\0HERDR_PANE_ID={e['pane']}\0".encode())
    table = base.parent / (base.name + "-mounts")
    if mounts == "default":
        mounts = f"proc {base} proc rw,nosuid,nodev,noexec,relatime 0 0\n"
    if mounts is not None:
        table.write_text(mounts, encoding="utf-8")
    return surfaces.ProcView(root=str(base), mounts=str(table))


def server(pid=100):
    return {"pid": pid, "ppid": 1, "comm": "herdr"}


def shell(pid=200, pane="w1:p1", **kw):
    return {"pid": pid, "ppid": 100, "comm": "bash", "pane": pane, **kw}


def problem_of(view, pane="w1:p1") -> str:
    return surfaces.shell_problem(surfaces.ProcRead(view, view.snapshot()), pane)


def test_a_child_owned_by_another_user_in_the_shells_session_keeps_the_pane(
        world, tmp_path, monkeypatch):
    """Catches: a process table that keeps only the caller's processes: a root-owned
    `sudo` in the shell's session is invisible and the shell is judged idle."""
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    base = [server(), shell()]
    assert problem_of(fake_proc(tmp_path / "p-idle", base)) == ""
    cases = {
        "sudo child": [{"pid": 300, "ppid": 200, "pgrp": 300, "sid": 200, "comm": "sudo"}],
        "own session": [{"pid": 300, "ppid": 200, "pgrp": 300, "sid": 300, "comm": "sudo"},
                        {"pid": 301, "ppid": 300, "pgrp": 301, "sid": 301, "comm": "make"}],
        "group stray": [{"pid": 300, "ppid": 1, "pgrp": 200, "sid": 300, "comm": "stray"}],
    }
    for name, extra in cases.items():
        view = fake_proc(tmp_path / ("p-" + name.replace(" ", "-")), base + extra)
        problem = problem_of(view)
        assert problem and extra[0]["comm"] in problem, (name, problem)
    # end to end through reap: the pane with the sudo job stays, an idle one closes
    run = world.run("r-sudo")
    busy, idle = world.open(run, "busy"), world.open(run, "idle")
    world.finish(run)
    entries = [server(), shell(200, busy["pane_id"]), shell(210, idle["pane_id"]),
               {"pid": 300, "ppid": 200, "pgrp": 300, "sid": 200, "comm": "sudo"}]
    view = fake_proc(tmp_path / "p-e2e", entries)
    monkeypatch.setattr(surfaces, "make_proc_view", lambda: view)
    rc, rows, err = world.reap(run)
    out = {r["pane_id"]: r for r in rows}
    assert out[busy["pane_id"]]["outcome"] == "kept" and "sudo" in out[busy["pane_id"]]["why"]
    assert out[idle["pane_id"]]["outcome"] == "closed"


def test_a_process_table_that_cannot_be_read_whole_keeps_the_pane(tmp_path):
    """Catches: a process table trusted although a process vanished while it was
    read, a `stat` could not be parsed, `/proc` hides other users' processes, or
    nothing at all was seen."""
    base = [server(), shell()]
    mount = "proc {root} proc rw,nosuid,{opt} 0 0\n"

    def mounts_for(name, opt):
        return mount.format(root=tmp_path / name, opt=opt)

    assert problem_of(fake_proc(tmp_path / "t-whole", base)) == ""
    off = fake_proc(tmp_path / "t-off", base, mounts_for("t-off", "hidepid=off"))
    assert problem_of(off) == ""
    vanished = fake_proc(tmp_path / "t-vanished", base + [{"pid": 999, "stat": ""}])
    assert "vanished" in problem_of_safe(vanished)
    garbled = fake_proc(tmp_path / "t-garbled", base + [{"pid": 999, "stat": "garbage\n"}])
    assert "cannot be parsed" in problem_of_safe(garbled)
    for opt in ("hidepid=2", "hidepid=noaccess"):
        name = "t-" + opt.replace("=", "-")
        view = fake_proc(tmp_path / name, base, mounts_for(name, opt))
        assert "hidepid" in problem_of_safe(view), opt
    no_table = fake_proc(tmp_path / "t-notable", base, None)
    assert "mount table cannot be read" in problem_of_safe(no_table)
    no_proc = fake_proc(tmp_path / "t-noproc", base, "tmpfs /run tmpfs rw 0 0\n")
    assert "no proc mount" in problem_of_safe(no_proc)
    empty = fake_proc(tmp_path / "t-empty", [])
    assert "no process" in problem_of_safe(empty)


def problem_of_safe(view, pane="w1:p1") -> str:
    """`shell_problem` over a table read the way `reap` reads it."""
    try:
        return surfaces.shell_problem(surfaces.ProcRead(view, view.snapshot()), pane)
    except OSError as exc:
        return surfaces.shell_problem(surfaces.ProcRead(view, [], f"OSError: {exc}"), pane)


# --------------------------------------------------------------------------- #
# the end of `hoh run`
# --------------------------------------------------------------------------- #


def test_hoh_run_reaps_the_surfaces_of_its_run_after_the_marker_is_released(
        world, monkeypatch):
    """Catches: a reap inside the process marker (where a run is never over) or
    none at all at the end of `hoh run`; another run's surface touched."""
    run, other = world.run("r-h1"), world.run("r-h1-other")
    info, other_info = world.open(run), world.open(other)
    world.finish(other)
    code, marker = run_cmd_run(world, monkeypatch, run, rc=0, finish=(
        Stage.READY_FOR_DELIVERY, Condition.ACTIVE))
    assert code == 0 and marker and marker[0] is not None, "the stub ran under the marker"
    assert info["pane_id"] not in world.fake.panes
    assert world.events(run) == ["closing", "closed"]
    assert other_info["pane_id"] in world.fake.panes and world.states(other) == ["owned"]


def test_hoh_run_never_closes_for_a_run_that_is_not_over_and_never_changes_its_exit_code(
        world, monkeypatch):
    """Catches: a reap that raises into `hoh run` or changes its exit code; a close
    for a run that is not over."""
    run = world.run("r-h2")
    info = world.open(run)
    code, marker = run_cmd_run(world, monkeypatch, run, rc=3)
    cap = world.capsys.readouterr()
    assert code == 3 and info["pane_id"] in world.fake.panes and cap.err == ""
    world.finish(run)
    world.control_error = herdr.HerdrUnavailable("not inside Herdr")
    code, marker = run_cmd_run(world, monkeypatch, run, rc=5)
    cap = world.capsys.readouterr()
    assert code == 5 and "surfaces: not reaped" in cap.err and cap.out == ""


def test_hoh_run_without_a_surfaces_record_does_nothing_at_all(world, monkeypatch):
    """Catches: a control made, a call to Herdr or a line on stderr for a run that
    never opened a surface."""
    run = world.run("r-h3")
    world.finish(run)
    code, marker = run_cmd_run(world, monkeypatch, run, rc=0)
    cap = world.capsys.readouterr()
    assert code == 0 and (cap.out, cap.err) == ("", "")
    assert world.controls == 0 and world.fake.calls == [] and world.views == 0


# --------------------------------------------------------------------------- #
# the end-of-run note cannot fail the run
# --------------------------------------------------------------------------- #

NOISY = [{"outcome": "kept", "pane_id": "w1:p1", "run_id": "r", "why": "an agent works"},
         {"outcome": "refused", "pane_id": "", "run_id": "r", "why": "unprovable"},
         {"outcome": "closed", "pane_id": "w1:p2", "run_id": "r", "why": "gone"}]


class _NoText(Exception):
    def __str__(self):
        raise RuntimeError("an exception whose text cannot be made")


class _WriteRaises:
    """A stream whose write fails (a pipe whose reader is gone, seen from Python)."""

    encoding = "utf-8"

    def write(self, text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


class _FlushRaises:
    encoding = "utf-8"

    def write(self, text):
        return len(text)

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def _stderr_states(monkeypatch):
    """(label, installer) for every stderr the end of a run must survive. The
    installer returns a cleanup function."""
    def closed():
        stream = io.StringIO()
        stream.close()
        return stream

    def orphan_pipe():
        r, w = os.pipe()
        os.close(r)
        return os.fdopen(w, "w")

    return [("write raises", _WriteRaises), ("flush raises", _FlushRaises),
            ("none", lambda: None), ("closed file", closed), ("orphan pipe", orphan_pipe)]


def test_the_end_of_run_reap_never_raises_and_never_writes_stdout_whatever_stderr_is(
        world, monkeypatch, tmp_path):
    """Catches: a note printed unguarded, so that a stderr that cannot be written
    ends `hoh run` in a traceback; `print(file=None)`, which writes the note to
    stdout where the run's JSON payload is; a closed file (`ValueError`) that
    escapes; an exception whose text cannot be made that escapes."""
    run_dir = tmp_path / "bare-run"
    run_dir.mkdir()
    (run_dir / surfaces.SURFACES_FILE).write_text("", encoding="utf-8")
    cases = {"noisy": lambda *a, **k: [dict(r) for r in NOISY],
             "failing": None, "no text": None}

    def failing(*a, **k):
        raise RuntimeError("boom")

    def no_text(*a, **k):
        raise _NoText()

    cases["failing"], cases["no text"] = failing, no_text
    expect = {"noisy": ["surface w1:p1 stays: an agent works\n",
                        "surface r stays: unprovable\n"],
              "failing": ["surfaces: not reaped (RuntimeError: boom)\n"],
              "no text": ["surfaces: not reaped\n"]}
    real_reap = surfaces.reap
    world.capsys.readouterr()
    for label, make in _stderr_states(monkeypatch):
        for name, reap in cases.items():
            monkeypatch.setattr(surfaces, "reap", reap)
            stream = make()
            monkeypatch.setattr(sys, "stderr", stream)
            try:
                try:
                    surfaces.reap_after_run(run_dir)
                except BaseException as exc:          # noqa: BLE001 - this is the finding
                    pytest.fail(f"reap_after_run raised {type(exc).__name__} "
                                f"({name}, stderr: {label})")
            finally:
                monkeypatch.setattr(sys, "stderr", sys.__stderr__)
                if hasattr(stream, "fileno") and not getattr(stream, "closed", True):
                    try:
                        stream.close()
                    except Exception:                 # noqa: BLE001 - the pipe is broken
                        pass
            assert world.capsys.readouterr().out == "", (name, label)

    # a working stderr gets every note and stdout stays empty: a capture ...
    for name, reap in cases.items():
        monkeypatch.setattr(surfaces, "reap", reap)
        capture = io.StringIO()
        monkeypatch.setattr(sys, "stderr", capture)
        surfaces.reap_after_run(run_dir)
        monkeypatch.setattr(sys, "stderr", sys.__stderr__)
        assert capture.getvalue() == "".join(expect[name]), name
        assert world.capsys.readouterr().out == ""
    # ... and a real descriptor, which the note reaches past the stream's buffer
    for name, reap in cases.items():
        monkeypatch.setattr(surfaces, "reap", reap)
        r, w = os.pipe()
        stream = os.fdopen(w, "w")
        monkeypatch.setattr(sys, "stderr", stream)
        stream.write("earlier\n")                     # what is already written stays first
        surfaces.reap_after_run(run_dir)
        monkeypatch.setattr(sys, "stderr", sys.__stderr__)
        stream.close()
        with os.fdopen(r) as reader:
            assert reader.read() == "earlier\n" + "".join(expect[name]), name
        assert world.capsys.readouterr().out == ""

    # the same through `hoh run`: the run's exit code, stdout the run's own
    monkeypatch.setattr(surfaces, "reap", real_reap)
    for label, make in _stderr_states(monkeypatch):
        for how in ("stays", "not reaped"):
            case = world.run(f"r-b1-{label.replace(' ', '-')}-{how.replace(' ', '-')}")
            info = world.open(case)
            world.add_agent(info["pane_id"], "working")
            if how == "not reaped":
                world.control_error = herdr.HerdrUnavailable("not inside Herdr")
            stream = make()
            monkeypatch.setattr(sys, "stderr", stream)
            try:
                code, _marker = run_cmd_run(world, monkeypatch, case, rc=7, finish=(
                    Stage.READY_FOR_DELIVERY, Condition.ACTIVE))
            finally:
                monkeypatch.setattr(sys, "stderr", sys.__stderr__)
                world.control_error = None
                if hasattr(stream, "fileno") and not getattr(stream, "closed", True):
                    try:
                        stream.close()
                    except Exception:                 # noqa: BLE001 - the pipe is broken
                        pass
            assert code == 7, (label, how)
            assert world.capsys.readouterr().out == "", (label, how)


CHILD = """\
import os, sys
from hoh import surfaces

mode, run_dir, report = sys.argv[1:4]


def failing(*a, **k):
    raise RuntimeError("boom")


def noisy(*a, **k):
    return [{"outcome": "kept", "pane_id": "w1:p1", "run_id": "r", "why": "an agent works"},
            {"outcome": "refused", "pane_id": "w1:p2", "run_id": "r", "why": "unprovable"}]


surfaces.reap = failing if mode == "failing" else noisy
if sys.argv[4] == "none":
    sys.stderr = None
with open(report, "w") as fh:
    fh.write(repr(sys.stderr is None))
surfaces.reap_after_run(run_dir)
sys.exit(7)
"""


def test_the_exit_code_of_a_process_survives_a_stderr_that_cannot_be_written(
        world, tmp_path):
    """Catches: a note left in the buffer of `sys.stderr` after a failed write: the
    interpreter flushes it once more at exit, the flush fails on the pipe whose
    reader is gone, and the process exits 120 in place of the run's code. Real
    child processes: an in-process stand-in cannot see the exit-time flush."""
    script = tmp_path / "child-reap.py"
    script.write_text(CHILD, encoding="utf-8")
    run_dir = tmp_path / "child-run"
    run_dir.mkdir()
    (run_dir / surfaces.SURFACES_FILE).write_text("", encoding="utf-8")
    src = str(Path(surfaces.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src}

    def child(mode, how, stderr_arg, report):
        argv = [sys.executable, "-B", str(script), mode, str(run_dir), str(report), how]
        if how == "closed":
            argv = ["/bin/sh", "-c", 'exec "$@" 2>&-', "sh", *argv]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stderr_arg, env=env, cwd=str(tmp_path))
        out, err = proc.communicate(timeout=120)
        return proc.returncode, out.decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")

    for mode in ("failing", "noisy"):
        for how in ("orphan pipe", "closed", "none"):
            report = tmp_path / f"report-{mode}-{how.replace(' ', '-')}.txt"
            if how == "orphan pipe":
                r, w = os.pipe()
                os.close(r)
                try:
                    code, out, _err = child(mode, how, w, report)
                finally:
                    os.close(w)
            else:
                code, out, _err = child(mode, how, subprocess.DEVNULL, report)
            assert (code, out) == (7, ""), (mode, how, code, out)
            if how != "orphan pipe":
                assert report.read_text(encoding="utf-8") == "True", (mode, how)

    # control: a readable pipe gets the note, and the exit code is the run's
    code, out, err = child("failing", "pipe", subprocess.PIPE, tmp_path / "report-ctl.txt")
    assert (code, out) == (7, "")
    assert err == "surfaces: not reaped (RuntimeError: boom)\n"
    code, out, err = child("noisy", "pipe", subprocess.PIPE, tmp_path / "report-ctl2.txt")
    assert (code, out) == (7, "")
    assert err == ("surface w1:p1 stays: an agent works\n"
                   "surface w1:p2 stays: unprovable\n")


# --------------------------------------------------------------------------- #
# two reapers of one run
# --------------------------------------------------------------------------- #


def _race_world(world, name):
    run = world.run(name)
    info = world.open(run)
    world.finish(run)
    world.reset()
    return run, info, world.run_dir(run)


def _summary(world, run):
    return pane_ledger.summary(world.run_dir(run))


def test_a_reaper_that_loses_a_race_records_the_close_not_a_refusal(
        world, monkeypatch, tmp_path):
    """Catches: a loser that appends `refused` after the winner's `closed` (`hoh
    status` then lists a closed pane as refused); a second `closed` for one pane; a
    `closing` left without its `closed`; a failed close that is still there read as a
    lost race; a record that cannot be read now reported as "released"."""
    # 1. a nested reap (the winner) runs inside the loser's close; the loser's fails
    run, info, run_dir = _race_world(world, "r-race-nested")
    real_close_tab = world.fake.close_tab
    depth = []

    def close_tab(tab, *, purpose=""):
        if not depth:
            depth.append(1)
            surfaces.reap(run_dir, close=True)        # the winner
            world.fake._log("close_tab", tab)
            raise DispatchError("tab_not_found")
        real_close_tab(tab, purpose=purpose)

    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(run)
    assert row["outcome"] == "released", row
    assert world.events(run) == ["closing", "closing", "closed"]
    summary = _summary(world, run)
    assert summary["refused"] == [] and summary["unconfirmed"] == []
    assert summary["closed"] == 1
    assert world.states(run) == ["released"] and info["pane_id"] not in world.fake.panes

    # 2. the winner's ledger line has not landed: the pane vanished, the record is released
    run, info, run_dir = _race_world(world, "r-race-unlanded")

    def close_tab(tab, *, purpose=""):
        real_close_tab(tab, purpose=purpose)
        world.registry(run).release(info["pane_id"], reason="closed")
        raise DispatchError("tab_not_found")

    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(run)
    assert row["outcome"] == "released", row
    assert world.events(run) == ["closing", "closed"]
    assert world.ledger(run)[-1]["already_gone"] is True
    summary = _summary(world, run)
    assert summary["refused"] == [] and summary["unconfirmed"] == [] and summary["closed"] == 1
    assert world.states(run) == ["released"]

    # 3. a close that fails while the pane is still there is a refusal, as before
    run, info, run_dir = _race_world(world, "r-race-real")

    def close_tab(tab, *, purpose=""):
        world.fake._log("close_tab", tab)
        raise DispatchError("herdr exit 1: boom")

    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(run)
    assert row["outcome"] == "refused" and "the close failed" in row["why"], row
    assert world.events(run) == ["closing", "refused"] and world.states(run) == ["owned"]
    assert info["pane_id"] in world.fake.panes

    # 3b. gone by the snapshot alone (Herdr does not say pane_not_found): still a refusal
    run, info, run_dir = _race_world(world, "r-race-unproven")
    hidden: set[str] = set()
    original = world.fake.snapshot

    def snapshot():
        snap = original()
        snap["panes"] = [p for p in snap["panes"] if p["pane_id"] not in hidden]
        return snap

    def close_tab(tab, *, purpose=""):
        world.fake._log("close_tab", tab)
        hidden.add(info["pane_id"])
        raise DispatchError("herdr exit 1: boom")

    monkeypatch.setattr(world.fake, "snapshot", snapshot)
    monkeypatch.setattr(world.fake, "close_tab", close_tab)
    row = world.one(run)
    assert row["outcome"] == "refused", row
    assert world.events(run) == ["closing", "refused"] and world.states(run) == ["owned"]
    monkeypatch.setattr(world.fake, "snapshot", original)
    monkeypatch.setattr(world.fake, "close_tab", real_close_tab)

    # 4. the winner finishes while the loser reads the process table
    run, info, run_dir = _race_world(world, "r-race-proctable")
    real_snapshot, fired = world.procs.snapshot, []

    def proc_snapshot():
        if not fired:
            fired.append(1)
            surfaces.reap(run_dir, close=True)        # the winner
        return real_snapshot()

    monkeypatch.setattr(world.procs, "snapshot", proc_snapshot)
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "released" and "another reaper" in row["why"], row
    assert world.events(run) == ["closing", "closed"]
    assert world.closes().count("close_tab") == 1, "the loser asked the runtime nothing"
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []
    monkeypatch.setattr(world.procs, "snapshot", real_snapshot)

    # 5. the winner finishes before the loser's snapshot
    run, info, run_dir = _race_world(world, "r-race-snapshot")
    original, fired = world.fake.snapshot, []

    def snapshot():
        if not fired:
            fired.append(1)
            surfaces.reap(run_dir, close=True)        # the winner
        return original()

    monkeypatch.setattr(world.fake, "snapshot", snapshot)
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "released", row
    assert world.events(run) == ["closing", "closed"]
    assert world.closes().count("close_tab") == 1
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []
    assert world.states(run) == ["released"]
    monkeypatch.setattr(world.fake, "snapshot", original)

    # 6. the winner finishes after the loser's look and before the loser takes the ledger
    # gate: under the gate the loser looks again, sees the winner's `closed` and writes
    # nothing (the check of the `closing` line and the line itself are one step)
    run, info, run_dir = _race_world(world, "r-race-window")
    real_gate, fired = pane_ledger.gate, []

    def gate(run_dir_):
        if not fired:
            fired.append(1)
            surfaces.reap(run_dir, close=True)        # the winner, in full
        return real_gate(run_dir_)

    monkeypatch.setattr(pane_ledger, "gate", gate)
    row = world.one(run)
    monkeypatch.setattr(pane_ledger, "gate", real_gate)
    assert row["outcome"] == "released", row
    assert world.events(run) == ["closing", "closed"]
    summary = _summary(world, run)
    assert summary["closed"] == 1 and summary["unconfirmed"] == [] and summary["refused"] == []
    assert world.closes().count("close_tab") == 1
    assert world.states(run) == ["released"]

    # 7. an older `closed` line of the same pane id (Herdr gave the id out again)
    run, info, run_dir = _race_world(world, "r-race-older")
    old = {"at": "2020-01-01T00:00:00Z", "event": "closed", "pane": info["pane_id"],
           "tab": info["tab_id"], "role": "program:run-host", "via": "surface",
           "reason": "run over"}
    durable_append(run_dir / pane_ledger.LEDGER, json.dumps(old, sort_keys=True) + "\n")
    row = world.one(run)
    assert row["outcome"] == "closed", row
    assert world.events(run) == ["closed", "closing", "closed"]
    assert world.states(run) == ["released"]

    # 8. a record that becomes unreadable while the process table is read is not "released"
    run, info, run_dir = _race_world(world, "r-race-unreadable")
    real_snapshot, fired = world.procs.snapshot, []

    def proc_snapshot():
        if not fired:
            fired.append(1)
            os.link(run_dir / surfaces.SURFACES_FILE, tmp_path / "second-name-of-the-record")
        return real_snapshot()

    monkeypatch.setattr(world.procs, "snapshot", proc_snapshot)
    world.reset()
    row = world.one(run)
    assert row["outcome"] == "refused" and "cannot be read" in row["why"], row
    assert world.closes() == [] and info["pane_id"] in world.fake.panes
    assert "closing" not in world.events(run)
    monkeypatch.setattr(world.procs, "snapshot", real_snapshot)


# --------------------------------------------------------------------------- #
# the transport
# --------------------------------------------------------------------------- #


def test_the_transport_returns_json_or_text_and_carries_herdrs_own_error(monkeypatch):
    """Catches: a transport that loses Herdr's own error text (`pane_not_found`),
    reads text as JSON, or treats a timeout as final."""
    calls = []

    def runner(result):
        def run(argv, **kw):
            calls.append((argv, kw))
            if isinstance(result, BaseException):
                raise result
            return result
        return run

    def done(code=0, out="", err=""):
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(subprocess, "run", runner(done(out='{"result": {"a": 1}}')))
    assert surfaces.herdr_transport(["herdr", "x"], "p") == {"result": {"a": 1}}
    argv, kw = calls[-1]
    assert argv == ["herdr", "x"] and kw["timeout"] == 30 and not kw.get("shell")
    monkeypatch.setattr(subprocess, "run", runner(done(out="plain text\n")))
    assert surfaces.herdr_transport(["herdr", "x"], "p", text=True) == "plain text\n"
    monkeypatch.setattr(subprocess, "run", runner(done(1, err="error: pane_not_found w1:p9")))
    with pytest.raises(DispatchError) as exc:
        surfaces.herdr_transport(["herdr", "pane", "read", "w1:p9"], "read")
    assert "pane_not_found" in str(exc.value) and exc.value.transient is False
    monkeypatch.setattr(subprocess, "run", runner(done(out="not json")))
    with pytest.raises(DispatchError, match="no JSON"):
        surfaces.herdr_transport(["herdr", "x"], "p")
    monkeypatch.setattr(subprocess, "run",
                        runner(subprocess.TimeoutExpired(["herdr"], 30)))
    with pytest.raises(DispatchError) as exc:
        surfaces.herdr_transport(["herdr", "x"], "p")
    assert exc.value.transient is True
    monkeypatch.setattr(subprocess, "run", runner(FileNotFoundError("no herdr")))
    with pytest.raises(DispatchError) as exc:
        surfaces.herdr_transport(["herdr", "x"], "p")
    assert exc.value.transient is True
