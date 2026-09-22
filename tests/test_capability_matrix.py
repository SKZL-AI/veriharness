"""The capability matrix: can a status be better than its evidence?

Every case here tries to get a stronger word out of the classifier than the
probe earned. The register is large and mostly negative today, which is
exactly the condition under which an over-generous rule would go unnoticed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "capability_probe", ROOT / "tools" / "capability_matrix.py")
cm = importlib.util.module_from_spec(_spec)
sys.modules["capability_probe"] = cm
_spec.loader.exec_module(cm)

# O185/O193. This file ships in the export and the register does not, so the
# module cannot read it at import time: a published clone would fail
# collection rather than skip. The fixture answers the question the conftest
# rule exists for -- withheld on purpose, named, with the reason.
from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def _register() -> dict:
    needs_evidence(PROGRAM_REGISTER)
    return json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())


def _req(probe: dict, **kw) -> dict:
    base = {"id": "X-01", "name": "x", "domain": "d", "target_phase": "P1",
            "core_required_1_0": True, "product_required_1_0": False,
            "autonomy_required": False, "optional_provider": False,
            "ui_only": False, "probe": probe}
    base.update(kw)
    return base


def test_a_named_equivalent_raises_missing_and_never_reaches_proven():
    """An equivalent is a thing that stands in for another thing. It has not
    been shown to *be* that thing, so the most it can buy is PARTIAL."""
    without = cm.classify(_req({"kind": "symbol", "symbol": "NotHere"}),
                          {}, ("", True), {})
    assert without["status"] == cm.MISSING

    with_eq = cm.classify(
        _req({"kind": "symbol", "symbol": "NotHere",
              "equivalent": "something adjacent does part of it"}),
        {}, ("", True), {})
    assert with_eq["status"] == cm.PARTIAL
    assert with_eq["equivalent_here"]

    # And it cannot lift a measured status either.
    nothing = cm.classify(_req({"kind": "none", "equivalent": "adjacent"}),
                          {}, ("", True), {})
    assert nothing["status"] == cm.PARTIAL


def test_a_symbol_without_its_test_is_not_proven():
    """The doctrine is explicit: implementation *and* test. A class that
    exists and a suite that never touches it is the oldest way to look ready."""
    symbols = {"Thing": ["src/hoh/x.py:10"]}
    proven = cm.classify(_req({"kind": "symbol", "symbol": "Thing",
                               "test": "tests/test_x.py"}),
                         symbols, ("tests/test_x.py::test_a", True), {})
    assert proven["status"] == cm.PROVEN

    unproven = cm.classify(_req({"kind": "symbol", "symbol": "Thing",
                                 "test": "tests/test_x.py"}),
                           symbols, ("tests/test_other.py::test_b", True), {})
    assert unproven["status"] == cm.IMPLEMENTED_NOT_PROVEN
    assert any("NOT collected" in e for e in unproven["evidence"])


def test_a_board_row_is_refused_as_a_probe():
    """O197. A board row answers "is this green right now"; the matrix answers
    "does the capability exist and is it tested". Reading one to decide the
    other made the board and the matrix a cycle that did not converge in four
    passes -- and it over-credited three capabilities whose rows passed for
    reasons that were not the capability (a recorded routing *deferral*, a
    doctor that exists without the end-to-end path, a preflight nothing
    obeys). A `row` probe now comes back NOT_DETERMINABLE, whatever the
    board says."""
    board = {"green_row": ("PASS", "measured")}
    r = cm.classify(_req({"kind": "row", "row": "green_row"}), {}, ("", True), board)
    assert r["status"] == cm.NOT_DETERMINABLE
    assert any("O197" in e for e in r["evidence"])


def test_the_matrix_never_reads_the_board():
    """The structural half: not just refused in `classify`, but not read at
    all, so no future probe kind can quietly reintroduce the cycle."""
    source = (ROOT / "tools" / "capability_matrix.py").read_text()
    measure = source[source.index("def measure()"):]
    measure = measure[:measure.index("\ndef ")]
    assert "_board()" not in measure, "measure() reads the board again"


def test_the_register_uses_no_board_rows():
    needs_evidence(PROGRAM_REGISTER)
    rows = [r["id"] for r in _register()["requirements"]
            if (r.get("probe") or {}).get("kind") == "row"]
    assert not rows, rows

def test_a_probe_that_could_not_run_is_not_a_finding_about_the_software():
    """NOT_DETERMINABLE, never MISSING. The two sentences are different and
    only one of them is about this repository."""
    r = cm.classify(_req({"kind": "row", "row": "anything"}), {}, ("", True), {})
    assert r["status"] == cm.NOT_DETERMINABLE
    r = cm.classify(_req({"kind": "test", "test": "tests/x.py::y"}), {}, None, {})
    assert r["status"] == cm.NOT_DETERMINABLE
    r = cm.classify(_req({"kind": "invented"}), {}, ("", True), {})
    assert r["status"] == cm.NOT_DETERMINABLE


def test_every_requirement_in_the_register_has_a_probe_the_tool_understands():
    """A register entry the classifier cannot read would come out
    NOT_DETERMINABLE forever and nobody would notice it was a typo."""
    known = {"none", "symbol", "method", "test", "row", "evidence"}
    for req in _register()["requirements"]:
        kind = (req.get("probe") or {}).get("kind")
        assert kind in known, (req["id"], kind)
        assert req["id"] and req["name"] and req["target_phase"]


def test_the_register_covers_the_phases_the_plan_names():
    """Superseded 2026-09-22 (O196). This used to assert
    `{"P1", "P2", "P3"} <= phases` where `phases` came from the register under
    test -- so a register that dropped P4 through P12 passed it, and so did
    every artefact derived from that register. Completeness is now decided by
    `tools/program_scope.py` against an inventory parsed from the pinned plan
    (`tests/test_program_scope.py`); what stays here is only what the matrix
    itself needs: every requirement names the plan item it implements."""
    register = _register()
    assert register.get("scope_plan_sha256"), "not reconciled against the plan"
    for r in register["requirements"]:
        assert r.get("authoritative_item"), r["id"]
def test_the_counts_add_up_to_the_register():
    """A summary that loses a row is how a gap disappears."""
    needs_evidence(PROGRAM_REGISTER)
    body = cm.measure()
    register = _register()
    assert sum(body["counts"].values()) == len(register["requirements"])
    assert len(body["capabilities"]) == len(register["requirements"])


def test_the_gap_register_lists_everything_that_is_not_proven():
    needs_evidence(PROGRAM_REGISTER)
    body = cm.measure()
    text = cm.gap_register(body)
    not_proven = [r["id"] for r in body["capabilities"] if r["status"] != cm.PROVEN]
    for rid in not_proven:
        assert f"`{rid}`" in text, rid
    proven = [r["id"] for r in body["capabilities"] if r["status"] == cm.PROVEN]
    for rid in proven:
        assert f"| `{rid}` |" not in text, rid


def test_this_tree_is_the_baseline_the_plan_was_written_against():
    """The positive control. V3.3 exists because the parallelism contracts are
    not there; if they ever are, this test is the first thing that says so."""
    needs_evidence(PROGRAM_REGISTER)
    body = cm.measure()
    by_id = {r["id"]: r["status"] for r in body["capabilities"]}
    assert by_id["P3-02"] == cm.MISSING, "ParallelWaveExecutor"
    # ExecutionPreflight measures and reports; nothing refuses to start a run
    # on its verdict yet, so it is PARTIAL -- the board row that said PASS
    # was answering a different question (O197).
    assert by_id["P3-11"] == cm.PARTIAL, "ExecutionPreflight is not wired in"
    # The capability report was filed as P3-10 until the register was
    # reconciled with the plan (O196); the plan files it under P5.
    assert by_id["P5-17"] == cm.PROVEN, "the truthful isolation report is"
    assert by_id["P1-07"] == cm.PROVEN, "the sandbox"
