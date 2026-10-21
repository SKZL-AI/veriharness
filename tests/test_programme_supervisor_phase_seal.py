"""A review of one phase is bound to that phase (R3), not to every phase of the programme.

The supervisor review of P2 was bound to a seal over all 239 checklist items and to a
digest of the whole capability matrix. On 2026-10-14 two P3 register rows were edited;
43 of 43 P2 items stayed VERIFIED, the seal moved, and the review of P2 read stale. A
programme of twelve phases cannot keep one review current if any edit anywhere stales it.

`phase_seal` / `phase_matrix_digest` bind a review to the items and matrix rows of its
phase. One case per rule, each beside a control that changes only the condition under
test: another phase's edit leaves the review current, an edit of its own phase stales
it, an item entering or leaving the phase stales it, and a record written before the
pair existed is judged exactly as before. Everything is built from a small hand-made
register and matrix (as `test_programme_supervisor.py` does); the real preserved
capsule is read only to pin that a legacy digest does not move.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "programme_supervisor_phase_seal", ROOT / "tools" / "programme_supervisor.py")
ps = importlib.util.module_from_spec(_spec)
sys.modules["programme_supervisor_phase_seal"] = ps
_spec.loader.exec_module(ps)


def _req(rid: str, phase: str) -> dict:
    return {"id": rid, "name": f"requirement {rid}", "target_phase": phase,
            "authoritative_item": f"{phase}.{rid}", "core_required_1_0": True,
            "optional_provider": False}


def _register(*extra: dict) -> dict:
    reqs = [_req("X-01", "PX"), _req("X-02", "PX"), _req("Y-01", "PY"), *extra]
    return {"plan_sha256": "plandigest", "requirements": reqs}


def _matrix(**rows) -> dict:
    """`_matrix(X_01="PROVEN", ...)`: id, status and one evidence line per row, plus
    the target phase the real matrix carries."""
    out = []
    for rid, status in rows.items():
        real = rid.replace("_", "-")
        out.append({"id": real, "target_phase": "PX" if real.startswith("X") else "PY",
                    "status": status, "evidence": [f"src/{rid.lower()}.py:1"]})
    return {"measured_at_utc": "2026-10-14T00:00:00Z", "capabilities": out}


BASE = dict(X_01="PROVEN", X_02="PROVEN", Y_01="MISSING")


def _review(register=None, matrix=None, phase="PX"):
    register = register or _register()
    matrix = matrix or _matrix(**BASE)
    capsule = ps.capsule_for(phase, register=register, matrix=matrix)
    snapshot, _, _ = ps.reconciled(register, matrix)
    record = ps.record_review(phase, capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={})
    return capsule, snapshot, record


def _stale(record, register, matrix, *, with_matrix=True):
    snapshot, _, _ = ps.reconciled(register, matrix)
    return ps.stale_against(record, snapshot=snapshot,
                            matrix_digest=ps.matrix_digest(matrix),
                            matrix=matrix if with_matrix else None)


# --------------------------------------------------------------------------- #
# what a new capsule and a new record carry
# --------------------------------------------------------------------------- #

def test_a_new_capsule_and_record_carry_the_phase_scoped_pair():
    capsule, snapshot, record = _review()
    matrix = _matrix(**BASE)
    assert capsule.selection.phase_seal == ps.phase_seal(snapshot, "PX")
    assert capsule.selection.phase_matrix_digest == ps.phase_matrix_digest(
        matrix, snapshot, "PX")
    assert record.phase_seal == capsule.selection.phase_seal
    assert record.phase_matrix_digest == capsule.selection.phase_matrix_digest
    assert record.snapshot_seal == snapshot.seal, "the whole seal is still recorded"
    assert record.matrix_digest == ps.matrix_digest(matrix)
    assert _stale(record, _register(), matrix) == []


def test_the_phase_seal_names_its_phase_and_covers_only_its_items():
    _, snapshot, _ = _review()
    assert ps.phase_seal(snapshot, "PX") != ps.phase_seal(snapshot, "PY")
    assert ps.phase_seal(snapshot, "PX") != snapshot.seal
    assert ps.phase_seal(snapshot, "PNONE") == ps.phase_seal(snapshot, "PNONE")
    assert ps.phase_seal(snapshot, "PNONE") != ps.phase_seal(snapshot, "PX")


# --------------------------------------------------------------------------- #
# another phase moves: still current. its own phase moves: stale
# --------------------------------------------------------------------------- #

def test_an_edit_of_another_phase_leaves_the_review_current():
    """The 2026-10-14 case: the register rows of another phase were edited."""
    _, _, record = _review()
    register = _register(_req("Y-02", "PY"))
    register["requirements"][2] = {**register["requirements"][2], "name": "renamed row"}
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="PROVEN", Y_02="MISSING")
    snapshot, _, _ = ps.reconciled(register, matrix)
    assert snapshot.seal != record.snapshot_seal, "the whole seal did move"
    assert _stale(record, register, matrix) == []


def test_a_status_change_of_another_phase_in_the_matrix_leaves_it_current():
    _, _, record = _review()
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="IMPLEMENTED_NOT_PROVEN")
    assert ps.matrix_digest(matrix) != record.matrix_digest
    assert _stale(record, _register(), matrix) == []


def test_a_status_change_of_its_own_phase_makes_it_stale_with_both_reasons():
    _, _, record = _review()
    matrix = _matrix(X_01="PROVEN", X_02="MISSING", Y_01="MISSING")
    reasons = _stale(record, _register(), matrix)
    assert len(reasons) == 2, reasons
    assert "phase seal of PX changed" in reasons[0], reasons
    assert "matrix rows of PX changed" in reasons[1], reasons


def test_a_changed_evidence_line_of_its_own_phase_makes_it_stale():
    _, _, record = _review()
    matrix = _matrix(**BASE)
    matrix["capabilities"][0]["evidence"] = ["src/elsewhere.py:9"]
    reasons = _stale(record, _register(), matrix)
    assert any("matrix rows of PX changed" in r for r in reasons), reasons


def test_a_field_a_row_gains_is_not_a_change_of_what_was_reviewed():
    """A watch annotation on a row (slice 3: evidence_commit, stale_since, last_watch)
    must not stale a review; the review judged id, phase, status and evidence."""
    _, _, record = _review()
    matrix = _matrix(**BASE)
    matrix["capabilities"][0]["last_watch"] = {"verdict": "PASS", "commit": "ab" * 20}
    matrix["capabilities"][0]["stale_since"] = "cd" * 20
    assert _stale(record, _register(), matrix) == []


def test_an_item_entering_or_leaving_the_phase_makes_it_stale():
    _, _, record = _review()
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", X_03="MISSING", Y_01="MISSING")
    entering = _register(_req("X-03", "PX"))
    assert any("phase seal of PX changed" in r for r in _stale(record, entering, matrix))
    leaving = {"plan_sha256": "plandigest",
               "requirements": [_req("X-01", "PX"), _req("Y-01", "PY")]}
    assert any("phase seal of PX changed" in r for r in
               _stale(record, leaving, _matrix(X_01="PROVEN", Y_01="MISSING")))


def test_the_measurement_clock_alone_moves_nothing():
    _, _, record = _review()
    matrix = _matrix(**BASE)
    matrix["measured_at_utc"] = "2030-01-01T00:00:00Z"
    assert _stale(record, _register(), matrix) == []


def test_without_the_matrix_the_whole_digest_is_compared_the_stricter_reading():
    """A caller that cannot hand the rows in must not get a looser answer: another
    phase's matrix edit then stales the review exactly as it did."""
    _, _, record = _review()
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="IMPLEMENTED_NOT_PROVEN")
    reasons = _stale(record, _register(), matrix, with_matrix=False)
    assert len(reasons) == 1 and "matrix digest changed" in reasons[0], reasons


def test_an_unsealed_snapshot_is_still_never_current():
    _, snapshot, record = _review()
    unsealed = snapshot.model_copy(update={"seal": "0" * 64})
    reasons = ps.stale_against(record, snapshot=unsealed,
                               matrix_digest=ps.matrix_digest(_matrix(**BASE)),
                               matrix=_matrix(**BASE))
    assert any("no intact seal" in r for r in reasons), reasons


# --------------------------------------------------------------------------- #
# the canonical brief a gate derives is the phase's own
# --------------------------------------------------------------------------- #

def test_the_comparable_digest_of_a_phase_ignores_another_phases_edit():
    """`readiness._capsule_binding_problems` rebuilds the canonical brief and compares
    digests; if the whole seal stayed in the comparison, a review bound to its phase
    would still read "not the brief this gate would put to a supervisor"."""
    capsule, _, _ = _review()
    register = _register(_req("Y-02", "PY"))
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="PROVEN", Y_02="MISSING")
    rebuilt = ps.capsule_for("PX", register=register, matrix=matrix)
    assert rebuilt.selection.source_seal != capsule.selection.source_seal
    assert ps.capsule_digest(rebuilt) != ps.capsule_digest(capsule), \
        "the digest of what a supervisor was given is still of the whole dump"
    assert (ps.comparable_digest(rebuilt, like=capsule)
            == ps.comparable_digest(capsule, like=capsule))


def test_the_comparable_digest_of_a_phase_follows_its_own_edit():
    capsule, _, _ = _review()
    moved = ps.capsule_for("PX", register=_register(),
                           matrix=_matrix(X_01="PROVEN", X_02="MISSING", Y_01="MISSING"))
    assert (ps.comparable_digest(moved, like=capsule)
            != ps.comparable_digest(capsule, like=capsule))


def test_a_capsule_for_another_phase_compares_differently():
    capsule, _, _ = _review()
    other = ps.capsule_for("PY", register=_register(), matrix=_matrix(**BASE))
    assert (ps.comparable_digest(other, like=capsule)
            != ps.comparable_digest(capsule, like=capsule))


def test_against_a_brief_preserved_before_the_pair_the_comparison_is_the_old_one():
    """Control: a legacy brief is current exactly while the whole board is, as it was."""
    capsule, _, _ = _review()
    body = json.loads(capsule.model_dump_json(by_alias=True))
    body["selection"].pop("phase_seal")
    body["selection"].pop("phase_matrix_digest")
    legacy = ps.ProgrammeCapsule.model_validate(body)
    same = ps.capsule_for("PX", register=_register(), matrix=_matrix(**BASE))
    assert ps.comparable_digest(same, like=legacy) == ps.capsule_digest(legacy)
    register = _register(_req("Y-02", "PY"))
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="MISSING", Y_02="MISSING")
    other_board = ps.capsule_for("PX", register=register, matrix=matrix)
    assert ps.comparable_digest(other_board, like=legacy) != ps.capsule_digest(legacy)


# --------------------------------------------------------------------------- #
# old records and capsules load, dump and digest exactly as before
# --------------------------------------------------------------------------- #

def _legacy(record):
    body = json.loads(record.model_dump_json())
    assert "phase_seal" in body
    body.pop("phase_seal")
    body.pop("phase_matrix_digest")
    return body


def test_a_record_without_the_pair_loads_and_is_judged_against_the_whole_seal():
    _, _, record = _review()
    old = ps.load_record(_legacy(record))
    assert old.phase_seal is None and old.phase_matrix_digest is None
    register = _register(_req("Y-02", "PY"))
    matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="MISSING", Y_02="MISSING")
    reasons = _stale(old, register, matrix)
    assert any("snapshot seal changed" in r for r in reasons), reasons
    assert _stale(old, _register(), _matrix(**BASE)) == []


def test_a_record_with_half_the_pair_is_judged_as_a_legacy_record():
    """Fail closed: only both halves bind a review to its phase; one half is no scope."""
    _, _, record = _review()
    for drop in ("phase_seal", "phase_matrix_digest"):
        body = json.loads(record.model_dump_json())
        body.pop(drop)
        half = ps.load_record(body)
        register = _register(_req("Y-02", "PY"))
        matrix = _matrix(X_01="PROVEN", X_02="PROVEN", Y_01="MISSING", Y_02="MISSING")
        reasons = _stale(half, register, matrix)
        assert any("snapshot seal changed" in r for r in reasons), (drop, reasons)


def test_a_legacy_record_dumps_without_the_new_keys():
    _, _, record = _review()
    old = ps.load_record(_legacy(record))
    assert "phase_seal" not in json.loads(old.model_dump_json())
    assert "phase_matrix_digest" not in json.loads(old.model_dump_json())


def test_a_legacy_capsule_dumps_and_digests_without_the_new_keys():
    capsule, _, _ = _review()
    body = json.loads(capsule.model_dump_json(by_alias=True))
    body["selection"].pop("phase_seal")
    body["selection"].pop("phase_matrix_digest")
    legacy = ps.ProgrammeCapsule.model_validate(body)
    assert legacy.selection.is_phase_scoped() is False
    dumped = json.loads(legacy.model_dump_json(by_alias=True))
    assert "phase_seal" not in dumped["selection"]
    assert dumped == body
    assert ps.capsule_digest(legacy) == ps._sha256(body)


#: The capsule digest the review 20261013T093422Z-S bound (`capsule_digest` in its record),
#: frozen here so that the pin does not read the live `REVIEW.json`: a later review of P2
#: under this tool replaces that file and must not turn this node red.
PRESERVED_CAPSULE_DIGEST = "76d2b079e307d549e3e775f75bcd305d8b77ecdfff88472d0d002281e0f926cb"


def test_the_preserved_capsule_of_the_sealing_review_keeps_its_digest():
    """Pins that adding the fields moved no preserved digest: the capsule that review was
    given still digests to the value its record carries."""
    capsule_file = ROOT / "dogfood/p2-gate/20261013T093422Z/capsule-p2.json"
    capsule = ps.ProgrammeCapsule.model_validate(
        json.loads(capsule_file.read_text(encoding="utf-8")))
    assert capsule.selection.is_phase_scoped() is False
    assert ps.capsule_digest(capsule) == PRESERVED_CAPSULE_DIGEST


# --------------------------------------------------------------------------- #
# the record is refused against a state its capsule was not cut from
# --------------------------------------------------------------------------- #

def test_a_record_against_another_state_of_the_phase_is_refused():
    capsule, _, _ = _review()
    other_register = _register()
    other_matrix = _matrix(**BASE)
    snapshot, _, _ = ps.reconciled(other_register, other_matrix)
    forged = capsule.model_copy(update={
        "selection": capsule.selection.model_copy(update={"phase_seal": "f" * 64})})
    with pytest.raises(ValueError, match="phase seal"):
        ps.record_review("PX", capsule=forged, findings=[], snapshot=snapshot, decisions={})


def _other(member):
    """Another member of the same enum."""
    return next(m for m in type(member) if m is not member)


def _field_values(item) -> dict:
    """Every field of a checklist item, and a value that differs from what the fixture's item
    holds. The table must name every field the model has: a field added later is a decision
    here, not a silent gap."""
    return {
        "item_id": item.item_id + "+edited", "phase": "PZ", "title": "another title",
        "requirement_ref": "PX.other", "source_digest": "f" * 64,
        "criticality": _other(item.criticality), "status": _other(item.status),
        "evidence_refs": ["matrix:elsewhere"], "finding_refs": ["F-9"],
        "last_reconciled_generation": 99, "reopen_reason": "why", "waiver_ref": "W-1"}


def test_the_table_of_item_fields_is_the_models_fields():
    from hoh.checklist import ChecklistItem
    _, snapshot, _ = _review()
    item = next(i for i in snapshot.items if i.phase == "PX")
    assert set(_field_values(item)) == set(ChecklistItem.model_fields)


@pytest.mark.parametrize("field", [
    "item_id", "phase", "title", "requirement_ref", "source_digest", "criticality", "status",
    "evidence_refs", "finding_refs", "last_reconciled_generation", "reopen_reason",
    "waiver_ref"])
def test_the_phase_seal_moves_when_any_one_field_of_an_item_of_the_phase_moves(field):
    """One node per covered field: a seal that leaves one of them out reads a review of a
    changed item as current. (Each is the node of the mutant that excludes that field.)"""
    _, snapshot, _ = _review()
    base = ps.phase_seal(snapshot, "PX")
    item = next(i for i in snapshot.items if i.phase == "PX")
    value = _field_values(item)[field]
    changed = snapshot.model_copy(update={"items": [
        i.model_copy(update={field: value}) if i.item_id == item.item_id else i
        for i in snapshot.items]})
    assert ps.phase_seal(changed, "PX") != base, field
    if field == "phase":
        assert ps.phase_seal(changed, "PZ") != ps.phase_seal(snapshot, "PZ"), \
            "and the phase the item entered"
    assert any(i.phase == "PY" for i in snapshot.items)
    assert ps.phase_seal(changed, "PY") == ps.phase_seal(snapshot, "PY")


def test_the_generation_is_part_of_the_seal():
    _, snapshot, _ = _review()
    moved = snapshot.model_copy(update={"generation": snapshot.generation + 1})
    assert ps.phase_seal(moved, "PX") != ps.phase_seal(snapshot, "PX")


# --- the same fields, reached from the register and the matrix (the real exposure) ---
#
# An edit of a register row's binding, criticality or plan digest moves one field of an
# item and leaves every matrix row of the phase byte-equal: the phase seal is then the
# only thing that can tell a review it is stale.

def _reconciled(register, matrix):
    snapshot, _, _ = ps.reconciled(register, matrix)
    return snapshot


@pytest.mark.parametrize("label, edit", [
    ("requirement-binding", lambda r, m: r["requirements"][0].update(
        authoritative_item="PX.other")),
    ("criticality", lambda r, m: r["requirements"][0].update(core_required_1_0=False)),
    ("source-digest", lambda r, m: r.update(plan_sha256="another plan digest")),
    ("title", lambda r, m: r["requirements"][0].update(name="renamed")),
])
def test_a_register_edit_moves_the_seal_while_the_matrix_rows_stay_equal(label, edit):
    import copy
    register, matrix = _register(), _matrix(**BASE)
    before = _reconciled(register, matrix)
    r2, m2 = copy.deepcopy(register), copy.deepcopy(matrix)
    edit(r2, m2)
    after = _reconciled(r2, m2)
    assert ps.phase_seal(after, "PX") != ps.phase_seal(before, "PX"), label
    assert (ps.phase_matrix_digest(m2, after, "PX")
            == ps.phase_matrix_digest(matrix, before, "PX")), \
        f"{label}: the matrix rows do not move, so the seal is the only detector"


def test_a_matrix_status_edit_moves_the_seal_and_the_rows():
    import copy
    register, matrix = _register(), _matrix(**BASE)
    before = _reconciled(register, matrix)
    m2 = copy.deepcopy(matrix)
    m2["capabilities"][0]["status"] = "MISSING"
    after = _reconciled(register, m2)
    assert ps.phase_seal(after, "PX") != ps.phase_seal(before, "PX")
    assert (ps.phase_matrix_digest(m2, after, "PX")
            != ps.phase_matrix_digest(matrix, before, "PX"))


def test_an_evidence_edit_moves_the_matrix_rows_not_the_seal():
    """Honest limit of the two bindings: an item's `evidence_refs` hold a reference
    (`matrix:X-01`), not the evidence text; the text of the row's evidence is what the
    matrix-row digest binds. Both halves are needed."""
    import copy
    register, matrix = _register(), _matrix(**BASE)
    before = _reconciled(register, matrix)
    m2 = copy.deepcopy(matrix)
    m2["capabilities"][0]["evidence"] = ["src/elsewhere.py:2"]
    after = _reconciled(register, m2)
    assert ps.phase_seal(after, "PX") == ps.phase_seal(before, "PX")
    assert (ps.phase_matrix_digest(m2, after, "PX")
            != ps.phase_matrix_digest(matrix, before, "PX"))


# --- which matrix rows belong to the phase ---------------------------------------------

def test_a_row_that_changes_its_target_phase_moves_the_digest():
    import copy
    register, matrix = _register(), _matrix(**BASE)
    snapshot = _reconciled(register, matrix)
    m2 = copy.deepcopy(matrix)
    m2["capabilities"][0]["target_phase"] = "PY"
    assert (ps.phase_matrix_digest(m2, snapshot, "PX")
            != ps.phase_matrix_digest(matrix, snapshot, "PX"))


def test_a_row_of_the_phase_that_no_item_answers_for_is_bound_too():
    """A matrix row saying `target_phase == PX` with no register requirement behind it is
    still a row of the phase (the closure verifier counts it)."""
    import copy
    register, matrix = _register(), _matrix(**BASE)
    snapshot = _reconciled(register, matrix)
    m2 = copy.deepcopy(matrix)
    m2["capabilities"].append({"id": "ORPHAN-1", "target_phase": "PX", "status": "MISSING",
                               "evidence": ["src/orphan.py:1"]})
    assert (ps.phase_matrix_digest(m2, snapshot, "PX")
            != ps.phase_matrix_digest(matrix, snapshot, "PX"))
    m3 = copy.deepcopy(matrix)
    m3["capabilities"].append({"id": "ORPHAN-2", "target_phase": "PY", "status": "MISSING"})
    assert (ps.phase_matrix_digest(m3, snapshot, "PX")
            == ps.phase_matrix_digest(matrix, snapshot, "PX")), "another phase's orphan row"


def test_the_probe_suffix_of_an_item_id_is_not_part_of_its_requirement():
    assert ps._item_requirement("P2-33+probe") == "P2-33"
    _, snapshot, _ = _review()
    item = next(i for i in snapshot.items if i.phase == "PX")
    suffixed = snapshot.model_copy(update={"items": [
        i.model_copy(update={"item_id": i.item_id + "+probe"}) if i.item_id == item.item_id
        else i for i in snapshot.items]})
    matrix = _matrix(**BASE)
    moved = {**matrix, "capabilities": [dict(c) for c in matrix["capabilities"]]}
    moved["capabilities"][0]["status"] = "MISSING"
    assert (ps.phase_matrix_digest(moved, suffixed, "PX")
            != ps.phase_matrix_digest(matrix, suffixed, "PX")), \
        "the row of `X-01+probe` is the row of `X-01`"


# --- both halves or none (D3), on the capsule's selection and on the record -----------

def test_a_selection_with_one_half_of_the_pair_is_not_phase_scoped_and_dumps_what_it_holds():
    capsule, _, _ = _review()
    body = json.loads(capsule.model_dump_json(by_alias=True))
    body["selection"]["phase_matrix_digest"] = None
    half = ps.ProgrammeCapsule.model_validate(body)
    assert half.selection.is_phase_scoped() is False
    assert "phase_seal" in json.loads(half.model_dump_json(by_alias=True))["selection"], \
        "a half that is set is dumped; only an unset pair is omitted"


def test_a_record_with_one_half_of_the_pair_dumps_the_half_it_holds():
    _, _, record = _review()
    body = json.loads(record.model_dump_json())
    body.pop("phase_matrix_digest")
    half = ps.load_record(body)
    assert "phase_seal" in json.loads(half.model_dump_json())


def test_a_row_that_already_disagrees_about_its_phase_is_still_the_items_row():
    """The row of a requirement an item of the phase answers for belongs to the phase
    whatever phase the row itself says; an edit of it moves the digest."""
    import copy
    register, matrix = _register(), _matrix(**BASE)
    matrix = copy.deepcopy(matrix)
    matrix["capabilities"][0]["target_phase"] = "PY"        # X-01's row says PY from the start
    snapshot = _reconciled(register, matrix)
    m2 = copy.deepcopy(matrix)
    m2["capabilities"][0]["status"] = "MISSING"
    assert (ps.phase_matrix_digest(m2, snapshot, "PX")
            != ps.phase_matrix_digest(matrix, snapshot, "PX"))
