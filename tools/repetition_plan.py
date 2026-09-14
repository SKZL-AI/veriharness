#!/usr/bin/env python3
"""repetition_plan.py -- what the frozen protocol actually requires per cell.

A campaign progress line that reads `15 / 15 task-arm cells` answers a question
the protocol did not ask. The frozen text says:

    Repetitions: **3 per (task, arm)** where the budget allows, 1 where it does
    not.

So before a campaign may be called complete, three things have to be answered
from the **frozen commit** and not from what would be convenient now:

1. what `where the budget allows` means;
2. which cells therefore require three repetitions;
3. which are limited to one by that same rule.

This program answers them, or says it cannot. It never picks a reading because
the reading is cheaper: where the frozen text does not decide, the answer is
`NOT_DETERMINABLE`, and a campaign measured under an undecidable requirement
may be reported as what it is -- one repetition per cell -- and not as the
three-repeat design the protocol claims.

Counting is likewise conservative. A result file is a repetition only if it
carries an actual run: a file whose arm never dispatched, a parked copy of an
earlier attempt, and a file from another campaign are each not a repetition,
and each is named rather than silently skipped.

Usage:
    python3 tools/repetition_plan.py [--campaign v2] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

HIER = Path(__file__).resolve().parent
HOH = HIER.parent

#: The commit that introduced the protocol. Every quotation below is read out
#: of this commit rather than out of the working tree, because the working
#: tree is where a protocol gets helpfully clarified after the fact.
PROTOKOLL_PFAD = "docs/BENCHMARK_PROTOCOL.md"

KAMPAGNEN_DIR = {
    "v1": HOH / "dogfood/benchmark/results",
    "v2": HOH / "dogfood/benchmark/results-v2",
    "v3": HOH / "dogfood/benchmark/results-v3",
}

#: `<task>.<arm>.<rep>.v<UTC>.json` -- a cell as it stood before a recount, and
#: `<task>.<arm>.<rep>.attemptN.v<UTC>.json` -- an attempt that did not deliver.
#: Both are kept and neither is a repetition.
_GEPARKT = re.compile(r"\.(v\d{8}T\d{6}Z|attempt\d+\.v\d{8}T\d{6}Z)\.json$")


def frozen_protocol_commit(kampagne: str = "v2") -> str:
    """The commit whose protocol text governs this campaign.

    For v1 and v2 that is the commit which **added** the file: they were run
    under the design frozen there, and reading them against a later text would
    be judging a finished campaign by a rule written after it.

    A campaign that pre-registers itself names its own commit instead, in
    `docs/benchmarks/<campaign>/PREREGISTRATION.json`. Without this, v3 would
    be measured against v1's repetition bullet -- the one that is not
    decidable -- and a v3 that ran all three repetitions per cell would still
    report `NOT_DETERMINABLE`, which is the wrong answer about a campaign that
    says `3 per (task, arm), without exception` in its own frozen text.
    """
    reg = HOH / "docs" / "benchmarks" / kampagne / "PREREGISTRATION.json"
    if reg.is_file():
        try:
            daten = json.loads(reg.read_text(encoding="utf-8"))
        except ValueError:                          # pragma: no cover - exotic
            daten = {}
        for schluessel in (f"benchmark_{kampagne}_protocol_commit",
                           "protocol_commit"):
            wert = str(daten.get(schluessel) or "").strip()
            if wert:
                return wert
    p = subprocess.run(
        ["git", "-C", str(HOH), "log", "--format=%H", "--diff-filter=A", "--",
         PROTOKOLL_PFAD],
        capture_output=True, text=True, check=False)
    zeilen = [z for z in p.stdout.split() if z]
    return zeilen[-1] if zeilen else ""


def frozen_text(commit: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(HOH), "show", f"{commit}:{PROTOKOLL_PFAD}"],
        capture_output=True, text=True, check=False)
    return p.stdout


#: `dispatch_budget = 9 per cell ...` in a pre-registered design block, and
#: `at most **9 role dispatches per task**` in the original prose. Read rather
#: than typed: `9` appeared as a literal in this file while the protocol
#: carried its own `9`, and nothing compared the two -- a constant beside a
#: result, one level up from the one this project was already caught with.
_BUDGET_ZEILE = re.compile(r"^\s*dispatch_budget\s*=\s*(\d+)\b", re.M)
_BUDGET_PROSA = re.compile(r"at most \*\*(\d+) role dispatches")


def per_run_budget(text: str) -> int | None:
    """The dispatch ceiling the frozen text names, or None if it names none."""
    for muster in (_BUDGET_ZEILE, _BUDGET_PROSA):
        m = muster.search(text)
        if m:
            return int(m.group(1))
    return None


def budget_rule(text: str) -> dict:
    """Every budget the frozen protocol defines, and what each governs.

    The distinction the advisory asked for explicitly: a per-run dispatch
    budget, a per-(task, arm) repetition allowance and a campaign cost are
    three different quantities, and only one of them is in the text.
    """
    zeilen = text.splitlines()
    treffer = [z.strip() for z in zeilen if "budget" in z.lower()]
    per_run = [z for z in treffer
               if "role dispatches per" in z or "dispatch budget" in z]
    kampagne = [z for z in treffer
                if any(w in z.lower() for w in
                       ("campaign budget", "total budget", "quota", "per campaign"))]
    return {
        "per_run_dispatch_budget_defined": bool(per_run),
        "per_run_dispatch_budget_quotes": per_run,
        "campaign_or_quota_budget_defined": bool(kampagne),
        "campaign_or_quota_budget_quotes": kampagne,
        "repetition_allowance_defined": False,
        "all_budget_mentions": treffer,
    }


#: A pre-registered campaign states its repetition rule as a machine-readable
#: line of its own design block. Matched rather than interpreted: the word
#: "unconditional" in the prose beside it is what a reader needs, and this is
#: what a tool needs.
_REP_ZEILE = re.compile(
    r"^\s*repetitions\s*=\s*(\d+)\s+per\s*\(task,\s*arm\)", re.M)


def repetition_requirement(text: str) -> dict:
    """Is `3 per (task, arm) where the budget allows` decidable as written?

    It is decidable only if the frozen text defines a budget that governs
    **how many repetitions may be attempted**. It defines one budget, and that
    budget governs a single run: nine role dispatches per task per arm. A
    per-run ceiling cannot say whether a second run of the same cell is
    affordable.

    And the bullet immediately after it points the other way:

        The benchmark stops when every cell has been attempted once.
        Attempting more is allowed; reporting a subset of cells is not.

    One bullet reads as a requirement of three, the next as a stopping rule at
    one with more permitted. Choosing between them now -- in either
    direction -- would be a post-hoc criterion, which is the thing freezing a
    protocol exists to prevent.
    """
    fest = _REP_ZEILE.search(text)
    if fest:
        # A pre-registration that fixes the number without a condition is
        # decidable by construction, and it is the only shape that is: the
        # undecidability this function exists to report came from a rule that
        # depended on a budget the same text never defined.
        n = int(fest.group(1))
        zeile = fest.group(0).strip()
        unbedingt = "without exception" in text.split(zeile, 1)[-1][:80] or \
                    "without exception" in zeile
        return {
            "verdict": "DETERMINABLE" if unbedingt else "NOT_DETERMINABLE",
            "required_repetitions": n if unbedingt else None,
            "repetition_bullet": [zeile],
            "stopping_bullet": [z.strip() for z in text.splitlines()
                                if "stopping_rule" in z],
            "reason": (
                f"the pre-registered design fixes {n} repetitions per (task, "
                f"arm) with no condition attached"
            ) if unbedingt else (
                "the pre-registered design names a number and qualifies it; a "
                "qualified number is the shape that was undecidable before"
            ),
        }
    b = budget_rule(text)
    wiederholung = [z.strip() for z in text.splitlines()
                    if "Repetitions:" in z]
    stopp = [z.strip() for z in text.splitlines()
             if "attempted once" in z]
    entscheidbar = b["repetition_allowance_defined"] or (
        b["campaign_or_quota_budget_defined"])
    return {
        "verdict": "DETERMINABLE" if entscheidbar else "NOT_DETERMINABLE",
        "required_repetitions": None,
        "repetition_bullet": wiederholung,
        "stopping_bullet": stopp,
        "reason": (
            "the frozen text defines exactly one budget, and it governs a "
            "single run (nine role dispatches per task per arm). A per-run "
            "ceiling cannot say whether a second run of the same cell is "
            "affordable, and no campaign, quota or wall-clock allowance is "
            "defined anywhere in it. The bullet that follows sets the "
            "stopping rule at one attempt per cell and calls more 'allowed'. "
            "Reading either bullet as decisive now would be a post-hoc "
            "criterion"
        ) if not entscheidbar else "a repetition allowance is defined",
    }


def zellen(kampagne: str) -> tuple[list[dict], list[str]]:
    """(counted cells, reasons a file was not counted)."""
    quelle = KAMPAGNEN_DIR[kampagne]
    gezaehlt, verworfen = [], []
    if not quelle.is_dir():
        return gezaehlt, [f"{quelle} does not exist"]
    for f in sorted(quelle.glob("*.json")):
        if _GEPARKT.search(f.name):
            verworfen.append(f"{f.name}: a parked earlier version, not a repetition")
            continue
        try:
            z = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            verworfen.append(f"{f.name}: unreadable ({exc})")
            continue
        # A result file whose arm never ran is not a repetition. Three
        # different things wear that shape and only one of them used to be
        # caught, which let a campaign in which **every cell crashed** report
        # itself complete and usable:
        #
        # * the arm recorded nothing at all;
        # * the *instrument* raised (`harness_error`) -- the repetition did
        #   not happen and has to be run again. Not the same as an arm that
        #   ran and failed;
        # * the arm's own provider call raised (`arm_detail.error`), which
        #   still returns a result file carrying `dispatches: 1`.
        #
        # `produced_final_state: false` is deliberately **not** in this list.
        # An arm that ran and changed nothing is a measured outcome; the
        # protocol says such a cell is kept, reported and named, and excluded
        # from the correctness comparison rather than from the campaign.
        detail = z.get("arm_detail") or {}
        if not detail:
            verworfen.append(f"{f.name}: the arm recorded nothing, so no run happened")
            continue
        if z.get("harness_error"):
            verworfen.append(
                f"{f.name}: the instrument raised ({str(z['harness_error'])[:80]}), "
                f"so this repetition did not happen")
            continue
        if detail.get("error"):
            verworfen.append(
                f"{f.name}: the arm's provider call raised "
                f"({str(detail['error'])[:80]}), so this repetition did not happen")
            continue
        z["_datei"] = f.name
        gezaehlt.append(z)
    return gezaehlt, verworfen


def _identitaet(z: dict) -> str:
    """What distinguishes one actual run from a copy of it.

    The temp tree a cell was measured in carries its own nonce and is never
    reused (`arbeitsbaum` in `tools/benchmark.py` makes sure of it), and the
    start time is written by the runner. Either alone would be weak; together
    they are what a copied-and-relabelled file cannot fake without editing
    both.
    """
    dd = z.get("arm_detail") or {}
    return "|".join(str(x) for x in (
        z.get("measured_tree") or dd.get("root") or dd.get("worktree") or "",
        z.get("started_at") or "",
    ))


def _gezaehlt_oder_behauptet(dd: dict) -> bool:
    """Did anything actually count this cell's dispatches?

    It used to ask whether the key `dispatch_budget` was present, which is a
    question about the *shape* of the record and not about the measurement.
    Every cell a current benchmark writes carries that key, so the guard that
    caught campaign v1's asserted figures was inert for every future campaign
    by construction -- and arm A's constant `1` would have certified as
    measured.

    The question is whether a counter produced the number.
    `dispatch_count.provider_calls` is that counter, and a log line that
    predates the field cannot answer, so a cell carrying any of those is
    unknown rather than conformant.
    """
    zaehlung = dd.get("dispatch_count")
    if isinstance(zaehlung, dict):
        if zaehlung.get("lines_without_the_figure"):
            return False
        return "provider_calls" in zaehlung
    return dd.get("dispatches_recounted") is not None


def _konformitaet(dd: dict, echt, grenze: int | None) -> str:
    if grenze is None:
        return "UNKNOWN_NO_BUDGET_IN_THE_FROZEN_TEXT"
    if not _gezaehlt_oder_behauptet(dd):
        return "UNKNOWN_FIGURE_WAS_ASSERTED"
    if not isinstance(echt, int):
        return "UNKNOWN"
    if echt > grenze:
        # Both readings are a violation, and one of them used not to be.
        # `EXCEEDED_AND_STOPPED` was excluded from the campaign's violation
        # list, so a campaign that spent 18 against a hard 9 on every single
        # run certified as matched-budget as long as each cell wrote the word
        # BUDGET_EXHAUSTED -- v2's exact number, with a string added. Under a
        # budget that is *enforced*, exceeding it is not a stopping rule doing
        # its job; it is the enforcement having failed.
        return ("EXCEEDED_AND_NOT_STOPPED"
                if str(dd.get("halt", "")) != "BUDGET_EXHAUSTED"
                else "EXCEEDED_AND_STOPPED")
    return "WITHIN"


def plan(kampagne: str) -> dict:
    commit = frozen_protocol_commit(kampagne)
    text = frozen_text(commit)
    anforderung = repetition_requirement(text)
    budget = budget_rule(text)
    grenze = per_run_budget(text)
    gezaehlt, verworfen = zellen(kampagne)

    aufgaben = sorted(p.name for p in (HOH / "dogfood/benchmark/tasks").iterdir()
                      if p.is_dir())
    # Plus any task a counted cell names that the task directory does not. A
    # result for a task nobody declared is a finding, and dropping it from the
    # table would hide it behind a row count that still looked right.
    fremd = sorted({z.get("task") for z in gezaehlt} - set(aufgaben) - {None})
    tabelle = []
    for task in [*aufgaben, *fremd]:
        for arm in ("A", "B", "C"):
            passend = [z for z in gezaehlt
                       if z.get("task") == task and z.get("arm") == arm]
            reps = sorted({z.get("repetition", 1) for z in passend})
            # Three copies of one run, relabelled 1, 2 and 3, counted as three
            # repetitions: `len(reps)` counts distinct *labels*, and a label
            # is written into the file by whoever wrote the file. A run has an
            # identity -- the tree it was measured in and the moment it
            # started -- and two repetitions that share one are one run.
            identitaeten = [_identitaet(z) for z in passend]
            doppelt = sorted({i for i in identitaeten
                              if identitaeten.count(i) > 1 and i})
            # The three quantities the advisory asked to keep apart, per
            # cell: what one run was allowed to spend, what it spent, and
            # whether the protocol's own consequence for exceeding it was
            # applied. The frozen text is explicit -- "a run that exceeds the
            # dispatch budget is stopped and recorded as BUDGET_EXHAUSTED" --
            # and an arm that ran past it without being stopped did not have
            # the matched resource the comparison is named after.
            ausgaben, konform = [], []
            for z in passend:
                dd = z.get("arm_detail") or {}
                gezaehlt_echt = dd.get("dispatches_recounted")
                echt = gezaehlt_echt or dd.get("dispatches")
                ausgaben.append(echt)
                konform.append(_konformitaet(dd, echt, grenze))
            tabelle.append({
                "task": task,
                "arm": arm,
                "dispatches_per_repetition": ausgaben,
                "per_run_dispatch_budget": grenze,
                "budget_conformance": konform,
                # The number the campaign's own frozen text fixes, not a 3
                # typed here. A `3` in this file would be the same shape as
                # the cost constant beside a benchmark result: right today,
                # unverifiable, and wrong the moment a campaign declares
                # something else.
                "required_repetitions": (
                    anforderung.get("required_repetitions")
                    if anforderung["verdict"] == "DETERMINABLE"
                    else "NOT_DETERMINABLE"),
                "completed_repetitions": len(reps),
                "repetition_numbers": reps,
                "repetitions_sharing_a_run_identity": doppelt,
                "budget_basis": (
                    "per-run dispatch budget only; no repetition allowance is "
                    "defined in the frozen protocol"
                    if anforderung["verdict"] != "DETERMINABLE" else
                    "per-run dispatch budget, and a repetition count the "
                    "pre-registration fixes without condition"),
                "reason": anforderung["reason"],
                "protocol_anchor": f"{commit[:12]}:{PROTOKOLL_PFAD}",
            })
    fehlend = [f"{r['task']}/{r['arm']}" for r in tabelle
               if r["completed_repetitions"] == 0]
    ueber = [f"{r['task']}/{r['arm']}" for r in tabelle
             if any(k.startswith("EXCEEDED") for k in r["budget_conformance"])]
    kopien = [f"{r['task']}/{r['arm']}" for r in tabelle
              if r["repetitions_sharing_a_run_identity"]]
    unbekannt = [f"{r['task']}/{r['arm']}" for r in tabelle
                 if any(k.startswith("UNKNOWN") for k in r["budget_conformance"])]
    return {
        "campaign": kampagne,
        "protocol_commit": commit,
        "protocol_repetition_requirement": anforderung["verdict"],
        "why": anforderung["reason"],
        "repetition_bullet": anforderung["repetition_bullet"],
        "stopping_bullet": anforderung["stopping_bullet"],
        "budget": budget,
        "cells": tabelle,
        "cells_with_no_repetition": fehlend,
        "tasks_not_declared_in_the_task_directory": fremd,
        "files_not_counted": verworfen,
        "one_repetition_each_complete": not fehlend,
        "cells_over_budget_and_not_stopped": ueber,
        "cells_whose_spend_is_unknown": unbekannt,
        "cells_whose_repetitions_share_a_run": kopien,
        "per_run_dispatch_budget": grenze,
        # Three states, not two. "No cell exceeded" is not the same as "every
        # cell was measured and stayed inside", and collapsing them would let
        # a campaign whose figures were asserted certify its own conformance.
        "budget_rule": ("VIOLATED" if ueber
                        else "NOT_DETERMINABLE" if unbekannt
                        else "ENFORCED"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign", default="v2", choices=sorted(KAMPAGNEN_DIR))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    p = plan(args.campaign)
    if args.json:
        print(json.dumps(p, indent=2))
        return 0 if p["one_repetition_each_complete"] else 1

    print(f"campaign {p['campaign']}, protocol frozen at "
          f"{p['protocol_commit'][:12]}")
    print(f"  protocol_repetition_requirement = "
          f"{p['protocol_repetition_requirement']}")
    print(f"  {p['why']}.")
    print()
    print(f"  {'task':<16s}{'arm':<5s}{'required':<18s}{'completed':<10s}reps")
    for r in p["cells"]:
        print(f"  {r['task']:<16s}{r['arm']:<5s}"
              f"{str(r['required_repetitions']):<18s}"
              f"{r['completed_repetitions']:<10d}{r['repetition_numbers']}")
    if p["files_not_counted"]:
        print("\n  not counted:")
        for z in p["files_not_counted"]:
            print(f"    {z}")
    print(f"\n  budget_rule = {p['budget_rule']}")
    if p["cells_whose_spend_is_unknown"]:
        print("  spend not measured (the figure was asserted) for: "
              + ", ".join(p["cells_whose_spend_is_unknown"]))
    if p["cells_over_budget_and_not_stopped"]:
        print("\n  over the per-run dispatch budget and NOT stopped, which the "
              "frozen protocol requires:")
        for z in p["cells_over_budget_and_not_stopped"]:
            print(f"    {z}")
    if p["cells_with_no_repetition"]:
        print("\n  cells with no repetition at all: "
              + ", ".join(p["cells_with_no_repetition"]))
    return 0 if p["one_repetition_each_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
