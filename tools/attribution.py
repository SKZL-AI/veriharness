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

HIER = Path(__file__).resolve().parent
HOH = HIER.parent

#: How a unit of work got done. The distinction that matters is the first two:
#: everything else is either not development or not a choice.
KATEGORIEN = (
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
FREMDE_HISTORIEN: dict[str, str] = {
    "public-export": (
        "the published export repository SKZL-AI/veriharness. Its history is "
        "an export of this one and its commits are its own; docs/READINESS.md "
        "and dogfood/external-ci/ carry what was measured there"
    ),
}


def commits_since(repo: Path, anker: str) -> list[str]:
    """Every commit after the anchor, oldest first, merges included.

    Merges are included deliberately. Excluding them is the sort of choice
    that quietly changes a denominator, and this project has already had to
    correct one number for exactly that (`--no-merges` on an attribution
    table).
    """
    text = _git(repo, "rev-list", "--reverse", f"{anker}..HEAD")
    return text.splitlines() if text else []


def _shas(repo: Path, eintrag: dict, probleme: list[str]) -> list[str]:
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
    if "commits" in eintrag:
        return list(eintrag["commits"])
    r = eintrag.get("range")
    if not r:
        probleme.append(f"{eintrag['id']}: names neither commits nor a range")
        return []
    text = _git(repo, "rev-list", "--reverse", f"{r['from']}..{r['to']}")
    if not text:
        probleme.append(
            f"{eintrag['id']}: the range {r['from'][:12]}..{r['to']} is empty"
        )
        return []
    return text.splitlines()


def _nur_das_ledger(repo: Path, sha: str) -> bool:
    """Did this commit touch `dogfood/ATTRIBUTION.json` and nothing else?

    A commit that changed only this ledger is the ledger recording earlier
    commits. It cannot itself be unaccounted development work, because it
    changed no code, no document and no evidence -- which is a stronger
    statement than "its message says it is bookkeeping", and is why this asks
    git rather than reading the message.

    A merge commit is deliberately not special-cased: `git show --name-only`
    on a merge lists nothing, so this returns False and the commit stays a gap
    that an entry has to claim.
    """
    text = _git(repo, "show", "--name-only", "--format=", sha)
    pfade = {z.strip() for z in text.splitlines() if z.strip()}
    return pfade == {"dogfood/ATTRIBUTION.json"}


def _anker_vorhanden(repo: Path, anker: str) -> bool:
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
    versuch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True)
    if versuch.returncode != 0 or versuch.stdout.strip() != "true":
        raise RuntimeError(
            f"{repo} is not a git work tree, or git could not answer: "
            f"{(versuch.stderr or versuch.stdout).strip()[:120]}. Whether this "
            f"repository contains the ledger's anchor is then unknown, and "
            f"unknown is not an environment gap.")
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{anker}^{{commit}}"],
                       capture_output=True, text=True)
    return p.returncode == 0


def pruefe(repo: Path, ledger: dict) -> dict:
    anker = ledger["anchor"]
    if not _anker_vorhanden(repo, anker):
        # O171: this used to return half a report -- no `commits_by_category`,
        # no `sentence`, no `ok` -- and the CLI read those keys unconditionally,
        # so running this in an export clone ended in `KeyError:
        # commits_by_category` instead of the environment gap the branch was
        # written to state. A gate that crashes where it means to say "I
        # cannot check this here" reports nothing at all, which is the one
        # outcome this project treats as worse than a red result.
        return {
            "anchor": anker,
            "commits_after_anchor": 0,
            "commits_by_category": {},
            "categories": {},
            "development_nodes": 0,
            "nodes_through_the_product": 0,
            "through_the_product": 0,
            "other_histories": {},
            "sentence": "nothing was verified here",
            "problems": [],
            "ok": False,
            "environment_gap": (
                f"this repository does not contain the anchor commit "
                f"{anker[:12]}, so it is not the history this ledger "
                f"describes. Published exports carry the ledger as a record "
                f"and cannot verify it; the repository that produced it can."
            ),
        }
    alle = commits_since(repo, anker)
    bekannt = set(alle)
    gesehen: dict[str, str] = {}
    probleme: list[str] = []

    fremd: dict[str, list[str]] = {}
    for eintrag in ledger["entries"]:
        herkunft = eintrag.get("history")
        if herkunft is not None:
            # Recorded, not resolved. Validated on what can be checked here:
            # a known history, a known category, an explicit commit list (a
            # range cannot be resolved in a history this repository does not
            # have), and a reason.
            if herkunft not in FREMDE_HISTORIEN:
                probleme.append(
                    f"{eintrag['id']}: unknown history {herkunft!r}; known are "
                    + ", ".join(sorted(FREMDE_HISTORIEN))
                )
            if eintrag["category"] not in KATEGORIEN:
                probleme.append(
                    f"{eintrag['id']}: unknown category {eintrag['category']!r}"
                )
            if eintrag.get("range"):
                probleme.append(
                    f"{eintrag['id']}: describes {herkunft} and names a range. "
                    "A range is resolved with git rev-list, which cannot run "
                    "against a history this repository does not have; name the "
                    "commits explicitly"
                )
            if not eintrag.get("commits"):
                probleme.append(
                    f"{eintrag['id']}: describes {herkunft} and names no commits"
                )
            if eintrag["category"] == "VERIHARNESS_RUN":
                probleme.append(
                    f"{eintrag['id']}: claims the product executed it in "
                    f"{herkunft}, whose run state this repository does not "
                    "carry, so the claim cannot be checked where it is made"
                )
            fremd.setdefault(herkunft, []).extend(eintrag.get("commits") or [])
            continue
        bereich = eintrag.get("range") or {}
        if str(bereich.get("to", "")).upper() in ("HEAD", "@"):
            probleme.append(
                f"{eintrag['id']}: its range ends at HEAD. An open range grows "
                "with the history and silently claims work the entry knows "
                "nothing about; close it and describe the rest in a second "
                "entry"
            )
        if eintrag["category"] not in KATEGORIEN:
            probleme.append(
                f"{eintrag['id']}: unknown category {eintrag['category']!r}"
            )
        for sha in _shas(repo, eintrag, probleme):
            if sha not in bekannt:
                probleme.append(
                    f"{eintrag['id']}: names {sha[:12]}, which is not a commit "
                    f"after {anker[:12]}"
                )
            elif sha in gesehen:
                probleme.append(
                    f"{sha[:12]} is claimed by both {gesehen[sha]} and "
                    f"{eintrag['id']}"
                )
            else:
                gesehen[sha] = eintrag["id"]
        if eintrag["category"] == "VERIHARNESS_RUN":
            zustand = eintrag.get("project_state", "")
            pfad = (repo / zustand) if zustand else None
            if not zustand:
                probleme.append(
                    f"{eintrag['id']}: claims the product executed it and names "
                    "no project state. Claiming it is free; pointing at what it "
                    "left is not"
                )
            elif not pfad.exists():
                probleme.append(
                    f"{eintrag['id']}: names a project state that is not there: "
                    f"{zustand}"
                )
            else:
                try:
                    st = json.loads(pfad.read_text())
                    knoten = {n["id"]: n.get("lifecycle") for n in st.get("nodes", [])}
                    verlangt = eintrag.get("node_id", "")
                    if verlangt and knoten.get(verlangt) != "MERGED":
                        probleme.append(
                            f"{eintrag['id']}: node {verlangt} in {zustand} is "
                            f"{knoten.get(verlangt) or 'absent'}, not MERGED"
                        )
                except (OSError, ValueError) as exc:
                    probleme.append(f"{eintrag['id']}: {zustand} unreadable: {exc}")

    nicht_zugeordnet = [c for c in alle if c not in gesehen]
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
    nicht_zugeordnet = [
        c for c in nicht_zugeordnet
        if c != alle[-1] and not _nur_das_ledger(repo, c)
    ]
    if nicht_zugeordnet:
        probleme.append(
            f"{len(nicht_zugeordnet)} commit(s) after the anchor belong to no "
            "entry: " + ", ".join(c[:12] for c in nicht_zugeordnet[:8])
        )

    nach_kategorie: dict[str, int] = {k: 0 for k in KATEGORIEN}
    knoten_gesamt = 0
    knoten_produkt = 0
    fremd_nach_kategorie: dict[str, dict[str, int]] = {}
    for eintrag in ledger["entries"]:
        herkunft = eintrag.get("history")
        if herkunft is not None:
            # Never in the internal denominator. The ratio this tool reports is
            # about the history the anchor names; adding commits from another
            # repository to it would change a number without changing anything
            # it measures.
            k = fremd_nach_kategorie.setdefault(herkunft, {})
            k[eintrag["category"]] = k.get(eintrag["category"], 0) + len(
                eintrag.get("commits") or [])
            continue
        n = len(_shas(repo, eintrag, []))
        nach_kategorie[eintrag["category"]] = nach_kategorie.get(
            eintrag["category"], 0
        ) + n
        if eintrag.get("is_development_node"):
            knoten_gesamt += 1
            if eintrag["category"] == "VERIHARNESS_RUN":
                knoten_produkt += 1

    return {
        "anchor": anker,
        "commits_after_anchor": len(alle),
        "commits_by_category": {k: v for k, v in nach_kategorie.items() if v},
        "development_nodes": knoten_gesamt,
        "nodes_through_the_product": knoten_produkt,
        "sentence": (
            f"{knoten_produkt} of {knoten_gesamt} post-anchor development nodes "
            "were executed through the shipped control plane"
        ),
        "other_histories": {
            h: {"commits_by_category": k,
                "what": FREMDE_HISTORIEN.get(h, "unknown history")}
            for h, k in sorted(fremd_nach_kategorie.items())
        },
        "problems": probleme,
        "ok": not probleme,
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
    bericht = pruefe(args.repo.resolve(), ledger)
    if args.json:
        print(json.dumps(bericht, indent=2))
        # One exit semantics for both output formats: 3 an environment gap,
        # 1 a finding, 0 a verified pass. The first version of this line
        # returned 0 for the gap, so `--json` reported as success exactly the
        # state the text mode had been given its own code to avoid -- the
        # repair of O171 in one format and not the other.
        if bericht.get("environment_gap"):
            return 3
        return 0 if bericht["ok"] else 1
    if bericht.get("environment_gap"):
        # Not a pass and not a violation: a third state, printed as itself and
        # given its own exit code so that `attribution.py && echo ok` cannot
        # print ok for a run that verified nothing (O171).
        print("ENVIRONMENT_GAP: nothing was verified here")
        print(f"  {bericht['environment_gap']}")
        return 3
    else:
        print(f"anchor {bericht['anchor'][:12]}, "
              f"{bericht['commits_after_anchor']} commit(s) after it")
        for k, v in sorted(bericht["commits_by_category"].items()):
            print(f"  {k:<26s} {v:>3d} commit(s)")
        print()
        print("  " + bericht["sentence"])
        for h, d in (bericht.get("other_histories") or {}).items():
            summe = sum(d["commits_by_category"].values())
            print(f"  recorded from {h}: {summe} commit(s), "
                  "not counted in the ratio above")
        for p in bericht["problems"]:
            print(f"  PROBLEM: {p}")
    return 0 if bericht["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
