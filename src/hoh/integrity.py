"""What a run's roles left untouched, measured before and after.

The captain's standing rule: nothing is deleted, previous state is versioned
or archived, unrelated worktrees stay untouched, worker roles do not publish.
Roles now run in the harness's `auto` mode (P1-09), and the approval policy's
deny-list is a *behavioural* control: it makes the normal tool paths for
`rm`, `git push` and the rest fail closed, and nothing more. An equivalent
invocation through an absolute path, a shell wrapper or an interpreter does
not match a textual rule. So the rule is not proven by the policy; it is
proven here, by inventory:

* **every pre-existing tracked file of the run's own worktree still exists**,
  or was archived by the house convention (`<name>.v<...>` beside it, or a
  copy under `.archiv/`) -- a replaced file is fine, a vanished one is not;
* **every ref of the project that is not the run's own branch is unchanged**:
  no branch moved, no tag moved or vanished;
* **the run's own branch only moved forward**: its old tip is an ancestor of
  the new one, so no history was rewritten;
* **every other worktree of the project is exactly as it was**: same HEAD,
  same tracked content, same status.

A violation is reported path by path and fails the run. It is never repaired
and then called valid: the run that deleted something is the run that is on
record as having done it.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: "
                           f"{(p.stderr or p.stdout).strip()[:160]}")
    return p.stdout


def _worktrees(repo: Path) -> list[dict]:
    out, current = [], {}
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                out.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        out.append(current)
    return out


def _tree_state(worktree: Path) -> str:
    """HEAD, tracked content and status of one worktree, as one digest."""
    h = hashlib.sha256()
    for args in (("rev-parse", "HEAD"), ("ls-files", "-s"),
                 ("status", "--porcelain", "--untracked-files=all")):
        try:
            h.update(_git(worktree, *args).encode())
        except RuntimeError as exc:
            h.update(f"<{exc}>".encode())
    return h.hexdigest()


def snapshot(run_worktree: Path | str) -> dict:
    """The protected state around one run, taken before its roles start."""
    wt = Path(run_worktree).resolve()
    branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    refs = {}
    for line in _git(wt, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        name, _, sha = line.partition(" ")
        refs[name] = sha
    others = {}
    for w in _worktrees(wt):
        path = Path(w["worktree"]).resolve()
        if path != wt:
            others[str(path)] = _tree_state(path)
    return {
        "run_worktree": str(wt),
        "run_branch": f"refs/heads/{branch}" if branch != "HEAD" else None,
        "run_head": _git(wt, "rev-parse", "HEAD").strip(),
        "tracked": sorted(_git(wt, "ls-files", "-z").split("\0")[:-1]),
        "refs": refs,
        "other_worktrees": others,
    }


def _archived(wt: Path, rel: str) -> bool:
    """Was a missing file parked by the house convention rather than lost?"""
    target = wt / rel
    parent, name = target.parent, target.name
    if parent.is_dir() and any(p.name.startswith(f"{name}.v") for p in parent.iterdir()):
        return True
    archive = wt / ".archiv"
    return archive.is_dir() and any(p.name.startswith(name) for p in archive.rglob("*"))


def verify(before: dict) -> list[str]:
    """Every way the run broke the rule, one line per path. Empty = held."""
    wt = Path(before["run_worktree"])
    problems: list[str] = []
    for rel in before["tracked"]:
        if not (wt / rel).exists() and not _archived(wt, rel):
            problems.append(f"deleted: {wt / rel} existed before the run and is "
                            "neither present nor archived")
    try:
        now_refs = {}
        for line in _git(wt, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
            name, _, sha = line.partition(" ")
            now_refs[name] = sha
    except RuntimeError as exc:
        return problems + [f"the repository cannot be read after the run: {exc}"]
    own = before.get("run_branch")
    for name, sha in before["refs"].items():
        if name not in now_refs:
            problems.append(f"ref removed: {name} (was {sha[:12]})")
            continue
        if now_refs[name] == sha:
            continue
        if name == own:
            ancestor = subprocess.run(
                ["git", "-C", str(wt), "merge-base", "--is-ancestor", sha, now_refs[name]],
                capture_output=True).returncode == 0
            if not ancestor:
                problems.append(f"history rewritten: {name} moved from {sha[:12]} "
                                f"to {now_refs[name][:12]}, which does not contain it")
        else:
            problems.append(f"ref moved: {name} {sha[:12]} -> {now_refs[name][:12]}; "
                            "only the run's own branch may move")
    for path, digest in before["other_worktrees"].items():
        if not Path(path).is_dir():
            problems.append(f"worktree removed: {path}")
        elif _tree_state(Path(path)) != digest:
            problems.append(f"unrelated worktree changed: {path}")
    return problems
