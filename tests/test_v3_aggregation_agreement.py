"""Two aggregations of campaign v3, written independently, must agree.

`tools/benchmark.py` was frozen for the campaign and its reporting half was
repaired afterwards (O154). The repair touches presentation only -- that was
established by call graph, and by the raw result files hashing identically
before and after -- but "touches presentation only" is a claim about a patch,
and a claim about a patch is worth exactly as much as a check of it.

So this file aggregates the same 45 cells a second time, from the result files
alone, without importing the reporter. Where the two disagree, one of them is
wrong and the benchmark cannot say which; the point of writing the second one
from scratch is that they do not share the mistake.

Deliberately different in construction: the reporter groups cells and formats
markdown rows; this counts with a `Counter` over flat tuples and compares
numbers. A shared helper would defeat the exercise.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path


from conftest import RUN_EVIDENCE_V3, braucht_evidenz

HOH = Path(__file__).resolve().parent.parent
ERGEBNISSE = HOH / "dogfood/benchmark/results-v3"


def zellen() -> list[dict]:
    if not ERGEBNISSE.is_dir():
        braucht_evidenz(RUN_EVIDENCE_V3)
    raus = []
    for f in sorted(ERGEBNISSE.glob("*.json")):
        if ".attempt" in f.name or re.search(r"\.v\d{8}T\d{6}Z\.json$", f.name):
            continue
        raus.append(json.loads(f.read_text()))
    return raus


def zweite_aggregation(alle: list[dict]) -> dict:
    """Counted flat, from the fields themselves."""
    je_zelle: Counter = Counter()
    bestanden: Counter = Counter()
    falsch: Counter = Counter()
    ausgaben: dict[tuple[str, str], list[int]] = {}
    for z in alle:
        k = (z["task"], z["arm"])
        je_zelle[k] += 1
        if (z.get("hidden_suite") or {}).get("passed"):
            bestanden[k] += 1
        if z.get("false_accept"):
            falsch[k] += 1
        n = (z.get("arm_detail") or {}).get("dispatches")
        if isinstance(n, int):
            ausgaben.setdefault(k, []).append(n)
    return {
        "runs": sum(je_zelle.values()),
        "cells": len(je_zelle),
        "repetitions_per_cell": dict(je_zelle),
        "hidden_pass_per_cell": {k: bestanden[k] for k in je_zelle},
        "false_accepts_per_cell": {k: falsch[k] for k in je_zelle},
        "false_accepts_total": sum(falsch.values()),
        "dispatch_range_per_cell": {
            k: (min(v), max(v)) for k, v in ausgaben.items()},
        "dispatch_total": sum(sum(v) for v in ausgaben.values()),
    }


def reporter():
    spec = importlib.util.spec_from_file_location(
        "benchmark", HOH / "tools" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


def aus_reportzeilen(zeilen: list[str]) -> dict:
    """Reads the reporter's own aggregate table back out of its markdown."""
    raus = {}
    for z in zeilen:
        teile = [t.strip() for t in z.strip().strip("|").split("|")]
        task = teile[0].strip("`")
        arm, reps, bestanden, falsch, spanne = (
            teile[1], int(teile[2]), teile[3], int(teile[4]), teile[5])
        raus[(task, arm)] = {
            "reps": reps,
            "hidden_pass": int(bestanden.split("/")[0]),
            "measured": int(bestanden.split("/")[1]),
            "false_accepts": falsch,
            "range": spanne,
        }
    return raus


def test_the_campaign_has_the_runs_it_declared():
    a = zweite_aggregation(zellen())
    assert a["runs"] == 45, "45 planned runs: 5 tasks x 3 arms x 3 repetitions"
    assert a["cells"] == 15
    assert set(a["repetitions_per_cell"].values()) == {3}, (
        "every cell owes exactly three repetitions, without exception")


def test_both_aggregations_report_the_same_repetition_counts():
    alle = zellen()
    zweit = zweite_aggregation(alle)
    erst = aus_reportzeilen(reporter()._aggregatzeilen(alle))
    assert set(erst) == set(zweit["repetitions_per_cell"])
    for k, v in erst.items():
        assert v["reps"] == zweit["repetitions_per_cell"][k], k


def test_both_aggregations_report_the_same_hidden_suite_results():
    alle = zellen()
    zweit = zweite_aggregation(alle)
    erst = aus_reportzeilen(reporter()._aggregatzeilen(alle))
    for k, v in erst.items():
        assert v["hidden_pass"] == zweit["hidden_pass_per_cell"][k], k


def test_both_aggregations_report_the_same_false_accepts():
    """The primary metric. If the two disagree here the campaign says nothing."""
    alle = zellen()
    zweit = zweite_aggregation(alle)
    erst = aus_reportzeilen(reporter()._aggregatzeilen(alle))
    for k, v in erst.items():
        assert v["false_accepts"] == zweit["false_accepts_per_cell"][k], k
    assert sum(v["false_accepts"] for v in erst.values()) == \
        zweit["false_accepts_total"]


def test_both_aggregations_report_the_same_dispatch_spend():
    alle = zellen()
    zweit = zweite_aggregation(alle)
    erst = aus_reportzeilen(reporter()._aggregatzeilen(alle))
    for k, v in erst.items():
        lo, hi = zweit["dispatch_range_per_cell"][k]
        erwartet = str(lo) if lo == hi else f"{lo}-{hi}"
        assert v["range"] == erwartet, (k, v["range"], erwartet)


def test_the_per_repetition_table_has_one_row_per_run():
    """The defect O154 was about: `{(task, arm): cell}` kept the last
    repetition of three, so a 45-run campaign rendered as 15 rows."""
    alle = zellen()
    zeilen = reporter()._wiederholungszeilen(alle)
    assert len(zeilen) == len(alle) == 45


def test_no_cell_exceeded_the_budget_the_protocol_declares():
    """Arm C shares nine dispatches across a node and its repair nodes. In v2
    each run got its own nine and three cells spent eighteen."""
    for z in zellen():
        n = (z.get("arm_detail") or {}).get("dispatches")
        assert isinstance(n, int)
        assert n <= 9, f"{z['task']}/{z['arm']}/{z['repetition']} spent {n}"
