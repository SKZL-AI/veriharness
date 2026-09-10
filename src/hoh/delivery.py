"""Delivery: the step from an internal checkpoint to an actual hand-off.

Handoff §6: *"An accepted checkpoint is an internal HoH state first of all. No
developer task may change a protected target branch through a firstmate
automerge ahead of the outer HoH QA. This assignment grants no additional push,
merge, deployment or purchasing rights."*

Until this module existed, `delivery_mode` and `yolo` were fields that were
carried around and displayed and **took effect nowhere** -- a reviewer called
them, rightly, "an enum value and a text field". This module gives them effect,
and does so as narrowly as the assignment permits:

* **`local-only`** -- HoH may fast-forward the working branch into its base
  branch, but only after the captain's explicit approval, and only if the
  branch is clean and really fast-forwardable. No rebase, no merge commit, no
  force.
* **`direct-PR` and `no-mistakes`** -- HoH does **not** deliver. Both routes
  belong to firstmate and to the captain; HoH marks the state as ready for
  delivery and names the branch. Taking push or PR rights for itself would be
  exactly the derivation the handoff forbids.

`yolo` stays hard at `off`: without `--approve` nothing happens.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class DeliveryRefused(RuntimeError):
    """Delivery is refused. Never force it -- the reason is the answer."""


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    description: str
    branch: str | None = None
    target: str | None = None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def current_branch(repo: Path | str) -> str:
    proc = _git(Path(repo), "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        raise DeliveryRefused(f"branch not readable: {proc.stderr.strip()}")
    return proc.stdout.strip()


def is_fast_forward(repo: Path | str, target: str, source: str) -> bool:
    """Is `source` a direct descendant of `target`?"""
    proc = _git(Path(repo), "merge-base", "--is-ancestor", target, source)
    return proc.returncode == 0


def _checkout_owner(repo: Path, branch: str) -> str | None:
    """The worktree in which `branch` is checked out -- or None.

    `git worktree list --porcelain` names, for each worktree, its path and the
    branch checked out there. Without this lookup HoH passed git's raw error
    message through ("refusing to update checked out branch"), which is correct
    but useless in operation: it does not say where the branch is or what to do
    about it.
    """
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return None
    path: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref in (f"refs/heads/{branch}", branch) and path:
                if Path(path).resolve() != repo:
                    return path
    return None


def deliver(
    *,
    repo: Path | str,
    mode: str,
    yolo: str,
    approved: bool,
    target_branch: str,
    candidate_id: str | None,
    tree_digest: str | None = None,
) -> DeliveryResult:
    """Performs the delivery, or refuses it with a reason."""
    repo = Path(repo).expanduser().resolve()

    if candidate_id is None:
        raise DeliveryRefused(
            "There is no accepted candidate. An internal checkpoint only "
            "comes into being once an iteration has been accepted."
        )

    if yolo != "off":
        raise DeliveryRefused(
            f"yolo={yolo!r} is not provided for in HoH. Merge autonomy belongs "
            "to firstmate and to the captain; this assignment grants no "
            "additional merge rights."
        )

    if not approved:
        raise DeliveryRefused(
            "No approval. Without the captain's explicit consent HoH delivers "
            "nothing -- not even a green checkpoint."
        )

    if mode in ("direct-PR", "no-mistakes"):
        branch = current_branch(repo)
        return DeliveryResult(
            delivered=False,
            description=(
                f"mode {mode}: HoH does not deliver by itself. The verified state "
                f"sits on branch '{branch}' (candidate {candidate_id}). Push, PR "
                f"and merge go through firstmate and the captain's approval -- HoH "
                f"derives no rights for that."
            ),
            branch=branch,
        )

    if mode != "local-only":
        raise DeliveryRefused(f"unknown delivery mode: {mode!r}")

    # --- local-only: a guarded fast-forward -------------------------------- #
    #
    # What gets delivered is what was **verified**. The guards below check the
    # state of the tree (clean, fast-forwardable, target free) -- none of them
    # binds the content to the verdict. Whoever commits in the worktree after
    # the acceptance, and the developer agent sits exactly there, would get
    # their own state delivered under the message "candidate X taken over".
    if tree_digest:
        actual = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        if actual != tree_digest:
            raise DeliveryRefused(
                f"The branch carries tree {actual or '(unreadable)'}, but the "
                f"accepted candidate {candidate_id} carries {tree_digest}. Only "
                "what was verified gets delivered -- something was added here "
                "after the acceptance."
            )

    branch = current_branch(repo)
    if branch == target_branch:
        raise DeliveryRefused(
            f"working branch and target branch are the same ({branch}). The "
            "developer is meant to work in an isolated worktree -- 'hoh worktree' "
            "creates one."
        )

    dirty = _git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        raise DeliveryRefused(
            "The working tree is not clean. Unlanded work is never cleared "
            f"away:\n{dirty[:400]}"
        )

    if not is_fast_forward(repo, target_branch, branch):
        raise DeliveryRefused(
            f"'{branch}' is not a direct descendant of '{target_branch}'. HoH does "
            "no rebase and no merge commit -- that is the captain's decision."
        )

    # If the target branch is checked out elsewhere, git refuses the push --
    # and rightly so: it would leave the index and working tree of that
    # worktree inconsistent. An `update-ref` would be the way around it, and
    # that is exactly why it is wrong: HoH would change a checkout that does
    # not belong to it. The approval covered the delivery, not an intervention
    # in someone else's work.
    owner = _checkout_owner(repo, target_branch)
    if owner:
        raise DeliveryRefused(
            f"'{target_branch}' is checked out in {owner}. A fast-forward there "
            "would change the index and working tree of that worktree -- HoH "
            "does not touch a checkout that does not belong to it.\n"
            f"Either take it over there yourself:\n"
            f"    git -C {owner} merge --ff-only {branch}\n"
            f"or deliver into a branch that is not checked out:\n"
            f"    hoh deliver <run> --approve --into <other-branch>"
        )

    proc = _git(repo, "push", ".", f"{branch}:{target_branch}")
    if proc.returncode != 0:
        raise DeliveryRefused(
            f"fast-forward to '{target_branch}' failed: {proc.stderr.strip()[:300]}"
        )

    return DeliveryResult(
        delivered=True,
        description=(
            f"candidate {candidate_id} taken over by fast-forward from '{branch}' "
            f"to '{target_branch}'. No rebase, no merge commit, no force."
        ),
        branch=branch,
        target=target_branch,
    )
