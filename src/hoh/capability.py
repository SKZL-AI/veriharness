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

import errno
import hashlib
import os
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


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
    #: Trees that must be untouched **on disk**, not only in content: their
    #: bytes, and every entry's mode, size, modification and change time
    #: (`frozen_digest`). For QA's candidate arena, which is sealed read-only
    #: for the dispatch: a write that was undone, a `chmod` and back, or a
    #: timestamp set back after a content write all leave a change time.
    frozen: tuple[Path, ...] = ()
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
            f" · freezes {len(self.frozen)} tree(s)"
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
            target = os.fsdecode(p.read_bytes()).split("gitdir:", 1)[-1].strip()
        except OSError:                   # pragma: no cover
            return ""
        p = Path(target)
    if not p.is_dir():
        return ""
    places = [p]
    shared = p / "commondir"
    if shared.is_file():
        try:
            target = (p / os.fsdecode(shared.read_bytes()).strip()).resolve()
        except OSError:                   # pragma: no cover
            target = None
        if target is not None and target.is_dir() and target != p:
            places.append(target)
    h = hashlib.sha256()
    for place in places:
        for name in GIT_STATE:
            f = place / name
            if f.is_file():
                h.update(os.fsencode(str(f)))
                h.update(f.read_bytes())
        for below in GIT_DIRECTORIES:
            tree_ = place / below
            if not tree_.is_dir():
                continue
            for f in sorted(tree_.rglob("*")):
                if f.is_file():
                    h.update(os.fsencode(str(f.relative_to(place))))
                    h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _entry(file_path: Path, rel: str, h) -> None:
    """Fold one filesystem entry into a digest, whatever kind it is.

    Every kind is folded in, including the ones an earlier version skipped:
    a symlink (as its target, not followed), an empty directory (which
    contains no files to notice), and a file's executable bit (which is not a
    content change). Each of those was a way past the check.

    Names and link text are folded in with `os.fsencode`, never a strict
    `.encode()`: a name that is not valid UTF-8 arrives as surrogate escapes,
    and `.encode()` raised on it and blocked the next dispatch (O249). The
    bytes are the filesystem's own, so two such names never collapse into
    one, and a valid UTF-8 name gives the same bytes -- and digest -- as before.
    """
    if file_path.is_symlink():
        h.update(b"L")
        h.update(os.fsencode(rel))
        h.update(b"\x00")
        h.update(os.fsencode(os.readlink(file_path)))
    elif file_path.is_dir():
        h.update(b"D")
        h.update(os.fsencode(rel))
        h.update(b"\x00")
    elif file_path.is_file():
        h.update(b"Fx" if os.access(file_path, os.X_OK) else b"F-")
        h.update(os.fsencode(rel))
        h.update(b"\x00")
        try:
            h.update(hashlib.sha256(file_path.read_bytes()).digest())
        except OSError:                            # pragma: no cover
            h.update(b"?")


#: The deepest directory nesting `tree_digest` and `inode_digest` walk, and the most entries either folds in. Past
#: either the walk is `TreeWalkRefused`: a digest of a part of a tree is not a digest of the tree. The walk is
#: iterative, so these are limits of the *measurement*, not of the interpreter's stack (a path-based walk meets
#: ENAMETOOLONG near 2000 levels long before the depth limit).
MAX_DIGEST_DEPTH = 4096
MAX_DIGEST_ENTRIES = 5_000_000


class TreeWalkRefused(OSError):
    """A tree could not be walked completely: a limit was passed, or a directory could not be listed.

    An `OSError`, so a caller that already handles one keeps working. Loud on purpose: the walks that raise it
    feed digests and a budget, and a part of a tree taken for the whole is the failure they exist to prevent."""


def walk_tree(root: Path | str, *, skip_denied: bool = True, prune: str | None = None,
              max_depth: int | None = None, max_entries: int | None = None):
    """Every entry below `root` as `(relative path, full path)`, in the order of `sorted(Path(root).rglob("*"))`.

    That order is a pre-order walk with the names of each directory sorted as strings -- a directory is followed by
    its contents and then by its next sibling -- which is what comparing paths component by component gives.
    **No link is followed and none is skipped:** a symlink (dangling or not) is an entry like any other, a
    directory is entered only if it is a real one. `Path.rglob` is not used because its answer differs between the
    interpreters the package supports (3.11 omits a dangling link when the pattern is a literal name and recurses
    once per level, so a deep tree raises `RecursionError`); this walk keeps an explicit stack and sees the same
    entries on all of them.

    A directory that cannot be listed: one that has vanished is skipped (it is gone); `PermissionError` is skipped
    when `skip_denied` (what every pathlib did, and what the digests keep) and `TreeWalkRefused` otherwise; any
    other error (a path too long, an I/O error) is `TreeWalkRefused` -- never a silent partial walk.
    `prune`: an entry with this name is neither yielded nor entered. `max_depth` / `max_entries`: more than that
    many directory levels / entries is `TreeWalkRefused`.
    """
    top = os.fspath(root)
    count = 0
    stack = []                                   # (full path, relative path, iterator over its sorted entries)

    def listing(full: str, depth: int):
        if max_depth is not None and depth > max_depth:
            raise TreeWalkRefused(errno.ELOOP, f"nested deeper than {max_depth} levels", full)
        try:
            with os.scandir(full) as scan:
                rows = [(e.name, e.is_dir(follow_symlinks=False)) for e in scan]
        except (FileNotFoundError, NotADirectoryError):
            return None
        except PermissionError as exc:
            if skip_denied:
                return None
            raise TreeWalkRefused(exc.errno, "cannot be listed", full) from exc
        except OSError as exc:
            raise TreeWalkRefused(exc.errno, f"cannot be listed ({exc.strerror})", full) from exc
        rows.sort(key=lambda row: row[0])
        return iter(rows)

    rows_ = listing(top, 1)
    if rows_ is not None:
        stack.append((top, "", rows_))
    while stack:
        full, rel, rows_ = stack[-1]
        row = next(rows_, None)
        if row is None:
            stack.pop()
            continue
        name, is_dir = row
        if name == prune:
            continue
        count += 1
        if max_entries is not None and count > max_entries:
            raise TreeWalkRefused(errno.EFBIG, f"holds more than {max_entries} entries", top)
        child = os.path.join(full, name)
        child_rel = f"{rel}/{name}" if rel else name
        yield child_rel, child
        if is_dir:
            below = listing(child, len(stack) + 1)
            if below is not None:
                stack.append((child, child_rel, below))


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
    if VCS_METADATA in p.parts:
        return h.hexdigest()[:16]
    for rel, full in walk_tree(p, prune=VCS_METADATA, max_depth=MAX_DIGEST_DEPTH,
                               max_entries=MAX_DIGEST_ENTRIES):
        _entry(Path(full), rel, h)
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

    `git status` runs with `core.quotePath=true` and is hashed as bytes: a
    user's `quotePath=false` would otherwise print a non-UTF-8 name raw,
    which both changed the digest and failed a strict decode.
    """
    p = Path(root)
    if not (p / VCS_METADATA).exists():
        return tree_digest(p)
    import subprocess

    out = subprocess.run(
        ["git", "-c", "core.quotePath=true", "-C", str(p),
         "status", "--porcelain", "-uall"],
        capture_output=True, check=False)
    if out.returncode != 0:                        # pragma: no cover - exotic
        return tree_digest(p)
    h = hashlib.sha256()
    h.update(out.stdout)
    h.update(git_state_digest(p).encode())
    return h.hexdigest()[:16]


def inode_digest(root: Path | str) -> str:
    """A digest over what `tree_digest` cannot see: each entry's inode facts.

    `lstat` of the root and of every entry below it (no link is followed),
    folding in the relative name, `st_mode`, size, `st_mtime_ns` and
    `st_ctime_ns`. The access time is left out: reading a frozen tree is the
    work a reviewer is there to do, and it must not read as a touch.

    The change time is what makes this worth having. A write that was undone,
    a mode changed and changed back, or a content write whose modification
    time was set back with `touch -d` all leave the bytes as they were -- and
    all of them move `st_ctime_ns`, which no unprivileged process can set.
    A path that does not exist digests as the empty string, like
    `tree_digest`.
    """
    p = Path(root)
    try:
        st = os.lstat(p)
    except OSError:
        return ""
    h = hashlib.sha256()

    def fold(rel: str, st_: os.stat_result) -> None:
        h.update(os.fsencode(rel))
        h.update(b"\x00")
        h.update(f"{st_.st_mode}:{st_.st_size}:{st_.st_mtime_ns}:"
                 f"{st_.st_ctime_ns}".encode())
        h.update(b"\x00")

    fold(".", st)
    if stat.S_ISDIR(st.st_mode):
        entries = [rel for rel, _full in walk_tree(p, max_depth=MAX_DIGEST_DEPTH,
                                                   max_entries=MAX_DIGEST_ENTRIES)]
        for rel in sorted(entries):
            try:
                fold(rel, os.lstat(p / rel))
            except OSError:                        # gone while walking
                h.update(os.fsencode(rel))
                h.update(b"\x00?\x00")
    return h.hexdigest()[:16]


def frozen_digest(root: Path | str) -> str:
    """Contents and inode facts of a frozen tree, as one comparable string."""
    return tree_digest(root) + ":" + inode_digest(root)


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
        h.update(os.fsencode(kind))
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
    frozen: dict[str, str] = field(default_factory=dict)

    @classmethod
    def take(cls, policy: RoleExecutionPolicy) -> CapabilityWitness:
        return cls(
            policy=policy,
            digests={str(p): repo_digest(p) for p in policy.protected},
            shallow={str(p): shallow_digest(p) for p in policy.protected_shallow},
            git={str(p): git_state_digest(p) for p in policy.protected},
            frozen={str(p): frozen_digest(p) for p in policy.frozen},
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
            if key in self.frozen:
                self.frozen[key] = frozen_digest(p)
                if key not in renewed:
                    renewed.append(key)
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
        for file_path, before in self.frozen.items():
            if frozen_digest(Path(file_path)) != before:
                out_list.append(
                    f"a frozen tree was touched during {self.policy.role}'s "
                    f"dispatch (contents, modes or change times): {file_path}"
                )
        return out_list


class CapabilityViolation(RuntimeError):
    """A role touched something its policy protects.

    Raised rather than returned: a run whose planner wrote into the tree its
    acceptance checks measure has not produced a verdict about the product, and
    continuing would attach a result to a measurement that was interfered with.
    """


# --------------------------------------------------------------------------- #
# The seal: QA's candidate arena, read-only on disk for QA's dispatch
# --------------------------------------------------------------------------- #

#: How long `apply_seal` waits after its last `chmod`. Change times come from
#: the kernel's coarse clock, so a `chmod` and back right after the seal could
#: otherwise land in the same tick as the seal itself and leave the frozen
#: digest unchanged. On a kernel with fine-grained timestamps the wait is
#: merely unnecessary.
SEAL_SETTLE_S = 0.05

#: Every write bit. The seal clears exactly these and nothing else: the read
#: and execute bits stay, so the candidate can still be read and run.
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class SealFailed(RuntimeError):
    """The candidate arena could not be sealed; whatever was changed is rolled back."""


class SealEntry(NamedTuple):
    """One entry of a seal plan: its original mode and its inode identity.

    The identity is what every later `chmod` is checked against. A path is
    only a name; between the plan and the unseal a reviewer can rename an
    entry and put a symlink or a hard link to a foreign file in its place.
    """

    mode: int
    st_dev: int
    st_ino: int


def seal_plan(root: Path | str) -> dict[str, SealEntry]:
    """Every entry under `root` with its mode and identity, root first.

    Keyed by the path relative to `root` (`.` for the root itself), ordered
    shallowest first, so the plan order is the `chmod` order. Symlinks are
    left out: their own mode means nothing, and following one leads out of
    the arena. A non-directory entry with more than one name (`st_nlink > 1`)
    raises `SealFailed` before anything is changed: one of its names may lie
    outside the arena, where a `chmod` would reach a file that is not ours,
    and the count cannot tell the two cases apart.
    """
    p = Path(root)
    st = os.lstat(p)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, f"not a directory: {p}")
    found: list[tuple[int, str, SealEntry]] = [
        (0, ".", SealEntry(st.st_mode, st.st_dev, st.st_ino))]
    for rel, full in walk_tree(p):
        st_ = os.lstat(full)
        if stat.S_ISLNK(st_.st_mode):
            continue
        if not stat.S_ISDIR(st_.st_mode) and st_.st_nlink > 1:
            raise SealFailed(
                f"{rel} has {st_.st_nlink} hard links: a file with more than "
                "one name is not sealed, because one name may lie outside the "
                "arena and the count cannot tell; nothing changed")
        found.append((rel.count(os.sep) + 1, rel,
                      SealEntry(st_.st_mode, st_.st_dev, st_.st_ino)))
    found.sort(key=lambda t: (t[0], t[1]))
    return {rel: entry for _, rel, entry in found}


def _open_inside(root: Path | str, rel: str) -> int:
    """An `O_PATH` descriptor for `rel`, reached from `root` one component at a time.

    No component is a symlink that gets followed: each is opened with
    `O_NOFOLLOW`, relative to the descriptor of the one before, so a name
    swapped for a link cannot lead the caller out of the arena. An absolute
    `rel`, a `..` or an empty component is refused. Every descriptor but the
    returned one is closed, on every way out.
    """
    if os.path.isabs(rel):
        raise OSError(errno.EINVAL, f"refusing an absolute path: {rel}")
    parts = [] if rel == "." else rel.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise OSError(errno.EINVAL, f"refusing a path outside the arena: {rel!r}")
    fd = os.open(root, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts):
            flags = os.O_PATH | os.O_NOFOLLOW
            if i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_ = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_
    except BaseException:
        os.close(fd)
        raise
    return fd


def _chmod_verified(root: Path | str, rel: str, entry: SealEntry, mode: int) -> None:
    """`chmod` the inode the plan recorded, and nothing else.

    The entry is opened inside the arena (`_open_inside`), checked against
    the plan's `(st_dev, st_ino)` and refused if it is a symlink or another
    inode, and then changed through `/proc/self/fd/<fd>` -- the pinned
    descriptor, so the name cannot be swapped between the check and the act.
    """
    fd = _open_inside(root, rel)
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, f"refusing a symlink in the arena: {rel}")
        if (st.st_dev, st.st_ino) != (entry.st_dev, entry.st_ino):
            raise OSError(errno.ESTALE,
                          f"refusing a replaced entry in the arena: {rel}")
        if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
            # A name outside the arena (planted after the plan, or a forged
            # plan for an arena that was released) would be changed too.
            raise OSError(errno.EMLINK,
                          f"a file with more than one name is left alone: {rel}")
        os.chmod(f"/proc/self/fd/{fd}", mode & 0o7777)
    finally:
        os.close(fd)


def unseal(root: Path | str, plan: dict[str, SealEntry]) -> list[str]:
    """Restore every planned mode; return the entries refused or not reached.

    Never raises: an unseal runs on ways out where an exception would hide
    the one that is already travelling. What it could not restore is named,
    for the caller to record.
    """
    failed: list[str] = []
    for rel, entry in plan.items():
        try:
            _chmod_verified(root, rel, entry, entry.mode)
        except Exception:                          # noqa: BLE001 - named, never raised
            failed.append(rel)
    return failed


def apply_seal(root: Path | str, plan: dict[str, SealEntry]) -> None:
    """Clear every write bit under `root`, in plan order, or change nothing.

    On a failing `chmod` what was already changed is restored (`unseal`) and
    `SealFailed` is raised. A sealed arena then waits `SEAL_SETTLE_S`.
    """
    done: dict[str, SealEntry] = {}
    for rel, entry in plan.items():
        try:
            _chmod_verified(root, rel, entry, entry.mode & ~_WRITE_BITS)
        except OSError as exc:
            not_restored = unseal(root, done)
            raise SealFailed(
                f"sealing {root} failed at {rel}: {exc}"
                + (f"; not restored: {not_restored}" if not_restored else "")
            ) from exc
        done[rel] = entry
    time.sleep(SEAL_SETTLE_S)


def seal_read_only(root: Path | str) -> dict[str, SealEntry]:
    """Plan and apply the seal; the plan is what restores it."""
    try:
        plan = seal_plan(root)
    except OSError as exc:
        raise SealFailed(f"planning the seal of {root} failed: {exc}") from exc
    apply_seal(root, plan)
    return plan


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
              shallow: tuple[Path, ...] = (),
              frozen: tuple[Path, ...] = ()) -> RoleExecutionPolicy:
    """Reads the candidate and the evidence; writes its verdict and nothing else.

    QA works *in* the arena rather than with its address in a prompt, because
    a reviewer that cannot run the thing is reviewing a description. It does
    not get a write scope there: its own artifacts once broke the candidate
    binding and cost the iteration.

    Its working directory is still the arena **root**, which is the shape
    O125 objected to for the planner. The arenas it may not touch are
    therefore named in `protected` -- every one except the candidate it is
    reviewing -- rather than being merely out of the way.

    The candidate it reviews is `frozen`: sealed read-only on disk for the
    dispatch, and witnessed down to each entry's change time, so that a write
    QA undid before answering is still a violation.
    """
    return RoleExecutionPolicy(
        role="qa",
        read_scopes=(arena,),
        write_scopes=(answer,),
        protected=protected,
        protected_shallow=shallow,
        frozen=frozen,
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
