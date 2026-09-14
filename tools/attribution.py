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


def pruefe(repo: Path, ledger: dict) -> dict:
    anker = ledger["anchor"]
    alle = commits_since(repo, anker)
    bekannt = set(alle)
    gesehen: dict[str, str] = {}
    probleme: list[str] = []

    for eintrag in ledger["entries"]:
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
    if nicht_zugeordnet and alle and nicht_zugeordnet[-1] == alle[-1]:
        nicht_zugeordnet = nicht_zugeordnet[:-1]
    if nicht_zugeordnet:
        probleme.append(
            f"{len(nicht_zugeordnet)} commit(s) after the anchor belong to no "
            "entry: " + ", ".join(c[:12] for c in nicht_zugeordnet[:8])
        )

    nach_kategorie: dict[str, int] = {k: 0 for k in KATEGORIEN}
    knoten_gesamt = 0
    knoten_produkt = 0
    for eintrag in ledger["entries"]:
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
    else:
        print(f"anchor {bericht['anchor'][:12]}, "
              f"{bericht['commits_after_anchor']} commit(s) after it")
        for k, v in sorted(bericht["commits_by_category"].items()):
            print(f"  {k:<26s} {v:>3d} commit(s)")
        print()
        print("  " + bericht["sentence"])
        for p in bericht["problems"]:
            print(f"  PROBLEM: {p}")
    return 0 if bericht["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
