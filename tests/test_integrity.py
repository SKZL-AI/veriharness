"""The role-integrity guard: does it notice every way the house rule breaks?

Roles run in `auto` mode, and the approval policy's deny-list is a behavioural
control -- `rm` through an absolute path or an interpreter does not match a
textual rule. The rule is therefore proven by inventory, and every case here
breaks it one way and requires the guard to name the path. The positive
control is ordinary work inside the run's own worktree, which must pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

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



# -- names that are not UTF-8 (O249) -----------------------------------------
#
# Git prints file and ref names as raw bytes. Every test here creates such a
# name inside a `TemporaryDirectory()` it closes itself, never in `tmp_path`
# or the `layout` fixture: a check that leaves one in the arena blocks the next
# dispatch.

_IDENT = (b"-c", b"user.email=t@t", b"-c", b"user.name=t")
_QUOTEPATH_FALSE = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotePath",
                    "GIT_CONFIG_VALUE_0": "false"}


def _gitb(repo: bytes, *args: bytes) -> bytes:
    return subprocess.run([b"git", b"-C", repo, *args], check=True,
                          capture_output=True).stdout.strip()


def _repo_with_tracked_non_utf8(base: bytes) -> bytes:
    repo = os.path.join(base, b"proj")
    os.mkdir(repo)
    _gitb(repo, b"init", b"-q", b"-b", b"main")
    with open(os.path.join(repo, b"caf\xe9.bin"), "wb") as f:
        f.write(b"x\n")
    _gitb(repo, b"add", b"-A")
    _gitb(repo, *_IDENT, b"commit", b"-qm", b"initial")
    return repo


def _clear_quotepath_env(monkeypatch):
    for key in _QUOTEPATH_FALSE:
        monkeypatch.delenv(key, raising=False)


def test_snapshot_tracked_non_utf8_name(monkeypatch):
    """Default configuration: `ls-files -z` prints the raw bytes regardless."""
    _clear_quotepath_env(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_tracked_non_utf8(os.fsencode(d))
        before = integrity.snapshot(os.fsdecode(repo))
        assert before["tracked"] == [os.fsdecode(b"caf\xe9.bin")]
        assert integrity.verify(before) == []


def test_snapshot_verify_roundtrip_non_utf8(monkeypatch):
    """Snapshot -> JSON -> verify, with a non-UTF-8 file, run branch and refs."""
    _clear_quotepath_env(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_tracked_non_utf8(os.fsencode(d))
        _gitb(repo, b"checkout", b"-q", b"-b", b"lauf-\xe9")
        # Two refs that one lossy decode would fold into a single key.
        _gitb(repo, b"branch", b"fremd-\xe8")
        _gitb(repo, b"branch", b"fremd-\xe9")

        text = json.dumps(integrity.snapshot(os.fsdecode(repo)), indent=2)
        assert "\\udce9" in text
        before = json.loads(text)
        assert before["run_branch"] == os.fsdecode(b"refs/heads/lauf-\xe9")
        assert [os.fsencode(t) for t in before["tracked"]] == [b"caf\xe9.bin"]
        assert {os.fsdecode(b"refs/heads/fremd-\xe8"),
                os.fsdecode(b"refs/heads/fremd-\xe9")} <= set(before["refs"])
        assert integrity.verify(before) == [], "the unchanged repository held"

        # The run's own branch moving forward is fine ...
        with open(os.path.join(repo, b"b.txt"), "wb") as f:
            f.write(b"b\n")
        _gitb(repo, b"add", b"-A")
        _gitb(repo, *_IDENT, b"commit", b"-qm", b"forward")
        assert integrity.verify(before) == []
        # ... another non-UTF-8 ref moving is not.
        tip = _gitb(repo, b"rev-parse", b"HEAD")
        _gitb(repo, b"update-ref", b"refs/heads/fremd-\xe8", tip)
        problems = integrity.verify(before)
        assert len(problems) == 1, problems
        assert problems[0].startswith(
            "ref moved: " + os.fsdecode(b"refs/heads/fremd-\xe8") + " "), problems


def test_tree_state_independent_of_quotepath(monkeypatch):
    """A user's `quotePath=false` must not change an unrelated worktree's digest."""
    _clear_quotepath_env(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_tracked_non_utf8(os.fsencode(d))
        with open(os.path.join(repo, b"neu-\xe9.txt"), "wb") as f:
            f.write(b"untracked\n")
        plain = integrity._tree_state(os.fsdecode(repo))
        for key, value in _QUOTEPATH_FALSE.items():
            monkeypatch.setenv(key, value)
        raw = _gitb(repo, b"status", b"--porcelain", b"--untracked-files=all")
        assert b"neu-\xe9.txt" in raw, "the environment did not switch quotePath off"
        assert integrity._tree_state(os.fsdecode(repo)) == plain
