"""H1/H2: candidate binding against real git trees.

The handoff requires binding to *exactly the content that was tested*. These
tests demonstrate that the commit id alone is not enough for that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh.workspace import (
    WorkspaceError,
    head_commit,
    is_clean,
    is_git_repo,
    snapshot,
    unchanged,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "Test")
    (r / "a.py").write_text("print('one')\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first")
    return r


def test_not_a_git_repo(tmp_path: Path):
    assert not is_git_repo(tmp_path / "does-not-exist")
    with pytest.raises(WorkspaceError, match="is not a git worktree"):
        snapshot(tmp_path, "c1")


def test_snapshot_records_commit_and_cleanliness(repo: Path):
    c = snapshot(repo, "c1")
    assert c.commit == head_commit(repo)
    assert c.tree_clean is True
    assert is_clean(repo)


def test_a_change_without_a_commit_changes_the_binding(repo: Path):
    """The decisive case: same commit, different content."""
    before = snapshot(repo, "c1")
    (repo / "a.py").write_text("print('two')\n", encoding="utf-8")
    after = snapshot(repo, "c1")

    assert before.commit == after.commit, "the commit is unchanged"
    assert before.binding() != after.binding(), "the binding must not be"
    assert after.tree_clean is False


def test_an_untracked_file_becomes_visible(repo: Path):
    """Handoff §6: required untracked files must not be invisibly absent."""
    before = snapshot(repo, "c1")
    (repo / "conftest_local.py").write_text("X = 1\n", encoding="utf-8")
    after = snapshot(repo, "c1")
    assert before.binding() != after.binding()


def test_an_ignored_file_does_not_change_the_binding(repo: Path):
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore")
    before = snapshot(repo, "c1")
    (repo / "run.log").write_text("noise\n", encoding="utf-8")
    assert snapshot(repo, "c1").binding() == before.binding()


def test_a_lock_file_counts_towards_the_binding(repo: Path):
    """Different dependencies = a different object under test."""
    (repo / "requirements.txt").write_text("pydantic==2.11.7\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "deps")
    before = snapshot(repo, "c1")

    (repo / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    assert snapshot(repo, "c1").binding() != before.binding()


def test_unchanged_detects_tampering_after_qa(repo: Path):
    """A05: sources changed after QA."""
    bound = snapshot(repo, "c1")
    assert unchanged(repo, bound)
    (repo / "a.py").write_text("print('tampered')\n", encoding="utf-8")
    assert not unchanged(repo, bound)


def test_a_commit_changes_the_binding(repo: Path):
    before = snapshot(repo, "c1")
    (repo / "b.py").write_text("print('new')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second")
    after = snapshot(repo, "c2")
    assert before.commit != after.commit
    assert before.binding() != after.binding()
    assert after.tree_clean is True


def test_a_subdirectory_of_a_repo_is_not_a_worktree_root(repo: Path):
    """K11, found by the dogfood run on itself, 2026-09-08.

    `is_git_repo` used to ask `--is-inside-work-tree`, which answers a
    different question: any subdirectory of a repository passed. The
    consequence went all the way down -- `snapshot` then bound the candidate
    to the **outer** repository's HEAD and tree, because `head_commit` and
    `working_tree` resolve upwards. A verdict would have described a different
    tree than the one under test, and every acceptance in this project rests
    on the candidate binding.

    It surfaced because the runner redirects `TMPDIR` into the arena, the
    arena lives inside this repository, and two tests asserting exactly this
    contract therefore failed in `d1` and `d2` -- blocking every acceptance.
    The tests were right; the implementation was too lax.
    """
    sub = repo / "subdir"
    sub.mkdir()

    assert is_git_repo(repo), "the repository root itself stays a worktree"
    assert not is_git_repo(sub), (
        "a subdirectory is inside a worktree but is not one -- accepting it "
        "makes snapshot bind to the outer repository"
    )
    with pytest.raises(WorkspaceError, match="is not a git worktree"):
        snapshot(sub, "c1")


def test_the_binding_would_have_described_the_outer_repo(repo: Path):
    """The measurement behind K11, pinned so the reason cannot get lost.

    Without the fix, `snapshot(subdir)` returns the outer repository's commit.
    This test proves the two are the same object, so that a future reader can
    see *why* a subdirectory must be refused rather than having to trust the
    prose.
    """
    sub = repo / "subdir"
    sub.mkdir()

    # The outer repository is a real worktree and binds fine.
    outer = snapshot(repo, "outer")
    assert outer.commit == head_commit(repo)

    # git resolves upwards from the subdirectory -- that is the whole trap.
    assert head_commit(sub) == outer.commit
    # And that is exactly why the subdirectory is refused now.
    assert not is_git_repo(sub)


def _anchor(repo: Path, candidate_id: str) -> str:
    """The tree `refs/hoh/candidates/<id>` holds, or "" when there is none."""
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         f"refs/hoh/candidates/{candidate_id}"],
        capture_output=True, text=True).stdout.strip()


def test_an_anchor_never_moves(repo: Path):
    """Catches (run qaw4) an anchor moved to another tree under the same
    candidate id: `git update-ref` overwrote it, and the integrity guard --
    refs never move -- failed the run. A second tree under an id already
    anchored leaves the anchor as it is (`anchor` returns None). Control: the
    same tree under the same id is the anchor (its name is returned), and
    another id anchors the new tree."""
    from hoh.workspace import anchor

    first = snapshot(repo, "c1")
    assert _anchor(repo, "c1") == first.tree_digest
    assert anchor(repo, first) == "refs/hoh/candidates/c1"

    (repo / "a.py").write_text("print('two')\n", encoding="utf-8")
    moved = snapshot(repo, "c1")
    assert moved.tree_digest != first.tree_digest
    assert anchor(repo, moved) is None
    assert _anchor(repo, "c1") == first.tree_digest
    other = snapshot(repo, "c2")
    assert _anchor(repo, "c2") == other.tree_digest != first.tree_digest


def test_unchanged_leaves_the_anchor_alone(repo: Path):
    """Catches `unchanged()` -- a comparison -- re-anchoring the candidate's id
    at the tree it compares: a source changed during QA moved the anchor of
    the frozen candidate to the change, a ref move the integrity guard fails.
    Control: an untouched tree is unchanged, and the anchor holds the bound
    tree throughout."""
    bound = snapshot(repo, "c1")
    assert unchanged(repo, bound)
    assert _anchor(repo, "c1") == bound.tree_digest
    (repo / "a.py").write_text("print('tampered')\n", encoding="utf-8")
    assert not unchanged(repo, bound)
    assert _anchor(repo, "c1") == bound.tree_digest


def test_a_racing_create_does_not_move_the_anchor(repo: Path, monkeypatch):
    """Catches `update-ref <ref> <tree>` without the empty old value: the ref
    is read first, but another create can land between that read and the
    `update-ref`, and then the call moved the anchor (the very failure of run
    qaw4) instead of being refused. The create is atomic only through git's
    refusal of an existing ref. Here the ref appears (at another tree) between
    the two calls: `anchor` returns None and the ref still holds the racing
    tree. Control: the same candidate id without the race is created, and its
    name is returned."""
    import types

    from hoh import workspace

    first = snapshot(repo, "c1")
    (repo / "a.py").write_text("print('two')\n", encoding="utf-8")
    second = snapshot(repo, "c2")
    assert second.tree_digest != first.tree_digest

    calm = second.model_copy(update={"candidate_id": "calm"})
    assert workspace.anchor(repo, calm) == "refs/hoh/candidates/calm"
    assert _anchor(repo, "calm") == second.tree_digest

    real = subprocess.run
    raced = []

    def interpose(args, **kwargs):
        if "update-ref" in args:
            raced.append(real(["git", "-C", str(repo), "update-ref",
                               "refs/hoh/candidates/race", first.tree_digest],
                              check=True, capture_output=True))
        return real(args, **kwargs)

    monkeypatch.setattr(workspace, "subprocess", types.SimpleNamespace(run=interpose))
    racing = second.model_copy(update={"candidate_id": "race"})
    assert workspace.anchor(repo, racing) is None
    assert len(raced) == 1
    assert _anchor(repo, "race") == first.tree_digest
