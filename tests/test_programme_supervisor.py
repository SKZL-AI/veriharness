"""This programme's own supervisor review, bound to `hoh.supervisor` (P2-27).

`hoh.supervisor` had 38 passing tests and nothing consulting it -- the same gap
P2-26 was held at PARTIAL for until `programme_checklist.py` consumed the
reconciler. These cases are about the *binding*: that the capsule is built
from the programme's real state and records what it leaves out, that a review
is only complete when every finding is decided, that `complete` is computed
and never handed in, and that a review of a board that has since moved is
reported as not current.

One case per rule, each next to a negative control that changes only the
condition under test and shows the verdict flipping.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "programme_supervisor", ROOT / "tools" / "programme_supervisor.py")
ps = importlib.util.module_from_spec(_spec)
sys.modules["programme_supervisor"] = ps
_spec.loader.exec_module(ps)

from hoh.checklist import Status
from hoh.supervisor import FindingKind, FindingStatus, ReviewCapsule


def _req(rid, phase="PX", *, core=True, optional=False) -> dict:
    return {"id": rid, "name": f"requirement {rid}", "target_phase": phase,
            "authoritative_item": f"{phase}.{rid}", "core_required_1_0": core,
            "optional_provider": optional}


def _register(*requirements) -> dict:
    reqs = list(requirements) or [_req("X-01"), _req("X-02"), _req("X-03")]
    return {"plan_sha256": "plandigest", "requirements": reqs}


def _matrix(**statuses: str) -> dict:
    return {"capabilities": [{"id": rid.replace("_", "-"), "status": s,
                              "evidence": [f"src/{rid}.py:1"]}
                             for rid, s in statuses.items()]}


def _state(register=None, matrix=None):
    register = register or _register()
    matrix = matrix or _matrix(X_01="PROVEN", X_02="MISSING", X_03="PROVEN")
    capsule = ps.capsule_for("PX", register=register, matrix=matrix)
    snapshot, _, _ = ps.reconciled(register, matrix)
    return capsule, snapshot, matrix


def _finding(fid="F-1", *, kind=FindingKind.PROPOSE_REOPEN, items=("X-01",),
             action="re-run the evidence producer", review="R-1") -> dict:
    return {"finding_id": fid, "review_id": review, "kind": kind.value,
            "severity": "high", "item_ids": list(items),
            "reason": "the evidence is older than the source it proves",
            "proposed_action": action, "supervisor_provider": "independent-supervisor"}


def _no_action(fid="F-0", *, severity="low") -> dict:
    """A supervisor saying "nothing here", at a severity that agrees with it.

    O228/J3: `NO_ACTION` was the one non-blocking kind and severity was never consulted,
    so relabelling a critical finding into silence cost two field edits while its reason
    survived verbatim. A finding of nothing that calls itself critical is not one.
    """
    raw = _finding(fid, kind=FindingKind.NO_ACTION, items=(), action=None)
    return {**raw, "severity": severity}


#: O224/D2. An apply on its own settles nothing: `blocking_findings` requires a
#: `repair_evidence` reference, because the record that prompted the field marked
#: fourteen findings applied while at least three of the repairs did not exist. Tests
#: whose subject is completeness therefore decide with this shape; the bare word is
#: still accepted by the loader and is exercised as its own negative control below.
def _applied(evidence="dogfood/p2-gate/20260924T0030Z/brief.md#F-1"):
    return {"disposition": "apply", "evidence": evidence}


def _record(findings, decisions, **kw):
    capsule, snapshot, _ = _state()
    return ps.record_review("PX", capsule=capsule, findings=findings,
                            snapshot=snapshot, decisions=decisions, **kw)


# --------------------------------------------------------------------------- #
# completeness
# --------------------------------------------------------------------------- #


def test_a_review_with_every_finding_decided_is_still_not_complete():
    """O229/G, the doctrine change, stated where it is computed.

    This test used to assert the opposite -- that deciding every finding completed the
    review -- and five rounds of review each defeated the then-current definition of a
    sufficient decision with one keystroke: `"apply"`, then `"x"`, then the same `"x"`
    respelled as a different actor, then `"0.0"` as repair evidence. The predicates
    were not too weak; the premise was. A decision is written by the party whose phase
    is being gated.

    So the decisions are still recorded -- `applied`, `rejected`, `repair_evidence` are
    provenance worth keeping -- and they settle nothing. A finding stops blocking when
    a fresh review over the current state does not raise it, which is the only event
    that is evidence about what the finding was about.

    The positive control is `test_a_review_that_found_nothing_is_complete`: a review
    with no findings is complete, so this is not a gate that can never close."""
    record = _record([_finding("F-1"), _finding("F-2", items=("X-02",))],
                     {"F-1": _applied(), "F-2": "the evidence was re-run yesterday"})
    assert record.complete is False
    assert record.blocking_findings == ("F-1", "F-2")
    assert record.applied == ("F-1",)
    assert [f.finding_id for f in record.rejected] == ["F-2"]


def test_every_finding_blocks_whether_or_not_it_was_decided():
    """The companion to the case above: leaving F-2 undecided changes nothing, because
    deciding it would also have changed nothing. Both findings block, and the record
    still names them, so what the supervisor found is not lost by being disagreed with."""
    record = _record([_finding("F-1"), _finding("F-2", items=("X-02",))],
                     {"F-1": _applied()})
    assert record.complete is False
    assert record.blocking_findings == ("F-1", "F-2")
    assert {f.finding_id for f in record.findings} == {"F-1", "F-2"}


def test_a_no_action_finding_needs_no_decision_and_does_not_block_completeness():
    """Catches treating "nothing here" as an open question: a supervisor
    saying so is a complete answer. Control: the same review with a
    PROPOSE_REOPEN in its place and no decision is not complete."""
    assert _record([_no_action("F-0")], {}).complete is True
    control = _record([_finding("F-0")], {})
    assert control.complete is False and control.blocking_findings == ("F-0",)


def test_complete_cannot_be_handed_in_or_assigned():
    """Catches a caller asserting completeness instead of earning it: `complete`
    is computed from the findings and decisions, the one field the gate reads."""
    record = _record([_finding("F-1")], {})
    assert record.complete is False
    body = record.model_dump(exclude=set(ps.COMPUTED_FIELDS))
    with pytest.raises(ValidationError):
        ps.ReviewRecord.model_validate({**body, "complete": True})
    with pytest.raises(ValidationError):
        ps.ReviewRecord.model_validate({**body, "blocking_findings": []})
    with pytest.raises(ValidationError):
        # O224/D2. The newest computed field is the one a caller would most want to
        # hand in, because it is the one that says the repairs were not shown.
        ps.ReviewRecord.model_validate({**body, "applied_without_evidence": []})
    with pytest.raises((ValidationError, AttributeError, TypeError)):
        record.complete = True
    with pytest.raises(ValidationError):
        record.review_id = "other"          # frozen, not only the computed field
    # Control: the same body without the handed-in value is accepted, and the
    # recomputation says what the decisions say.
    assert ps.ReviewRecord.model_validate(body).complete is False


def test_a_stored_complete_that_disagrees_with_its_decisions_is_refused_on_load():
    """Catches an edited record file: reading it back recomputes `complete`,
    and a stored True over an undecided finding is refused, not believed."""
    record = _record([_finding("F-1")], {})
    stored = json.loads(record.model_dump_json())
    assert ps.load_record(stored).complete is False           # control: honest file loads
    stored["complete"] = True
    with pytest.raises(ValueError, match="complete"):
        ps.load_record(stored)


# --------------------------------------------------------------------------- #
# decisions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_rejection_with_a_blank_reason_is_refused(blank):
    """Catches a silent drop dressed as a rejection: `reject_finding` requires
    a reason, and this binding must not route around it."""
    with pytest.raises(ValueError, match="reason"):
        _record([_finding("F-1")], {"F-1": blank})


def test_a_rejection_with_a_reason_is_recorded_with_the_reason_and_who():
    """Control for the blank case: a non-blank reason is accepted and the
    rejection carries it and the actor (SV-05)."""
    record = _record([_finding("F-1")], {"F-1": "measured again, still holds"},
                     rejected_by="orchestrator-7")
    [rejected] = record.rejected
    assert rejected.status is FindingStatus.REJECTED
    assert rejected.rejection_reason == "measured again, still holds"
    assert rejected.rejected_by == "orchestrator-7"
    # O229/G: the rejection is recorded in full and settles nothing.
    assert record.complete is False
    assert record.blocking_findings == ("F-1",)


def test_a_finding_validate_refuses_is_a_validation_failure_not_a_finding():
    """Catches a malformed finding being counted as the supervisor's answer.
    It names an item the snapshot does not hold, so `supervisor.validate`
    refuses it; it is recorded by name with the product's reasons, and the
    review is not complete. Control: the same finding naming a real item is
    recorded as a finding."""
    bad = _record([_finding("F-9", items=("NOPE-1",))], {})
    assert [f.finding_id for f in bad.findings] == []
    [failure] = bad.validation_failures
    assert failure.finding_id == "F-9"
    assert any("NOPE-1" in r for r in failure.reasons)
    assert bad.complete is False

    good = _record([_finding("F-9", items=("X-01",))], {"F-9": _applied()})
    assert [f.finding_id for f in good.findings] == ["F-9"]
    assert good.validation_failures == ()
    # It is a finding rather than a validation failure, which is what this case is
    # about; it still blocks, because O229/G leaves no decision able to settle one.
    assert good.complete is False


def test_a_schema_invalid_finding_is_named_as_a_validation_failure():
    """Catches a crash (or a silent skip) on a finding with a blank reason: the
    product's schema refuses it and the record names it."""
    raw = _finding("F-3")
    raw["reason"] = "   "
    record = _record([raw], {})
    assert [v.finding_id for v in record.validation_failures] == ["F-3"]
    assert "schema" in record.validation_failures[0].reasons[0]


def test_a_decision_about_a_finding_nobody_returned_is_refused():
    """Catches mismatched findings and decisions files: a decision about
    nothing means the decisions were taken over some other review."""
    with pytest.raises(ValueError, match="did not return"):
        _record([_finding("F-1")], {"F-1": _applied(), "F-404": _applied()})


def test_applying_a_finding_does_not_change_the_snapshot_the_review_is_bound_to():
    """Catches "apply" writing state: the reopened snapshot `apply_finding`
    returns is unsealed and not kept; the record stays bound to the sealed
    snapshot it was performed against."""
    capsule, snapshot, _ = _state()
    before = snapshot.model_dump()
    record = ps.record_review("PX", capsule=capsule, findings=[_finding("F-1")],
                              snapshot=snapshot, decisions={"F-1": "apply"})
    assert snapshot.model_dump() == before
    assert record.snapshot_seal == snapshot.seal and snapshot.is_sealed()


# --------------------------------------------------------------------------- #
# the capsule
# --------------------------------------------------------------------------- #


def test_selection_prefers_pending_items_and_records_how_many_were_left_out():
    """Catches the silent cut: a capsule that showed a supervisor two thirds of
    a phase would produce a review that reads as complete. With a budget of
    two and four items, both pending items are kept over the verified ones, and
    the two verified ones are named as left out."""
    register = _register(_req("X-01"), _req("X-02"), _req("X-03"), _req("X-04"))
    matrix = _matrix(X_01="PROVEN", X_02="MISSING", X_03="PROVEN", X_04="PARTIAL")
    capsule = ps.capsule_for("PX", register=register, matrix=matrix, max_items=2)
    assert isinstance(capsule, ReviewCapsule)
    shown = {i.item_id: i.status for i in capsule.snapshot.items}
    assert shown == {"X-02": Status.EVIDENCE_PENDING, "X-04": Status.EVIDENCE_PENDING}
    sel = capsule.selection
    assert (sel.considered, sel.included, sel.left_out) == (4, 2, 2)
    assert sel.left_out_ids == ["X-01", "X-03"]
    assert "pending" in sel.rule.lower() or "open" in sel.rule.lower()
    # And the supervisor reading only the summaries is told as well.
    assert capsule.evidence_summaries[0].startswith("SELECTION:")
    assert "2 left out" in capsule.evidence_summaries[0]
    assert "X-01" in capsule.evidence_summaries[0]


def test_selection_leaves_nothing_out_when_the_budget_suffices():
    """Control for the binding budget: the only change is max_items, and the
    capsule shows every item and says nothing was left out."""
    register = _register(_req("X-01"), _req("X-02"), _req("X-03"), _req("X-04"))
    matrix = _matrix(X_01="PROVEN", X_02="MISSING", X_03="PROVEN", X_04="PARTIAL")
    capsule = ps.capsule_for("PX", register=register, matrix=matrix, max_items=4)
    assert len(capsule.snapshot.items) == 4
    assert capsule.selection.left_out == 0 and capsule.selection.left_out_ids == []
    assert "nothing left out" in capsule.evidence_summaries[0]


def test_selection_puts_phase_blocking_before_non_blocking():
    """Catches an optional provider crowding out a blocking item: a pending
    non-blocking item gives way to a verified blocking one."""
    register = _register(_req("X-01"), _req("X-02", core=False, optional=True))
    matrix = _matrix(X_01="PROVEN", X_02="MISSING")
    capsule = ps.capsule_for("PX", register=register, matrix=matrix, max_items=1)
    assert [i.item_id for i in capsule.snapshot.items] == ["X-01"]
    assert capsule.selection.left_out_ids == ["X-02"]
    # Control: with room for both, both are shown.
    both = ps.capsule_for("PX", register=register, matrix=matrix, max_items=2)
    assert [i.item_id for i in both.snapshot.items] == ["X-01", "X-02"]


def test_selection_only_takes_items_of_the_phase_under_review():
    """Catches another phase's items being shown -- the capsule's own check
    would refuse them, and a budget spent on them is a budget not spent here."""
    register = _register(_req("X-01"), _req("Y-01", phase="PY"))
    matrix = _matrix(X_01="MISSING", Y_01="MISSING")
    capsule = ps.capsule_for("PX", register=register, matrix=matrix)
    assert [i.item_id for i in capsule.snapshot.items] == ["X-01"]
    assert capsule.selection.considered == 1


def test_selection_records_evidence_summaries_it_had_no_room_for():
    """Catches the evidence budget cutting silently: with max_evidence=2 the
    selection line takes one slot, the pending item the other, and the verified
    items without a summary are named."""
    capsule, _, _ = _state()
    small = ps.capsule_for("PX", register=_register(), matrix=_matrix(
        X_01="PROVEN", X_02="MISSING", X_03="PROVEN"), max_evidence=2)
    assert len(small.evidence_summaries) == 2
    assert small.evidence_summaries[1].startswith("X-02")
    assert small.selection.evidence_left_out_ids == ["X-01", "X-03"]
    assert "evidence summary(ies) left out" in small.evidence_summaries[0]
    # Control: the default budget has room for all three.
    assert capsule.selection.evidence_left_out_ids == []


def test_the_capsule_snapshot_is_sealed_and_agrees_with_the_full_reconciliation():
    """Catches a capsule showing a supervisor statuses nobody reconciled."""
    capsule, snapshot, _ = _state()
    assert capsule.snapshot.is_sealed()
    full = {i.item_id: i.status for i in snapshot.items}
    assert all(full[i.item_id] is i.status for i in capsule.snapshot.items)


def test_an_unknown_phase_raises_rather_than_producing_an_empty_capsule():
    """Catches a review of nothing: the snapshot holds no item of phase PZ, and
    no capsule is built to find that out."""
    with pytest.raises(ValueError, match="PZ"):
        ps.capsule_for("PZ", register=_register(), matrix=_matrix(X_01="PROVEN"))


def test_unknown_phase_control_a_known_phase_builds_a_capsule():
    """Control for the unknown_phase case: only the phase name changes."""
    capsule = ps.capsule_for("PX", register=_register(), matrix=_matrix(X_01="PROVEN"))
    assert capsule.phase == "PX" and len(capsule.snapshot.items) == 3


# --------------------------------------------------------------------------- #
# binding and staleness
# --------------------------------------------------------------------------- #


def test_the_record_is_bound_to_the_capsule_seal_and_matrix_it_was_performed_against():
    """Catches a review that cannot say which board it reviewed."""
    capsule, snapshot, matrix = _state()
    record = ps.record_review("PX", capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={})
    assert record.capsule_digest == ps.capsule_digest(capsule)
    assert record.snapshot_seal == snapshot.seal
    assert record.matrix_digest == ps.matrix_digest(matrix)
    assert record.complete is True        # no findings at all is a complete answer


def test_stale_against_is_empty_when_neither_seal_nor_matrix_moved():
    """Control for the two staleness cases below."""
    capsule, snapshot, matrix = _state()
    record = ps.record_review("PX", capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={})
    assert ps.stale_against(record, snapshot=snapshot,
                            matrix_digest=ps.matrix_digest(matrix)) == []


def test_stale_against_reports_a_changed_snapshot_seal():
    """Catches last week's review passing as current: the board moved (X-02
    became PROVEN), so the seal changed."""
    capsule, snapshot, matrix = _state()
    record = ps.record_review("PX", capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={})
    moved = _matrix(X_01="PROVEN", X_02="PROVEN", X_03="PROVEN")
    new_snapshot, _, _ = ps.reconciled(_register(), moved)
    reasons = ps.stale_against(record, snapshot=new_snapshot,
                               matrix_digest=ps.matrix_digest(matrix))
    assert len(reasons) == 1 and "seal" in reasons[0]


def test_stale_against_reports_a_changed_matrix_digest():
    """Catches a re-measured matrix going unnoticed when the seal happens not
    to move (only the evidence text changed)."""
    capsule, snapshot, matrix = _state()
    record = ps.record_review("PX", capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={})
    remeasured = json.loads(json.dumps(matrix))
    remeasured["capabilities"][0]["evidence"] = ["src/elsewhere.py:9"]
    reasons = ps.stale_against(record, snapshot=snapshot,
                               matrix_digest=ps.matrix_digest(remeasured))
    assert len(reasons) == 1 and "matrix" in reasons[0]


def test_a_review_against_a_different_snapshot_than_its_capsule_is_refused():
    """Catches findings about one board being recorded against another."""
    capsule, _, _ = _state()
    other, _, _ = ps.reconciled(_register(), _matrix(X_01="PROVEN", X_02="PROVEN",
                                                     X_03="PROVEN"))
    with pytest.raises(ValueError, match="different snapshot"):
        ps.record_review("PX", capsule=capsule, findings=[], snapshot=other, decisions={})


# --------------------------------------------------------------------------- #
# Supervisor can FIND, reconciler can VERIFY
# --------------------------------------------------------------------------- #


def test_the_module_never_sets_a_closing_status():
    """Catches the binding deciding what only the reconciler (or a complete
    waiver) may decide. Control: the product's supervisor module, which names
    these statuses in order to refuse them, does contain them -- so the grep
    is capable of finding them."""
    source = (ROOT / "tools" / "programme_supervisor.py").read_text(encoding="utf-8")
    for forbidden in ("Status.VERIFIED", "WAIVED", "NOT_APPLICABLE"):
        assert forbidden not in source, forbidden
    control = (ROOT / "src" / "hoh" / "supervisor.py").read_text(encoding="utf-8")
    assert "Status.VERIFIED" in control and "WAIVED" in control


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _files(tmp_path, monkeypatch, register=None, matrix=None):
    (tmp_path / "register.json").write_text(json.dumps(register or _register()))
    (tmp_path / "matrix.json").write_text(json.dumps(
        matrix or _matrix(X_01="PROVEN", X_02="MISSING", X_03="PROVEN")))
    monkeypatch.setattr(ps, "REGISTER", tmp_path / "register.json")
    monkeypatch.setattr(ps, "MATRIX", tmp_path / "matrix.json")


def test_a_missing_register_is_an_environment_gap_not_a_passing_review(tmp_path, monkeypatch):
    """Catches exit 0 without a register: an environment gap is not a pass."""
    monkeypatch.setattr(ps, "REGISTER", tmp_path / "nope.json")
    assert ps.main(["--phase", "P2"]) == 3


def test_main_emits_the_capsule_as_json_without_writing(tmp_path, monkeypatch, capsys):
    """Catches a capsule command that writes into the tree or prints no selection."""
    _files(tmp_path, monkeypatch)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert ps.main(["--phase", "PX"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["phase"] == "PX" and "selection" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_main_exit_code_follows_complete_a_finding_is_1_none_is_0(tmp_path, monkeypatch):
    """Catches an exit code that does not follow `complete`. Renamed with the O229/G
    doctrine: deciding a finding no longer changes the code, and the only thing that
    does is whether there were findings at all."""
    _files(tmp_path, monkeypatch)
    (tmp_path / "f.json").write_text(json.dumps([_finding("F-1")]))
    (tmp_path / "open.json").write_text(json.dumps({}))
    (tmp_path / "done.json").write_text(json.dumps({"F-1": _applied()}))
    (tmp_path / "none.json").write_text(json.dumps([]))
    base = ["--phase", "PX", "--findings", str(tmp_path / "f.json")]
    # A finding, decided or not, keeps the exit code at 1 (O229/G).
    assert ps.main([*base, "--decisions", str(tmp_path / "open.json"),
                    "--out", str(tmp_path / "r1.json")]) == 1
    assert ps.main([*base, "--decisions", str(tmp_path / "done.json"),
                    "--out", str(tmp_path / "r2.json")]) == 1
    # Exit 0 is reachable, and only by a review that found nothing.
    assert ps.main(["--phase", "PX", "--findings", str(tmp_path / "none.json"),
                    "--decisions", str(tmp_path / "open.json"),
                    "--out", str(tmp_path / "r3.json")]) == 0
    r1 = ps.load_record(json.loads((tmp_path / "r1.json").read_text()))
    r2 = ps.load_record(json.loads((tmp_path / "r2.json").read_text()))
    r3 = ps.load_record(json.loads((tmp_path / "r3.json").read_text()))
    assert (r1.complete, r1.blocking_findings) == (False, ("F-1",))
    assert (r2.complete, r2.blocking_findings) == (False, ("F-1",))
    assert (r3.complete, r3.blocking_findings) == (True, ())
    # A record is never overwritten.
    assert ps.main([*base, "--decisions", str(tmp_path / "done.json"),
                    "--out", str(tmp_path / "r1.json")]) == 2


def test_this_programmes_p2_capsule_is_built_from_the_real_register():
    """Catches the binding working only on synthetic data, and a status that is copied
    rather than derived.

    Re-stated 2026-10-12 (strand frontier3). Until the O266 records merge this asserted
    that some P2 item was still EVIDENCE_PENDING ("P2 is the open frontier"); O266
    recorded every P2 row PROVEN, so that premise is false and the true state is the one
    below: P2's capability coverage is closed -- every P2 item VERIFIED, derived from the
    matrix -- and P2 is still not closed, because its exit is held by the checklist-gate
    instruments of `tools/build_plan.py` (see `tests/test_programme_checklist.py`), of
    which the supervisor review is the one this binding serves: the preserved reviews
    still leave release-blocking findings open against this very snapshot, so the capsule
    is the brief of a review that is due, not the record of one that closed the phase.

    Re-stated after the v0.3.0 tag (strand ptests, O267 section 8(a)) because P2's exit is
    a live measurement. The closure review (`20261013T093422Z`) answered the findings the
    earlier reviews left open, and the matrix and the register move with every regeneration,
    so "every item VERIFIED" and "findings still open" are states of one commit. What this
    pins is the derivation and the records: one item per P2 row and only phase P2; each
    item VERIFIED exactly when its matrix row is PROVEN and pending otherwise, on the P2
    capsule and on the capsule of the first phase whose rows are not all PROVEN (the
    control, which has a pending item); the open findings are a subset of the preserved
    release-blocking ones; and, bounded to the reviews before the closure review, they are
    exactly the findings that review's `findings.json` carries.
    """
    if not ps.REGISTER.is_file() or not ps.MATRIX.is_file():
        pytest.skip("the register and matrix are internal")
    register = json.loads(ps.REGISTER.read_text(encoding="utf-8"))
    matrix = json.loads(ps.MATRIX.read_text(encoding="utf-8"))
    measured = {c["id"]: c.get("status") for c in matrix["capabilities"]}
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    p2 = [r["id"] for r in register["requirements"] if r["target_phase"] == "P2"]
    assert capsule.selection.considered == len(p2)
    assert {i.phase for i in capsule.snapshot.items} == {"P2"}

    def derived(snapshot):
        """Derived, not copied: an item is VERIFIED exactly when its matrix row is PROVEN
        (an item `P2-07+<extra>` shares the measurement of its requirement)."""
        assert snapshot.items
        for item in snapshot.items:
            proven = measured.get(item.item_id.split("+", 1)[0]) == "PROVEN"
            want = Status.VERIFIED if proven else Status.EVIDENCE_PENDING
            assert item.status is want, (item.item_id, item.status)

    derived(capsule.snapshot)
    # The control, on the real data: the first phase whose rows are not all PROVEN has a
    # pending item, so the derivation above is shown to be able to say "not VERIFIED".
    controls = [r["target_phase"] for r in register["requirements"]
                if measured.get(r["id"]) != "PROVEN"]
    assert controls, "every register row is PROVEN: no control phase"
    other = ps.capsule_for(controls[0], register=register, matrix=matrix)
    assert any(i.status is Status.EVIDENCE_PENDING for i in other.snapshot.items)
    derived(other.snapshot)

    # Which findings are open is the preserved reviews' state, not pinned: whatever is
    # open is a release-blocking finding the checklist binding lists.
    still_open = ps.open_findings_for("P2", snapshot=capsule.snapshot)
    assert set(still_open) <= set(
        ps.programme_checklist._preserved_release_blocking_findings())
    # The control in the records, which do not move: before the closure review, what was
    # open is what that review was given and carries.
    stamp = "20261013T093422Z"
    earlier = ps.open_findings_for("P2", snapshot=capsule.snapshot, before=stamp)
    assert earlier
    carried = json.loads((ps.HOH / "dogfood/p2-gate" / stamp / "findings.json")
                         .read_text(encoding="utf-8"))
    assert set(earlier) == {f["finding_id"] for f in carried}


# --------------------------------------------------------------------------- #
# O224/D2 -- an apply is not a repair
# --------------------------------------------------------------------------- #


def test_a_bare_apply_settles_nothing_and_is_named_as_applied_without_evidence():
    """Catches the failure this field exists for: the first P2 review recorded
    fourteen findings as applied and was `complete: true`, while at least three of
    the repairs it claimed did not exist anywhere in the tree. Agreeing with a
    supervisor is not the same act as repairing what it found, so an apply that
    cites no evidence leaves the finding blocking and says so by name."""
    record = _record([_finding("F-1")], {"F-1": "apply"})
    assert record.complete is False
    assert record.blocking_findings == ("F-1",)
    assert record.applied == ("F-1",)
    assert record.applied_without_evidence == ("F-1",)
    assert record.repair_evidence == {}


def test_an_apply_with_an_evidence_reference_is_recorded_and_still_blocks():
    """Was the control for "an apply with evidence settles it". Under O229/G the
    reference is recorded and reported -- `applied_without_evidence` is empty, which is
    the diagnostic the field exists for -- and the finding still blocks. `"0.0"` passing
    the shape check is why this stopped being load-bearing."""
    record = _record([_finding("F-1")], {"F-1": _applied("tools/preserve.py:1")})
    assert record.complete is False
    assert record.blocking_findings == ("F-1",)
    assert record.applied == ("F-1",)
    assert record.applied_without_evidence == ()
    assert record.repair_evidence == {"F-1": "tools/preserve.py:1"}


def test_a_decision_that_is_neither_a_string_nor_a_mapping_is_refused():
    """Catches a decisions file whose values are lists or numbers being read as a
    rejection reason: `str(decision)` would have made `[1]` a reason and recorded a
    rejection nobody wrote."""
    with pytest.raises(TypeError, match="neither a string nor a mapping"):
        _record([_finding("F-1")], {"F-1": ["apply"]})


# --------------------------------------------------------------------------- #
# O224/D1 -- currency must mean the matrix changed, not that the clock moved
# --------------------------------------------------------------------------- #


def test_two_measurements_of_one_unchanged_matrix_have_the_same_digest():
    """Catches the defect that made "this review is current" unreachable: the
    matrix carries the wall clock it was measured at, so a digest over the whole
    mapping differed on every measurement of an identical tree, and every review
    ever taken reported STALE against the matrix it was taken from."""
    first = {"measured_at_utc": "2026-09-24T06:00:00Z", "capabilities": [{"id": "X"}]}
    second = {"measured_at_utc": "2026-09-24T07:30:00Z", "capabilities": [{"id": "X"}]}
    assert first != second
    assert ps.matrix_digest(first) == ps.matrix_digest(second)


def test_the_control_is_a_matrix_whose_measurement_actually_changed():
    """Control for the case above: excluding the clock must not make the digest
    blind to the thing it exists to bind. One changed status, same timestamp."""
    before = {"measured_at_utc": "2026-09-24T06:00:00Z",
              "capabilities": [{"id": "X", "status": "PARTIAL"}]}
    after = {"measured_at_utc": "2026-09-24T06:00:00Z",
             "capabilities": [{"id": "X", "status": "PROVEN"}]}
    assert ps.matrix_digest(before) != ps.matrix_digest(after)


def test_a_review_of_an_untouched_matrix_is_not_stale_when_only_the_clock_moved():
    """The end-to-end form of the same failure, at the level the gate reads: the
    gate asks `stale_against`, and a gate that can never report "current" is a gate
    that can never be satisfied honestly."""
    capsule, snapshot, matrix = _state()
    record = ps.record_review("PX", capsule=capsule, findings=[],
                              snapshot=snapshot, decisions={})
    remeasured = {**matrix, "measured_at_utc": "2099-01-01T00:00:00Z"}
    assert ps.stale_against(record, snapshot=snapshot,
                            matrix_digest=ps.matrix_digest(remeasured)) == []


def test_a_review_that_found_nothing_is_complete():
    """The positive control for the O229/G doctrine, and the reason it is not "the
    gate can never close": a supervisor that returned no findings has given a complete
    answer, and a review of it is complete."""
    record = _record([], {})
    assert record.complete is True
    assert record.blocking_findings == ()


def test_a_no_action_finding_is_a_finding_of_nothing_and_does_not_block():
    """The other half: `NO_ACTION` is a supervisor saying "nothing here", which is an
    answer about the thing it looked at, so it does not hold the phase open."""
    record = _record([_no_action("F-0")], {})
    assert record.complete is True
    assert record.blocking_findings == ()


def test_findings_that_are_not_objects_are_refused_by_the_digest():
    """O227/G1: `dict()` accepts a list of key/value pairs, so the same findings
    re-serialised as pair lists digested identically -- while the gate's accounting,
    which counted only `dict` entries, saw none of them, and a record claiming zero
    findings passed over a file holding fourteen. Two readers of one file must not
    disagree about what is in it."""
    real = [_finding("F-1"), _finding("F-2", items=("X-02",))]
    pairs = [list(f.items()) for f in real]
    with pytest.raises(TypeError, match="not a mapping"):
        ps.findings_digest(pairs)
    assert ps.findings_digest(real) == ps.findings_digest(list(reversed(real)))


def test_a_no_action_that_calls_itself_critical_is_not_a_finding_of_nothing():
    """O228/J3, at the level where it is computed: the kind alone was the whole test, so
    a relabelled critical finding was silence with its reason intact."""
    loud = _record([_no_action("F-0", severity="critical")], {})
    assert loud.blocking_findings == ("F-0",)
    assert loud.complete is False
    quiet = _record([_no_action("F-0", severity="low")], {})
    assert quiet.blocking_findings == ()
    assert quiet.complete is True


# --------------------------------------------------------------------------- #
# O231/N2: a later review resolves a finding by naming the review that raised it
# --------------------------------------------------------------------------- #
#
# `resolution_state` keyed on the bare finding id, so a later `NO_ACTION` of an id
# resolved any earlier finding of that id and nothing checked that the resolver was about
# *that* finding. The ten ids the review of 20260924T134525Z settled stay settled by the
# rule of their time; a review stamped after `NAMED_RESOLUTION_AFTER` must name what it
# resolves, and the canonical brief has to tell its supervisor how.

RAISED = "20260924T0030Z"          # a review that raised F-1 and F-2
LATER = "20261012T0900Z"           # after the cutoff
BEFORE_CUTOFF = "20260925T0000Z"


def _ref(review: str, fid: str) -> str:
    """The evidence ref that names a raised finding, spelled out and not derived: the
    format is the contract, so the tests do not call the function that produces it."""
    return f"dogfood/p2-gate/{review}/findings.json#{fid}"


def _quiet(fid, *refs, severity="low") -> dict:
    """A supervisor saying there is nothing at `fid`, citing `refs`."""
    return {**_no_action(fid, severity=severity), "evidence_refs": list(refs)}


def _open_after(*reviews) -> list[str]:
    still_open, _resolved = ps.resolution_state(list(reviews))
    return sorted(still_open)


def test_the_cutoff_is_the_day_of_the_change_and_a_stamp_not_the_clock():
    """The constant is pinned because moving it re-judges history. It is a stamp, read
    through `_stamp_key` like every other review comparison, never `datetime.now()`."""
    assert ps.NAMED_RESOLUTION_AFTER == "20261011T000000Z"
    assert ps._stamp_key(ps.NAMED_RESOLUTION_AFTER) == ("20261011", "000000")
    assert ps.review_ref("20260924T0030Z", "F-1") == \
        "dogfood/p2-gate/20260924T0030Z/findings.json#F-1"


@pytest.mark.parametrize("refs", [
    pytest.param([], id="names-nothing"),
    pytest.param(["dogfood/p2-gate/20260924T130000Z/findings.json#F-1"], id="another-review"),
    pytest.param(["dogfood/p2-gate/20260924T0030Z/findings.json#F-2"], id="another-finding"),
    pytest.param(["dogfood/p2-gate/20260924T0030Z/findings.json"], id="no-anchor"),
    pytest.param(["20260924T0030Z"], id="the-stamp-alone"),
    pytest.param([" dogfood/p2-gate/20260924T0030Z/findings.json#F-1"], id="leading-space"),
    pytest.param(["dogfood/p2-gate/20260924T0030Z/findings.json#F-1 "], id="trailing-space"),
    pytest.param(["DOGFOOD/P2-GATE/20260924T0030Z/findings.json#F-1"], id="upper-case"),
    pytest.param(["x/dogfood/p2-gate/20260924T0030Z/findings.json#F-1"], id="path-prefix"),
    pytest.param([None, 5, ["dogfood/p2-gate/20260924T0030Z/findings.json#F-1"]],
                 id="not-strings"),
    pytest.param("dogfood/p2-gate/20260924T0030Z/findings.json#F-1", id="a-string-not-a-list"),
    pytest.param(None, id="none"),
])
def test_a_later_no_action_that_does_not_name_the_raising_review_resolves_nothing(refs):
    """Wrong or missing name: the finding stays open, whatever the id spells."""
    raised = (RAISED, [_finding("F-1"), _finding("F-2", items=("X-02",))])
    quiet = {**_quiet("F-1"), "evidence_refs": refs}
    still_open, resolved = ps.resolution_state([raised, (LATER, [quiet])])
    assert sorted(still_open) == ["F-1", "F-2"] and resolved == []


def test_a_no_action_that_resolved_nothing_is_listed_with_its_review():
    """`unnamed_resolutions` is how the board says why a finding the supervisor answered
    is still open; a finding it did resolve, and one answered before the cutoff, are not
    listed."""
    raised = (RAISED, [_finding("F-1"), _finding("F-2", items=("X-02",))])
    later = (LATER, [_quiet("F-1"), _quiet("F-2", _ref(RAISED, "F-2"))])
    assert ps.unnamed_resolutions([raised, later]) == [(LATER, "F-1")]
    assert ps.unnamed_resolutions([raised, (BEFORE_CUTOFF, [_quiet("F-1")])]) == []
    named_later = (LATER + "1", [_quiet("F-1", _ref(RAISED, "F-1"))])
    assert ps.unnamed_resolutions([raised, later, named_later]) == []


def test_a_later_no_action_that_names_the_raising_review_resolves_it():
    """The positive control: only the evidence ref differs from the case above."""
    raised = (RAISED, [_finding("F-1"), _finding("F-2", items=("X-02",))])
    named = _quiet("F-1", _ref(RAISED, "F-1"))
    still_open, resolved = ps.resolution_state([raised, (LATER, [named])])
    assert sorted(still_open) == ["F-2"] and resolved == ["F-1"]
    # one name among others is enough, and the order of the findings does not matter
    mixed = _quiet("F-1", "tools/x.py#y", _ref(RAISED, "F-1"), "docs/z.md")
    assert _open_after(raised, (LATER, [mixed])) == ["F-2"]
    assert _open_after((LATER, [mixed]), raised) == ["F-2"]


def test_a_review_stamped_up_to_the_cutoff_still_resolves_by_the_bare_id():
    """Grandfathering by the clock of the review, not by the id: the stamp at the cutoff
    and every earlier one keep the rule they were written under, and one second after it
    does not. The 20260924T134525Z review is why."""
    raised = (RAISED, [_finding("F-1")])
    assert _open_after(raised, (BEFORE_CUTOFF, [_quiet("F-1")])) == []
    assert _open_after(raised, ("20261011T000000Z", [_quiet("F-1")])) == []
    assert _open_after(raised, ("20261011T000001Z", [_quiet("F-1")])) == ["F-1"]
    assert _open_after(raised, ("20261011T0000Z", [_quiet("F-1")])) == []   # same instant


def test_the_ten_ids_the_last_review_settled_stay_settled():
    """F3 F4 F5 F6 F9 F12 F14 M2 M3 M10, by the shape the preserved reviews have: raised
    in two reviews, answered by bare id with no `evidence_refs` by one stamped before the
    cutoff. The same open set and the same resolved list as `resolution_state` gave
    before this change -- nothing was re-judged."""
    settled = ["F3", "F4", "F5", "F6", "F9", "F12", "F14", "M2", "M3", "M10"]
    still = ["F1", "M1", "N1"]
    first = ("20260924T0030Z", [_finding(i) for i in settled[:7] + ["F1"]])
    second = ("20260924T130000Z", [_finding(i) for i in settled[7:] + ["M1"]])
    third = ("20260924T134525Z",
             [_finding("N1")] + [{**_no_action(i), "evidence_refs": []} for i in settled])
    still_open, resolved = ps.resolution_state([third, second, first])
    assert sorted(still_open) == sorted(still)
    assert resolved == sorted(settled)


def test_the_preserved_reviews_leave_the_same_open_and_resolved_sets():
    """The same statement over the real preserved reviews of this tree, so the synthetic
    shape above cannot drift from what is on disk: the reviews stamped up to the cutoff
    leave exactly the ids the board has always read -- ten resolved, nineteen open."""
    from conftest import Withheld, needs_evidence
    needs_evidence(Withheld(
        "dogfood/p2-gate/20260924T134525Z/findings.json", "internal-working-document",
        "the preserved supervisor reviews are internal working documents"))
    history = [(name, found) for name, found in ps.preserved_reviews(
        ps.REVIEW_ROOT, repo=ps.HOH)
        if ps._stamp_key(name) <= ps._stamp_key("20261011T000000Z")]
    assert [name for name, _ in history][:3] == [
        "20260924T0030Z", "20260924T130000Z", "20260924T134525Z"]
    still_open, resolved = ps.resolution_state(history)
    assert resolved == sorted(["F3", "F4", "F5", "F6", "F9", "F12", "F14", "M2", "M3", "M10"])
    assert sorted(still_open) == sorted([
        "F1", "F2", "F7", "F8", "F10", "F11", "F13",
        "M1", "M4", "M5", "M6", "M7", "M8", "M9", "M12", "N1", "N2", "N3", "N4"])


def test_a_review_does_not_resolve_its_own_finding():
    """A review that raises F-1 and, in the same file, names itself in a `NO_ACTION` of
    F-1 has contradicted itself, not looked again: only a *strictly earlier* review can
    have raised what this one resolves."""
    same = (LATER, [_finding("F-1"), _quiet("F-1", _ref(LATER, "F-1"))])
    assert _open_after(same) == ["F-1"]


def test_a_finding_raised_again_after_a_resolution_must_be_named_afresh():
    """The resolution belongs to the raise it named. Raised in A, resolved naming A,
    raised again in C: a later NO_ACTION that cites A resolves nothing; one that cites C
    does. (A review that raised it twice without a resolution holds it by either.)"""
    a = ("20261012T0100Z", [_finding("F-1")])
    b = ("20261012T0200Z", [_quiet("F-1", _ref(a[0], "F-1"))])
    c = ("20261012T0300Z", [_finding("F-1")])
    assert _open_after(a, b) == []
    assert _open_after(a, b, c) == ["F-1"]
    stale = ("20261012T0400Z", [_quiet("F-1", _ref(a[0], "F-1"))])
    assert _open_after(a, b, c, stale) == ["F-1"]
    fresh = ("20261012T0400Z", [_quiet("F-1", _ref(c[0], "F-1"))])
    assert _open_after(a, b, c, fresh) == []
    both = ("20261012T0150Z", [_finding("F-1")])
    twice = ("20261012T0400Z", [_quiet("F-1", _ref(both[0], "F-1"))])
    assert _open_after(a, both, twice) == []


def test_naming_does_not_depend_on_the_order_the_reviews_are_given():
    raised = (RAISED, [_finding("F-1")])
    named = (LATER, [_quiet("F-1", _ref(RAISED, "F-1"))])
    assert ps.resolution_state([raised, named]) == ps.resolution_state([named, raised])
    bare = (LATER, [_quiet("F-1")])
    assert ps.resolution_state([raised, bare]) == ps.resolution_state([bare, raised])


def test_a_no_action_that_calls_itself_critical_is_a_raise_not_a_resolution():
    """The severity rule is unchanged: naming a review does not make a critical
    `NO_ACTION` a finding of nothing."""
    raised = (RAISED, [_finding("F-1")])
    loud = (LATER, [_quiet("F-1", _ref(RAISED, "F-1"), severity="critical")])
    assert _open_after(raised, loud) == ["F-1"]


# --- the brief tells the supervisor which review raised each open finding ---------

def _brief_tree(tmp_path: Path, **reviews):
    """`reviews` maps a stamp to the findings it returned; written where
    `preserved_reviews` reads them. Returns (register, matrix, review root, repo)."""
    root = tmp_path / "dogfood" / "p2-gate"
    for stamp, found in reviews.items():
        (root / stamp).mkdir(parents=True)
        (root / stamp / "findings.json").write_text(json.dumps(found), encoding="utf-8")
    return _register(), _matrix(X_01="PROVEN", X_02="MISSING", X_03="PROVEN"), root, tmp_path


def _brief(tmp_path, before=None, **reviews):
    register, matrix, root, repo = _brief_tree(tmp_path, **reviews)
    return ps.canonical_brief(register=register, matrix=matrix, phase="PX",
                              review_root=root, before=before, repo=repo)


def test_the_canonical_brief_says_which_review_raised_each_open_finding(tmp_path):
    """Without this a fresh supervisor cannot resolve anything, whatever it finds: the
    exact string it must cite is derived here, per finding, from the preserved reviews."""
    brief = _brief(tmp_path, **{RAISED: [_finding("F-1"), _finding("F-2", items=("X-02",))],
                                 "20260924T130000Z": [_finding("M-1")]})
    by_id = {o.finding_id: o for o in brief.open_finding_origins}
    assert sorted(by_id) == ["F-1", "F-2", "M-1"]
    assert by_id["F-1"].raised_by == [RAISED]
    assert by_id["F-1"].resolve_with == [_ref(RAISED, "F-1")]
    assert by_id["M-1"].resolve_with == [_ref("20260924T130000Z", "M-1")]
    assert brief.resolution_rule == ps.RESOLUTION_RULE
    assert "evidence_refs" in brief.resolution_rule and ps.NAMED_RESOLUTION_AFTER \
        in brief.resolution_rule
    printed = json.loads(brief.model_dump_json(by_alias=True))      # what --canonical-brief prints
    assert printed["open_finding_origins"][0]["resolve_with"]
    assert printed["resolution_rule"]


def test_a_brief_with_no_open_finding_serializes_exactly_as_it_did_before(tmp_path):
    """The two new fields are omitted when empty, so a capsule preserved before they
    existed, and a brief of a tree with nothing open, dump and digest as they always did;
    a record bound to such a capsule keeps its digest."""
    brief = _brief(tmp_path)
    assert brief.open_findings == [] and brief.open_finding_origins == []
    printed = json.loads(brief.model_dump_json(by_alias=True))
    assert "open_finding_origins" not in printed and "resolution_rule" not in printed
    again = ps.ProgrammeCapsule.model_validate(printed)
    assert ps.capsule_digest(again) == ps.capsule_digest(brief)
    assert ps._sha256(printed) == ps.capsule_digest(brief)


def test_end_to_end_only_a_review_that_names_the_raiser_changes_the_next_brief(tmp_path):
    """The brief is built, a later review is written three ways, and the brief built
    after it carries the finding exactly when the review did not name its raiser."""
    raised = {RAISED: [_finding("F-1")]}
    assert [f.finding_id for f in _brief(tmp_path / "a", **raised).open_findings] == ["F-1"]

    cited = _ref(RAISED, "F-1")
    for label, refs, still_carried in (("names it", [cited], False),
                                       ("names nothing", [], True),
                                       ("names another review",
                                        [_ref("20260924T130000Z", "F-1")], True)):
        brief = _brief(tmp_path / label.replace(" ", "-"),
                       **raised, **{LATER: [_quiet("F-1", *refs)]})
        carried = [f.finding_id for f in brief.open_findings]
        assert (carried == ["F-1"]) is still_carried, label
        assert (brief.open_finding_origins != []) is still_carried, label


def test_the_brief_a_review_was_given_is_the_brief_the_gate_rebuilds_after_it(tmp_path):
    """What the gate does: it rebuilds the brief `before` the review under check and
    compares digests with the preserved capsule. Before this the rebuild already held the
    review's own raises and resolutions, so a review that settled or raised anything could
    never match its own brief -- built both ways, here. The review is the same one in all
    three cases; only what it returns differs."""
    cited = _ref(RAISED, "F-1")
    for label, returned in (
            ("resolves every open finding",
             [_quiet("F-1", cited), _quiet("F-2", _ref(RAISED, "F-2"))]),
            ("resolves", [_quiet("F-1", cited)]),
            ("raises", [_finding("F-9", items=("X-02",))]),
            ("returns nothing", [])):
        root = tmp_path / label.replace(" ", "-")
        given = _brief(root, **{RAISED: [_finding("F-1"), _finding("F-2", items=("X-02",))]})
        register, matrix, review_root, repo = (_register(), _matrix(
            X_01="PROVEN", X_02="MISSING", X_03="PROVEN"), root / "dogfood" / "p2-gate", root)
        (review_root / LATER).mkdir()
        (review_root / LATER / "findings.json").write_text(json.dumps(returned),
                                                          encoding="utf-8")
        rebuilt = ps.canonical_brief(register=register, matrix=matrix, phase="PX",
                                     review_root=review_root, before=LATER, repo=repo)
        assert ps.capsule_digest(rebuilt) == ps.capsule_digest(given), label
        # and the operator's brief after the review (`before=None`) does see it
        after = ps.canonical_brief(register=register, matrix=matrix, phase="PX",
                                   review_root=review_root, before=None, repo=repo)
        assert (ps.capsule_digest(after) != ps.capsule_digest(given)) is bool(returned), label


def test_the_resolver_shape_in_the_brief_is_a_finding_the_product_accepts(tmp_path):
    """PM4. `supervisor.validate` refuses a `NO_ACTION` that names a phase, and one
    refused finding makes the whole review INCOMPLETE; so the brief says what a resolver
    looks like, with an example, and the example is checked here against the product's
    own validator, evidence-shape check and resolution rule -- filled in, it validates,
    reads as a finding of nothing, and resolves what it names."""
    from hoh import supervisor

    brief = _brief(tmp_path, **{RAISED: [_finding("F-1")]})
    assert json.dumps(ps.RESOLVER_EXAMPLE, sort_keys=True) in brief.resolution_rule
    origin = brief.open_finding_origins[0]

    filled = {**ps.RESOLVER_EXAMPLE, "finding_id": "F-1", "review_id": "R-later",
              "reason": "looked at the item again and nothing is there",
              "evidence_refs": [origin.resolve_with[0]],
              "supervisor_provider": "independent-supervisor"}
    capsule, snapshot, _ = _state()
    assert supervisor.validate(filled, snapshot=snapshot) == []
    assert ps.evidence_reference_problems(filled["evidence_refs"][0]) == []
    assert ps._quiet(filled)
    assert _open_after((RAISED, [_finding("F-1")]), (LATER, [filled])) == []

    # why the example names no item: a phase is refused for NO_ACTION by the product
    names_the_phase = {**filled, "item_ids": ["PX"]}
    assert supervisor.validate(names_the_phase, snapshot=snapshot)
    names_an_item = {**filled, "item_ids": ["X-01"]}
    assert supervisor.validate(names_an_item, snapshot=snapshot) == []
    # and a proposed action is refused for it
    assert supervisor.validate({**filled, "proposed_action": "do it"}, snapshot=snapshot)


def test_the_capsule_refuses_origins_that_do_not_describe_what_it_carries(tmp_path):
    """The validator of `ProgrammeCapsule`: origins must name exactly the carried open
    findings, once each, and come with the rule that reads them. A capsule file edited
    to disagree with itself is not a brief (the gate reads that as "cannot be read as a
    capsule")."""
    brief = _brief(tmp_path, **{RAISED: [_finding("F-1"), _finding("F-2", items=("X-02",))]})
    body = json.loads(brief.model_dump_json(by_alias=True))
    assert len(body["open_finding_origins"]) == 2
    ps.ProgrammeCapsule.model_validate(body)                       # the control

    fewer = {**body, "open_finding_origins": body["open_finding_origins"][:1]}
    foreign = {**body, "open_finding_origins": [
        {**body["open_finding_origins"][0], "finding_id": "F-9"},
        body["open_finding_origins"][1]]}
    twice = {**body, "open_finding_origins": [body["open_finding_origins"][0]] * 2}
    no_rule = {**body, "resolution_rule": ""}
    for label, edited in (("fewer", fewer), ("foreign id", foreign), ("twice", twice),
                          ("no rule", no_rule)):
        with pytest.raises(ValidationError):
            ps.ProgrammeCapsule.model_validate(edited)
        assert label


def test_a_finding_two_reviews_hold_open_names_both_raisers(tmp_path):
    """`resolve_with` is "one per raiser": raised in two reviews and resolved by neither, it
    is carried once, with both reviews in `raised_by` and a ref for each -- and either ref
    resolves it (disclosed: naming one of several raisers resolves the id)."""
    second = "20260925T0000Z"
    reviews = [(RAISED, [_finding("F-1")]), (second, [_finding("F-1")])]
    assert ps.resolution_origins(reviews) == {"F-1": [RAISED, second]}
    brief = _brief(tmp_path, **{RAISED: [_finding("F-1")], second: [_finding("F-1")]})
    origin = brief.open_finding_origins[0]
    assert origin.raised_by == [RAISED, second]
    assert origin.resolve_with == [_ref(RAISED, "F-1"), _ref(second, "F-1")]
    for review in (RAISED, second):
        assert _open_after(*reviews, (LATER, [_quiet("F-1", _ref(review, "F-1"))])) == []
