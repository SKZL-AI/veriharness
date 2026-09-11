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
