"""The build plan: does it ever produce an order that cannot be executed?

The layering is the whole artifact, so every case here is about a shape that
would let a bad order look like a good one.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_plan_probe", ROOT / "tools" / "build_plan.py")
bp = importlib.util.module_from_spec(_spec)
sys.modules["build_plan_probe"] = bp
_spec.loader.exec_module(bp)

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def test_layers_put_a_dependency_before_what_needs_it():
    waves, unplaced = bp.layers({"a": [], "b": ["a"], "c": ["b"], "d": []})
    assert not unplaced
    order = {item: i for i, wave in enumerate(waves) for item in wave}
    assert order["a"] < order["b"] < order["c"]
    assert order["d"] == 0, "an item with no dependency is ready now"


def test_a_cycle_is_reported_and_not_quietly_broken():
    """A topological sort that drops an edge to make progress produces a plan
    containing a step nobody can take."""
    waves, unplaced = bp.layers({"a": ["b"], "b": ["a"], "c": []})
    assert unplaced == ["a", "b"]
    assert waves == [["c"]]


def test_an_item_depending_on_something_outside_the_set_is_unplaceable():
    waves, unplaced = bp.layers({"a": ["ghost"]})
    assert unplaced == ["a"] and waves == []


def test_a_proven_dependency_is_satisfied_and_not_a_step():
    """Half the point of deriving the order from the matrix: work that is
    already done must not appear in the plan as a predecessor to wait for."""
    body = {"capabilities": [
        {"id": "done", "name": "d", "target_phase": "P1", "status": "PROVEN",
         "core_required_1_0": True, "depends_on": []},
        {"id": "open", "name": "o", "target_phase": "P2", "status": "MISSING",
         "core_required_1_0": True, "depends_on": ["done"]},
    ]}
    plan = bp.build(body)
    assert plan["open_count"] == 1
    assert plan["waves"][0]["items"][0]["id"] == "open"
    assert plan["waves"][0]["items"][0]["depends_on"] == []
    assert not plan["unplaceable"] and not plan["dangling_dependencies"]


def test_a_dependency_on_a_requirement_that_does_not_exist_is_reported():
    body = {"capabilities": [
        {"id": "open", "name": "o", "target_phase": "P2", "status": "MISSING",
         "core_required_1_0": True, "depends_on": ["typo-01"]},
    ]}
    plan = bp.build(body)
    assert plan["dangling_dependencies"] == {"open": ["typo-01"]}


def test_the_checklist_carries_only_open_work_and_says_what_closes_it():
    body = {"capabilities": [
        {"id": "done", "name": "d", "target_phase": "P1", "status": "PROVEN",
         "core_required_1_0": True, "depends_on": []},
        {"id": "open", "name": "o", "target_phase": "P2", "status": "PARTIAL",
         "core_required_1_0": False, "depends_on": []},
    ]}
    check = bp.checklist(bp.build(body))
    assert [i["item_id"] for i in check["items"]] == ["open"]
    assert check["items"][0]["phase_blocking"] is False
    assert check["items"][0]["closes_when"]
    assert check["blocking_count"] == 0


def test_the_real_register_produces_an_executable_order():
    """The positive control over the actual plan: no cycle, no dangling
    dependency, and the parallel wave executor after the pieces it needs."""
    needs_evidence(PROGRAM_REGISTER)
    cm = bp._matrix()
    body = cm.measure()
    import json
    register = json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
    deps = {r["id"]: r.get("depends_on") or [] for r in register["requirements"]}
    for r in body["capabilities"]:
        r["depends_on"] = deps.get(r["id"], [])
    plan = bp.build(body)
    assert not plan["unplaceable"], plan["unplaceable"]
    assert not plan["dangling_dependencies"], plan["dangling_dependencies"]
    where = {item["id"]: wave["wave"]
             for wave in plan["waves"] for item in wave["items"]}
    assert where["P3-02"] > where["P3-01"], "the executor needs its planner"
    assert where["P3-02"] > where["P3-04"], "and its budget reservation"
    assert where["P3-07"] > where["P3-02"], "backpressure needs a wave to slow"


def _cap(rid, phase, status="MISSING", **kw):
    base = {"id": rid, "name": rid, "target_phase": phase, "status": status,
            "core_required_1_0": True, "depends_on": []}
    base.update(kw)
    return base


def test_a_later_phase_waits_for_the_earlier_phase_gate():
    """The plan is "the authoritative implementation sequence": a P3 item is
    not ready while a blocking P2 item is open, however independent it is."""
    body = {"capabilities": [_cap("P2-01", "P2"), _cap("P3-01", "P3")]}
    plan = bp.build(body, ["P2", "P3"], {})
    assert plan["ready_now"] == ["P2-01"]
    where = {i["id"]: w["wave"] for w in plan["waves"] for i in w["items"]}
    assert where["P3-01"] > where["P2-01"]


def test_a_proven_phase_does_not_hold_the_next_one():
    body = {"capabilities": [_cap("P2-01", "P2", "PROVEN"), _cap("P3-01", "P3")]}
    assert bp.build(body, ["P2", "P3"], {})["ready_now"] == ["P3-01"]


def test_an_unmet_external_gate_holds_everything_after_it():
    """P7's product proof is not a requirement anybody implements, so nothing
    in the register can close it. It must hold P8 shut rather than vanish."""
    body = {"capabilities": [_cap("P6-01", "P6", "PROVEN"), _cap("P8-01", "P8")]}
    plan = bp.build(body, ["P6", "P7", "P8"],
                    {"P7": (False, "product proof not run")})
    assert plan["ready_now"] == []
    assert plan["held_by_phase_gate"] == ["P8-01"]
    assert plan["phase_gates"]["P7"]["external"] == "product proof not run"
    assert not plan["unplaceable"], "held is not the same as impossible"


def test_optional_and_ui_items_do_not_hold_a_phase_gate():
    """System-One starts in SHADOW and the early UI is explicitly not
    authoritative, so an open item of either kind must not block the phase."""
    body = {"capabilities": [
        _cap("P3-01", "P3", "PROVEN"),
        _cap("P3-02", "P3", optional_provider=True),
        _cap("P3-03", "P3", ui_only=True),
        _cap("P4-01", "P4")]}
    plan = bp.build(body, ["P3", "P4"], {})
    assert "P4-01" in plan["ready_now"]


def test_a_waiver_needs_a_document_to_count():
    """"VERIFIED or validly WAIVED": a waiver with no document is not valid."""
    body = {"capabilities": [_cap("P1-01", "P1", "PARTIAL",
                                  waiver={"reason": "later"}),
                             _cap("P2-01", "P2")]}
    assert bp.build(body, ["P1", "P2"], {})["ready_now"] == ["P1-01"]
    body["capabilities"][0]["waiver"] = {"reason": "later",
                                         "document": "docs/ROUTING.md"}
    assert bp.build(body, ["P1", "P2"], {})["ready_now"] == ["P2-01"]


def test_the_real_programme_starts_where_the_plan_says_it_does():
    """The positive control over the actual register and inventory: the only
    thing ready is the earliest open blocking item, and later phases are held
    by gates that name their reason rather than disappearing."""
    needs_evidence(PROGRAM_REGISTER)
    order, gates = bp.phase_gates()
    assert order == [f"P{n}" for n in range(13)]
    assert gates["P0"][0] is True, gates["P0"][1]
    assert gates["P7"][0] is False, "product proof is not measured yet"


def test_an_item_held_by_a_gate_is_still_on_the_checklist():
    """O196's second shape. The checklist was built from the dependency
    layers, so the moment an early gate stayed shut, everything behind it
    vanished from the list of work -- "not ready" silently became "not
    represented"."""
    body = {"capabilities": [_cap("P6-01", "P6", "PROVEN"), _cap("P8-01", "P8"),
                             _cap("P9-01", "P9")]}
    plan = bp.build(body, ["P6", "P7", "P8", "P9"],
                    {"P7": (False, "product proof not run")})
    ids = {i["item_id"] for i in bp.checklist(plan)["items"]}
    assert ids == {"P8-01", "P9-01"}
    held = {i["item_id"]: i for i in bp.checklist(plan)["items"]}
    assert held["P8-01"]["ready_now"] is False
    assert held["P8-01"]["held_by_phase_gate"] == ["P7"]
