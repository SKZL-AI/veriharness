"""The programme-completeness gate: can a partial register certify itself?

O196. The previous completeness test asserted `{"P1", "P2", "P3"} <= phases`
where `phases` came from the register being tested. Every case here is built
so that the expected side comes from somewhere the register cannot touch:
either a fixture inventory written in this file, or the pinned plan parsed
from its bytes.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "program_scope_probe", ROOT / "tools" / "program_scope.py")
ps = importlib.util.module_from_spec(_spec)
sys.modules["program_scope_probe"] = ps
_spec.loader.exec_module(ps)

#: A two-phase programme with one capability in each, one non-capability item,
#: and a plan digest. Small enough that every control below changes exactly one
#: thing.
SCOPE = {
    "plan_sha256": "a" * 64,
    "phases": [
        {"phase": "P0", "items": 1, "capabilities": 0},
        {"phase": "P1", "items": 2, "capabilities": 1},
        {"phase": "P2", "items": 1, "capabilities": 1},
    ],
    "items": [
        {"id": "P0.deliver.baseline", "phase": "P0", "kind": "baseline_deliverable",
         "section": "Deliver:"},
        {"id": "P1.build.alpha", "phase": "P1", "kind": "capability",
         "section": "Build/verify:"},
        {"id": "P1.gate.exit", "phase": "P1", "kind": "gate",
         "section": "Exit includes:"},
        {"id": "P2.build.beta", "phase": "P2", "kind": "capability",
         "section": "Build/verify:"},
    ],
}
REGISTER = {"requirements": [
    {"id": "P1-01", "target_phase": "P1", "authoritative_item": "P1.build.alpha"},
    {"id": "P2-01", "target_phase": "P2", "authoritative_item": "P2.build.beta"},
]}


def _run(scope=SCOPE, register=REGISTER, sha="a" * 64):
    return ps.completeness(scope, register, sha)


def test_the_positive_control_a_complete_register_passes():
    """Without this every negative case would also pass against a gate that
    refused everything."""
    assert _run() == []


def test_1_removing_one_later_capability_is_caught():
    register = {"requirements": [REGISTER["requirements"][0]]}
    problems = _run(register=register)
    assert any("P2.build.beta" in p and "no requirement" in p for p in problems)


def test_2_a_whole_later_phase_disappearing_is_caught():
    """The failure the old test could not see: drop every P2 requirement and
    the register is still internally consistent."""
    register = {"requirements": [REGISTER["requirements"][0]]}
    problems = _run(register=register)
    assert any(p.startswith("P2: a capability phase with no requirement")
               for p in problems)


def test_2b_a_phase_vanishing_from_the_inventory_is_caught():
    scope = copy.deepcopy(SCOPE)
    scope["phases"].append({"phase": "P3", "items": 0, "capabilities": 0})
    assert any(p.startswith("P3: a phase with no items") for p in _run(scope=scope))


def test_3_a_duplicated_requirement_id_is_caught():
    register = copy.deepcopy(REGISTER)
    register["requirements"].append(
        {"id": "P1-01", "target_phase": "P1", "authoritative_item": "P1.gate.exit"})
    assert any("P1-01 is used 2 times" in p for p in _run(register=register))


def test_4_two_requirements_claiming_one_capability_is_caught():
    register = copy.deepcopy(REGISTER)
    register["requirements"].append(
        {"id": "P1-02", "target_phase": "P1", "authoritative_item": "P1.build.alpha"})
    problems = _run(register=register)
    assert any("P1.build.alpha is claimed by 2 requirements" in p for p in problems)


def test_5_a_phase_the_plan_does_not_have_is_caught():
    register = copy.deepcopy(REGISTER)
    register["requirements"][1]["target_phase"] = "P99"
    assert any("'P99' is not a phase of the plan" in p for p in _run(register=register))


def test_6_an_invented_requirement_is_surfaced():
    """Two shapes: no item at all, and an item the plan does not contain."""
    register = copy.deepcopy(REGISTER)
    register["requirements"].append({"id": "X-01", "target_phase": "P1"})
    register["requirements"].append(
        {"id": "X-02", "target_phase": "P1", "authoritative_item": "P1.build.gamma"})
    problems = _run(register=register)
    assert any("X-01: no authoritative_item" in p for p in problems)
    assert any("X-02: authoritative_item 'P1.build.gamma' is not an item" in p
               for p in problems)


def test_a_requirement_filed_under_the_wrong_phase_is_caught():
    register = copy.deepcopy(REGISTER)
    register["requirements"][0]["target_phase"] = "P2"
    assert any("P1-01: says P2 but its item is in P1" in p
               for p in _run(register=register))


def test_an_inventory_from_a_different_plan_is_refused():
    """The inventory is bound to the bytes it was parsed from. A plan that
    moved underneath it makes the whole comparison about something else."""
    assert any("re-derive it" in p for p in _run(sha="b" * 64))


def test_an_unclassified_item_fails_instead_of_disappearing():
    scope = copy.deepcopy(SCOPE)
    scope["items"].append({"id": "P1.mystery.x", "phase": "P1",
                           "kind": "UNCLASSIFIED", "section": "Somewhere:"})
    assert any("P1.mystery.x" in p and "classified" in p for p in _run(scope=scope))


def test_a_non_capability_item_needs_no_requirement():
    """The gate must not demand requirements for gates, invariants and
    validation steps -- that would force invented work to satisfy it."""
    problems = _run()
    assert not any("P1.gate.exit" in p for p in problems)


# --------------------------------------------------------------------------- #
# The parser, against the plan's own shapes
# --------------------------------------------------------------------------- #

def test_prose_is_joined_and_a_prose_only_phase_keeps_its_capability():
    """P11 is written entirely in prose. A parser that read only bullets made
    that whole phase vanish, which is precisely the failure O196 is about."""
    text = (
        "# P11 — v1.2 Calibrated Thing\n\n"
        "Promote only specific low-risk decision classes.\n\n"
        "Laya local first if evidence supports it.\n"
    )
    phases, items = ps.parse(text)
    assert [p["phase"] for p in phases] == ["P11"]
    assert phases[0]["version"] == "1.2"
    kinds = [i.kind for i in items]
    assert kinds.count("optional_capability") == 1
    assert "constraint" in kinds


def test_wrapped_prose_is_one_item_not_two():
    text = ("# P8 — VeriHarness Core 1.0\n\n"
            "Core 1.0 must natively execute safe project-level parallel waves; operator-launched\n"
            "parallel runs are not sufficient.\n")
    _, items = ps.parse(text)
    assert len(items) == 1 and items[0].kind == "release_property"


def test_an_unknown_section_is_unclassified_not_dropped():
    text = "# P4 — v0.5 Thing\n\nSomething nobody tabled:\n\n- a thing\n"
    _, items = ps.parse(text)
    assert [i.kind for i in items] == ["UNCLASSIFIED"]


def test_ids_are_stable_and_unique_within_a_phase():
    text = "# P2 — v0.3 Thing\n\nBuild/verify:\n\n- same\n- same\n"
    _, items = ps.parse(text)
    assert [i.item_id for i in items] == ["P2.build-verify.same",
                                          "P2.build-verify.same-2"]


def _plan():
    root = os.environ.get(ps.PLAN_ENV)
    plan = ps.plan_path(Path(root)) if root else None
    if plan is None:
        pytest.skip(f"the pinned plan is not reachable here; set ${ps.PLAN_ENV}")
    return plan


def test_the_real_plan_parses_into_thirteen_phases_with_nothing_unclassified():
    scope = ps.build(_plan())
    assert [p["phase"] for p in scope["phases"]] == [f"P{n}" for n in range(13)]
    assert scope["counts"]["unclassified"] == 0
    kinds = {p["phase"]: p["phase_kind"] for p in scope["phases"]}
    assert kinds["P0"] == "baseline"
    assert kinds["P7"] == "non-capability", "product proof builds nothing"
    assert kinds["P11"] == "capability", "the prose-only phase survives"


def test_a_retired_id_reused_by_a_live_requirement_is_caught():
    """Caught in this repository before it was a test: the first migration
    numbered new P3 requirements from the highest *live* id and reissued four
    retired ones, so one id named two different things."""
    register = copy.deepcopy(REGISTER)
    register["retired"] = [{"id": "P1-01", "reason": "split", "superseded_by": "P2-01"}]
    assert any("P1-01 is both retired and live" in p for p in _run(register=register))


def test_a_retired_entry_must_say_what_replaced_it():
    register = copy.deepcopy(REGISTER)
    register["retired"] = [{"id": "P1-09", "reason": "moved"}]
    assert any("retired P1-09: superseded_by None" in p for p in _run(register=register))
    register["retired"] = [{"id": "P1-09", "superseded_by": "P1-01"}]
    assert any("retired P1-09: no reason" in p for p in _run(register=register))


def test_a_v3_3_contract_nobody_carries_is_caught():
    """The package's own machine-readable list of what V3.3 adds. Several of
    those names are not a line of the production line, which is exactly why
    they are checked separately rather than trusted to the item mapping."""
    problems = ps.completeness(SCOPE, REGISTER, "a" * 64,
                               contracts=["WorkspaceLease"])
    assert any("'WorkspaceLease' is carried by no requirement" in p for p in problems)
    register = copy.deepcopy(REGISTER)
    register["requirements"][0]["contracts"] = ["WorkspaceLease"]
    assert ps.completeness(SCOPE, register, "a" * 64, contracts=["WorkspaceLease"]) == []


def test_the_real_register_is_complete_against_the_real_plan():
    """The positive control over the actual programme, with both expected
    sides parsed from the pinned plan and neither from the register."""
    plan = _plan()
    import json
    scope = ps.build(plan)
    register = json.loads(ps.REGISTER.read_text())
    master = json.loads((plan.parent.parent / ps.MASTER_FILE).read_text())
    problems = ps.completeness(scope, register, scope["plan_sha256"],
                               master["new_in_v3_3"])
    assert problems == [], problems[:5]
    # And the scope it was checked against is the whole programme, not P1-P3.
    phases = {i["phase"] for i in scope["items"] if i["capability"]}
    assert {"P4", "P5", "P6", "P8", "P9", "P10", "P11", "P12"} <= phases


def test_a_skipped_or_repeated_phase_is_caught():
    """P0..P12 in order. A gap is how a later phase goes missing without any
    item-level check noticing, because there is no item left to miss."""
    gap = copy.deepcopy(SCOPE)
    gap["phases"] = [gap["phases"][0], gap["phases"][2]]       # P0, P2
    assert any("not P0..P" in p for p in _run(scope=gap))
    twice = copy.deepcopy(SCOPE)
    twice["phases"].insert(2, dict(twice["phases"][1]))         # P1 twice
    assert any("not P0..P" in p for p in _run(scope=twice))
    late = copy.deepcopy(SCOPE)
    late["phases"] = late["phases"][1:]                          # starts at P1
    assert any("not P0..P" in p for p in _run(scope=late))

def test_the_same_line_twice_in_one_section_fails_and_in_two_sections_does_not():
    same = "# P2 — v0.3 Thing\n\nBuild/verify:\n\n- same\n- same\n"
    _, items = ps.parse(same)
    assert [i.kind for i in items] == ["capability", "DUPLICATE"]
    scope = {"plan_sha256": None, "phases": [{"phase": "P0", "items": 0,
                                              "capabilities": 0}],
             "items": [i.as_dict() for i in items]}
    assert any("appears twice in one section" in p
               for p in ps.completeness(scope, {"requirements": []}, None))

    split = "# P4 — v0.5 A\n\nProduct:\n\n- evidence pack\n\n# P5 — v0.6 B\n\nProduct:\n\n- evidence pack\n"
    _, items = ps.parse(split)
    assert [i.kind for i in items] == ["product_capability", "product_capability"]
    assert len({i.item_id for i in items}) == 2


def test_every_capability_item_has_exactly_one_disposition():
    """Silence is not a disposition. Every plan item is either mapped to a
    requirement or carries a non-capability kind, and the dispositions
    document says which for every one of them."""
    plan = _plan()
    import json
    scope = ps.build(plan)
    register = json.loads(ps.REGISTER.read_text())
    rows = ps.dispositions(scope, register)
    assert len(rows) == len(scope["items"])
    for row in rows:
        assert row["disposition"], row
        if row["capability"]:
            assert row["requirement"], row
        else:
            assert row["disposition"].startswith("non-capability"), row
