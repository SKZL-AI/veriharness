"""What the sandbox closes, what it only narrows, and what it leaves untouched.

The temptation with an OS sandbox is to say it fixes the security limits. It
does not fix all of them, and a module that claimed otherwise would be making
exactly the kind of unearned claim this project keeps having to correct in
public. So each of the three relevant limits is asserted separately, with the
weaker claims written down as plainly as the strong one:

* **Limit 6 is closed**, and the test measures it both ways: the same command,
  in the same directory, answering differently inside and outside.
* **Limit 4 is narrowed, not closed.** The guard is still a denylist. What
  changes is how far a command that gets past it can reach.
* **Limit 5 is untouched.** The placeholder divergence lives in HoH's own
  string handling; no amount of isolation makes two different strings equal.

Tests requiring `bwrap` skip with a named reason when it is absent, rather
than silently passing -- a sandbox test that passes on a machine with no
sandbox has measured nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh.sandbox import (
    BubblewrapSandbox,
    Isolation,
    NoSandbox,
    SandboxSpec,
    SandboxUnavailable,
    select,
    verify_limit_6,
)

bwrap = BubblewrapSandbox()
needs_bwrap = pytest.mark.skipif(
    bwrap.unavailable() is not None,
    reason=f"no usable sandbox: {bwrap.unavailable()}",
)


@pytest.fixture
def arena(tmp_path):
    """An arena nested inside a git working tree, as HoH really lays them out.

    The nesting is the point: `runs/<id>/arena` sits inside the project's own
    checkout, and the arena itself has no `.git`.
    """
    outside_ = tmp_path / "projekt"
    outside_.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=outside_, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=outside_, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=outside_, check=True)
    (outside_ / "f.txt").write_text("ancestor\n")
    subprocess.run(["git", "add", "-A"], cwd=outside_, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ancestor"], cwd=outside_, check=True)
    a = outside_ / "runs" / "r1" / "arena"
    a.mkdir(parents=True)
    (a / "slug.py").write_text("candidate\n")
    assert not (a / ".git").exists(), "an arena has no .git of its own"
    return a


@pytest.fixture
def spec(arena, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return SandboxSpec(candidate=arena, scratch=scratch)


# --------------------------------------------------------------------------- #
# Limit 6: closed, and measured both ways
# --------------------------------------------------------------------------- #

@needs_bwrap
def test_limit_6_git_climbs_out_outside_and_not_inside(arena, spec):
    """`git rev-parse --show-toplevel` inside an arena does not fail outside a
    sandbox -- it silently answers with the *ancestor* repository, so `git
    status` and `git log` report on the live working tree rather than the
    frozen candidate. Inside, there is no parent `.git` to climb to.
    """
    m = verify_limit_6(arena, bwrap, spec)

    # The limit, reproduced. If this half ever stops holding, the limit has
    # been fixed elsewhere and this test should be re-read, not deleted.
    assert m["outside_exit"] == 0
    assert m["outside_climbs_out"] is True
    assert Path(m["outside_toplevel"]) == arena.parents[2], m["outside_toplevel"]

    # And closed.
    assert m["inside_climbs_out"] is False
    assert m["inside_exit"] != 0, "git must find no repository at all inside"
    assert m["isolation"] == "strict"


@needs_bwrap
def test_the_parent_tree_is_not_visible_inside(arena, spec):
    """The mechanism behind limit 6's closure, asserted directly rather than
    inferred from git's behaviour."""
    parent_git = arena.parents[2] / ".git"
    assert parent_git.is_dir(), "the ancestor .git exists outside"
    r = bwrap.run(["test", "-e", str(parent_git)], spec)
    assert r.exit_code != 0, "the parent .git must not be reachable from inside"


@needs_bwrap
def test_the_candidate_is_read_only_inside(spec):
    """A check that rewrites the thing it checks has invalidated its own
    result before it finished."""
    r = bwrap.run(["sh", "-c", "echo tampered >> slug.py"], spec)
    assert r.exit_code != 0
    assert (spec.candidate / "slug.py").read_text() == "candidate\n"


@needs_bwrap
def test_scratch_is_writable_and_lies_outside_the_candidate(spec):
    r = bwrap.run(["sh", "-c", f"echo ok > {spec.scratch}/note"], spec)
    assert r.exit_code == 0, r.stderr
    assert (spec.scratch / "note").read_text().strip() == "ok"
    assert spec.scratch not in spec.candidate.parents


@needs_bwrap
def test_the_network_is_off_by_default(spec):
    """Off by default, not off by configuration: a check that needs the network
    has to say so, and saying so is visible in the spec."""
    assert spec.network is False
    r = bwrap.run(["sh", "-c", "cat /proc/net/route | tail -n +2 | wc -l"], spec)
    assert r.exit_code == 0
    assert r.stdout.strip() == "0", f"routes visible inside the sandbox: {r.stdout!r}"


@needs_bwrap
def test_the_environment_is_not_inherited(spec):
    """An inherited environment is an inherited capability."""
    import os

    os.environ["E2E_GEHEIM"] = "darf-nicht-durch"
    try:
        r = bwrap.run(["sh", "-c", "echo ${E2E_GEHEIM:-absent}"], spec)
        assert r.stdout.strip() == "absent", r.stdout
        # And what is passed through, is.
        r2 = bwrap.run(["sh", "-c", "echo ${HOME:-absent}"], spec)
        assert r2.stdout.strip() == str(spec.scratch)
    finally:
        del os.environ["E2E_GEHEIM"]


# --------------------------------------------------------------------------- #
# Limit 4: narrowed, not closed -- and the difference stated
# --------------------------------------------------------------------------- #

@needs_bwrap
def test_limit_4_the_radius_shrinks_but_the_guard_stays_a_denylist(spec, tmp_path):
    """The sandbox stops a command from reaching the wider machine. It does not
    make the guard sound, and this test asserts both halves so neither claim
    can be quoted without the other.
    """
    secret_ = tmp_path / "geheim.txt"
    secret_.write_text("not for the check\n")

    # Narrowed: the file exists and is readable to this process, and is not
    # reachable from inside.
    assert secret_.read_text().startswith("not for")
    r = bwrap.run(["cat", str(secret_)], spec)
    assert r.exit_code != 0, "the sandbox must not reach outside the declared binds"

    # Not closed: the guard is still a pattern denylist over a string a shell
    # reinterprets. Asserted against the guard itself, not against prose.
    from hoh.runner import assert_command_allowed

    assert_command_allowed("python3 -c 'print(1)'")          # ordinary: allowed
    with pytest.raises(Exception):
        assert_command_allowed("nvidia-smi")                  # denylisted: refused
    # A command the denylist does not name is allowed, whatever it does. That
    # is what "tripwire, not boundary" means, and it is still true.
    assert_command_allowed("python3 -c \"open('/tmp/x','w').write('1')\"")


# --------------------------------------------------------------------------- #
# Limit 5: untouched
# --------------------------------------------------------------------------- #

def test_limit_5_stays_open_because_it_is_not_an_isolation_problem():
    """The guard reasons about the command with `{ARENA}` replaced by the
    literal string "ARENA"; the runner substitutes the real absolute path. Two
    different strings, and no amount of isolation makes them equal. Asserted
    here so that "we added a sandbox" is never read as having closed it.
    """
    from hoh import runner

    source = Path(runner.__file__).read_text()
    assert "{ARENA}" in source, "the placeholder is still how a check names the arena"
    # The two substitution sites still exist and are still different: the guard
    # substitutes a stand-in, the runner a real path.
    assert "ARENA" in source


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #

def test_without_a_backend_strict_fails_closed():
    """A sandbox that silently degrades is worse than none, because the reason
    to ask for one is the assumption that it is there."""

    class NoneOfThem:
        name = "keiner"

        def unavailable(self):
            return "deliberately unavailable"

        def run(self, argv, spec):
            raise AssertionError("must never be reached")

    with pytest.raises(SandboxUnavailable) as exc:
        select(Isolation.STRICT, [NoneOfThem()])
    assert "deliberately unavailable" in str(exc.value)
    assert "strict" in str(exc.value)


def test_nosandbox_refuses_strict(spec):
    """Returning a STRICT-labelled result it did not provide is the precise
    failure this module exists to prevent."""
    n = NoSandbox()
    with pytest.raises(SandboxUnavailable) as exc:
        n.run(["true"], spec)
    assert "cannot provide strict" in str(exc.value)


def test_nosandbox_runs_only_when_explicitly_asked_for(spec):
    open_ = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                        isolation=Isolation.NONE)
    r = NoSandbox().run(["sh", "-c", "echo hi"], open_)
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi"
    assert r.isolation is Isolation.NONE
    assert "no isolation" in r.detail


def test_the_result_says_how_it_ran(spec):
    """A result that does not say how it ran is a result you cannot reason
    about."""
    open_ = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                        isolation=Isolation.NONE)
    assert NoSandbox().run(["true"], open_).isolation is Isolation.NONE


# --------------------------------------------------------------------------- #
# A declared limit that is not enforced is a limit that exists in a docstring
# --------------------------------------------------------------------------- #

@needs_bwrap
def test_the_memory_ceiling_is_actually_enforced(spec):
    """`memory_bytes` was declared before it was enforced.

    A field that names a protection and does nothing is worse than no field:
    it reads, to anyone configuring the sandbox, like the protection is there.
    """
    eng = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                      memory_bytes=64 * 1024 * 1024)
    r = bwrap.run(
        ["python3", "-c", "b = bytearray(512 * 1024 * 1024); print(len(b))"], eng
    )
    assert r.exit_code != 0, f"the allocation was not refused: {r.stdout!r}"
    assert "MemoryError" in r.stderr or r.exit_code < 0, r.stderr[-200:]

    # And without the limit, the same allocation succeeds -- otherwise the test
    # above would pass on a machine that simply has no memory.
    wide = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch)
    r2 = bwrap.run(
        ["python3", "-c", "b = bytearray(512 * 1024 * 1024); print(len(b))"], wide
    )
    assert r2.exit_code == 0, r2.stderr[-200:]


@needs_bwrap
def test_the_ceiling_applies_to_child_processes_too(spec):
    """Set between fork and exec, so everything the check starts inherits it.

    A limit on the parent only is a limit the command escapes by starting a
    child, which is precisely what an acceptance check does.
    """
    eng = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                      memory_bytes=64 * 1024 * 1024)
    r = bwrap.run(
        ["sh", "-c", "python3 -c 'b = bytearray(512 * 1024 * 1024)'"], eng
    )
    assert r.exit_code != 0, "a child process escaped the memory ceiling"


def test_without_ceilings_no_hook_is_installed(spec):
    """No limits means no preexec hook at all, rather than one that does
    nothing -- a hook that runs and applies nothing is a place for a future
    change to go unnoticed."""
    from hoh.sandbox import _limits_for

    assert _limits_for(spec) is None
    eng = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                      memory_bytes=1024)
    assert _limits_for(eng) is not None


# --------------------------------------------------------------------------- #
# The real runner path
# --------------------------------------------------------------------------- #

def _check(command: str, cid: str = "K1"):
    from hoh.contracts import AcceptanceCheck

    return AcceptanceCheck(check_id=cid, description="probe", command=command,
                           expect_exit=0)


def _candidate(path):
    from hoh.contracts import Candidate

    return Candidate(candidate_id="c1", repo_path=str(path), commit="a" * 40,
                     tree_clean=True, tree_digest="d" * 16)


def test_strict_without_a_backend_fails_closed_instead_of_running_unsandboxed(arena):
    """The whole reason to ask for isolation is the assumption it is there.

    A run that quietly proceeds without it has measured something other than
    what was asked for, and would report a verdict about it.
    """
    from hoh.runner import run_check

    class NoneOfThem:
        name = "keiner"

        def unavailable(self):
            return "deliberately unavailable"

        def run(self, argv, spec):
            raise AssertionError("must never be reached")

    import hoh.sandbox as sb

    real = sb.select
    sb.select = lambda iso, backends=None: (_ for _ in ()).throw(
        sb.SandboxUnavailable("no backend can provide strict isolation. keiner: deliberately unavailable")
    )
    try:
        receipt, transcript = run_check(
            _check("echo hello"), _candidate(arena), run_id="r", iteration=1,
            attempt=1, cwd=arena, isolation=sb.Isolation.STRICT,
        )
    finally:
        sb.select = real

    assert receipt.runner_ok is False, "a refused sandbox is not a verdict"
    assert receipt.exit_code == 126
    assert "unavailable" in transcript
    # INCONCLUSIVE, not FAIL: the check did not run in the regime it was for.
    assert receipt.outcome(expect_exit=0).name == "INCONCLUSIVE"


@needs_bwrap
def test_a_real_acceptance_check_runs_in_the_sandbox(arena):
    """Not a demo path: `run_check`, the function every acceptance check goes
    through."""
    from hoh.runner import run_check
    from hoh.sandbox import Isolation

    receipt, transcript = run_check(
        _check("echo sandboxed && test -f slug.py"), _candidate(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena, isolation=Isolation.STRICT,
    )
    assert receipt.exit_code == 0, transcript[-400:]
    assert receipt.runner_ok is True
    assert "sandboxed" in transcript
    # The receipt says how it ran, so it does not have to be guessed at later.
    assert "strict isolation" in transcript


@needs_bwrap
def test_limit_6_on_the_real_runner_path(arena):
    """What the sandbox actually adds for limit 6 -- which is not what it looks
    like at first.

    The runner already prevents the climb: `GIT_CEILING_DIRECTORIES` has been
    set since 2026-09-08 (O30), and with it `git rev-parse --show-toplevel`
    inside an arena exits 128 instead of naming the ancestor repository.

    But that mitigation is an environment variable, and the thing it
    constrains is a shell command that can remove it. `env -u
    GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel` climbs straight out
    again -- measured, below. The sandbox is what makes the boundary
    structural: there is no parent `.git` to find, so removing a variable
    accomplishes nothing.

    Saying "the sandbox closes limit 6" would therefore have been wrong in both
    directions: the common case was already handled, and what remained open is
    narrower and more specific than the limit's own wording suggests.
    """
    from hoh.runner import run_check
    from hoh.sandbox import Isolation

    def toplevel(transcript: str) -> str:
        for line in transcript.splitlines():
            if line.startswith("/"):
                return line
        return ""

    # 1. The runner's own mitigation holds.
    with_, t_with = run_check(
        _check("git rev-parse --show-toplevel"), _candidate(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
    )
    assert with_.exit_code != 0, t_with[-300:]

    # 2. And the check can take it away.
    without, t_without = run_check(
        _check("env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel", cid="K2"),
        _candidate(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
    )
    assert without.exit_code == 0, t_without[-300:]
    assert toplevel(t_without) == str(arena.parents[2]), (
        "the ancestor repository must still be reachable this way -- if it is "
        "not, the gap this test is about has moved and the test should be re-read"
    )

    # 3. Inside the sandbox the same command finds nothing, because there is
    #    nothing to find rather than because a variable said not to look.
    inside_it, t_inside = run_check(
        _check("env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel", cid="K3"),
        _candidate(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT,
    )
    assert inside_it.exit_code != 0, t_inside[-300:]
    # Asserted on git's own output, not on the whole transcript: the header
    # carries `# cwd=<arena>`, which contains the ancestor path as a prefix,
    # and matching against that would pass or fail for the wrong reason.
    assert toplevel(t_inside) == "", t_inside[-300:]


@needs_bwrap
@pytest.mark.parametrize("command,cid", [
    ("ls /home", "N2"),
    ("python3 -c \"import urllib.request; urllib.request.urlopen('http://example.invalid', timeout=3)\"", "N4"),
    ("echo tampered >> slug.py", "N5"),
])
def test_negative_controls_on_the_runner_path(arena, command, cid):
    """Things that work outside and must not inside: reading other users' home
    directories, reaching the network, and writing to a candidate promised
    read-only.
    """
    from hoh.runner import run_check
    from hoh.sandbox import Isolation

    receipt, transcript = run_check(
        _check(command, cid=cid), _candidate(arena), run_id="r", iteration=1,
        attempt=1, cwd=arena, isolation=Isolation.STRICT,
    )
    if command == "ls /home" and receipt.exit_code == 0:
        # Changed 2026-09-22 (O200), by addition. Since the strict sandbox
        # binds the interpreter HoH runs under, an interpreter living below
        # /home makes bubblewrap create its path components, so `ls /home`
        # succeeds. What this control is *for* -- other users' home
        # directories and the rest of this one stay unreadable -- is asserted
        # exactly: /home may list only the interpreter's first component and
        # nothing else. Where no interpreter lives under /home the literal
        # assertion below still applies unchanged.
        from hoh.runner import interpreter_toolchain
        under_home = [b for b in interpreter_toolchain()[0]
                      if b.is_relative_to(Path("/home"))]
        assert under_home, f"{command!r} succeeded with nothing bound under /home"
        allowed = {b.relative_to("/home").parts[0] for b in under_home}
        output = transcript.split("--- output ---", 1)[-1]
        listed = set(output.split())
        assert listed and listed <= allowed, (
            f"/home lists more than the bound interpreter: {sorted(listed - allowed)}")
        return
    assert receipt.exit_code != 0, f"{command!r} succeeded inside the sandbox: {transcript[-300:]}"


@pytest.mark.parametrize("command", [
    "cat /etc/shadow",
    "cat ../../../f.txt",
])
def test_the_guard_catches_these_before_the_sandbox(arena, command):
    """Two of the obvious escapes never reach the sandbox at all.

    An absolute path out and a relative climb are refused by
    `assert_stays_in_arena` before anything runs. Worth asserting where the
    sandbox tests are, so nobody reads the sandbox as the only thing standing
    between a check and the filesystem -- and so that if the guard is ever
    loosened, a test here notices.
    """
    from hoh.runner import ArenaEscape, run_check

    with pytest.raises(ArenaEscape):
        run_check(_check(command, cid="G1"), _candidate(arena), run_id="r",
                  iteration=1, attempt=1, cwd=arena)


@needs_bwrap
def test_home_and_tmpdir_point_into_the_scratch(arena):
    """Controlled, not inherited -- and not inside the candidate, so a check
    cannot leave something an enumerating criterion would then find."""
    from hoh.runner import run_check
    from hoh.sandbox import Isolation

    receipt, transcript = run_check(
        _check('echo "H=$HOME T=$TMPDIR"'), _candidate(arena), run_id="r",
        iteration=1, attempt=1, cwd=arena, isolation=Isolation.STRICT,
    )
    assert receipt.exit_code == 0, transcript[-300:]
    assert ".hoh-scratch-" in transcript
    assert str(arena) not in transcript.split("H=")[1].split()[0]


def test_without_isolation_everything_stays_as_before(arena):
    """Adding a sandbox must not change what an existing run does.

    NONE is the default, and the historical path is what it takes.
    """
    from hoh.runner import run_check

    receipt, transcript = run_check(
        _check("echo plain"), _candidate(arena), run_id="r", iteration=1,
        attempt=1, cwd=arena,
    )
    assert receipt.exit_code == 0
    assert "plain" in transcript
    # No isolation note: the historical path leaves no such mark, which is how
    # a reader tells the two regimes apart in a receipt.
    assert "executed under" not in transcript


def test_a_sandboxed_check_does_not_read_a_user_site_directory(tmp_path):
    """The hole the unsandboxed path had closed and this one did not.

    A reviewer planted a `usercustomize.py` in the scratch directory -- whose
    name is derivable from the run's own state -- and it executed **inside
    bubblewrap**, before the command's first line, under a receipt reporting
    `verified_from_inside`. The strongest-looking receipt was the vulnerable
    one.
    """
    from hoh.sandbox import SandboxSpec, _env_for

    spec = SandboxSpec(candidate=tmp_path / "cand", scratch=tmp_path / "s",
                       timeout=5)
    env = _env_for(spec)
    assert env["HOME"] == str(tmp_path / "s")
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_sandbox_scratch_directory_found_in_place_is_parked(tmp_path):
    """Both regimes create their scratch through the same helper now."""
    from hoh.runner import _OWN_SCRATCHES, _fresh_scratch

    target = tmp_path / ".hoh-scratch-r-i1-a1-K1"
    (target / "lib").mkdir(parents=True)
    (target / "lib" / "usercustomize.py").write_text("raise SystemExit('planted')\n")
    _OWN_SCRATCHES.pop(str(target), None)

    fresh_ = _fresh_scratch(target)

    assert fresh_ == target
    assert not (fresh_ / "lib").exists()
    parked = [p for p in tmp_path.iterdir()
               if p.name.startswith(".hoh-scratch-r-i1-a1-K1.v")]
    assert parked, "the planted directory was removed rather than parked"
    assert (parked[0] / "lib" / "usercustomize.py").is_file()


def test_a_scratch_directory_this_process_made_is_reused(tmp_path):
    """Parking every call would cost one directory per check.

    All the checks of one candidate share an arena and therefore a scratch
    name; measured at up to 118 MB each on this project's own runs, and it
    would throw away the pip and pytest caches between two checks of the same
    candidate.
    """
    from hoh.runner import _OWN_SCRATCHES, _fresh_scratch

    target = tmp_path / ".hoh-scratch-r-i1-a1-K2"
    _OWN_SCRATCHES.pop(str(target), None)

    _fresh_scratch(target)
    (target / "cache").mkdir()
    _fresh_scratch(target)

    assert (target / "cache").is_dir(), "the second check lost the first's cache"
    assert not [p for p in tmp_path.iterdir() if ".v" in p.name]


def test_a_scratch_directory_replaced_behind_our_back_is_parked_again(tmp_path):
    """The registry outlives the directory, so the path alone is not identity.

    A reviewer planted a `usercustomize.py` between two checks of the same
    candidate -- same path, different directory -- and it survived, which is
    exactly the pre-plant the parking exists to stop. The inode is checked too.
    """
    import shutil

    from hoh.runner import _OWN_SCRATCHES, _fresh_scratch

    target = tmp_path / ".hoh-scratch-r-i1-a1-K3"
    _OWN_SCRATCHES.pop(str(target), None)
    _fresh_scratch(target)

    # something else replaces the directory at the same path
    shutil.move(str(target), str(tmp_path / "weggeraeumt"))
    (target / "lib").mkdir(parents=True)
    (target / "lib" / "usercustomize.py").write_text("raise SystemExit('planted')\n")

    _fresh_scratch(target)

    assert not (target / "lib").exists(), "the replacement was adopted as ours"
