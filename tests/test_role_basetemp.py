"""Every pytest a role runs has a basetemp of its own, under the run's arena root.

The incident (run p217c, 2026-10-08): a QA role ran a scratch pytest on the
shared default basetemp `/tmp/pytest-of-<user>`. The house rules (amendment A3,
point 3) call that directory foreign: pytest's retention removes the numbered
directories of *other* pytest processes there, and it printed `rm_rf` warnings on
`garbage-*` directories that were not the role's. The role's pane had no
`TMPDIR`, so pytest fell back to `/tmp`.

What is shown here, each by running the code and not by reading it:

* the **launcher**: a role pane is opened with `TMPDIR` in its `--env`, pointing at
  the role's own directory below the run's arena root, never at `/tmp/pytest-of-*`;
  three roles, three different directories; a real pytest started with that
  environment puts its basetemp inside the directory;
* **no `PYTEST_ADDOPTS` and no `--basetemp`** is set by the product anywhere: pytest
  empties an existing `--basetemp`, and an environment variable cannot name a fresh
  path per run (house rules, A3 point 3);
* the **direct path** (`HarnessDispatcher`, `--no-herdr`): the child's environment
  carries the same `TMPDIR`;
* the directory exists **before** the first dispatch and the capability witness
  does not count what pytest writes into it as a protected-tree change;
* the **role prompts** state the rule;
* the **runner**'s own checks already run with `TMPDIR` in their own scratch, in
  both isolation regimes: a guard that stays green, not a discriminating criterion.

Nothing here starts Herdr or an agent: `subprocess` inside `hoh.dispatchers` is
replaced by something that fails the test, and the agent runtime is the fake of
`test_agent_control`.
"""

from __future__ import annotations

import ast
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from test_agent_control import FakeAgentControl, _dispatcher, _no_process
from test_policy_wiring import Dispatcher, build, repo, spec  # noqa: F401

from hoh import dispatchers, roles
from hoh.contracts import Role
from hoh.controller import DispatchError

ROLES = [Role.PLANNER, Role.DEVELOPER, Role.QA]


@pytest.fixture(autouse=True)
def herdr_stub(tmp_path_factory, monkeypatch):
    """A `herdr` first on PATH that logs every call. None of these tests may call it."""
    bin_dir = tmp_path_factory.mktemp("stubbin")
    log = bin_dir / "calls.log"
    stub = bin_dir / "herdr"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {shlex.quote(str(log))}\nexit 1\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert Path(shutil.which("herdr")) == stub
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", "a test reached Herdr"


def _inside(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents or path.resolve() == root.resolve()


def _pane_env(tmp_path, monkeypatch, role: Role):
    """The `--env` a role's pane is opened with, and the dispatcher that opened it."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    d.register_role_scratch()
    assert sorted(p.name for p in d.arenas_dir.iterdir()) == [
        f".hoh-scratch-role-{r.value}" for r in sorted(Role)], (
        "every role's directory exists before the first dispatch: the witness "
        "compares the arena root's listing around it")
    d._ensure_agent(role, state)
    opened = [c for c in fake.calls if c[0] == "open_surface"]
    assert len(opened) == 1, "one tab for one role"
    return opened[0][3], d


# --- the launcher ------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_a_role_pane_is_opened_with_a_tmpdir_inside_its_own_scratch(
        tmp_path, monkeypatch, role):
    env, d = _pane_env(tmp_path, monkeypatch, role)

    assert "TMPDIR" in env, (
        f"the {role.value} pane has no scratch environment: its pytest takes /tmp")
    scratch = Path(env["TMPDIR"])
    assert scratch == d.arenas_dir / f".hoh-scratch-role-{role.value}"
    assert scratch.is_dir(), "made before the pane is opened, not by the role"
    assert d.arenas_dir.resolve() in scratch.resolve().parents, (
        "below the run's arena root, the one directory family the run owns")
    below_the_root = scratch.resolve().relative_to(d.arenas_dir.resolve()).parts
    assert not any(part.startswith("pytest-of-") for part in below_the_root), (
        "the shared default family `pytest-of-<user>` is not where a role's pytest goes")
    # The old variable of O215 is still there: this adds, it does not replace.
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_three_roles_get_three_different_directories(tmp_path, monkeypatch):
    seen = {}
    for role in ROLES:
        (tmp_path / role.value).mkdir()
        env, _ = _pane_env(tmp_path / role.value, monkeypatch, role)
        seen[role] = env["TMPDIR"]
    assert len(set(seen.values())) == 3


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_the_product_sets_no_pytest_addopts_and_no_basetemp(tmp_path, monkeypatch, role):
    """pytest empties an existing `--basetemp` before every run, so two runs of one
    role would wipe each other's trees; the rule allows only a fresh path per run, which
    no static variable can name. TMPDIR alone is the product's whole contribution."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    env, d = _pane_env(tmp_path, monkeypatch, role)
    assert "PYTEST_ADDOPTS" not in env
    assert not any("basetemp" in k.lower() or "--basetemp" in v for k, v in env.items()), env
    assert "PYTEST_ADDOPTS" not in dispatchers.role_scratch_env(env["TMPDIR"])
    assert "PYTEST_ADDOPTS" not in dispatchers.role_environment(
        dispatchers.role_scratch_env(env["TMPDIR"]))
    tree = ast.parse(Path(dispatchers.__file__).read_text(encoding="utf-8"))
    names = [n for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == "PYTEST_ADDOPTS"]
    assert not names, (
        "a string constant 'PYTEST_ADDOPTS' in dispatchers.py (any quote style, any form: "
        f"environ[...], setdefault, a dict key) at lines {[n.lineno for n in names]}")


def test_a_pytest_started_with_the_pane_environment_keeps_its_basetemp_in_the_scratch(
        tmp_path, monkeypatch):
    """The property the whole change is for, on a real pytest: with the pane's `TMPDIR` and
    nothing else, the default basetemp `$TMPDIR/pytest-of-<user>` is inside the role's own
    directory. (Its retention then touches only that role's earlier runs.)"""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    env, d = _pane_env(tmp_path, monkeypatch, Role.QA)
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_probe.py").write_text(
        "def test_probe(tmp_path):\n    print('PROBE_TMP_PATH', tmp_path)\n", encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    child_env.update(env)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "test_probe.py"],
        cwd=project, env=child_env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    printed = [line.split(" ", 1)[1] for line in done.stdout.splitlines()
               if line.startswith("PROBE_TMP_PATH ")]
    assert printed, done.stdout
    scratch = Path(env["TMPDIR"])
    assert _inside(Path(printed[0]), scratch), (printed[0], scratch)
    assert any(p.name.startswith("pytest-of-") for p in scratch.iterdir()), (
        "the default basetemp family is inside the role's directory")


def test_a_scratch_that_cannot_be_made_refuses_the_dispatcher_and_does_not_fall_back(
        tmp_path, monkeypatch):
    """No `/tmp` fallback: a name that is a link, or not a directory, refuses."""
    _no_process(monkeypatch)
    answers = tmp_path / "run" / "answers"
    answers.mkdir(parents=True)
    arenas = tmp_path / "_arenas" / "run"
    arenas.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (arenas / ".hoh-scratch-role-qa").symlink_to(elsewhere, target_is_directory=True)

    d = dispatchers.HerdrDispatcher(
        answers_dir=answers, profiles={r: "claude" for r in Role}, cwd=tmp_path,
        control=FakeAgentControl())
    with pytest.raises(DispatchError, match="qa"):
        d.register_role_scratch()


def test_build_dispatcher_registers_the_scratch_of_both_adapters(tmp_path, monkeypatch):
    """The one factory the CLI builds its dispatcher with: the scratch exists, for
    every role, when it returns -- before a single dispatch."""
    from hoh import herdr

    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    for prefer_herdr in (True, False):
        run = tmp_path / ("herdr" if prefer_herdr else "direct") / "runs" / "r1"
        built = dispatchers.build_dispatcher(
            answers_dir=run / "answers", profiles={r: "claude" for r in Role}, cwd=tmp_path,
            prefer_herdr=prefer_herdr)
        assert isinstance(built, dispatchers.HerdrDispatcher if prefer_herdr
                          else dispatchers.HarnessDispatcher)
        assert set(built.role_scratch) == set(Role)
        for role, directory in built.role_scratch.items():
            assert directory == built.arenas_dir / f".hoh-scratch-role-{role.value}"
            assert directory.is_dir()
            assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
        assert "PYTEST_ADDOPTS" not in os.environ, "the product does not set it process-wide either"
        # A resumed run builds its dispatcher again over the same directories.
        before = dict(built.role_scratch)
        built.register_role_scratch()
        assert built.role_scratch == before and all(d.is_dir() for d in before.values())


def test_a_dispatcher_nobody_registered_makes_no_directory(tmp_path):
    """Constructing a dispatcher (as many tests do, some on `/tmp`) writes nothing
    but its answers directory."""
    answers = tmp_path / "run" / "answers"
    d = dispatchers.HarnessDispatcher(
        answers_dir=answers, profiles={}, cwd=tmp_path)
    assert d.role_scratch == {} and not (tmp_path / "_arenas").exists()


# --- the direct path ----------------------------------------------------------


def test_the_direct_subprocess_path_carries_the_same_tmpdir(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    answers = tmp_path / "run" / "answers"
    answers.mkdir(parents=True)
    d = dispatchers.HarnessDispatcher(
        answers_dir=answers, profiles={r: "claude" for r in Role}, cwd=tmp_path)
    d.register_role_scratch()
    got = {}

    def fake_run(argv, **kwargs):
        got["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=fake_run, TimeoutExpired=Exception))
    state = types.SimpleNamespace(run_id="r1", iteration=1, attempt=1)
    try:
        d.dispatch(Role.QA, "review", state=state)
    except Exception:
        pass   # what the reply parser makes of "{}" is not this test's subject

    env = got["env"]
    assert env["TMPDIR"] == str(d.arenas_dir / ".hoh-scratch-role-qa")
    assert "PYTEST_ADDOPTS" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_an_inherited_pytest_addopts_passes_through_the_direct_path_unchanged(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    env = dispatchers.role_environment(dispatchers.role_scratch_env("/x/scratch"))
    assert env["PYTEST_ADDOPTS"] == "-q" and env["TMPDIR"] == "/x/scratch"


def test_role_scratch_env_is_exactly_one_key():
    assert set(dispatchers.role_scratch_env("/x/scratch")) == {"TMPDIR"}


def test_a_directory_of_someone_else_or_an_unmakeable_one_refuses(tmp_path, monkeypatch):
    arenas = tmp_path / "_arenas" / "run"
    with monkeypatch.context() as m:
        m.setattr(os, "geteuid", lambda: os.getuid() + 12345)
        with pytest.raises(DispatchError, match="own"):
            dispatchers._role_scratch_dirs(arenas)
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(DispatchError, match="cannot be made"):
        dispatchers._role_scratch_dirs(blocker / "run")


def test_ensure_role_scratch_makes_a_missing_directory_again(tmp_path, monkeypatch):
    _no_process(monkeypatch)
    d, _ = _dispatcher(tmp_path, FakeAgentControl())
    d.ensure_role_scratch(Role.QA)                       # nothing registered: a no-op
    assert not d.arenas_dir.exists() or not list(d.arenas_dir.iterdir())
    d.register_role_scratch()
    gone = d.role_scratch[Role.QA]
    gone.rename(gone.with_name(gone.name + ".moved-away"))   # what a prune used to do
    assert not gone.exists()
    d.ensure_role_scratch(Role.QA)
    assert gone.is_dir() and stat.S_IMODE(os.lstat(gone).st_mode) == 0o700
    for other in (Role.PLANNER, Role.DEVELOPER):
        assert d.role_scratch[other].is_dir()


def test_the_controller_makes_the_scratch_before_it_takes_the_witness(tmp_path, repo, spec):   # noqa: F811
    """Made *inside* the dispatch the directory is a new child of the arena root and the
    planner's and developer's listing watch reports it; made before, nothing is reported."""
    class Scratching(Dispatcher):
        def ensure_role_scratch(self, role):
            self.ensured = getattr(self, "ensured", []) + [role]
            dispatchers._role_scratch_dirs(arenas_dir_of_the_run, (role,))

    dispatcher = Scratching(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, dispatcher)
    arenas_dir_of_the_run = store.arenas_dir
    for role in (Role.PLANNER, Role.DEVELOPER):
        directory = store.arenas_dir / f".hoh-scratch-role-{role.value}"
        assert not directory.exists()
        ctrl._dispatch_admitted(role, "work", state)          # no CapabilityViolation
        assert directory.is_dir()
    assert dispatcher.ensured == [Role.PLANNER, Role.DEVELOPER]


def test_prune_never_moves_a_role_scratch(tmp_path):
    """The reproduction of the review finding: `_end_of_run` prunes also when the role
    tabs stay open, and a pane whose `TMPDIR` was filed away makes Python use `/tmp`."""
    from hoh.store import RunStore

    store = RunStore(tmp_path, "r1")
    arenas = store.arenas_dir
    now = time.time()
    for i, role in enumerate(ROLES):
        directory = arenas / f".hoh-scratch-role-{role.value}"
        directory.mkdir()
        os.utime(directory, (now - 100 + i, now - 100 + i))
    receipts = [arenas / f".hoh-scratch-r1-i1-a1-K{n}" for n in range(1, 6)]
    for n, directory in enumerate(receipts):
        directory.mkdir()
        os.utime(directory, (now - 50 + n, now - 50 + n))

    moved = store.prune()["arenas"]

    for role in ROLES:
        assert (arenas / f".hoh-scratch-role-{role.value}").is_dir(), moved
    assert not any(name.startswith(".hoh-scratch-role-") for name in moved)
    # The runner's own receipt scratch is still filed by age, three kept.
    assert sorted(moved) == sorted(d.name for d in receipts[:2])


# --- the witness ----------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_what_pytest_writes_into_a_role_scratch_is_not_a_witness_violation(
        tmp_path, repo, spec, role):   # noqa: F811
    """The directory exists before the witness is taken (a new child of the arena
    root *during* a dispatch is reported) and its content is not a protected tree
    (`other_arenas` skips the `.hoh-scratch-` family)."""
    from hoh.capability import CapabilityWitness

    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    scratch = dispatchers._role_scratch_dirs(store.arenas_dir)[role]
    witness = CapabilityWitness.take(ctrl._policy_for(role, state))

    deep = scratch / "pytest-basetemp" / "pytest-0" / "test_x0"
    deep.mkdir(parents=True)
    (deep / "out.txt").write_text("a pytest tmp_path\n", encoding="utf-8")
    (scratch / "pytest-of-someone").mkdir()

    assert witness.violations() == []


# --- the prompts ------------------------------------------------------------------


def test_the_house_rules_block_of_every_role_prompt_states_the_basetemp_rule():
    from hoh.evidence import EvidenceBundle
    from hoh.contracts import AcceptanceCheck, DevelopmentPlan

    plan = DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="c", spec_digest="d",
        objective="g", targets=["t"],
        acceptance_checks=[AcceptanceCheck(check_id="K1", description="d",
                                           command="true", expect_exit=0)])
    prompts = {
        "planner": roles.planner_prompt(
            iteration=1, spec_text="s", evidence=EvidenceBundle(run_id="r", iteration=1),
            repo_path="/p", base_candidate_id="c", spec_digest="d", run_id="r"),
        "developer": roles.developer_prompt(
            iteration=1, attempt=1, spec_text="s", plan=plan, workspace="/w",
            warm_start=False, evidence=EvidenceBundle(run_id="r", iteration=1)),
        "qa": roles.qa_prompt(
            iteration=1, spec_text="s", plan=plan, candidate_path="a",
            candidate_binding="b", receipts_summary="r"),
    }
    for role, text in prompts.items():
        assert "/tmp/pytest-of-<user>" in text, role
        assert "$TMPDIR/pytest-of-<user>" in text, role
        assert "--basetemp" in text and "TMPDIR" in text, role
        assert "touches only your own earlier runs" in text, role
        assert 'test -d "$TMPDIR"' in text and "stop" in text, role
        assert "existing directory" in text and "does not exist yet" in text, role


# --- the runner's own checks (a guard that stays green) --------------------------


def test_an_unsandboxed_check_runs_with_tempfile_in_its_own_scratch(tmp_path):
    """pytest's default temp root is `tempfile.gettempdir()`, which follows
    `TMPDIR`: this is the property the runner's scratch gives a check's pytest."""
    from hoh import runner
    from hoh.contracts import AcceptanceCheck, Candidate

    arena = tmp_path / "arenas" / "run" / "abc123"
    arena.mkdir(parents=True)
    command = 'python3 -c "import tempfile; print(tempfile.gettempdir())"'
    receipt, log = runner.run_check(
        AcceptanceCheck(check_id="K1", description="probe", command=command, expect_exit=0),
        Candidate(candidate_id="c1", repo_path=str(arena), commit="a" * 40,
                  tree_clean=True, tree_digest="d1"),
        run_id="r1", iteration=1, attempt=1, cwd=arena, isolation=None)
    assert receipt.exit_code == 0, log
    printed = [line for line in log.splitlines() if line.startswith("/")]
    scratch = runner._scratch_dir(arena)
    assert printed and Path(printed[-1]).resolve() == scratch.resolve(), (printed, scratch)
    assert scratch.parent == arena.parent and scratch != Path("/tmp")


def test_the_sandboxed_environment_puts_tmpdir_in_the_scratch(tmp_path):
    from hoh import sandbox

    spec_ = sandbox.SandboxSpec(candidate=tmp_path / "c", scratch=tmp_path / "s")
    env = sandbox._env_for(spec_)
    assert env["TMPDIR"] == env["HOME"] == str(tmp_path / "s")
