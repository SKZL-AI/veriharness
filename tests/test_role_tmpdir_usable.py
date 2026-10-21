"""A role's `TMPDIR` is a directory its own user can really use -- or the run refuses.

Follow-up of `tests/test_role_basetemp.py` (integration review rbtint-a, MINOR 1 and 2;
rbtint-b, MINOR 1 and 2). What is shown here, each by running the code:

* an existing role directory that this user cannot read, write and search (mode 0500, 0300,
  0000, ...) is **refused**, in the one function that makes the directories, at
  registration and again before every dispatch. Python does not complain about such a
  `TMPDIR`: `tempfile.gettempdir()` skips it and takes `/tmp`, which is the incident the
  role directories exist to prevent;
* the refusal is a `DispatchError` the controller does not retry (not infrastructure), and
  nothing is charged and no role is called;
* the length of `TMPDIR`: a role directory below the run's arena root is 117-130 characters
  on the real layout, and a Unix socket path (`multiprocessing.Manager()`, the `forkserver`
  start method) may not exceed 107 bytes. The residual is stated in the role prompt -- as a
  possibility, because it depends on the interpreter: the CPython of the public CI (3.13.16)
  starts the `forkserver` with a 117- and a 130-character `TMPDIR`, the others measured (3.11,
  3.12, 3.13.5) do not -- and what the prompt says is held against a real child process on the
  interpreter running the tests (Linux only);
* the house-rules bullet of the three role prompts is the one the spec shows, word for word.

Nothing here starts Herdr or an agent: the autouse fixture of `test_role_basetemp` puts a
logging stub `herdr` first on PATH and fails a test that reaches it.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from test_agent_control import FakeAgentControl, _dispatcher, _no_process
from test_policy_wiring import Dispatcher, build, repo, spec  # noqa: F401
from test_role_basetemp import herdr_stub  # noqa: F401  (autouse: no test may reach Herdr)

from hoh import dispatchers, failures, roles
from hoh.contracts import Role
from hoh.controller import DispatchError

ROLES = [Role.PLANNER, Role.DEVELOPER, Role.QA]

#: The shortest role directory measured on the real layout (run 20261008t1930z-rbt, role
#: qa); the others measured 121-130. `NOTES.md` section 3 of the follow-up spec.
REAL_LAYOUT_SHORTEST_TMPDIR = 117
#: CPython's `multiprocessing` makes `<TMPDIR>/pymp-XXXXXXXX/listener-XXXXXXXX`: 32 more
#: characters, against 107 usable bytes of `sun_path`.
SOCKET_SUFFIX = len("/pymp-XXXXXXXX/listener-XXXXXXXX")
SUN_PATH_USABLE = 107

BULLET = """\
- **pytest stays in your own TMPDIR.** Never run it on the shared default
  `/tmp/pytest-of-<user>`: that directory belongs to every session of this
  machine, and pytest's retention removes the numbered directories of other
  sessions there. In a pane HoH opened, `TMPDIR` points at a directory of your
  own whose last component starts with `.hoh-scratch-role-`, so pytest's default
  basetemp, `$TMPDIR/pytest-of-<user>`, is yours alone and its retention
  touches only your own earlier runs: leave `TMPDIR` set. If `echo $TMPDIR`
  shows nothing or a path without `.hoh-scratch-role-` in it, or
  `test -d "$TMPDIR"` or `test -w "$TMPDIR"` fails, stop: do not run pytest, and
  say so in your answer (Python falls back to `/tmp` when `TMPDIR` is missing or
  not writable). Never pass `--basetemp` an existing directory -- pytest empties
  it first; give it a fresh path that does not exist yet, below `$TMPDIR`. That
  `TMPDIR` is long (over 100 characters) and a Unix socket path may not exceed
  107 bytes: `multiprocessing.Manager()` or the `forkserver` start method, which
  make theirs below `$TMPDIR`, may fail here with `AF_UNIX path too long`,
  depending on the interpreter and the platform. If they do, that is the layout,
  not the code under test, and the acceptance checks run under a `TMPDIR` just
  as long: report it, and do not unset `TMPDIR` to get around it.
"""

#: The sentence the bullet carried until the CI of CPython 3.13.16 showed it false there: a
#: failure stated as certain. Kept only so that the detector below is itself tested.
CERTAIN_FAILURE_SENTENCE = (
    "`multiprocessing.Manager()` or the `forkserver` start method, which make theirs below "
    "`$TMPDIR`, fail here with `AF_UNIX path too long`.")


@contextlib.contextmanager
def _mode(path: Path, mode: int):
    """`path` with `mode`, put back to 0700 afterwards so pytest's own retention can go on."""
    os.chmod(path, mode)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)


def _not_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("root passes the kernel's access check, and Python uses such a TMPDIR too")


def _arenas(tmp_path: Path) -> Path:
    arenas = tmp_path / "_arenas" / "run"
    arenas.mkdir(parents=True)
    return arenas


# --- an existing directory this user cannot use is refused --------------------------


@pytest.mark.parametrize("mode", [0o500, 0o300, 0o100, 0o400, 0o600, 0o000],
                         ids=lambda m: f"{m:04o}")
def test_an_existing_role_directory_the_owner_cannot_use_is_refused(tmp_path, mode):
    """The review's reproduction: `mkdir -m500 .hoh-scratch-role-qa` was accepted, and
    `gettempdir()` then returned `/tmp`."""
    _not_root()
    arenas = _arenas(tmp_path)
    directory = arenas / ".hoh-scratch-role-qa"
    directory.mkdir(mode=0o700)
    with _mode(directory, mode):
        with pytest.raises(DispatchError) as raised:
            dispatchers._role_scratch_dirs(arenas)
        # A refusal is not a repair: the mode is what the user left, and nothing was changed.
        assert stat.S_IMODE(os.lstat(directory).st_mode) == mode
    message = str(raised.value)
    assert "qa" in message and f"{mode:04o}" in message, message
    assert str(directory) in message and "/tmp" in message, message


def test_a_directory_the_kernel_says_is_not_usable_is_refused_too(tmp_path, monkeypatch):
    """A mode of 0700 is not the whole truth (a read-only file system, an ACL): the answer of
    `os.access` for the effective user counts as well."""
    arenas = _arenas(tmp_path)
    directory = arenas / ".hoh-scratch-role-developer"
    directory.mkdir(mode=0o700)
    real_access = os.access

    def refusing(path, mode, *args, **kwargs):
        if Path(path) == directory:
            return False
        return real_access(path, mode, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "access", refusing)
        with pytest.raises(DispatchError, match="developer"):
            dispatchers._role_scratch_dirs(arenas)


def test_the_refusal_is_not_hypothetical_python_skips_such_a_tmpdir(tmp_path):
    """Why the product must refuse and not trust the directory: with the 0500 directory as
    `TMPDIR`, a child's `tempfile.gettempdir()` is **not** that directory. (Root writes through
    mode bits, and Python uses such a directory too.)"""
    _not_root()
    arenas = _arenas(tmp_path)
    directory = arenas / ".hoh-scratch-role-qa"
    directory.mkdir(mode=0o700)
    with _mode(directory, 0o500):
        env = {**os.environ, "TMPDIR": str(directory)}
        done = subprocess.run(
            [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
            env=env, capture_output=True, text=True, timeout=60)
    # Either Python took another place (`/tmp`; mode 0500 is skipped without a word) or, where
    # there is none (the STRICT sandbox has no `/tmp`), it failed: it never used the directory.
    used = done.returncode == 0 and Path(done.stdout.strip()) == directory
    assert not used, "Python used the unwritable TMPDIR; the refusal would not be needed"


@pytest.mark.parametrize("kind", ["file", "symlink-to-a-directory", "dangling-symlink"])
def test_a_name_that_is_not_a_plain_directory_stays_refused(tmp_path, kind):
    """Guard (green on the pin): a file, or a link, where the role directory belongs."""
    arenas = _arenas(tmp_path)
    name = arenas / ".hoh-scratch-role-planner"
    if kind == "file":
        name.write_text("not a directory\n", encoding="utf-8")
    else:
        target = tmp_path / ("elsewhere" if kind == "symlink-to-a-directory" else "nowhere")
        if kind == "symlink-to-a-directory":
            target.mkdir()
        name.symlink_to(target, target_is_directory=True)
    with pytest.raises(DispatchError, match="planner"):
        dispatchers._role_scratch_dirs(arenas)


def test_a_usable_directory_is_accepted_and_left_as_it_is(tmp_path):
    """Guard (green on the pin): the check refuses what is unusable, it does not touch
    a directory that is fine -- not even one that is more open than 0700."""
    arenas = _arenas(tmp_path)
    existing = arenas / ".hoh-scratch-role-qa"
    existing.mkdir(mode=0o700)
    os.chmod(existing, 0o750)
    made = dispatchers._role_scratch_dirs(arenas)
    assert set(made) == set(Role) and made[Role.QA] == existing
    assert stat.S_IMODE(os.lstat(existing).st_mode) == 0o750
    assert stat.S_IMODE(os.lstat(made[Role.PLANNER]).st_mode) == 0o700


# --- at registration, at dispatch, in the controller ---------------------------------


@pytest.mark.parametrize("prefer_herdr", [True, False], ids=["herdr", "direct"])
def test_build_dispatcher_refuses_an_unusable_role_directory(tmp_path, monkeypatch, prefer_herdr):
    """The one factory the CLI builds its dispatcher with: the run does not start (the CLI
    prints a `build_dispatcher` failure and exits 2), and no pane was opened."""
    from hoh import herdr

    _not_root()
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    run = tmp_path / "runs" / "r1"
    arenas = tmp_path / "runs" / "_arenas" / "r1"
    arenas.mkdir(parents=True)
    directory = arenas / ".hoh-scratch-role-developer"
    directory.mkdir(mode=0o700)
    with _mode(directory, 0o500):
        with pytest.raises(DispatchError, match="developer"):
            dispatchers.build_dispatcher(
                answers_dir=run / "answers", profiles={r: "claude" for r in Role}, cwd=tmp_path,
                prefer_herdr=prefer_herdr)


def test_ensure_role_scratch_refuses_a_directory_that_became_unusable(tmp_path, monkeypatch):
    """Registered fine, then the mode changed before a later dispatch (a role's `chmod`, a
    tool): `ensure_role_scratch` is what the controller calls before every dispatch."""
    _not_root()
    _no_process(monkeypatch)
    d, _ = _dispatcher(tmp_path, FakeAgentControl())
    d.register_role_scratch()
    with _mode(d.role_scratch[Role.QA], 0o500):
        with pytest.raises(DispatchError, match="qa"):
            d.ensure_role_scratch(Role.QA)
        d.ensure_role_scratch(Role.PLANNER)         # the other roles are not affected
    d.ensure_role_scratch(Role.QA)                  # usable again: accepted again


@pytest.mark.parametrize("role, called", [(Role.PLANNER, []), (Role.DEVELOPER, [Role.PLANNER])],
                         ids=["planner", "developer"])
def test_the_controller_refuses_the_dispatch_and_charges_nothing(
        tmp_path, repo, spec, role, called):   # noqa: F811
    """With the real controller (`run_iteration`, which `cli.cmd_run` calls and whose exception it
    turns into "Iteration aborted and run blocked: ..."): the refusal comes before the budget is
    charged and before the role is called -- the planner before the developer, so the developer's
    refusal leaves exactly the planner's call and charge behind -- and the controller does not
    treat it as an outage to wait out (`failures.is_infrastructure` is false: the operator must
    act)."""
    _not_root()

    class Scratching(Dispatcher):
        def ensure_role_scratch(self, role_):
            dispatchers._role_scratch_dirs(arenas_of_the_run, (role_,))

    dispatcher = Scratching(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, dispatcher)
    arenas_of_the_run = store.arenas_dir
    directory = arenas_of_the_run / f".hoh-scratch-role-{role.value}"
    directory.mkdir(mode=0o700)
    with _mode(directory, 0o500):
        with pytest.raises(DispatchError, match=role.value) as raised:
            ctrl.run_iteration(state)
    assert dispatcher.calls == called, "no role was called for the refused one"
    assert state.usage.dispatches == len(called), "nothing was charged for a refusal"
    assert not failures.is_infrastructure(failures.classify(raised.value)), "no automatic wait or retry"


# --- TMPDIR length: the residual, stated and measured --------------------------------

_CHILD = textwrap.dedent('''
    import multiprocessing as mp
    import sys
    import tempfile


    def plus_one(x):
        return x + 1


    if __name__ == "__main__":
        print("TMPDIR_LEN", len(tempfile.gettempdir()))
        if sys.argv[1] == "forkserver":
            with mp.get_context("forkserver").Pool(1) as pool:
                assert pool.map(plus_one, [1]) == [2]
        else:
            with mp.Manager() as manager:
                shared = manager.dict()
                shared["k"] = 1
                assert shared["k"] == 1
        print("SOCKET_OK")
''')


def _short_root():
    """A short, writable, self-removing directory, or a skip with the reason. The tests below
    need an absolute path of about 20 characters, which `tmp_path` never is."""
    if not sys.platform.startswith("linux"):
        pytest.skip("AF_UNIX path limits measured on Linux only")
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK | os.X_OK)):
        pytest.skip("/dev/shm is not a writable directory here: no short absolute path")
    return tempfile.TemporaryDirectory(dir=shm, prefix="hoh-")


def _role_tmpdir_of_length(root: Path, length: int | None) -> Path:
    """The QA role directory `_role_scratch_dirs` makes, below an arena root padded so the
    role directory is exactly `length` characters (None: no padding)."""
    arenas = root / "_arenas" / "run"
    if length is not None:
        tail = len("/run/.hoh-scratch-role-qa")
        pad = length - tail - len(str(root))
        assert pad > 2, (length, str(root))
        arenas = root / ("p" * (pad - 1)) / "run"
    made = dispatchers._role_scratch_dirs(arenas, (Role.QA,))[Role.QA]
    if length is not None:
        assert len(str(made)) == length
    return made


def _socket_child(tmpdir: Path, script_dir: Path, how: str):
    script = script_dir / "socket_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmpdir)}
    return subprocess.run([sys.executable, str(script), how], env=env, cwd=script_dir,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("how", ["forkserver", "manager"])
def test_a_role_directory_that_fits_hosts_multiprocessing_sockets(tmp_path, how):
    """The control: the directory the product makes (mode 0700, ours, plain) is a good
    `TMPDIR` for a `Manager` and the `forkserver` -- the only thing that stops them on the real
    layout is its length. Linux only (skips elsewhere, and where `/dev/shm` is missing)."""
    with _short_root() as root_:
        tmpdir = _role_tmpdir_of_length(Path(root_), None)
        assert len(str(tmpdir)) + SOCKET_SUFFIX <= SUN_PATH_USABLE, str(tmpdir)
        done = _socket_child(tmpdir, tmp_path, how)
    assert done.returncode == 0 and "SOCKET_OK" in done.stdout, done.stdout + done.stderr


def _states_failure_as_certain(text: str) -> bool:
    """Whether `text` says the socket users *fail* here, without a modal ("may", "can",
    "might"): the one claim that is true on some interpreters and false on others. Case and
    line wrapping do not matter."""
    flat = " ".join(text.split())
    return re.search(r"(?<!may )(?<!can )(?<!might )fails? here with", flat, re.IGNORECASE) is not None


def _prompt_overclaims(text: str, outcomes: "dict[str, str]") -> bool:
    """The prompt states a failure as certain, and a child on this interpreter started anyway."""
    return _states_failure_as_certain(text) and any(o == "starts" for o in outcomes.values())


def _outcome(done: "subprocess.CompletedProcess[str]") -> str:
    """What one run of `_CHILD` came to. Exactly two ends are known: it starts the socket user,
    or it fails with the words the prompt quotes. Any other end is a different story than the
    one the prompt tells and fails the test."""
    output = done.stdout + done.stderr
    if done.returncode == 0:
        assert "SOCKET_OK" in done.stdout, output
        return "starts"
    assert "AF_UNIX path too long" in done.stderr and "SOCKET_OK" not in done.stdout, output
    return "fails with AF_UNIX path too long"


def _done(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(["child"], returncode, stdout, stderr)


def test_the_outcome_of_a_child_is_one_of_the_two_the_prompt_allows():
    """The classifier itself, on made-up results: it does not wave through a child that died of
    something else, or one that exited 0 without reaching the end of the script."""
    assert _outcome(_done(0, "TMPDIR_LEN 20\nSOCKET_OK\n")) == "starts"
    assert _outcome(_done(1, "TMPDIR_LEN 117\n", "OSError: AF_UNIX path too long\n")) == (
        "fails with AF_UNIX path too long")
    for odd in (_done(0, "TMPDIR_LEN 20\n"), _done(1, "", "ModuleNotFoundError: x\n"),
                _done(1, "SOCKET_OK\n", "AF_UNIX path too long\n"), _done(-9)):
        with pytest.raises(AssertionError):
            _outcome(odd)


def test_a_failure_stated_as_certain_is_told_from_one_stated_as_possible():
    """The detector, on the sentence the prompt had and the one it has."""
    assert _states_failure_as_certain(CERTAIN_FAILURE_SENTENCE)
    assert _states_failure_as_certain("they\nfail   here with `AF_UNIX path too long`")
    assert not _states_failure_as_certain(BULLET)
    assert not _states_failure_as_certain("they may\nfail here with `AF_UNIX path too long`")
    assert _states_failure_as_certain("Both fail   here\nwith `AF_UNIX path too long`")
    assert _states_failure_as_certain("The forkserver fails here with `AF_UNIX path too long`")
    assert not _states_failure_as_certain("they can fail here with `AF_UNIX path too long`")
    assert _states_failure_as_certain("BOTH FAILS HERE WITH `AF_UNIX path too long`")
    assert not _states_failure_as_certain("they might fail here with `AF_UNIX path too long`")
    assert not _states_failure_as_certain("nothing about sockets")
    starts, fails = "starts", "fails with AF_UNIX path too long"
    # the prompt of the base on the public CI's interpreter (the forkserver starts) is caught;
    # the same prompt where both fail, and the possibility-prompt anywhere, are not
    assert _prompt_overclaims(CERTAIN_FAILURE_SENTENCE, {"forkserver": starts, "manager": fails})
    assert not _prompt_overclaims(CERTAIN_FAILURE_SENTENCE, {"forkserver": fails, "manager": fails})
    assert not _prompt_overclaims(BULLET, {"forkserver": starts, "manager": starts})


def test_the_prompt_states_the_socket_failure_as_a_possibility_it_names_the_reason_for():
    """What the prompt tells a role is true on every supported interpreter: the failure is
    stated as possible and as depending on the interpreter and the platform, and a role is told to report it
    only if it happens. (Whether it happens is measured on the running interpreter below.)"""
    for role, text in _prompts().items():
        flat = " ".join(text.split())
        assert not _states_failure_as_certain(text), f"{role}: a failure stated as certain"
        assert "may fail here with `AF_UNIX path too long`, depending on the interpreter and the platform" in flat, role
        assert "If they do, that is the layout, not the code under test" in flat, role


def test_the_controller_hands_each_role_the_possibility_sentence_and_no_certainty(tmp_path, repo, spec):   # noqa: F811
    """The path production takes (`cmd_run` -> `run_iteration` -> `roles.<role>_prompt`): the
    three prompts a real controller dispatches carry the bullet once, as possible, never as
    certain -- not only the prompt functions called by hand."""
    d = Dispatcher(repo=repo)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)
    assert set(d.prompts) == set(Role), "all three roles were dispatched"
    for role, prompt in d.prompts.items():
        assert prompt.count(BULLET) == 1, role
        assert not _states_failure_as_certain(prompt), role


@pytest.mark.parametrize("length", [REAL_LAYOUT_SHORTEST_TMPDIR, 130], ids=["shortest", "longest"])
def test_the_residual_the_prompt_states_is_what_a_child_process_meets(tmp_path, length):
    """The claim of the role prompt, run: on a role directory as long as the real layout's
    (117 and 130 measured), each of the `forkserver` and a `Manager` either starts or fails with
    the very words the prompt quotes -- nothing else -- and the prompt claims neither more nor
    less than that. Which one it is depends on the interpreter: 3.11, 3.12 and 3.13.5 fail
    (measured), the `forkserver` of 3.13.16 (the public CI) starts. The prompt therefore says
    "may fail"; if the prompt ever states the failure as certain again, this test goes red on
    an interpreter that starts, and the sentence is to be changed in `roles.py`, not this test
    silenced."""
    prompt_claims = ("AF_UNIX path too long", "107 bytes", "over 100 characters")
    flat = " ".join(roles._HOUSE_RULES_POINTER.split())      # the bullet wraps lines
    assert all(claim in " ".join(BULLET.split()) for claim in prompt_claims)
    assert all(claim in flat for claim in prompt_claims), (
        "the prompt says what this test measures")
    assert REAL_LAYOUT_SHORTEST_TMPDIR > 100
    assert REAL_LAYOUT_SHORTEST_TMPDIR + SOCKET_SUFFIX > SUN_PATH_USABLE
    with _short_root() as root_:
        tmpdir = _role_tmpdir_of_length(Path(root_), length)
        outcomes = {how: _outcome(_socket_child(tmpdir, tmp_path, how))
                    for how in ("forkserver", "manager")}
    assert not _prompt_overclaims(roles._HOUSE_RULES_POINTER, outcomes), (
        f"the prompt states a failure as certain, and this interpreter ({sys.version.split()[0]}) "
        f"met: {outcomes}")


# --- the prompt ---------------------------------------------------------------------


def _prompts() -> dict[str, str]:
    from hoh.contracts import AcceptanceCheck, DevelopmentPlan
    from hoh.evidence import EvidenceBundle

    plan = DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="c", spec_digest="d",
        objective="g", targets=["t"],
        acceptance_checks=[AcceptanceCheck(check_id="K1", description="d",
                                           command="true", expect_exit=0)])
    return {
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


def test_every_role_prompt_carries_the_basetemp_bullet_of_the_spec_word_for_word():
    """The review found the spec's "verbatim" bullet and the shipped one apart (rbtint-b
    MINOR 1); the criterion that was meant to hold them together checked fragments. This one
    holds the whole text, once, in all three prompts."""
    for role, text in _prompts().items():
        assert text.count(BULLET) == 1, f"{role}: the bullet is not in the prompt exactly once"


def test_the_bullet_names_the_prefix_the_product_gives_the_directories():
    """A rename of `ROLE_SCRATCH_PREFIX` must take the prompt with it: a role is told to stop
    unless its `TMPDIR` carries this name, which is how a pane restored from before the
    setting (`TMPDIR=/tmp`, no role directory) is noticed by the role."""
    assert dispatchers.ROLE_SCRATCH_PREFIX in BULLET
    assert dispatchers.ROLE_SCRATCH_PREFIX in roles._HOUSE_RULES_POINTER
    assert 'test -w "$TMPDIR"' in roles._HOUSE_RULES_POINTER


# --- the restored-pane comment -------------------------------------------------------


def test_the_restored_pane_gap_is_named_for_tmpdir_where_the_pane_is_adopted():
    """rbtint-b MINOR 2: the comment at the adoption of a restored pane named O215 and the
    bytecode and not `TMPDIR`. A documentation pin: the comment sits between the two
    statements that bound the branch."""
    import inspect

    source = inspect.getsource(dispatchers.HerdrDispatcher._ensure_agent)
    start = source.index("restored = self._reusable_agent(name, state)")
    end = source.index("self.agents[role] = name", start)
    branch = source[start:end]
    assert "TMPDIR" in branch and "role_scratch_env" in branch, branch
    assert "/tmp/pytest-of-<user>" in branch, "and what follows from it"
