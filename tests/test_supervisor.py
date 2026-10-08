"""The checklist supervisor and its typed findings (V3.3 P2-27): Supervisor can
FIND, Reconciler can VERIFY.

One case per rule, each next to a negative control that changes only the
condition under test and shows the verdict flipping -- a check that holds
whichever way the condition goes is a check of nothing. From the plan's list:
SV-02 (only schema-validated findings are accepted), SV-03 (a finding cannot
mutate programme state except through `apply_finding`, which only reopens and
never closes a gate), SV-05 (a false finding can be rejected, and the
rejection is recorded), and the bounded review capsule.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.checklist import (
    ChecklistItem,
    Criticality,
    ItemEvidence,
    Snapshot,
    Status,
    phase_gate,
    reconcile,
)
from hoh.contracts import Strict
from hoh.supervisor import (
    FindingKind,
    FindingStatus,
    ReviewCapsule,
    SupervisorFinding,
    apply_finding,
    reject_finding,
    validate,
)

D1 = "d1-source"


def _item(item_id="CL-A", *, phase="P1", refs=("e1",),
          crit=Criticality.P0_RELEASE_BLOCKING) -> ChecklistItem:
    return ChecklistItem(
        item_id=item_id, phase=phase, title=f"title of {item_id}",
        requirement_ref="R1", source_digest=D1, criticality=crit,
        evidence_refs=list(refs),
    )


def _sealed(*ids, phase="P1") -> Snapshot:
    """A snapshot from `reconcile` in which every item is VERIFIED."""
    ids = ids or ("CL-A",)
    return reconcile(
        [_item(i, phase=phase) for i in ids],
        evidence={i: ItemEvidence(present_refs=["e1"], source_digest=D1) for i in ids},
        generation=1)


def _finding(**kw) -> SupervisorFinding:
    fields = dict(finding_id="F-1", review_id="R-1", kind=FindingKind.PROPOSE_REOPEN,
                  severity="high", item_ids=["CL-A"], reason="digest moved",
                  evidence_refs=["ev-1"], proposed_action="reopen CL-A",
                  supervisor_provider="test")
    fields.update(kw)
    return SupervisorFinding(**fields)


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_finding_kinds_are_exactly_the_specified_members_in_order():
    """A kind added or reordered silently changes what a serialised finding
    means; the spec fixes the list, so it is pinned here."""
    assert [k.value for k in FindingKind] == [
        "NO_ACTION", "PROPOSE_REOPEN", "PROPOSE_REVALIDATION", "PROPOSE_REPAIR_NODE",
        "PROPOSE_MISSING_WORK_ITEM", "PROPOSE_DEPENDENCY", "PHASE_NOT_READY",
        "RELEASE_NOT_READY"]
    assert all(k.name == k.value for k in FindingKind)


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_finding_with_a_blank_reason_is_refused_at_construction(reason):
    """A finding nobody can act on is noise. Accepted with a blank or
    whitespace-only reason, it would reach Core as an unexplained reopen."""
    with pytest.raises(ValidationError):
        _finding(reason=reason)
    # Control: the same finding with a reason constructs, and starts OPEN.
    assert _finding(reason="digest moved").status is FindingStatus.OPEN


def test_sv02_an_unknown_field_on_a_finding_is_refused():
    """SV-02: the finding sits on the project's Strict base. Without it, a
    supervisor could hand in `status_override="VERIFIED"` and have the field
    dropped silently -- or, worse, read by some later consumer."""
    assert issubclass(SupervisorFinding, Strict)
    with pytest.raises(ValidationError):
        SupervisorFinding(**_finding().model_dump(), status_override="VERIFIED")
    # Control: the same fields without the extra one construct.
    SupervisorFinding(**_finding().model_dump())


def test_sv02_validate_refuses_a_finding_that_is_not_schema_valid():
    """SV-02: only schema-validated findings are accepted. A raw dict with a
    bad kind, or an instance whose reason was blanked after construction
    (the model does not validate assignment), must not pass validate()."""
    snap = _sealed()
    bad = _finding().model_dump()
    bad["kind"] = "MARK_VERIFIED"
    assert any("not schema-valid" in r for r in validate(bad, snapshot=snap))
    blanked = _finding()
    blanked.reason = ""
    assert any("not schema-valid" in r for r in validate(blanked, snapshot=snap))
    # Control: the same dict with a real kind validates clean.
    good = _finding().model_dump()
    assert validate(good, snapshot=snap) == []


# --------------------------------------------------------------------------- #
# Rule 1 -- item_ids must be in the snapshot
# --------------------------------------------------------------------------- #


def test_rule1_a_finding_naming_an_unknown_item_is_refused_with_that_id_named():
    """Rule 1: a finding about CL-ZZ, which the checklist does not contain, is
    either a typo or a hallucination; acted on, it would do nothing visible
    and look handled."""
    reasons = validate(_finding(item_ids=["CL-A", "CL-ZZ"]), snapshot=_sealed())
    assert len(reasons) == 1 and "'CL-ZZ'" in reasons[0]
    # Control: only known ids -- accepted.
    assert validate(_finding(item_ids=["CL-A"]), snapshot=_sealed()) == []


def test_rule1_a_phase_name_is_not_an_item_for_an_item_level_finding():
    """Rule 1: 'P1' is allowed in item_ids for phase-level kinds only. A
    PROPOSE_REOPEN naming a phase would otherwise pass validation and reopen
    nothing."""
    assert validate(_finding(item_ids=["P1"]), snapshot=_sealed()) != []
    # Control: the same entry on a PHASE_NOT_READY finding is accepted.
    assert validate(_finding(kind=FindingKind.PHASE_NOT_READY, item_ids=["P1"],
                             proposed_action="hold the phase"),
                    snapshot=_sealed()) == []


# --------------------------------------------------------------------------- #
# Rule 2 -- NO_ACTION carries no action, every other kind carries one
# --------------------------------------------------------------------------- #


def test_rule2_no_action_with_a_proposed_action_is_refused():
    """Rule 2: a NO_ACTION finding that proposes something contradicts
    itself; Core could not tell which half the supervisor meant."""
    f = _finding(kind=FindingKind.NO_ACTION, proposed_action="reopen CL-A")
    assert any("NO_ACTION but proposes" in r for r in validate(f, snapshot=_sealed()))
    # Control: the same NO_ACTION finding without an action is accepted.
    for action in (None, "", "   "):
        f = _finding(kind=FindingKind.NO_ACTION, proposed_action=action)
        assert validate(f, snapshot=_sealed()) == []


@pytest.mark.parametrize("kind", [k for k in FindingKind if k is not FindingKind.NO_ACTION])
def test_rule2_an_actionable_kind_without_a_proposed_action_is_refused(kind):
    """Rule 2: any kind but NO_ACTION proposes something. Without an action,
    Core would have to guess what the supervisor wanted done."""
    ids = ["P1"] if kind in (FindingKind.PHASE_NOT_READY, FindingKind.RELEASE_NOT_READY) else ["CL-A"]
    f = _finding(kind=kind, item_ids=ids, proposed_action=None)
    assert any("proposes no action" in r for r in validate(f, snapshot=_sealed()))
    # Control: the same finding with an action is accepted.
    f = _finding(kind=kind, item_ids=ids, proposed_action="look at P1 again")
    assert validate(f, snapshot=_sealed()) == []


# --------------------------------------------------------------------------- #
# Rule 3 -- a decided finding is no input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [FindingStatus.REJECTED, FindingStatus.APPLIED])
def test_rule3_a_rejected_or_applied_finding_is_refused_as_input(status):
    """Rule 3: a finding already decided must not be acted on again -- a
    rejected one would be resurrected, an applied one would reopen twice."""
    extra = ({"rejection_reason": "false", "rejected_by": "core"}
             if status is FindingStatus.REJECTED else {})
    f = _finding(status=status, **extra)
    assert any(f"already {status.value}" in r for r in validate(f, snapshot=_sealed()))
    # Control: the same finding OPEN is accepted.
    assert validate(_finding(status=FindingStatus.OPEN), snapshot=_sealed()) == []


# --------------------------------------------------------------------------- #
# Rule 4 -- PHASE_NOT_READY says which phase
# --------------------------------------------------------------------------- #


def test_rule4_phase_not_ready_naming_no_phase_is_refused():
    """Rule 4: a phase verdict that does not say which phase is not
    actionable. 'P10' does not name 'P1' -- a substring check would let it
    through."""
    f = _finding(kind=FindingKind.PHASE_NOT_READY, item_ids=[],
                 proposed_action="hold P10 until the review is done")
    assert any("names no phase" in r for r in validate(f, snapshot=_sealed()))


def test_rule4_control_phase_not_ready_naming_the_phase_in_item_ids_is_accepted():
    """Negative control for rule 4: the phase named in item_ids."""
    f = _finding(kind=FindingKind.PHASE_NOT_READY, item_ids=["P1"],
                 proposed_action="hold until the review is done")
    assert validate(f, snapshot=_sealed()) == []


def test_rule4_control_phase_not_ready_naming_the_phase_in_the_action_is_accepted():
    """Negative control for rule 4: the phase named in proposed_action."""
    f = _finding(kind=FindingKind.PHASE_NOT_READY, item_ids=[],
                 proposed_action="hold P1 until the review is done")
    assert validate(f, snapshot=_sealed()) == []


# --------------------------------------------------------------------------- #
# SV-03 -- apply_finding reopens and nothing else
# --------------------------------------------------------------------------- #


def test_sv03_apply_finding_reopens_the_named_item_with_the_reason():
    """SV-03 positive case: the one thing a finding may do. Without it, a
    supervisor could find but never make the checklist show it."""
    snap = _sealed("CL-A", "CL-B")
    new, desc = apply_finding(_finding(), snapshot=snap)
    by_id = {i.item_id: i for i in new.items}
    assert by_id["CL-A"].status is Status.REOPENED
    assert by_id["CL-A"].reopen_reason == "digest moved"
    assert "F-1" in by_id["CL-A"].finding_refs
    assert by_id["CL-B"].status is Status.VERIFIED, "an unnamed item was touched"
    added = new.transitions[len(snap.transitions):]
    assert [(t.item_id, t.from_status, t.to_status) for t in added] == [
        ("CL-A", Status.VERIFIED, Status.REOPENED)]
    assert "\n" not in desc and "F-1" in desc and "CL-A" in desc


@pytest.mark.parametrize("to", [Status.VERIFIED, Status.WAIVED, Status.NOT_APPLICABLE])
def test_sv03_a_supervisor_cannot_set_a_closing_status(to):
    """SV-03: VERIFIED, WAIVED and NOT_APPLICABLE are decided by reconcile
    against evidence or a complete waiver. A supervisor able to set them
    would be able to accept its own candidate."""
    snap = _sealed()
    with pytest.raises(ValueError, match="only set REOPENED"):
        apply_finding(_finding(), snapshot=snap, to_status=to)
    # Control: the same call with REOPENED goes through.
    new, _ = apply_finding(_finding(), snapshot=snap, to_status=Status.REOPENED)
    assert new.items[0].status is Status.REOPENED


def test_sv03_a_supervisor_cannot_apply_a_finding_validate_rejects():
    """SV-03: apply_finding is gated by validate(). Without the gate a
    finding about an unknown item, or one proposing nothing, would still
    change the checklist."""
    snap = _sealed()
    for bad in (_finding(item_ids=["CL-ZZ"]), _finding(proposed_action=None)):
        assert validate(bad, snapshot=snap)
        with pytest.raises(ValueError, match="finding refused"):
            apply_finding(bad, snapshot=snap)
    # Control: the valid finding applies.
    apply_finding(_finding(), snapshot=snap)


def test_sv03_a_supervisor_cannot_close_a_gate_through_apply_finding():
    """SV-03: the snapshot apply_finding returns is unsealed, because
    reconcile did not produce it. A result carrying the old seal -- or a
    freshly computed one -- would let a gate close on supervisor-made state."""
    snap = _sealed()
    assert snap.is_sealed() and phase_gate(snap, "P1").closed, \
        "control: the sealed, verified snapshot must close"
    new, _ = apply_finding(_finding(), snapshot=snap)
    assert new.seal is None and not new.is_sealed()
    verdict = phase_gate(new, "P1")
    assert not verdict.closed
    assert {b.item_id for b in verdict.blockers} == {"(snapshot)", "CL-A"}
    # The input is not mutated: purity, not just a correct output.
    assert snap.is_sealed() and snap.items[0].status is Status.VERIFIED
    assert snap.items[0].finding_refs == []


def test_sv03_a_supervisor_cannot_close_a_gate_even_with_a_finding_naming_no_item():
    """SV-03: a finding that reopens nothing (a phase-level verdict, a
    NO_ACTION) still returns an unsealed snapshot. Handing back the sealed
    input would make 'apply' a way to launder a snapshot through."""
    snap = _sealed()
    for f in (_finding(kind=FindingKind.RELEASE_NOT_READY, item_ids=["P1"],
                       proposed_action="do not release"),
              _finding(kind=FindingKind.NO_ACTION, item_ids=["CL-A"], proposed_action=None)):
        new, desc = apply_finding(f, snapshot=snap)
        assert "nothing reopened" in desc
        assert [i.status for i in new.items] == [Status.VERIFIED]
        assert not new.is_sealed() and not phase_gate(new, "P1").closed


def test_sv03_only_reconcile_against_evidence_closes_the_gate_again():
    """The supervisor finds, the reconciler verifies: after a reopen, the
    gate closes again only through reconcile -- here phase_gate re-deriving
    from evidence. This pins the documented boundary: a reopen alone is not a
    confirmed finding, and evidence decides."""
    new, _ = apply_finding(_finding(), snapshot=_sealed())
    evidence = {"CL-A": ItemEvidence(present_refs=["e1"], source_digest=D1)}
    assert phase_gate(new, "P1", evidence=evidence).closed
    # Control: with the finding confirmed in the evidence, it does not close.
    confirmed = {"CL-A": ItemEvidence(present_refs=["e1"], source_digest=D1,
                                      open_findings=["F-1"])}
    assert not phase_gate(new, "P1", evidence=confirmed).closed


# --------------------------------------------------------------------------- #
# SV-05 -- a false finding can be rejected, and the rejection is recorded
# --------------------------------------------------------------------------- #


def test_sv05_reject_records_status_reason_and_actor_and_leaves_the_input_open():
    """SV-05: a rejection that left no record would be indistinguishable from
    a finding that was silently dropped."""
    f = _finding()
    rejected = reject_finding(f, reason="digest did not move; stale cache",
                              rejected_by="core")
    assert rejected.status is FindingStatus.REJECTED
    assert rejected.rejection_reason == "digest did not move; stale cache"
    assert rejected.rejected_by == "core"
    assert f.status is FindingStatus.OPEN and f.rejection_reason is None


def test_sv05_a_rejected_finding_is_refused_by_validate_and_apply():
    """SV-05: once rejected, a finding must not reopen anything -- otherwise
    rejection would be a label, not a decision."""
    snap = _sealed()
    f = _finding()
    rejected = reject_finding(f, reason="false positive", rejected_by="core")
    assert any("already REJECTED" in r for r in validate(rejected, snapshot=snap))
    with pytest.raises(ValueError, match="already REJECTED"):
        apply_finding(rejected, snapshot=snap)
    # Control: the same finding, un-rejected, is accepted and applies.
    assert validate(f, snapshot=snap) == []
    apply_finding(f, snapshot=snap)


def test_sv05_reject_refuses_a_blank_reason_or_actor_and_a_decided_finding():
    """SV-05: a rejection without a reason cannot be audited, and rejecting
    twice would overwrite the first record."""
    f = _finding()
    for kw in ({"reason": "  ", "rejected_by": "core"},
               {"reason": "false", "rejected_by": ""}):
        with pytest.raises(ValueError):
            reject_finding(f, **kw)
    once = reject_finding(f, reason="false", rejected_by="core")
    with pytest.raises(ValueError, match="already REJECTED"):
        reject_finding(once, reason="again", rejected_by="core")
    # A REJECTED status without a recorded reason is refused at construction.
    with pytest.raises(ValidationError):
        _finding(status=FindingStatus.REJECTED)


# --------------------------------------------------------------------------- #
# the review capsule is bounded
# --------------------------------------------------------------------------- #


def test_capsule_over_max_items_fails_closed_naming_what_was_dropped():
    """'Do not dump historical chat': a capsule silently truncated to its
    bound would let a supervisor review part of the checklist as if it were
    all of it. It fails, and says which items would be lost."""
    snap = _sealed("CL-A", "CL-B", "CL-C")
    with pytest.raises(ValidationError) as exc:
        ReviewCapsule(phase="P1", snapshot=snap, max_items=2)
    msg = str(exc.value)
    assert "1 would be dropped" in msg and "CL-C" in msg and "max_items=2" in msg
    # Control: exactly at the bound is accepted.
    capsule = ReviewCapsule(phase="P1", snapshot=snap, max_items=3)
    assert len(capsule.snapshot.items) == 3


def test_capsule_over_max_items_in_open_findings_fails_closed():
    """The item bound covers the open findings too; otherwise a whole
    history of findings could be carried in under a small checklist."""
    snap = _sealed()
    findings = [_finding(finding_id=f"F-{n}") for n in range(3)]
    with pytest.raises(ValidationError, match="F-2"):
        ReviewCapsule(phase="P1", snapshot=snap, open_findings=findings, max_items=2)
    # Control: at the bound.
    ReviewCapsule(phase="P1", snapshot=snap, open_findings=findings[:2], max_items=2)


def test_capsule_over_max_evidence_fails_closed_naming_what_was_dropped():
    """The evidence summaries are 'a short list'. Over the bound, the capsule
    refuses and names the summaries that would be lost."""
    snap = _sealed()
    summaries = ["run 1 green", "run 2 green", "run 3 red"]
    with pytest.raises(ValidationError) as exc:
        ReviewCapsule(phase="P1", snapshot=snap, evidence_summaries=summaries,
                      max_evidence=2)
    msg = str(exc.value)
    assert "max_evidence=2" in msg and "run 3 red" in msg
    # Control: exactly at the bound is accepted.
    ReviewCapsule(phase="P1", snapshot=snap, evidence_summaries=summaries[:2],
                  max_evidence=2)


def test_capsule_refuses_an_evidence_summary_that_is_a_dumped_history():
    """A 'summary' that is a pasted transcript defeats the item count. The
    length bound catches it instead of cutting it silently."""
    snap = _sealed()
    with pytest.raises(ValidationError, match="max_summary_chars=20"):
        ReviewCapsule(phase="P1", snapshot=snap, evidence_summaries=["x" * 21],
                      max_summary_chars=20)
    # Control: at the bound.
    ReviewCapsule(phase="P1", snapshot=snap, evidence_summaries=["x" * 20],
                  max_summary_chars=20)


# --- the gaps this module's own QA reported about itself ---------------------------

def test_a_capsule_about_a_phase_the_snapshot_does_not_hold_is_refused():
    """Its own QA asked for this: a capsule for a nonexistent phase sends a
    supervisor to review something that is not there, and whatever it reports
    would be about nothing."""
    snapshot = _sealed("CL-A")
    ReviewCapsule(phase="P1", snapshot=snapshot)          # control: the real phase
    with pytest.raises(ValidationError) as caught:
        ReviewCapsule(phase="P9", snapshot=snapshot)
    assert "would report about nothing" in str(caught.value)


def test_a_capsule_carrying_a_decided_finding_is_refused():
    """A supervisor asked to weigh a closed finding is being asked to reopen a
    decision it may not take."""
    snapshot = _sealed("CL-A")
    open_one = _finding(item_ids=["CL-A"])
    ReviewCapsule(phase="P1", snapshot=snapshot, open_findings=[open_one])

    decided = reject_finding(open_one, rejected_by="the captain",
                             reason="the evidence was there after all")
    with pytest.raises(ValidationError) as caught:
        ReviewCapsule(phase="P1", snapshot=snapshot, open_findings=[decided])
    assert "already been decided" in str(caught.value)
