"""P2-42, the controller wiring: `hoh run` for a registered project opens one
registry session, and every temporary workspace of the run -- the scratch
(``HOME``/``TMPDIR``) of each acceptance check, candidate and baseline, and the
temporary files `workspace.py` makes -- is a workspace of that session's
registered ``tmp`` root (`dogfood/p242-wire/spec.md`).

These tests drive the real `hoh run` (`cli.main`) and the real `Controller` with
a fake dispatcher: no Herdr, no agent, no host storage root. The registry is
`test_storage_tiers.World` (injected process table and boot id); the admission
is a recording double. Every temporary tree is under pytest's ``tmp_path``.
Fault cases come with a control that changes only the condition under test.

Mutant ids (``M..``) in a docstring name the spec's table of mutations the
integration review applies; each test says which it kills.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import types
import uuid
from pathlib import Path

import pytest

import hoh.storage_ephemeral as se
from hoh import cli, dispatchers, integrity
from hoh import controller as controller_module
from hoh import stages
from hoh import storage_registry as sr
from hoh.contracts import Budgets, Condition, Role, RunState
from hoh.controller import Controller, new_run_id
from hoh.timers import TimerStore
from hoh.storage_pressure import (GiB, GuestObserver, NoHost, PressureState, Refusal,
                                  StorageAdmission, StoragePolicy)
from hoh.store import RunStore
from test_storage_tiers import PROJECT, World, isolated_env  # noqa: F401  (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "hoh"
SPEC = "# Spec\ndone.txt must exist.\n"
CHECK = 'echo "TMPDIR=$TMPDIR HOME=$HOME"; test -f done.txt'


# --------------------------------------------------------------------------- #
# the world: a registry, a repository, a recording admission, a fake dispatcher
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def w(tmp_path) -> World:
    world = World(tmp_path / "w")
    world.fake.procs[os.getpid()] = 4242          # this process, as the registry's process table has it
    return world


def standalone_repo(base: Path) -> Path:
    """A repository of its own: no registered project's."""
    r = base / "project"
    r.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, capture_output=True)
    return r


def make_repo(base: Path, w: World, name: str = "project") -> Path:
    """The registered project's own repository: a linked worktree of the canonical repository the
    world registered as `PROJECT` (what a run of that project works in)."""
    r = base / name
    base.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(w.canon), "worktree", "add", "-q", "-b",
                    f"wt-{uuid.uuid4().hex[:8]}", str(r)], check=True, capture_output=True)
    return r


@pytest.fixture
def repo(tmp_path, w) -> Path:
    return make_repo(tmp_path, w)


class Admission:
    """Admits everything and writes down what was asked and what was given back;
    ``refuse_nth`` makes the n-th ``tmp_workspace`` reservation (1-based) refused
    with ``reason``."""

    def __init__(self, ledger: Path, *, refuse_nth: int | None = None,
                 reason: str = "state CRITICAL refuses every write-heavy operation",
                 raise_kind: str | None = None) -> None:
        self.ledger = ledger
        self.refuse_nth, self.reason = refuse_nth, reason
        self.raise_kind = raise_kind
        self.asked: list[str] = []
        self.projects: list[tuple[str, str]] = []
        self.open: dict[str, str] = {}
        self.released: list[str] = []
        self._n = 0

    def reserve(self, project_id, op_kind, *, estimate_bytes=None):
        self.asked.append(op_kind)
        self.projects.append((op_kind, project_id))
        if op_kind == self.raise_kind:
            raise OSError(5, "the ledger cannot answer (simulated)")
        if op_kind == "tmp_workspace":
            self._n += 1
            if self._n == self.refuse_nth:
                return Refusal(project_id=project_id, op_kind=op_kind, reason=self.reason,
                               state=PressureState.CRITICAL, effective_free=1 << 20, reserved=0,
                               estimate=1 << 30, reserve=30 << 30)
        grant = types.SimpleNamespace(admitted=True, id=f"res-{len(self.asked)}", op_kind=op_kind,
                                      estimate=estimate_bytes or 0)
        self.open[grant.id] = op_kind
        return grant

    def release(self, reservation, actual_bytes):
        self.open.pop(reservation.id)
        self.released.append(reservation.op_kind)

    def kinds(self, kind: str) -> int:
        return self.asked.count(kind)


class Fake:
    """Planner, developer and QA the test decides (a plan with one new check)."""

    def __init__(self, repo: Path, command: str = CHECK) -> None:
        self.repo = repo
        self.command = command
        self.calls: list[str] = []
        self.developer_raise: BaseException | None = None

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role.value)
        if role is Role.PLANNER:
            base = state.last_accepted_candidate or state.base_candidate
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
                "spec_digest": state.spec_digest, "objective": "add done.txt",
                "targets": ["app.py"], "preserve": [],
                "acceptance_checks": [{"check_id": "K1", "description": "done.txt exists",
                                       "command": self.command, "expect_exit": 0, "preserves": False}],
                "out_of_scope": [], "evidence_refs": [],
                "repair_only": False, "repair_reason": None})
        if role is Role.DEVELOPER:
            if self.developer_raise is not None:
                raise self.developer_raise
            (self.repo / "done.txt").write_text("ok\n", encoding="utf-8")
            return "done"
        return json.dumps({"verdicts": [{
            "check_id": "K1", "outcome": "PASS",
            "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1",
            "reproduction": "pytest", "note": "green"}], "open_gaps": [], "summary": "all good"})

    def follow_up(self, role, state, missing, *, charge):
        charge()
        raise missing

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def new_run(tmp_path: Path, repo: Path, *, project: str = PROJECT, run_id: str = "r1"):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    store = RunStore(tmp_path / "runs", run_id)
    state = Controller.new_state(run_id=run_id, repo_path=repo, project_name=project,
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)
    return spec, store, state


def through_the_cli(tmp_path, repo, w, monkeypatch, *, admission=None, fake=None,
                    project: str = PROJECT, run_id: str = "r1", prepared: bool = False,
                    extra: tuple = ()):
    """`hoh run` itself -- the real parser, `_cmd_run` and its abort handler -- on a fake
    dispatcher. The run is made here unless `prepared` (the caller made it). Returns
    (rc, store, admission, fake)."""
    if prepared:
        store = RunStore(tmp_path / "runs", run_id)
    else:
        spec, store, state = new_run(tmp_path, repo, project=project, run_id=run_id)
    admission = admission or Admission(tmp_path / "ledger")
    fake = fake or Fake(repo)
    monkeypatch.setattr(cli, "storage_admission_for",
                        lambda option, **kw: (admission, w.registry.state_root))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "StorageRegistry", lambda *a, **k: w.registry)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: fake)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", run_id, "--no-herdr",
                   "--iterations", "1", *extra])
    return rc, store, admission, fake


def documents(text: str) -> list[dict]:
    """The JSON documents `_print` wrote, in order."""
    out, decoder, i = [], json.JSONDecoder(), 0
    while i < len(text):
        if text[i] in " \n\r\t":
            i += 1
            continue
        try:
            doc, i = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find("\n", i) + 1 or len(text)
            continue
        out.append(doc)
    return out


def tmp_root(w: World, session: str) -> Path:
    return w.registry.state_root / "projects" / PROJECT / "tmp" / session


def records(root: Path) -> list[dict]:
    found = []
    for item in sorted(root.glob(".*.vh-ws.json")):
        found.append(json.loads(item.read_text(encoding="utf-8")))
    return found


def workspace_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def log_text(store: RunStore, receipt_id: str) -> str:
    return (store.logs_dir / f"{receipt_id}.txt").read_text(encoding="utf-8")


def beside_the_arenas(store: RunStore) -> list[str]:
    """Every name next to the arenas and in the arenas' root that is a runner scratch."""
    names = []
    for parent in {store.arenas_dir, store.arenas_dir.parent}:
        if parent.is_dir():
            names += [n for n in os.listdir(parent)
                      if n.endswith(".scratch") or n.startswith(".hoh-scratch-")]
    for parent in store.arenas_dir.rglob("*"):
        if parent.is_dir() and (parent.name.endswith(".scratch")
                                or parent.name.startswith(".hoh-scratch-")):
            names.append(parent.name)
    return sorted(set(names))


# --------------------------------------------------------------------------- #
# W1 -- `hoh run` for a registered project
# --------------------------------------------------------------------------- #


def test_hoh_run_puts_every_workspace_of_the_run_under_one_registered_session(
        tmp_path, repo, w, monkeypatch, capsys):
    """W1. Catches a run whose temporary state is not under a registered root (M1-M5).
    One real `hoh run` iteration (NONE) on a registered project: the output names
    the session and the project's ``tmp`` root; the ``$TMPDIR`` and ``$HOME`` the
    candidate check and the baseline check printed are two different workspaces of
    that root, named from the run; no ``*.scratch`` and no ``.hoh-scratch-*`` was made
    beside the arenas; the root holds three records -- the baseline check, the
    candidate check and the iteration -- each RELEASED and removed, and no directory;
    the admission was asked for exactly those three ``tmp_workspace`` reservations
    and every reservation was given back; the session's lease is closed (renamed,
    never removed); and the iteration was accepted. Negative control:
    ``test_an_unregistered_project_keeps_the_sibling_scratch``."""
    given: list[tuple[Path, Path | None]] = []
    real_check = controller_module.run_check

    def spy(check, candidate, **kw):
        given.append((Path(kw["cwd"]), kw.get("scratch")))
        return real_check(check, candidate, **kw)

    monkeypatch.setattr(controller_module, "run_check", spy)
    rc, store, admission, fake = through_the_cli(tmp_path, repo, w, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, out
    info = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    assert len(info) == 1
    session = info[0]["session"]
    root = tmp_root(w, session)
    assert info[0] == {"project": PROJECT, "session": session, "root": str(root),
                       "matched_by": "name"}
    assert session == "r1" and root.is_dir()
    assert w.registry.owner_of(root).status == "PROTECTED"

    printed = {}
    for receipt_id in ("r1-i1-a1-K1-basis", "r1-i1-a1-K1"):
        line = next(ln for ln in log_text(store, receipt_id).splitlines() if ln.startswith("TMPDIR="))
        tmpdir, home = (part.split("=", 1)[1] for part in line.split(" "))
        assert tmpdir == home
        printed[receipt_id] = Path(tmpdir)
    assert len(set(printed.values())) == 2
    for path in printed.values():
        assert path.parent == root and path.name.startswith("r1-")
        assert not path.exists()                     # the creator removed it

    # the arena is the controller's own and the scratch is the registered workspace: the two
    # arguments of every check the controller ran are chosen by the controller (R-1)
    assert len(given) == 2
    arenas = Path(os.path.realpath(store.arenas_dir))
    assert all(Path(os.path.realpath(cwd)).is_relative_to(arenas) for cwd, _ in given)
    assert all(scratch is not None and Path(scratch).parent == root for _, scratch in given)
    assert beside_the_arenas(store) == []
    found = records(root)
    assert sorted((r["creator"], r["purpose"]) for r in found) == [
        ("controller.run_check", "check"), ("controller.run_check", "check"),
        ("controller.run_iteration", "run")]
    assert all(r["state"] == se.RELEASED and r["removed"] is True and r["run_id"] == "r1"
               and r["session_id"] == session and r["project_id"] == PROJECT for r in found)
    assert workspace_dirs(root) == []
    assert admission.kinds("tmp_workspace") == 3 and admission.open == {}
    assert sorted(k for k in admission.released if k == "tmp_workspace") == ["tmp_workspace"] * 3
    session_dir = w.registry.state_root / "projects" / PROJECT / "sessions" / session
    assert not (session_dir / "LEASE.json").exists()
    assert len(list(session_dir.glob("LEASE.json.v*"))) == 1
    assert store.read_state().last_accepted_candidate is not None
    assert "temporary roots: project 'pd' (by name), session 'r1'" \
        in "\n".join(store.read_state().history)


def test_an_unregistered_project_keeps_the_sibling_scratch(tmp_path, repo, w, monkeypatch, capsys):
    """W1c, the control of W1 and the stated limit of the wiring: the same run for a
    project the registry does not know has no root to put a workspace under. It says so
    (one output document and one note in the run state), asks the admission for no
    ``tmp_workspace``, opens no session, and the runner's sibling ``<arena>.scratch`` is
    used as before. Catches a wiring that raises for an unregistered project or that
    pretends to have a root."""
    own = standalone_repo(tmp_path / "own")
    rc, store, admission, fake = through_the_cli(tmp_path, own, w, monkeypatch,
                                                 project="unknown-project")
    out = capsys.readouterr().out
    assert rc == 0, out
    shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    assert len(shown) == 1 and shown[0]["state"] == "not registered"
    assert shown[0]["scratch"] == "runner sibling scratch" and "no registered project" in shown[0]["why"]
    assert admission.kinds("tmp_workspace") == 0
    assert any(n.endswith(".scratch") for n in beside_the_arenas(store))
    assert "temporary roots: no registered project" in "\n".join(store.read_state().history)
    assert not (w.registry.state_root / "projects" / "unknown-project").exists()


def test_a_registered_project_whose_root_cannot_be_opened_does_not_start(
        tmp_path, repo, w, monkeypatch, capsys):
    """W7. Catches a run that goes on without its root (M8), and one that pauses a run that is not its own
    (B1): (a) the session of the run is held by a **live lease of another process** -- that process is
    executing the run -- so `hoh run` stops before any role is dispatched with exit 2 and the words the
    launcher reads as "in flight" (``Run r1 is already being executed ...``), **writes nothing into the run**
    (the state on disk is the one it was: not paused, not NEEDS_YOU, not re-sequenced) and is not read as a
    storage problem; (b) the root itself cannot be made after the lease is taken: exit 2,
    ``NEEDS_YOU: storage admission unavailable: the temporary root ...`` on stderr, the run stopped
    NEEDS_YOU **on disk**, the lease taken is given back. Negative control: the same run when the session can
    be opened is W1."""
    from hoh import launcher

    other = 777
    w.fake.procs[other] = 9000
    w.registry.open_session(PROJECT, "r1", pid=other, boot_id="boot-d")
    spec, held_store, held_state = new_run(tmp_path, repo)
    before = held_store.read_state()
    rc, store, admission, fake = through_the_cli(tmp_path, repo, w, monkeypatch, prepared=True)
    err = capsys.readouterr().err
    assert rc == 2 and fake.calls == []
    assert err.startswith("Run r1 is already being executed by another process")
    assert launcher.storage_refusal(err) is None
    after = store.read_state()
    assert (after.condition, after.stop_reason, after.blocked_kind, after.write_seq) == (
        before.condition, before.stop_reason, before.blocked_kind, before.write_seq)
    assert after.condition is Condition.ACTIVE and not after.stop_reason
    assert beside_the_arenas(store) == [] and admission.kinds("tmp_workspace") == 0

    def broken(project_id, session_id, kind):
        raise sr.StorageOwnershipError("the root cannot be made (simulated)")

    w.fake.procs.pop(other)
    monkeypatch.setattr(w.registry, "scratch_dir", broken)
    again = tmp_path / "again"
    rc, store, admission, fake = through_the_cli(again, make_repo(again, w), w, monkeypatch,
                                                 run_id="r2")
    err = capsys.readouterr().err
    assert rc == 2 and "the root cannot be made (simulated)" in err
    assert err.startswith("NEEDS_YOU: storage admission unavailable: the temporary root of the run's project")
    stopped = store.read_state()
    assert stopped.condition is Condition.PAUSED
    assert (stopped.stop_reason or "").startswith("storage admission unavailable: the temporary root")
    lease = w.registry._read_lease(PROJECT, "r2")
    assert lease is None                                   # opened, then closed again (versioned)
    assert list((w.registry.state_root / "projects" / PROJECT / "sessions" / "r2")
                .glob("LEASE.json.v*"))


def test_a_stale_master_with_a_live_lease_of_the_run_writes_nothing(
        tmp_path, repo, w, monkeypatch, capsys):
    """W7b. Catches the refused caller that pauses the live master's run (B1, the reviewer's probe shape):
    process A is the live master of run r1 (master S5/5 declared, a live lease of its pid on the run's
    session, its state in memory); a second ``hoh run r1 --master-session S3 --master-epoch 3`` -- a
    superseded master -- is refused with exit 2 and **nothing written**: the state on disk is A's, A's
    next write is accepted (no ``StaleWrite``), nothing is dispatched. Negative control: W7 (b), where
    nobody else runs the run and NEEDS_YOU is written."""
    from hoh import dispatchrecovery

    other = 777
    w.fake.procs[other] = 9000
    w.registry.open_session(PROJECT, "r1", pid=other, boot_id="boot-d")
    spec, store, state = new_run(tmp_path, repo)
    dispatchrecovery.declare_master(store.dir, session="S5", epoch=5)
    live_view = store.read_state()                          # what the live master holds in memory
    rc, store, admission, fake = through_the_cli(
        tmp_path, repo, w, monkeypatch, prepared=True,
        extra=("--master-session", "S3", "--master-epoch", "3"))
    err = capsys.readouterr().err
    assert rc == 2 and fake.calls == []
    assert err.startswith("Run r1 is already being executed by another process")
    after = store.read_state()
    assert (after.condition, after.stop_reason, after.write_seq) == (
        live_view.condition, live_view.stop_reason, live_view.write_seq)
    store.write_state(live_view)                            # the live master's next write: accepted


def test_the_session_is_closed_and_the_workspaces_released_when_the_iteration_aborts(
        tmp_path, repo, w, monkeypatch, capsys):
    """W8. Catches a lease or a workspace left behind by an abnormal way out (M2, M3): the
    developer raises, `hoh run` blocks the run and returns 1 -- and the session lease is
    closed, every workspace is RELEASED and removed, and every reservation was given back
    (the iteration's, the baseline check never ran: only the iteration workspace exists).
    Negative control: W1, the normal way out."""
    fake = Fake(repo)
    fake.developer_raise = RuntimeError("the developer broke (simulated)")
    rc, store, admission, fake = through_the_cli(tmp_path, repo, w, monkeypatch, fake=fake)
    capsys.readouterr()
    assert rc == 1
    root = tmp_root(w, "r1")
    assert [(r["purpose"], r["state"], r["removed"]) for r in records(root)] \
        == [("run", se.RELEASED, True)]
    assert workspace_dirs(root) == [] and admission.open == {}
    session_dir = w.registry.state_root / "projects" / PROJECT / "sessions" / "r1"
    assert not (session_dir / "LEASE.json").exists() and list(session_dir.glob("LEASE.json.v*"))


def test_a_failure_right_after_the_session_opens_still_closes_the_lease(
        tmp_path, repo, w, monkeypatch):
    """W20. Catches the close registered after something that can raise (M18): the output document of the
    storage admission -- the first thing `hoh run` prints after the session opens -- fails (a closed
    stdout), the call raises, and the lease is closed all the same (renamed, not removed). Negative
    control: W1 (nothing fails)."""
    real_print = cli._print

    def printing(obj):
        if isinstance(obj, dict) and "storage_admission" in obj:
            raise OSError(5, "stdout is gone (simulated)")
        real_print(obj)

    monkeypatch.setattr(cli, "_print", printing)
    with pytest.raises(OSError, match="stdout is gone"):
        through_the_cli(tmp_path, repo, w, monkeypatch)
    session_dir = w.registry.state_root / "projects" / PROJECT / "sessions" / "r1"
    assert not (session_dir / "LEASE.json").exists() and list(session_dir.glob("LEASE.json.v*"))


def test_a_second_hoh_run_of_the_same_run_reuses_the_root_and_leaves_one_lease_per_process(
        tmp_path, repo, w, monkeypatch, capsys):
    """W13. Catches a resumed run that cannot get its root again, or one that litters (M2): `hoh run`
    twice on one run id (what a resume does, one process each) opens the same session id the
    second time -- its first lease was closed, so it is superseded by nothing -- and reaches
    the same ``tmp`` root; every workspace of both processes is RELEASED, and the session has
    two closed leases and no open one. Negative control: W1 (one process)."""
    rc, store, admission, fake = through_the_cli(tmp_path, repo, w, monkeypatch)
    capsys.readouterr()
    assert rc == 0
    rc, store, admission, fake = second_run(tmp_path, repo, w, monkeypatch, admission, fake)
    out = capsys.readouterr().out
    assert rc == 0, out
    root = tmp_root(w, "r1")
    assert all(r["state"] == se.RELEASED and r["removed"] for r in records(root))
    assert len(records(root)) >= 5 and workspace_dirs(root) == [] and admission.open == {}
    session_dir = w.registry.state_root / "projects" / PROJECT / "sessions" / "r1"
    assert not (session_dir / "LEASE.json").exists()
    assert len(list(session_dir.glob("LEASE.json.v*"))) == 2


def second_run(tmp_path, repo, w, monkeypatch, admission, fake):
    """`hoh run r1` again on the run `through_the_cli` made (same state, same root)."""
    store = RunStore(tmp_path / "runs", "r1")
    monkeypatch.setattr(cli, "storage_admission_for",
                        lambda option, **kw: (admission, w.registry.state_root))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "StorageRegistry", lambda *a, **k: w.registry)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: fake)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--no-herdr", "--iterations", "1"])
    return rc, store, admission, fake


def test_a_worktree_of_a_registered_project_s_repository_belongs_to_that_project(
        tmp_path, repo, w, monkeypatch, capsys):
    """W14. Catches a wiring that never engages for the product's own runs (M13): the launcher
    starts a run with the worktree's directory name as its project name (`launcher.py` passes no
    ``--project``), so the name is no registered id. A linked worktree of the registered project's
    repository (the same git common directory -- the registry's own rule for one root, one
    project) finds its project: the output says ``matched_by: repository``, the workspaces are
    under that project's ``tmp`` root and the admission was asked on behalf of that project.
    Negative controls: a separate clone of the same repository is another repository and gets
    no root (the sibling scratch, and the output says why); and a worktree of a repository no
    project owns gets none either."""
    wt = tmp_path / "wt-x"
    subprocess.run(["git", "-C", str(w.canon), "worktree", "add", "-q", "-b", "wtx", str(wt)],
                   check=True, capture_output=True)
    rc, store, admission, fake = through_the_cli(tmp_path, wt, w, monkeypatch, project="wt-x",
                                                 fake=Fake(wt))
    out = capsys.readouterr().out
    assert rc == 0, out
    shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    assert shown[0]["project"] == PROJECT and shown[0]["matched_by"] == "repository"
    assert shown[0]["session"] == "r1" and records(tmp_root(w, "r1"))
    assert {p for kind, p in admission.projects if kind == "tmp_workspace"} == {PROJECT}
    assert beside_the_arenas(store) == []

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(w.canon), str(clone)], check=True, capture_output=True)
    unowned = standalone_repo(tmp_path / "unowned")
    for repo_path, name in ((clone, "clone-x"), (unowned, "plain-x")):
        base = tmp_path / name
        base.mkdir()
        rc, store, admission, fake = through_the_cli(base, repo_path, w, monkeypatch, project=name,
                                                     fake=Fake(repo_path), run_id=f"r-{name}")
        out = capsys.readouterr().out
        shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
        assert rc == 0 and shown[0]["state"] == "not registered", out
        assert admission.kinds("tmp_workspace") == 0


# --------------------------------------------------------------------------- #
# W2, W3 -- the controller, STRICT, and the temporary files of workspace.py
# --------------------------------------------------------------------------- #


def controller_in(tmp_path, repo, w, *, admission=None, isolation=None, with_roots=True,
                  fake=None, run_id: str = "r1"):
    spec, store, state = new_run(tmp_path, repo, run_id=run_id)
    admission = admission or Admission(tmp_path / "ledger")
    roots = None
    if with_roots:
        roots = se.RunSession.begin(w.registry, PROJECT, run_id, admission=admission).roots
    fake = fake or Fake(repo)
    ctrl = Controller(store, fake, spec_path=spec, isolation=isolation, storage_admission=admission,
                      ephemeral_roots=roots)
    return ctrl, state, store, admission, fake


class Recording:
    """A sandbox backend that only records the scratch the runner hands it (the
    pattern of ``test_storage_ephemeral``'s M4); the command is not run."""

    name = "recording"

    def __init__(self) -> None:
        self.scratch: list[Path] = []

    def unavailable(self):
        return None

    def plan(self, argv, spec):
        from hoh.sandbox import LaunchPlan

        self.scratch.append(Path(spec.scratch))
        return LaunchPlan(argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"},
                          cwd=str(spec.candidate), proves_isolation=False)


def test_strict_checks_get_a_registered_workspace_as_their_sandbox_scratch(
        tmp_path, repo, w, monkeypatch):
    """W2. Catches a STRICT check whose scratch is not the registered workspace (M4, M5): with
    a recording backend the `SandboxSpec` of the baseline check and of the candidate check
    each name a workspace of the session's root -- two different ones -- and no
    ``.hoh-scratch-<receipt>`` is made beside the arenas. (The backend only records; the
    receipts are not verdicts.) Negative control: without roots the specs name the pin's
    ``.hoh-scratch-<receipt>`` siblings."""
    from hoh import sandbox

    backend = Recording()
    monkeypatch.setattr(sandbox, "select", lambda isolation: backend)
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w,
                                                        isolation=sandbox.Isolation.STRICT)
    ctrl.run_iteration(state)
    root = tmp_root(w, "r1")
    assert len(backend.scratch) == 2 and len(set(backend.scratch)) == 2
    assert all(p.parent == root for p in backend.scratch)
    assert beside_the_arenas(store) == [] and admission.open == {}

    backend.scratch.clear()
    control = tmp_path / "control"
    ctrl, state, store, admission, fake = controller_in(control, make_repo(control, w), w,
                                                        isolation=sandbox.Isolation.STRICT,
                                                        with_roots=False, run_id="r2")
    ctrl.run_iteration(state)
    assert [p.name for p in backend.scratch] == [".hoh-scratch-r2-i1-a1-K1-basis",
                                                 ".hoh-scratch-r2-i1-a1-K1"]


def test_the_temporary_files_of_workspace_py_are_made_in_the_iteration_workspace(
        tmp_path, repo, w, monkeypatch):
    """W3. Catches an iteration whose git index and tar are made in the host's temporary
    directory (M6, M7): `snapshot` (the git index) and `materialize` (the tar of the
    candidate) make their `NamedTemporaryFile`s -- recorded here at the call -- only in
    the directory of the workspace the iteration made (purpose ``run``); the default
    temporary directory of the process is that workspace during the iteration and what it
    was before once the iteration is over, also when the iteration fails. Negative
    control: a controller without roots makes the same files in the default directory."""
    made: list[str] = []
    real = tempfile.NamedTemporaryFile

    def spy(*args, **kw):
        handle = real(*args, **kw)
        made.append(os.path.dirname(handle.name))
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", spy)
    before = tempfile.tempdir
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w)
    ctrl.run_iteration(state)
    root = tmp_root(w, "r1")
    run_records = [r for r in records(root) if r["purpose"] == "run"]
    assert len(run_records) == 1
    assert len(made) >= 4 and set(made) == {str(root / run_records[0]["workspace_id"])}
    assert tempfile.tempdir is before

    made.clear()
    failing = tmp_path / "failing"
    broken = Fake(make_repo(failing, w))
    broken.developer_raise = RuntimeError("simulated")
    ctrl, state, store, admission, fake = controller_in(failing, broken.repo, w, fake=broken,
                                                        run_id="r2")
    with pytest.raises(RuntimeError):
        ctrl.run_iteration(state)
    assert tempfile.tempdir is before

    made.clear()
    plain = tmp_path / "plain"
    ctrl, state, store, admission, fake = controller_in(plain, make_repo(plain, w), w,
                                                        with_roots=False, run_id="r3")
    ctrl.run_iteration(state)
    assert made and set(made) == {tempfile.gettempdir()}


# --------------------------------------------------------------------------- #
# W4, W5 -- refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("nth,where", [(1, "the iteration's workspace"),
                                       (2, "the baseline check's workspace"),
                                       (3, "the candidate check's workspace")])
def test_a_refused_admission_parks_the_run_and_nothing_falls_back(
        tmp_path, repo, w, nth, where):
    """W4. Catches a refusal that is swallowed, turned into a verdict or answered with a
    sibling or host directory (M9): the admission refuses the n-th temporary workspace
    for lack of space -- the iteration's, the baseline check's, the candidate check's -- and
    the run is parked on ``storage_pressure`` (BLOCKED, a recheck due), the iteration
    returns not accepted, no check ran after the refusal, no ``.scratch`` or
    ``.hoh-scratch-*`` was made, no workspace directory is left and every reservation made
    was given back. Negative control: the same run with the refusal removed is accepted."""
    admission = Admission(tmp_path / "ledger", refuse_nth=nth)
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w, admission=admission)
    out = ctrl.run_iteration(state)
    persisted = store.read_state()
    assert out.accepted is False and persisted.condition is Condition.BLOCKED, where
    assert persisted.blocked_kind == "storage_pressure" and persisted.retry_after
    assert beside_the_arenas(store) == [] and admission.open == {}
    assert workspace_dirs(tmp_root(w, "r1")) == []
    assert not list(store.receipts_dir.glob("*K1.json")) if nth == 3 else True
    if nth == 1:
        assert fake.calls == []                            # parked before anything was spent
    root_records = records(tmp_root(w, "r1"))
    assert all(r["state"] == se.RELEASED for r in root_records)
    assert len(root_records) == nth - 1

    control = tmp_path / "control"
    ctrl, state, store, admission, fake = controller_in(
        control, make_repo(control, w), w, admission=Admission(control / "ledger"), run_id="r2")
    assert ctrl.run_iteration(state).accepted is True


def test_a_ledger_refusal_stops_the_run_for_a_person_not_parks_it(tmp_path, repo, w):
    """W4b. Catches a refusal that is not about capacity parked for a retry that cannot help: a ledger refusal (not free space) of the iteration's workspace stops the run
    NEEDS_YOU with the admission's own words, parks nothing. Negative control: W4 (capacity)."""
    admission = Admission(tmp_path / "ledger", refuse_nth=1, reason="boot not claimed")
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w, admission=admission)
    ctrl.run_iteration(state)
    persisted = store.read_state()
    assert persisted.blocked_kind != "storage_pressure" and not persisted.retry_after
    assert "storage admission unavailable: boot not claimed" in (persisted.stop_reason or "")
    assert fake.calls == [] and admission.open == {}


def test_a_scratch_the_runner_refuses_is_a_receipt_that_ran_nothing_not_a_fallback(
        tmp_path, repo, w, monkeypatch):
    """W5. Catches a refused workspace answered with the sibling scratch or a crash (M10): a
    file planted in each check's workspace before the runner looks at it makes
    `run_check(scratch=)` raise; the candidate check and the baseline check each leave a
    receipt with exit 126 and ``runner_ok`` false (INCONCLUSIVE, never a verdict) whose log
    says ``SCRATCH REFUSED, not executed``, no ``.scratch`` is made, the iteration is not
    accepted and the workspaces are released. Negative control: without the planted file
    the same run is accepted (W1)."""
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w)
    real_create = ctrl.ephemeral_roots.create

    def planting(**kw):
        ws = real_create(**kw)
        if kw["purpose"] == "check":
            (ws.path / "planted").write_text("x", encoding="utf-8")
        return ws

    monkeypatch.setattr(ctrl.ephemeral_roots, "create", planting)
    out = ctrl.run_iteration(state)
    assert out.accepted is False
    for receipt_id in ("r1-i1-a1-K1", "r1-i1-a1-K1-basis"):
        receipt = json.loads((store.receipts_dir / f"{receipt_id}.json").read_text())
        assert receipt["exit_code"] == 126 and receipt["runner_ok"] is False
        assert "SCRATCH REFUSED, not executed" in log_text(store, receipt_id) \
            or "refused before execution" in log_text(store, receipt_id)
    assert beside_the_arenas(store) == [] and admission.open == {}
    assert all(r["state"] == se.RELEASED for r in records(tmp_root(w, "r1")))


class FreeSpace:
    """``os.statvfs`` stand-in: the free bytes the test chooses, never the machine's."""

    def __init__(self, gib: float) -> None:
        self.free = int(gib * GiB)

    def __call__(self, path):
        return types.SimpleNamespace(f_bavail=self.free, f_frsize=1, f_favail=10_000_000,
                                     f_fsid=0x6E57)


def real_admission(state_root: Path, free_gib: float) -> StorageAdmission:
    """A real `StorageAdmission` (the product's ledger) on `state_root` with simulated free
    space, its boot claimed as a host entry point does."""
    adm = StorageAdmission.for_state_root(
        state_root, StoragePolicy(),
        GuestObserver(state_root, NoHost(), statvfs=FreeSpace(free_gib)),
        pid_alive=lambda pid: True, proc_start_time=lambda pid: 111,
        boot_id=lambda: "boot-A", pid_namespace=lambda: "pid:[4026532229]")
    adm.claim_boot()
    return adm


def ledger_lines(adm: StorageAdmission) -> list[dict]:
    return [json.loads(ln) for ln in adm.history_path.read_text(encoding="utf-8").splitlines()]


def test_the_product_s_own_ledger_is_balanced_and_its_refusal_parks(tmp_path, repo, w, monkeypatch,
                                                                    capsys):
    """W12. Catches a wiring that works only against a double (M1, M3): the same run against the
    real `StorageAdmission` ledger. With room, the history holds exactly three closed
    ``tmp_workspace`` reservations (the baseline check, the candidate check, the iteration),
    each with the bytes found when its workspace was removed, and no reservation is open; with
    30.5 GiB free (the policy's reserve is 30, a workspace is estimated at 1) the **real** refusal of the iteration's workspace
    parks the run on ``storage_pressure`` before any role is dispatched, and the ledger has no
    open reservation. Negative control: the first half."""
    adm = real_admission(w.registry.state_root, 100)
    rc, store, adm, fake = through_the_cli(tmp_path, repo, w, monkeypatch, admission=adm)
    capsys.readouterr()
    assert rc == 0 and adm.open_reservations() == []
    tmp = [ln for ln in ledger_lines(adm) if ln["op_kind"] == "tmp_workspace"]
    assert len(tmp) == 3 and all(ln["project_id"] == PROJECT and ln["actual"] >= 0 for ln in tmp)

    other = tmp_path / "tight"
    adm = real_admission(w.registry.state_root, 30.5)
    rc, store, adm, fake = through_the_cli(other, make_repo(other, w), w, monkeypatch, admission=adm,
                                           run_id="r2")
    capsys.readouterr()
    state = store.read_state()
    assert state.condition is Condition.BLOCKED and state.blocked_kind == "storage_pressure"
    assert fake.calls == [] and adm.open_reservations() == []
    assert [ln for ln in ledger_lines(adm) if ln["op_kind"] == "tmp_workspace"] == tmp  # none new



# --------------------------------------------------------------------------- #
# W15 -- the iteration's workspace is asked for after every gate of the iteration
# --------------------------------------------------------------------------- #


def _halt_cancelled(ctrl, state, store):
    stages.cancel(state, "operator")
    store.write_state(state)


def _halt_paused(ctrl, state, store):
    stages.pause(state, "operator")
    store.write_state(state)


def _halt_pending_cancel(ctrl, state, store):
    store.request_stop("cancel", "operator")


def _halt_budget(ctrl, state, store):
    state.usage.iterations = state.budgets.max_iterations
    store.write_state(state)


def _halt_spec_drift(ctrl, state, store):
    ctrl.spec_path.write_text(SPEC + "edited behind the run's back\n", encoding="utf-8")


def _what_happened(ctrl, state, store) -> tuple:
    try:
        out = ctrl.run_iteration(state)
        said = ("outcome", out.reason.split(":")[0][:40])
    except Exception as exc:                          # noqa: BLE001 - compared, not judged
        said = ("raised", type(exc).__name__)
    s = store.read_state()
    return (said, s.stage.value, s.condition.value, s.blocked_kind, s.stop_reason,
            bool(s.retry_after), store.read_stop_request() is None)


@pytest.mark.parametrize("halt", [_halt_cancelled, _halt_paused, _halt_pending_cancel,
                                  _halt_budget, _halt_spec_drift],
                         ids=lambda f: f.__name__[len("_halt_"):])
def test_a_run_that_would_not_start_is_never_parked_for_storage(tmp_path, w, halt):
    """W15. Catches the iteration's workspace asked for before the iteration's own gates (M14): with
    the first ``tmp_workspace`` refused for lack of space, a run that is CANCELLED, PAUSED, has a
    pending cancel request, has spent its budget or whose specification changed behind its back is
    treated exactly as the same run without roots is -- the same outcome or exception, the same stage,
    condition, ``blocked_kind``, stop reason, no recheck due, the stop request consumed as before --
    the admission is never asked, no timer is armed and nothing is parked on ``storage_pressure``
    (an operator's cancel is not turned into a retry that ``resume-storage`` would lift).
    Negative control: W4 (the same refusal on a run that was going to run parks it)."""
    seen = {}
    for flavour in ("roots", "plain"):
        base = tmp_path / flavour
        admission = Admission(base / "ledger", refuse_nth=1)
        ctrl, state, store, admission, fake = controller_in(
            base, make_repo(base / "r", w), w, admission=admission,
            with_roots=flavour == "roots")
        halt(ctrl, state, store)
        seen[flavour] = (_what_happened(ctrl, state, store), admission.kinds("tmp_workspace"),
                         [t.kind for t in TimerStore(store.dir.parent).read()])
        assert fake.calls == []
    assert seen["roots"][0] == seen["plain"][0], seen
    assert seen["roots"][0][3] != "storage_pressure"
    assert seen["roots"][1] == 0 and seen["roots"][2] == []


def test_an_admission_that_cannot_answer_stops_the_run_like_a_role_dispatch(tmp_path, w):
    """W4c. Catches a workspace admission that raises and ends in the generic abort (rc 1, BLOCKED
    with no kind) where a role dispatch stops NEEDS_YOU (F3): the ledger raising for the iteration's
    workspace leaves the run PAUSED with ``storage admission unavailable: ...`` and no role dispatched,
    exactly the shape a ledger that raises for a ``role_run`` leaves; nothing is created. Negative
    control: that role-dispatch run, on a controller without roots."""
    a = tmp_path / "a"
    ctrl, state, store, admission, fake = controller_in(
        a, make_repo(a, w), w, admission=Admission(a / "ledger", raise_kind="tmp_workspace"))
    ctrl.run_iteration(state)
    mine = store.read_state()
    assert mine.condition is Condition.PAUSED and fake.calls == []
    assert (mine.stop_reason or "").startswith("storage admission unavailable: OSError")
    assert workspace_dirs(tmp_root(w, "r1")) == [] and admission.open == {}
    b = tmp_path / "b"
    ctrl, state, store, admission, fake = controller_in(
        b, make_repo(b, w), w, admission=Admission(b / "ledger", raise_kind="role_run"),
        with_roots=False, run_id="r2")
    ctrl.run_iteration(state)
    role = store.read_state()
    assert (role.condition, role.blocked_kind, (role.stop_reason or "")[:40]) == (
        mine.condition, mine.blocked_kind, (mine.stop_reason or "")[:40])


# --------------------------------------------------------------------------- #
# W16 -- a ValueError is a refusal receipt only when it comes out of run_check(scratch=)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("site", ["baseline", "candidate"])
def test_a_value_error_is_a_scratch_refusal_only_where_the_controller_gave_a_scratch(
        tmp_path, repo, w, monkeypatch, site):
    """W16. Catches a ``ValueError`` swallowed as a receipt where no scratch was given (M10), and one
    out of *making* the workspace (F4a) labelled ``SCRATCH REFUSED``: (a) without roots a
    ``ValueError`` out of ``run_check`` at either site propagates as it always did; (b) with roots a
    ``ValueError`` raised by ``EphemeralRoots.create`` for a check propagates too -- no receipt says
    "SCRATCH REFUSED" for a problem that is not the runner's guard. Negative control: W5 (the guard's own
    ``ValueError`` with roots is a receipt)."""
    real_check = controller_module.run_check
    suffix = "basis" if site == "baseline" else ""

    def spy(check, candidate, **kw):
        if kw.get("receipt_suffix", "") == suffix:
            raise ValueError("boom (simulated)")
        return real_check(check, candidate, **kw)

    monkeypatch.setattr(controller_module, "run_check", spy)
    plain = tmp_path / "plain"
    ctrl, state, store, admission, fake = controller_in(plain, make_repo(plain, w), w,
                                                        with_roots=False, run_id="r3")
    with pytest.raises(ValueError, match="boom"):
        ctrl.run_iteration(state)
    monkeypatch.setattr(controller_module, "run_check", real_check)

    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w)
    real_create = ctrl.ephemeral_roots.create
    checks = []

    def create(**kw):
        if kw["purpose"] == "check":
            checks.append(1)                   # the baseline check's workspace is the first
            if len(checks) == (1 if site == "baseline" else 2):
                raise ValueError("a policy file is broken (simulated)")
        return real_create(**kw)

    monkeypatch.setattr(ctrl.ephemeral_roots, "create", create)
    with pytest.raises(ValueError, match="policy file"):
        ctrl.run_iteration(state)
    refused = [p for p in store.logs_dir.glob("*.txt") if "SCRATCH REFUSED" in p.read_text()]
    assert refused == [] and admission.open == {}


# --------------------------------------------------------------------------- #
# W17 -- a project is found by its name AND its repository, and a failure is said as what it is
# --------------------------------------------------------------------------- #


def test_a_name_binds_a_project_only_to_its_own_repository(tmp_path, repo, w, monkeypatch):
    """W17. Catches a directory that merely carries a project's name binding that project (F2, M13) and
    a git failure reported as "not a repository" (F4b): ``project_for_run`` returns the project for the
    registered name only when the run's repository is the project's (the same git common
    directory: its own checkout, a linked worktree); an unrelated repository or a separate clone
    with that name binds nothing and the reason names the repository; a worktree under another name
    finds the project by repository; a project registered on a plain directory, and a run in one, is
    bound by name alone (stated limit); two projects with one repository (not reachable through
    ``register_project``, made by hand) bind nothing, and the reason names both; a registry with no
    project says so; and when git cannot be read the reason says it cannot be read -- with the name
    registered or not. Negative control: the project's own worktree, by name."""
    pf = se.project_for_run
    assert pf(w.registry, PROJECT, repo) == (PROJECT, "name")
    assert pf(w.registry, PROJECT, w.canon) == (PROJECT, "name")
    foreign = standalone_repo(tmp_path / "foreign")
    project_id, why = pf(w.registry, PROJECT, foreign)
    assert project_id is None and "is not the repository of the registered project 'pd'" in why
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(w.canon), str(clone)], check=True, capture_output=True)
    assert pf(w.registry, PROJECT, clone)[0] is None
    assert pf(w.registry, "other-name", repo) == (PROJECT, "repository")
    assert pf(w.registry, "other-name", foreign)[0] is None
    plain_project = tmp_path / "plain-project"
    plain_project.mkdir()
    w.registry.register_project("pp", plain_project)
    plain_run = tmp_path / "plain-run"
    plain_run.mkdir()
    assert pf(w.registry, "pp", plain_run) == ("pp", "name")

    # two projects with one repository: made by hand, the registry refuses to make them
    record = json.loads((w.registry.state_root / "projects" / PROJECT / "PROJECT.json").read_text())
    twin = w.registry.state_root / "projects" / "pe"
    twin.mkdir()
    (twin / "PROJECT.json").write_text(
        json.dumps(dict(record, project_id="pe", archive_namespace="pe")), encoding="utf-8")
    project_id, why = pf(w.registry, "other-name", repo)
    assert project_id is None and "several registered projects (pd, pe)" in why

    def unreadable(path):
        raise sr.StorageOwnershipError("git failed (simulated)")

    monkeypatch.setattr(w.registry, "_repo_identity", unreadable)
    for name in (PROJECT, "other-name"):
        project_id, why = pf(w.registry, name, repo)
        assert project_id is None and "cannot be read" in why and "git failed (simulated)" in why
        assert "not a repository root" not in why
    empty = sr.StorageRegistry(tmp_path / "empty-state")
    empty.init()
    assert pf(empty, PROJECT, repo) == (None, "no project is registered")


def test_a_directory_that_carries_a_project_s_name_gets_no_root_through_the_cli(
        tmp_path, w, monkeypatch, capsys):
    """W17b. The same through ``hoh run``: an unrelated repository whose run is called by the registered
    project's name is not given that project's root (F2): the output says why, the admission is not
    asked for a workspace and the sibling scratch is used. Negative control: W1 (the project's own
    repository, same name)."""
    foreign = standalone_repo(tmp_path / "foreign")
    rc, store, admission, fake = through_the_cli(tmp_path, foreign, w, monkeypatch, project=PROJECT)
    out = capsys.readouterr().out
    assert rc == 0, out
    shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    assert shown[0]["state"] == "not registered" and "is not the repository of" in shown[0]["why"]
    assert admission.kinds("tmp_workspace") == 0 and records(tmp_root(w, "r1")) == []


def test_a_registered_name_whose_repository_cannot_be_read_binds_nothing(tmp_path, repo, w, monkeypatch):
    """W17c. Catches the unreadable-repository branches of the name lookup (X22): when only the run's
    repository cannot be read, a registered name binds nothing and the reason says the run's repository identity
    cannot be read -- with the name registered or not; when only the project's repository cannot be read, the
    registered name binds nothing and the reason names the project; and the repository search skips an unreadable
    project and says how many it skipped. Negative control: W17 (everything readable)."""
    pf = se.project_for_run
    real = w.registry._repo_identity
    run_path, project_path = Path(os.path.realpath(repo)), Path(os.path.realpath(w.canon))

    def failing_for(bad: Path):
        def identity(path):
            if Path(path) == bad:
                raise sr.StorageOwnershipError(f"git failed on {bad.name} (simulated)")
            return real(path)
        return identity

    monkeypatch.setattr(w.registry, "_repo_identity", failing_for(run_path))
    project_id, why = pf(w.registry, PROJECT, repo)
    assert project_id is None and "the run's repository identity cannot be read" in why
    assert "git failed on project (simulated)" in why and "not confirmed as the registered project 'pd'" in why
    project_id, why = pf(w.registry, "other-name", repo)
    assert project_id is None and "no registered project has this name and the run's repository identity cannot be read" in why

    monkeypatch.setattr(w.registry, "_repo_identity", failing_for(project_path))
    project_id, why = pf(w.registry, PROJECT, repo)
    assert project_id is None and "the repository identity of the registered project 'pd' cannot be read" in why
    project_id, why = pf(w.registry, "other-name", repo)
    assert project_id is None and "1 registered project(s) have a repository that cannot be read" in why

    monkeypatch.setattr(w.registry, "_repo_identity", real)
    assert pf(w.registry, PROJECT, repo) == (PROJECT, "name")


# --------------------------------------------------------------------------- #
# W18, W19 -- a workspace that stays; a session that cannot be closed
# --------------------------------------------------------------------------- #


def test_a_workspace_the_creator_cannot_remove_stays_and_is_noted(tmp_path, repo, w):
    """W18. Catches a leftover that is silent or fatal (L9): each check makes a read-only
    subdirectory with a file in it, which the creator's cleanup does not remove (it never changes
    a mode); the two workspaces stay, their records say RELEASED with ``removed`` false and why, the run
    state gets one note for each, the next check gets a fresh workspace, the reservations are given
    back and the iteration is accepted (a leftover is not a verdict). Negative control: W1 (nothing
    stays)."""
    if os.geteuid() == 0:
        pytest.skip("root removes read-only directories")
    stuck = 'mkdir -p "$TMPDIR/d/e" && touch "$TMPDIR/d/e/f" && chmod 555 "$TMPDIR/d/e"; test -f done.txt'
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w, fake=Fake(repo, stuck))
    out = ctrl.run_iteration(state)
    root = tmp_root(w, "r1")
    try:
        left = [r for r in records(root) if r["purpose"] == "check"]
        assert len(left) == 2 and all(r["state"] == se.RELEASED and r["removed"] is False
                                      and "some entries could not be removed" in r["release_note"]
                                      for r in left)
        assert workspace_dirs(root) == sorted(r["workspace_id"] for r in left)
        history = "\n".join(store.read_state().history)
        assert history.count("stays: some entries could not be removed") == 2
        assert admission.open == {} and out.accepted is True
    finally:
        for hidden in root.glob("*/d/e"):
            hidden.chmod(0o755)


def test_the_iteration_workspace_that_stays_is_noted_on_disk(tmp_path, repo, w, monkeypatch):
    """W18b. Catches a note that is made after the iteration's last write and so never reaches the disk
    (M4): the iteration's workspace gets a read-only subdirectory the cleanup does not remove (made inside
    the process's default temporary directory while the iteration runs); it stays, its record says
    RELEASED / ``removed`` false, and the run state **read from the disk** carries the note. Negative
    control: W18 (the checks' workspaces), and a workspace that is removed leaves no note."""
    if os.geteuid() == 0:
        pytest.skip("root removes read-only directories")
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w)
    real_snapshot = controller_module.snapshot

    def snapshot_and_leave_something(*args, **kw):
        stuck = Path(tempfile.gettempdir()) / "d" / "e"
        if not stuck.exists():
            stuck.mkdir(parents=True)
            (stuck / "f").write_text("x", encoding="utf-8")
            stuck.chmod(0o555)
        return real_snapshot(*args, **kw)

    monkeypatch.setattr(controller_module, "snapshot", snapshot_and_leave_something)
    ctrl.run_iteration(state)
    root = tmp_root(w, "r1")
    try:
        [run_record] = [r for r in records(root) if r["purpose"] == "run"]
        assert run_record["state"] == se.RELEASED and run_record["removed"] is False
        history = "\n".join(store.read_state().history)
        assert history.count(f"temporary workspace {run_record['workspace_id']} stays:") == 1
    finally:
        for hidden in root.glob("*/d/e"):
            hidden.chmod(0o755)


def test_closing_the_run_session_reports_instead_of_raising(w, capsys):
    """W19. Catches a run that fails because its lease could not be closed: ``RunSession.close``
    returns "" when the lease ended and the reason when it did not (the lease on record is no longer this
    one), and ``cli._close_ephemeral`` says it on stderr and raises nothing. Negative control: the
    first ``close``."""
    session = se.RunSession.begin(w.registry, PROJECT, "r9")
    assert session.close() == ""
    why = session.close()
    assert "StorageOwnershipError" in why
    cli._close_ephemeral(session)
    assert "closing the temporary-root session 'r9' failed" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# W21 -- the product's own resume gate asks for what the resumed iteration asks for
# --------------------------------------------------------------------------- #


def _armed_storage_timers(runs: Path) -> list:
    from hoh.timers import TimerKind, TimerStatus

    return [t for t in TimerStore(runs).read()
            if t.status is TimerStatus.ARMED and t.kind is TimerKind.STORAGE_RECHECK]


def _after_due(runs: Path):
    from datetime import UTC, datetime, timedelta

    [timer] = _armed_storage_timers(runs)
    due = datetime.strptime(timer.due_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return due + timedelta(seconds=1)


def test_the_resume_gate_does_not_unpark_a_run_the_wired_iteration_would_park_again(
        tmp_path, repo, w, monkeypatch, capsys):
    """W21. Catches the unpark / re-park loop of the band (B2): `resume_storage` unparked on a ``role_run``
    alone (2 GiB) while the wired iteration holds its 1 GiB ``tmp_workspace`` when the planner asks, so it
    needs 3 GiB. With the real ledger at 32.5 GiB free (reserve 30) the run is parked at the planner with the
    iteration charged once, and the product's own `resume_storage` -- six passes -- leaves it parked: every
    pass is ``rearmed`` with the recheck count 1..6 (the escalation accumulates and the sixth says NEEDS_YOU),
    no role runs, **no iteration is spent**. At 33.5 GiB the pass resumes it and the next `hoh run` runs the
    iteration to an accepted checkpoint, parked no more. Negative control: the last half, and W4 (a run that
    is going to run is parked)."""
    from hoh.resumeloop import resume_storage

    runs = tmp_path / "runs"
    rc, store, adm, fake = through_the_cli(tmp_path, repo, w, monkeypatch,
                                           admission=real_admission(w.registry.state_root, 32.5))
    capsys.readouterr()
    parked = store.read_state()
    assert parked.condition is Condition.BLOCKED and parked.blocked_kind == "storage_pressure"
    assert fake.calls == [] and f"estimate={2 * GiB} B" in (parked.blocked_reason or "")
    charged = parked.usage.iterations
    for n in range(1, 7):
        [rec] = resume_storage(runs, admission=real_admission(w.registry.state_root, 32.5),
                               now=_after_due(runs), projects=False)
        assert rec["action"] == "rearmed" and rec["recheck"] == n, rec
        assert (rec.get("verdict") == "NEEDS_YOU") == (n == 6), rec
        still = store.read_state()
        assert still.condition is Condition.BLOCKED and still.blocked_kind == "storage_pressure"
        assert still.usage.iterations == charged and fake.calls == []
    roomy = real_admission(w.registry.state_root, 33.5)
    [rec] = resume_storage(runs, admission=roomy, now=_after_due(runs), projects=False)
    assert rec["action"] == "resumed", rec
    rc, store, adm, fake = second_run(tmp_path, repo, w, monkeypatch, roomy, fake)
    capsys.readouterr()
    done = store.read_state()
    assert rc == 0 and fake.calls[:1] == ["planner"] and "qa" in fake.calls
    assert done.blocked_kind != "storage_pressure" and done.last_accepted_candidate is not None
    assert done.usage.iterations == charged + 1


# --------------------------------------------------------------------------- #
# W9 -- names
# --------------------------------------------------------------------------- #


def test_every_run_id_has_a_session_name_and_two_ids_never_share_one(tmp_path, w):
    """W9. Catches a run id that is not a valid session or workspace name: a run id
    that already is a short lower-case id (and does not end the way a mapped name does) is
    its own name; the id `new_run_id()` makes (upper-case, punctuation) and an 80-character one
    become valid names, are stable, and two ids that differ only in case get two names; a
    literal id that looks like another id's mapped name is mapped itself, so no run id is another's
    name (two ids share a name only if their SHA-256 agree in 48 bits). The id `hoh start` makes
    opens a real session."""
    for run_id in ("r1", "20261008t1533z-p242"):
        assert se.name_for_run(run_id) == run_id
    made = new_run_id()
    odd = [made, "A" * 80, "Run One", "run one", "RUN-ONE", "run-one", "___", "x" * 48, "x" * 49]
    names = [se.name_for_run(r) for r in odd]
    assert names == [se.name_for_run(r) for r in odd]
    assert all(se._PREFIX.match(n) and sr._ID.match(n) for n in names)
    assert len(set(names)) == len(odd)
    assert se.name_for_run("Run-One") != se.name_for_run("run-one")
    mapped = se.name_for_run("AB")
    again = se.name_for_run(mapped)                       # the adversarial id: AB's own mapped name
    assert again != mapped and se.name_for_run("run-0123456789ab") != "run-0123456789ab"
    assert len({mapped, again, se.name_for_run("ab"), se.name_for_run("run-0123456789ab")}) == 4
    session = se.RunSession.begin(w.registry, PROJECT, made)
    assert session.root.is_dir() and session.close() == ""


def test_a_run_id_the_product_makes_runs_an_iteration_through_the_controller(tmp_path, repo, w):
    """W22. Catches a workspace made from the raw run id (X8): `new_run_id()` makes upper-case ids
    (``hoh-20261009T043139Z-4d7106``), which are neither a valid session id nor a workspace name. One whole
    iteration with such an id -- the real `Controller`, the registered root, both checks -- is accepted;
    the session, the root's name and every record carry `name_for_run` of it, and every workspace name starts
    with that name. Negative control: W1 (a plain id)."""
    made = new_run_id()
    assert made != made.lower()
    name = se.name_for_run(made)
    ctrl, state, store, admission, fake = controller_in(tmp_path, repo, w, run_id=made)
    out = ctrl.run_iteration(state)
    assert out.accepted is True and admission.open == {}
    root = tmp_root(w, name)
    found = records(root)
    assert len(found) == 3 and all(r["run_id"] == name and r["session_id"] == name
                                   and r["workspace_id"].startswith(name + "-")
                                   and r["state"] == se.RELEASED and r["removed"] for r in found)


# --------------------------------------------------------------------------- #
# W10 -- the matrix, as a dry run of the records entry
# --------------------------------------------------------------------------- #


def test_the_matrix_reads_proven_for_the_row_once_its_equivalent_is_removed():
    """W10. Catches a consumption cap that still holds (M11: no entry point names the module)
    and a lift that could be had without the equivalent being removed. As written at this
    wiring's merge, the register's own row for P2-42 carried an `equivalent` and the matrix
    said PARTIAL because of it, and the row with the equivalent removed (what a lift would
    do) read PROVEN for the reason this wiring made true: `storage_ephemeral` is named in an
    import statement by `src/hoh/cli.py`, so the pin's reason -- PARTIAL **capped for want of
    an importer** -- is gone. The chain is import text (O222); what makes it more is W1-W8,
    which run it.

    Re-stated 2026-10-12 (strand frontier3): the lift was had. O266 removed the equivalent
    (the register keeps it as `previous_equivalent`, with the day, the actor and the
    evidence of the lift) and recorded the row PROVEN. So the row **as it is** reads
    PROVEN, for that reason and uncapped, and the control is now the other way round: the
    same row with the lifted equivalent put back reads PARTIAL, capped by it -- a lift is
    had by removing the equivalent from the register and by nothing else."""
    import importlib.util
    import sys

    from conftest import PROGRAM_REGISTER, needs_evidence

    needs_evidence(PROGRAM_REGISTER)
    spec = importlib.util.spec_from_file_location("capability_probe_w10",
                                                  ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_probe_w10"] = cm
    spec.loader.exec_module(cm)
    row = next(r for r in json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
               ["requirements"] if r["id"] == "P2-42")
    # The lift is in the register, attributed: no equivalent on the probe any more, and
    # the one there was is kept with the day, the actor and the evidence of its removal.
    assert "equivalent" not in row["probe"], row["probe"].get("equivalent")
    lift = row["previous_equivalent"]
    assert all(str(lift.get(k, "")).strip() for k in ("text", "lifted_on", "by", "evidence")), \
        sorted(lift)
    graph = cm._import_graph()
    assert "src/hoh/cli.py" in graph["storage_ephemeral"]            # named by the command surface itself
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("storage_ephemeral", graph)
    symbols = cm._symbols()
    collection = ("tests/test_storage_ephemeral.py", True)
    got = cm.classify(row, symbols, collection, {}, graph)
    assert got["status"] == cm.PROVEN, got["evidence"]
    assert any("named in an import statement by src/hoh/cli.py" in e for e in got["evidence"])
    assert not any("no entry point" in e for e in got["evidence"])
    assert not any("CAPPED at PARTIAL" in e for e in got["evidence"])
    assert "equivalent_here" not in got
    # Control: the cap still bites. The same row with the lifted equivalent put back is
    # PARTIAL, capped by it -- nothing but its removal lifts a row.
    capped = dict(row, probe=dict(row["probe"], equivalent=lift["text"]))
    kept = cm.classify(capped, symbols, collection, {}, graph)
    assert kept["status"] == cm.PARTIAL, kept["evidence"]
    assert any("CAPPED at PARTIAL by the equivalent" in e for e in kept["evidence"])
    assert not any("no entry point" in e for e in kept["evidence"])


# --------------------------------------------------------------------------- #
# W11 -- the creation sites of src, lexically
# --------------------------------------------------------------------------- #

#: Every `tempfile` call that makes a file or directory in ``src/hoh``, and where its output
#: goes. A lexical tripwire like the writer scan of ``test_durable_writers``: it names the
#: sites it finds and fails on a new one; it does not prove that no other temporary
#: directory is made (a path built by hand, a subprocess, a library).
CREATION_SITES = {
    ("storage_ephemeral.py", "EphemeralRoots.create", "mkdtemp"):
        "REGISTERED: the workspace itself, made in the registered root",
    ("workspace.py", "working_tree", "NamedTemporaryFile"):
        "PROCESS_TEMPDIR: the iteration's workspace is tempfile's default directory (W3)",
    ("workspace.py", "materialize", "NamedTemporaryFile"):
        "PROCESS_TEMPDIR: the iteration's workspace is tempfile's default directory (W3)",
    ("mergegate.py", "_matrix_reading", "TemporaryDirectory"):
        "PROCESS_TEMPDIR: the private object store and the written-out merge tree of one matrix reading by "
        "the merge gate (tempfile's default directory, a few megabytes, removed when the reading ends)",
    ("mergegate.py", "_merge_tree", "TemporaryDirectory"):
        "PROCESS_TEMPDIR: the private object store of one `git merge-tree` run by the merge gate "
        "(tempfile's default directory, a few kilobytes, removed when the measurement ends)",
}
_MAKERS = {"mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryDirectory", "TemporaryFile",
           "SpooledTemporaryFile"}


def _sites() -> dict[tuple[str, str, str], int]:
    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, scope):
            for child in ast.iter_child_nodes(node):
                name = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{scope}.{child.name}" if scope else child.name
                if isinstance(child, ast.Call):
                    func = child.func
                    maker = func.attr if isinstance(func, ast.Attribute) else \
                        func.id if isinstance(func, ast.Name) else ""
                    if maker in _MAKERS:
                        key = (path.name, scope, maker)
                        found[key] = found.get(key, 0) + 1
                walk(child, name)

        walk(tree, "")
    return found


def test_every_tempfile_creation_site_of_src_is_classified():
    """W11. Catches a temporary file or directory made somewhere new (M12): the set of
    `tempfile` creators in ``src/hoh`` is exactly the three sites above, once each. A fourth
    fails until it is routed through a workspace and named here. Lexical, not a proof:
    stated residuals of the wiring (the runner's sibling scratch for library use and
    unregistered projects, the role scratch of the panes, the arenas) are not `tempfile`
    calls and are listed in the spec, not here."""
    assert _sites() == {key: 1 for key in CREATION_SITES}
