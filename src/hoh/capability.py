"""What a role may touch, enforced rather than requested.

The planner's prompt says, in those words: *implement nothing, edit nothing,
test nothing*. A real run's transcript shows it moving the file under test,
writing a reference implementation, and executing the acceptance criteria it
was drafting. The handoff this project was built from says the same thing about
prompts that this module exists to act on: *"writing read-only into a prompt is
not enforcement when a shell is freely available."*

Two things were true at once when that was measured, and keeping them apart is
the whole point of this file:

* The planner could **not** reach the repository or the candidate. A03 gives it
  a copy of its own, and that held.
* The planner's working directory was the **arena root**, and the candidate
  arena for every iteration and attempt is a sibling there. Nothing structural
  stopped it from writing into the tree the acceptance checks run in.

So the boundary was in the right place for one threat and absent for another,
and neither was written down as a policy anybody could check. This module makes
it one.

## What enforcement means here, precisely

There are three mechanisms, and they are honestly different in strength:

1. **Placement.** A role is given a directory that contains what it may see and
   nothing else. The planner's copy gets a parent of its own, so the candidate
   arenas are no longer siblings. This is structural: the thing is not there.
2. **Detection.** Every protected tree is digested before the role runs and
   after. A change is a violation, the run fails closed, and the report names
   the paths. This catches what placement cannot -- an absolute path, a
   symlink, a `..`, a temporary file moved in afterwards -- because it does not
   reason about *how* a write happened, only that the bytes differ.
3. **Declaration.** The policy says what a role may do, in fields a reader can
   check against the prompt. A role with no policy gets nothing: the default
   denies every capability, so adding a role without thinking about it fails
   closed rather than inheriting the last role's rights.

What this is **not** is a security boundary against a hostile agent running as
the same user. Such an agent can `chmod`, can reach any path it can name, and
can re-create what it deleted. Detection sees all of that after the fact and
the run refuses to produce a verdict -- which is the property that matters for
evidence -- but nothing here prevents it. `sandbox.py` is where prevention
lives, and it applies to check commands, not to agents in panes.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    """One thing a role may be permitted to do."""

    #: Read files under the role's read scopes.
    FS_READ = "fs_read"
    #: Create, modify or remove files under the role's write scopes.
    FS_WRITE = "fs_write"
    #: Run `git` in a way that only inspects (log, status, show, diff).
    GIT_READ = "git_read"
    #: Run `git` in a way that changes a repository (add, commit, checkout).
    GIT_WRITE = "git_write"
    #: Start processes at all.
    PROCESS_EXEC = "process_exec"
    #: Reach the network.
    NETWORK = "network"


@dataclass(frozen=True)
class RoleExecutionPolicy:
    """What one role may touch, and where its answer is allowed to come from.

    Every field defaults to the most restrictive value. A role constructed
    without arguments may read nothing, write nothing, run nothing and reach
    nothing -- which is deliberately useless, so that a new role has to state
    its needs rather than inherit someone else's.
    """

    role: str
    #: Directories the role may read. A path outside all of them is not
    #: *prevented* from being read -- an agent can open any file it can name --
    #: but reading one is outside the declared contract, and `protected` below
    #: is what turns that from a statement into a check.
    read_scopes: tuple[Path, ...] = ()
    #: Directories the role may write. Empty means: nowhere.
    write_scopes: tuple[Path, ...] = ()
    #: Trees whose contents must be **byte-identical** before and after the
    #: role runs. This is the enforceable half of the policy.
    protected: tuple[Path, ...] = ()
    #: Directories whose **set of direct children** must not change while the
    #: role runs. Weaker than `protected` and deliberately so: a directory
    #: that legitimately gains a child between dispatches cannot be digested
    #: whole, but nothing legitimate adds one during a dispatch.
    protected_shallow: tuple[Path, ...] = ()
    git_read: bool = False
    git_write: bool = False
    process_exec: bool = False
    network: bool = False
    #: The one file the role's answer may come from. A role that writes its
    #: result anywhere else has not answered.
    output_channel: Path | None = None
    #: How this role's *checks* execute, where it runs any. "none" or "strict".
    isolation: str = "none"
    #: Capabilities granted here **because they cannot be withheld**, not
    #: because the role is meant to use them. An agent in a pane has a shell;
    #: declaring `process_exec` false would be a statement this code cannot
    #: back, and a policy that claims more than it enforces is the exact
    #: defect O125 was. Naming them keeps the prompt and the policy
    #: comparable: where the prompt forbids something listed here, the
    #: difference is a request, and `protected` is what turns any of it into
    #: a check.
    declared_but_unenforced: tuple[str, ...] = ()

    def allows(self, cap: Capability) -> bool:
        return {
            Capability.FS_READ: bool(self.read_scopes),
            Capability.FS_WRITE: bool(self.write_scopes),
            Capability.GIT_READ: self.git_read,
            Capability.GIT_WRITE: self.git_write,
            Capability.PROCESS_EXEC: self.process_exec,
            Capability.NETWORK: self.network,
        }[cap]

    def may_write(self, path: Path | str) -> bool:
        """Is `path` inside a declared write scope?

        Resolved on both sides, so a `..`, a symlink or an absolute path does
        not get a different answer than the place it actually lands.
        """
        try:
            target = Path(path).expanduser().resolve()
        except OSError:                            # pragma: no cover - exotic
            return False
        for scope in self.write_scopes:
            s = scope.expanduser().resolve()
            if target == s or s in target.parents:
                return True
        return False

    def summary(self) -> str:
        allowed_ = [c.value for c in Capability if self.allows(c)]
        return (
            f"{self.role}: {', '.join(allowed_) or 'nothing'}"
            f" · writes {len(self.write_scopes)} scope(s)"
            f" · protects {len(self.protected)} tree(s)"
            f" + {len(self.protected_shallow)} listing(s)"
            f" · isolation {self.isolation}"
        )


#: Git's own metadata is excluded from a digest. Not because writing there is
#: harmless -- it is the opposite -- but because reading a repository changes
#: it: `git status` refreshes the index, and a witness that fires on that is a
#: witness nobody leaves switched on. Git-level mutation is covered instead by
#: the post-hoc measurement in `tools/confinement_evidence.py`, which reads
#: refs, unreachable objects, the index and the working tree.
VCS_METADATA = ".git"

#: What *is* read out of `.git`, because these change only when somebody moves
#: the repository's history: the current head, the ref files, and the packed
#: ref list. The index is deliberately not among them -- it is what a plain
#: read refreshes.
#: Plus the files that decide what a *checkout* of that history contains.
#: `materialize()` builds every candidate with `git archive`, which honours
#: `$GIT_DIR/info/attributes`: one line of `export-ignore` there removes a file
#: from every future candidate, with no ref moving and no tracked file
#: changing. A reviewer demonstrated it. `config` and the hooks are in for the
#: same reason -- `core.hooksPath`, a clean/smudge filter and a hook all change
#: what comes out of a checkout without touching the history.
GIT_STATE = ("HEAD", "packed-refs", "config",
               "info/attributes", "info/exclude")
GIT_DIRECTORIES = ("refs", "hooks")


def git_state_digest(root: Path | str) -> str:
    """A digest over a repository's refs and head, ignoring its index.

    Excluding `.git` wholesale would have made a commit invisible to the
    witness, and a commit is exactly how a write gets made durable. Excluding
    only the index keeps the check quiet under ordinary reads while still
    seeing history move.

    **A linked worktree keeps almost none of this where it looks like it
    does.** Its `.git` is a pointer file to `…/.git/worktrees/<name>`, and that
    directory holds `HEAD`, `index` and `logs` -- but no `refs/` and no
    `packed-refs`. Those live in the common directory named by `commondir`
    beside it. HoH runs every candidate in a linked worktree, so reading only
    the pointer's target would have digested a `HEAD` that says
    `ref: refs/heads/<branch>` before and after a commit, and seen nothing.
    Both directories are read.
    """
    p = Path(root) / VCS_METADATA
    if p.is_file():                       # a worktree: .git is a pointer file
        try:
            target = p.read_text().split("gitdir:", 1)[-1].strip()
        except OSError:                   # pragma: no cover
            return ""
        p = Path(target)
    if not p.is_dir():
        return ""
    places = [p]
    shared = p / "commondir"
    if shared.is_file():
        try:
            target = (p / shared.read_text().strip()).resolve()
        except OSError:                   # pragma: no cover
            target = None
        if target is not None and target.is_dir() and target != p:
            places.append(target)
    h = hashlib.sha256()
    for place in places:
        for name in GIT_STATE:
            f = place / name
            if f.is_file():
                h.update(str(f).encode())
                h.update(f.read_bytes())
        for below in GIT_DIRECTORIES:
            tree_ = place / below
            if not tree_.is_dir():
                continue
            for f in sorted(tree_.rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(place)).encode())
                    h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _entry(file_path: Path, rel: str, h) -> None:
    """Fold one filesystem entry into a digest, whatever kind it is.

    Every kind is folded in, including the ones an earlier version skipped:
    a symlink (as its target, not followed), an empty directory (which
    contains no files to notice), and a file's executable bit (which is not a
    content change). Each of those was a way past the check.
    """
    if file_path.is_symlink():
        h.update(b"L")
        h.update(rel.encode())
        h.update(b"\x00")
        h.update(os.readlink(file_path).encode())
    elif file_path.is_dir():
        h.update(b"D")
        h.update(rel.encode())
        h.update(b"\x00")
    elif file_path.is_file():
        h.update(b"Fx" if os.access(file_path, os.X_OK) else b"F-")
        h.update(rel.encode())
        h.update(b"\x00")
        try:
            h.update(hashlib.sha256(file_path.read_bytes()).digest())
        except OSError:                            # pragma: no cover
            h.update(b"?")


def tree_digest(root: Path | str) -> str:
    """A digest over a path's contents: a directory tree, or a single file.

    Names are included, so a rename is a change. A path that does not exist
    digests as the empty string rather than raising: a protected tree that is
    not there yet is a real situation (the first iteration), and it is caught
    by the *comparison*, where "absent" and "present" differ.

    Bytecode is **not** excluded. It was, and that was a working way through:
    a `.pyc` whose header matches the source's mtime and size wins at import,
    so a stub could be made to pass its own acceptance criteria with nothing
    in the source tree changing. A check run legitimately leaves bytecode in
    the arena it ran in -- but that arena is not protected during the dispatch
    that produced it, and every tree that *is* protected has no business
    gaining bytecode while a role runs.
    """
    p = Path(root)
    h = hashlib.sha256()
    if p.is_symlink() or p.is_file():
        _entry(p, p.name, h)
        return h.hexdigest()[:16]
    if not p.is_dir():
        return ""
    for entry in sorted(p.rglob("*")):
        if VCS_METADATA in entry.parts:
            continue
        _entry(entry, str(entry.relative_to(p)), h)
    return h.hexdigest()[:16]


def repo_digest(root: Path | str) -> str:
    """What a **git worktree** contributes to a witness, without walking it.

    A full `tree_digest` over a repository is wrong twice. It is slow -- 48.8 s
    over this project's own tree, paid twice per planner and QA dispatch -- and
    it is *stricter than the run's own freeze check*, which binds through
    `git write-tree` and therefore honours `.gitignore`. A background process
    the developer left running, writing `__pycache__` into the repository, then
    failed an iteration that `unchanged()` considers untouched.

    So a repository is measured the way the freeze check measures it: the
    working tree's own report of what differs from HEAD, plus the history
    state. Files git ignores cannot reach a candidate -- `materialize()` builds
    it with `git archive` -- so they cannot affect a measurement, and the
    witness has no business failing a run over them.
    """
    p = Path(root)
    if not (p / VCS_METADATA).exists():
        return tree_digest(p)
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(p), "status", "--porcelain", "-uall"],
        capture_output=True, text=True, check=False)
    if out.returncode != 0:                        # pragma: no cover - exotic
        return tree_digest(p)
    h = hashlib.sha256()
    h.update(out.stdout.encode())
    h.update(git_state_digest(p).encode())
    return h.hexdigest()[:16]


def shallow_digest(root: Path | str) -> str:
    """A digest over a directory's **direct children's names only**.

    What this catches that a full digest cannot afford to: a new directory
    appearing beside the trees that are protected. The arena root gains a
    child every iteration in the ordinary course of things, so digesting it
    fully around a dispatch is wrong -- but nothing legitimate creates one
    *while a role is running*, and a staging directory created then, to be
    moved in afterwards, is exactly the shape of the attack the full digest
    was designed to miss.
    """
    p = Path(root)
    if not p.is_dir():
        return ""
    h = hashlib.sha256()
    for kind in sorted(x.name for x in p.iterdir()):
        h.update(kind.encode())
        h.update(b"\x00")
    return h.hexdigest()[:16]


@dataclass
class CapabilityWitness:
    """Digests of a policy's protected trees, taken at a point in time.

    Named a witness rather than a snapshot because it holds no content: it can
    say *that* something changed and which tree, never what it was. That is
    enough to fail closed and it keeps the check cheap enough to run around
    every role dispatch.
    """

    policy: RoleExecutionPolicy
    digests: dict[str, str] = field(default_factory=dict)
    shallow: dict[str, str] = field(default_factory=dict)
    git: dict[str, str] = field(default_factory=dict)

    @classmethod
    def take(cls, policy: RoleExecutionPolicy) -> CapabilityWitness:
        return cls(
            policy=policy,
            digests={str(p): repo_digest(p) for p in policy.protected},
            shallow={str(p): shallow_digest(p) for p in policy.protected_shallow},
            git={str(p): git_state_digest(p) for p in policy.protected},
        )

    def witness_again(self, paths: Iterable[Path | str]) -> list[str]:
        """Re-takes the baseline for exactly these paths, and says which.

        For the one writer this witness cannot otherwise account for: the
        controller itself. A retry has to charge a dispatch, charging means
        persisting `state.json`, and `state.json` is protected -- so the
        controller's own bookkeeping would read as a violation by the role.

        Re-taking the **whole** witness there was the first attempt and it was
        wrong: it re-baselined every protected tree, so a role that wrote into
        `checks.json` in the same window had that write folded into the new
        baseline and disappeared. Measured by an adversarial review, which
        planted exactly that write and watched the run continue with a
        hijacked acceptance suite.

        This re-takes one path. Everything else keeps the baseline it was
        taken with, so a write anywhere else in the window is still caught.
        What remains uncovered is a write to the *same* path between the
        controller's write and this call -- a file the controller is
        rewriting anyway, so no digest could attribute it either. Recorded in
        `docs/LIMITATIONS.md` rather than left to be found.
        """
        renewed = []
        for file_path in paths:
            key = str(file_path)
            p = Path(key)
            if key in self.digests:
                self.digests[key] = repo_digest(p)
                renewed.append(key)
            if key in self.git:
                self.git[key] = git_state_digest(p)
            if key in self.shallow:
                self.shallow[key] = shallow_digest(p)
        return renewed

    def violations(self) -> list[str]:
        """Protected trees that differ from when this witness was taken.

        The wording is "changed during X's dispatch", never "X changed": two
        digests at two points in time cannot identify a writer. A stray check
        process, a second run over the same arena root, or a person at a
        terminal would all produce this, and a ledger entry saying the planner
        did it would be the wrong kind of sentence in an evidence system.
        """
        out_list = []
        for file_path, before in self.digests.items():
            after = repo_digest(Path(file_path))
            if after != before:
                out_list.append(
                    f"a protected tree changed during {self.policy.role}'s "
                    f"dispatch: {file_path} "
                    f"({before or 'absent'} -> {after or 'absent'})"
                )
        for file_path, before in self.git.items():
            after = git_state_digest(Path(file_path))
            if after != before:
                out_list.append(
                    f"a protected repository's history moved during "
                    f"{self.policy.role}'s dispatch: {file_path}"
                )
        for file_path, before in self.shallow.items():
            after = shallow_digest(Path(file_path))
            if after != before:
                out_list.append(
                    f"a protected directory gained or lost a child during "
                    f"{self.policy.role}'s dispatch: {file_path}"
                )
        return out_list


class CapabilityViolation(RuntimeError):
    """A role touched something its policy protects.

    Raised rather than returned: a run whose planner wrote into the tree its
    acceptance checks measure has not produced a verdict about the product, and
    continuing would attach a result to a measurement that was interfered with.
    """


#: The three roles, with what each actually needs. Read this next to the
#: prompts in `roles.py`: where the two disagree, one of them is wrong, and
#: this file is the one a machine can check.
def planner_policy(*, read_copy: Path, answer: Path,
                   protected: tuple[Path, ...] = (),
                   shallow: tuple[Path, ...] = ()) -> RoleExecutionPolicy:
    """Reads a copy, writes one file, and every other tree is verified.

    The read copy is writable -- it is the planner's own, it is discarded, and
    making it read-only would only invite a `chmod`.

    The write scope is the **answer file itself**, not the directory it lives
    in. Every role answers into the same directory, so a scope of
    `answers/` declared that the planner may write QA's verdict, which is
    precisely the confusion this file exists to remove.

    `process_exec` is true and the prompt says *test nothing*. Both are
    correct: an agent in a pane has a shell, so denying it here would be a
    claim this code cannot back. It is listed in `declared_but_unenforced` so
    that the difference between what is requested and what is enforced stays
    readable instead of looking like an oversight.
    """
    return RoleExecutionPolicy(
        role="planner",
        read_scopes=(read_copy,),
        write_scopes=(answer,),
        protected=protected,
        protected_shallow=shallow,
        git_read=True,
        git_write=False,
        process_exec=True,
        network=False,
        output_channel=answer,
        isolation="none",
        declared_but_unenforced=("process_exec", "git_read"),
    )


def developer_policy(*, arena: Path, answer: Path,
                     protected: tuple[Path, ...] = (),
                     shallow: tuple[Path, ...] = ()) -> RoleExecutionPolicy:
    """The one role that writes the artifact, and the only one.

    Its workspace is therefore not protected -- that is the point of it -- and
    everything else is: the run's own evidence, and the arenas of iterations
    that are already measured.
    """
    return RoleExecutionPolicy(
        role="developer",
        read_scopes=(arena,),
        write_scopes=(arena, answer),
        protected=protected,
        protected_shallow=shallow,
        git_read=True,
        git_write=False,
        process_exec=True,
        network=False,
        output_channel=answer,
        isolation="none",
        declared_but_unenforced=("network",),
    )


def qa_policy(*, arena: Path, answer: Path,
              protected: tuple[Path, ...] = (),
              shallow: tuple[Path, ...] = ()) -> RoleExecutionPolicy:
    """Reads the candidate and the evidence; writes its verdict and nothing else.

    QA works *in* the arena rather than with its address in a prompt, because
    a reviewer that cannot run the thing is reviewing a description. It does
    not get a write scope there: its own artifacts once broke the candidate
    binding and cost the iteration.

    Its working directory is still the arena **root**, which is the shape
    O125 objected to for the planner. The arenas it may not touch are
    therefore named in `protected` -- every one except the candidate it is
    reviewing -- rather than being merely out of the way.
    """
    return RoleExecutionPolicy(
        role="qa",
        read_scopes=(arena,),
        write_scopes=(answer,),
        protected=protected,
        protected_shallow=shallow,
        git_read=True,
        git_write=False,
        process_exec=True,
        network=False,
        output_channel=answer,
        isolation="none",
        declared_but_unenforced=("network",),
    )


#: What a role gets when nobody said. Every capability denied, every tree
#: protected that the caller names, and no write scope at all -- so a role
#: added without a policy cannot run rather than running with someone else's
#: rights.
def denied_policy(role: str, *, protected: tuple[Path, ...] = ()) -> RoleExecutionPolicy:
    return RoleExecutionPolicy(role=role, protected=protected)
