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


def test_without_a_provider_nothing_is_approved(tmp_path):
    """Adding this module changes nothing for a deployment that configures no
    authority: the run blocks and a person answers, exactly as before."""
    p = NoApprovalProvider()
    assert p.may_approve(tmp_path) is False
    a = p.approve(tmp_path, tmp_path)
    assert a.granted is False
    assert "no approval authority is configured" in a.detail
    assert str(tmp_path) in a.as_reason()


def test_a_provider_approves_only_its_own_worktree(tmp_path):
    """The scope is the point.

    A helper that can trust any path turns "HoH will not trust arbitrary
    directories" back into "HoH will trust arbitrary directories, via this".
    """
    allowed_ = tmp_path / "meins"
    allowed_.mkdir()
    foreign = tmp_path / "fremd"
    foreign.mkdir()
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)

    p = ScopedScriptProvider(script=script_, allowed=(allowed_,))
    assert p.may_approve(allowed_) is True
    assert p.may_approve(foreign) is False

    a = p.approve(foreign, tmp_path)
    assert a.granted is False
    assert "not in this provider's scope" in a.detail
    assert p.granted == [], "a refused worktree must not be recorded as granted"

    b = p.approve(allowed_, tmp_path)
    assert b.granted is True
    assert p.granted == [allowed_]


def test_the_path_is_resolved_before_it_is_compared(tmp_path):
    """`allowed/../allowed` is the same worktree; `allowed/../other` is not.

    Comparing unresolved strings would let a caller walk out of the scope with
    a relative path, which is the one trick this check exists to stop.
    """
    allowed_ = tmp_path / "meins"
    allowed_.mkdir()
    (tmp_path / "fremd").mkdir()
    p = ScopedScriptProvider(script=tmp_path / "x.sh", allowed=(allowed_,))
    assert p.may_approve(tmp_path / "meins" / ".." / "meins") is True
    assert p.may_approve(tmp_path / "meins" / ".." / "fremd") is False


def test_a_missing_helper_script_does_not_approve(tmp_path):
    allowed_ = tmp_path / "meins"
    allowed_.mkdir()
    p = ScopedScriptProvider(script=tmp_path / "gibtesnicht.sh", allowed=(allowed_,))
    a = p.approve(allowed_, tmp_path)
    assert a.granted is False
    assert "does not exist" in a.detail


def test_a_failing_helper_script_does_not_approve(tmp_path):
    allowed_ = tmp_path / "meins"
    allowed_.mkdir()
    script_ = tmp_path / "nein.sh"
    script_.write_text("#!/bin/sh\necho 'user declined' >&2\nexit 3\n")
    script_.chmod(0o755)
    p = ScopedScriptProvider(script=script_, allowed=(allowed_,))
    a = p.approve(allowed_, tmp_path)
    assert a.granted is False
    assert "exited 3" in a.detail
    assert "user declined" in a.detail


def test_the_answer_says_what_it_was_for(tmp_path):
    """"Trust was granted" without saying to what is the same shape of claim
    this project keeps having to correct."""
    a = Approval(granted=True, worktree=Path("/w/t"), provider="p", detail="ok")
    assert "/w/t" in a.as_reason() and "granted" in a.as_reason()
    b = Approval(granted=False, worktree=Path("/w/t"), provider="p", detail="nope")
    assert "refused" in b.as_reason() and "nope" in b.as_reason()


def test_the_launcher_has_no_authority_without_configuration(tmp_path):
    """No hardcoded path anywhere: a machine's layout must not become a product
    requirement."""
    from hoh.launcher import HohRunLauncher

    l = HohRunLauncher(tmp_path, tmp_path)
    assert l.approvals.name == "none"
    assert l.approvals.approve(tmp_path, tmp_path).granted is False


def test_the_launcher_records_every_answer(tmp_path):
    from hoh.launcher import HohRunLauncher

    allowed_ = tmp_path / "wt"
    allowed_.mkdir()
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)
    l = HohRunLauncher(
        tmp_path, tmp_path,
        approvals=ScopedScriptProvider(script=script_, allowed=(allowed_,)),
    )
    assert l._approve(allowed_).granted is True
    assert l._approve(tmp_path / "anderswo").granted is False
    assert len(l.approvals_given) == 2, "every answer goes on the record, including refusals"


def test_a_directory_as_scope_covers_future_worktrees_too(tmp_path):
    """A fixed list cannot cover work that does not exist yet.

    A repair node is created by a gate failure, gets its own worktree, and
    needs it trusted -- all after any list would have been written.
    """
    from hoh.approval import PrefixScopedProvider

    root = tmp_path / "worktrees" / "projekt"
    (root / "hoh-a").mkdir(parents=True)
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)

    p = PrefixScopedProvider(script=script_, prefix=root)
    assert p.may_approve(root / "hoh-a") is True

    # The repair-node case, in the order it actually happens: the scope is
    # configured before the worktree exists, and approval is asked for after
    # Herdr has created it. A name nobody has created yet is NOT approvable --
    # `Path.resolve()` is non-strict, so such a path resolves perfectly well,
    # and granting on it would trust whatever the next writer puts there.
    future_ = root / "hoh-repair-2-1"
    assert p.may_approve(future_) is False
    future_.mkdir()
    assert p.may_approve(future_) is True

    # Outside the prefix, and the prefix itself.
    assert p.may_approve(tmp_path / "woanders") is False
    assert p.may_approve(root) is False
    # And a relative path cannot walk out of it.
    assert p.may_approve(root / "hoh-a" / ".." / ".." / "fremd") is False


def test_a_scope_that_is_too_broad_is_refused_at_once(tmp_path):
    """A provider allowed to approve anything under `/` or a home directory has
    no scope at all, and should fail when it is built rather than when it is
    used."""
    from hoh.approval import PrefixScopedProvider

    for too_broad in (Path("/"), Path("/home"), Path.home(), Path("/tmp")):
        with pytest.raises(ValueError) as exc:
            PrefixScopedProvider(script=tmp_path / "x.sh", prefix=too_broad)
        assert "too broad" in str(exc.value)


# --------------------------------------------------------------------------- #
# Scope checks, written as if someone were trying to get past them
# --------------------------------------------------------------------------- #


def _provider(tmp_path, **kw):
    from hoh.approval import PrefixScopedProvider

    root = tmp_path / "worktrees" / "projekt"
    root.mkdir(parents=True, exist_ok=True)
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)
    return PrefixScopedProvider(script=script_, prefix=root, **kw), root


def test_a_sibling_with_a_longer_name_is_not_inside_the_scope(tmp_path):
    """`/scope/foo` does not contain `/scope/foobar`. A `startswith` check --
    the obvious way to write this -- says it does, and that is a directory
    outside the scope getting an agent's write access."""
    from hoh.approval import PrefixScopedProvider

    inside_ = tmp_path / "wt" / "projekt"
    inside_.mkdir(parents=True)
    siblings = tmp_path / "wt" / "projekt-anderes"
    siblings.mkdir()
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)

    p = PrefixScopedProvider(script=script_, prefix=inside_)
    assert str(siblings).startswith(str(inside_)), "the trap this test is about"
    assert p.may_approve(siblings) is False
    assert "not beneath" in p.approve(siblings, tmp_path).detail


def test_a_symlink_out_of_the_scope_is_refused(tmp_path):
    """The scope is about where the directory *is*, not what it is called."""
    p, root = _provider(tmp_path)
    outside = tmp_path / "geheim"
    outside.mkdir()
    link = root / "sieht-harmlos-aus"
    link.symlink_to(outside)

    assert p.may_approve(link) is False
    a = p.approve(link, tmp_path)
    assert a.granted is False
    assert "not beneath" in a.detail
    assert p.granted == []


def test_a_symlink_inside_the_scope_is_still_approved(tmp_path):
    """The control for the test above: refusing every symlink would be easy
    and would also refuse legitimate layouts. What is refused is escaping."""
    p, root = _provider(tmp_path)
    real = root / "echt"
    real.mkdir()
    link = root / "zeigt-nach-innen"
    link.symlink_to(real)
    assert p.may_approve(link) is True


def test_a_dotdot_escape_is_refused(tmp_path):
    p, root = _provider(tmp_path)
    (root / "a").mkdir()
    foreign = tmp_path / "fremd"
    foreign.mkdir()
    assert p.may_approve(root / "a" / ".." / ".." / "fremd") is False


def test_a_file_is_not_a_worktree(tmp_path):
    p, root = _provider(tmp_path)
    file = root / "keine-mappe"
    file.write_text("x")
    assert p.may_approve(file) is False
    assert "does not exist as a directory" in p.approve(file, tmp_path).detail


def test_a_target_swapped_after_the_check_is_refused(tmp_path):
    """Time-of-check to time-of-use. The window is small; what is on the other
    side of it is an agent with write access to wherever the path now points."""
    p, root = _provider(tmp_path)
    target = root / "wt"
    target.mkdir()
    outside = tmp_path / "woanders"
    outside.mkdir()

    real_identity = p._identity

    counter = {"n": 0}

    def alternating(path):
        # The scope check and the identity taken with it see the real
        # directory; the last look, immediately before the grant, sees
        # something else.
        counter["n"] += 1
        if counter["n"] <= 2:
            return real_identity(path)
        return real_identity(outside)

    p._identity = alternating
    a = p.approve(target, tmp_path)
    assert a.granted is False
    assert "changed identity" in a.detail
    assert p.granted == []


def test_a_scope_that_is_only_one_level_deep_is_refused(tmp_path):
    from hoh.approval import PrefixScopedProvider

    for too_broad in (Path("/opt"), Path("/srv"), Path("/anything")):
        with pytest.raises(ValueError, match="too broad"):
            PrefixScopedProvider(script=tmp_path / "x.sh", prefix=too_broad)


def test_a_parent_of_the_home_directory_is_refused(tmp_path):
    from hoh.approval import PrefixScopedProvider

    parent_dir = Path.home().resolve().parent
    with pytest.raises(ValueError, match="too broad"):
        PrefixScopedProvider(script=tmp_path / "x.sh", prefix=parent_dir)


# --------------------------------------------------------------------------- #
# Whose worktree, not only where
# --------------------------------------------------------------------------- #


def _repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=path, check=True,
    )
    return path


def test_only_worktrees_of_this_project_are_approved(tmp_path):
    """"Somewhere under the worktree directory" is a location, not an owner.

    A deployment that names a shared worktree root would otherwise let this
    provider trust another project's worktree sitting in the same place.
    """
    import subprocess

    meins = _repo(tmp_path / "meins")
    foreign = _repo(tmp_path / "fremd")
    root = tmp_path / "worktrees" / "gemeinsam"
    root.mkdir(parents=True)
    subprocess.run(["git", "worktree", "add", "-q", str(root / "meins-wt"), "-b", "x"],
                   cwd=meins, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(root / "fremd-wt"), "-b", "y"],
                   cwd=foreign, check=True)

    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)

    from hoh.approval import PrefixScopedProvider

    p = PrefixScopedProvider(script=script_, prefix=root, expected_repo=meins)
    assert p.may_approve(root / "meins-wt") is True
    assert p.may_approve(root / "fremd-wt") is False
    a = p.approve(root / "fremd-wt", foreign)
    assert a.granted is False
    assert "not to this project" in a.detail

    # Without the binding, both are in scope -- which is the state this option
    # exists to improve on, kept visible rather than implied.
    open_ = PrefixScopedProvider(script=script_, prefix=root)
    assert open_.may_approve(root / "fremd-wt") is True


def test_a_directory_that_is_no_worktree_at_all_is_refused_when_bound(tmp_path):
    meins = _repo(tmp_path / "meins")
    root = tmp_path / "worktrees" / "projekt"
    (root / "kein-repo").mkdir(parents=True)
    script_ = tmp_path / "ja.sh"
    script_.write_text("#!/bin/sh\nexit 0\n")
    script_.chmod(0o755)

    from hoh.approval import PrefixScopedProvider

    p = PrefixScopedProvider(script=script_, prefix=root, expected_repo=meins)
    a = p.approve(root / "kein-repo", meins)
    assert a.granted is False
    assert "not a git worktree" in a.detail or "not to this project" in a.detail


def test_the_grant_records_the_resolved_path_not_the_one_it_was_asked_about(tmp_path):
    """What was trusted has to be readable afterwards without re-deriving it."""
    p, root = _provider(tmp_path)
    real = root / "echt"
    real.mkdir()
    link = root / "zeigt-nach-innen"
    link.symlink_to(real)
    a = p.approve(link, tmp_path)
    assert a.granted is True
    assert a.worktree == real.resolve()
    assert p.granted == [real.resolve()]
