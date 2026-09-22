#!/usr/bin/env python3
"""attribution.py -- who actually did the work after O105.

The question this answers is uncomfortable on purpose: **how much of this
phase did the product execute, and how much did the session that is writing
about the product execute by hand?** A project that dogfoods its own control
plane and cannot say which commits went through it is making the same kind of
claim it spends its documentation refusing.

The categories are declared, in `dogfood/ATTRIBUTION.json`. That is a
judgement -- "this was bootstrap work that could not have gone through the
product" is not derivable from git. What is *not* a judgement, and is checked
here:

* **every commit after the anchor is covered by exactly one entry.** An entry
  names a commit range; the ranges must tile the history with no gap and no
  overlap. Nothing can be quietly left out of the denominator, which is the
  only way this number could be flattered.
* **no range ends at `HEAD`.** That was allowed, for a real reason -- an entry
  describing a merge cannot name its own sha. It is also how an entry claiming
  `VERIHARNESS_RUN` came to swallow four later commits the product had nothing
  to do with, and the tiling check could not see it, because the range still
  tiled. The self-reference is answered by a second commit that closes the
  range, which costs one commit and cannot grow.
* **an entry claiming `VERIHARNESS_RUN` must name a project state that
  exists** and whose node reached `MERGED`. Claiming the product did it is
  free; pointing at the state it left is not.
* **the counts are derived**, never written down. A hand-maintained ratio in a
  document is a ratio that drifts.

Usage:
    python3 tools/attribution.py [--repo PATH] [--json]

Exit 0 when the ledger tiles the history and every claim resolves.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent

#: How a unit of work got done. The distinction that matters is the first two:
#: everything else is either not development or not a choice.
CATEGORIES = (
    # Executed through the shipped control plane: a ProjectState node, a real
    # run, acceptance checks, receipts, a merge the product decided.
    "VERIHARNESS_RUN",
    # The main session wrote it. Legitimate for bootstrap, for an independent
    # audit of the product's own evidence, and for a security boundary the
    # product would have to be trusted to build for itself -- and a cost
    # wherever it is none of those.
    "MAIN_ORCHESTRATOR_DIRECT",
    # A deterministic tool ran and its output was committed. No agent involved.
    "DETERMINISTIC_TOOL",
    # A human decision or a human-authored change.
    "CAPTAIN",
    # Produced or verified by the external CI, not on this machine.
    "EXTERNAL_CI",
    # O179. The product developed it through a plain `hoh run` -- plan,
    # developer, independent QA, acceptance against receipts -- and the
    # **orchestrator** decided the merge after reviewing it. Its own category
    # because neither of the two it sits between is true: `VERIHARNESS_RUN`
    # says "a merge the product decided" and requires a ProjectState node
    # with a MERGED lifecycle, which a plain run does not have and did not
    # do; `MAIN_ORCHESTRATOR_DIRECT` says the main session wrote it, which is
    # false where a run did.
    #
    # Forcing either would have been a false entry in the one file whose
    # whole purpose is not to carry one. The evidence bar is not lowered by
    # the split -- it is raised: an entry here names the run state and the
    # accepted candidate, and the candidate's recorded tree digest has to
    # equal the git tree of one of the commits the entry claims. That binds
    # the claim to bytes, where VERIHARNESS_RUN binds it to a lifecycle
    # string.
    "VERIHARNESS_RUN_ORCHESTRATOR_MERGED",
)


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return p.stdout.strip() if p.returncode == 0 else ""


#: Histories other than the one this ledger's anchor names. An entry may
#: describe work that happened in a **different repository** -- the public
#: export is the case that exists: its commits are real, reviewed and merged,
#: and none of their shas is in the development history this ledger tiles.
#:
#: Such an entry is recorded, never resolved. Resolving it here would report
#: "names a commit that is not after the anchor", which is true and useless:
#: the sha was never supposed to be in this history. Counting it in
#: `commits_by_category` would be worse -- it would add commits to a
#: denominator this ledger does not cover, which is the quiet number
#: corruption O140 is about. So these entries are counted separately, under
#: the history they belong to.
#:
#: Data rather than a branch, for the same reason the export manifest keeps
#: its acknowledged references as data: a decision that can be read and
#: counted is a different thing from a special case inside a function.
FOREIGN_HISTORIES: dict[str, str] = {
    "public-export": (
        "the published export repository SKZL-AI/veriharness. Its history is "
        "an export of this one and its commits are its own; docs/READINESS.md "
        "and dogfood/external-ci/ carry what was measured there"
    ),
}


def commits_since(repo: Path, anchor: str) -> list[str]:
    """Every commit after the anchor, oldest first, merges included.

    Merges are included deliberately. Excluding them is the sort of choice
    that quietly changes a denominator, and this project has already had to
    correct one number for exactly that (`--no-merges` on an attribution
    table).
    """
    text = _git(repo, "rev-list", "--reverse", f"{anchor}..HEAD")
    return text.splitlines() if text else []


def _shas(repo: Path, entry: dict, problems: list[str]) -> list[str]:
    """The commits an entry claims: an explicit list, or a resolved range.

    A range exists for one reason. The entry that records *this* ledger cannot
    name its own commit -- the sha does not exist until the commit is made,
    and adding it afterwards needs another commit, which is then also
    unattributed. The project has met this shape before (O97: the commit that
    writes an attribution table changes it) and solved it the same way, by
    naming a boundary instead of a value.

    So the last entry may say `{"from": "<sha>", "to": "HEAD"}` and be correct
    at every point in time. It is not a loophole: the range is declared, and
    everything inside it is still claimed by exactly one entry.
    """
    if "commits" in entry:
        return list(entry["commits"])
    r = entry.get("range")
    if not r:
        problems.append(f"{entry['id']}: names neither commits nor a range")
        return []
    text = _git(repo, "rev-list", "--reverse", f"{r['from']}..{r['to']}")
    if not text:
        problems.append(
            f"{entry['id']}: the range {r['from'][:12]}..{r['to']} is empty"
        )
        return []
    return text.splitlines()


def _run_evidence(repo: Path, entry: dict) -> list[str]:
    """What an entry claiming a plain run has to point at.

    Claiming it is free; pointing at what it left is not -- the same sentence
    `VERIHARNESS_RUN` is held to, asked of the artefact a plain run actually
    produces. Four things, and the last is the one that matters:

    * a run state exists at the named path;
    * it records an accepted candidate;
    * that candidate's id is the one the entry names -- so an entry cannot
      point at a run and mean a different iteration of it;
    * and the candidate's recorded `tree_digest` equals the git tree of one
      of the commits this entry claims. That is the binding to bytes. A
      lifecycle string says a node was merged; this says *these* contents
      were the ones accepted.

    The digest is compared against every claimed commit rather than the first,
    because an entry legitimately names the run's own commits and the merge
    that brought them in, and only one of those carries the accepted tree.
    """
    path = entry.get("run_state", "")
    if not path:
        return [f"{entry['id']}: claims a run produced it and names no run "
                "state. Pointing at what the run left is the whole difference "
                "between this and a claim"]
    full_ = repo / path
    if not full_.is_file():
        return [f"{entry['id']}: names a run state that is not there: {path}"]
    try:
        state = json.loads(full_.read_text())
    except (OSError, ValueError) as exc:
        return [f"{entry['id']}: {path} unreadable: {exc}"]

    candidate = state.get("last_accepted_candidate") or {}
    if not candidate:
        return [f"{entry['id']}: {path} records no accepted candidate, so "
                "nothing in it was accepted"]
    demands = entry.get("candidate_id", "")
    if not demands:
        return [f"{entry['id']}: names a run state and no candidate_id"]
    if candidate.get("candidate_id") != demands:
        return [f"{entry['id']}: names candidate {demands}, but {path} "
                f"records {candidate.get('candidate_id') or 'none'} as the "
                "accepted one"]

    digest = candidate.get("tree_digest")
    if not digest:
        return [f"{entry['id']}: the accepted candidate in {path} carries no "
                "tree_digest, so the claim cannot be bound to any contents"]
    trees_ = set()
    for sha in entry.get("commits") or []:
        tree_ = _git(repo, "rev-parse", f"{sha}^{{tree}}").strip()
        if tree_:
            trees_.add(tree_)
    if digest not in trees_:
        return [f"{entry['id']}: the accepted candidate's tree {digest[:12]} "
                "is not the tree of any commit this entry claims. The entry "
                "points at a run whose accepted contents are somewhere else"]
    return []


#: Files whose sole change cannot be development work, because each is
#: generated from the tree it describes and contains nothing else. O186 added
#: the second one: the succession capsule is written by a tool, its content is
#: derived, and it is re-written after every commit -- so without this rule
#: each capsule commit created the next unattributed commit, forever.
BOOKKEEPING_PATHS = frozenset({
    "dogfood/ATTRIBUTION.json",
    "dogfood/succession/SUCCESSION.json",
})


def _only_the_ledger(repo: Path, sha: str) -> bool:
    """Did this commit touch exactly one bookkeeping file and nothing else?

    A commit that changed only this ledger is the ledger recording earlier
    commits. It cannot itself be unaccounted development work, because it
    changed no code, no document and no evidence -- which is a stronger
    statement than "its message says it is bookkeeping", and is why this asks
    git rather than reading the message. The same argument, and only that
    argument, extends the rule to the succession capsule.

    **One** of them, not a subset: a commit carrying both is a commit doing
    two things at once, and the point of the rule is that the diff leaves no
    room for a third.

    A merge commit is deliberately not special-cased: `git show --name-only`
    on a merge lists nothing, so this returns False and the commit stays a gap
    that an entry has to claim.
    """
    text = _git(repo, "show", "--name-only", "--format=", sha)
    paths = {z.strip() for z in text.splitlines() if z.strip()}
    return len(paths) == 1 and paths <= BOOKKEEPING_PATHS


def _anchor_exists(repo: Path, anchor: str) -> bool:
    """Does this repository contain the anchor commit at all?

    The ledger describes the history of the repository that produced it. A
    published export is a different repository with a different history, so
    none of the shas exist there -- which is not a defect in the ledger, it is
    the export being an export. The exact-head CI found this the first time
    `dogfood/ATTRIBUTION.json` was published: sixty entries reported as naming
    commits that are "not a commit after the anchor", when what was true is
    that the anchor is not in that clone either.
    """
    # Two different absences, and only one of them authorises a skip. A
    # repository that answers "no such object" demonstrably does not contain
    # the anchor. A `git` that cannot run at all answers nothing, and reading
    # its silence as "different history" would be the fail-open shape this
    # project has been removing: the check would go quiet exactly when it
    # cannot see.
    attempt_ = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True)
    if attempt_.returncode != 0 or attempt_.stdout.strip() != "true":
        raise RuntimeError(
            f"{repo} is not a git work tree, or git could not answer: "
            f"{(attempt_.stderr or attempt_.stdout).strip()[:120]}. Whether this "
            f"repository contains the ledger's anchor is then unknown, and "
            f"unknown is not an environment gap.")
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{anchor}^{{commit}}"],
                       capture_output=True, text=True)
    return p.returncode == 0


def check(repo: Path, ledger: dict) -> dict:
    anchor = ledger["anchor"]
    if not _anchor_exists(repo, anchor):
        # O171: this used to return half a report -- no `commits_by_category`,
        # no `sentence`, no `ok` -- and the CLI read those keys unconditionally,
        # so running this in an export clone ended in `KeyError:
        # commits_by_category` instead of the environment gap the branch was
        # written to state. A gate that crashes where it means to say "I
        # cannot check this here" reports nothing at all, which is the one
        # outcome this project treats as worse than a red result.
        return {
            "anchor": anchor,
            "commits_after_anchor": 0,
            "commits_by_category": {},
            "categories": {},
            "development_nodes": 0,
            "nodes_through_the_product": 0,
            "nodes_developed_by_the_product_merged_by_the_orchestrator": 0,
            "through_the_product": 0,
            "other_histories": {},
            "sentence": "nothing was verified here",
            "problems": [],
            "ok": False,
            "environment_gap": (
                f"this repository does not contain the anchor commit "
                f"{anchor[:12]}, so it is not the history this ledger "
                f"describes. Published exports carry the ledger as a record "
                f"and cannot verify it; the repository that produced it can."
            ),
        }
    all_ = commits_since(repo, anchor)
    known = set(all_)
    seen_: dict[str, str] = {}
    problems: list[str] = []

    foreign: dict[str, list[str]] = {}
    for entry in ledger["entries"]:
        origin = entry.get("history")
        if origin is not None:
            # Recorded, not resolved. Validated on what can be checked here:
            # a known history, a known category, an explicit commit list (a
            # range cannot be resolved in a history this repository does not
            # have), and a reason.
            if origin not in FOREIGN_HISTORIES:
                problems.append(
                    f"{entry['id']}: unknown history {origin!r}; known are "
                    + ", ".join(sorted(FOREIGN_HISTORIES))
                )
            if entry["category"] not in CATEGORIES:
                problems.append(
                    f"{entry['id']}: unknown category {entry['category']!r}"
                )
            if entry.get("range"):
                problems.append(
                    f"{entry['id']}: describes {origin} and names a range. "
                    "A range is resolved with git rev-list, which cannot run "
                    "against a history this repository does not have; name the "
                    "commits explicitly"
                )
            if not entry.get("commits"):
                problems.append(
                    f"{entry['id']}: describes {origin} and names no commits"
                )
            if entry["category"] == "VERIHARNESS_RUN":
                problems.append(
                    f"{entry['id']}: claims the product executed it in "
                    f"{origin}, whose run state this repository does not "
                    "carry, so the claim cannot be checked where it is made"
                )
            foreign.setdefault(origin, []).extend(entry.get("commits") or [])
            continue
        region = entry.get("range") or {}
        if str(region.get("to", "")).upper() in ("HEAD", "@"):
            problems.append(
                f"{entry['id']}: its range ends at HEAD. An open range grows "
                "with the history and silently claims work the entry knows "
                "nothing about; close it and describe the rest in a second "
                "entry"
            )
        if entry["category"] not in CATEGORIES:
            problems.append(
                f"{entry['id']}: unknown category {entry['category']!r}"
            )
        for sha in _shas(repo, entry, problems):
            if sha not in known:
                problems.append(
                    f"{entry['id']}: names {sha[:12]}, which is not a commit "
                    f"after {anchor[:12]}"
                )
            elif sha in seen_:
                problems.append(
                    f"{sha[:12]} is claimed by both {seen_[sha]} and "
                    f"{entry['id']}"
                )
            else:
                seen_[sha] = entry["id"]
        if entry["category"] == "VERIHARNESS_RUN":
            state = entry.get("project_state", "")
            path = (repo / state) if state else None
            if not state:
                problems.append(
                    f"{entry['id']}: claims the product executed it and names "
                    "no project state. Claiming it is free; pointing at what it "
                    "left is not"
                )
            elif not path.exists():
                problems.append(
                    f"{entry['id']}: names a project state that is not there: "
                    f"{state}"
                )
            else:
                try:
                    st = json.loads(path.read_text())
                    nodes = {n["id"]: n.get("lifecycle") for n in st.get("nodes", [])}
                    demands = entry.get("node_id", "")
                    if demands and nodes.get(demands) != "MERGED":
                        problems.append(
                            f"{entry['id']}: node {demands} in {state} is "
                            f"{nodes.get(demands) or 'absent'}, not MERGED"
                        )
                except (OSError, ValueError) as exc:
                    problems.append(f"{entry['id']}: {state} unreadable: {exc}")
        if entry["category"] == "VERIHARNESS_RUN_ORCHESTRATOR_MERGED":
            problems.extend(_run_evidence(repo, entry))

    unattributed = [c for c in all_ if c not in seen_]
    # `HEAD` itself may be unattributed, and exactly it: a commit cannot name
    # its own sha, so the entry describing a commit is written in the next one.
    # That is the price of closing the ranges, and it is one commit, bounded.
    # Two unattributed commits is a gap, which is what this check is for.
    #
    # O169 widened that by one shape, because with only the tip tolerated this
    # check has **no reachable green state** once a release needs both an
    # attribution commit and a commit after it. The attribution commit is the
    # tip and is tolerated; the next commit displaces it and it becomes a gap;
    # writing an entry for it needs another commit, which is then the tip, and
    # so on. So a commit that touches *only* `dogfood/ATTRIBUTION.json` is
    # tolerated as well, wherever it sits in the history. It is the ledger
    # writing itself, by construction: a commit that changed no code, no
    # document and no evidence cannot be work this ledger is failing to
    # account for, and where it sits does not change that. Position was tried
    # first -- only a trailing *run* of such commits -- and it was wrong for a
    # reason worth keeping: as soon as one attributed commit lands after the
    # ledger-only one, the run ends and a content-free commit becomes a gap
    # again. The property that makes it safe is what it touched, not where it
    # is. Anything that touched more than the ledger is still a gap, at the end
    # or in the middle.
    unattributed = [
        c for c in unattributed
        if c != all_[-1] and not _only_the_ledger(repo, c)
    ]
    if unattributed:
        problems.append(
            f"{len(unattributed)} commit(s) after the anchor belong to no "
            "entry: " + ", ".join(c[:12] for c in unattributed[:8])
        )

    by_category: dict[str, int] = {k: 0 for k in CATEGORIES}
    nodes_total = 0
    nodes_by_product = 0
    nodes_developed = 0
    foreign_by_category: dict[str, dict[str, int]] = {}
    for entry in ledger["entries"]:
        origin = entry.get("history")
        if origin is not None:
            # Never in the internal denominator. The ratio this tool reports is
            # about the history the anchor names; adding commits from another
            # repository to it would change a number without changing anything
            # it measures.
            k = foreign_by_category.setdefault(origin, {})
            k[entry["category"]] = k.get(entry["category"], 0) + len(
                entry.get("commits") or [])
            continue
        n = len(_shas(repo, entry, []))
        by_category[entry["category"]] = by_category.get(
            entry["category"], 0
        ) + n
        if entry.get("is_development_node"):
            nodes_total += 1
            if entry["category"] == "VERIHARNESS_RUN":
                nodes_by_product += 1
            elif entry["category"] == "VERIHARNESS_RUN_ORCHESTRATOR_MERGED":
                # Its own count, reported on its own line. Folding it into the
                # numerator would claim the product decided a merge it did not
                # decide; leaving it only in the denominator would deny that
                # the product developed the node at all. Both are false, so
                # neither number moves and a third one says what happened.
                nodes_developed += 1

    return {
        "anchor": anchor,
        "commits_after_anchor": len(all_),
        "commits_by_category": {k: v for k, v in by_category.items() if v},
        "development_nodes": nodes_total,
        "nodes_through_the_product": nodes_by_product,
        "nodes_developed_by_the_product_merged_by_the_orchestrator":
            nodes_developed,
        "sentence": (
            f"{nodes_by_product} of {nodes_total} post-anchor development nodes "
            "were executed through the shipped control plane"
        ),
        "other_histories": {
            h: {"commits_by_category": k,
                "what": FOREIGN_HISTORIES.get(h, "unknown history")}
            for h, k in sorted(foreign_by_category.items())
        },
        "problems": problems,
        "ok": not problems,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(HOH), type=Path)
    ap.add_argument("--ledger", default=str(HOH / "dogfood/ATTRIBUTION.json"),
                    type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.ledger.exists():
        print(f"no ledger at {args.ledger}", file=sys.stderr)
        return 2
    ledger = json.loads(args.ledger.read_text())
    report = check(args.repo.resolve(), ledger)
    if args.json:
        print(json.dumps(report, indent=2))
        # One exit semantics for both output formats: 3 an environment gap,
        # 1 a finding, 0 a verified pass. The first version of this line
        # returned 0 for the gap, so `--json` reported as success exactly the
        # state the text mode had been given its own code to avoid -- the
        # repair of O171 in one format and not the other.
        if report.get("environment_gap"):
            return 3
        return 0 if report["ok"] else 1
    if report.get("environment_gap"):
        # Not a pass and not a violation: a third state, printed as itself and
        # given its own exit code so that `attribution.py && echo ok` cannot
        # print ok for a run that verified nothing (O171).
        print("ENVIRONMENT_GAP: nothing was verified here")
        print(f"  {report['environment_gap']}")
        return 3
    else:
        print(f"anchor {report['anchor'][:12]}, "
              f"{report['commits_after_anchor']} commit(s) after it")
        for k, v in sorted(report["commits_by_category"].items()):
            print(f"  {k:<26s} {v:>3d} commit(s)")
        print()
        print("  " + report["sentence"])
        n = report.get(
            "nodes_developed_by_the_product_merged_by_the_orchestrator", 0)
        if n:
            print(f"  {n} further node(s) the product developed and the "
                  "orchestrator merged after review, counted in neither "
                  "number above")
        for h, d in (report.get("other_histories") or {}).items():
            total_ = sum(d["commits_by_category"].values())
            print(f"  recorded from {h}: {total_} commit(s), "
                  "not counted in the ratio above")
        for p in report["problems"]:
            print(f"  PROBLEM: {p}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
