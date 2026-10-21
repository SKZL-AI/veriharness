"""TR1 (strand tabreap): the surface book -- ownership by record written at birth, "finished" by a
lifecycle fact (never an agent's status, never a name), `hoh reap` (a dry run unless `--apply`),
`hoh surface register | release`, `hoh worktree` recording its workspaces, and the end of `hoh run`.

Every test runs with a stub `herdr` first on `PATH` that logs its argv and fails, with the
`hoh.herdr` functions replaced by something that fails the test, with `subprocess.run` allowed for
`git` only, and with the stub's log asserted empty at the end (autouse). The runtime is a
`FakeAgentControl` (tests/test_agent_control.py) with workspaces behind the `surfaces.make_control`
seam; the process table is a fake; the registry is the injected one of tests/test_storage_registry.py.
No test starts Herdr or an agent, reads the real `/proc` for a pane or writes outside `tmp_path`.
Each test names the failure it catches and the mutants that must die on it.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hoh import agentcontrol, cli, herdr, pane_ledger, procs, surfaces
from hoh.contracts import Condition, RunState, Stage
from hoh.controller import DispatchError
from hoh.storage_registry import OWNED
from hoh.store import RunStore

from test_agent_control import FakeAgentControl
from test_storage_registry import (BOOT, git, isolated_env,  # noqa: F401  (autouse fixture)
                                   make_repo, new_registry)
from test_storage_registry import fake as _registry_fake

fake = _registry_fake            # the fixture of test_storage_registry, by name (the tests below take it as `fake`)

try:
    from hoh import surfacebook
except ImportError:                                      # the pin before this increment
    surfacebook = None

REAL_RUN = subprocess.run
REAL_MAKE_CONTROL = surfaces.make_control
SRC = Path(surfaces.__file__).resolve().parent

#: Set by the isolation fixture while it is in force. A `World` (the only door to a control and a
#: registry) refuses to exist without it: a module imported without its fixtures must fail closed.
ISOLATION = {"in_force": False}
ACTING = {"close_tab", "close_pane", "close_workspace"}


def need_the_module() -> None:
    if surfacebook is None:
        pytest.fail("hoh.surfacebook does not exist: the surface book is not built")


def need_isolation() -> None:
    if not ISOLATION["in_force"]:
        pytest.fail("the isolation fixture is not in force: refusing to build a world that could "
                    "reach the real Herdr")


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    """No test reaches Herdr: a stub `herdr` first on PATH logs and fails, the `hoh.herdr`
    functions fail the test, `subprocess.run` runs `git` and nothing else, and the stub's log must
    be empty at the end."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "stub-herdr.log"
    stub = bin_dir / "herdr"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
                "HOH_STATE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    def refuse(*a, **k):
        pytest.fail(f"a test reached the outside world: {a!r} {k!r}")

    def only_git(argv, *a, **k):
        if list(argv)[0] != "git":
            refuse(argv)
        return REAL_RUN(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", only_git)
    for name in ("_cli", "snapshot", "agents", "find_agent", "liveness", "agent_status",
                 "read_agent", "worktree_create", "worktree_list"):
        monkeypatch.setattr(herdr, name, refuse)
    ISOLATION["in_force"] = True
    yield
    ISOLATION["in_force"] = False
    assert not log.exists() or log.read_text(encoding="utf-8") == "", "the stub herdr was called"


class FakeHerdr(FakeAgentControl):
    """The fake runtime with several workspaces: panes (a plain shell or an agent) are made with
    `add_pane`; `close_workspace` closes a workspace and, when `cascade` says so, the workspaces
    linked to it, and answers `workspace_not_found` for one that is not there."""

    def __init__(self) -> None:
        super().__init__()
        self.cascade: dict[str, list[str]] = {}
        self.snapshot_error: Exception | None = None
        self.snapshots = 0

    def add_pane(self, ws, cwd, *, agent=None, status="idle", tab=None) -> str:
        self._n += 1
        pane, tab = f"{ws}:p{self._n}", tab or f"{ws}:t{self._n}"
        self.panes[pane] = {"pane_id": pane, "tab_id": tab, "workspace_id": ws, "cwd": str(cwd),
                            "foreground_cwd": str(cwd)}
        if agent:
            self.agents[agent] = {"name": agent, "pane_id": pane, "tab_id": tab, "agent": "claude",
                                  "agent_status": status, "cwd": str(cwd),
                                  "foreground_cwd": str(cwd), "workspace_id": ws}
        return pane

    def snapshot(self):
        self.snapshots += 1
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return super().snapshot()

    def close_tab(self, tab_id, *, purpose=""):
        self._log("close_tab", tab_id, purpose=purpose)
        for pane in [p for p, r in self.panes.items() if r["tab_id"] == tab_id]:
            self._drop(pane)

    def _drop(self, pane) -> None:
        self.panes.pop(pane, None)
        for name in [n for n, a in self.agents.items() if a["pane_id"] == pane]:
            self.agents.pop(name)

    def close_workspace(self, workspace_id, *, purpose=""):
        self._log("close_workspace", workspace_id, purpose=purpose)
        if not any(p["workspace_id"] == workspace_id for p in self.panes.values()):
            raise DispatchError("workspace_not_found")
        for ws in [workspace_id, *self.cascade.get(workspace_id, [])]:
            for pane in [p for p, r in self.panes.items() if r["workspace_id"] == ws]:
                self._drop(pane)


class Procs:
    """The process table: a Herdr server and the plain shells below it (as tests/test_surfaces)."""

    SERVER = 100

    def __init__(self) -> None:
        self.table = [procs.Proc(pid=self.SERVER, ppid=1, pgrp=self.SERVER, sid=self.SERVER,
                                 state="S", start=self.SERVER, comm="herdr")]
        self.env: dict[int, str] = {}
        self._next = 200

    def add_shell(self, pane, *, state="S") -> procs.Proc:
        pid, self._next = self._next, self._next + 10
        proc = procs.Proc(pid=pid, ppid=self.SERVER, pgrp=pid, sid=pid, state=state, start=pid,
                          comm="bash")
        self.table.append(proc)
        self.env[pid] = pane
        return proc

    def set_state(self, pane, state) -> None:
        for i, p in enumerate(self.table):
            if self.env.get(p.pid) == pane:
                self.table[i] = procs.Proc(pid=p.pid, ppid=p.ppid, pgrp=p.pgrp, sid=p.sid,
                                           state=state, start=p.start, comm=p.comm)

    def snapshot(self):
        return list(self.table)

    def pane_of(self, pid):
        return self.env.get(pid)


class World:
    """An initialised registry with project `p` (a repository whose branch `feat` is not merged),
    one fake runtime, one fake process table, and `hoh`."""

    def __init__(self, tmp_path: Path, fake, monkeypatch, capsys) -> None:
        need_the_module()
        need_isolation()
        self.tmp_path, self.capsys, self.monkeypatch = tmp_path, capsys, monkeypatch
        assert surfaces.make_control is REAL_MAKE_CONTROL       # replaced below, never left real
        fake.procs[os.getpid()] = 4242
        self.reg = new_registry(tmp_path, fake)
        self.reg.init()
        self.repo = make_repo(tmp_path / "proj")
        self.main = git(self.repo, "branch", "--show-current")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.cands: dict[str, str] = {}
        for name in ("feat", "other"):
            git(self.repo, "checkout", "-q", "-b", name, self.base)
            (self.repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", name)
            self.cands[name] = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "checkout", "-q", self.main)
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "later")
        self.reg.register_project("p", self.repo)
        self.fake = FakeHerdr()
        self.procs = Procs()
        self.controls = 0
        self.root = tmp_path / "runs"                           # a runs root
        self.root.mkdir()
        self.dirs = tmp_path / "dirs"
        self.dirs.mkdir()
        self.roots_seen: list[str] = []
        monkeypatch.setattr(surfacebook, "open_registry",
                            lambda root: (self.roots_seen.append(str(root)), self.reg)[1])
        monkeypatch.setattr(surfaces, "make_control", self._make_control)
        monkeypatch.setattr(surfaces, "make_proc_view", lambda: self.procs)
        monkeypatch.setenv("HOH_STATE_ROOT", str(self.reg.state_root))

    def _make_control(self):
        self.controls += 1
        return self.fake

    # -- surfaces ------------------------------------------------------------ #
    def agent(self, name, *, ws="w7", status="idle") -> str:
        """A tab with an agent in its own directory below `tmp_path`; the pane id."""
        folder = self.dirs / name
        folder.mkdir(exist_ok=True)
        return self.fake.add_pane(ws, folder, agent=name, status=status)

    def hoh(self, *argv):
        rc = cli.main(["--root", str(self.root), *argv])
        cap = self.capsys.readouterr()
        return rc, cap.out, cap.err

    def register(self, pane, kind="reviewer", strand="s1", perspective="a", **change):
        argv = ["surface", "register", "--project", "p", "--kind", kind, "--pane", pane]
        if kind != "orchestrator":
            argv += ["--strand", strand, "--perspective", perspective]
        for key, value in change.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return self.hoh(*argv)

    def reviewer(self, strand="s1", perspective="a", *, kind="reviewer", status="idle",
                 candidate="", name=None) -> str:
        pane = self.agent(name or f"vh-{strand}-{kind[:3]}-{perspective}", status=status)
        extra = {"candidate": candidate} if candidate else {}
        rc, out, err = self.register(pane, kind, strand, perspective, **extra)
        assert rc == 0, err
        return pane

    def sweep(self, *extra, apply=False):
        rc, out, err = self.hoh("reap", "--project", "p", *extra, *(["--apply"] if apply else []))
        return rc, (json.loads(out) if out.strip() else None), err

    def rows(self, *extra, apply=False) -> dict[str, dict]:
        rc, doc, err = self.sweep(*extra, apply=apply)
        assert doc is not None, err
        return {r["pane_id"]: r for r in doc["surfaces"]}

    # -- the book ------------------------------------------------------------ #
    @property
    def book(self) -> Path:
        return self.reg.state_root / "projects" / "p" / "receipts" / "surfaces"

    def ledger(self, home=None) -> list[dict]:
        path = Path(home or self.book) / pane_ledger.LEDGER
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def lines(self, home=None) -> list[dict]:
        path = Path(home or self.book) / surfaces.SURFACES_FILE
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def books(self, home=None) -> dict[str, dict]:
        return surfacebook.BookRegistry(Path(home or self.book)).books()[0]

    def owned_panes(self, home=None) -> set[str]:
        return {a.pane_id for a in surfaces.registry_for(Path(home or self.book)).owned()}

    def verbs(self) -> list[str]:
        return self.fake.verbs()

    def acted(self) -> list[tuple]:
        return [c for c in self.fake.calls if c[0] in ACTING]

    def reset(self) -> None:
        self.fake.calls.clear()
        self.controls = 0

    def merge(self, branch="feat") -> str:
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
        return git(self.repo, "rev-parse", "HEAD")

    # -- runs roots and workspaces ------------------------------------------- #
    def run_dir(self, name, state="ended", root=None, *, stage=Stage.READY_FOR_DELIVERY,
                condition=Condition.ACTIVE, state_text=None) -> Path:
        """A run directory: a real `state.json` (by default a run that is over: ready for delivery
        and active -- `surfaces.run_over`'s own words) and, unless `state` is None, a process
        marker. `state_text` replaces the state file's text."""
        folder = Path(root or self.root) / name
        folder.mkdir(exist_ok=True)
        run_state = RunState(run_id=name, repo_path=str(self.dirs), project_name="p",
                             spec_path=str(self.dirs / "spec.md"), spec_digest="d",
                             policy_digest="d", profile_digest="d", stage=stage,
                             condition=condition)
        (folder / "state.json").write_text(
            run_state.model_dump_json() if state_text is None else state_text, encoding="utf-8")
        if state is not None:
            (folder / "process.json").write_text(
                json.dumps({"pid": 1, "boot_id": "b", "since": "s", "state": state}) + "\n",
                encoding="utf-8")
        return folder

    def workspace(self, label, *, root=None, shell=True) -> tuple[str, str]:
        """A workspace the way `hoh worktree` leaves it: one tab, one plain shell, recorded in
        the runs root. Returns (workspace id, pane id)."""
        ws = f"w{label}"
        folder = self.dirs / f"wt-{label}"
        folder.mkdir()
        before = self.fake.snapshot()
        pane = self.fake.add_pane(ws, folder)
        if shell:
            self.procs.add_shell(pane)
        after = self.fake.snapshot()
        records, notes = surfacebook.register_workspaces(
            root=root or self.root, repo=str(self.dirs), worktree=str(folder), before=before,
            after=after, backend=self.fake.backend)
        assert [r["pane_id"] for r in records] == [pane] and notes == [], (records, notes)
        return ws, pane

    def pair(self, label, *, root=None, linked_panes=1) -> tuple[tuple[str, str], tuple[str, str]]:
        """What one `hoh worktree` call leaves: the target's workspace and the linked workspace of
        the worktree, recorded together as one group (the call's two snapshots), the target's close
        taking the linked workspace with it. `linked_panes=2` makes the linked workspace one the
        record declines (two panes). Returns ((target ws, pane), (linked ws, pane))."""
        folder_t, folder_d = self.dirs / f"tgt-{label}", self.dirs / f"dev-{label}"
        folder_t.mkdir()
        folder_d.mkdir()
        tws, dws = f"wa{label}", f"wb{label}"       # the target sorts first: closed first
        before = self.fake.snapshot()
        tpane = self.fake.add_pane(tws, folder_t)
        dpane = self.fake.add_pane(dws, folder_d)
        for _ in range(linked_panes - 1):
            self.fake.add_pane(dws, folder_d)
        self.procs.add_shell(tpane)
        self.procs.add_shell(dpane)
        after = self.fake.snapshot()
        records, notes = surfacebook.register_workspaces(
            root=root or self.root, repo=str(folder_t), worktree=str(folder_d), before=before,
            after=after, backend=self.fake.backend)
        want = {tpane} if linked_panes != 1 else {tpane, dpane}
        assert {r["pane_id"] for r in records} == want, (records, notes)
        self.fake.cascade[tws] = [dws]
        return (tws, tpane), (dws, dpane)

    def tree_files(self) -> dict[str, str]:
        out = {}
        for base in (self.reg.state_root, self.root):
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    path = Path(dirpath) / name
                    out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out


@pytest.fixture
def world(tmp_path, fake, monkeypatch, capsys):
    return World(tmp_path, fake, monkeypatch, capsys)


# --------------------------------------------------------------------------- #
# born registered
# --------------------------------------------------------------------------- #


def test_a_surface_is_born_registered_with_its_kind_strand_perspective_round_and_identity(world):
    """Catches: a surface that is closable without a record of what it is for (kind, strand,
    perspective, round), a record outside the registry's owned space, a lease left open, a record
    the unchanged reaper cannot read. Mutants: the `book` object not written; the book created
    outside the registered session; `close_session` dropped."""
    a = world.reviewer("s1", "a", candidate=world.cands["feat"])
    b = world.reviewer("s1", "b", candidate=world.cands["feat"])
    d = world.reviewer("s1", "w", kind="drafter")
    orch = world.agent("vh-orchestrator-1")
    world.monkeypatch.setenv("HERDR_PANE_ID", orch)
    rc, out, err = world.hoh("surface", "register", "--project", "p", "--kind", "orchestrator")
    assert rc == 0, err                                   # the pane defaults to $HERDR_PANE_ID
    info = json.loads(out)
    assert (info["pane_id"], info["kind"], info["agent"]) == (orch, "orchestrator",
                                                              "vh-orchestrator-1")
    owner = world.reg.owner_of(world.book)            # the registered scratch directory itself
    assert (owner.status, owner.project_id, owner.kind, owner.session_id) == (
        "PROTECTED", "p", "receipts", "surfaces"), owner
    inside = world.reg.owner_of(world.book / surfaces.SURFACES_FILE)   # and what is inside it
    assert (inside.status, inside.project_id, inside.kind) == (OWNED, "p", "receipts"), inside
    sessions = world.reg.state_root / "projects" / "p" / "sessions" / "surfaces"
    assert not (sessions / "LEASE.json").exists(), "the session lease was left open"
    lines = world.lines()
    assert [ln["event"] for ln in lines] == ["adopt"] * 4
    by_pane = {ln["pane_id"]: ln for ln in lines}
    first = by_pane[a]
    assert first["book"] == {"kind": "reviewer", "strand": "s1", "perspective": "a", "round": 1,
                             "candidate": world.cands["feat"],
                             "into": f"refs/heads/{world.main}", "review": "",
                             "workspace_id": "", "runs_root": ""}
    assert first["name"] == "vh-s1-rev-a" and first["cwd"] == str(world.dirs / "vh-s1-rev-a")
    assert first["tab_id"] == world.fake.panes[a]["tab_id"] and first["role"] == "program:reviewer"
    assert by_pane[b]["book"]["perspective"] == "b"
    assert by_pane[d]["book"]["kind"] == "drafter" and by_pane[d]["role"] == "program:drafter"
    assert by_pane[orch]["book"]["strand"] == "" and by_pane[orch]["role"] == "program:orchestrator"
    assert world.owned_panes() == {a, b, d, orch}          # the unchanged I10a reader sees them
    assert world.verbs().count("open_surface") == 0 and world.acted() == []
    c_pane = world.agent("vh-s1-rev-c")
    rc, out, err = world.register(c_pane, "reviewer", "s1", "c", round=3)
    assert rc == 0 and json.loads(out)["round"] == 3, err
    rc, out, err = world.register(a, "reviewer", "s1", "a", round=2)       # the next round of `a`
    assert rc == 0 and json.loads(out)["round"] == 2, err
    assert json.loads(out)["candidate"] == world.cands["feat"]              # the candidate is kept
    assert world.owned_panes() == {a, b, d, orch, c_pane}
    assert world.books()[a]["round"] == 2 and world.books()[b]["round"] == 1
    longest = world.register(world.agent("vh-long"), "reviewer", "s" * 32, "abcdef", round=999)
    assert longest[0] == 0 and json.loads(longest[1])["round"] == 999, longest   # the bounds, admitted


def test_a_registration_that_cannot_be_proven_is_refused_with_nothing_written(world):
    """Catches: a surface recorded that Herdr does not show, that has no agent, whose directory
    cannot identify it, with a strand id or a candidate that is not exactly what it must be, or a
    second live reviewer for a strand and perspective that has one. Mutants: the snapshot not
    consulted; the agent check dropped; the breadth check dropped; `fullmatch` replaced by `match`;
    the candidate's ancestry check dropped; the live-slot check dropped."""
    shell = world.fake.add_pane("w7", world.dirs)             # a pane with no agent, in a broad dir
    ok = world.agent("vh-s1-rev-a")
    two = world.agent("vh-two-a")
    world.fake.agents["vh-two-b"] = dict(world.fake.agents["vh-two-a"], name="vh-two-b")
    broad = world.fake.add_pane("w7", world.tmp_path, agent="vh-broad-a")   # tmp_path is HOME
    shared = world.agent("vh-shared-a")
    world.fake.add_pane("w7", world.dirs, tab=world.fake.panes[shared]["tab_id"])  # a second pane
    before = world.tree_files()
    world.monkeypatch.setenv("HERDR_PANE_ID", ok)   # the caller's own pane: the default of --pane

    def without_pane(kind):
        return world.hoh("surface", "register", "--project", "p", "--kind", kind, "--strand", "s1",
                         "--perspective", "a")

    bad = {
        "reviewer without --pane": without_pane("reviewer"),
        "drafter without --pane": without_pane("drafter"),
        "unknown pane": world.register("w7:p999"),
        "no agent": world.register(shell),
        "two agents": world.register(two),
        "broad directory": world.register(broad),
        "tab shared with another pane": world.register(shared),
        "kind": world.register(ok, kind="role"),
        "kind spelling": world.register(ok, kind="Reviewer"),
        "a workspace is recorded by hoh worktree": world.register(ok, kind="run-workspace"),
        "strand upper case": world.register(ok, strand="S1"),
        "strand trailing newline": world.register(ok, strand="s1\n"),
        "strand too long": world.register(ok, strand="s" * 33),
        "strand empty": world.register(ok, strand=""),
        "perspective upper": world.register(ok, perspective="A"),
        "perspective too long": world.register(ok, perspective="abcdefg"),
        "round zero": world.register(ok, round=0),
        "round too big": world.register(ok, round=1000),
        "candidate short": world.register(ok, candidate=world.cands["feat"][:12]),
        "candidate branch": world.register(ok, candidate="feat"),
        "candidate HEAD": world.register(ok, candidate="HEAD"),
        "candidate upper": world.register(ok, candidate=world.cands["feat"].upper()),
        "candidate unknown": world.register(ok, candidate="0" * 40),
        "candidate already merged": world.register(ok, candidate=world.base),
        "into without a candidate": world.register(ok, into=f"refs/heads/{world.main}"),
        "into is a tag": world.register(ok, candidate=world.cands["feat"], into="refs/tags/v1"),
        "into is short": world.register(ok, candidate=world.cands["feat"], into=world.main),
        "into unknown": world.register(ok, candidate=world.cands["feat"], into="refs/heads/nope"),
        "into dotdot": world.register(ok, candidate=world.cands["feat"],
                                      into="refs/heads/a..b"),
        "into newline": world.register(ok, candidate=world.cands["feat"],
                                       into=f"refs/heads/{world.main}\n"),
        "into already holds the candidate": world.register(ok, candidate=world.base,
                                                           into=f"refs/heads/{world.main}"),
        "pane id": world.register("w7:p1 ; x"),
    }
    for what, (rc, out, err) in bad.items():
        assert (rc, out) == (2, ""), (what, rc, out, err)
        assert err.startswith("surface register refused: ") and err.count("\n") == 1, (what, err)
    assert "--pane is required" in bad["reviewer without --pane"][2]
    world.monkeypatch.delenv("HERDR_PANE_ID")
    rc, out, err = world.hoh("surface", "register", "--project", "p", "--kind", "orchestrator",
                             "--strand", "s1", "--pane", ok)
    assert (rc, out) == (2, "") and "no strand" in err, err
    real_snapshot = world.fake.snapshot
    for what, setup in {
            "the snapshot cannot be read": lambda: setattr(world.fake, "snapshot_error",
                                                           DispatchError("down")),
            "the snapshot is not an object": lambda: setattr(world.fake, "snapshot",
                                                             lambda: ["not", "an", "object"]),
            "the snapshot has no list of agents": lambda: setattr(
                world.fake, "snapshot", lambda: {"panes": [], "tabs": []}),
            "a row of the snapshot is no object": lambda: setattr(
                world.fake, "snapshot", lambda: {"panes": ["x"], "tabs": [], "agents": []})}.items():
        setup()
        rc, out, err = world.register(ok)
        assert (rc, out) == (2, "") and err.startswith("surface register refused: "), (what, err)
        world.fake.snapshot_error = None
        world.fake.snapshot = real_snapshot
    assert world.tree_files() == before and not world.book.exists()
    rc, out, err = world.hoh("surface", "register", "--project", "nope", "--kind", "reviewer",
                             "--pane", ok, "--strand", "s1", "--perspective", "a")
    assert (rc, out) == (2, "") and "not registered" in err, err
    assert world.tree_files() == before and not world.book.exists()   # not even the book
    with pytest.raises(SystemExit) as caught:                     # argparse: a round that is no number
        world.register(ok, round="x")
    assert caught.value.code == 2
    world.capsys.readouterr()
    assert world.register(ok)[0] == 0                             # control: the same pane is fine
    again = world.agent("vh-s1-rev-a2")
    rc, out, err = world.register(again)
    assert (rc, out) == (2, "") and "already has a live reviewer a" in err, err
    rc, out, err = world.register(ok)
    assert rc == 2 and "registered already at round 1" in err, err
    for what, change in {"the same round": {"round": 1}, "a lower round": {"round": 0}}.items():
        rc, out, err = world.register(ok, **change)
        assert rc == 2, (what, err)
    for what, args in {"another strand": ("reviewer", "s7", "a"),
                       "another perspective": ("reviewer", "s1", "b"),
                       "another kind": ("drafter", "s1", "a")}.items():
        rc, out, err = world.register(ok, *args, round=2)
        assert (rc, out) == (2, "") and "registered already" in err, (what, err)
    world.fake.agents["renamed"] = dict(world.fake.agents.pop("vh-s1-rev-a"), name="renamed")
    rc, out, err = world.register(ok, round=2)                    # the agent in the pane is another
    assert (rc, out) == (2, "") and "named 'renamed'" in err, err
    world.fake.agents["vh-s1-rev-a"] = dict(world.fake.agents.pop("renamed"), name="vh-s1-rev-a")
    world.fake._drop(ok)                                          # the registered pane is gone
    rc, out, err = world.register(again)
    assert rc == 0, err                                           # its slot is free
    assert world.owned_panes() == {again}                         # and its old record is released
    assert any(ln["event"] == "release" and ln["pane_id"] == ok for ln in world.lines())
    rc, out, err = world.register(again)
    assert rc == 2 and "registered already" in err, err


# --------------------------------------------------------------------------- #
# finished is a lifecycle fact
# --------------------------------------------------------------------------- #


def test_an_idle_reviewer_of_an_open_strand_is_kept_whatever_its_agent_or_its_report_says(world):
    """Catches: the defect of 2026-10-10 -- a reviewer closed because its agent is `idle` or
    `done` (it waits between rounds and is re-used for the re-bind), or because its report is
    written (in the reviewer's own directory and in its `reports/` directory, footer complete), or
    because a run beside it has ended (the sweep judges that run: its workspace is `would_close`,
    the reviewers are not). Mutants: the strand rule answers 'over' for an idle agent (idle ==
    finished); a reviewer is over when a `*.md` with a `VERDICT:` line is in its own directory
    (i5's rejected rule, V1); a candidate that is not merged counts as merged."""
    a = world.reviewer("s1", "a", candidate=world.cands["feat"], status="idle")
    b = world.reviewer("s1", "b", candidate=world.cands["feat"], status="done")
    c = world.reviewer("s1", "c", status="working")
    for name in ("vh-s1-rev-a", "vh-s1-rev-b", "vh-s1-rev-c"):      # a report with a footer, in each
        folder = world.dirs / name                                  # reviewer's own directory and in
        (folder / "reports").mkdir()                                # its reports/ directory
        for target in (folder, folder / "reports"):
            (target / "report-a.md").write_text(
                "VERDICT: RELEASE_YES\nCANDIDATE: x\nBASE: x\nSPEC_SHA256: x\nBLOCKING_OPEN: 0\n",
                encoding="utf-8")
    world.run_dir("r1")                                              # a finished run, and a
    ws, wpane = world.workspace("03")                                # workspace of its root:
    rows = world.rows("--runs-root", str(world.root))                # the run IS judged (dry run)
    assert rows[wpane]["outcome"] == "would_close", rows              # and closes the workspace --
    assert {rows[p]["outcome"] for p in (a, b, c)} == {"pending"}, rows   # not the reviewers
    for apply in (False, True, True):
        rows = world.rows(apply=apply, **{})
        assert {rows[p]["outcome"] for p in (a, b, c)} == {"pending"}, rows
        assert all("strand s1 is open" in rows[p]["why"] for p in (a, b, c)), rows
    assert world.acted() == [] and {a, b, c} <= set(world.fake.panes)
    kept = [ln for ln in world.ledger() if ln["event"] == "kept"]
    assert sorted(ln["pane"] for ln in kept) == sorted([a, b, c]), kept   # one manifest line each,
    assert [ln["event"] for ln in world.ledger()].count("kept") == 3      # not one per sweep
    assert all(ln["why"].startswith("strand s1 is open") for ln in kept)
    rows = world.rows(apply=True)
    assert rows[a]["outcome"] == rows[b]["outcome"] == "pending"
    assert len(world.ledger()) == 3                                     # idempotent


def test_a_reviewer_of_a_merged_strand_is_closed_at_the_merge(world):
    """Catches: reviewers that outlive their strand's merge, closed before it, a merge judged by
    equality with HEAD (a `--no-ff` merge commit is not the candidate), another strand's reviewers
    swept along, a strand with two candidates judged merged when one is, the candidate of a replaced
    reviewer counted, a surface registered for a strand that is finished already. Mutants: every
    candidate counts as merged; merged means the candidate is HEAD; one merged candidate is enough;
    a released surface's candidate counts; a finished strand gets a new surface."""
    a = world.reviewer("s1", "a", candidate=world.cands["feat"])
    b = world.reviewer("s1", "b", candidate=world.cands["feat"])
    spec = world.reviewer("s1", "x", kind="drafter")                # no candidate: the strand's
    other = world.reviewer("s2", "a", candidate=world.cands["other"])
    s3a = world.reviewer("s3", "a", candidate=world.cands["feat"])   # a split strand: its second
    s3b = world.reviewer("s3", "b", candidate=world.cands["other"])  # candidate is not merged
    old5 = world.reviewer("s5", "a", candidate=world.cands["other"])
    world.fake._drop(old5)                                           # gone: the replacement's
    s5 = world.reviewer("s5", "a", candidate=world.cands["feat"])    # candidate is the only one
    assert old5 not in world.owned_panes()
    rows = world.rows()
    assert {rows[p]["outcome"] for p in (a, b, spec, other, s3a, s3b, s5)} == {"pending"}, rows
    merged_tip = world.merge("feat")
    rows = world.rows()
    assert [rows[p]["outcome"] for p in (a, b, spec, s5)] == ["would_close"] * 4, rows
    assert rows[other]["outcome"] == "pending" and "strand s2 is open" in rows[other]["why"]
    assert {rows[p]["outcome"] for p in (s3a, s3b)} == {"pending"}, rows   # one candidate merged only
    assert all(f"merged: its candidate {world.cands['feat'][:12]}" in rows[p]["why"]
               and merged_tip[:12] in rows[p]["why"] for p in (a, b, spec)), rows
    assert world.acted() == []                                       # a dry run closes nothing
    rc, out, err = world.register(world.agent("vh-s1-rev-z"), "reviewer", "s1", "z")
    assert (rc, out) == (2, "") and "strand s1 is merged" in err and "new surface" in err, err
    tabs = [world.fake.panes[p]["tab_id"] for p in (a, b, spec, s5)]
    rows = world.rows(apply=True)
    assert [rows[p]["outcome"] for p in (a, b, spec, s5)] == ["closed"] * 4, rows
    assert sorted(c[1] for c in world.acted()) == sorted(tabs), world.acted()
    assert {other, s3a, s3b} <= set(world.fake.panes) and {a, b, spec, s5}.isdisjoint(world.fake.panes)
    events = [(ln["event"], ln["pane"]) for ln in world.ledger()
              if ln["event"] in ("closing", "closed")]
    for p in (a, b, spec):
        assert [e for e, pane in events if pane == p] == ["closing", "closed"], events
    closing = [ln for ln in world.ledger() if ln["event"] == "closing"]
    assert all("merged" in ln["reason"] and ln["scope"] == "tab" for ln in closing), closing
    assert world.owned_panes() == {other, s3a, s3b}                  # the others released
    world.reset()
    assert world.rows(apply=True)[other]["outcome"] == "pending" and world.acted() == []
    git(world.repo, "checkout", "-q", "other")      # HEAD moves onto the candidate's own branch:
    assert world.rows()[other]["outcome"] == "pending"      # the strand was registered against `main`
    git(world.repo, "checkout", "-q", "--detach")
    assert world.rows()[other]["outcome"] == "pending"
    git(world.repo, "checkout", "-q", world.main)
    world.merge("other")                                             # the second candidate: now every
    rows = world.rows(apply=True)                                    # candidate of s3 is merged
    assert {rows[p]["outcome"] for p in (other, s3a, s3b)} == {"closed"}, rows
    assert "its candidate" in rows[s3a]["why"] and "; " in rows[s3a]["why"], rows


def test_a_parked_strand_keeps_its_reviewers_until_it_is_released(world):
    """Catches: reviewers closed because a run is parked or finished, a release that is not
    explicit, recorded, idempotent and per strand. Mutants: the release records are not read; a
    release releases every strand."""
    a = world.reviewer("s1", "a")
    b = world.reviewer("s1", "b")
    other = world.reviewer("s2", "a")
    world.run_dir("r1")                                              # parked: the process ended
    for _ in range(3):
        rows = world.rows(apply=True)
        assert {rows[p]["outcome"] for p in (a, b, other)} == {"pending"}, rows
    assert world.acted() == []
    for bad in ("S1", "s1\n", "", "x" * 33):
        rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", bad,
                                 "--reason", "done")
        assert (rc, out) == (2, "") and err.startswith("surface release refused: "), (bad, err)
    for reason in ("", "   ", "x" * 201, "a\nb"):
        rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "s1",
                                 "--reason", reason)
        assert (rc, out) == (2, ""), (reason, err)
    for project in ("nope", "../p"):                                   # a project that is not registered
        rc, out, err = world.hoh("surface", "release", "--project", project, "--strand", "s1",
                                 "--reason", "done")
        assert (rc, out) == (2, "") and "not registered" in err, (project, err)
    assert not (world.book / "strand-events.jsonl").exists()
    rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "s9",
                             "--reason", "x" * 200)                    # exactly at the bound
    assert rc == 0 and json.loads(out)["already"] is False, err
    rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "s1",
                             "--reason", "spec pinned")
    assert rc == 0 and json.loads(out)["already"] is False, err
    rc, out, err = world.register(world.agent("vh-s1-rev-z"), "reviewer", "s1", "z")
    assert (rc, out) == (2, "") and "strand s1 is released" in err, err      # a finished strand
    rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "s1",
                             "--reason", "again")
    assert rc == 0 and json.loads(out)["already"] is True, err
    events = (world.book / "strand-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(e)["strand"] for e in events] == ["s9", "s1"]
    assert json.loads(events[1])["reason"] == "spec pinned"
    rows = world.rows()
    assert [rows[p]["outcome"] for p in (a, b)] == ["would_close"] * 2 and \
        "strand s1 is released" in rows[a]["why"], rows
    assert rows[other]["outcome"] == "pending"
    rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "s2",
                             "--reason", "also done")
    assert rc == 0, err
    rows = world.rows("--strand", "s1", apply=True)                  # a sweep of one strand ...
    assert set(rows) == {a, b} and [rows[p]["outcome"] for p in (a, b)] == ["closed"] * 2, rows
    assert other in world.fake.panes and world.owned_panes() == {other}   # ... leaves the other
    assert world.rows(apply=True)[other]["outcome"] == "closed"           # a full sweep closes it


def test_an_unrecorded_tab_with_a_matching_name_is_never_closed(world):
    """Catches: ownership inferred from a name or a label (the defect of 2026-10-10: forty-seven
    reviewer tabs and thirteen shells closed by their look), a pane id reused by another agent.
    Mutants: a surface is judged by its name pattern, not by the record; the recorded-name check
    dropped."""
    mine = world.reviewer("s1", "a")
    lookalike = world.agent("vh-s1-rev-b")                           # same naming, never registered
    shells = [world.fake.add_pane("w7", world.dirs / f"sh{i}") for i in range(3)]
    for pane in shells:
        world.procs.add_shell(pane)
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "done")
    rows = world.rows(apply=True)
    assert set(rows) == {mine} and rows[mine]["outcome"] == "closed", rows   # only the record is looked at
    assert lookalike in world.fake.panes and all(p in world.fake.panes for p in shells)
    assert [c[0] for c in world.acted()] == ["close_tab"]
    # the pane id of a recorded surface is reused by another agent: refused, not closed
    reused = world.reviewer("s3", "a")
    world.hoh("surface", "release", "--project", "p", "--strand", "s3", "--reason", "done")
    name = "vh-s3-rev-a"
    world.fake.agents["someone-else"] = dict(world.fake.agents.pop(name), name="someone-else")
    world.reset()
    rows = world.rows(apply=True)
    assert rows[reused]["outcome"] == "refused" and "not the one that was registered" in \
        rows[reused]["why"], rows
    assert reused in world.fake.panes and world.acted() == []


def test_an_orchestrator_surface_is_never_closed_by_reap(world):
    """Catches: the operator's own session closed because a strand it served ended. Mutants: an
    orchestrator surface judged like a reviewer's; judged over."""
    orch = world.agent("vh-orchestrator-1")
    rc, out, err = world.register(orch, "orchestrator")
    assert rc == 0, err
    a = world.reviewer("s1", "a")
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "done")
    world.run_dir("r1")
    for extra in ((), ("--strand", "s1"), ("--strand=s1",), ("--runs-root", str(world.root))):
        rows = world.rows(*extra, apply=True)
        assert world.fake.panes[orch]["tab_id"] not in [c[1] for c in world.acted()], extra
        assert orch in world.fake.panes, extra
        if "--strand" not in extra and "--strand=s1" not in extra:
            assert rows[orch]["outcome"] == "pending" and \
                "never reaped automatically" in rows[orch]["why"], rows
    assert a not in world.fake.panes                                  # the reviewer closed
    kept = [ln for ln in world.ledger() if ln["pane"] == orch]
    assert [ln["event"] for ln in kept] == ["kept"], kept


# --------------------------------------------------------------------------- #
# run workspaces
# --------------------------------------------------------------------------- #


def test_a_run_workspace_is_closed_only_when_every_run_of_its_root_has_ended(world):
    """Catches: a workspace closed while one of its root's runs is still live, with no run at all,
    on a marker that is not a plain readable `ended`, or on a run whose process ended but whose run
    waits (paused, blocked, checkpointed, at a stage short of delivery: the run is not over). Mutants:
    one ended run is enough; no run counts as over; the marker's existence is enough; the process
    lock is not asked; a run whose process ended is over (the state is not asked)."""
    ws, pane = world.workspace("20")
    outcome = lambda: world.rows("--runs-root", str(world.root))[pane]      # noqa: E731
    assert outcome()["outcome"] == "pending" and "no run has been recorded" in outcome()["why"]
    (world.root / "preserve-policy.json").write_text("{}", encoding="utf-8")   # not a run
    (world.root / "_arenas").mkdir()                                           # not a run
    assert outcome()["outcome"] == "pending"
    world.run_dir("done-1", "ended")
    live = world.run_dir("run-2", "running")
    assert outcome()["outcome"] == "pending" and "run run-2" in outcome()["why"]
    (live / "process.json").write_text(json.dumps({"state": "ended"}), encoding="utf-8")
    assert outcome()["outcome"] == "would_close", outcome()
    never = world.run_dir("run-3", None)                                       # state, no marker
    assert outcome()["outcome"] == "pending"
    (never / "process.json").write_text('{"state": "ended"}', encoding="utf-8")
    assert outcome()["outcome"] == "would_close"
    for what, content in {"array": "[]", "string": '"ended"', "state missing": "{}",
                          "state spelled": '{"state": "Ended"}', "state list": '{"state": ["ended"]}',
                          "state padded": '{"state": "ended "}',
                          "utf-16": '{"state": "ended"}'.encode("utf-16"),
                          "lone surrogate": b'{"state": "ended\\ud800"}',
                          "corrupt": '{"state": "ended"', "non-utf8": b"\xff\xfe{}",
                          "nul": b'{"state": "ended\x00"}', "empty": "",
                          "huge": '{"state": "ended", "x": "' + "a" * 70000 + '"}'}.items():
        marker = never / "process.json"
        marker.write_bytes(content if isinstance(content, bytes) else content.encode())
        assert outcome()["outcome"] == "pending", (what, outcome())
    limit = surfacebook.PROCESS_JSON_LIMIT                                     # the bound, both sides
    marker.write_bytes(b'{"state": "ended"}' + b" " * (limit - 18))
    assert marker.stat().st_size == limit and outcome()["outcome"] == "would_close"
    marker.write_bytes(b'{"state": "ended"}' + b" " * (limit - 17))
    assert marker.stat().st_size == limit + 1 and outcome()["outcome"] == "pending"
    marker.write_text('{"state": "ended"}', encoding="utf-8")
    os.symlink(world.dirs, world.root / "linked-run")                          # a link in the root
    assert outcome()["outcome"] == "pending" and "is a link" in outcome()["why"]
    held = world.root / "linked-run"
    os.rename(held, world.tmp_path / "linked-away")
    fifo = never / "process.json"
    os.rename(fifo, never / "process.json.parked")
    os.mkfifo(fifo)
    assert outcome()["outcome"] == "pending", "a FIFO marker must not block the reaper"
    os.rename(fifo, never / "process.json.fifo")
    os.mkdir(fifo)
    assert outcome()["outcome"] == "pending"
    os.rename(fifo, never / "process.json.dir")
    os.symlink(never / "process.json.parked", fifo)
    assert outcome()["outcome"] == "pending", "a marker that is a link is not read"
    os.rename(fifo, never / "process.json.link")
    os.rename(never / "process.json.parked", never / "process.json")
    assert outcome()["outcome"] == "would_close"
    moved = world.tmp_path / "elsewhere" / world.root.name              # the root moved, same name:
    moved.parent.mkdir()                                                # the record names its path
    os.rename(world.root, moved)
    rows = world.rows("--runs-root", str(moved))
    assert rows[pane]["outcome"] == "pending" and "recorded for the runs root" in rows[pane]["why"]
    os.rename(moved, world.root)
    assert outcome()["outcome"] == "would_close"
    lock = os.open(world.root / "run-2" / ".process.lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock, fcntl.LOCK_EX)                                           # a live holder
    try:
        assert outcome()["outcome"] == "pending" and "live process" in outcome()["why"]
    finally:
        os.close(lock)
    assert outcome()["outcome"] == "would_close"
    # An ended process is not an ended run: the marker says `ended` for each of these, and none is
    # over by I10a's rule (failed, cancelled, or ready for delivery) -- the next `hoh run` goes on.
    for what, change in {"paused": dict(condition=Condition.PAUSED, stage=Stage.DEVELOPING),
                         "blocked": dict(condition=Condition.BLOCKED, stage=Stage.VERIFYING),
                         "active at developing": dict(condition=Condition.ACTIVE,
                                                      stage=Stage.DEVELOPING),
                         "active at a checkpoint": dict(condition=Condition.ACTIVE,
                                                        stage=Stage.CHECKPOINTED),
                         "active, never planned": dict(condition=Condition.ACTIVE,
                                                       stage=Stage.NEW)}.items():
        world.run_dir("run-3", "ended", **change)
        assert outcome()["outcome"] == "pending", (what, outcome())
        assert "run run-3" in outcome()["why"] and "not over" in outcome()["why"], outcome()
    for what, change in {"failed": dict(condition=Condition.FAILED, stage=Stage.DEVELOPING),
                         "cancelled": dict(condition=Condition.CANCELLED, stage=Stage.PLANNING),
                         "delivered": dict(condition=Condition.ACTIVE,
                                           stage=Stage.READY_FOR_DELIVERY)}.items():
        world.run_dir("run-3", "ended", **change)
        assert outcome()["outcome"] == "would_close", (what, outcome())
    for what, text in {"state unreadable": "{}", "state corrupt": "{", "state empty": "",
                       "state is an array": "[]"}.items():
        world.run_dir("run-3", "ended", state_text=text)
        assert outcome()["outcome"] == "pending", (what, outcome())
        assert "not over" in outcome()["why"], (what, outcome())
    world.run_dir("run-3", "ended")                                            # the control
    assert outcome()["outcome"] == "would_close"


def test_a_recorded_workspace_of_an_ended_run_is_closed_and_the_worktree_stays(world):
    """Catches: a workspace of an ended run left open, the pair of workspaces `hoh worktree` makes
    (a target and its linked dev workspace, one group) closed twice or reported as a failure, a
    worktree directory touched. Mutants: the close scoped to the tab, not the workspace; the
    worktree directory removed (a rename stands in for the forbidden call)."""
    world.run_dir("r1")
    (target, tpane), (dev, dpane) = world.pair("30")      # one `hoh worktree` call: one group;
    # closing the target closes the dev workspace (the fake's cascade, set by `pair`)
    folders = sorted(p for p in world.dirs.iterdir() if p.name.startswith(("tgt-", "dev-")))
    assert len(folders) == 2
    rows = world.rows("--runs-root", str(world.root))
    assert [rows[p]["outcome"] for p in (tpane, dpane)] == ["would_close"] * 2, rows
    assert world.acted() == []
    rows = world.rows("--runs-root", str(world.root), apply=True)
    assert rows[tpane]["outcome"] == "closed" and rows[dpane]["outcome"] in ("released", "closed")
    assert [c[:2] for c in world.acted()] == [("close_workspace", target)], world.acted()
    assert tpane not in world.fake.panes and dpane not in world.fake.panes
    assert sorted(p for p in world.dirs.iterdir() if p.name.startswith(("tgt-", "dev-"))) == folders
    assert all(f.is_dir() for f in folders)                           # the worktrees stay
    ledger = world.ledger(world.root)
    assert [ln["event"] for ln in ledger if ln["pane"] == tpane] == ["closing", "closed"]
    assert next(ln for ln in ledger if ln["event"] == "closing")["scope"] == "workspace"
    mine = [ln for ln in ledger if ln["pane"] == dpane]       # asked to close, found it gone
    assert [ln["event"] for ln in mine] == ["closing", "closed"] and mine[1]["already_gone"], ledger
    assert world.owned_panes(world.root) == set()
    world.reset()
    assert world.rows("--runs-root", str(world.root), apply=True) == {} and world.acted() == []


def test_nothing_unrecorded_working_orchestrator_or_calling_is_ever_closed(world):
    """Catches: a workspace closed with a stranger's pane in it, with a working agent, with a
    shell that is not asleep, whose pane left its directory, or that is the caller's own; an
    unrecorded workspace swept along. Mutants: strangers not looked at; an agent in `working` is
    closed; the calling tab is closed; a shell that is not asleep is closed."""
    world.run_dir("r1")
    stranger_ws, stranger_pane = world.workspace("40")
    world.fake.add_pane(stranger_ws, world.dirs, tab="w40:tX")          # a pane nobody recorded
    working_ws, working_pane = world.workspace("41", shell=False)
    world.fake.agents["worker"] = {"name": "worker", "pane_id": working_pane,
                                   "tab_id": world.fake.panes[working_pane]["tab_id"],
                                   "agent": "claude", "agent_status": "working", "cwd": "/x",
                                   "foreground_cwd": "/x", "workspace_id": working_ws}
    busy_ws, busy_pane = world.workspace("42")
    world.procs.set_state(busy_pane, "R")                               # a shell that is running
    left_ws, left_pane = world.workspace("43")
    world.fake.panes[left_pane]["foreground_cwd"] = str(world.tmp_path / "elsewhere")
    mine_ws, mine_pane = world.workspace("44")
    unrecorded = world.fake.add_pane("w45", world.dirs)                  # a workspace nobody recorded
    world.procs.add_shell(unrecorded)
    world.monkeypatch.setenv("HERDR_PANE_ID", mine_pane)
    world.monkeypatch.setenv("HERDR_TAB_ID", world.fake.panes[mine_pane]["tab_id"])
    rows = world.rows("--runs-root", str(world.root), apply=True)
    assert rows[stranger_pane]["outcome"] == "refused" and "does not own" in rows[stranger_pane]["why"]
    assert rows[working_pane]["outcome"] == "kept" and "working" in rows[working_pane]["why"]
    assert rows[busy_pane]["outcome"] == "kept" and "not proven idle" in rows[busy_pane]["why"]
    assert rows[left_pane]["outcome"] == "refused" and "not at or below" in rows[left_pane]["why"]
    assert rows[mine_pane]["outcome"] == "kept" and "reap from another pane" in rows[mine_pane]["why"]
    assert unrecorded not in rows and unrecorded in world.fake.panes
    assert world.acted() == [], world.acted()
    for pane in (stranger_pane, working_pane, busy_pane, left_pane, mine_pane):
        assert pane in world.fake.panes


def test_a_cascaded_close_and_a_missing_workspace_are_closed_not_errors(world):
    """Catches: a workspace that vanished with its linked workspace, or between the snapshot and
    the close, reported as a failure (Herdr answers `workspace_not_found`), and a real failure
    reported as a success. Mutants: the 'gone' proof after a failed close dropped."""
    world.run_dir("r1")
    ws, pane = world.workspace("50")
    world.fake.add_pane("w51", world.dirs)
    real = world.fake.close_workspace

    def vanished(workspace_id, *, purpose=""):                          # closed by someone else first
        world.fake._drop(pane)
        raise DispatchError("workspace_not_found")

    world.fake.close_workspace = vanished
    row = world.rows("--runs-root", str(world.root), apply=True)[pane]
    assert row["outcome"] == "released" and "another reaper closed the pane first" in row["why"], row
    assert [ln["event"] for ln in world.ledger(world.root) if ln["pane"] == pane][-1] == "closed"
    assert world.owned_panes(world.root) == set()
    ws2, pane2 = world.workspace("52")                                  # a failure that did not close
    def broken(workspace_id, *, purpose=""):
        raise DispatchError("herdr is on fire")
    world.fake.close_workspace = broken
    row = world.rows("--runs-root", str(world.root), apply=True)[pane2]
    assert row["outcome"] == "refused" and "the close failed" in row["why"], row
    assert pane2 in world.fake.panes and world.owned_panes(world.root) == {pane2}
    world.fake.close_workspace = real
    world.fake._drop(pane2)                                             # gone already, no close needed
    row = world.rows("--runs-root", str(world.root), apply=True)[pane2]
    assert row["outcome"] == "released" and "already gone" in row["why"], row


def test_reap_is_a_dry_run_unless_applied_and_every_surface_has_a_manifest_line(world):
    """Catches: a sweep that closes without `--apply`, a close or a keep with no line of the
    ledger, a flag spelled another way. Mutants: the sweep always closes; a pending surface leaves
    no `kept` line."""
    a = world.reviewer("s1", "a")
    keep = world.reviewer("s2", "a", candidate=world.cands["other"])
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "done")
    ws, wpane = world.workspace("60")
    world.run_dir("r1")
    before = world.tree_files()
    world.reset()
    for extra in ((), ("--strand", "s1"), ("--strand=s1",)):
        rc, doc, err = world.sweep("--runs-root", str(world.root), *extra)
        assert rc == 0 and doc["apply"] is False, err
        assert world.acted() == [] and world.tree_files() == before, extra
    rc, doc, err = world.sweep("--runs-root", str(world.root))
    by_pane = {r["pane_id"]: r for r in doc["surfaces"]}
    assert by_pane[a]["outcome"] == "would_close" and by_pane[keep]["outcome"] == "pending"
    assert by_pane[wpane]["outcome"] == "would_close"
    assert (by_pane[a]["kind"], by_pane[a]["strand"], by_pane[a]["perspective"],
            by_pane[a]["round"]) == ("reviewer", "s1", "a", 1)
    assert by_pane[wpane]["kind"] == "run-workspace" and by_pane[wpane]["workspace_id"] == ws
    assert doc["tips"] == {f"refs/heads/{world.main}": git(world.repo, "rev-parse", "HEAD")}
    assert sorted(doc["homes"]) == sorted([str(world.book), str(world.root)])
    only = world.rows("--strand", "s1")
    assert set(only) == {a}, only                                         # the filter: that strand only
    rc, doc, err = world.sweep("--runs-root", str(world.root), apply=True)
    assert rc == 0 and doc["apply"] is True, err
    assert sorted(c[0] for c in world.acted()) == ["close_tab", "close_workspace"], world.acted()
    book_events = [(ln["event"], ln["pane"]) for ln in world.ledger()]
    assert sorted(book_events) == sorted([("kept", keep), ("closing", a), ("closed", a)]), book_events
    assert [ln["event"] for ln in world.ledger(world.root)] == ["closing", "closed"]
    assert all(ln["why" if ln["event"] == "kept" else "reason"] for ln in world.ledger()
               if ln["event"] in ("kept", "closing"))
    with pytest.raises(SystemExit) as caught:
        world.hoh("reap", "--project", "p", "--close")
    assert caught.value.code == 2                                         # one spelling: --apply
    world.capsys.readouterr()


def test_a_close_of_the_target_never_takes_an_unexamined_linked_workspace_with_it(world):
    """Catches: the cascade of the 2026-10-07 manifest -- Herdr's close of a target workspace ends
    its linked workspace, which the reaper had not examined: a stranger's working agent in it, the
    caller's own pane in it, an agent that is working, a linked workspace that was never recorded.
    The workspaces one `hoh worktree` call made are one unit: closed only if every member passes.
    Mutants: the group is not asked (each workspace judged alone); the member that is not in the
    record is not counted; a member that is gone already blocks the close; a released member that
    Herdr still lists is ignored; the group fields are not checked (key spelling, size bounds); an
    exception of the group rule closes on."""
    def case(label):
        root = world.tmp_path / f"runs-{label}"
        root.mkdir()
        world.run_dir("r1", root=root)                                   # the run has ended
        return root

    def sweep(root, apply):
        return world.rows("--runs-root", str(root), apply=apply)

    # a stranger's working agent in the linked workspace
    root = case("a")
    (tws, tpane), (dws, dpane) = world.pair("a", root=root)
    outsider = world.fake.add_pane(dws, world.dirs, agent="someone", status="working",
                                   tab=f"{dws}:tX")
    for apply in (False, True):
        rows = sweep(root, apply)
        assert rows[dpane]["outcome"] == "refused" and "does not own" in rows[dpane]["why"], rows
        assert rows[tpane]["outcome"] == "refused", rows
        assert f"workspace {dws}" in rows[tpane]["why"] and "created together" in rows[tpane]["why"]
    assert world.acted() == [] and {tpane, dpane, outsider} <= set(world.fake.panes)
    assert world.owned_panes(root) == {tpane, dpane}

    # the linked workspace's own agent is working
    root = case("b")
    (tws, tpane), (dws, dpane) = world.pair("b", root=root)
    world.fake.agents["worker"] = {"name": "worker", "pane_id": dpane,
                                   "tab_id": world.fake.panes[dpane]["tab_id"], "agent": "claude",
                                   "agent_status": "working", "cwd": "/x", "foreground_cwd": "/x",
                                   "workspace_id": dws}
    for apply in (False, True):
        rows = sweep(root, apply)
        assert rows[dpane]["outcome"] == "kept" and "working" in rows[dpane]["why"], rows
        assert rows[tpane]["outcome"] == "kept" and "created together" in rows[tpane]["why"], rows
    assert world.acted() == [] and {tpane, dpane} <= set(world.fake.panes)

    # the caller's own pane is in the linked workspace; and then in the target's
    for who in ("linked", "target"):
        root = case(f"c-{who}")
        (tws, tpane), (dws, dpane) = world.pair(f"c-{who}", root=root)
        mine = dpane if who == "linked" else tpane
        world.monkeypatch.setenv("HERDR_PANE_ID", mine)
        world.monkeypatch.setenv("HERDR_TAB_ID", world.fake.panes[mine]["tab_id"])
        for apply in (False, True):
            rows = sweep(root, apply)
            assert {rows[tpane]["outcome"], rows[dpane]["outcome"]} == {"kept"}, (who, rows)
            assert any("reap from another pane" in rows[p]["why"] for p in (tpane, dpane))
        assert world.acted() == [] and {tpane, dpane} <= set(world.fake.panes), who
        world.monkeypatch.delenv("HERDR_PANE_ID")
        world.monkeypatch.delenv("HERDR_TAB_ID")

    # the linked workspace was never recorded (two panes when it was made): refused, with the count
    root = case("d")
    (tws, tpane), (dws, dpane) = world.pair("d", root=root, linked_panes=2)
    for apply in (False, True):
        rows = sweep(root, apply)
        assert set(rows) == {tpane} and rows[tpane]["outcome"] == "refused", rows
        assert "created together with 1 other workspace(s) of which 0 are in the record" in (
            rows[tpane]["why"]), rows
    assert world.acted() == [] and tpane in world.fake.panes
    assert len([p for p in world.fake.panes.values() if p["workspace_id"] == dws]) == 2

    # a record that says fewer members than the record holds (forged) is refused as well
    root = case("e")
    (tws, tpane), (dws, dpane) = world.pair("e", root=root)
    path = root / surfaces.SURFACES_FILE
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    lines[1]["book"]["group_size"] = 1
    path.write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")
    rows = sweep(root, True)
    assert {rows[tpane]["outcome"], rows[dpane]["outcome"]} == {"refused"}, rows
    assert world.acted() == []

    # a group key or a size that is not exactly what `hoh worktree` writes is no lifecycle record
    root = case("j")
    (tws, tpane), (dws, dpane) = world.pair("j", root=root)
    path = root / surfaces.SURFACES_FILE
    good = path.read_text(encoding="utf-8")
    key = json.loads(good.splitlines()[0])["book"]["group"]
    assert surfacebook.GROUP_RE.fullmatch(key)
    odd = {"group missing": ("group", None), "group a list": ("group", [key]),
           "group upper": ("group", key.upper()), "group newline": ("group", key + "\n"),
           "group short": ("group", key[:-1]), "group empty": ("group", ""),
           "size bool": ("group_size", True), "size string": ("group_size", "2"),
           "size float": ("group_size", 2.0), "size zero": ("group_size", 0),
           "size three": ("group_size", 3), "size negative": ("group_size", -2),
           "size missing": ("group_size", None)}
    for what, (field, value) in odd.items():
        lines = [json.loads(ln) for ln in good.splitlines()]
        if value is None:
            lines[0]["book"].pop(field)
        else:
            lines[0]["book"][field] = value
        path.write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")
        rows = sweep(root, True)
        assert rows[tpane]["outcome"] == "pending" and "no lifecycle record" in rows[tpane]["why"], (
            what, rows)
        assert rows[dpane]["outcome"] == "refused", (what, rows)         # its mate is not in the record
        assert world.acted() == [], what
    path.write_text(good, encoding="utf-8")
    assert [r["outcome"] for r in sweep(root, False).values()] == ["would_close"] * 2   # the control

    # a member that was released while Herdr still lists it was not examined by this reap: kept
    root = case("h")
    (tws, tpane), (dws, dpane) = world.pair("h", root=root)
    surfaces.registry_for(root).release(dpane, reason="by hand")
    rows = sweep(root, True)
    assert rows[tpane]["outcome"] == "kept" and "still lists it" in rows[tpane]["why"], rows
    assert world.acted() == [] and {tpane, dpane} <= set(world.fake.panes)

    # the control: nothing in the way -- both are judged, one close ends both, the worktrees stay
    root = case("f")
    (tws, tpane), (dws, dpane) = world.pair("f", root=root)
    rows = sweep(root, False)
    assert [rows[p]["outcome"] for p in (tpane, dpane)] == ["would_close"] * 2, rows
    assert world.acted() == []
    rows = sweep(root, True)
    assert [c[:2] for c in world.acted()] == [("close_workspace", tws)], world.acted()
    assert rows[tpane]["outcome"] == "closed" and rows[dpane]["outcome"] == "released", rows
    assert tpane not in world.fake.panes and dpane not in world.fake.panes
    world.reset()

    # the linked workspace is gone already (closed by hand): nothing left to examine, the target goes
    root = case("g")
    (tws, tpane), (dws, dpane) = world.pair("g", root=root)
    world.fake._drop(dpane)
    rows = sweep(root, False)
    assert rows[dpane]["outcome"] == "would_release" and rows[tpane]["outcome"] == "would_close", rows
    rows = sweep(root, True)
    assert [c[:2] for c in world.acted()] == [("close_workspace", tws)], world.acted()
    assert rows[tpane]["outcome"] == "closed", rows

    # a group that cannot be determined is not a reason to close: refused, nothing sent
    root = case("k")
    (tws, tpane), (dws, dpane) = world.pair("k", root=root)

    def boom(self, home, surface):
        raise RuntimeError("the record of the group is gone")

    world.monkeypatch.setattr(surfacebook.Rule, "group_of", boom)
    world.reset()
    rows = sweep(root, True)
    assert {rows[tpane]["outcome"], rows[dpane]["outcome"]} == {"refused"}, rows
    assert "cannot be determined" in rows[tpane]["why"] and world.acted() == [], rows


# --------------------------------------------------------------------------- #
# born registered: hoh worktree and the end of hoh run
# --------------------------------------------------------------------------- #


def _make_worktree(world, monkeypatch, workspaces, *, path=None):
    """`herdr.worktree_create` replaced by something that makes `workspaces` (id, number of panes,
    directory) in the fake runtime and answers like Herdr, minus ids."""
    calls: list[dict] = []
    path = path or str(world.dirs / "dev")

    def create(**kwargs):
        calls.append(kwargs)
        for ws, panes, folder in workspaces:
            for _ in range(panes):
                world.fake.add_pane(ws, folder)
        return {"worktree": {"path": path}}

    monkeypatch.setattr(herdr, "worktree_create", create)
    return calls


def test_hoh_worktree_records_the_workspaces_it_created_and_only_those(world, monkeypatch):
    """Catches: a creation that leaves no record (the workspaces of 88 runs could only be guessed
    from a label), a workspace that existed before recorded as ours, a workspace that is not what
    the command made, a concurrent creation's workspace recorded with the pair. Mutants: every
    workspace of the after-snapshot is recorded; the directory check dropped; a workspace with two
    panes recorded; a third workspace recorded with the pair; the group key or its size not
    written."""
    monkeypatch.setenv("HERDR_ENV", "1")
    old = world.fake.add_pane("w9", world.dirs)                       # the operator's own workspace
    target = world.dirs / "target"
    dev = world.dirs / "dev"
    target.mkdir()
    dev.mkdir()
    calls = _make_worktree(world, monkeypatch, [("w60", 1, target), ("w61", 1, dev)])
    rc, out, err = world.hoh("worktree", "--repo", str(target), "--branch", "loop/x-dev")
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["worktree"] == str(dev) and len(calls) == 1
    assert sorted(w["workspace_id"] for w in doc["workspaces"]) == ["w60", "w61"], doc
    assert old not in {w["pane_id"] for w in doc["workspaces"]}
    lines = world.lines(world.root)
    assert [ln["book"]["workspace_id"] for ln in lines] == ["w60", "w61"]
    assert all(ln["book"]["kind"] == "run-workspace" and
               ln["book"]["runs_root"] == str(world.root.resolve()) for ln in lines)
    assert "worktree: " not in err and world.owned_panes(world.root) == {
        w["pane_id"] for w in doc["workspaces"]}
    groups = {ln["book"]["group"] for ln in lines}               # one call, one group of two
    assert len(groups) == 1 and surfacebook.GROUP_RE.fullmatch(next(iter(groups))), lines
    assert [ln["book"]["group_size"] for ln in lines] == [2, 2]
    assert {w["group"] for w in doc["workspaces"]} == groups
    # what is not recorded, and why (the worktree is made either way, the exit code is 0)
    other_root = world.tmp_path / "runs2"
    other_root.mkdir()
    cases = {
        "two panes": ([("w62", 2, dev)], "holds 2 panes"),
        "pane elsewhere": ([("w63", 1, world.tmp_path / "elsewhere")], "not at or below"),
        "nothing new": ([], "no new workspace"),
        # somebody else's workspace appeared while the worktree was made: a creation makes a
        # target's workspace and a linked one, never three, and never two in the same place
        "three new workspaces": ([("w70", 1, target), ("w71", 1, dev),
                                  ("w72", 1, world.tmp_path / "elsewhere")],
                                 "more than one creation makes"),
        "two in one place": ([("w73", 1, dev), ("w74", 1, dev)], "the place of the same one"),
        "two targets": ([("w75", 1, target), ("w76", 1, target)], "the place of the same one"),
    }
    for what, (made, note) in cases.items():
        _make_worktree(world, monkeypatch, made)
        rc, out, err = world.hoh("--root", str(other_root), "worktree", "--repo", str(target),
                                 "--branch", f"loop/{what[:3]}")
        assert rc == 0 and json.loads(out)["workspaces"] == [], (what, err)
        assert note in err and err.startswith("worktree: "), (what, err)
        assert not (other_root / surfaces.SURFACES_FILE).exists(), what
    single = world.tmp_path / "runs3"                                  # the target's workspace was
    single.mkdir()                                                     # there before: a group of one
    _make_worktree(world, monkeypatch, [("w77", 1, dev)])
    rc, out, err = world.hoh("--root", str(single), "worktree", "--repo", str(target),
                             "--branch", "loop/s")
    assert rc == 0 and "worktree: " not in err, err
    assert [ln["book"]["group_size"] for ln in world.lines(single)] == [1]
    _make_worktree(world, monkeypatch, [("w64", 1, dev)])
    rc, out, err = world.hoh("--root", str(world.tmp_path / "missing"), "worktree", "--repo",
                             str(target), "--branch", "loop/m")
    assert rc == 0 and "is not a plain directory" in err and json.loads(out)["workspaces"] == []
    real_snapshot = world.fake.snapshot
    world.fake.snapshot = lambda: ["not", "an", "object"]              # a snapshot that is no snapshot
    _make_worktree(world, monkeypatch, [("w66", 1, dev)])
    rc, out, err = world.hoh("--root", str(other_root), "worktree", "--repo", str(target),
                             "--branch", "loop/o")
    assert rc == 0 and "unusable" in err and json.loads(out)["workspaces"] == [], err
    world.fake.snapshot = real_snapshot
    world.fake.snapshot_error = DispatchError("down")                  # no snapshot at all
    calls = _make_worktree(world, monkeypatch, [("w65", 1, dev)])
    rc, out, err = world.hoh("--root", str(other_root), "worktree", "--repo", str(target),
                             "--branch", "loop/n")
    assert rc == 0 and len(calls) == 1 and "will not be recorded" in err, err
    assert json.loads(out)["workspaces"] == [] and not (other_root / surfaces.SURFACES_FILE).exists()


def test_the_end_of_hoh_run_reaps_the_workspaces_of_its_root_once_every_run_has_ended(
        world, monkeypatch):
    """Catches: workspaces left open at the end of the last run of a root, closed while another
    run of the root is live or waits (an ended process is not an ended run), an exit code or a
    stdout changed by the reap, a failure of the reap that escapes. Mutants: `cmd_run` does not
    call the reap; a failing reap raises; the reap ignores the other runs of the root."""
    ws, pane = world.workspace("70")

    def run(name, rc=0, stage=Stage.DEVELOPING, condition=Condition.ACTIVE):
        store = RunStore(world.root, name)
        store.ensure()
        state = RunState(run_id=name, repo_path=str(world.dirs), project_name="p",
                         spec_path=str(world.dirs / "spec.md"), spec_digest="d",
                         policy_digest="d", profile_digest="d", stage=stage,
                         condition=condition)
        store.state_path.write_text(state.model_dump_json(), encoding="utf-8")
        monkeypatch.setattr(cli, "_cmd_run", lambda args: rc)
        return cli.cmd_run(argparse.Namespace(root=str(world.root), run_id=name))

    world.run_dir("other", "running")                                  # another run of the root
    assert run("r1", rc=3) == 3
    cap = world.capsys.readouterr()
    assert (cap.out, cap.err) == ("", ""), cap                     # pending is not worth a note
    assert world.acted() == [] and pane in world.fake.panes
    world.run_dir("other", "ended")                                    # its process ended, delivered
    assert run("r2", rc=5, condition=Condition.PAUSED) == 5            # an ended process, a run that
    cap = world.capsys.readouterr()                                   # waits (paused): not over
    assert (cap.out, cap.err) == ("", ""), cap
    assert world.acted() == [] and pane in world.fake.panes
    world.run_dir("r1", "ended", condition=Condition.FAILED)          # now r1 is over (failed)
    world.run_dir("r2", "ended", condition=Condition.CANCELLED)       # and r2 (cancelled)
    assert run("r3", rc=0, stage=Stage.READY_FOR_DELIVERY) == 0       # the last run is delivered
    cap = world.capsys.readouterr()
    assert cap.out == "", cap
    assert [c[:2] for c in world.acted()] == [("close_workspace", ws)] and pane not in world.fake.panes
    ws2, pane2 = world.workspace("71")
    monkeypatch.setattr(surfacebook.BookRegistry, "books",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run("r4", rc=0, stage=Stage.READY_FOR_DELIVERY) == 0        # a failing reap changes nothing
    cap = world.capsys.readouterr()
    assert cap.out == "" and "not reaped" in cap.err and pane2 in world.fake.panes
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_run")
    called = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)]
    assert called.index("reap_after_run") < called.index("reap_root_after_run")


# --------------------------------------------------------------------------- #
# fail closed, the command surface, the lane
# --------------------------------------------------------------------------- #


def test_a_record_or_event_file_that_cannot_be_read_closes_nothing(world):
    """Catches: a surface closed on a record or a release that is unreadable, forged, mistyped or
    odd (missing, link, directory, mode 000, non-UTF-8, NUL, corrupt, non-object, wrong types), or
    on a lifecycle fact that cannot be had (the repository gone). Mutants: any event line counts
    as a release; the record's `book` object is trusted without checking its types."""
    a = world.reviewer("s1", "a", candidate=world.cands["feat"])
    book = world.book
    events, record = book / "strand-events.jsonl", book / surfaces.SURFACES_FILE
    saved = record.read_bytes()
    line = json.loads(saved.decode().splitlines()[0])
    release_s1 = b'{"v": 1, "event": "release", "strand": "s1", "reason": "r", "at": "t"}\n'

    def forged(**changes):
        return (json.dumps({**line, "book": {**line["book"], **changes}}) + "\n").encode()

    def nothing_closed(what):
        rc, doc, err = world.sweep(apply=True)
        assert world.acted() == [] and a in world.fake.panes, (what, doc, err)
        assert doc is None or all(r["outcome"] in ("pending", "refused") for r in doc["surfaces"]), \
            (what, doc)

    def parked(path, how):
        """`path` set aside (a rename) and replaced by `how()`, which makes a link or a directory."""
        os.rename(path, path.with_name(path.name + ".parked"))
        how()
        return path.with_name(path.name + ".parked")

    # 1. the release file is odd and the record is good: nothing is released
    for what, content in {"strand is a list": b'{"event": "release", "strand": ["s1"]}\n',
                          "strand upper case": b'{"event": "release", "strand": "S1"}\n',
                          "another event": b'{"event": "note", "strand": "s1"}\n',
                          "array": b'["s1"]\n', "non-utf8": b"\xff\xfe\n", "nul": b"\x00\n",
                          "corrupt": b'{"event": "release", "strand": "s1"\n',
                          "utf-16": release_s1.decode().encode("utf-16"),
                          "lone surrogate": b'{"event": "release", "strand": "s1\\ud800"}\n',
                          "empty": b""}.items():
        events.write_bytes(content)
        nothing_closed(f"events: {what}")
    side = parked(events, lambda: os.symlink(world.dirs, events))
    nothing_closed("events: a link")
    os.rename(events, book / "events.link")
    os.symlink(book / "no-such-file", events)
    nothing_closed("events: a dangling link")
    os.rename(events, book / "events.dangling")
    os.mkdir(events)
    nothing_closed("events: a directory")
    os.rename(events, book / "events.dir")
    os.mkfifo(events)
    nothing_closed("events: a FIFO")
    os.rename(events, book / "events.fifo")
    os.rename(side, events)
    # 2. the record is odd and the strand is released by a good line: an odd record is no record
    events.write_bytes(release_s1)
    for what, content in {
            "tab id is a list": json.dumps({**line, "tab_id": ["x"]}).encode() + b"\n",
            "round is a bool": forged(round=True), "round is a string": forged(round="1"),
            "round zero": forged(round=0), "kind is a list": forged(kind=["reviewer"]),
            "kind unknown": forged(kind="role"), "strand with a space": forged(strand="s 1"),
            "candidate short": forged(candidate="abc"), "review is a list": forged(review=["r1"]),
        "review on a drafter's line": forged(kind="drafter", review="r1"),
        "review with an upper case": forged(review="R1"), "into without a candidate": forged(
                candidate=""), "into is a tag": forged(into="refs/tags/v1"),
            "book is a list": (json.dumps({**line, "book": []}) + "\n").encode(),
            "book is missing": (json.dumps({k: v for k, v in line.items() if k != "book"})
                                + "\n").encode(),
            "line is an array": b"[1, 2]\n", "line is a string": b'"x"\n',
            "non-utf8": b"\xff\xfe\n", "nul": b"\x00\x00\n", "corrupt": saved[:-10],
            "utf-16": saved.decode().encode("utf-16"),
            "empty": b""}.items():
        record.write_bytes(content)
        nothing_closed(f"record: {what}")
    record.write_bytes(saved.replace(a.encode(), b"w7:\\ud800"))          # a lone surrogate in a pane id
    rc, out, err = world.hoh("reap", "--project", "p")                    # ... is printed as ASCII JSON
    assert rc == 0 and out.isascii() and "ud800" in out, (rc, err)
    assert json.loads(out)["surfaces"][0]["outcome"] == "would_release" and world.acted() == []
    record.write_bytes(saved)
    assert world.rows()[a]["outcome"] == "would_close"                  # control: a good pair closes
    limit = surfacebook.RECORD_LIMIT                                    # the ceiling, both sides

    def padded(text: bytes, size: int) -> bytes:
        """The one-line JSON `text` followed by spaces and a newline: exactly `size` bytes."""
        body = text.rstrip(b"\n")
        return body + b" " * (size - len(body) - 1) + b"\n"

    record.write_bytes(padded(saved, limit))
    assert record.stat().st_size == limit
    assert world.rows()[a]["outcome"] == "would_close"                  # exactly at it: read
    record.write_bytes(padded(saved, limit + 1))
    assert record.stat().st_size == limit + 1
    nothing_closed("record: one byte over the ceiling")
    over = world.rows()
    assert [r["outcome"] for r in over.values()] == ["refused"]
    assert "larger than" in next(iter(over.values()))["why"]
    record.write_bytes(saved)
    events.write_bytes(padded(release_s1, limit))
    assert world.rows()[a]["outcome"] == "would_close"                  # a release at the ceiling
    events.write_bytes(padded(release_s1, limit + 1))
    nothing_closed("events: one byte over the ceiling")
    events.write_bytes(release_s1)
    side = parked(record, lambda: os.symlink(world.dirs, record))
    nothing_closed("record: a link")
    os.rename(record, book / "record.link")
    os.symlink(book / "no-such-file", record)
    nothing_closed("record: a dangling link")
    os.rename(record, book / "record.dangling")
    os.mkdir(record)
    nothing_closed("record: a directory")
    os.rename(record, book / "record.dir")
    os.mkfifo(record)
    nothing_closed("record: a FIFO")
    os.rename(record, book / "record.fifo")
    os.rename(side, record)
    os.link(record, book / "record.second-name")                        # a second name
    nothing_closed("record: a hard link")
    os.rename(record, book / "record.two-names")                        # set both names aside,
    record.write_bytes(saved)                                           # and write a fresh record
    if os.geteuid() != 0:                                                # root reads mode 000
        os.chmod(record, 0)
        try:
            nothing_closed("record: mode 000")
        finally:
            os.chmod(record, 0o644)
    # 3. the lifecycle fact cannot be had: the strand is merged but the repository is gone
    events.write_bytes(b"")
    git(world.repo, "merge", "-q", "--no-ff", "-m", "m", "feat")
    os.rename(world.repo, world.tmp_path / "proj-away")
    rows = world.rows(apply=True)
    assert rows[a]["outcome"] == "pending" and "a merge cannot be judged" in rows[a]["why"], rows
    assert world.acted() == []
    os.rename(world.tmp_path / "proj-away", world.repo)
    assert world.rows(apply=True)[a]["outcome"] == "closed"              # control: the fact, a close


def test_the_commands_their_spellings_and_the_outside_of_herdr(world, monkeypatch):
    """Catches: a sweep that needs Herdr when there is nothing to judge, one that runs outside
    Herdr, a runs root that is a link followed, a flag spelled two ways giving two answers.
    Mutants: a symlinked runs root accepted; `--strand=value` not equal to `--strand value`."""
    rc, out, err = world.hoh("reap", "--project", "p")                 # no book yet: nothing to judge
    assert rc == 0 and json.loads(out)["surfaces"] == [] and world.controls == 0, err
    rc, out, err = world.hoh("reap", "--project", "nope")
    assert (rc, out) == (2, "") and err.startswith("reap refused: "), err
    a = world.reviewer("s1", "a")
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "done")
    spaced = world.rows("--strand", "s1")
    eq = world.rows("--strand=s1")
    assert spaced == eq and set(spaced) == {a}
    base = world.tmp_path / "runs-base"                                 # a directory of runs roots
    base.mkdir()
    c1, c2, plain = base / "20261001T0000Z-x-runs", base / "20261001T0001Z-y-runs", base / "plain-run"
    for folder in (c1, c2, plain):
        folder.mkdir()
    w1, p1 = world.workspace("81", root=c1)
    w2, p2 = world.workspace("82", root=c2)
    world.run_dir("r", root=c1)
    world.run_dir("r", root=c2)
    (plain / surfaces.SURFACES_FILE).write_text(json.dumps({             # an I10a run host: no `book`
        "v": 1, "at": "t", "event": "surface", "run_id": "plain-run", "program": "p",
        "role": "program:run-host", "backend": "fake", "pane_id": "w9:p9", "tab_id": "w9:t9",
        "cwd": str(world.dirs)}) + "\n", encoding="utf-8")
    os.symlink(c1, base / "linked-root")                                 # a link is not followed
    rc, doc, err = world.sweep("--runs-root", str(base))
    assert rc == 0, err
    assert sorted(h for h in doc["homes"] if h.startswith(str(base))) == [str(c1), str(c2)], doc["homes"]
    assert {r["pane_id"]: r["outcome"] for r in doc["surfaces"] if r["workspace_id"]} == {
        p1: "would_close", p2: "would_close"}, doc["surfaces"]
    away = world.tmp_path / "book-away"                                  # the book swapped for a link
    os.rename(world.book, away)
    os.symlink(away, world.book)
    for argv in (("reap", "--project", "p", "--apply"),
                 ("surface", "release", "--project", "p", "--strand", "s1", "--reason", "x")):
        rc, out, err = world.hoh(*argv)
        assert (rc, out) == (2, "") and "not the registered surface book" in err, (argv, err)
    rc, out, err = world.register(world.agent("vh-s1-rev-z"), perspective="z")
    assert (rc, out) == (2, "") and "not the registered surface book" in err, err
    os.rename(world.book, world.tmp_path / "book-link")
    os.rename(away, world.book)
    assert world.acted() == []
    link = world.tmp_path / "linked-root"
    os.symlink(world.root, link)
    rc, out, err = world.hoh("reap", "--project", "p", "--runs-root", str(link))
    assert (rc, out) == (2, "") and "is not a plain directory" in err, err
    rc, out, err = world.hoh("reap", "--project", "p", "--runs-root", str(world.tmp_path / "none"))
    assert (rc, out) == (2, ""), err
    state = str(world.reg.state_root)
    monkeypatch.setenv("HOH_STATE_ROOT", str(world.tmp_path / "not-the-state-root"))
    for argv in (("reap", "--project", "p"), ("surface", "release", "--project", "p", "--strand",
                                              "s1", "--reason", "r"),
                 ("surface", "register", "--project", "p", "--kind", "orchestrator", "--pane", a)):
        world.roots_seen.clear()
        world.hoh(*argv, "--storage-root", state)
        assert world.roots_seen == [state], (argv, world.roots_seen)       # the option, not the environment
    monkeypatch.setenv("HOH_STATE_ROOT", state)
    before = world.tree_files()
    monkeypatch.setattr(surfaces, "make_control", REAL_MAKE_CONTROL)     # outside Herdr
    rc, out, err = world.hoh("reap", "--project", "p", "--apply")
    assert rc == 2 and out == "" and err.startswith("reap failed: "), (rc, out, err)
    rc, out, err = world.register(a)
    assert (rc, out) == (2, "") and err.startswith("surface register refused: "), err
    assert world.tree_files() == before and a in world.fake.panes


def test_the_module_removes_nothing_and_spells_no_herdr_command():
    """Catches: a cleanup, a removal, a truncate or a Herdr command line added to the book. Mutant:
    an `os.unlink` call (a rename stands in for it in the mutant file)."""
    need_the_module()
    path = SRC / "surfacebook.py"
    text = path.read_text(encoding="utf-8")
    assert text.isascii()
    tree = ast.parse(text)
    forbidden = {"remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace",
                 "truncate", "ftruncate", "rmtree", "copyfile", "move", "write_text",
                 "write_bytes", "symlink", "link", "kill", "killpg", "chmod"}
    calls = sorted({n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr in forbidden})
    assert calls == [], f"surfacebook calls {calls}"
    bare = sorted({n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id in {"open", "exec", "eval"}})
    assert bare == [], bare
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree)
                                     if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"shutil", "signal", "socket", "urllib", "http", "tempfile"}, imported
    spelled = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.List) and n.elts
               and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == "herdr"]
    assert spelled == [], spelled
    flags = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in {"mkdir", "makedirs", "mkfifo"}]
    assert flags == [], "the book creates no directory itself: the registry does"


def test_close_workspace_is_an_owned_verb_that_spells_one_command(tmp_path):
    """Catches: a workspace closed that the registry does not own, one that holds a stranger's
    pane, one the snapshot does not show, a runtime without the verb asked anyway. Mutants: the
    ownership check dropped; the stranger check dropped."""
    wire: list[list[str]] = []
    adapter = agentcontrol.HerdrAgentControl(lambda argv, purpose, text=False: wire.append(argv) or {})
    adapter.close_workspace("w1")
    assert wire == [["herdr", "workspace", "close", "w1"]]
    fake_rt = FakeHerdr()
    mine = fake_rt.add_pane("w5", tmp_path)
    other = fake_rt.add_pane("w5", tmp_path)
    solo = fake_rt.add_pane("w6", tmp_path)
    registry = agentcontrol.OwnedAgentRegistry(tmp_path / "owned.jsonl", run_id="r")
    owned = agentcontrol.OwnedAgentControl(fake_rt, registry)
    for pane in (mine, solo):
        registry.adopt(agentcontrol.ProgramAgentRole.program("x"), program="p", backend="fake",
                       pane_id=pane, tab_id=fake_rt.panes[pane]["tab_id"], name="", cwd=str(tmp_path))
    with pytest.raises(agentcontrol.NotOwned):                          # w5 holds `other`
        owned.close_workspace("w5")
    with pytest.raises(agentcontrol.NotOwned):                          # not shown by the snapshot
        owned.close_workspace("w99")
    with pytest.raises(agentcontrol.NotOwned):
        owned.close_workspace("")
    assert [c for c in fake_rt.calls if c[0] == "close_workspace"] == []
    owned.close_workspace("w6", purpose="x")
    assert [c[:2] for c in fake_rt.calls if c[0] == "close_workspace"] == [("close_workspace", "w6")]
    assert solo not in fake_rt.panes and other in fake_rt.panes

    class NoVerb:
        backend = "x"
        def snapshot(self):
            return {"panes": [{"pane_id": "w8:p1", "tab_id": "w8:t1", "workspace_id": "w8"}],
                    "tabs": [], "agents": []}
    reg2 = agentcontrol.OwnedAgentRegistry(tmp_path / "owned2.jsonl", run_id="r")
    reg2.adopt(agentcontrol.ProgramAgentRole.program("x"), program="p", backend="x",
               pane_id="w8:p1", tab_id="w8:t1", name="", cwd=str(tmp_path))
    with pytest.raises(DispatchError, match="cannot close a workspace"):
        agentcontrol.OwnedAgentControl(NoVerb(), reg2).close_workspace("w8")


def test_the_world_fails_closed_without_the_isolation(tmp_path, fake, monkeypatch, capsys):
    """Catches: a world built without the isolation fixture (a star import skips names that start
    with an underscore), reaching the real Herdr seam. Mutant: the check dropped."""
    ISOLATION["in_force"] = False
    with pytest.raises(pytest.fail.Exception, match="isolation fixture is not in force"):
        World(tmp_path, fake, monkeypatch, capsys)
    ISOLATION["in_force"] = True
    built = World(tmp_path, fake, monkeypatch, capsys)
    assert surfaces.make_control is not REAL_MAKE_CONTROL
    assert built.fake is surfaces.make_control()


# --------------------------------------------------------------------------- #
# tabreapf: the repair of the accepted TR1 candidate (integration reviews tabreap-int-a/-b)
# --------------------------------------------------------------------------- #


def _second_project(world, name="q"):
    """Project `name`: a clone of the world's repository (the candidates exist there too) with
    its own book. Returns the book directory of that project (created at its first surface)."""
    other = world.tmp_path / f"proj-{name}"
    git(world.tmp_path, "clone", "-q", str(world.repo), str(other))
    world.reg.register_project(name, other)
    return world.reg.state_root / "projects" / name / "receipts" / "surfaces"


def _register_in(world, project, pane, strand, perspective="a", kind="reviewer", **extra):
    argv = ["surface", "register", "--project", project, "--kind", kind, "--pane", pane,
            "--strand", strand, "--perspective", perspective]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    rc, out, err = world.hoh(*argv)
    assert rc == 0, err


def test_one_projects_release_or_merge_never_makes_another_homes_reviewer_closable(world):
    """Catches (integration review tabreap-int-a, B-1): the Rule judged the reviewer and drafter
    lines of EVERY home a sweep discovered by the strand events and the repository of the project
    given, so `hoh surface release --project p --strand s1` and then `hoh reap --project p
    --runs-root <another project's book> --apply` closed that project's reviewer of ITS strand
    `s1`; a merge in p's repository did the same for a candidate both repositories hold. A
    reviewer or drafter line is honoured only in the project's own book. Mutants: every home's
    lines are judged by the project given; the home is compared with the book by its last name."""
    qbook = _second_project(world)
    pq1 = world.agent("vh-q-s1-rev-a")
    pq2 = world.agent("vh-q-s2-rev-a")
    pqd = world.agent("vh-q-s1-dra-w")                                   # a drafter of q's s1
    _register_in(world, "q", pq1, "s1")
    _register_in(world, "q", pqd, "s1", perspective="w", kind="drafter")
    _register_in(world, "q", pq2, "s2", candidate=world.cands["feat"])
    mine1 = world.reviewer("s1", "a")                                    # p's own, strand s1
    mine2 = world.reviewer("s2", "a", candidate=world.cands["feat"])     # p's own, strand s2
    tabs = {world.fake.panes[p]["tab_id"] for p in (mine1, mine2)}
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "p's own")
    world.merge("feat")                                                  # in p's repository only
    assert qbook.is_dir() and qbook != world.book and qbook.name == world.book.name
    for named in (str(qbook), str(qbook.parent)):                        # the book, its parent
        rows = world.rows("--runs-root", named)
        assert rows[mine1]["outcome"] == rows[mine2]["outcome"] == "would_close", rows
        for pane in (pq1, pqd, pq2):
            assert rows[pane]["outcome"] == "pending", rows
            assert "own surface book" in rows[pane]["why"], rows
    alias = str(world.book / ".." / "surfaces")                          # the project's own book,
    rc, doc, err = world.sweep("--runs-root", alias)                     # spelled another way: one home
    assert doc["homes"] == [str(world.book)], doc["homes"]
    assert [r["pane_id"] for r in doc["surfaces"]].count(mine1) == 1, doc["surfaces"]
    world.rows("--runs-root", str(qbook.parent), apply=True)
    assert {c[1] for c in world.acted()} == tabs, world.acted()          # p's two, nothing of q's
    assert {pq1, pqd, pq2} <= set(world.fake.panes), "another project's reviewer or drafter closed"
    # without a project (no book, no repository) nothing of q's is judged either; nor at the end of
    # a `hoh run` whose runs root is another project's book
    rc, out, err = world.hoh("reap", "--runs-root", str(qbook.parent), "--apply")
    assert rc == 0 and {pq1, pqd, pq2} <= set(world.fake.panes), (out, err)
    surfacebook.reap_root_after_run(qbook)
    assert {pq1, pqd, pq2} <= set(world.fake.panes)
    # the control: q's own release, swept as q: its reviewer of s1 closes, s2 (not merged in q) stays
    rc, out, err = world.hoh("surface", "release", "--project", "q", "--strand", "s1",
                             "--reason", "q's own")
    assert rc == 0, err
    rc, out, err = world.hoh("reap", "--project", "q", "--apply")
    by_pane = {r["pane_id"]: r["outcome"] for r in json.loads(out)["surfaces"]}
    assert by_pane[pq1] == by_pane[pqd] == "closed" and by_pane[pq2] == "pending", out
    assert not ({pq1, pqd} & set(world.fake.panes)) and pq2 in world.fake.panes


def test_a_pane_the_book_owns_is_never_a_run_workspace(world, monkeypatch):
    """Catches (tabreap-int-a, M-1): an orchestrator registered in the book whose pane also reached
    a runs root's record (a concurrent creation in the place of a target, the named limit of 7.4)
    was closed by `hoh reap --project p --runs-root R --apply`: the book was never consulted for a
    run workspace. Now: the sweep that knows the project refuses to treat a pane its book owns as a
    run workspace, and `hoh worktree` does not record a workspace that holds the caller's own pane
    or tab. Mutants: the book is not consulted for run workspaces; the caller's pane is recorded."""
    import copy
    orch = world.agent("vh-orchestrator-1", ws="w90")
    caller_dir = world.dirs / "caller"
    caller_dir.mkdir()
    caller = world.fake.add_pane("w99", caller_dir)
    world.procs.add_shell(caller)
    monkeypatch.setenv("HERDR_PANE_ID", caller)
    monkeypatch.setenv("HERDR_TAB_ID", world.fake.panes[caller]["tab_id"])
    rc, out, err = world.hoh("surface", "register", "--project", "p", "--kind", "orchestrator",
                             "--pane", orch)
    assert rc == 0, err
    world.run_dir("r1")                                                  # an ended run
    after = world.fake.snapshot()
    before = copy.deepcopy(after)
    before["panes"], before["agents"] = [], []
    only = copy.deepcopy(after)
    only["panes"] = [r for r in after["panes"] if r["pane_id"] == orch]
    only["agents"] = []
    # the orchestrator's pane "appears" between the two snapshots of a creation: recorded, as
    # the snapshots alone cannot tell (7.4) ...
    records, notes = surfacebook.register_workspaces(
        root=world.root, repo=str(world.dirs), worktree=str(world.dirs / "nowhere"),
        before=before, after=only, backend=world.fake.backend)
    assert [r["pane_id"] for r in records] == [orch], (records, notes)
    # ... and the sweep that knows the project does not close it
    for apply in (False, True):
        rc, doc, err = world.sweep("--runs-root", str(world.root), apply=apply)
        mine = [r for r in doc["surfaces"] if r["pane_id"] == orch]
        assert {r["kind"] for r in mine} == {"orchestrator", "run-workspace"}, mine
        assert all(r["outcome"] in ("pending", "refused") for r in mine), mine
        ws = next(r for r in mine if r["kind"] == "run-workspace")
        assert "surface book" in ws["why"] and "orchestrator" in ws["why"], ws
    assert world.acted() == [] and orch in world.fake.panes
    # a book line that does not parse (version skew, a hand edit) is still the book's: an unknown
    # kind, a round out of range, a strand off its pattern, a `book` that is no object
    book_file = world.book / surfaces.SURFACES_FILE
    good_text = book_file.read_text(encoding="utf-8")
    for what, edit in {"unknown kind": lambda b: b.update(kind="weird"),
                       "round 1001": lambda b: b.update(round=1001),
                       "strand off the pattern": lambda b: b.update(strand="Bad Strand")}.items():
        lines = [json.loads(ln) for ln in good_text.splitlines()]
        edit(next(ln for ln in lines if ln["pane_id"] == orch)["book"])
        book_file.write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")
        assert orch not in world.books(), what                         # it really does not parse
        rc, doc, err = world.sweep("--runs-root", str(world.root), apply=True)
        ws = [r for r in doc["surfaces"] if r["kind"] == "run-workspace" and r["pane_id"] == orch]
        assert [r["outcome"] for r in ws] == ["pending"], (what, doc)
        assert "surface book" in ws[0]["why"] and "unreadable line" in ws[0]["why"], (what, ws)
        assert world.acted() == [] and orch in world.fake.panes, what
    book_file.write_text(good_text, encoding="utf-8")
    # `hoh worktree` itself never records a workspace that holds the caller's pane or tab
    monkeypatch.setenv("HERDR_ENV", "1")
    target, dev = world.dirs / "target", world.dirs / "dev"
    target.mkdir()
    dev.mkdir()
    other_root = world.tmp_path / "runs2"
    other_root.mkdir()
    first = f"w60:p{world.fake._n + 1}"                                  # the target workspace's pane
    monkeypatch.setenv("HERDR_PANE_ID", first)
    monkeypatch.delenv("HERDR_TAB_ID")
    _make_worktree(world, monkeypatch, [("w60", 1, target), ("w61", 1, dev)])
    rc, out, err = world.hoh("--root", str(other_root), "worktree", "--repo", str(target),
                             "--branch", "loop/c")
    assert rc == 0, err
    got = json.loads(out)["workspaces"]
    assert [w["workspace_id"] for w in got] == ["w61"], (got, err)
    assert "the caller's own" in err, err
    # ... nor one that holds the caller's TAB (the caller's pane is elsewhere)
    tab_dir = world.tmp_path / "runs3"
    tab_dir.mkdir()
    n = world.fake._n
    monkeypatch.setenv("HERDR_PANE_ID", caller)
    monkeypatch.setenv("HERDR_TAB_ID", f"w62:t{n + 1}")                  # the target workspace's tab
    _make_worktree(world, monkeypatch, [("w62", 1, target), ("w63", 1, dev)])
    rc, out, err = world.hoh("--root", str(tab_dir), "worktree", "--repo", str(target),
                             "--branch", "loop/t")
    assert rc == 0, err
    assert [w["workspace_id"] for w in json.loads(out)["workspaces"]] == ["w63"], (out, err)
    assert "the caller's own" in err, err
    # a book that cannot be read cannot show that a pane is not its own: nothing is closed on it
    book_file = world.book / surfaces.SURFACES_FILE
    os.replace(book_file, world.book / "owned-surfaces.parked")
    os.mkfifo(book_file)
    world.reset()
    rc, doc, err = world.sweep("--runs-root", str(world.root), apply=True)
    ws = [r for r in doc["surfaces"] if r["kind"] == "run-workspace" and r["pane_id"] == orch]
    assert [r["outcome"] for r in ws] == ["pending"] and "cannot be read" in ws[0]["why"], doc
    assert world.acted() == [] and orch in world.fake.panes


def test_a_hostile_sibling_run_directory_neither_hangs_the_reap_nor_is_read(world):
    """Catches (tabreap-int-a, M-2): a sibling run directory whose `state.json` or `.process.lock`
    is a FIFO blocked `hoh reap` and the end of `hoh run` of its root (`surfaces.run_over` and
    `RunStore.running_process` open them unguarded), and a link or a huge file was read. Every
    file read for a sibling run is first a regular file, no link, of a bounded size; anything else
    is a `refused` row with the run's name (exit 1), nothing is closed and nothing blocks. A
    regular file that cannot be OPENED (mode 000: `RunStore.running_process` reads an unopenable
    lock as "no holder", a fail-open) is refused as well; one that opens but cannot be parsed
    (non-UTF-8, NUL, corrupt, array, empty) is the run not being over: `pending`, no hang.
    Mutants: the guard is dropped; the size bound is exclusive; a link is followed; readability is
    not asked."""
    import signal

    class Hang(BaseException):
        pass

    def alarm(seconds):
        def handler(signum, frame):
            raise Hang("blocked on a sibling run directory")
        signal.signal(signal.SIGALRM, handler)
        signal.alarm(seconds)

    limit = getattr(surfacebook, "STATE_JSON_LIMIT", 4 << 20)         # 4 MiB, pinned by 7.9
    assert limit == 4 << 20
    n = [0]

    def home():
        n[0] += 1
        root = world.tmp_path / f"runs-h{n[0]}"
        root.mkdir()
        ws, pane = world.workspace(f"9{n[0]}", root=root)
        good = world.run_dir("good", root=root)                          # a run that is over
        bad = world.run_dir("bad", root=root)
        return root, pane, good, bad

    def hostile(bad, variant):
        if variant == "fifo state":
            os.replace(bad / "state.json", bad / "state.real")
            os.mkfifo(bad / "state.json")
        elif variant == "fifo lock":
            os.mkfifo(bad / ".process.lock")
        elif variant == "state is a link":
            os.replace(bad / "state.json", bad / "state.real")
            os.symlink(bad / "state.real", bad / "state.json")
        elif variant == "state is a directory":
            os.replace(bad / "state.json", bad / "state.real")
            os.mkdir(bad / "state.json")
        elif variant == "lock is a link":
            (bad / "lock.real").write_text("", encoding="utf-8")
            os.symlink(bad / "lock.real", bad / ".process.lock")
        elif variant == "state mode 000":
            os.chmod(bad / "state.json", 0)
        elif variant == "lock mode 000":                  # `running_process` reads an unopenable
            lock = bad / ".process.lock"                  # lock as "no holder": refuse it
            lock.write_text("", encoding="utf-8")
            os.chmod(lock, 0)
        elif variant == "state is a dangling link":
            os.replace(bad / "state.json", bad / "state.real")
            os.symlink(bad / "nowhere.json", bad / "state.json")
        elif variant == "state one byte over the bound":
            text = (bad / "state.json").read_text(encoding="utf-8")
            (bad / "state.json").write_text(text + " " * (limit + 1 - len(text)), encoding="utf-8")
            assert (bad / "state.json").stat().st_size == limit + 1

    for variant in ("fifo state", "fifo lock", "state is a link", "state is a dangling link",
                    "state is a directory", "lock is a link", "state one byte over the bound",
                    "state mode 000", "lock mode 000"):
        if variant.endswith("mode 000") and os.geteuid() == 0:
            continue                                                    # root opens it anyway
        root, pane, good, bad = home()
        hostile(bad, variant)
        world.reset()
        alarm(10)
        try:
            rc, doc, err = world.sweep("--runs-root", str(root), apply=True)
            surfacebook.reap_root_after_run(root)
        except Hang:
            pytest.fail(f"{variant}: the reap blocks on a hostile sibling run directory")
        finally:
            signal.alarm(0)
        row = next(r for r in doc["surfaces"] if r["pane_id"] == pane)
        assert row["outcome"] == "refused" and "run bad" in row["why"], (variant, row)
        if variant.endswith("mode 000"):
            assert "cannot be opened" in row["why"], (variant, row)
        assert rc == 1 and world.acted() == [] and pane in world.fake.panes, variant
        note = world.capsys.readouterr().err
        assert "workspace" in note and "run bad" in note, (variant, note)
    # a regular file that cannot be parsed is the run's own business (not over), never a hang
    for variant, content in {"corrupt": b'{"run_id": "bad"', "array": b"[]", "non-utf8": b"\xff\xfe{}",
                             "nul": b'{"run_id": "ba\x00d"}', "empty": b""}.items():
        root, pane, good, bad = home()
        (bad / "state.json").write_bytes(content)
        alarm(10)
        try:
            rows = world.rows("--runs-root", str(root), apply=True)
            surfacebook.reap_root_after_run(root)
        except Hang:
            pytest.fail(f"{variant}: the reap blocks")
        finally:
            signal.alarm(0)
        assert rows[pane]["outcome"] == "pending", (variant, rows[pane])
        assert "run bad" in rows[pane]["why"] and pane in world.fake.panes, variant
    world.capsys.readouterr()
    # the bound, exactly: a state.json of exactly STATE_JSON_LIMIT bytes is read and is over
    root, pane, good, bad = home()
    text = (bad / "state.json").read_text(encoding="utf-8")
    (bad / "state.json").write_text(text + " " * (limit - len(text)), encoding="utf-8")
    assert (bad / "state.json").stat().st_size == limit
    rows = world.rows("--runs-root", str(root))
    assert rows[pane]["outcome"] == "would_close", rows


def test_the_default_path_of_reap_is_the_old_behaviour_and_a_rule_checks_the_agent_name(world):
    """Catches (tabreap-int-a, M-3): spec 6 and 7.6 promise that `surfaces.reap` without a
    keyword is byte for byte the old reaper; the identity check of the recorded agent name ran
    there too, so a record with a name and another idle agent in its pane was `would_close` on the
    base and `refused` on the candidate. The check belongs to a rule. Mutants: the name is compared
    on the default path (the pin's behaviour); the name is not compared under a rule (M4)."""
    from hoh.agentcontrol import ProgramAgentRole
    world.run_dir("r-default")
    run_dir = world.root / "r-default"
    folder = world.dirs / "d"
    folder.mkdir()
    pane = world.fake.add_pane("w5", folder, agent="intruder", status="idle")
    world.procs.add_shell(pane)
    surfaces.registry_for(run_dir).adopt(
        ProgramAgentRole.program("run-host"), program="p", backend=world.fake.backend,
        pane_id=pane, tab_id=world.fake.panes[pane]["tab_id"], name="registered-name",
        cwd=str(folder))
    plain = surfaces.reap(run_dir, close=False)
    assert [r["outcome"] for r in plain] == ["would_close"], plain       # the old reaper's answer
    ruled = surfaces.reap(run_dir, close=False, over_for=lambda home, surface: (True, "over"))
    assert [r["outcome"] for r in ruled] == ["refused"], ruled           # a rule: the pane is not ours
    assert "not the one that was registered" in ruled[0]["why"]
    assert world.acted() == []


def test_a_strand_filter_limits_what_is_printed_and_the_exit_code_to_that_strand(world):
    """Catches (tabreap-int-a/-b, MINOR): `hoh reap --strand S` appended the `refused` row of a
    home that cannot be read before the filter, so a strand-limited sweep (the merge step) printed
    and failed (exit 1) for a broken home that has nothing to do with its strand (spec 7.7: the
    flag limits what is printed). The home is still named under `homes`; without `--strand` it is
    a `refused` row; the project's OWN book that cannot be read is a `refused` row whatever the
    flag says. Mutants: the filter is not applied to a refused home row; it hides the own book."""
    a = world.reviewer("s1", "a")
    world.hoh("surface", "release", "--project", "p", "--strand", "s1", "--reason", "done")
    broken = world.tmp_path / "runs-broken"
    broken.mkdir()
    os.mkfifo(broken / surfaces.SURFACES_FILE)                           # a record that cannot be read
    rc, doc, err = world.sweep("--runs-root", str(broken))
    assert rc == 1 and any(r["outcome"] == "refused" and r["home"] == str(broken)
                           for r in doc["surfaces"]), (rc, doc)
    rc, doc, err = world.sweep("--runs-root", str(broken), "--strand", "s1")
    assert rc == 0, (rc, err)
    assert [r["pane_id"] for r in doc["surfaces"]] == [a], doc          # that strand only
    assert str(broken) in doc["homes"]                                   # the home is still named
    rc, doc, err = world.sweep("--runs-root", str(broken), "--strand=s1", apply=True)
    assert rc == 0 and [r["outcome"] for r in doc["surfaces"]] == ["closed"], (rc, doc)
    # the project's OWN book that cannot be read is never hidden by the filter: the merge step
    # (`--strand <id>`) must be able to tell it from "nothing to close"
    book_file = world.book / surfaces.SURFACES_FILE
    os.replace(book_file, world.book / "owned-surfaces.parked")
    os.mkfifo(book_file)
    for flag in ("--strand", "--strand=s9"):
        argv = ["s9"] if flag == "--strand" else []
        rc, doc, err = world.sweep(flag, *argv, apply=True)
        own = [r for r in doc["surfaces"] if r["home"] == str(world.book)]
        assert rc == 1 and [r["outcome"] for r in own] == ["refused"], (flag, rc, doc)


# --------------------------------------------------------------------------- #
# tabreapf2: a book that is damaged cannot show that a pane is not its own
# (integration review tabreapf-int-a r2, F-1; probes test_f3 and test_f4 of that review)
# --------------------------------------------------------------------------- #


def _orchestrator_in_a_runs_root(world, tag):
    """An orchestrator registered in the project's book whose pane is ALSO recorded as a run workspace
    of a runs root whose run has ended (the named limit of 7.4: a concurrent creation in the place
    of a target). Returns (its pane id, the runs root)."""
    import copy
    orch = world.agent(f"vh-orch-{tag}", ws=f"w9{tag}")
    caller_dir = world.dirs / f"caller-{tag}"
    caller_dir.mkdir()
    caller = world.fake.add_pane(f"w99{tag}", caller_dir)
    world.procs.add_shell(caller)
    world.monkeypatch.setenv("HERDR_PANE_ID", caller)                    # the caller is another pane
    world.monkeypatch.setenv("HERDR_TAB_ID", world.fake.panes[caller]["tab_id"])
    rc, out, err = world.hoh("surface", "register", "--project", "p", "--kind", "orchestrator",
                             "--pane", orch)
    assert rc == 0, err
    root = world.tmp_path / f"runs-{tag}"
    root.mkdir()
    world.run_dir("r1", root=root)                                       # an ended run
    after = world.fake.snapshot()
    before = copy.deepcopy(after)
    before["panes"], before["agents"] = [], []
    only = copy.deepcopy(after)
    only["panes"] = [r for r in after["panes"] if r["pane_id"] == orch]
    only["agents"] = []
    records, notes = surfacebook.register_workspaces(
        root=root, repo=str(world.dirs), worktree=str(world.dirs / "nowhere"),
        before=before, after=only, backend=world.fake.backend)
    assert [r["pane_id"] for r in records] == [orch], (records, notes)
    return orch, root


def _run_workspace_row(world, root, orch, apply):
    rc, doc, err = world.sweep("--runs-root", str(root), apply=apply)
    rows = [r for r in doc["surfaces"] if r["pane_id"] == orch and r["kind"] == "run-workspace"]
    assert len(rows) == 1, doc
    return rows[0]


def test_a_damaged_book_line_of_the_owner_does_not_unguard_its_run_workspace(world):
    """Catches (tabreapf-int-a r2, F-1; the reviewer's test_f3): `OwnedAgentRegistry.agents()` drops
    silently a book line that is not JSON, is truncated, is not UTF-8, has a role that does not
    parse, another `run_id`, an unknown event or a `pane_id` that is no string; the orchestrator's
    pane was then "not the book's" and its run workspace (the named limit of 7.4) was closed. A
    book with a dropped line it cannot attribute to another pane cannot show that a pane is not its
    own; a dropped line that names the pane leaves the pane the book's. Mutants: the registry's fold
    alone is asked (the previous revision); bytes that are not UTF-8 are replaced, not refused; a
    line that is not JSON is skipped; a line with a pane id that is no string is skipped; a dropped
    line that names the pane is not looked at."""
    orch, root = _orchestrator_in_a_runs_root(world, "d")
    book_file = world.book / surfaces.SURFACES_FILE
    good = book_file.read_bytes()
    assert orch.encode() in good

    def own(line: str) -> bool:
        return json.loads(line).get("pane_id") == orch and json.loads(line).get("event") == "adopt"

    variants = {
        "truncated json": lambda t: t[: len(t) // 2],
        "garbage": lambda t: "}{ not json",
        "control characters": lambda t: "\x00\x01\x02",
        "role does not parse": lambda t: t.replace("program:orchestrator", "program:Bad Role!"),
        "another run id": lambda t: t.replace('"run_id": "surfaces"', '"run_id": "other"'),
        "unknown event": lambda t: t.replace('"event": "adopt"', '"event": "weird"'),
        "pane id is a list": lambda t: t.replace('"pane_id": "', '"pane_id": ["').replace(
            '", "program"', '"], "program"'),
        "pane id is a number": lambda t: t.replace(f'"pane_id": "{orch}"', '"pane_id": 7'),
        "line is an array": lambda t: "[1, 2]",
        "line is a string": lambda t: '"adopt"',
        "no pane id": lambda t: json.dumps({k: v for k, v in json.loads(t).items() if k != "pane_id"}),
        # the registry drops an `agent` / `release` of a pane no claim registered before it:
        # the orchestrator's own registration, with only its event rewritten, is such a line
        "event is agent": lambda t: t.replace('"event": "adopt"', '"event": "agent"'),
        "event is release": lambda t: t.replace('"event": "adopt"', '"event": "release"'),
        "event agent of another run": lambda t: t.replace('"event": "adopt"', '"event": "agent"').replace(
            '"run_id": "surfaces"', '"run_id": "other"'),
        "event release of another run": lambda t: t.replace(
            '"event": "adopt"', '"event": "release"').replace('"run_id": "surfaces"', '"run_id": "other"'),
        "registration lost, a release of the pane left": lambda t: json.dumps(
            {"at": "t", "v": 1, "event": "release", "pane_id": orch, "run_id": "surfaces",
             "reason": "closed"}),
        # the empty pane id cannot be attributed to any pane: alone and with the events that
        # an exemption of `agent` / `release` used to wave through
        "pane id is empty": lambda t: t.replace(f'"pane_id": "{orch}"', '"pane_id": ""'),
        "pane id empty, event agent": lambda t: t.replace(f'"pane_id": "{orch}"', '"pane_id": ""').replace(
            '"event": "adopt"', '"event": "agent"'),
        "pane id empty, event release": lambda t: t.replace(f'"pane_id": "{orch}"', '"pane_id": ""').replace(
            '"event": "adopt"', '"event": "release"'),
        # an event that is no string (unhashable): refused by name, not by a crash of the rule
        "event is a list": lambda t: t.replace('"event": "adopt"', '"event": ["adopt"]'),
        "event is an object": lambda t: t.replace('"event": "adopt"', '"event": {"a": 1}'),
        "event is missing": lambda t: json.dumps({k: v for k, v in json.loads(t).items() if k != "event"}),
    }
    for what, edit in variants.items():
        lines = good.decode("utf-8").splitlines()
        lines = [edit(ln) if own(ln) else ln for ln in lines]
        book_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for apply in (False, True):
            row = _run_workspace_row(world, root, orch, apply)
            assert row["outcome"] == "pending", (what, row)
            assert "surface book is damaged" in row["why"], (what, row)
            assert "terminal rule failed" not in row["why"], (what, row)     # a named reason
        assert world.acted() == [] and orch in world.fake.panes, what
    # a byte that is not UTF-8 inside the pane id: the fold reads "w9d:\ufffdp.." (not the pane)
    mangled = good.replace(orch.encode(), orch.encode()[:-1] + b"\xff", 1)
    assert mangled != good
    book_file.write_bytes(mangled)
    for apply in (False, True):
        row = _run_workspace_row(world, root, orch, apply)
        assert row["outcome"] == "pending" and "not UTF-8" in row["why"], row
    assert world.acted() == [] and orch in world.fake.panes
    book_file.write_bytes(good)                                          # the control: the book intact
    row = _run_workspace_row(world, root, orch, False)
    assert row["outcome"] == "pending" and "recorded in the project's surface book" in row["why"]


def test_a_book_that_cannot_be_read_closes_no_run_workspace(world):
    """Catches (tabreapf-int-a r2, the reviewer's test_f4): a whole book that is not UTF-8 read as
    "owns nothing" with no error, and every unreadable shape (mode 000, a FIFO, a directory, a link,
    a second hard link, one byte over the ceiling) must close nothing and never block. Mutants: the
    registry's fold alone is asked."""
    import signal

    class Hang(BaseException):
        pass

    def arm(seconds):
        def handler(signum, frame):
            raise Hang("blocked on the book")
        signal.signal(signal.SIGALRM, handler)
        signal.alarm(seconds)

    orch, root = _orchestrator_in_a_runs_root(world, "u")
    book_file = world.book / surfaces.SURFACES_FILE
    good = book_file.read_bytes()
    parked = world.book / "owned-surfaces.parked"
    os.replace(book_file, parked)
    shapes = ["not UTF-8", "mode 000", "fifo", "directory", "link", "second hard link", "huge",
              "line nested too deeply"]
    for what in shapes:
        if what == "mode 000" and os.geteuid() == 0:
            continue                                                     # root reads it anyway
        if what == "not UTF-8":
            book_file.write_bytes(b"\xff\xfe\x00\x01" * 50)
        elif what == "mode 000":
            book_file.write_bytes(good)
            book_file.chmod(0)
        elif what == "fifo":
            os.mkfifo(book_file)
        elif what == "directory":
            book_file.mkdir()
        elif what == "link":
            os.symlink(parked, book_file)
        elif what == "second hard link":
            book_file.write_bytes(good)
            os.link(book_file, world.book / "second-name")
        elif what == "huge":
            with open(book_file, "wb") as fh:
                fh.truncate((16 << 20) + 1)
        elif what == "line nested too deeply":                           # json.loads: RecursionError
            lines = good.decode("utf-8").splitlines()
            lines = ["[" * 100000 if orch in ln else ln for ln in lines]
            book_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        arm(15)
        try:
            for apply in (False, True):
                row = _run_workspace_row(world, root, orch, apply)
                assert row["outcome"] == "pending", (what, row)
                assert "surface book" in row["why"], (what, row)
        except Hang:
            pytest.fail(f"{what}: the sweep blocks on the book")
        finally:
            signal.alarm(0)
            if what == "mode 000":
                book_file.chmod(0o600)
        assert world.acted() == [] and orch in world.fake.panes, what
        if what == "second hard link":
            os.replace(world.book / "second-name", world.tmp_path / "second-name-parked")
        if what in ("directory",):
            os.rename(book_file, world.book / "dir-parked")
        else:
            os.replace(book_file, world.book / f"parked-{what.replace(' ', '-')}")


def test_a_damaged_book_does_not_block_what_it_does_not_touch(world):
    """Catches (the cost of the fix, tabreapf2): refusing every run workspace of a project because its
    book holds ANY dropped line (the reviewer's coarse proposal, `skipped > 0`) would let one old line
    of another run stop every cleanup for ever. A dropped line that names another pane by a string, an
    event of a pane nobody registered, a blank line and a release of the pane itself are not doubt.
    Mutants: any dropped line is doubt; a dropped line of another pane is doubt about this one."""
    orch, root = _orchestrator_in_a_runs_root(world, "c")
    ws, plain = world.workspace("77", root=root)                        # a run workspace the book never owned
    book_file = world.book / surfaces.SURFACES_FILE
    good = book_file.read_text(encoding="utf-8")
    foreign = json.loads(good.splitlines()[0])
    other = dict(foreign, pane_id="w0:p0", role="program:Bad Role!")      # another pane, role dropped
    stray = {"at": "t", "v": 1, "event": "release", "pane_id": "w0:p1", "run_id": "surfaces",
             "reason": "x"}                                              # an event of a pane nobody registered
    old_run = dict(foreign, pane_id="w0:p2", run_id="an-older-run")     # a line of another run
    book_file.write_text(good + "\n" + json.dumps(other) + "\n" + json.dumps(stray) + "\n"
                         + json.dumps(old_run) + "\n\n", encoding="utf-8")
    row = _run_workspace_row(world, root, plain, False)
    assert row["outcome"] == "would_close", row                          # not the book's, not in doubt
    assert _run_workspace_row(world, root, orch, False)["outcome"] == "pending"   # the owner: still the book's
    # a pane the book RELEASED is no longer its own: its run workspace is closable
    rc, out, err = world.hoh("surface", "release", "--project", "p", "--strand", "x", "--reason", "x")
    assert rc == 0, err
    lines = [json.loads(ln) for ln in book_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    release = {"at": "t", "v": 1, "event": "release", "pane_id": orch, "run_id": "surfaces",
               "reason": "gone"}
    book_file.write_text("".join(json.dumps(ln) + "\n" for ln in lines if ln.get("pane_id") != "w0:p0")
                         + json.dumps(release) + "\n", encoding="utf-8")
    assert _run_workspace_row(world, root, orch, False)["outcome"] == "would_close"
