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


from conftest import RUN_EVIDENCE_V3, needs_evidence

HOH = Path(__file__).resolve().parent.parent
RESULTS = HOH / "dogfood/benchmark/results-v3"


def cells() -> list[dict]:
    if not RESULTS.is_dir():
        needs_evidence(RUN_EVIDENCE_V3)
    out_list = []
    for f in sorted(RESULTS.glob("*.json")):
        if ".attempt" in f.name or re.search(r"\.v\d{8}T\d{6}Z\.json$", f.name):
            continue
        out_list.append(json.loads(f.read_text()))
    return out_list


def second_aggregation(all_: list[dict]) -> dict:
    """Counted flat, from the fields themselves."""
    per_cell: Counter = Counter()
    passed_: Counter = Counter()
    wrong: Counter = Counter()
    outputs_: dict[tuple[str, str], list[int]] = {}
    for z in all_:
        k = (z["task"], z["arm"])
        per_cell[k] += 1
        if (z.get("hidden_suite") or {}).get("passed"):
            passed_[k] += 1
        if z.get("false_accept"):
            wrong[k] += 1
        n = (z.get("arm_detail") or {}).get("dispatches")
        if isinstance(n, int):
            outputs_.setdefault(k, []).append(n)
    return {
        "runs": sum(per_cell.values()),
        "cells": len(per_cell),
        "repetitions_per_cell": dict(per_cell),
        "hidden_pass_per_cell": {k: passed_[k] for k in per_cell},
        "false_accepts_per_cell": {k: wrong[k] for k in per_cell},
        "false_accepts_total": sum(wrong.values()),
        "dispatch_range_per_cell": {
            k: (min(v), max(v)) for k, v in outputs_.items()},
        "dispatch_total": sum(sum(v) for v in outputs_.values()),
    }


def reporter():
    spec = importlib.util.spec_from_file_location(
        "benchmark", HOH / "tools" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


def from_report_rows(lines: list[str]) -> dict:
    """Reads the reporter's own aggregate table back out of its markdown."""
    out_list = {}
    for z in lines:
        parts = [t.strip() for t in z.strip().strip("|").split("|")]
        task = parts[0].strip("`")
        arm, reps, passed_, wrong, span = (
            parts[1], int(parts[2]), parts[3], int(parts[4]), parts[5])
        out_list[(task, arm)] = {
            "reps": reps,
            "hidden_pass": int(passed_.split("/")[0]),
            "measured": int(passed_.split("/")[1]),
            "false_accepts": wrong,
            "range": span,
        }
    return out_list


def test_the_campaign_has_the_runs_it_declared():
    a = second_aggregation(cells())
    assert a["runs"] == 45, "45 planned runs: 5 tasks x 3 arms x 3 repetitions"
    assert a["cells"] == 15
    assert set(a["repetitions_per_cell"].values()) == {3}, (
        "every cell owes exactly three repetitions, without exception")


def test_both_aggregations_report_the_same_repetition_counts():
    all_ = cells()
    second_ = second_aggregation(all_)
    first_ = from_report_rows(reporter()._aggregate_rows(all_))
    assert set(first_) == set(second_["repetitions_per_cell"])
    for k, v in first_.items():
        assert v["reps"] == second_["repetitions_per_cell"][k], k


def test_both_aggregations_report_the_same_hidden_suite_results():
    all_ = cells()
    second_ = second_aggregation(all_)
    first_ = from_report_rows(reporter()._aggregate_rows(all_))
    for k, v in first_.items():
        assert v["hidden_pass"] == second_["hidden_pass_per_cell"][k], k


def test_both_aggregations_report_the_same_false_accepts():
    """The primary metric. If the two disagree here the campaign says nothing."""
    all_ = cells()
    second_ = second_aggregation(all_)
    first_ = from_report_rows(reporter()._aggregate_rows(all_))
    for k, v in first_.items():
        assert v["false_accepts"] == second_["false_accepts_per_cell"][k], k
    assert sum(v["false_accepts"] for v in first_.values()) == \
        second_["false_accepts_total"]


def test_both_aggregations_report_the_same_dispatch_spend():
    all_ = cells()
    second_ = second_aggregation(all_)
    first_ = from_report_rows(reporter()._aggregate_rows(all_))
    for k, v in first_.items():
        lo, hi = second_["dispatch_range_per_cell"][k]
        expected = str(lo) if lo == hi else f"{lo}-{hi}"
        assert v["range"] == expected, (k, v["range"], expected)


def test_the_per_repetition_table_has_one_row_per_run():
    """The defect O154 was about: `{(task, arm): cell}` kept the last
    repetition of three, so a 45-run campaign rendered as 15 rows."""
    all_ = cells()
    lines = reporter()._wiederholungszeilen(all_)
    assert len(lines) == len(all_) == 45


def test_no_cell_exceeded_the_budget_the_protocol_declares():
    """Arm C shares nine dispatches across a node and its repair nodes. In v2
    each run got its own nine and three cells spent eighteen."""
    for z in cells():
        n = (z.get("arm_detail") or {}).get("dispatches")
        assert isinstance(n, int)
        assert n <= 9, f"{z['task']}/{z['arm']}/{z['repetition']} spent {n}"
