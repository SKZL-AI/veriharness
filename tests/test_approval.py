"""Who may answer a trust prompt, and for what.

HoH refuses to trust a directory it was merely pointed at, and that refusal is
load-bearing: trusting on request would let anything that can name a path
obtain an agent's access to it. The cost is that a fresh worktree stops at a
trust dialog, which is where an otherwise unattended loop stops.

These tests pin the three properties that make a way through acceptable rather
than a hole: absent by default, scoped to named worktrees, and explicit about
what it answered.
"""

from __future__ import annotations

from pathlib import Path

import pytest


from hoh.approval import Approval, NoApprovalProvider, ScopedScriptProvider


def test_ohne_provider_wird_nichts_freigegeben(tmp_path):
    """Adding this module changes nothing for a deployment that configures no
    authority: the run blocks and a person answers, exactly as before."""
    p = NoApprovalProvider()
    assert p.may_approve(tmp_path) is False
    a = p.approve(tmp_path, tmp_path)
    assert a.granted is False
    assert "no approval authority is configured" in a.detail
    assert str(tmp_path) in a.as_reason()


def test_ein_provider_gibt_nur_seinen_eigenen_worktree_frei(tmp_path):
    """The scope is the point.

    A helper that can trust any path turns "HoH will not trust arbitrary
    directories" back into "HoH will trust arbitrary directories, via this".
    """
    erlaubt = tmp_path / "meins"
    erlaubt.mkdir()
    fremd = tmp_path / "fremd"
    fremd.mkdir()
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)

    p = ScopedScriptProvider(script=skript, allowed=(erlaubt,))
    assert p.may_approve(erlaubt) is True
    assert p.may_approve(fremd) is False

    a = p.approve(fremd, tmp_path)
    assert a.granted is False
    assert "not in this provider's scope" in a.detail
    assert p.granted == [], "a refused worktree must not be recorded as granted"

    b = p.approve(erlaubt, tmp_path)
    assert b.granted is True
    assert p.granted == [erlaubt]


def test_der_pfad_wird_aufgeloest_bevor_er_verglichen_wird(tmp_path):
    """`allowed/../allowed` is the same worktree; `allowed/../other` is not.

    Comparing unresolved strings would let a caller walk out of the scope with
    a relative path, which is the one trick this check exists to stop.
    """
    erlaubt = tmp_path / "meins"
    erlaubt.mkdir()
    (tmp_path / "fremd").mkdir()
    p = ScopedScriptProvider(script=tmp_path / "x.sh", allowed=(erlaubt,))
    assert p.may_approve(tmp_path / "meins" / ".." / "meins") is True
    assert p.may_approve(tmp_path / "meins" / ".." / "fremd") is False


def test_ein_fehlendes_hilfsskript_gibt_nicht_frei(tmp_path):
    erlaubt = tmp_path / "meins"
    erlaubt.mkdir()
    p = ScopedScriptProvider(script=tmp_path / "gibtesnicht.sh", allowed=(erlaubt,))
    a = p.approve(erlaubt, tmp_path)
    assert a.granted is False
    assert "does not exist" in a.detail


def test_ein_scheiterndes_hilfsskript_gibt_nicht_frei(tmp_path):
    erlaubt = tmp_path / "meins"
    erlaubt.mkdir()
    skript = tmp_path / "nein.sh"
    skript.write_text("#!/bin/sh\necho 'user declined' >&2\nexit 3\n")
    skript.chmod(0o755)
    p = ScopedScriptProvider(script=skript, allowed=(erlaubt,))
    a = p.approve(erlaubt, tmp_path)
    assert a.granted is False
    assert "exited 3" in a.detail
    assert "user declined" in a.detail


def test_die_antwort_sagt_wofuer_sie_galt(tmp_path):
    """"Trust was granted" without saying to what is the same shape of claim
    this project keeps having to correct."""
    a = Approval(granted=True, worktree=Path("/w/t"), provider="p", detail="ok")
    assert "/w/t" in a.as_reason() and "granted" in a.as_reason()
    b = Approval(granted=False, worktree=Path("/w/t"), provider="p", detail="nope")
    assert "refused" in b.as_reason() and "nope" in b.as_reason()


def test_der_launcher_hat_ohne_konfiguration_keine_autoritaet(tmp_path):
    """No hardcoded path anywhere: a machine's layout must not become a product
    requirement."""
    from hoh.launcher import HohRunLauncher

    l = HohRunLauncher(tmp_path, tmp_path)
    assert l.approvals.name == "none"
    assert l.approvals.approve(tmp_path, tmp_path).granted is False


def test_der_launcher_haelt_jede_antwort_fest(tmp_path):
    from hoh.launcher import HohRunLauncher

    erlaubt = tmp_path / "wt"
    erlaubt.mkdir()
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)
    l = HohRunLauncher(
        tmp_path, tmp_path,
        approvals=ScopedScriptProvider(script=skript, allowed=(erlaubt,)),
    )
    assert l._approve(erlaubt).granted is True
    assert l._approve(tmp_path / "anderswo").granted is False
    assert len(l.approvals_given) == 2, "every answer goes on the record, including refusals"


def test_ein_verzeichnis_als_scope_deckt_auch_kuenftige_worktrees(tmp_path):
    """A fixed list cannot cover work that does not exist yet.

    A repair node is created by a gate failure, gets its own worktree, and
    needs it trusted -- all after any list would have been written.
    """
    from hoh.approval import PrefixScopedProvider

    wurzel = tmp_path / "worktrees" / "projekt"
    (wurzel / "hoh-a").mkdir(parents=True)
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)

    p = PrefixScopedProvider(script=skript, prefix=wurzel)
    assert p.may_approve(wurzel / "hoh-a") is True

    # The repair-node case, in the order it actually happens: the scope is
    # configured before the worktree exists, and approval is asked for after
    # Herdr has created it. A name nobody has created yet is NOT approvable --
    # `Path.resolve()` is non-strict, so such a path resolves perfectly well,
    # and granting on it would trust whatever the next writer puts there.
    kuenftig = wurzel / "hoh-repair-2-1"
    assert p.may_approve(kuenftig) is False
    kuenftig.mkdir()
    assert p.may_approve(kuenftig) is True

    # Outside the prefix, and the prefix itself.
    assert p.may_approve(tmp_path / "woanders") is False
    assert p.may_approve(wurzel) is False
    # And a relative path cannot walk out of it.
    assert p.may_approve(wurzel / "hoh-a" / ".." / ".." / "fremd") is False


def test_ein_zu_weiter_scope_wird_sofort_abgelehnt(tmp_path):
    """A provider allowed to approve anything under `/` or a home directory has
    no scope at all, and should fail when it is built rather than when it is
    used."""
    from hoh.approval import PrefixScopedProvider

    for zu_weit in (Path("/"), Path("/home"), Path.home(), Path("/tmp")):
        with pytest.raises(ValueError) as exc:
            PrefixScopedProvider(script=tmp_path / "x.sh", prefix=zu_weit)
        assert "too broad" in str(exc.value)


# --------------------------------------------------------------------------- #
# Scope checks, written as if someone were trying to get past them
# --------------------------------------------------------------------------- #


def _provider(tmp_path, **kw):
    from hoh.approval import PrefixScopedProvider

    wurzel = tmp_path / "worktrees" / "projekt"
    wurzel.mkdir(parents=True, exist_ok=True)
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)
    return PrefixScopedProvider(script=skript, prefix=wurzel, **kw), wurzel


def test_a_sibling_with_a_longer_name_is_not_inside_the_scope(tmp_path):
    """`/scope/foo` does not contain `/scope/foobar`. A `startswith` check --
    the obvious way to write this -- says it does, and that is a directory
    outside the scope getting an agent's write access."""
    from hoh.approval import PrefixScopedProvider

    innen = tmp_path / "wt" / "projekt"
    innen.mkdir(parents=True)
    geschwister = tmp_path / "wt" / "projekt-anderes"
    geschwister.mkdir()
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)

    p = PrefixScopedProvider(script=skript, prefix=innen)
    assert str(geschwister).startswith(str(innen)), "the trap this test is about"
    assert p.may_approve(geschwister) is False
    assert "not beneath" in p.approve(geschwister, tmp_path).detail


def test_a_symlink_out_of_the_scope_is_refused(tmp_path):
    """The scope is about where the directory *is*, not what it is called."""
    p, wurzel = _provider(tmp_path)
    draussen = tmp_path / "geheim"
    draussen.mkdir()
    link = wurzel / "sieht-harmlos-aus"
    link.symlink_to(draussen)

    assert p.may_approve(link) is False
    a = p.approve(link, tmp_path)
    assert a.granted is False
    assert "not beneath" in a.detail
    assert p.granted == []


def test_a_symlink_inside_the_scope_is_still_approved(tmp_path):
    """The control for the test above: refusing every symlink would be easy
    and would also refuse legitimate layouts. What is refused is escaping."""
    p, wurzel = _provider(tmp_path)
    echt = wurzel / "echt"
    echt.mkdir()
    link = wurzel / "zeigt-nach-innen"
    link.symlink_to(echt)
    assert p.may_approve(link) is True


def test_a_dotdot_escape_is_refused(tmp_path):
    p, wurzel = _provider(tmp_path)
    (wurzel / "a").mkdir()
    fremd = tmp_path / "fremd"
    fremd.mkdir()
    assert p.may_approve(wurzel / "a" / ".." / ".." / "fremd") is False


def test_a_file_is_not_a_worktree(tmp_path):
    p, wurzel = _provider(tmp_path)
    datei = wurzel / "keine-mappe"
    datei.write_text("x")
    assert p.may_approve(datei) is False
    assert "does not exist as a directory" in p.approve(datei, tmp_path).detail


def test_a_target_swapped_after_the_check_is_refused(tmp_path):
    """Time-of-check to time-of-use. The window is small; what is on the other
    side of it is an agent with write access to wherever the path now points."""
    p, wurzel = _provider(tmp_path)
    ziel = wurzel / "wt"
    ziel.mkdir()
    draussen = tmp_path / "woanders"
    draussen.mkdir()

    echte_identitaet = p._identity

    zaehler = {"n": 0}

    def wechselnd(pfad):
        # The scope check and the identity taken with it see the real
        # directory; the last look, immediately before the grant, sees
        # something else.
        zaehler["n"] += 1
        if zaehler["n"] <= 2:
            return echte_identitaet(pfad)
        return echte_identitaet(draussen)

    p._identity = wechselnd
    a = p.approve(ziel, tmp_path)
    assert a.granted is False
    assert "changed identity" in a.detail
    assert p.granted == []


def test_a_scope_that_is_only_one_level_deep_is_refused(tmp_path):
    from hoh.approval import PrefixScopedProvider

    for zu_weit in (Path("/opt"), Path("/srv"), Path("/anything")):
        with pytest.raises(ValueError, match="too broad"):
            PrefixScopedProvider(script=tmp_path / "x.sh", prefix=zu_weit)


def test_a_parent_of_the_home_directory_is_refused(tmp_path):
    from hoh.approval import PrefixScopedProvider

    eltern = Path.home().resolve().parent
    with pytest.raises(ValueError, match="too broad"):
        PrefixScopedProvider(script=tmp_path / "x.sh", prefix=eltern)


# --------------------------------------------------------------------------- #
# Whose worktree, not only where
# --------------------------------------------------------------------------- #


def _repo(pfad: Path) -> Path:
    import subprocess

    pfad.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=pfad, check=True)
    (pfad / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=pfad, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=pfad, check=True,
    )
    return pfad


def test_only_worktrees_of_this_project_are_approved(tmp_path):
    """"Somewhere under the worktree directory" is a location, not an owner.

    A deployment that names a shared worktree root would otherwise let this
    provider trust another project's worktree sitting in the same place.
    """
    import subprocess

    meins = _repo(tmp_path / "meins")
    fremd = _repo(tmp_path / "fremd")
    wurzel = tmp_path / "worktrees" / "gemeinsam"
    wurzel.mkdir(parents=True)
    subprocess.run(["git", "worktree", "add", "-q", str(wurzel / "meins-wt"), "-b", "x"],
                   cwd=meins, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(wurzel / "fremd-wt"), "-b", "y"],
                   cwd=fremd, check=True)

    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)

    from hoh.approval import PrefixScopedProvider

    p = PrefixScopedProvider(script=skript, prefix=wurzel, expected_repo=meins)
    assert p.may_approve(wurzel / "meins-wt") is True
    assert p.may_approve(wurzel / "fremd-wt") is False
    a = p.approve(wurzel / "fremd-wt", fremd)
    assert a.granted is False
    assert "not to this project" in a.detail

    # Without the binding, both are in scope -- which is the state this option
    # exists to improve on, kept visible rather than implied.
    offen = PrefixScopedProvider(script=skript, prefix=wurzel)
    assert offen.may_approve(wurzel / "fremd-wt") is True


def test_a_directory_that_is_no_worktree_at_all_is_refused_when_bound(tmp_path):
    meins = _repo(tmp_path / "meins")
    wurzel = tmp_path / "worktrees" / "projekt"
    (wurzel / "kein-repo").mkdir(parents=True)
    skript = tmp_path / "ja.sh"
    skript.write_text("#!/bin/sh\nexit 0\n")
    skript.chmod(0o755)

    from hoh.approval import PrefixScopedProvider

    p = PrefixScopedProvider(script=skript, prefix=wurzel, expected_repo=meins)
    a = p.approve(wurzel / "kein-repo", meins)
    assert a.granted is False
    assert "not a git worktree" in a.detail or "not to this project" in a.detail


def test_the_grant_records_the_resolved_path_not_the_one_it_was_asked_about(tmp_path):
    """What was trusted has to be readable afterwards without re-deriving it."""
    p, wurzel = _provider(tmp_path)
    echt = wurzel / "echt"
    echt.mkdir()
    link = wurzel / "zeigt-nach-innen"
    link.symlink_to(echt)
    a = p.approve(link, tmp_path)
    assert a.granted is True
    assert a.worktree == echt.resolve()
    assert p.granted == [echt.resolve()]
