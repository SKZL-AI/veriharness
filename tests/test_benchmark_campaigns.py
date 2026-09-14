"""Can a replication touch the campaign it is replicating?

`docs/BENCHMARK_PROTOCOL.md` marks campaign v1 `PRE-O125-CLOSURE` and says it
stays exactly as it was measured: no cell re-run, no number repaired, no cell
reinterpreted in the light of what was learned afterwards. A benchmark edited
after its result is known is not a benchmark.

That is a promise about a directory, so it is testable. These tests are short
on purpose -- the interesting part is not the code, it is that the promise has
a check at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent


def _laden():
    spec = importlib.util.spec_from_file_location(
        "benchmark", WURZEL / "tools" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("benchmark", mod)
    spec.loader.exec_module(mod)
    return mod


bm = _laden()


def test_the_two_campaigns_write_to_different_directories():
    assert bm.ergebnisse_fuer("v1") != bm.ergebnisse_fuer("v2")
    assert bm.ergebnisse_fuer("v1").name == "results"
    assert bm.ergebnisse_fuer("v2").name == "results-v2"


def test_an_unknown_campaign_is_refused_rather_than_defaulted():
    """Defaulting would put a replication's cells in the campaign it replicates."""
    with pytest.raises(SystemExit):
        bm.ergebnisse_fuer("v9")


def test_campaign_v1_still_holds_the_fifteen_cells_it_was_measured_with():
    """The promise this file exists for.

    A count rather than a digest: cells carry timings, so a digest would change
    for a reason that is not an edit. Fifteen is what the results document
    reports, and a sixteenth appearing here means a replication wrote into the
    campaign it was replicating.
    """
    quelle = bm.ergebnisse_fuer("v1")
    if not quelle.is_dir():
        pytest.skip("campaign v1's cells are not in this checkout")
    zellen = [f for f in quelle.glob("*.json") if ".attempt" not in f.name]
    assert len(zellen) == 15, sorted(f.name for f in zellen)


def test_the_report_reads_the_campaign_it_is_asked_for(tmp_path, monkeypatch):
    import json

    v2 = tmp_path / "results-v2"
    v2.mkdir()
    (v2 / "t.A.1.json").write_text(json.dumps({
        "task": "t", "arm": "A", "rep": 1, "seconds": 1.0,
        "hidden_suite": {"passed": True}, "false_accept": False,
        "produced_final_state": True,
    }))
    monkeypatch.setitem(bm.KAMPAGNEN, "v2", v2)

    class Args:
        campaign = "v2"
        write = False

    assert bm.cmd_report(Args()) == 0


def test_a_replication_cannot_overwrite_the_document_it_replicates(tmp_path,
                                                                   monkeypatch):
    """One forgotten flag would have rewritten v1's results in place."""
    import json

    v2 = tmp_path / "results-v2"
    v2.mkdir()
    (v2 / "t.A.1.json").write_text(json.dumps({
        "task": "t", "arm": "A", "rep": 1, "seconds": 1.0,
        "hidden_suite": {"passed": True}, "false_accept": False,
        "produced_final_state": True,
    }))
    monkeypatch.setitem(bm.KAMPAGNEN, "v2", v2)
    monkeypatch.setattr(bm, "HOH", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BENCHMARK_RESULTS.md").write_text("v1, untouched\n")

    class Args:
        campaign = "v2"
        write = True

    bm.cmd_report(Args())

    assert (tmp_path / "docs" / "BENCHMARK_RESULTS.md").read_text() == \
        "v1, untouched\n"
    assert (tmp_path / "docs" / "BENCHMARK_RESULTS_v2.md").is_file()


def test_the_replication_header_says_it_is_one_and_names_what_it_may_not_claim():
    kopf = "\n".join(bm._kampagnenkopf("v2"))
    assert "declared replication" in kopf
    assert "partial" in kopf
    assert "would not license" in kopf


def _log(d: Path, *aufrufe) -> None:
    """Writes a dispatch log. `None` stands for a line that predates the field."""
    import json

    d.mkdir(parents=True, exist_ok=True)
    (d / "telemetry.jsonl").write_text("".join(
        json.dumps({"role": "planner"} if n is None
                   else {"role": "planner", "provider_calls": n}) + "\n"
        for n in aufrufe))


def test_dispatches_are_counted_from_the_logs_and_not_asserted(tmp_path):
    """A benchmark whose premise is a matched budget cannot assert its budget.

    It was `min(iterations * 3, DISPATCH_BUDGET)` -- a constant in arm C. The
    first cell of the replication ran a node **and** a repair node, spent 18
    dispatches, and reported 9.
    """
    root = tmp_path / "root"
    _log(root / "node1", *([1] * 9))
    _log(root / "repair-1-1", *([1] * 9))

    assert bm.gezaehlte_dispatches(root) == 18
    assert bm.gezaehlte_dispatches(root) > bm.DISPATCH_BUDGET


def test_a_line_is_not_a_call_in_either_direction(tmp_path):
    """The replacement asserted its own premise, one level down.

    "`telemetry.jsonl` has one line per dispatch by construction" -- it does
    not. `tools/budget_evidence.py` measured both directions on a fixture: a
    role that retried twice writes one line for three provider calls, and a
    role refused at the budget writes one line for none.
    """
    root = tmp_path / "root"
    _log(root / "node1", 3, 0, 1)

    z = bm.dispatch_zaehlung(root)
    assert z["lines"] == 3
    assert z["provider_calls"] == 4
    assert z["lines_without_the_figure"] == 0


def test_a_line_that_predates_the_figure_is_reported_not_scored_as_one(tmp_path):
    """Reading an old line as "1" is how the asserted figure got in.

    Campaign v1 and v2 were logged before `provider_calls` existed. Their
    lines cannot answer, and the counter says so instead of producing a
    number that looks measured.
    """
    root = tmp_path / "root"
    _log(root / "node1", None, None, 2)

    z = bm.dispatch_zaehlung(root)
    assert z["provider_calls"] == 2
    assert z["lines_without_the_figure"] == 2
    assert z["lines"] == 3


def test_a_run_tree_with_no_log_counts_zero_rather_than_guessing(tmp_path):
    assert bm.gezaehlte_dispatches(tmp_path) == 0
    assert bm.dispatch_zaehlung(tmp_path) == {
        "provider_calls": 0, "lines": 0, "lines_without_the_figure": 0}


def test_the_comparison_pairs_cells_and_counts_only_the_pairs(tmp_path,
                                                              monkeypatch):
    """The protocol allows exactly this comparison and no wider one.

    A replication reporting its own cells next to v1's totals would be
    comparing a subset with a whole, which is what the protocol was frozen to
    prevent. A cell that ran in only one campaign is in neither the table nor
    its count.
    """
    import json

    v1 = tmp_path / "results"
    v1.mkdir()
    for task, final, passed in (("a", False, False), ("b", True, True)):
        (v1 / f"{task}.C.1.json").write_text(json.dumps({
            "task": task, "arm": "C", "rep": 1, "seconds": 1.0,
            "hidden_suite": {"passed": passed}, "false_accept": False,
            "produced_final_state": final,
        }))
    monkeypatch.setitem(bm.KAMPAGNEN, "v1", v1)

    zellen = [
        {"task": "a", "arm": "C", "hidden_suite": {"passed": True},
         "produced_final_state": True},
        # no v1 counterpart: must not appear
        {"task": "z", "arm": "C", "hidden_suite": {"passed": True},
         "produced_final_state": True},
    ]
    zeilen = bm._vergleich(zellen)
    text = "\n".join(zeilen)

    assert "`a`" in text
    assert "`z`" not in text, "a cell with no counterpart was compared anyway"
    assert "1 cell(s) appear on both sides" in text
    assert "**no**" in text, "v1's missing final state should stand out"
    assert "(excluded)" in text, (
        "a cell with no final state must not have its suite result reported "
        "as a correctness outcome -- that is the protocol's own exclusion rule")
    assert "| FAIL |" not in text, (
        "the excluded cell's suite result leaked into the comparison")


def test_a_campaign_with_no_counterparts_says_so(tmp_path, monkeypatch):
    v1 = tmp_path / "results"
    v1.mkdir()
    monkeypatch.setitem(bm.KAMPAGNEN, "v1", v1)
    zeilen = bm._vergleich([{"task": "z", "arm": "C",
                             "hidden_suite": {"passed": True},
                             "produced_final_state": True}])
    assert "No cell of this campaign has a v1 counterpart yet." in zeilen


def test_a_candidate_file_named_after_a_stdlib_module_cannot_change_the_verdict(
        tmp_path):
    """The hidden suite is the verdict, and the candidate could answer its imports.

    `python -m unittest` from inside the candidate puts the candidate at the
    front of `sys.path`, ahead of the standard library. A stub `unittest.py`
    beside a failing hidden suite flipped `hidden_suite.passed` to true and
    `false_accept` to false. No malice is needed: `types.py`, `copy.py`,
    `string.py`, `token.py` are all plausible names for an arm to write.
    """
    aufgabe = tmp_path / "tasks" / "shadow"
    aufgabe.mkdir(parents=True)
    (aufgabe / "hidden_test.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_it_fails(self):\n"
        "        self.assertEqual(1, 2)\n", encoding="utf-8")

    kandidat = tmp_path / "kandidat"
    kandidat.mkdir()
    (kandidat / "app.py").write_text("x = 1\n", encoding="utf-8")
    # The hijack: a module the suite imports, answered by the candidate.
    (kandidat / "unittest.py").write_text(
        "class TestCase:\n"
        "    def __init__(self, *a, **k): pass\n"
        "def main(*a, **k):\n"
        "    raise SystemExit(0)\n", encoding="utf-8")

    bm.AUFGABEN = tmp_path / "tasks"
    v = bm.hidden_verdict(kandidat, "shadow")

    assert not v["passed"], (
        "the candidate answered the suite's import and the verdict flipped")
    assert "unittest.py" in v["shadowed_stdlib_modules"]
