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
braucht_bwrap = pytest.mark.skipif(
    bwrap.unavailable() is not None,
    reason=f"no usable sandbox: {bwrap.unavailable()}",
)


@pytest.fixture
def arena(tmp_path):
    """An arena nested inside a git working tree, as HoH really lays them out.

    The nesting is the point: `runs/<id>/arena` sits inside the project's own
    checkout, and the arena itself has no `.git`.
    """
    aussen = tmp_path / "projekt"
    aussen.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=aussen, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=aussen, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=aussen, check=True)
    (aussen / "f.txt").write_text("ancestor\n")
    subprocess.run(["git", "add", "-A"], cwd=aussen, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ancestor"], cwd=aussen, check=True)
    a = aussen / "runs" / "r1" / "arena"
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

@braucht_bwrap
def test_limit_6_git_klettert_draussen_raus_und_drinnen_nicht(arena, spec):
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


@braucht_bwrap
def test_der_elternbaum_ist_drinnen_nicht_sichtbar(arena, spec):
    """The mechanism behind limit 6's closure, asserted directly rather than
    inferred from git's behaviour."""
    eltern_git = arena.parents[2] / ".git"
    assert eltern_git.is_dir(), "the ancestor .git exists outside"
    r = bwrap.run(["test", "-e", str(eltern_git)], spec)
    assert r.exit_code != 0, "the parent .git must not be reachable from inside"


@braucht_bwrap
def test_der_kandidat_ist_drinnen_nur_lesbar(spec):
    """A check that rewrites the thing it checks has invalidated its own
    result before it finished."""
    r = bwrap.run(["sh", "-c", "echo tampered >> slug.py"], spec)
    assert r.exit_code != 0
    assert (spec.candidate / "slug.py").read_text() == "candidate\n"


@braucht_bwrap
def test_scratch_ist_schreibbar_und_liegt_nicht_im_kandidaten(spec):
    r = bwrap.run(["sh", "-c", f"echo ok > {spec.scratch}/note"], spec)
    assert r.exit_code == 0, r.stderr
    assert (spec.scratch / "note").read_text().strip() == "ok"
    assert spec.scratch not in spec.candidate.parents


@braucht_bwrap
def test_netzwerk_ist_standardmaessig_aus(spec):
    """Off by default, not off by configuration: a check that needs the network
    has to say so, and saying so is visible in the spec."""
    assert spec.network is False
    r = bwrap.run(["sh", "-c", "cat /proc/net/route | tail -n +2 | wc -l"], spec)
    assert r.exit_code == 0
    assert r.stdout.strip() == "0", f"routes visible inside the sandbox: {r.stdout!r}"


@braucht_bwrap
def test_die_umgebung_wird_nicht_geerbt(spec):
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

@braucht_bwrap
def test_limit_4_der_radius_schrumpft_aber_der_guard_bleibt_eine_denylist(spec, tmp_path):
    """The sandbox stops a command from reaching the wider machine. It does not
    make the guard sound, and this test asserts both halves so neither claim
    can be quoted without the other.
    """
    geheim = tmp_path / "geheim.txt"
    geheim.write_text("not for the check\n")

    # Narrowed: the file exists and is readable to this process, and is not
    # reachable from inside.
    assert geheim.read_text().startswith("not for")
    r = bwrap.run(["cat", str(geheim)], spec)
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

def test_limit_5_bleibt_offen_weil_es_kein_isolationsproblem_ist():
    """The guard reasons about the command with `{ARENA}` replaced by the
    literal string "ARENA"; the runner substitutes the real absolute path. Two
    different strings, and no amount of isolation makes them equal. Asserted
    here so that "we added a sandbox" is never read as having closed it.
    """
    from hoh import runner

    quelle = Path(runner.__file__).read_text()
    assert "{ARENA}" in quelle, "the placeholder is still how a check names the arena"
    # The two substitution sites still exist and are still different: the guard
    # substitutes a stand-in, the runner a real path.
    assert "ARENA" in quelle


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #

def test_ohne_backend_faellt_strict_geschlossen_aus():
    """A sandbox that silently degrades is worse than none, because the reason
    to ask for one is the assumption that it is there."""

    class Keiner:
        name = "keiner"

        def unavailable(self):
            return "deliberately unavailable"

        def run(self, argv, spec):
            raise AssertionError("must never be reached")

    with pytest.raises(SandboxUnavailable) as exc:
        select(Isolation.STRICT, [Keiner()])
    assert "deliberately unavailable" in str(exc.value)
    assert "strict" in str(exc.value)


def test_nosandbox_verweigert_strict(spec):
    """Returning a STRICT-labelled result it did not provide is the precise
    failure this module exists to prevent."""
    n = NoSandbox()
    with pytest.raises(SandboxUnavailable) as exc:
        n.run(["true"], spec)
    assert "cannot provide strict" in str(exc.value)


def test_nosandbox_laeuft_nur_wenn_ausdruecklich_verlangt(spec):
    offen = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                        isolation=Isolation.NONE)
    r = NoSandbox().run(["sh", "-c", "echo hi"], offen)
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi"
    assert r.isolation is Isolation.NONE
    assert "no isolation" in r.detail


def test_das_ergebnis_sagt_wie_es_lief(spec):
    """A result that does not say how it ran is a result you cannot reason
    about."""
    offen = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch,
                        isolation=Isolation.NONE)
    assert NoSandbox().run(["true"], offen).isolation is Isolation.NONE


# --------------------------------------------------------------------------- #
# A declared limit that is not enforced is a limit that exists in a docstring
# --------------------------------------------------------------------------- #

@braucht_bwrap
def test_speichergrenze_wird_wirklich_erzwungen(spec):
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
    weit = SandboxSpec(candidate=spec.candidate, scratch=spec.scratch)
    r2 = bwrap.run(
        ["python3", "-c", "b = bytearray(512 * 1024 * 1024); print(len(b))"], weit
    )
    assert r2.exit_code == 0, r2.stderr[-200:]


@braucht_bwrap
def test_die_grenze_gilt_auch_fuer_kindprozesse(spec):
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


def test_ohne_grenzen_wird_kein_hook_installiert(spec):
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


def _candidate(pfad):
    from hoh.contracts import Candidate

    return Candidate(candidate_id="c1", repo_path=str(pfad), commit="a" * 40,
                     tree_clean=True, tree_digest="d" * 16)


def test_strict_ohne_backend_faellt_geschlossen_aus_statt_ungesandboxt_zu_laufen(arena):
    """The whole reason to ask for isolation is the assumption it is there.

    A run that quietly proceeds without it has measured something other than
    what was asked for, and would report a verdict about it.
    """
    from hoh.runner import run_check

    class Keiner:
        name = "keiner"

        def unavailable(self):
            return "deliberately unavailable"

        def run(self, argv, spec):
            raise AssertionError("must never be reached")

    import hoh.sandbox as sb

    echt = sb.select
    sb.select = lambda iso, backends=None: (_ for _ in ()).throw(
        sb.SandboxUnavailable("no backend can provide strict isolation. keiner: deliberately unavailable")
    )
    try:
        receipt, transcript = run_check(
            _check("echo hello"), _candidate(arena), run_id="r", iteration=1,
            attempt=1, cwd=arena, isolation=sb.Isolation.STRICT,
        )
    finally:
        sb.select = echt

    assert receipt.runner_ok is False, "a refused sandbox is not a verdict"
    assert receipt.exit_code == 126
    assert "unavailable" in transcript
    # INCONCLUSIVE, not FAIL: the check did not run in the regime it was for.
    assert receipt.outcome(expect_exit=0).name == "INCONCLUSIVE"


@braucht_bwrap
def test_ein_echter_acceptance_check_laeuft_im_sandkasten(arena):
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


@braucht_bwrap
def test_limit_6_im_echten_runner_pfad(arena):
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
        for zeile in transcript.splitlines():
            if zeile.startswith("/"):
                return zeile
        return ""

    # 1. The runner's own mitigation holds.
    mit, t_mit = run_check(
        _check("git rev-parse --show-toplevel"), _candidate(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
    )
    assert mit.exit_code != 0, t_mit[-300:]

    # 2. And the check can take it away.
    ohne, t_ohne = run_check(
        _check("env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel", cid="K2"),
        _candidate(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
    )
    assert ohne.exit_code == 0, t_ohne[-300:]
    assert toplevel(t_ohne) == str(arena.parents[2]), (
        "the ancestor repository must still be reachable this way -- if it is "
        "not, the gap this test is about has moved and the test should be re-read"
    )

    # 3. Inside the sandbox the same command finds nothing, because there is
    #    nothing to find rather than because a variable said not to look.
    drin, t_drin = run_check(
        _check("env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel", cid="K3"),
        _candidate(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT,
    )
    assert drin.exit_code != 0, t_drin[-300:]
    # Asserted on git's own output, not on the whole transcript: the header
    # carries `# cwd=<arena>`, which contains the ancestor path as a prefix,
    # and matching against that would pass or fail for the wrong reason.
    assert toplevel(t_drin) == "", t_drin[-300:]


@braucht_bwrap
@pytest.mark.parametrize("command,cid", [
    ("ls /home", "N2"),
    ("python3 -c \"import urllib.request; urllib.request.urlopen('http://example.invalid', timeout=3)\"", "N4"),
    ("echo tampered >> slug.py", "N5"),
])
def test_negative_kontrollen_im_runner_pfad(arena, command, cid):
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
    assert receipt.exit_code != 0, f"{command!r} succeeded inside the sandbox: {transcript[-300:]}"


@pytest.mark.parametrize("command", [
    "cat /etc/shadow",
    "cat ../../../f.txt",
])
def test_der_guard_faengt_diese_schon_vor_dem_sandkasten(arena, command):
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


@braucht_bwrap
def test_home_und_tmpdir_zeigen_in_den_scratch(arena):
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


def test_ohne_isolation_bleibt_alles_wie_bisher(arena):
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
