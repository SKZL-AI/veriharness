"""P2-42, the roles' scratch: a role's `TMPDIR` is a directory of the iteration's registered
temporary workspace for a run of a registered project, and every role is told in words
where its helper files go (`dogfood/p242r2/spec.md`).

The field case (run 20261009t2004z-i10b, 2026-10-09): the planner made an empty file with
`cat > "$TMPDIR/../mkplan.py"`. `$TMPDIR` was its role directory, a direct child of the run's
arena root, so `$TMPDIR/..` was the arena root, the capability witness saw a new child of a
protected listing and blocked the run -- correctly. These tests hold that the witness still
does exactly that where the role directory is still in the arena root (no registered project),
and that for a registered project the same stray lands in a workspace the run owns, which the
iteration removes.

Nothing here starts Herdr or an agent: the dispatcher of the end-to-end tests is the real
`HarnessDispatcher` and its "agent" is a small Python script of this file (`ROLEISH`); the
Herdr pane of the unit tests is the fake of `test_agent_control`. Every directory is below
pytest's `tmp_path`. The role-like process is given the environment and the working
directory the product builds, read back from the objects it built (the opened pane's `--env`,
the `subprocess.run` of the real dispatcher), never from a configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from test_agent_control import FakeAgentControl, _dispatcher, _no_process
from test_ephemeral_wiring import (Admission, Fake, beside_the_arenas, documents,  # noqa: F401
                                   make_repo, new_run, no_sleep, records, repo,
                                   standalone_repo, tmp_root, w, workspace_dirs)
from test_role_basetemp import herdr_stub  # noqa: F401  (autouse: no test may reach Herdr)
from test_storage_tiers import isolated_env  # noqa: F401  (autouse: the git and HOME identity of the world)

from hoh import cli, dispatchers, integrity, roles
from hoh import storage_registry as sr
from hoh.contracts import Role
from hoh.controller import DispatchError, PaneNotClean
from hoh.store import RunStore

ROLES = [Role.PLANNER, Role.DEVELOPER, Role.QA]
PREFIX = dispatchers.ROLE_SCRATCH_PREFIX

#: What a role does without being told a path: the default temporary-file APIs, the scratchpad a
#: Claude Code session makes below `TMPDIR` (observed in the field run), and -- for the one role
#: `ROLEISH_STRAY` names -- the stray of the field case. It answers with what the test prepared.
ROLEISH = r'''
import json, os, sys, tempfile
from pathlib import Path

role = sys.argv[1]
out = Path(os.environ["ROLEISH_OUT"])
tmpdir = Path(os.environ["TMPDIR"])
made = {"role": role, "cwd": os.getcwd(), "TMPDIR": str(tmpdir)}
fd, path = tempfile.mkstemp(suffix=".py")
os.close(fd)
made["mkstemp"] = path
with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as fh:
    made["named"] = fh.name
made["mkdtemp"] = tempfile.mkdtemp()
made["gettempdir"] = tempfile.gettempdir()
pad = tmpdir / f"claude-{os.getuid()}" / "slug" / "session" / "scratchpad"
pad.mkdir(parents=True)
(pad / "mkplan.py").write_text("x = 1\n", encoding="utf-8")
made["scratchpad"] = str(pad / "mkplan.py")
if os.environ.get("ROLEISH_STRAY") == role:
    stray = tmpdir / ".." / "mkplan.py"
    stray.write_text("", encoding="utf-8")
    made["stray"] = os.path.normpath(stray)
if os.environ.get("ROLEISH_ARENA_STRAY_ROLE") == role:
    Path(os.environ["ROLEISH_ARENA_STRAY"]).write_text("", encoding="utf-8")
workspace = Path(os.path.normpath(tmpdir / ".."))
record = workspace.parent / f".{workspace.name}.vh-ws.json"
if record.is_file():
    made["workspace_record"] = json.loads(record.read_text(encoding="utf-8"))
(out / f"{role}.json").write_text(json.dumps(made), encoding="utf-8")
if os.environ.get("ROLEISH_FAIL") == role:
    sys.exit(3)
if role == "developer":
    (Path.cwd() / "done.txt").write_text("ok\n", encoding="utf-8")
    print("done")
else:
    print(json.loads(Path(os.environ["ROLEISH_ANSWERS"]).read_text(encoding="utf-8"))[role])
'''


def _inside(path: Path, root: Path) -> bool:
    return root.resolve() in Path(os.path.realpath(path)).parents


def _pane_env(tmp_path: Path, monkeypatch, role: Role, *, bind: Path | None):
    """The `--env` a role's pane is opened with, the dispatcher that opened it, and the working
    directory of the pane. `bind` is the workspace of a registered iteration, or None for a
    dispatcher that registered its role directories in the arena root."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    if bind is None:
        d.register_role_scratch()
    else:
        d.role_scratch_deferred = True
        bind.mkdir(parents=True, exist_ok=True)
        d.bind_role_scratch(bind)
    d._ensure_agent(role, state)
    opened = [c for c in fake.calls if c[0] == "open_surface"]
    assert len(opened) == 1
    return opened[0][3], d


# --- the dispatcher: bound, unbound, refused ---------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_a_bound_pane_gets_a_tmpdir_below_the_workspace_and_nothing_is_made_in_the_arena_root(
        tmp_path, monkeypatch, role):
    """S1. The pane's `--env`, read back from the pane the dispatcher opened: `TMPDIR` is the
    role's directory below the bound workspace; the arena root holds no role directory."""
    workspace = tmp_path / "ws"
    env, d = _pane_env(tmp_path, monkeypatch, role, bind=workspace)
    scratch = Path(env["TMPDIR"])
    assert scratch == workspace / f"{PREFIX}{role.value}"
    assert scratch.is_dir() and scratch.parent == workspace
    assert d.role_scratch[role] == scratch
    assert set(d.role_scratch) == set(Role)
    arena_listing = sorted(p.name for p in d.arenas_dir.iterdir()) if d.arenas_dir.is_dir() else []
    assert not [n for n in arena_listing if n.startswith(PREFIX)], arena_listing
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_deferred_dispatcher_has_no_scratch_until_one_is_bound_and_none_after(tmp_path):
    """S2. Catches a deferred dispatcher that falls back to the arena root or to `/tmp`. Built
    by the factory the CLI uses; nothing made at build time; every role refused until bound;
    refused again after the unbind."""
    run = tmp_path / "runs" / "r1"
    built = dispatchers.build_dispatcher(
        answers_dir=run / "answers", profiles={r: "claude" for r in Role}, cwd=tmp_path,
        prefer_herdr=False, defer_role_scratch=True)
    assert built.role_scratch_deferred is True and built.role_scratch == {}
    assert not built.arenas_dir.exists() or list(built.arenas_dir.iterdir()) == []
    for role in Role:
        with pytest.raises(DispatchError, match=role.value):
            built.ensure_role_scratch(role)
        with pytest.raises(DispatchError, match=role.value):
            built._scratch_env(role)       # a direct dispatch would otherwise run with the host TMPDIR
    workspace = tmp_path / "ws"
    workspace.mkdir()
    built.bind_role_scratch(workspace)
    for role in Role:
        built.ensure_role_scratch(role)
        assert built._scratch_env(role) == {"TMPDIR": str(workspace / f"{PREFIX}{role.value}")}
    built.unbind_role_scratch()
    assert built.role_scratch == {}
    for role in Role:
        with pytest.raises(DispatchError, match=role.value):
            built.ensure_role_scratch(role)
        with pytest.raises(DispatchError, match=role.value):
            built._scratch_env(role)
    assert not built.arenas_dir.exists() or list(built.arenas_dir.iterdir()) == []


def test_a_dispatcher_that_is_not_deferred_keeps_the_arena_root_directories(tmp_path):
    """S2c, the control: the factory's default (a run of no registered project) makes the role
    directories in the arena root at build time and an unbind does not take them away."""
    run = tmp_path / "runs" / "r1"
    built = dispatchers.build_dispatcher(
        answers_dir=run / "answers", profiles={r: "claude" for r in Role}, cwd=tmp_path,
        prefer_herdr=False)
    assert built.role_scratch_deferred is False
    assert {r: p.parent for r, p in built.role_scratch.items()} == {r: built.arenas_dir for r in Role}
    before = dict(built.role_scratch)
    built.unbind_role_scratch()
    assert built.role_scratch == before and all(p.is_dir() for p in before.values())
    assert all(built._scratch_env(r) == {"TMPDIR": str(before[r])} for r in Role)


def test_binding_never_makes_the_workspace_and_a_vanished_one_is_refused(tmp_path):
    """S3. The workspace is the creator's own registered directory: a binding to a path that
    does not exist raises and makes nothing; a bound workspace that was moved away is not made
    again by `ensure_role_scratch` (it would be an unregistered directory)."""
    run = tmp_path / "runs" / "r1"
    built = dispatchers.build_dispatcher(
        answers_dir=run / "answers", profiles={r: "claude" for r in Role}, cwd=tmp_path,
        prefer_herdr=False, defer_role_scratch=True)
    missing = tmp_path / "no-such-workspace"
    with pytest.raises(DispatchError):
        built.bind_role_scratch(missing)
    assert not missing.exists() and built.role_scratch == {}
    workspace = tmp_path / "ws"
    workspace.mkdir()
    built.bind_role_scratch(workspace)
    moved = tmp_path / "ws.moved"
    os.rename(workspace, moved)
    with pytest.raises(DispatchError):
        built.ensure_role_scratch(Role.QA)
    assert not workspace.exists(), "the vanished workspace was made again, unregistered"


# --- the role-like process: what it writes by default -------------------------------------


def _role_like(env: dict, cwd: Path, code: str, tmp_path: Path) -> dict:
    """A process that writes helper files the way a role does, with the pane's environment:
    the default temporary-file APIs, a file in `$TMPDIR/..`, and a file named by a relative
    path. Returns what it made."""
    base = {k: v for k, v in os.environ.items() if k not in ("TMPDIR", "PYTEST_ADDOPTS")}
    base.update(env)
    done = subprocess.run([sys.executable, "-B", "-c", code], cwd=cwd, env=base,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


DEFAULTS = r'''
import json, os, tempfile
made = {}
fd, made["mkstemp"] = tempfile.mkstemp(suffix=".py"); os.close(fd)
with tempfile.NamedTemporaryFile(delete=False) as fh: made["named"] = fh.name
made["mkdtemp"] = tempfile.mkdtemp()
with open(os.path.join(os.environ["TMPDIR"], "..", "mkplan.py"), "w"): pass
made["stray"] = os.path.normpath(os.path.join(os.environ["TMPDIR"], "..", "mkplan.py"))
print(json.dumps(made))
'''


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_a_role_like_process_writing_a_helper_by_default_lands_in_its_scratch_not_the_arena_root(
        tmp_path, monkeypatch, role):
    """S4. The property the slice is for, on the pane's own environment. A process started with
    the pane's `--env` and cwd that makes temporary files by default (`mkstemp`, a named
    temporary file, `mkdtemp`) has every one of them inside the role's directory -- bound to a
    registered workspace -- and the file the field planner made in `$TMPDIR/..` is in the
    workspace, not in the arena root. Negative control below: the same process on a dispatcher
    that registered its directories in the arena root puts that stray in the arena root."""
    workspace = tmp_path / "ws"
    env, d = _pane_env(tmp_path, monkeypatch, role, bind=workspace)
    arena_before = sorted(os.listdir(d.arenas_dir)) if d.arenas_dir.is_dir() else []
    made = _role_like(env, tmp_path, DEFAULTS, tmp_path)
    scratch = Path(env["TMPDIR"])
    for key in ("mkstemp", "named", "mkdtemp"):
        assert _inside(Path(made[key]), scratch), (key, made[key], scratch)
    assert Path(made["stray"]) == workspace / "mkplan.py"
    assert (workspace / "mkplan.py").exists()
    arena_after = sorted(os.listdir(d.arenas_dir)) if d.arenas_dir.is_dir() else []
    assert arena_after == arena_before, "the arena root gained a child"


def test_the_same_process_on_an_arena_root_directory_puts_the_stray_in_the_arena_root(
        tmp_path, monkeypatch):
    """S4c, the control and the field case: with the role directory a child of the arena root, the
    default files are in the scratch and `$TMPDIR/..` is the arena root itself -- the child the
    witness reports. (Hence the sentence in the role prompt, and the registered workspace.)"""
    env, d = _pane_env(tmp_path, monkeypatch, Role.PLANNER, bind=None)
    made = _role_like(env, tmp_path, DEFAULTS, tmp_path)
    scratch = Path(env["TMPDIR"])
    assert scratch.parent == d.arenas_dir
    assert all(_inside(Path(made[k]), scratch) for k in ("mkstemp", "named", "mkdtemp"))
    assert Path(made["stray"]) == d.arenas_dir / "mkplan.py" and (d.arenas_dir / "mkplan.py").exists()


# --- a pane that outlives its iteration ------------------------------------------------------


def _only_kept_and_nothing_closed(d, fake, exc, pane) -> None:
    """G1 of the second review: "neither reused nor closed" has to hold for the error path
    of `hoh run` too. That path is `cli._waiting_for_approval(exc)` (the tabs stay open, by
    type) and otherwise `close_own`, which re-takes the earlier process's panes from the
    registry and closes every own pane that is not kept. Driven here for real: the refusal is
    of the type the CLI keeps tabs for, the pane is recorded as kept, and `close_own` closes
    nothing."""
    assert cli._waiting_for_approval(exc), "the CLI keeps the role tabs open for this type"
    assert pane in d._kept()
    closes_before = [c for c in fake.calls if c[0].startswith("close")]
    d.close_own()
    closes = [c for c in fake.calls if c[0].startswith("close")]
    assert closes == closes_before == [], closes
    assert pane in fake.panes, "the pane is still there"


def _deferred_herdr(tmp_path, monkeypatch):
    """A Herdr dispatcher over the fake control, deferred as the factory leaves it."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    d.role_scratch_deferred = True
    return d, state, fake


def _opened(fake) -> int:
    return len([c for c in fake.calls if c[0] == "open_surface"])


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_a_pane_opened_for_an_earlier_iteration_is_refused_after_the_rebind(
        tmp_path, monkeypatch, role):
    """S8. F1 of the first review, PROVEN there with a probe: a pane keeps the environment it
    was opened with; the workspace of its iteration is released and the next iteration binds
    another, so the pane's `TMPDIR` is a directory that is gone and Python falls back to
    `/tmp`. Here: bind ws1, open the pane, unbind, bind ws2, ask for the role's agent again --
    refused with the reason, no second pane, the old pane not closed. Controls: the same
    binding (the same iteration) reuses the pane; a dispatcher that is not deferred reuses
    its pane across an unbind and a bind of nothing."""
    d, state, fake = _deferred_herdr(tmp_path, monkeypatch)
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    d.bind_role_scratch(ws1)
    name = d._ensure_agent(role, state)
    assert _opened(fake) == 1
    assert d._ensure_agent(role, state) == name, "the same iteration reuses its pane"
    assert _opened(fake) == 1
    d.unbind_role_scratch()
    d.bind_role_scratch(ws2)
    pane = d._role_panes()[role]
    with pytest.raises(PaneNotClean, match=r"TMPDIR.*ws1|ws1.*TMPDIR") as caught:
        d._ensure_agent(role, state)
    assert _opened(fake) == 1, "no second pane was opened"
    assert d.agents[role] == name and pane in d._kept(), "the pane is kept"
    _only_kept_and_nothing_closed(d, fake, caught.value, pane)


def test_a_pane_is_refused_while_nothing_is_bound(tmp_path, monkeypatch):
    """S8b. The unbind between two iterations leaves a deferred dispatcher with no scratch, and
    a pane of the iteration before is not handed out in that window either."""
    d, state, fake = _deferred_herdr(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    d.bind_role_scratch(ws)
    d._ensure_agent(Role.QA, state)
    pane = d._role_panes()[Role.QA]
    d.unbind_role_scratch()
    with pytest.raises(PaneNotClean, match="qa") as caught:
        d._ensure_agent(Role.QA, state)
    _only_kept_and_nothing_closed(d, fake, caught.value, pane)


@pytest.mark.parametrize("deferred", [True, False], ids=["deferred", "not-deferred"])
def test_an_agent_an_earlier_process_left_in_a_pane_is_refused_by_a_deferred_dispatcher(
        tmp_path, monkeypatch, deferred):
    """S9. F1, the approval-resume route: a pane kept for an approval is found by name by the
    next process (`_reusable_agent`, the real lookup over the fake control). Its environment is
    unknown, and its workspace is a released one. A deferred dispatcher refuses it with the
    type the CLI keeps tabs for, keeps the pane, does not make it the role's agent and closes
    nothing -- also through `close_own` (G1); the control, a dispatcher that is not deferred,
    adopts it exactly as before."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    # what the earlier process left: an idle agent of this run's name in a pane of this run's directory
    left = fake.open_surface(cwd=str(d.store_dir), label="hoh planner")
    name = d._agent_name(Role.PLANNER, state)
    fake.start(name, kind="claude", pane_id=left.pane_id)
    opened = _opened(fake)
    if deferred:
        d.role_scratch_deferred = True
        ws = tmp_path / "ws"
        ws.mkdir()
        d.bind_role_scratch(ws)
        with pytest.raises(PaneNotClean, match="earlier process") as caught:
            d._ensure_agent(Role.PLANNER, state)
        assert Role.PLANNER not in d.agents and _opened(fake) == opened
        assert not [c for c in fake.calls if c[0].startswith("close")]
        _only_kept_and_nothing_closed(d, fake, caught.value, left.pane_id)
    else:
        d.register_role_scratch()
        assert d._ensure_agent(Role.PLANNER, state) == name
        assert d.agents[Role.PLANNER] == name and _opened(fake) == opened
        assert left.pane_id not in d._kept()


# --- the prompt, built and read back --------------------------------------------------------


def _built_prompts() -> dict[str, str]:
    """The three role prompts as the controller builds them (the real functions)."""
    from hoh.contracts import AcceptanceCheck, DevelopmentPlan
    from hoh.evidence import EvidenceBundle

    plan = DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="b", spec_digest="d", objective="z",
        targets=["t"], acceptance_checks=[AcceptanceCheck(
            check_id="K1", description="d", command="true", expect_exit=0)])
    evidence = EvidenceBundle(run_id="r", iteration=1)
    return {
        "planner": roles.planner_prompt(
            iteration=1, spec_text="s", evidence=evidence, repo_path="/p",
            base_candidate_id="b", spec_digest="d", run_id="r"),
        "developer": roles.developer_prompt(
            iteration=1, attempt=1, spec_text="s", plan=plan, workspace="/w",
            warm_start=False, evidence=evidence),
        "qa": roles.qa_prompt(
            iteration=1, spec_text="s", plan=plan, candidate_path="a",
            candidate_binding="x", receipts_summary="-"),
    }


@pytest.mark.parametrize("role", ["planner", "developer", "qa"])
def test_every_role_prompt_tells_the_role_where_its_helper_files_go(role):
    """S5. Read back from the prompt each role's builder returns (not from the constant): the
    helper-file rule is there -- inside `$TMPDIR`, nothing beside or above it, `$TMPDIR/..` named,
    no probing with an empty file, and the consequence (a new entry in the arena root ends the run
    for the planner and the developer) -- and the pytest bullet it extends is still there."""
    prompt = " ".join(_built_prompts()[role].split())
    for part in ("A helper file goes inside `$TMPDIR`, and nothing beside or above it.",
                 "Do not write to `$TMPDIR/..`",
                 "the directory your working directory sits in",
                 "do not make an empty file to see whether a location is writable",
                 "for the planner and the developer, the listing of the arena root",
                 "ends the run as a capability violation",
                 "a tool that honours `TMPDIR` already does",
                 "pytest stays in your own TMPDIR"):
        assert part in prompt, (role, part)
    assert PREFIX in prompt


# --- the whole `hoh run`, with a role-like process --------------------------------------------


def _answers(repo: Path, state) -> dict:
    """What the planner and QA answer in iteration 1: the fake's own documents."""
    fake = Fake(repo)
    st = state.model_copy(update={"iteration": 1, "attempt": 1})
    return {"planner": fake.dispatch(Role.PLANNER, "", state=st),
            "qa": fake.dispatch(Role.QA, "", state=st)}


def _run(tmp_path, repo, w, monkeypatch, *, project, stray=None, fail=None, arena_stray=None):
    """`hoh run` (the real parser and `_run_in`) with the real `build_dispatcher` and the real
    `HarnessDispatcher.dispatch`, whose agent command is `ROLEISH`. Returns a namespace: `rc`, `store`,
    `admission`, `reports` (what each role-like process reported), `seen` (the kwargs of each
    `build_dispatcher` call), `built` (what it returned) and `spawned` ((role, cwd, env) of each
    agent process the real dispatch started)."""
    spec, store, state = new_run(tmp_path, repo, project=project, run_id="r1")
    out = tmp_path / "roleish-out"
    out.mkdir()
    (tmp_path / "roleish.py").write_text(ROLEISH, encoding="utf-8")
    (tmp_path / "answers.json").write_text(json.dumps(_answers(repo, state)), encoding="utf-8")
    real_run = subprocess.run
    heads = {"[role/project-planner]": "planner", "[role/developer]": "developer",
             "[role/qa-tester]": "qa"}
    spawned = []

    def stand_in(command, **kw):
        """The agent CLI. The real `HarnessDispatcher.dispatch` calls this with the command line
        (the role prompt as its last word), the cwd and the env it built; the prompt names words
        the suite's process guard refuses as argv, so the role-like process gets the role's name
        instead and the cwd and env unchanged."""
        role = heads[command[-1].splitlines()[0]]
        spawned.append((role, kw["cwd"], kw["env"]))
        return real_run([sys.executable, "-B", str(tmp_path / "roleish.py"), role], **kw)

    class Proc:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        run = staticmethod(stand_in)

    monkeypatch.setattr(dispatchers, "subprocess", Proc())
    monkeypatch.setenv("ROLEISH_OUT", str(out))
    monkeypatch.setenv("ROLEISH_ANSWERS", str(tmp_path / "answers.json"))
    for name, value in (("ROLEISH_STRAY", stray), ("ROLEISH_FAIL", fail),
                        ("ROLEISH_ARENA_STRAY_ROLE", arena_stray)):
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROLEISH_ARENA_STRAY", str(store.arenas_dir / "intrusion.txt"))
    admission = Admission(tmp_path / "ledger")
    monkeypatch.setattr(cli, "storage_admission_for",
                        lambda option, **kw: (admission, w.registry.state_root))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "StorageRegistry", lambda *a, **k: w.registry)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))
    seen, built = [], []
    real_build = dispatchers.build_dispatcher

    def build(**kw):
        seen.append(kw)
        built.append(real_build(**kw))
        return built[-1]

    monkeypatch.setattr(dispatchers, "build_dispatcher", build)
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--no-herdr", "--iterations", "1"])
    reports = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("*.json"))}
    return types.SimpleNamespace(rc=rc, store=store, admission=admission, reports=reports,
                                 seen=seen, spawned=spawned, built=built)


def test_hoh_run_of_a_registered_project_keeps_every_role_scratch_in_the_iteration_workspace(
        tmp_path, repo, w, monkeypatch, capsys):
    """S6. One real `hoh run` iteration (NONE) of a registered project, the planner making the
    field stray. Read back from what the role-like processes reported while they ran: each one's
    `TMPDIR` is `<root>/<iteration workspace>/.hoh-scratch-role-<role>`; the workspace's record
    said ACTIVE, purpose `run`, creator `controller.run_iteration` at that moment; the default
    temporary files and the scratchpad are inside the role directory; the planner's stray is in
    the workspace (`$TMPDIR/..`), not in the arena root; the witness stayed silent and the
    iteration was accepted; the factory was asked to defer the role scratch; the arena root has
    no `.hoh-scratch-*` and no stray; and when the run is over the workspace and everything in
    it, the stray included, is gone and its record says RELEASED and removed. Negative control:
    S7 (the project is not registered)."""
    res = _run(tmp_path, repo, w, monkeypatch, project="pd", stray="planner")
    rc, store, admission, reports, seen, spawned, built = (
        res.rc, res.store, res.admission, res.reports, res.seen, res.spawned, res.built)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [kw.get("defer_role_scratch") for kw in seen] == [True]
    assert set(reports) == {"planner", "developer", "qa"}
    root = tmp_root(w, "r1")
    workspaces = set()
    for role, made in reports.items():
        scratch = Path(made["TMPDIR"])
        assert scratch.name == f"{PREFIX}{role}" and scratch.parent.parent == root, made["TMPDIR"]
        workspaces.add(scratch.parent)
        for key in ("mkstemp", "named", "mkdtemp", "scratchpad"):
            assert _inside(Path(made[key]), scratch), (role, key, made[key])
        assert made["gettempdir"] == str(scratch)
        rec = made["workspace_record"]
        assert (rec["state"], rec["purpose"], rec["creator"], rec["run_id"]) == (
            "ACTIVE", "run", "controller.run_iteration", "r1"), rec
    assert [(r, e["TMPDIR"]) for r, _cwd, e in spawned] == [
        (r, reports[r]["TMPDIR"]) for r in ("planner", "developer", "qa")], (
        "the env the real dispatch handed the agent is the one the process saw")
    assert len(workspaces) == 1, "one iteration, one workspace for the three roles"
    (workspace,) = workspaces
    assert reports["planner"]["stray"] == str(workspace / "mkplan.py")
    assert not workspace.exists(), "the iteration released its workspace, the stray with it"
    assert beside_the_arenas(store) == []
    assert not [n for n in os.listdir(store.arenas_dir) if n == "mkplan.py"]
    found = [r for r in records(root) if r["purpose"] == "run"]
    assert len(found) == 1 and found[0]["state"] == "RELEASED" and found[0]["removed"] is True
    assert admission.kinds("tmp_workspace") == 3 and admission.open == {}
    state = store.read_state()
    assert state.blocked_kind is None and state.last_accepted_candidate is not None
    assert "scratch (TMPDIR)" in "\n".join(state.history)
    assert len(built) == 1 and built[0].role_scratch == {} and built[0]._role_scratch_base is None, (
        "the iteration took its role directories out again when it released the workspace")


def test_the_witness_still_blocks_the_stray_where_the_role_directory_is_in_the_arena_root(
        tmp_path, w, monkeypatch, capsys):
    """S7, the control of S6 and the guarantee that the witness is not weakened: the same run of
    a project that is not registered (no workspace, so the role directories are the arena root's
    children, as before) with the same stray. The role's default files are in its role directory,
    the factory was not asked to defer (`defer_role_scratch` absent or false), and the stray -- a new
    child of the arena root -- is a capability violation: the run blocks, kind `capability_violation`, naming the arena root."""
    own = standalone_repo(tmp_path / "own")
    res = _run(tmp_path, own, w, monkeypatch, project="unknown-project", stray="planner")
    rc, store, admission, reports, seen = res.rc, res.store, res.admission, res.reports, res.seen
    out = capsys.readouterr().out
    state = store.read_state()
    assert len(seen) == 1 and not seen[0].get("defer_role_scratch")
    assert state.blocked_kind == "capability_violation", (rc, out, state.blocked_reason)
    assert "gained or lost a child" in (state.blocked_reason or "")
    assert str(store.arenas_dir) in (state.blocked_reason or "")
    planner = reports["planner"]
    scratch = Path(planner["TMPDIR"])
    assert scratch == store.arenas_dir / f"{PREFIX}planner"
    assert all(_inside(Path(planner[k]), scratch) for k in ("mkstemp", "named", "mkdtemp", "scratchpad"))
    assert Path(planner["stray"]) == store.arenas_dir / "mkplan.py"
    assert (store.arenas_dir / "mkplan.py").exists(), "left where it was, as evidence"
    assert "workspace_record" not in planner
    assert admission.kinds("tmp_workspace") == 0


def test_an_unregistered_run_without_a_stray_is_accepted_with_its_role_directories_in_the_arena_root(
        tmp_path, w, monkeypatch, capsys):
    """S7b, the control of S7: no stray, the same unregistered run is accepted -- so S7 blocked
    for the stray and for nothing else -- and each role's `TMPDIR` is its arena-root directory."""
    own = standalone_repo(tmp_path / "own")
    res = _run(tmp_path, own, w, monkeypatch, project="unknown-project", stray=None)
    rc, store, reports = res.rc, res.store, res.reports
    out = capsys.readouterr().out
    assert rc == 0, out
    assert store.read_state().blocked_kind is None
    for role, made in reports.items():
        assert Path(made["TMPDIR"]) == store.arenas_dir / f"{PREFIX}{role}"


def test_the_witness_still_blocks_a_write_into_the_arena_root_of_a_registered_run(
        tmp_path, repo, w, monkeypatch, capsys):
    """S10 (A-F2). In a registered run `$TMPDIR/..` is the run's own workspace root, scratch the
    run owns and no policy watches; the witness itself is untouched. A role that writes into the
    arena root -- a path it names, not a default -- is still a capability violation. (S7 holds
    the same for the unregistered run.)"""
    res = _run(tmp_path, repo, w, monkeypatch, project="pd", arena_stray="planner")
    shown = [d["ephemeral_roots"] for d in documents(capsys.readouterr().out) if "ephemeral_roots" in d]
    assert [s.get("project") for s in shown] == ["pd"], "the run is a registered project's"
    state = res.store.read_state()
    assert state.blocked_kind == "capability_violation", (res.rc, state.blocked_reason)
    assert str(res.store.arenas_dir) in (state.blocked_reason or "")
    assert (res.store.arenas_dir / "intrusion.txt").exists(), "left where it was, as evidence"


@pytest.mark.parametrize("role", ["planner", "developer"])
def test_a_dispatch_that_fails_still_takes_the_role_directories_out_and_releases_the_workspace(
        tmp_path, repo, w, monkeypatch, capsys, role):
    """S11 (B-1). The role-like process exits non-zero without an answer, so the dispatch raises
    inside the iteration. `_run_temp` leaves through its `finally`: the dispatcher has no
    role directories bound any more and the iteration's workspace is released and removed."""
    try:
        res = _run(tmp_path, repo, w, monkeypatch, project="pd", fail=role)
    finally:
        capsys.readouterr()
    assert [kw.get("defer_role_scratch") for kw in res.seen] == [True]
    assert role in res.reports, "the dispatch really ran"
    (dispatcher,) = res.built
    assert dispatcher.role_scratch == {} and dispatcher._role_scratch_base is None
    found = [r for r in records(tmp_root(w, "r1")) if r["purpose"] == "run"]
    assert len(found) == 1 and found[0]["state"] == "RELEASED" and found[0]["removed"] is True
    assert workspace_dirs(tmp_root(w, "r1")) == []
