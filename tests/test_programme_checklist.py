"""This programme's own gates, decided by the product's reconciler (P2-28).

The binding is what makes `hoh.checklist` a capability rather than a component
with tests: until something consulted it, P2-26 was held at PARTIAL for the
same reason P2-24 was. So these cases are about the *binding* -- that the
statuses are derived and not copied, that the gate re-derives, that an unsound
checklist produces no verdict at all, and that the verdict agrees with the
programme's other, independently built view of the same question.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "programme_checklist", ROOT / "tools" / "programme_checklist.py")
pc = importlib.util.module_from_spec(_spec)
sys.modules["programme_checklist"] = pc
_spec.loader.exec_module(pc)

from hoh.checklist import Criticality, Status, phase_gate, reconcile  # noqa: E402


def _register(**kw) -> dict:
    base = {
        "plan_sha256": "plandigest",
        "requirements": [
            {"id": "X-01", "name": "first", "target_phase": "PX",
             "authoritative_item": "PX.a", "core_required_1_0": True},
            {"id": "X-02", "name": "second", "target_phase": "PX",
             "authoritative_item": "PX.b", "core_required_1_0": False},
        ],
    }
    base.update(kw)
    return base


def _matrix(*statuses: str) -> dict:
    return {"capabilities": [{"id": f"X-0{i + 1}", "status": s}
                             for i, s in enumerate(statuses)]}


def test_the_status_is_derived_from_the_matrix_not_copied_from_it():
    """Nothing in the binding writes VERIFIED. The matrix says what it
    measured; `reconcile` decides what that means, which is the whole point of
    routing the programme's checklist through the product."""
    items, evidence = pc.build(_register(), _matrix("PROVEN", "MISSING"))
    snapshot = reconcile(items, evidence=evidence, generation=1)
    by_id = {i.item_id: i for i in snapshot.items}
    assert by_id["X-01"].status is Status.VERIFIED
    assert by_id["X-02"].status is Status.EVIDENCE_PENDING
    # And the binding itself never names a status:
    source = (ROOT / "tools" / "programme_checklist.py").read_text(encoding="utf-8")
    assert "Status.VERIFIED" not in source


def test_a_partial_measurement_is_not_evidence():
    """`capability_matrix` is careful that PARTIAL means something stands in for
    the thing. Counting it here would import that distinction and drop it."""
    items, evidence = pc.build(_register(), _matrix("PARTIAL", "PARTIAL"))
    snapshot = reconcile(items, evidence=evidence, generation=1)
    assert {i.status for i in snapshot.items} == {Status.EVIDENCE_PENDING}


def test_a_requirement_the_matrix_never_measured_has_no_evidence_at_all():
    items, evidence = pc.build(_register(), {"capabilities": []})
    assert evidence == {}
    snapshot = reconcile(items, evidence=evidence, generation=1)
    assert {i.status for i in snapshot.items} == {Status.EVIDENCE_PENDING}


def test_the_gate_re_derives_rather_than_trusting_the_snapshot():
    items, evidence = pc.build(_register(), _matrix("PROVEN", "PROVEN"))
    snapshot = reconcile(items, evidence=evidence, generation=1)
    verdict = phase_gate(snapshot, "PX", evidence=evidence)
    assert verdict.closed and verdict.derived_from_evidence


def test_only_an_explicitly_optional_provider_stops_blocking():
    """The first mapping made anything without a 1.0 flag non-blocking, and
    eighteen unbuilt UI requirements came out as a closed P9 gate. A
    requirement is in the register because the programme promised it."""
    assert pc.criticality_of({"core_required_1_0": True}) \
        is Criticality.P0_RELEASE_BLOCKING
    assert pc.criticality_of({"optional_provider": True}) is Criticality.P2_NONBLOCKING
    assert pc.criticality_of({}) is Criticality.P1_PHASE_BLOCKING
    assert pc.criticality_of({"ui_only": True}) is Criticality.P1_PHASE_BLOCKING


def test_an_unsound_checklist_produces_no_verdict(tmp_path, monkeypatch, capsys):
    """A checklist that is structurally wrong cannot produce a meaningful
    verdict, and reporting one anyway is the false green this file exists to
    prevent. Here: two requirements sharing an id."""
    register = _register(requirements=[
        {"id": "X-01", "name": "first", "target_phase": "PX",
         "authoritative_item": "PX.a", "core_required_1_0": True},
        {"id": "X-01", "name": "again", "target_phase": "PX",
         "authoritative_item": "PX.b", "core_required_1_0": True},
    ])
    (tmp_path / "register.json").write_text(json.dumps(register), encoding="utf-8")
    (tmp_path / "matrix.json").write_text(json.dumps(_matrix("PROVEN", "PROVEN")),
                                          encoding="utf-8")
    monkeypatch.setattr(pc, "REGISTER", tmp_path / "register.json")
    monkeypatch.setattr(pc, "MATRIX", tmp_path / "matrix.json")
    assert pc.main([]) == 1
    assert "duplicate item id" in capsys.readouterr().err


def test_a_missing_register_is_an_environment_gap_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "REGISTER", tmp_path / "nope.json")
    assert pc.main([]) == 3


def test_this_programmes_own_gates_agree_with_the_independently_built_plan():
    """Two views of one question, built by different code from the same measured
    matrix: `tools/build_plan.py` derives the phase gates from the capability
    matrix directly, and this binding derives them through the product's
    reconciler. They must agree about P1 and P2, or one of them is wrong.
    """
    if not pc.REGISTER.is_file() or not pc.MATRIX.is_file():
        pytest.skip("the register and matrix are internal")
    items, evidence = pc.build(
        json.loads(pc.REGISTER.read_text(encoding="utf-8")),
        json.loads(pc.MATRIX.read_text(encoding="utf-8")))
    snapshot = reconcile(items, evidence=evidence, generation=1)
    p1 = phase_gate(snapshot, "P1", evidence=evidence)
    p2 = phase_gate(snapshot, "P2", evidence=evidence)
    assert p1.closed, [b.item_id for b in p1.blockers]
    assert not p2.closed, "P2 is the open frontier; a closed verdict here is a false green"

    # The other view: every P1 requirement measured PROVEN, and some P2 one not.
    measured = {c["id"]: c["status"]
                for c in json.loads(pc.MATRIX.read_text(encoding="utf-8"))["capabilities"]}
    register = json.loads(pc.REGISTER.read_text(encoding="utf-8"))["requirements"]
    p1_ids = [r["id"] for r in register if r["target_phase"] == "P1"]
    assert all(measured.get(i) == "PROVEN" for i in p1_ids)
    p2_ids = [r["id"] for r in register if r["target_phase"] == "P2"]
    assert any(measured.get(i) != "PROVEN" for i in p2_ids)


def test_the_written_snapshot_carries_its_seal_and_the_gate_mode(tmp_path):
    if not pc.REGISTER.is_file():
        pytest.skip("the register is internal")
    out = tmp_path / "snapshot.json"
    # O229/L3: the tool now exits 1 when a preserved review has left release-blocking
    # findings open, because exiting 0 published `programme_checklist | PASS` on the board
    # with fourteen of them in hand. Both codes write the snapshot, and the snapshot is
    # what this test is about -- so the code is asserted to be one of the two rather than
    # pinned to a number that depends on whether this tree currently has open findings.
    rc = pc.main(["--out", str(out)])
    assert rc in (0, 1), rc
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["seal"] and written["generation"] == 1
    assert all(g["derived_from_evidence"] for g in written["gates"].values())
    assert len(written["items"]) > 100


def test_open_release_blocking_findings_move_the_exit_code(tmp_path, monkeypatch):
    """O229/L3. `fsck`'s ninth check fired only to stderr with the code left at 0, so the
    board read `programme_checklist | PASS | 186 item(s) reconciled` while fourteen
    unresolved release-blocking findings sat in the preserved review. That is O228/K5's
    "dead code that looks like a safety check" with one extra step, and it was mine.

    The control is the same tree with nothing preserved: the code returns to 0, so this
    pins the *cause* rather than just the number."""
    if not pc.REGISTER.is_file():
        pytest.skip("the register is internal")
    assert pc._preserved_release_blocking_findings()          # this tree has them
    assert pc.main(["--out", str(tmp_path / "a.json")]) == 1

    monkeypatch.setattr(pc, "_preserved_release_blocking_findings", lambda: [])
    assert pc.main(["--out", str(tmp_path / "b.json")]) == 0


def test_an_unstamped_or_symlinked_directory_cannot_erase_the_findings(tmp_path,
                                                                     monkeypatch):
    """O229/L3, the integrity half: this helper was written in the same entry that repaired
    the identical defect next door in `readiness.py`, and the variable was already called
    `stamped`. No stamp shape was required, so one `mkdir` of a lowercase name outranked
    every timestamp and erased the fourteen; it also asked only the newest directory."""
    root = tmp_path / "dogfood/p2-gate"
    (root / "20260924T0030Z").mkdir(parents=True)
    (root / "20260924T0030Z/findings.json").write_text(
        json.dumps([{"finding_id": "F1", "kind": "PROPOSE_REOPEN", "severity": "critical"}]),
        encoding="utf-8")
    monkeypatch.setattr(pc, "HOH", tmp_path)
    assert pc._preserved_release_blocking_findings() == ["F1"]

    for name in ("zzz", "draft", "notes"):
        (root / name).mkdir()
        (root / name / "findings.json").write_text("[]", encoding="utf-8")
    assert pc._preserved_release_blocking_findings() == ["F1"], "an unstamped name won"

    # And an honest later review that returns nothing does not forget either: under
    # accumulation only an explicit resolution settles a raised finding (O229/L2).
    (root / "20260925T0000Z").mkdir()
    (root / "20260925T0000Z/findings.json").write_text("[]", encoding="utf-8")
    assert pc._preserved_release_blocking_findings() == ["F1"]


# --------------------------------------------------------------------------- #
# O220/F2, M5: a verdict over capability coverage is not a phase exit.
# --------------------------------------------------------------------------- #

def _gate_scope(*items: tuple[str, str]) -> dict:
    """An inventory with phases PX and PY and the given gate items."""
    return {"phases": [{"phase": "PX"}, {"phase": "PY"}],
            "items": [{"id": item_id, "phase": phase, "kind": "gate", "text": item_id,
                       "capability": False} for item_id, phase in items]}


def _rows(**statuses: str) -> dict:
    return {"capabilities": [{"id": rid.replace("_", "-"), "status": s}
                             for rid, s in statuses.items()]}


def _x03(**kw) -> dict:
    base = {"id": "X-03", "name": "optional", "target_phase": "PX",
            "authoritative_item": "PX.gate", "optional_provider": True}
    base.update(kw)
    return base


def _run_over(tmp_path, monkeypatch, capsys, register, matrix, scope, *argv,
              name="run"):
    """`main` over a fixture register, matrix and inventory, findings stubbed to `[]`.
    `scope` is the inventory, or a `(None, why)` answer of `_programme_scope`."""
    (tmp_path / f"{name}-register.json").write_text(json.dumps(register), encoding="utf-8")
    (tmp_path / f"{name}-matrix.json").write_text(json.dumps(matrix), encoding="utf-8")
    monkeypatch.setattr(pc, "REGISTER", tmp_path / f"{name}-register.json")
    monkeypatch.setattr(pc, "MATRIX", tmp_path / f"{name}-matrix.json")
    monkeypatch.setattr(pc, "_preserved_release_blocking_findings", lambda: [])
    answer = scope if isinstance(scope, tuple) else (scope, "")
    monkeypatch.setattr(pc, "_programme_scope", lambda: answer)
    out = tmp_path / f"{name}-snapshot.json"
    rc = pc.main(["--out", str(out), *argv])
    return rc, capsys.readouterr().out, json.loads(out.read_text(encoding="utf-8"))


def test_a_closed_phase_with_an_unbound_gate_condition_does_not_read_closed(
        tmp_path, monkeypatch, capsys):
    """P11 read `closed` while carrying an amendment invariant nothing evaluates."""
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                              _matrix("PROVEN", "PROVEN"), _gate_scope(("PX.gate", "PX")))
    assert rc == 0
    assert ("  PX: capability coverage closed; phase exit not evaluated "
            "(1 gate condition(s) bound to no requirement)") in out, out
    assert "  PX: closed" not in out
    assert snap["phase_exit"]["PX"] == {"evaluated": False, "bound": [],
                                        "unbound": ["PX.gate"], "not_held": []}
    assert snap["gates"]["PX"]["closed"] is True

    # Control: X-01 claims the gate item, so the verdict covers it.
    register = _register()
    register["requirements"][0]["also_covers"] = ["PX.gate"]
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, register,
                              _matrix("PROVEN", "PROVEN"), _gate_scope(("PX.gate", "PX")),
                              name="control")
    assert rc == 0
    assert "  PX: closed\n" in out, out
    assert snap["phase_exit"]["PX"] == {"evaluated": True, "bound": ["PX.gate"],
                                        "unbound": [], "not_held": []}


def test_a_blocked_phase_with_an_unbound_gate_condition_says_capability_coverage(
        tmp_path, monkeypatch, capsys):
    rc, out, _snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                               _matrix("MISSING", "PROVEN"),
                               _gate_scope(("PX.gate", "PX")))
    assert rc == 0
    assert ("  PX: capability coverage: 1 blocking; phase exit not evaluated "
            "(1 gate condition(s) bound to no requirement)") in out, out


def test_a_gate_item_bound_only_through_a_non_blocking_requirement_is_not_held(
        tmp_path, monkeypatch, capsys):
    """`phase_gate` passes over a non-blocking item, so a gate condition claimed only
    through one that is not PROVEN holds nothing shut."""
    not_held = ("1 gate condition(s) bound only through a non-blocking requirement "
                "not PROVEN")
    register = _register(requirements=_register()["requirements"] + [_x03()])
    scope = _gate_scope(("PX.gate", "PX"))
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, register,
                              _rows(X_01="PROVEN", X_02="PROVEN", X_03="MISSING"), scope)
    assert rc == 0
    assert (f"  PX: capability coverage closed; phase exit not evaluated ({not_held})"
            in out), out
    assert snap["phase_exit"]["PX"]["not_held"] == ["PX.gate"]
    assert snap["phase_exit"]["PX"]["evaluated"] is False

    # Control: proven, the optional claimer covers the item.
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, register,
                              _rows(X_01="PROVEN", X_02="PROVEN", X_03="PROVEN"), scope,
                              name="proven")
    assert "  PX: closed\n" in out, out
    assert snap["phase_exit"]["PX"]["bound"] == ["PX.gate"]

    # Mixed claim: one held claim must not hide an unheld one.
    mixed = _register()
    mixed["requirements"][0]["also_covers"] = ["PX.gate"]
    mixed["requirements"].append(_x03())
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, mixed,
                              _rows(X_01="PROVEN", X_02="PROVEN", X_03="MISSING"), scope,
                              name="mixed")
    assert "  PX: closed" not in out, out
    assert snap["phase_exit"]["PX"]["not_held"] == ["PX.gate"]

    # Both kinds at once: the note names both parts.
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, register,
                              _rows(X_01="PROVEN", X_02="PROVEN", X_03="MISSING"),
                              _gate_scope(("PX.gate", "PX"), ("PX.free", "PX")),
                              name="both")
    assert ("  PX: capability coverage closed; phase exit not evaluated "
            f"(1 gate condition(s) bound to no requirement, {not_held})") in out, out


def test_a_phase_with_gate_conditions_and_no_checklist_item_is_shown(
        tmp_path, monkeypatch, capsys):
    """P7 has 54 validation conditions and no requirement, and got no line at all."""
    scope = _gate_scope(("PY.gate", "PY"))
    rc, out, _snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                               _matrix("PROVEN", "PROVEN"), scope, "--json")
    assert rc == 0
    assert json.loads(out)["PY"] == {"closed": False, "blockers": 0,
                                     "phase_exit_evaluated": False}
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                              _matrix("PROVEN", "PROVEN"), scope, name="text")
    assert ("  PY: no checklist item; phase exit not evaluated "
            "(1 gate condition(s) bound to no requirement)") in out, out
    assert snap["phase_exit"]["PY"]["unbound"] == ["PY.gate"]
    assert "PY" not in snap["gates"], "the gates key is the checklist's verdicts, unchanged"


def test_without_the_inventory_no_phase_exit_is_evaluated(tmp_path, monkeypatch, capsys):
    """Through the real `_programme_scope`: the build plan finds no inventory."""
    monkeypatch.setattr(pc._build_plan(), "_scope", lambda: None)
    why = "no programme inventory in this tree"
    assert pc._programme_scope() == (None, why)
    scope = pc._programme_scope()
    rc, out, snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                              _matrix("PROVEN", "PROVEN"), scope)
    assert rc == 0
    assert f"  PX: capability coverage closed; phase exit not evaluated ({why})" in out, out
    assert snap["phase_exit"] == {"PX": {"evaluated": False, "why": why}}
    rc, out, _snap = _run_over(tmp_path, monkeypatch, capsys, _register(),
                               _matrix("PROVEN", "PROVEN"), scope, "--json", name="json")
    assert json.loads(out)["PX"]["phase_exit_evaluated"] is False


def test_an_unreadable_inventory_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    for n, exc in enumerate((ValueError("PROGRAMME_AMENDMENTS.json records 2 problem(s)"),
                             PermissionError("permission denied: the scope file"))):
        def raising(exc=exc):
            raise exc
        monkeypatch.setattr(pc._build_plan(), "_scope", raising)
        scope, why = pc._programme_scope()
        assert scope is None and str(exc) in why, why
        assert why.startswith("programme inventory unreadable: ")
        (tmp_path / f"r{n}.json").write_text(json.dumps(_register()), encoding="utf-8")
        (tmp_path / f"m{n}.json").write_text(json.dumps(_matrix("PROVEN", "PROVEN")),
                                             encoding="utf-8")
        monkeypatch.setattr(pc, "REGISTER", tmp_path / f"r{n}.json")
        monkeypatch.setattr(pc, "MATRIX", tmp_path / f"m{n}.json")
        monkeypatch.setattr(pc, "_preserved_release_blocking_findings", lambda: [])
        out = tmp_path / f"s{n}.json"
        assert pc.main(["--out", str(out)]) == 0
        snap = json.loads(out.read_text(encoding="utf-8"))
        assert snap["phase_exit"]["PX"] == {"evaluated": False, "why": why}
        capsys.readouterr()


def test_the_real_inventory_reaches_the_checklist(tmp_path, capsys):
    """The positive control, nothing stubbed: the real inventory with its amendments,
    the real register and the real matrix."""
    from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: PLC0415
    needs_evidence(PROGRAM_REGISTER)
    scope, why = pc._programme_scope()
    assert scope is not None, why
    register = json.loads(pc.REGISTER.read_text(encoding="utf-8"))
    matrix = json.loads(pc.MATRIX.read_text(encoding="utf-8"))
    exits = pc.phase_exit(scope, register, matrix)
    exit_includes = [f"P2.exit-includes.{s}" for s in (
        "unattended-test", "no-human-attached", "controller-session-replacement",
        "automatic-continuation", "no-lost-state")]
    assert sorted(exits["P2"]["bound"]) == sorted(exit_includes)
    assert len(exits["P2"]["unbound"]) == 3
    assert all(i.startswith("P2.checklist-gate.") for i in exits["P2"]["unbound"])
    assert ("P11.a2.required-invariant.the-calibrated-decision-plane-may-consume-"
            "evidence-independe") in exits["P11"]["unbound"]
    assert exits["P7"]["unbound"]

    out = tmp_path / "snapshot.json"
    rc = pc.main(["--out", str(out)])
    assert rc in (0, 1), rc
    text = capsys.readouterr().out
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["phase_exit"]["P11"]["evaluated"] is False
    assert snap["phase_exit"]["P7"]["evaluated"] is False
    assert snap["phase_exit"]["P2"]["bound"] == exits["P2"]["bound"]
    assert "  P11: closed" not in text, text
    assert "  P7: no checklist item; phase exit not evaluated" in text, text


# --------------------------------------------------------------------------- #
# T-PSD6: the preserved review root must resolve inside the repository.
# --------------------------------------------------------------------------- #

def _foreign_review(where: Path) -> None:
    (where / "20260924T0030Z").mkdir(parents=True)
    (where / "20260924T0030Z/findings.json").write_text(
        json.dumps([{"finding_id": "F-FOREIGN", "kind": "PROPOSE_REOPEN",
                     "severity": "critical"}]), encoding="utf-8")


def test_a_review_root_outside_the_repository_is_not_read(tmp_path, monkeypatch, capsys):
    """A symlinked `dogfood/p2-gate` leading out of the repository fed a foreign
    review's findings into `fsck`'s ninth check, and an empty foreign review left it
    empty. It now fails closed (Q9): raised, exit 1, said first, snapshot written."""
    _foreign_review(tmp_path / "outside")
    repo = tmp_path / "repo"
    (repo / "dogfood").mkdir(parents=True)
    (repo / "dogfood/p2-gate").symlink_to(Path("../../outside"))
    monkeypatch.setattr(pc, "HOH", repo)
    with pytest.raises(pc.ReviewRootOutside):
        pc._preserved_release_blocking_findings()

    (tmp_path / "register.json").write_text(json.dumps(_register()), encoding="utf-8")
    (tmp_path / "matrix.json").write_text(json.dumps(_matrix("PROVEN", "PROVEN")),
                                          encoding="utf-8")
    monkeypatch.setattr(pc, "REGISTER", tmp_path / "register.json")
    monkeypatch.setattr(pc, "MATRIX", tmp_path / "matrix.json")
    monkeypatch.setattr(pc, "_programme_scope", lambda: (_gate_scope(), ""))
    snapshot = tmp_path / "snapshot.json"
    assert pc.main(["--out", str(snapshot)]) == 1
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("the preserved review root dogfood/p2-gate resolves outside "
                            "the repository"), first
    assert json.loads(snapshot.read_text(encoding="utf-8"))["gates"]["PX"]
    assert pc.main(["--json"]) == 1
    assert "review_root_problem" in json.loads(capsys.readouterr().out)

    # A looping root cannot be resolved, and fails closed the same way.
    looping = tmp_path / "looping"
    (looping / "dogfood").mkdir(parents=True)
    (looping / "dogfood/p2-gate").symlink_to(Path("p2-gate"))
    monkeypatch.setattr(pc, "HOH", looping)
    with pytest.raises(pc.ReviewRootOutside):
        pc._preserved_release_blocking_findings()

    # An absent root is still no preserved review, as on the pin.
    monkeypatch.setattr(pc, "HOH", tmp_path / "bare")
    assert pc._preserved_release_blocking_findings() == []

    # Control: the same review under a real in-repo root is read.
    inside = tmp_path / "inside"
    _foreign_review(inside / "dogfood/p2-gate")
    monkeypatch.setattr(pc, "HOH", inside)
    assert pc._preserved_release_blocking_findings() == ["F-FOREIGN"]
