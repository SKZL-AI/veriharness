"""The build plan: does it ever produce an order that cannot be executed?

The layering is the whole artifact, so every case here is about a shape that
would let a bad order look like a good one.
"""
from __future__ import annotations

import importlib.util
import json
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


# --------------------------------------------------------------------------- #
# Gate binding (O220/F2, M5): a gate-kind plan item is measured through the
# requirement of its own phase that claims it.
# --------------------------------------------------------------------------- #

def _gate_item(item_id, phase="P2", kind="gate", text=None):
    return {"id": item_id, "phase": phase, "kind": kind, "text": text or item_id,
            "capability": False}


def _gate_scope(*items):
    """Phases P1..P3 with the given gate items and nothing else."""
    return {"phases": [{"phase": p} for p in ("P1", "P2", "P3")], "items": list(items)}


def _req(rid, phase, item=None, also=()):
    return {"id": rid, "name": rid, "target_phase": phase, "authoritative_item": item,
            "also_covers": list(also), "core_required_1_0": True}


def _row(rid, status, phase="P2", **kw):
    return _cap(rid, phase, status, **kw)


def _gates(monkeypatch, items, requirements, capabilities):
    monkeypatch.setattr(bp, "_scope", lambda: _gate_scope(*items))
    return bp.phase_gates(capabilities, requirements)[1]


def test_a_gate_item_bound_to_a_proven_requirement_is_met(monkeypatch):
    gates = _gates(monkeypatch, [_gate_item("P2.exit.a")],
                   [_req("P2-33", "P2", "P2.exit.a")], [_row("P2-33", "PROVEN")])
    assert gates["P2"][0] is True, gates["P2"][1]
    assert gates["P2"][1] == "1 exit condition(s), each bound to a PROVEN requirement"


def test_a_bound_gate_item_is_measured_not_assumed(monkeypatch):
    """Bound is not met: the matrix decides, and no row is no measurement."""
    items, reqs = [_gate_item("P2.exit.a")], [_req("P2-33", "P2", "P2.exit.a")]
    met, why = _gates(monkeypatch, items, reqs,
                      [_row("P2-33", "IMPLEMENTED_NOT_PROVEN")])["P2"]
    assert met is False and "P2-33 IMPLEMENTED_NOT_PROVEN" in why, why
    met, why = _gates(monkeypatch, items, reqs, None)["P2"]
    assert met is False and "P2-33 not measured" in why, why


def test_an_unbound_gate_item_still_counts_as_unmeasured(monkeypatch):
    met, why = _gates(monkeypatch,
                      [_gate_item("P2.exit.a"), _gate_item("P2.exit.free")],
                      [_req("P2-33", "P2", "P2.exit.a")],
                      [_row("P2-33", "PROVEN")])["P2"]
    assert met is False
    assert "1 exit condition(s) nothing here measures yet" in why, why
    assert "not PROVEN" not in why, why


def test_a_waiver_does_not_meet_an_exit_condition(monkeypatch):
    """A documented waiver satisfies a requirement for the build order; it does
    not run the exit test the plan names (DECISIONS Q1)."""
    row = _row("P2-33", "PARTIAL",
               waiver={"reason": "later", "document": "docs/ROUTING.md"})
    met, why = _gates(monkeypatch, [_gate_item("P2.exit.a")],
                      [_req("P2-33", "P2", "P2.exit.a")], [row])["P2"]
    assert met is False and "P2-33 PARTIAL" in why, why


def test_a_claim_from_another_phase_binds_nothing(monkeypatch):
    met, why = _gates(monkeypatch, [_gate_item("P2.exit.a")],
                      [_req("P1-01", "P1", "P2.exit.a")],
                      [_row("P1-01", "PROVEN", phase="P1")])["P2"]
    assert met is False and "nothing here measures yet" in why, why
    assert bp.gate_bindings([_gate_item("P2.exit.a")],
                            [_req("P1-01", "P1", "P2.exit.a")]) == {"P2.exit.a": []}


def test_also_covers_binds_like_the_authoritative_item(monkeypatch):
    items = [_gate_item("P2.exit.a"), _gate_item("P2.exit.b")]
    reqs = [_req("P2-33", "P2", "P2.exit.a", also=["P2.exit.b"])]
    assert bp.gate_bindings(items, reqs) == {"P2.exit.a": ["P2-33"],
                                             "P2.exit.b": ["P2-33"]}
    met, why = _gates(monkeypatch, items, reqs, [_row("P2-33", "PROVEN")])["P2"]
    assert met is True, why


def test_every_requirement_bound_to_an_item_must_be_proven(monkeypatch):
    met, why = _gates(monkeypatch, [_gate_item("P2.exit.a")],
                      [_req("P2-33", "P2", "P2.exit.a"),
                       _req("P2-34", "P2", "P2.exit.other", also=["P2.exit.a"])],
                      [_row("P2-33", "PROVEN"), _row("P2-34", "MISSING")])["P2"]
    assert met is False and "P2-34 MISSING" in why, why


def test_the_count_is_of_bound_items_not_met(monkeypatch):
    met, why = _gates(monkeypatch, [_gate_item("P2.exit.a"), _gate_item("P2.exit.b")],
                      [_req("P2-33", "P2", "P2.exit.a"), _req("P2-34", "P2", "P2.exit.b")],
                      [_row("P2-33", "PROVEN"), _row("P2-34", "MISSING")])["P2"]
    assert met is False
    assert why == "1 exit condition(s) bound to requirement(s) not PROVEN: P2-34 MISSING"


def test_a_phase_without_gate_items_has_no_gate_entry(monkeypatch):
    gates = _gates(monkeypatch, [_gate_item("P2.exit.a")],
                   [_req("P2-33", "P2", "P2.exit.a")], [_row("P2-33", "PROVEN")])
    assert set(gates) == {"P2"}


def test_the_command_line_measures_the_bound_gate_items(tmp_path, monkeypatch, capsys):
    """`main` hands the measured rows and the register to `phase_gates`; called
    bare, every bound item would read as not measured."""
    register = tmp_path / "program/v3_3/REQUIREMENTS.json"
    register.parent.mkdir(parents=True)
    register.write_text(json.dumps({"requirements": [
        _req("P2-33", "P2", "P2.exit.a")]}), encoding="utf-8")
    monkeypatch.setattr(bp, "HOH", tmp_path)
    monkeypatch.setattr(bp, "_scope", lambda: _gate_scope(_gate_item("P2.exit.a")))

    def matrix_with(status):
        class _Missing(Exception):
            pass

        class _Matrix:
            RegisterMissing = _Missing

            @staticmethod
            def measure():
                return {"capabilities": [_row("P2-33", status)]}
        return lambda: _Matrix

    monkeypatch.setattr(bp, "_matrix", matrix_with("IMPLEMENTED_NOT_PROVEN"))
    assert bp.main([]) == 0
    out = capsys.readouterr().out
    assert "P2-33 IMPLEMENTED_NOT_PROVEN" in out, out

    monkeypatch.setattr(bp, "_matrix", matrix_with("PROVEN"))
    assert bp.main([]) == 0
    out = capsys.readouterr().out
    assert "gate P2" not in out, out


def test_the_real_exit_includes_are_bound_and_the_checklist_gates_are_not():
    """The positive control over the real inventory and register, nothing stubbed:
    P2's five exit-includes are bound to P2-33..37, its three checklist gates to
    nothing -- no requirement claims them, and none may (DECISIONS Q2). They are
    not "unmeasured" either: the programme's own instruments measure them
    (`tests/test_build_plan_checklist_gate.py`), so what the gate says about them
    is a measured reason, or a met condition once the closure has happened."""
    needs_evidence(PROGRAM_REGISTER)
    scope = bp._scope()
    requirements = bp._register_requirements()
    bindings = bp.gate_bindings(scope["items"], requirements)
    exits = {k: v for k, v in bindings.items() if k.startswith("P2.exit-includes.")}
    assert len(exits) == 5, exits
    assert sorted(rid for v in exits.values() for rid in v) == \
        [f"P2-{n}" for n in range(33, 38)]
    assert all(len(v) == 1 for v in exits.values()), exits
    gates_ = {k: v for k, v in bindings.items() if k.startswith("P2.checklist-gate.")}
    assert len(gates_) == 3 and all(v == [] for v in gates_.values()), gates_

    proven = [_row(r["id"], "PROVEN", phase=r["target_phase"]) for r in requirements]
    met, why = bp.phase_gates(proven, requirements)[1]["P2"]
    assert "nothing here measures yet" not in why, why
    measures = bp.checklist_gate_measures()
    assert met is all(answer for answer, _reason in measures.values()), (met, why)


# --- the five P0 measurement rows read their status back ----------------------

_P0_MEASURE_IDS = (
    "P0.measure.fresh-install-path",
    "P0.measure.current-cli-commands",
    "P0.measure.current-service-runtime-model",
    "P0.measure.current-human-intervention-points",
    "P0.measure.existing-long-horizon-dogfood-evidence",
)
_HEADINGS = {
    "fresh-install-path": "Fresh install path",
    "current-cli-commands": "Current CLI commands",
    "current-service-and-runtime-model": "Current service and runtime model",
    "current-human-intervention-points": "Current human intervention points",
    "existing-long-horizon-dogfood-evidence":
        "Existing long-horizon dogfood evidence",
}


def _baseline(tmp_path, sections, monkeypatch=None):
    """A current baseline with the given {heading: body} sections, and `a.txt`
    (two lines) for them to cite, under a tree that `bp.HOH` is pointed at."""
    doc = tmp_path / "program/v3_3/VERIHARNESS_CURRENT_BASELINE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Current baseline\n\n" + "".join(
        f"## {heading}\n\n{body}\n\n" for heading, body in sections.items()))
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    return tmp_path


def _point_at(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "HOH", tmp_path)


def test_a_measurement_is_measured_only_when_its_section_reads_back(
        tmp_path, monkeypatch):
    _baseline(tmp_path, {
        "Fresh install path": "* a figure — `a.txt:1`",
        "Current CLI commands": "* NOT_MEASURED — the CLI did not answer",
        "Current service and runtime model": "* a — `a.txt:1; a.txt:9`",
        "Current human intervention points": "A sentence, and no figure.",
    })
    _point_at(monkeypatch, tmp_path)
    assert bp.measurement_status("P0.measure.fresh-install-path") == (
        "measured: VERIHARNESS_CURRENT_BASELINE.md#fresh-install-path")
    assert bp.measurement_status("P0.measure.current-cli-commands") == (
        "NOT MEASURED: the section reports NOT_MEASURED")
    assert bp.measurement_status("P0.measure.current-service-runtime-model") == (
        "NOT MEASURED: cited line a.txt:9 does not exist")
    assert bp.measurement_status(
        "P0.measure.current-human-intervention-points") == (
        "NOT MEASURED: the section has no figure with a source")
    assert bp.measurement_status(
        "P0.measure.existing-long-horizon-dogfood-evidence") == (
        "NOT MEASURED: no section `Existing long-horizon dogfood evidence`")


def test_without_the_baseline_no_measurement_is_measured(tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path)
    for item in _P0_MEASURE_IDS:
        assert bp.measurement_status(item) == (
            "NOT MEASURED: VERIHARNESS_CURRENT_BASELINE.md is missing"), item
    _baseline(tmp_path, {"Fresh install path": "* a — `a.txt:1`"})
    assert bp.measurement_status("P0.measure.no-such-item").startswith(
        "NOT MEASURED: no section")


def _p0_rows(text):
    rows, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.startswith("## P0 ")
        elif inside and line.startswith("| ") and not line.startswith("| line"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def test_the_dispositions_table_shows_each_measurement_row_status(monkeypatch):
    needs_evidence(PROGRAM_REGISTER)
    monkeypatch.setattr(bp, "measurement_status", lambda i: f"S[{i}]")
    text = bp.render_dispositions({"measured_at_utc": "t"}, {"capabilities": []})
    p0 = _p0_rows(text)
    measured = [r for r in p0 if r[2] == "measurement"]
    assert len(measured) == 5, measured
    assert {r[-1] for r in measured} == {f"S[{i}]" for i in _P0_MEASURE_IDS}
    assert text.count("| file present |") == 11


def test_the_real_dispositions_never_leave_a_measurement_row_blank():
    needs_evidence(PROGRAM_REGISTER)
    text = bp.render_dispositions({"measured_at_utc": "t"}, {"capabilities": []})
    measured = [r for r in _p0_rows(text) if r[2] == "measurement"]
    assert len(measured) == 5, measured
    for row in measured:
        assert row[-1].startswith(("measured: ", "NOT MEASURED: ")), row


def test_every_measurement_row_points_at_its_exact_heading_anchor(
        tmp_path, monkeypatch):
    _baseline(tmp_path, {h: "* f — `a.txt:1 to a.txt:2`"
                         for h in _HEADINGS.values()})
    _point_at(monkeypatch, tmp_path)
    got = [bp.measurement_status(i) for i in _P0_MEASURE_IDS]
    assert got == ["measured: VERIHARNESS_CURRENT_BASELINE.md#" + a
                   for a in _HEADINGS], got
