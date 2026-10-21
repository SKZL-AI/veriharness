"""Candidate binding through git plumbing.

Handoff §6: *"Every check binds to exactly the content that was tested: a
commit plus a clean source tree, or an immutable source/asset snapshot, the
relevant build and dependency locks, the test definitions and the run
parameters. Required untracked files must not be invisibly absent."*

## Why git and not something home-grown

An earlier version hashed every file itself and copied the working tree file by
file. That cost some 330 lines and produced **four** failure classes of its
own, every one of which was found only by an adversarial review: `git
write-tree` over the *index* instead of the working tree, quoted paths with
umlauts, dereferenced symlinks, a missing file mode.

The decisive break came last: `materialize` copied from the **current** working
tree. For a candidate from an earlier iteration it therefore quietly delivered
the wrong content -- and the check "was this criterion already green on the
predecessor state" compared against the *new* state.

git has been able to do all of this for twenty years:

* A temporary index plus `git add -A` and `git write-tree` yields a
  **content-addressed tree** over tracked *and* untracked-not-ignored files --
  with modes, faithful to symlinks, without quoting problems, and without
  touching the real index.
* `git archive <tree>` materializes exactly that tree, regardless of what the
  working tree has done since.

Apart from `commit_candidate`, this module changes no file of the working tree
and not the real index. `commit_candidate` does both -- exclusively in a
worktree created for the run, never on an integration branch, and only when the
tree carries exactly the verified candidate.
The developer is the only writer to the artifact (paper §3.4.2).
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import Candidate, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only; `hoh.wslease` imports this module
    from .wslease import WorkspaceLease


#: Branches HoH never commits to. An integration branch is the place where
#: other people's work comes together; an automatic commit there is exactly
#: the derivation of rights that handoff §6 forbids.
PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk", "release"})


def _is_own_worktree(path: Path) -> bool:
    """Is `path` a created git worktree rather than the main checkout?

    In a linked worktree `.git` is a **file** containing `gitdir: ...`; in the
    main checkout it is a directory. That is the most reliable local
    distinction, and it needs no path heuristics.
    """
    marker = path / ".git"
    return marker.is_file()


class WorkspaceError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} in {repo} failed ({proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def is_git_repo(repo: Path | str) -> bool:
    """Is `repo` the **root** of a git worktree -- not merely inside one?

    Until 2026-09-08 this asked `--is-inside-work-tree`, which answers a
    different question. Any subdirectory of a repository passed, and the
    consequence went all the way down: `snapshot` then bound the candidate to
    the **outer** repository's HEAD and tree, because `head_commit` and
    `working_tree` resolve upwards. Measured on a subdirectory of this very
    repository: the binding came back with the outer HEAD, byte-identical to
    `git -C ~/hoh rev-parse HEAD`. A verdict would have described a different
    tree than the one under test -- the deepest failure class this project
    has, since every acceptance rests on the candidate binding.

    Found by the dogfood run on itself: two tests asserted that `snapshot`
    refuses a non-worktree path, and they had always passed only because
    pytest's temporary directories normally sit outside the repository. The
    runner redirects `TMPDIR` into the arena, the arena lives under
    `runs/_arenas/`, and that is inside the repository -- so both tests failed
    in `d1` and `d2` and blocked every acceptance. The tests were right; the
    implementation was too lax.

    `--show-toplevel` answers the question that was meant. A linked worktree
    reports its own path, so `hoh worktree` keeps working.
    """
    try:
        top = _git(Path(repo), "rev-parse", "--show-toplevel")
    except WorkspaceError:
        return False
    if not top:
        return False
    try:
        return Path(top).resolve() == Path(repo).expanduser().resolve()
    except OSError:
        return False


def head_commit(repo: Path | str) -> str:
    return _git(Path(repo), "rev-parse", "HEAD")


#: The directory, inside a worktree's own git directory, where its
#: WorkspaceLease (`hoh.wslease`) lives. Inside the git directory because that
#: is the worktree's identity to git: every spelling of the path reaches the
#: same one, `git add -A` never sees it, and `git worktree remove` takes it
#: along with the worktree it governed.
LEASE_DIRNAME = "hoh-workspace-lease"


def lease_directory(repo: Path | str) -> Path:
    """Where the WorkspaceLease of the worktree rooted at `repo` is kept.

    `repo` must be the **root** of a worktree (`is_git_repo`): the lease of
    the outer repository is not the lease of a subdirectory. The path is the
    resolved `git rev-parse --absolute-git-dir` -- `<repo>/.git` for a main
    checkout, `<common>/worktrees/<name>` for a linked worktree -- so two
    spellings of one worktree (a symbolic link, `..`, a trailing slash) name
    one directory and two worktrees of one repository name two.
    """
    path = Path(repo).expanduser()
    if not is_git_repo(path):
        raise WorkspaceError(f"{path} is not a git worktree")
    git_dir = _git(path, "rev-parse", "--absolute-git-dir")
    if not git_dir:
        raise WorkspaceError(f"{path} reports no git directory")
    return Path(git_dir).resolve() / LEASE_DIRNAME


def is_clean(repo: Path | str) -> bool:
    return _git(Path(repo), "status", "--porcelain") == ""


def has_uncommitted_work(repo: Path | str) -> bool:
    """The commit's own question: is there anything in this worktree that `commit_candidate`
    (`git add -A`, then `git commit`) would take?

    Not `is_clean`. `git status --porcelain` answers what *status* shows, and that follows
    configuration the commit does not: `status.showUntrackedFiles=no` -- in the repository, in the
    worktree's own config or in the user's -- hides every untracked file from it, while `git add
    -A` takes them. A tree that is "clean" to status and dirty to the commit is how one run's
    leftover reached another run's commit (`controller._admit_worktree`, review of P3-06s2).
    So it is asked of the two commands that do the work, neither of which reads that setting:

    * `git add -A --dry-run` names every path `add -A` would stage -- modified, deleted and
      untracked-and-not-ignored, by the same exclude rules (`.gitignore`, `.git/info/exclude`,
      `core.excludesFile`) the commit applies, so a path the commit would not take is not a
      leftover;
    * `git diff-index --cached --name-only HEAD` names what the index already holds beyond HEAD,
      which `git commit` takes without `add`. `--ignore-submodules=none` overrides
      `diff.ignoreSubmodules` and the `.gitmodules` `ignore` setting, so a changed submodule
      pointer is not hidden either.

    Either listing non-empty is "yes". It changes nothing: no object, no reference and no byte
    of the index (`--dry-run`; `--no-optional-locks` only forgoes git's optional index refresh).
    It is **not** lock-free: `git add` takes `index.lock` for the duration of the command, so a
    foreign git command that holds it makes this one fail -- by design a refusal, not a race.
    A git that cannot answer (an unborn HEAD, a damaged index, a held `index.lock`) is a
    `WorkspaceError`, never "no": the caller refuses on it.

    Conservative where status-like precision would cost the rule: a *staged deletion* of a
    tracked file whose working copy still exists (`git rm --cached`) is listed by the index
    half although `add -A` would re-stage the file and the commit would be empty. Over-reporting
    is the safe direction: the caller refuses another run and a person looks.
    """
    path = Path(repo)
    would_stage = _git(path, "--no-optional-locks", "add", "-A", "--dry-run")
    held = _git(path, "--no-optional-locks", "diff-index", "--cached", "--name-only",
                "--ignore-submodules=none", "HEAD", "--")
    return bool(would_stage or held)


def working_tree(repo: Path | str) -> str:
    """A content-addressed tree over the **working tree**, leaving the index alone.

    A temporary index receives the current state (`add -A` respects
    `.gitignore` and picks up untracked, non-ignored files), and a tree object
    is written from it. The real index stays untouched -- `git status` in the
    project looks exactly as it did before.
    """
    path = Path(repo)
    with tempfile.NamedTemporaryFile(prefix="hoh-index-", delete=False) as tmp:
        index = tmp.name
    try:
        env = {"GIT_INDEX_FILE": index}
        _git(path, "read-tree", "HEAD", env=env, check=False)
        _git(path, "add", "-A", env=env)
        return _git(path, "write-tree", env=env)
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


def snapshot(repo: Path | str, candidate_id: str, *, note: str | None = None) -> Candidate:
    """Produces the candidate binding for the current state."""
    path = Path(repo).expanduser().resolve()
    if not is_git_repo(path):
        raise WorkspaceError(f"{path} is not a git worktree")
    candidate = Candidate(
        candidate_id=candidate_id,
        repo_path=str(path),
        commit=head_commit(path),
        tree_clean=is_clean(path),
        tree_digest=working_tree(path),
        created_at=utcnow(),
        note=note,
    )
    anchor(repo, candidate)   # against git maintenance; failure is not fatal
    return candidate



#: Namespace for the anchors. Nothing under `refs/hoh/` is touched by git of
#: its own accord; the refs are additive and are never deleted.
ANCHOR_PREFIX = "refs/hoh/candidates"


def anchor(repo: Path | str, candidate: Candidate) -> str | None:
    """Anchors the candidate tree as a git ref and returns the ref name.

    Without this a candidate hangs off an **unreachable** git object: the tree
    is produced by `git write-tree` over a temporary index and is held by no
    reference. A reviewer demonstrated that ordinary git maintenance removes it
    afterwards -- and then `last_accepted_candidate` points at something that
    no longer exists: `materialize` fails, no criterion discriminates any more,
    and **every further iteration is rejected without anything saying why**.

    A linked worktree shares the object database with the main repository, so
    maintenance run by a third party is enough to trigger this. A ref is the
    smallest measure that rules it out: it is created, never removed, and makes
    the object reachable.

    **An anchor never moves** (run qaw4: the resumed attempt of an aborted
    iteration reused the candidate id, `update-ref` moved the anchor to the
    new tree, and the integrity guard -- refs never move -- failed the run).
    The ref is created only if it does not exist (`update-ref <ref> <tree>
    ""`, atomic: git refuses an existing ref). An anchor that already holds
    this tree is the anchor, and its name is returned. One that holds another
    tree is left exactly as it is, and None is returned, as for any other
    failure to anchor (not fatal). The controller never asks for that: every
    attempt has its own candidate id.
    """
    path = Path(repo).expanduser().resolve()
    ref = f"{ANCHOR_PREFIX}/{candidate.candidate_id}"
    held = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True,
    )
    if held.returncode == 0:
        return ref if held.stdout.strip() == candidate.tree_digest else None
    proc = subprocess.run(
        ["git", "-C", str(path), "update-ref", ref, candidate.tree_digest, ""],
        capture_output=True, text=True,
    )
    return ref if proc.returncode == 0 else None


def unchanged(repo: Path | str, candidate: Candidate) -> bool:
    """Is the working tree still exactly the bound candidate?"""
    return snapshot(repo, candidate.candidate_id).binding() == candidate.binding()


def materialize(candidate: Candidate, dest: Path | str) -> Path:
    """Creates an isolated copy of exactly the **bound** candidate.

    The paper (§3.4.3) explicitly has QA check an isolated copy, and the
    handoff requires immutable production sources with separate QA artifacts.

    `git archive` works on the tree object, not on the working tree. A
    candidate from an earlier iteration is therefore restored correctly -- the
    home-grown copy read the current state and quietly delivered the wrong
    content for older candidates.
    """
    src = Path(candidate.repo_path)
    target = Path(dest).expanduser()
    if target.exists() and any(target.iterdir()):
        raise WorkspaceError(f"target directory {target} is not empty")
    target.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        ["git", "-C", str(src), "archive", "--format=tar", candidate.tree_digest],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git archive {candidate.tree_digest[:12]} failed: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp.write(proc.stdout)
        tar_path = tmp.name
    try:
        with tarfile.open(tar_path) as tar:
            # No escape: git trees contain only relative paths, but the check
            # costs nothing and documents the intent.
            root = target.resolve()
            for entry in tar.getmembers():
                member = (target / entry.name).resolve()
                if not str(member).startswith(str(root)):
                    raise WorkspaceError(
                        f"path points out of the arena: {entry.name!r}"
                    )
            tar.extractall(target, filter="tar")
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass

    return target


def commit_candidate(
    repo: Path | str, candidate: Candidate, *, run_id: str, iteration: int,
    lease: "WorkspaceLease | None" = None,
) -> str:
    """Turns an accepted candidate into a durable commit.

    Until this existed, an accepted checkpoint lived **only** as a dirty
    working tree: the candidate binding is content-addressed (`git write-tree`)
    and works without a commit for exactly that reason -- which was and remains
    right for the checking. For durability it is not enough. A `git checkout`
    or a `git stash` would have quietly destroyed six iterations of work, and
    `hoh deliver` consequently refused with "the working tree is not clean".

    The commit is the **inner** act of making things durable, not a delivery:
    it is created on the working branch in the isolated worktree that HoH
    created itself. No push, no merge, no protected branch -- that stays
    reserved for `deliver` with explicit approval (handoff §6).

    **One writer per worktree (P3-06).** With a `lease`, the commit is made
    only if that lease is held *now* by its handle and governs *this* worktree
    (`WorkspaceLease.check`): a handle that never acquired, one that was
    superseded, released or has run out, and a lease taken for another
    worktree are each refused with a `WorkspaceLeaseError` (a
    `WorkspaceError`), and a value that is not a lease with a plain
    `WorkspaceError`, before the first `git add -A`. Without a lease nothing
    changes: the existing callers pass none.

    Returns the commit SHA. If there is nothing to commit, the existing HEAD.
    """
    path = Path(repo).expanduser().resolve()

    # The ownership test comes **before** the first `git add -A`. Without it
    # this function committed to whichever branch happened to be checked out --
    # `main` in the captain's live checkout, in the worst case -- and picked up
    # that unfinished work through `add -A`. A reviewer reproduced exactly
    # this. `hoh start` only requires "is a git worktree"; the hint about
    # `hoh worktree` was non-binding. From here on it is not.
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    if branch in ("", "HEAD"):
        raise WorkspaceError(
            f"{path} is on no named branch (detached HEAD). HoH does not write "
            "there."
        )
    if branch in PROTECTED_BRANCHES:
        raise WorkspaceError(
            f"{path} is on the protected branch '{branch}'. The developer "
            "belongs in an isolated worktree -- 'hoh worktree' creates one. "
            "HoH never commits to an integration branch."
        )
    if not _is_own_worktree(path):
        raise WorkspaceError(
            f"{path} is the main checkout, not a worktree created for this "
            "run. HoH commits only into a working tree that was created for "
            "this run -- otherwise `git add -A` sweeps up other people's work."
        )

    # After the ownership tests, before anything is read or written: a handle
    # that does not hold the worktree has no business asking whether it is
    # clean either.
    if lease is not None:
        check = getattr(lease, "check", None)
        if not callable(check):
            raise WorkspaceError(
                f"lease must be a WorkspaceLease or None, not {type(lease).__name__}: "
                "a value that is not a lease is never taken for 'no lease'"
            )
        check(path)

    # The commit's own question (`has_uncommitted_work`), not `git status`'s: under
    # `status.showUntrackedFiles=no` a tree whose only change is an untracked file is "clean"
    # to status and was accepted here without a commit -- the history said "committed as <the
    # old head>" and the file stayed in the tree for the next run.
    if not has_uncommitted_work(path):
        return head_commit(path)

    before = working_tree(path)
    if before != candidate.tree_digest:
        raise WorkspaceError(
            f"The working tree carries {before}, but the accepted candidate "
            f"carries {candidate.tree_digest}. Only what was verified gets "
            "committed -- something came in between here."
        )

    _git(path, "add", "-A")
    _git(
        path,
        "-c", "user.name=hoh",
        "-c", "user.email=hoh@localhost",
        "commit",
        "-m",
        f"HoH {run_id} iteration {iteration}: candidate {candidate.candidate_id}\n\n"
        f"Accepted after independent QA with runner receipts.\n"
        f"Tree: {candidate.tree_digest}\n"
        f"Base: {candidate.commit}",
    )
    new_head = head_commit(path)
    after = working_tree(path)
    if after != candidate.tree_digest:
        raise WorkspaceError(
            "The tree changed while committing -- the commit does not reflect "
            "the verified state."
        )
    return new_head
