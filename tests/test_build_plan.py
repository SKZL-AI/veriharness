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
