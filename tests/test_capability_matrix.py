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
            "ui_only": False, "authoritative_item": "X.item", "probe": probe}
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


# --------------------------------------------------------------------------- #
# Evidence probes that speak for part of a record (P2 exit-includes)
# --------------------------------------------------------------------------- #

def _record(tmp_path, monkeypatch, data, *, rederives=True, changed=(), dirty=False):
    import subprocess
    import types

    ev = ROOT / "dogfood"                    # any in-tree directory will do
    rec = tmp_path / "record.json"
    data = {"evidence_head": "abc", "evidence_dirty": dirty, **data}
    rec.write_text(json.dumps(data))
    monkeypatch.setattr(cm, "_inside", lambda p: rec if p == "R" else ev)
    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0 if rederives else 1, "", ""))
    fake = types.ModuleType("durability_e2e")
    fake.stale_paths = lambda head: list(changed)
    monkeypatch.setitem(sys.modules, "durability_e2e", fake)


def _probe(**kw):
    return {"kind": "evidence", "path": "R", "evidence_dir": "E",
            "deriver": "durability_e2e.py", **kw}


def test_a_keyed_probe_reads_only_its_requirement(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"requirements": {"P2-33": {"satisfied": True},
                                                     "P2-34": {"satisfied": False}}})
    assert cm.evidence_status(_probe(key="P2-33"))[0] == cm.PROVEN
    assert cm.evidence_status(_probe(key="P2-34"))[0] == cm.IMPLEMENTED_NOT_PROVEN
    assert cm.evidence_status(_probe(key="P2-99"))[0] == cm.IMPLEMENTED_NOT_PROVEN


def test_a_stressor_probe_needs_pass_not_not_run(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"stressors": {"DS1": "PASS", "DS2": "NOT_RUN"}})
    assert cm.evidence_status(_probe(key="stressor:DS1"))[0] == cm.PROVEN
    assert cm.evidence_status(_probe(key="stressor:DS2"))[0] == cm.IMPLEMENTED_NOT_PROVEN


def test_stale_dirty_or_underivable_evidence_is_not_proof(tmp_path, monkeypatch):
    good = {"requirements": {"P2-33": {"satisfied": True}}}
    for kw in ({"changed": ["src/hoh/succession.py"]}, {"dirty": True},
               {"rederives": False}):
        _record(tmp_path, monkeypatch, good, **kw)
        status, note = cm.evidence_status(_probe(key="P2-33"))
        assert status == cm.IMPLEMENTED_NOT_PROVEN, (kw, note)


def test_an_unlisted_deriver_is_refused(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"requirement_satisfied": True})
    assert cm.evidence_status(_probe(deriver="arbitrary_tool.py"))[0] == cm.NOT_DETERMINABLE


# --------------------------------------------------------------------------- #
# The key is bound to the requirement that carries the probe
# --------------------------------------------------------------------------- #

_P2_33 = "P2.exit-includes.unattended-test"
_P2_35 = "P2.exit-includes.controller-session-replacement"


def _p2_record(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"requirements": {
        "P2-33": {"authoritative_item": _P2_33, "satisfied": True},
        "P2-35": {"authoritative_item": _P2_35, "satisfied": True}}})


def test_a_key_names_another_requirement_is_refused(tmp_path, monkeypatch):
    _p2_record(tmp_path, monkeypatch)
    req = _req({}, id="P2-33", authoritative_item=_P2_33)
    status, note = cm.evidence_status(_probe(key="P2-33"), requirement=req)
    assert status == cm.PROVEN, note
    status, note = cm.evidence_status(_probe(key="P2-35"), requirement=req)
    assert status == cm.NOT_DETERMINABLE, note
    assert "P2-35" in note and "P2-33" in note, note
    assert _P2_33 in note and _P2_35 in note, note


def test_a_key_names_another_requirement_unless_the_record_names_its_item(
        tmp_path, monkeypatch):
    """A record entry that is missing, or names no item, cannot be bound."""
    _record(tmp_path, monkeypatch, {"requirements": {
        "P2-33": {"satisfied": True}, "P2-34": "not an object"}})
    req = _req({}, id="P2-33", authoritative_item=_P2_33)
    for key in ("P2-33", "P2-34", "P2-99"):
        status, note = cm.evidence_status(_probe(key=key), requirement=req)
        assert status == cm.NOT_DETERMINABLE, (key, note)
    # ... and neither does a register entry that names no item of its own.
    _p2_record(tmp_path, monkeypatch)
    bare = {k: v for k, v in req.items() if k != "authoritative_item"}
    assert cm.evidence_status(_probe(key="P2-33"),
                              requirement=bare)[0] == cm.NOT_DETERMINABLE


def test_classify_forwards_the_entry_so_a_key_names_another_is_refused(
        tmp_path, monkeypatch):
    _p2_record(tmp_path, monkeypatch)
    right = cm.classify(_req(_probe(key="P2-33"), id="P2-33",
                             authoritative_item=_P2_33), {}, None, None)
    wrong = cm.classify(_req(_probe(key="P2-35"), id="P2-33",
                             authoritative_item=_P2_33), {}, None, None)
    assert right["status"] == cm.PROVEN, right
    assert wrong["status"] == cm.NOT_DETERMINABLE, wrong


def test_a_stressor_key_needs_a_matching_stressor_for(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"stressors": {"DS1": "PASS"}})
    req = _req({}, id="P2-11", authoritative_item="P2.build-verify.crash-reboot-recovery")
    status, note = cm.evidence_status(
        _probe(key="stressor:DS1", stressor_for="P2-11"), requirement=req)
    assert status == cm.PROVEN, note
    status, note = cm.evidence_status(_probe(key="stressor:DS1"), requirement=req)
    assert status == cm.NOT_DETERMINABLE, note
    assert "stressor:DS1" in note and "P2-11" in note and "stressor_for" in note, note
    status, note = cm.evidence_status(
        _probe(key="stressor:DS1", stressor_for="P2-35"), requirement=req)
    assert status == cm.NOT_DETERMINABLE, note
    assert "stressor:DS1" in note and "P2-11" in note and "P2-35" in note, note


def test_a_keyless_durability_probe_is_refused(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, {"requirement_satisfied": True})
    req = _req({}, id="P2-33", authoritative_item=_P2_33)
    for kw in ({}, {"requirement": req}):
        status, note = cm.evidence_status(_probe(), **kw)
        assert status == cm.NOT_DETERMINABLE, (kw, note)
        assert "several requirements" in note, note


def test_a_keyless_default_deriver_probe_reads_as_before(tmp_path, monkeypatch):
    """P1-16: the default deriver, no key, the top-level flag -- unchanged."""
    _record(tmp_path, monkeypatch, {"requirement_satisfied": True})
    probe = {k: v for k, v in _probe().items() if k != "deriver"}
    req = _req(probe, id="P1-16", authoritative_item="P1.product-requirement.fresh-clone")
    assert cm.evidence_status(probe)[0] == cm.PROVEN
    assert cm.evidence_status(probe, requirement=req)[0] == cm.PROVEN
