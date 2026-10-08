#!/usr/bin/env python3
"""Prove the topology of a self-hosted run before it costs a dispatch.

O177 falsified one arrangement for good: a VeriHarness run whose development
worktree belongs to the **same repository the orchestrator is working in**. A
worktree shares its repository's history, so an ordinary commit in the main tree
moves history under the run, and the capability witness -- correctly -- refuses
to distinguish that from the agent having done it. Both runs that day ended
BLOCKED after two to six paid dispatches. The witness was right; the topology
was wrong.

The repair recorded then was prose: *use isolated clones, not worktrees.* This
file is that rule turned into a check, because the orchestrator that wrote the
prose is the same one that later proposed going back to worktrees to reach a
stronger attribution category. A rule kept only in a document is a rule kept by
memory.

**The one honest reason someone would want the worktree topology** is byte-level
attribution: `VERIHARNESS_RUN` binds a claim to contents by requiring the
accepted candidate's `tree_digest` to equal the git tree of a claimed commit, and
a run in a foreign clone looks unreachable from the integration repository. It is
not. A git tree object is content-addressed and carries no history: contents that
are byte-identical hash to the same tree in every repository. So the candidate's
tree can be reproduced exactly in the integration repository --

    git commit-tree <candidate tree> -p <pin> -m "..."

-- and the existing binding holds with the isolation intact. `verify-import`
below is that arithmetic, checked rather than asserted. Isolation is never
weakened to satisfy an instrument.

Usage:
    self_hosted_topology.py check --repo RUN_WORKTREE --pin COMMIT
                                 [--integration PATH] [--runs PATH]
                                 [--exclusive REASON]
    self_hosted_topology.py verify-import --run-state PATH --commit SHA
                                 [--integration PATH]

Exit codes: 0 sound, 1 refused, 3 the question could not be asked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

#: Run conditions that still hold history. A finished run cannot have its
#: witnesses invalidated any more, so it does not make a second live run on the
#: same repository unsafe.
LIVE_CONDITIONS = frozenset({"ACTIVE", "BLOCKED", "PARKED"})


def _git(repo: Path, *args: str) -> str:
    """git in `repo`, stdout stripped, empty string when the call fails.

    Failure is not raised because every caller here is asking a question whose
    "no answer" is itself an answer -- a path that is not a repository, a commit
    that is not present. Those become refusals with a message, not tracebacks.
    """
    try:
        done = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def repository_identity(path: Path) -> str:
    """Which *repository* a checkout belongs to, not which checkout it is.

    This is the whole discrimination the check rests on. `--git-dir` of a linked
    worktree points inside the parent's `.git/worktrees/<name>`, which differs
    per worktree and would make every worktree look like its own repository --
    exactly the confusion O177 punished. `--git-common-dir` is shared by the main
    worktree and all its linked worktrees, so two checkouts that can move each
    other's history resolve to the same string here.
    """
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return ""
    try:
        return str(Path(common).resolve())
    except OSError:
        return common


def main_worktree(path: Path) -> Path | None:
    """The main worktree of `path`'s repository, or None if it cannot be read."""
    listing = _git(path, "worktree", "list", "--porcelain")
    for line in listing.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):])
    return None


def live_runs_sharing(runs_root: Path, identity: str, *, own: Path) -> list[str]:
    """Other live runs whose project repository is the same repository.

    Two runs on one history invalidate each other for the same reason the
    orchestrator did in O177: whichever commits first moves the ground under the
    other. This looks at what the run states actually record, so a run that has
    finished (ACCEPTED, STOPPED) does not count -- it has no witness left to
    break.

    `own` is the worktree being checked, excluded by *checkout path* rather than
    by state path: the run under examination has a state file of its own as soon
    as `hoh start` has written one, and a check that flagged the run for being
    itself would refuse every sound topology there is.
    """
    found: list[str] = []
    if not runs_root.is_dir():
        return found
    try:
        mine = own.resolve()
    except OSError:
        mine = own
    for state_path in sorted(runs_root.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("condition") not in LIVE_CONDITIONS:
            continue
        repo = state.get("repo_path") or state.get("repo") or ""
        if not repo:
            continue
        try:
            theirs = Path(repo).resolve()
        except OSError:
            theirs = Path(repo)
        if theirs == mine:
            continue
        if repository_identity(Path(repo)) == identity:
            found.append(f"{state.get('run_id') or state_path.parent.name} "
                         f"({state.get('condition')}) in {repo}")
    return found


def check(repo: Path, pin: str, *, integration: Path, runs: Path | None,
          exclusive: str = "") -> list[str]:
    """Why this run's topology is not the isolated one, or `[]` when it is.

    The order matters: the identity comparison comes first because it is the
    falsified arrangement itself, and a run in that arrangement is refused
    whatever else is true of it.
    """
    problems: list[str] = []

    if not repo.is_dir():
        return [f"the run worktree is not there: {repo}"]
    identity = repository_identity(repo)
    if not identity:
        return [f"{repo} is not a git checkout, so its repository cannot be "
                "identified and the topology cannot be proven"]
    integration_identity = repository_identity(integration)
    if not integration_identity:
        return [f"{integration} is not a git checkout, so there is nothing to "
                "compare the run's repository against"]

    if identity == integration_identity:
        if not exclusive:
            problems.append(
                f"the run worktree {repo} belongs to the same repository as the "
                f"integration tree {integration}. This is the topology O177 "
                "falsified: a commit in the integration tree moves history under "
                "the run, and the capability witness cannot tell that apart from "
                "the agent doing it. Use a dedicated clone pinned to the source "
                "commit. A byte-identical import binds the claim to contents "
                "anyway, because a git tree is content-addressed -- isolation "
                "does not have to be weakened for attribution")
        else:
            # The narrow exception the plan allows: explicitly serialized and
            # exclusive, so no concurrent history movement is possible. It is
            # still refused when anything shows the integration tree is live.
            if _git(integration, "status", "--porcelain"):
                problems.append(
                    "the exclusive-worktree exception was claimed, but the "
                    "integration tree has uncommitted changes -- which is what a "
                    "tree being worked in looks like. The exception requires that "
                    "no concurrent history movement is possible")

    main = main_worktree(repo)
    if main is None:
        problems.append(f"the main worktree of {repo}'s repository could not be "
                        "read, so it cannot be shown to be pinned")
    else:
        head = _git(main, "rev-parse", "HEAD")
        if not head:
            problems.append(f"{main} has no readable HEAD")
        elif not pin:
            problems.append("no pin was given, so 'pinned to the exact source "
                            "commit' is not a checkable statement")
        elif head != pin:
            problems.append(
                f"the run's repository is at {head[:12]}, not at the pin "
                f"{pin[:12]}. A candidate produced over a different base cannot "
                "have its tree reproduced on the pin, so the import would not "
                "bind")
        if _git(main, "status", "--porcelain") and main.resolve() != integration.resolve():
            problems.append(
                f"the run's own repository {main} is dirty before the run "
                "starts. Then the candidate's tree carries contents nobody "
                "authored in this run, and the attribution claim would cover them")

    if pin and not _git(integration, "cat-file", "-t", pin):
        problems.append(
            f"the pin {pin[:12]} is not an object in the integration repository, "
            "so there is nothing to parent the import commit on")

    if runs is not None:
        shared = live_runs_sharing(runs, identity, own=repo)
        if shared:
            problems.append(
                "another live run holds the same repository, so whichever "
                "commits first moves the ground under the other: "
                + "; ".join(shared))

    return problems


def verify_import(run_state: Path, commit: str, *, integration: Path) -> list[str]:
    """Does `commit` carry exactly the accepted candidate's contents?

    The same arithmetic `tools/attribution.py` performs when it validates a
    `VERIHARNESS_RUN` entry, asked *before* the entry is written -- so a merge
    that does not actually preserve the product's bytes is caught while it is
    still cheap to fix, instead of becoming a false claim in the one file whose
    purpose is not to carry one.
    """
    if not run_state.is_file():
        return [f"no run state at {run_state}"]
    try:
        state = json.loads(run_state.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{run_state} unreadable: {exc}"]
    candidate = state.get("last_accepted_candidate") or {}
    if not candidate:
        return [f"{run_state} records no accepted candidate, so there are no "
                "accepted contents to bind to"]
    digest = candidate.get("tree_digest") or ""
    if not digest:
        return [f"the accepted candidate in {run_state} carries no tree_digest"]
    tree = _git(integration, "rev-parse", f"{commit}^{{tree}}")
    if not tree:
        return [f"{commit} is not a commit in {integration}"]
    if tree != digest:
        return [f"{commit} carries tree {tree[:12]}, the accepted candidate "
                f"{candidate.get('candidate_id')} carries {digest[:12]}. The "
                "import did not preserve the product's bytes, so no claim may "
                "be bound to this commit"]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="prove the run's topology is the isolated one")
    c.add_argument("--repo", type=Path, required=True,
                   help="the run's development worktree")
    c.add_argument("--pin", default="",
                   help="the source commit the run's repository must sit on")
    c.add_argument("--integration", type=Path, default=HOH,
                   help="the tree the result will be integrated into")
    c.add_argument("--runs", type=Path, default=None,
                   help="a runs root to check for other live runs on this history")
    c.add_argument("--exclusive", default="",
                   help="claim the serialized-exclusive worktree exception, with "
                        "the reason it holds")
    c.add_argument("--json", action="store_true")

    v = sub.add_parser("verify-import",
                       help="prove a commit carries the accepted candidate's bytes")
    v.add_argument("--run-state", type=Path, required=True)
    v.add_argument("--commit", required=True)
    v.add_argument("--integration", type=Path, default=HOH)
    v.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.command == "check":
        problems = check(args.repo, args.pin, integration=args.integration,
                         runs=args.runs, exclusive=args.exclusive)
        what = f"topology of {args.repo}"
    else:
        problems = verify_import(args.run_state, args.commit,
                                 integration=args.integration)
        what = f"import binding of {args.commit[:12]}"

    if args.json:
        print(json.dumps({"what": what,
                          "status": "FAIL" if problems else "PASS",
                          "problems": problems}, indent=2))
    elif problems:
        print(f"{what}: refused ({len(problems)} problem(s))", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
    else:
        print(f"{what}: sound")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
