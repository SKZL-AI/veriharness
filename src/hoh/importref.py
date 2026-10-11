"""`hoh import`: bring an accepted candidate into the integration repository, prove it is the accepted bytes, and preview
the merge (A6 increment I6, plan row P5-38).

The orchestrator did this by hand after every accepted run: fetch the candidate from the run's development worktree,
run `tools/self_hosted_topology.py verify-import`, and ask `git merge-tree` against the integration tip and against the
other imports. This module is that sequence as one command with one receipt:

1. `--commit` is a **full id**; the run state names the accepted candidate (`last_accepted_candidate`: its
   `tree_digest`, its `candidate_id`, and the development worktree `repo_path` the commit is fetched from);
2. the commit is fetched **by its id** into the repository (`git fetch <worktree> <id>`, protocol v2, objects checked):
   no ref is written by the fetch, and `FETCH_HEAD` is not either;
3. **its tree must be the accepted candidate's tree** (the arithmetic of `self_hosted_topology.verify_import`: the tree
   of the commit equals `tree_digest`). A commit that is not that tree is refused and **no ref is created**;
4. the commit is published as **`refs/imports/<id>`**, created only if absent (an import ref never moves);
5. a **merge preview** (`git merge-tree --write-tree`, which writes tree objects and no ref, index or working tree) is made
   against the base (the full id typed with `--base`, or the repository's `HEAD` resolved here) and against each other
   import named with `--against`, or every other unmerged import with `--against-unmerged`; each preview says `clean`,
   `conflict` (with the conflicted paths) or `error`, and how the two commits relate;
6. a receipt records all of it, create-only.

It merges nothing and moves no branch. The comparison of the full suites of base and candidate, the check of the diff
against the spec's scope, and the hygiene of parked-copy directories are **not** part of this command (later slices).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .commitref import FULL_ID, CommitRefused, git, head_commit, require_full_id, require_new_ref, resolve_commit
from .durable_io import DurableWriteNotSynced, durable_create
from .mergegate import Outcome

SCHEMA = "hoh-import/1"
IMPORT_PREFIX = "refs/imports/"
MAX_AGAINST = 32
MAX_CONFLICTS = 50

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


class ImportRefused(Exception):
    """The import cannot be made: bad input, a tree that is not the accepted one, an existing name. No ref was
    created."""


def accepted_candidate(state_path: str) -> dict[str, str]:
    """The accepted candidate of a run state, read without any write: `candidate_id`, `tree_digest`, `repo_path`,
    `run_id`. The same facts `tools/self_hosted_topology.py verify-import` reads."""
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportRefused(f"--run-state {state_path!r} is unreadable: {exc}") from exc
    candidate = state.get("last_accepted_candidate") if isinstance(state, dict) else None
    if not isinstance(candidate, dict) or not candidate:
        raise ImportRefused(f"{state_path} records no accepted candidate: there are no accepted contents to bind to")
    digest = candidate.get("tree_digest")
    if not isinstance(digest, str) or FULL_ID.fullmatch(digest) is None:
        raise ImportRefused("the accepted candidate carries no tree_digest that is a full tree id")
    source = candidate.get("repo_path")
    if not isinstance(source, str) or not os.path.isabs(source) or not os.path.isdir(source):
        raise ImportRefused("the accepted candidate's development worktree is not an absolute path of a directory here "
                            "(nothing is fetched from a URL)")
    return {"candidate_id": str(candidate.get("candidate_id", "")), "tree_digest": digest,
            "repo_path": source, "run_id": str(state.get("run_id", ""))}


def verify_tree(repo: str, commit: str, candidate: dict[str, str]) -> str:
    """The tree of `commit`, which must be the accepted candidate's `tree_digest` (else `ImportRefused`)."""
    tree = git(repo, "rev-parse", "--verify", "--quiet", commit + "^{tree}", allow=(0, 1)).stdout.decode().strip()
    if tree != candidate["tree_digest"]:
        raise ImportRefused(
            f"{commit[:12]} carries tree {tree[:12] or '?'}, the accepted candidate {candidate['candidate_id']} "
            f"carries {candidate['tree_digest'][:12]}: the import did not preserve the accepted bytes")
    return tree


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, allow=(0, 1)).returncode == 0


def preview(repo: str, left: str, right: str) -> dict[str, Any]:
    """What merging `right` into `left` would do, asked without touching a ref, the index or a working tree."""
    try:
        relation = ("same" if left == right else "contained" if _is_ancestor(repo, right, left)
                    else "fast-forward" if _is_ancestor(repo, left, right) else "diverged")
        done = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", left, right,
                   allow=(0, 1, 128))
    except CommitRefused as exc:
        return {"left": left, "right": right, "relation": "unknown", "status": "error", "detail": str(exc)[:200]}
    fields = [f for f in done.stdout.decode("utf-8", "replace").split("\0")]
    if done.returncode == 0:
        return {"left": left, "right": right, "relation": relation, "status": "clean", "result_tree": fields[0]}
    if done.returncode == 1:
        names = sorted({f for f in fields[1:] if f})
        return {"left": left, "right": right, "relation": relation, "status": "conflict",
                "result_tree": fields[0], "conflicts": names[:MAX_CONFLICTS], "conflict_count": len(names)}
    return {"left": left, "right": right, "relation": relation, "status": "error",
            "detail": done.stderr.decode("utf-8", "replace").strip()[:200]}


def _imports(repo: str) -> dict[str, str]:
    """`{id: commit}` for every `refs/imports/*` that is a commit."""
    out = git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(objecttype)", IMPORT_PREFIX).stdout
    found = {}
    for row in out.decode().splitlines():
        ref, oid, kind = row.split("\t")
        if kind == "commit":
            found[ref[len(IMPORT_PREFIX):]] = oid
    return found


def make_import(*, repo: str, run_state: str, commit: str, name: str, base: str | None = None,
                against: Sequence[str] = (), against_unmerged: bool = False) -> dict[str, Any]:
    """Fetch, verify, publish the import ref and preview; the receipt body. Raises `ImportRefused` before any ref
    is created."""
    if _ID.fullmatch(name or "") is None or name.endswith(".lock") or ".." in name:
        raise ImportRefused(f"--id {name!r} is not an import id (letters, digits, '.', '_', '-'; 32 characters)")
    try:
        commit = require_full_id("--commit", commit)
        base = resolve_commit(repo, "--base", base) if base else head_commit(repo)
        candidate = accepted_candidate(run_state)
        ref = IMPORT_PREFIX + name
        require_new_ref(repo, ref)
        known = _imports(repo)
        peers: dict[str, str] = {}
        for other in against:
            if other == name or other not in known:
                raise ImportRefused(f"--against {other!r} is not another import of this repository")
            peers[other] = known[other]
        if against_unmerged:
            for other, oid in sorted(known.items()):
                if other != name and not _is_ancestor(repo, oid, base):
                    peers.setdefault(other, oid)
        if len(peers) > MAX_AGAINST:
            raise ImportRefused(f"{len(peers)} imports to compare against: more than {MAX_AGAINST}")

        present = git(repo, "cat-file", "-e", commit + "^{commit}", allow=(0, 1, 128)).returncode == 0
        if not present:
            git(repo, "-c", "protocol.version=2", "-c", "transfer.fsckObjects=true", "fetch", "--no-tags",
                "--no-write-fetch-head", "--quiet", candidate["repo_path"], commit)
        resolve_commit(repo, "--commit", commit)
        tree = verify_tree(repo, commit, candidate)
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.decode().split()[1:]
        git(repo, "update-ref", ref, commit, "")
        previews = [{"against": "base", **preview(repo, base, commit)}]
        previews += [{"against": f"import:{other}", **preview(repo, oid, commit)} for other, oid in sorted(peers.items())]
    except CommitRefused as exc:
        raise ImportRefused(str(exc)) from exc
    return {"schema": SCHEMA, "ref": ref, "commit": commit, "tree": tree, "parents": parents, "base": base,
            "fetched": not present, "run_id": candidate["run_id"], "candidate_id": candidate["candidate_id"],
            "tree_digest": candidate["tree_digest"], "source": candidate["repo_path"], "previews": previews,
            "all_clean": all(p["status"] == "clean" for p in previews),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def run_import(*, repo: str, run_state: str, commit: str, name: str, receipt: str, base: str | None = None,
               against: Sequence[str] = (), against_unmerged: bool = False) -> Outcome:
    """Exit 0: imported, every preview clean. Exit 1: imported, at least one preview is a conflict or an error (the
    receipt says which). Exit 2: refused, no ref, no receipt. Exit 3: the ref exists but the receipt could not be
    written."""
    try:
        target = Path(receipt)
        if os.path.lexists(target) or not target.parent.is_dir():
            raise ImportRefused(f"--receipt {receipt}: exists already, or its directory does not")
        result = make_import(repo=repo, run_state=run_state, commit=commit, name=name, base=base, against=against,
                             against_unmerged=against_unmerged)
    except ImportRefused as exc:
        return Outcome(2, "", f"import refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001 - a crash must not read as an import
        return Outcome(2, "", f"import refused: the command itself failed: {type(exc).__name__}: {exc}\n")
    lines = [f"IMPORT: {result['ref']} {result['commit']}", f"TREE: {result['tree']}", f"BASE: {result['base']}"]
    for item in result["previews"]:
        extra = f" ({item['conflict_count']} path(s))" if item["status"] == "conflict" else ""
        lines.append(f"PREVIEW {item['against']}: {item['status'].upper()} {item['relation']}{extra}")
    summary = "\n".join(lines) + "\n"
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    warning = ""
    try:
        try:
            durable_create(target, text)
        except DurableWriteNotSynced as exc:
            warning = f"import warning: the receipt is in place but its directory entry may not be durable ({exc.strerror})\n"
        if target.read_bytes() != text.encode("utf-8"):
            raise OSError("what was read back is not what was written")
    except (OSError, ValueError, RuntimeError) as exc:
        return Outcome(3, summary, f"import made, receipt NOT written ({exc}); the import stands at {result['ref']}\n")
    return Outcome(0 if result["all_clean"] else 1, summary + f"RECEIPT: {receipt}\n", warning)
