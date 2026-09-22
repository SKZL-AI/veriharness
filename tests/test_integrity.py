"""The role-integrity guard: does it notice every way the house rule breaks?

Roles run in `auto` mode, and the approval policy's deny-list is a behavioural
control -- `rm` through an absolute path or an interpreter does not match a
textual rule. The rule is therefore proven by inventory, and every case here
breaks it one way and requires the guard to name the path. The positive
control is ordinary work inside the run's own worktree, which must pass.
"""
from __future__ import annotations

import subprocess

import pytest

from hoh import integrity


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(cwd, msg="c"):
    _git("add", "-A", cwd=cwd)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=cwd)


@pytest.fixture
def layout(tmp_path):
    """A project with a main checkout, the run's worktree, and one unrelated
    worktree -- the three things a run can touch."""
    main = tmp_path / "project"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "README.md").write_text("readme\n")
    (main / "keep.txt").write_text("keep\n")
    _commit(main, "initial")
    _git("tag", "v0", cwd=main)
    run_wt = tmp_path / "wt-run"
    other_wt = tmp_path / "wt-other"
    _git("worktree", "add", "-q", "-b", "hoh-run", str(run_wt), cwd=main)
    _git("worktree", "add", "-q", "-b", "other", str(other_wt), cwd=main)
    return {"main": main, "run": run_wt, "other": other_wt}


def test_the_positive_control_ordinary_work_inside_the_run_passes(layout):
    before = integrity.snapshot(layout["run"])
    (layout["run"] / "greet.py").write_text("def greet(n): return f'Hello, {n}!'\n")
    (layout["run"] / "README.md").write_text("readme, edited\n")
    _commit(layout["run"], "work")
    assert integrity.verify(before) == []


def test_a_deleted_pre_existing_file_fails_and_is_named(layout):
    before = integrity.snapshot(layout["run"])
    (layout["run"] / "keep.txt").unlink()
    problems = integrity.verify(before)
    assert any("deleted:" in p and "keep.txt" in p for p in problems), problems


def test_a_deletion_through_an_interpreter_is_caught_all_the_same(layout):
    """The case the deny-list cannot see: no `rm` in sight."""
    before = integrity.snapshot(layout["run"])
    subprocess.run(["python3", "-c", "import os; os.remove('keep.txt')"],
                   cwd=layout["run"], check=True)
    assert any("keep.txt" in p for p in integrity.verify(before))


def test_a_replacement_that_archives_the_old_version_passes(layout):
    """Versioning is the house convention; a moved-aside file is not lost."""
    before = integrity.snapshot(layout["run"])
    old = layout["run"] / "keep.txt"
    old.rename(old.with_name("keep.txt.v1.20260922T000000Z"))
    assert integrity.verify(before) == []


def test_an_archive_copy_also_counts(layout):
    before = integrity.snapshot(layout["run"])
    (layout["run"] / ".archiv").mkdir()
    (layout["run"] / "keep.txt").rename(layout["run"] / ".archiv" / "keep.txt.v1")
    assert integrity.verify(before) == []


def test_a_change_to_an_unrelated_worktree_fails(layout):
    before = integrity.snapshot(layout["run"])
    (layout["other"] / "stray.txt").write_text("written from the wrong place\n")
    problems = integrity.verify(before)
    assert any("unrelated worktree changed" in p and "wt-other" in p for p in problems)


def test_a_write_into_the_main_checkout_fails(layout):
    """A write outside the run's own scope, into the project's checkout."""
    before = integrity.snapshot(layout["run"])
    (layout["main"] / "README.md").write_text("tampered\n")
    assert any(str(layout["main"].resolve()) in p for p in integrity.verify(before))


def test_a_moved_or_removed_foreign_ref_fails(layout):
    before = integrity.snapshot(layout["run"])
    _git("tag", "-d", "v0", cwd=layout["main"])
    assert any("ref removed: refs/tags/v0" in p for p in integrity.verify(before))


def test_rewriting_the_run_branch_fails_but_advancing_it_does_not(layout):
    before = integrity.snapshot(layout["run"])
    (layout["run"] / "a.txt").write_text("a\n")
    _commit(layout["run"], "forward")
    assert integrity.verify(before) == []
    # Rewrite: point the branch at a commit that does not contain the old tip.
    _git("checkout", "-q", "--orphan", "rewritten", cwd=layout["run"])
    (layout["run"] / "b.txt").write_text("b\n")
    _commit(layout["run"], "orphan")
    orphan = _git("rev-parse", "HEAD", cwd=layout["run"])
    _git("update-ref", "refs/heads/hoh-run", orphan, cwd=layout["run"])
    problems = integrity.verify(before)
    assert any("history rewritten: refs/heads/hoh-run" in p for p in problems), problems


def test_a_removed_worktree_fails(layout, tmp_path):
    before = integrity.snapshot(layout["run"])
    layout["other"].rename(tmp_path / "moved-away")
    assert any("worktree removed" in p for p in integrity.verify(before))
