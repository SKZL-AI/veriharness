"""Accepted candidate -> exactly that candidate lands, once. Never a branch name.

The evidence audit of 2026-09-25 found `HohRunLauncher.merge` merging the
*branch name*: whatever the branch pointed to at merge time landed, and the
decision record named the candidate's base commit. For a dirty-tree candidate
that base does not contain the accepted work at all. These tests run the real
launcher against a real repository:

* the branch head carries the accepted tree -> that exact commit is integrated
  and named in the receipt;
* the branch advanced to B after A was accepted -> refused, main untouched;
* the accepted (dirty-tree) work was never committed onto the branch -> refused,
  not "already contained" (which is what merging the base used to report);
* a second merge after a crash finds the same commit already contained.
"""

from __future__ import annotations

import subprocess

from hoh.launcher import HohRunLauncher
from hoh.orchestrator import MergeFailure, RunOutcome, RunVerdict
from hoh.project import TaskNode


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@l",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@l",
                               "PATH": "/usr/bin:/bin"}).stdout.strip()


def repo_with_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "hoh-n1")
    (repo / "a.txt").write_text("accepted\n")
    git(repo, "commit", "-q", "-am", "accepted work")
    accepted = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return repo, base, accepted


def launcher(tmp_path, repo):
    return HohRunLauncher(tmp_path / "root", repo, mainline="main")


def outcome(commit, tree):
    return RunOutcome(RunVerdict.ACCEPTED, "accepted c1", candidate=commit,
                      candidate_tree=tree)


def test_the_accepted_commit_is_integrated_and_named(tmp_path):
    repo, base, accepted = repo_with_branch(tmp_path)
    tree = git(repo, "rev-parse", f"{accepted}^{{tree}}")
    m = launcher(tmp_path, repo).merge(TaskNode(id="n1"), outcome(accepted, tree))
    assert m.landed and m.integrated == accepted and m.accepted_tree == tree
    parents = git(repo, "log", "-1", "--format=%P", "main").split()
    assert parents[1] == accepted
    assert (repo / "a.txt").read_text() == "accepted\n"


def test_a_branch_advanced_after_acceptance_is_refused(tmp_path):
    repo, base, accepted = repo_with_branch(tmp_path)
    tree = git(repo, "rev-parse", f"{accepted}^{{tree}}")
    git(repo, "checkout", "-q", "hoh-n1")
    (repo / "a.txt").write_text("unverified B\n")
    git(repo, "commit", "-q", "-am", "B, after acceptance")
    git(repo, "checkout", "-q", "main")
    main_before = git(repo, "rev-parse", "main")
    m = launcher(tmp_path, repo).merge(TaskNode(id="n1"), outcome(accepted, tree))
    assert not m.landed and m.failure is MergeFailure.CANDIDATE_MISMATCH
    assert "moved after acceptance" in m.detail
    assert git(repo, "rev-parse", "main") == main_before
    assert (repo / "a.txt").read_text() == "base\n"


def test_uncommitted_dirty_tree_work_is_refused_not_reported_as_contained(tmp_path):
    """The dirty-tree shape: the candidate names the base, the tree is elsewhere."""
    repo, base, accepted = repo_with_branch(tmp_path)
    tree = git(repo, "rev-parse", f"{accepted}^{{tree}}")
    git(repo, "branch", "-f", "hoh-n1", base)       # the work never reached the branch
    m = launcher(tmp_path, repo).merge(TaskNode(id="n1"), outcome(base, tree))
    assert not m.landed and m.failure is MergeFailure.CANDIDATE_MISMATCH
    assert m.detail != "already contained in the mainline"


def test_a_repeat_after_a_crash_finds_the_same_commit_contained(tmp_path):
    repo, base, accepted = repo_with_branch(tmp_path)
    tree = git(repo, "rev-parse", f"{accepted}^{{tree}}")
    ln = launcher(tmp_path, repo)
    first = ln.merge(TaskNode(id="n1"), outcome(accepted, tree))
    head = git(repo, "rev-parse", "main")
    again = ln.merge(TaskNode(id="n1"), outcome(accepted, tree))
    assert first.landed and again.landed and again.integrated == accepted
    assert again.detail == "already contained in the mainline"
    assert git(repo, "rev-parse", "main") == head        # no second merge commit


def test_without_a_tree_the_commit_itself_must_match(tmp_path):
    repo, base, accepted = repo_with_branch(tmp_path)
    m = launcher(tmp_path, repo).merge(TaskNode(id="n1"), outcome(base, None))
    assert m.failure is MergeFailure.CANDIDATE_MISMATCH
