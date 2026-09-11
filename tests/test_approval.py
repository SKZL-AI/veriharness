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
