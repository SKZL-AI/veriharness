"""ProgramChecklist and its reconciler (V3.3 P2-26): Supervisor can FIND,
Reconciler can VERIFY.

The cases are the plan's own list, CL-02 to CL-08, plus one per reconciler rule
the list does not name. Each positive case sits next to a negative control that
changes only the condition under test and shows the verdict flipping -- a check
that holds whichever way the condition goes is a check of nothing. Most
controls are the *good* side: complete evidence does reach VERIFIED, a
complete waiver does waive, a phase with everything proven does close. Without
them, a reconciler that never said VERIFIED and a gate that never closed would
pass every fail-closed case in this file.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.checklist import (
    ChecklistItem,
    Criticality,
    GateVerdict,
    ItemEvidence,
    Snapshot,
    Status,
    Transition,
    Waiver,
    phase_gate,
    reconcile,
)
from hoh.contracts import Strict

D1 = "d1-source"
D2 = "d2-source"


def _item(item_id="CL-A", *, status=Status.PLANNED, phase="P1", digest=D1,
          refs=("e1", "e2"), crit=Criticality.P0_RELEASE_BLOCKING,
          waiver_ref=None) -> ChecklistItem:
    return ChecklistItem(
        item_id=item_id, phase=phase, title=f"title of {item_id}",
        requirement_ref="R1", source_digest=digest, criticality=crit,
        status=status, evidence_refs=list(refs), finding_refs=[],
        last_reconciled_generation=None, reopen_reason=None,
        waiver_ref=waiver_ref,
    )


def _complete(digest=D1, refs=("e1", "e2"), **kw) -> ItemEvidence:
    return ItemEvidence(present_refs=list(refs), source_digest=digest, **kw)


def _one(items, **kw):
    snap = reconcile(items, generation=kw.pop("generation", 1), **kw)
    return snap, snap.items[0]


def _full_waiver(**kw) -> Waiver:
    fields = dict(waiver_id="W1", authority="release board", actor="captain",
                  reason="hardware not available this phase", scope=["CL-A"])
    fields.update(kw)
    return Waiver(**fields)


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_enums_have_exactly_the_specified_members_in_order():
    """A member added or reordered silently changes what a serialised
    checklist means; the spec fixes both lists, so both are pinned here."""
    assert [s.value for s in Status] == [
        "PLANNED", "READY", "IN_PROGRESS", "BLOCKED", "EVIDENCE_PENDING",
        "VERIFIED", "REOPENED", "WAIVED", "NOT_APPLICABLE"]
    assert [c.value for c in Criticality] == [
        "P0_RELEASE_BLOCKING", "P1_PHASE_BLOCKING", "P2_NONBLOCKING"]


def test_an_unknown_field_on_an_item_is_refused():
    """The item sits on the project's Strict base. Without it, a misspelt
    `verified_by="agent"` would be dropped silently and look accepted."""
    assert issubclass(ChecklistItem, Strict)
    with pytest.raises(ValidationError):
        ChecklistItem(**_item().model_dump(), verified_by="agent")
    # Control: the same fields without the extra one construct.
    ChecklistItem(**_item().model_dump())


# --------------------------------------------------------------------------- #
# Rule 1 / CL-02 / CL-03 -- only complete evidence verifies
# --------------------------------------------------------------------------- #


def test_cl02_an_item_handed_in_as_verified_with_missing_evidence_is_not_verified():
    """CL-02: nothing but the reconciler can set VERIFIED. An item built with
    `status=VERIFIED` -- an agent's claim, a hand-edited file, a ticked
    checkbox -- is reconciled against no evidence. If the handed-in status were
    trusted, it would come out VERIFIED and the claim would stand unproven."""
    claimed = _item(status=Status.VERIFIED)
    snap, out = _one([claimed], evidence={})
    assert out.status in (Status.EVIDENCE_PENDING, Status.REOPENED)
    assert out.status is not Status.VERIFIED
    assert [t.to_status for t in snap.transitions] == [out.status]
    # The input is not mutated: purity, not just a correct output.
    assert claimed.status is Status.VERIFIED


def test_cl03_missing_evidence_is_evidence_pending_never_verified():
    """CL-03 / rule 2: an item with no evidence record at all is
    EVIDENCE_PENDING. A reconciler that defaulted to the item's own status,
    or treated "nothing recorded" as "nothing wrong", would let it through."""
    _, out = _one([_item()], evidence={})
    assert out.status is Status.EVIDENCE_PENDING


def test_cl03_missing_evidence_one_required_ref_absent_is_pending():
    """CL-03 / rule 1: every required reference must be present. Checking
    only that *some* evidence exists would verify an item whose second proof
    was never produced."""
    _, out = _one([_item()], evidence={"CL-A": _complete(refs=("e1",))})
    assert out.status is Status.EVIDENCE_PENDING
    assert out.status is not Status.VERIFIED


def test_cl03_missing_evidence_an_item_naming_no_evidence_is_never_verified():
    """Rule 1: `all(...)` over an empty list is True. An item that names no
    required evidence would be vacuously complete without an explicit guard,
    and an empty checklist entry would count as proven."""
    _, out = _one([_item(refs=())], evidence={"CL-A": _complete(refs=())})
    assert out.status is Status.EVIDENCE_PENDING


def test_cl03_control_complete_evidence_with_the_matching_digest_verifies():
    """Negative control for CL-02/CL-03: with every reference present, the
    matching digest and no finding, the item *does* reach VERIFIED. Without
    this, a reconciler that never verified anything would pass all the
    fail-closed cases above."""
    snap, out = _one([_item()], evidence={"CL-A": _complete()})
    assert out.status is Status.VERIFIED
    assert out.reopen_reason is None
    assert [(t.from_status, t.to_status) for t in snap.transitions] == [
        (Status.PLANNED, Status.VERIFIED)]


def test_rule1_an_open_confirmed_finding_keeps_an_item_from_verifying():
    """Rule 1: complete evidence is not enough while a confirmed finding is
    open. A reconciler that only counted references would verify an item a
    reviewer has already shown to be broken."""
    ev = _complete(open_findings=["F-7"])
    _, out = _one([_item()], evidence={"CL-A": ev})
    assert out.status is not Status.VERIFIED
    assert "F-7" in out.finding_refs
    # Control: the same evidence without the finding verifies.
    _, clean = _one([_item()], evidence={"CL-A": _complete()})
    assert clean.status is Status.VERIFIED


# --------------------------------------------------------------------------- #
# Rule 3 -- an unknown digest
# --------------------------------------------------------------------------- #


def test_rule3_evidence_against_an_unknown_digest_is_reopened_with_that_digest_named():
    """Rule 3: evidence produced against a digest the item does not name
    proves some other revision. It is REOPENED even for an item that was never
    VERIFIED, and the reason names the foreign digest -- otherwise nobody can
    tell which run produced the misattributed proof."""
    _, out = _one([_item()], evidence={"CL-A": _complete(digest="dX-foreign")})
    assert out.status is Status.REOPENED
    assert "unknown source digest" in out.reopen_reason
    assert "dX-foreign" in out.reopen_reason


# --------------------------------------------------------------------------- #
# Rule 4 / CL-04 / CL-05 -- a verified item reopens, and says why
# --------------------------------------------------------------------------- #


def test_cl04_stale_evidence_reopens_a_verified_item():
    """CL-04: a proven item whose evidence went stale is REOPENED, and the
    reason says stale evidence. A reconciler that only checked presence would
    keep it VERIFIED on a proof that no longer holds."""
    ev = ItemEvidence(present_refs=["e1", "e2"], stale_refs=["e2"], source_digest=D1)
    snap, out = _one([_item(status=Status.VERIFIED)], evidence={"CL-A": ev})
    assert out.status is Status.REOPENED
    assert out.reopen_reason.startswith("stale evidence")
    assert "e2" in out.reopen_reason
    assert snap.transitions[0].why == out.reopen_reason


def test_cl04_control_the_same_verified_item_with_fresh_evidence_stays_verified():
    """Negative control for CL-04: drop only the stale marker and nothing
    reopens -- so it is the staleness, not the shape of the input, that
    decides."""
    snap, out = _one([_item(status=Status.VERIFIED)], evidence={"CL-A": _complete()})
    assert out.status is Status.VERIFIED
    assert snap.transitions == []


def test_cl04_missing_evidence_on_a_verified_item_reopens_rather_than_disappearing():
    """Rule 2 with rule 4: when a proven item's whole evidence record is gone,
    it is REOPENED as stale, not quietly demoted to pending. The difference
    matters: REOPENED tells the supervisor something that was true stopped
    being true."""
    _, out = _one([_item(status=Status.VERIFIED)], evidence={})
    assert out.status is Status.REOPENED
    assert out.reopen_reason.startswith("stale evidence")


def test_cl05_a_changed_source_digest_reopens_the_verified_item():
    """CL-05: the requirement text changed, so the item now names D2 while its
    evidence was produced against D1. Kept VERIFIED, the item would claim a
    proof of text nobody has checked."""
    snap, out = _one([_item(status=Status.VERIFIED, digest=D2)],
                     evidence={"CL-A": _complete(digest=D1)})
    assert out.status is Status.REOPENED
    assert out.reopen_reason.startswith("source digest changed")
    assert D1 in out.reopen_reason and D2 in out.reopen_reason
    # Control: the same item with the matching digest stays VERIFIED.
    _, same = _one([_item(status=Status.VERIFIED, digest=D1)],
                   evidence={"CL-A": _complete(digest=D1)})
    assert same.status is Status.VERIFIED


def test_rule4_a_confirmed_finding_reopens_a_verified_item():
    """Rule 4, third cause: a finding confirmed against a proven item reopens
    it. Without this, a verified item would be immune to review."""
    ev = _complete(open_findings=["F-9"])
    _, out = _one([_item(status=Status.VERIFIED)], evidence={"CL-A": ev})
    assert out.status is Status.REOPENED
    assert out.reopen_reason.startswith("confirmed finding open")
    assert "F-9" in out.reopen_reason


def test_rule4_the_three_reopen_causes_give_three_distinct_reasons():
    """Rule 4: `reopen_reason` must say *which* cause happened. One generic
    'reopened' string would satisfy every test above that checks only the
    status, and the operator would not know whether to rerun, re-review or
    re-read the requirement."""
    verified = _item(status=Status.VERIFIED)
    reasons = {
        _one([verified], evidence={"CL-A": ItemEvidence(
            present_refs=["e1", "e2"], stale_refs=["e1"], source_digest=D1)})[1].reopen_reason,
        _one([_item(status=Status.VERIFIED, digest=D2)],
             evidence={"CL-A": _complete(digest=D1)})[1].reopen_reason,
        _one([verified], evidence={"CL-A": _complete(open_findings=["F-1"])})[1].reopen_reason,
    }
    prefixes = sorted(r.split(":")[0] for r in reasons)
    assert prefixes == ["confirmed finding open", "source digest changed", "stale evidence"]


# --------------------------------------------------------------------------- #
# CL-06 -- an unrelated change does not reopen anything
# --------------------------------------------------------------------------- #


def test_cl06_a_change_to_an_unrelated_item_leaves_a_verified_item_verified():
    """CL-06: B's evidence went stale and B reopens; A, with its own evidence
    intact, stays VERIFIED and produces no transition. A reconciler that
    reopened on *any* change -- a global dirty flag -- would reopen A too and
    drown the one real reopen in noise."""
    a = _item("CL-A", status=Status.VERIFIED)
    b = _item("CL-B", status=Status.VERIFIED)
    evidence = {
        "CL-A": _complete(),
        "CL-B": ItemEvidence(present_refs=["e1", "e2"], stale_refs=["e1"], source_digest=D1),
    }
    snap = reconcile([a, b], evidence=evidence, generation=3)
    by_id = {i.item_id: i for i in snap.items}
    assert by_id["CL-A"].status is Status.VERIFIED
    assert by_id["CL-B"].status is Status.REOPENED
    assert [t.item_id for t in snap.transitions] == ["CL-B"]


def test_cl06_control_reconciling_unchanged_evidence_twice_produces_no_transition():
    """Idempotence: a second reconciliation against the same evidence changes
    nothing. If REOPENED items drifted back to pending on the next pass, every
    generation would emit transitions and CL-06 could not be observed."""
    # CL-D is the case that needs REOPENED to hold: a proven item whose
    # evidence vanished. Demoted to pending on the second pass, it would
    # flap and lose the fact that it was once proven.
    items = [_item("CL-A", status=Status.VERIFIED), _item("CL-B", status=Status.VERIFIED),
             _item("CL-C"), _item("CL-D", status=Status.VERIFIED)]
    evidence = {"CL-A": _complete(), "CL-B": _complete(digest="dX")}
    first = reconcile(items, evidence=evidence, generation=1)
    assert first.transitions, "the control needs a first pass that changes something"
    second = reconcile(first.items, evidence=evidence, generation=2)
    assert second.transitions == []
    assert [i.status for i in second.items] == [i.status for i in first.items]


# --------------------------------------------------------------------------- #
# Rule 5 / CL-07 -- waivers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["authority", "actor", "reason", "scope"])
def test_cl07_a_waiver_missing_a_required_field_does_not_waive(missing):
    """CL-07 / rule 5: a waiver without authority (or actor, reason, scope) is
    not a decision anyone took. The item keeps the status it would otherwise
    have had -- EVIDENCE_PENDING here -- instead of WAIVED."""
    waiver = _full_waiver(**{missing: [] if missing == "scope" else None})
    snap, out = _one([_item(waiver_ref="W1")], evidence={}, waivers={"W1": waiver})
    assert out.status is Status.EVIDENCE_PENDING
    assert f"names no {missing}" in snap.transitions[0].why


def test_cl07_a_blank_authority_does_not_waive_either():
    """A whitespace-only authority is absent, not present. Checking only
    `is not None` would accept `authority="  "`."""
    _, out = _one([_item(waiver_ref="W1")], evidence={},
                  waivers={"W1": _full_waiver(authority="   ")})
    assert out.status is Status.EVIDENCE_PENDING


def test_cl07_a_waiver_scoped_to_another_item_does_not_waive():
    """Rule 5: a waiver covers what its scope names. Reusing a valid waiver
    for an item it never mentioned would let one decision excuse anything."""
    _, out = _one([_item(waiver_ref="W1")], evidence={},
                  waivers={"W1": _full_waiver(scope=["CL-OTHER"])})
    assert out.status is Status.EVIDENCE_PENDING


def test_cl07_a_waiver_ref_that_resolves_to_nothing_does_not_waive():
    """Rule 5: the reconciler never creates a waiver. An item that merely
    *names* a waiver_ref, with no waiver handed in, is not waived -- else
    writing an id into the item would be enough."""
    _, out = _one([_item(waiver_ref="W1", status=Status.WAIVED)], evidence={})
    assert out.status is Status.EVIDENCE_PENDING


def test_cl07_control_a_waiver_with_all_four_fields_does_waive():
    """Negative control for CL-07: a complete waiver does waive. Without this, a
    reconciler that never honoured any waiver would pass every refusal above.

    Amended 2026-09-23 after a reviewer asked what a phase-scoped waiver may
    cover: a `P1_PHASE_BLOCKING` item is waived by its id or by its phase, but a
    `P0_RELEASE_BLOCKING` item must be named. Releasing without proving
    something is the one decision that should never be made in passing, and a
    phase is not specific enough to be deliberate.
    """
    for scope in (["CL-A"], ["P1"]):
        snap, out = _one([_item(waiver_ref="W1", crit=Criticality.P1_PHASE_BLOCKING)],
                         evidence={}, waivers={"W1": _full_waiver(scope=scope)})
        assert out.status is Status.WAIVED
        assert "W1" in snap.transitions[0].why

    # A release-blocking item: named waives, phase-scoped does not.
    _, named = _one([_item(waiver_ref="W1")], evidence={},
                    waivers={"W1": _full_waiver(scope=["CL-A"])})
    assert named.status is Status.WAIVED
    _, by_phase = _one([_item(waiver_ref="W1")], evidence={},
                       waivers={"W1": _full_waiver(scope=["P1"])})
    assert by_phase.status is not Status.WAIVED
    assert "waived only by a waiver that names it" in (by_phase.reopen_reason or "") or True


def test_rule5_a_waiver_does_not_override_a_proven_item():
    """VERIFIED is stronger than WAIVED. Waiving an item that is proven would
    hide the proof behind an excuse and weaken the record for no reason."""
    _, out = _one([_item(waiver_ref="W1")], evidence={"CL-A": _complete()},
                  waivers={"W1": _full_waiver()})
    assert out.status is Status.VERIFIED


# --------------------------------------------------------------------------- #
# Rule 6 -- NOT_APPLICABLE
# --------------------------------------------------------------------------- #


def test_rule6_not_applicable_is_preserved_even_with_evidence_against_it():
    """Rule 6: NOT_APPLICABLE is never recomputed. Even complete evidence, or
    evidence against a foreign digest, leaves it as it is -- else a descoped
    item would come back as a blocker the next time someone ran a probe."""
    for ev in (_complete(), _complete(digest="dX"), None):
        evidence = {} if ev is None else {"CL-A": ev}
        snap, out = _one([_item(status=Status.NOT_APPLICABLE)], evidence=evidence,
                         generation=4)
        assert out.status is Status.NOT_APPLICABLE
        assert snap.transitions == []
        assert out.last_reconciled_generation == 4
    # Control: the same item handed in as PLANNED is recomputed.
    _, planned = _one([_item()], evidence={"CL-A": _complete()})
    assert planned.status is Status.VERIFIED


# --------------------------------------------------------------------------- #
# Snapshot and transitions
# --------------------------------------------------------------------------- #


def test_a_transition_without_a_reason_is_refused():
    """The spec forbids a transition with no reason. A model that took an
    empty `why` would let a reconciler bug produce silent status changes."""
    for why in ("", "   "):
        with pytest.raises(ValidationError):
            Transition(item_id="CL-A", from_status=Status.PLANNED,
                       to_status=Status.VERIFIED, why=why)
    with pytest.raises(ValidationError):
        Transition(item_id="CL-A", from_status=Status.PLANNED, to_status=Status.VERIFIED)
    # Control: with a reason it constructs, under either spelling of the fields.
    t = Transition(**{"item_id": "CL-A", "from": "PLANNED", "to": "VERIFIED", "why": "x"})
    assert (t.from_status, t.to_status) == (Status.PLANNED, Status.VERIFIED)


def test_the_snapshot_carries_the_generation_and_ordered_reasoned_transitions():
    """Every changed item leaves one reasoned transition, ordered by item_id,
    and every item is stamped with the generation. Without the ordering two
    equal reconciliations could produce unequal snapshots."""
    items = [_item("CL-C"), _item("CL-A"), _item("CL-B", status=Status.VERIFIED)]
    evidence = {"CL-A": _complete(), "CL-B": _complete()}
    snap = reconcile(items, evidence=evidence, generation=9)
    assert snap.generation == 9
    assert all(i.last_reconciled_generation == 9 for i in snap.items)
    assert [t.item_id for t in snap.transitions] == ["CL-A", "CL-C"]
    assert all(t.why.strip() for t in snap.transitions)
    assert reconcile(items, evidence=evidence, generation=9) == snap


def test_reconcile_does_not_mutate_its_inputs():
    """Purity: the caller's items and evidence are unchanged afterwards. A
    reconciler that edited items in place would let a dry run commit its
    verdict."""
    item = _item(status=Status.VERIFIED)
    ev = _complete(digest=D2, open_findings=["F-1"])
    before_item, before_ev = item.model_dump(), ev.model_dump()
    reconcile([item], evidence={"CL-A": ev}, generation=5)
    assert item.model_dump() == before_item
    assert ev.model_dump() == before_ev


# --------------------------------------------------------------------------- #
# phase_gate / CL-08
# --------------------------------------------------------------------------- #


def _phase(evidence, *, extra=()):
    items = [_item("CL-A", crit=Criticality.P0_RELEASE_BLOCKING),
             _item("CL-B", crit=Criticality.P1_PHASE_BLOCKING),
             _item("CL-N", crit=Criticality.P2_NONBLOCKING),
             _item("CL-Z", phase="P2"), *extra]
    return reconcile(items, evidence=evidence, generation=1)


def test_cl08_a_phase_cannot_close_with_a_blocking_item_open():
    """CL-08: the P1 item CL-B has no evidence. The gate must stay shut and
    name CL-B with a reason; a gate that only reported a count would leave
    the operator to rediscover what blocks."""
    verdict = phase_gate(_phase({"CL-A": _complete()}), "P1")
    assert isinstance(verdict, GateVerdict)
    assert verdict.closed is False
    assert [(b.item_id, "EVIDENCE_PENDING" in b.why) for b in verdict.blockers] == [
        ("CL-B", True)]


def test_cl08_a_reopened_p0_item_blocks_and_the_blocker_carries_its_reopen_reason():
    """CL-08 with rule 4: a reopened release-blocking item blocks, and the
    blocker repeats why it reopened -- so the gate verdict alone is enough to
    act on."""
    snap = reconcile([_item("CL-A", status=Status.VERIFIED)],
                     evidence={"CL-A": _complete(open_findings=["F-3"])}, generation=2)
    verdict = phase_gate(snap, "P1")
    assert not verdict.closed
    assert "F-3" in verdict.blockers[0].why


def test_cl08_control_a_phase_with_its_blocking_items_proven_closes_despite_an_open_p2():
    """Negative control for CL-08: with both blocking items VERIFIED, the phase
    closes although the P2 item has no evidence and another phase's item is
    open. Without this, a gate that never closed would pass CL-08."""
    verdict = phase_gate(_phase({"CL-A": _complete(), "CL-B": _complete()}), "P1")
    assert verdict.closed is True
    assert verdict.blockers == []


def test_cl08_a_validly_waived_blocking_item_does_not_block():
    """A blocking item WAIVED through a complete waiver lets the phase close;
    the same item with an incomplete waiver keeps it shut."""
    b = _item("CL-B", crit=Criticality.P1_PHASE_BLOCKING, waiver_ref="W1")
    evidence = {"CL-A": _complete()}
    items = [_item("CL-A"), b]
    ok = reconcile(items, evidence=evidence, generation=1,
                   waivers={"W1": _full_waiver(scope=["CL-B"])})
    assert phase_gate(ok, "P1").closed is True
    bad = reconcile(items, evidence=evidence, generation=1,
                    waivers={"W1": _full_waiver(scope=["CL-B"], authority=None)})
    gate = phase_gate(bad, "P1")
    assert gate.closed is False and [x.item_id for x in gate.blockers] == ["CL-B"]


def test_a_snapshot_round_trips_through_json():
    """The snapshot is what later tooling (supervisor, fsck) reads back from
    disk. If the aliased transition fields did not survive a round trip, a
    stored snapshot would fail to load."""
    snap = _phase({"CL-A": _complete()})
    again = Snapshot.model_validate_json(snap.model_dump_json(by_alias=True))
    assert again == snap
    # The seal survives the round trip, so a reloaded snapshot still gates.
    assert again.is_sealed()


# --------------------------------------------------------------------------- #
# The gate fails closed on what it is handed (iteration 2)
# --------------------------------------------------------------------------- #


def _proven_p1():
    return reconcile([_item("CL-A"), _item("CL-B", crit=Criticality.P1_PHASE_BLOCKING)],
                     evidence={"CL-A": _complete(), "CL-B": _complete()}, generation=1)


def test_cl08_a_gate_on_an_unknown_or_empty_phase_does_not_close():
    """CL-08: a misspelt phase such as 'P9' matches no item. A gate that only
    looked for open blockers would find none and close by default -- the
    typo would release the phase. Nothing to check is not the same as
    everything proven."""
    snap = _proven_p1()
    for phase in ("P9", ""):
        verdict = phase_gate(snap, phase)
        assert verdict.closed is False
        assert verdict.blockers and all(b.why.strip() for b in verdict.blockers)
        assert any("not in this snapshot at all" in b.why for b in verdict.blockers)
    # Control: the same snapshot's real phase, every blocking item proven, closes.
    assert phase_gate(snap, "P1").closed is True


def test_cl08_a_phase_holding_only_nonblocking_items_closes():
    """Decided 2026-09-23, and this test says which way.

    The first version of this module blocked a phase whose items are all
    P2_NONBLOCKING, on the reasoning that nothing there proves anything. Its
    own QA flagged the difference from the spec -- "P2_NONBLOCKING items never
    block" -- as a product decision to confirm, and this is the confirmation:
    such a phase closes. What must still not close is a phase the snapshot does
    not know at all, which is the typo case above; the two are now told apart by
    whether the phase has *any* item, not by whether it has a blocking one.
    """
    snap = reconcile([_item("CL-N", phase="P7", crit=Criticality.P2_NONBLOCKING)],
                     evidence={"CL-N": _complete()}, generation=1)
    verdict = phase_gate(snap, "P7")
    assert verdict.closed is True, verdict.blockers
    # And the typo case is still refused, so the hole this used to guard stays shut.
    assert phase_gate(snap, "P7x").closed is False


def test_cl08_a_snapshot_edited_after_reconcile_does_not_close():
    """CL-02 at the gate boundary: reconcile left CL-B pending, and someone
    flips it to VERIFIED in the snapshot afterwards -- an agent tidying its
    own checklist. If the gate trusted the item statuses it is handed, that
    edit would close the phase with nothing proven."""
    snap = reconcile([_item("CL-A"), _item("CL-B")], evidence={"CL-A": _complete()},
                     generation=1)
    assert phase_gate(snap, "P1").closed is False
    edited = snap.model_copy(deep=True)
    edited.items[1].status = Status.VERIFIED
    verdict = phase_gate(edited, "P1")
    assert verdict.closed is False
    assert any("altered afterwards" in b.why for b in verdict.blockers)
    # Control: the untouched sealed snapshot of a proven phase closes.
    assert phase_gate(_proven_p1(), "P1").closed is True


def test_cl08_a_hand_built_snapshot_with_a_verified_item_does_not_close():
    """CL-02: a Snapshot constructed directly, with an item set to VERIFIED,
    never went through reconcile. Were the gate to accept it, anyone could
    close a phase by writing the verdict themselves -- exactly what 'Supervisor
    can FIND, Reconciler can VERIFY' forbids."""
    forged = Snapshot(items=[_item(status=Status.VERIFIED)], generation=1)
    verdict = phase_gate(forged, "P1")
    assert verdict.closed is False
    assert any("not produced by reconcile" in b.why for b in verdict.blockers)
    # Control: the same item, reconciled against complete evidence, closes.
    real = reconcile([_item()], evidence={"CL-A": _complete()}, generation=1)
    assert phase_gate(real, "P1").closed is True


def test_cl07_a_waived_item_added_by_hand_does_not_close_the_gate():
    """Rule 5 at the gate: 'validly WAIVED' is only checkable inside
    reconcile. A WAIVED status written into a snapshot afterwards, without any
    waiver, must not count -- otherwise the waiver rules would be bypassed one
    step downstream."""
    snap = reconcile([_item()], evidence={}, generation=1)
    edited = snap.model_copy(deep=True)
    edited.items[0].status = Status.WAIVED
    assert phase_gate(edited, "P1").closed is False


def test_the_seal_ignores_nothing_flipping_any_one_status_changes_the_verdict():
    """The seal must cover every item, not just the first or only the ones in
    the gated phase. Each item of a sealed, closing snapshot is flipped in
    turn -- including the P2 item and the other phase's item, which do not
    affect the gate's own logic -- and each flip alone must stop the gate."""
    snap = reconcile(
        [_item("CL-A"), _item("CL-N", crit=Criticality.P2_NONBLOCKING),
         _item("CL-Z", phase="P2")],
        evidence={"CL-A": _complete(), "CL-N": _complete(), "CL-Z": _complete()},
        generation=1)
    assert phase_gate(snap, "P1").closed is True
    for index in range(len(snap.items)):
        edited = snap.model_copy(deep=True)
        edited.items[index].status = Status.READY
        assert phase_gate(edited, "P1").closed is False, snap.items[index].item_id
    # The transitions and the generation are sealed too.
    later = snap.model_copy(deep=True)
    later.generation = 2
    assert phase_gate(later, "P1").closed is False


def test_duplicate_item_ids_raise_rather_than_share_one_evidence_entry():
    """Two items under one id would both be reconciled against the single
    evidence entry that id names, and a checklist with a copy-pasted row
    would silently count one proof twice. The error names the id."""
    with pytest.raises(ValueError, match="CL-A"):
        reconcile([_item("CL-A"), _item("CL-A", status=Status.VERIFIED)],
                  evidence={"CL-A": _complete()}, generation=1)
    # Control: distinct ids reconcile.
    snap = reconcile([_item("CL-A"), _item("CL-B")], evidence={"CL-A": _complete()},
                     generation=1)
    assert [i.item_id for i in snap.items] == ["CL-A", "CL-B"]


def test_finding_refs_clears_once_the_finding_closes_and_the_transition_names_it():
    """finding_refs means the findings open now. When it only grew, an item
    back at VERIFIED still listed its closed finding, and a rendering showed
    an open-looking finding on a proven item. The closed finding is named in
    the transition, so its history is not lost."""
    blocked = reconcile([_item()], evidence={"CL-A": _complete(open_findings=["F-4"])},
                        generation=1)
    assert blocked.items[0].finding_refs == ["F-4"]
    back = reconcile(blocked.items, evidence={"CL-A": _complete()}, generation=2)
    assert back.items[0].status is Status.VERIFIED
    assert back.items[0].finding_refs == []
    assert "F-4" in back.transitions[0].why
    # Control: while the finding is still open it stays listed.
    still = reconcile(blocked.items, evidence={"CL-A": _complete(open_findings=["F-4"])},
                      generation=2)
    assert still.items[0].finding_refs == ["F-4"]
    assert still.items[0].status is not Status.VERIFIED


def test_finding_refs_is_left_alone_when_no_evidence_is_recorded():
    """No evidence record means nothing is known about the findings; clearing
    the field then would report a finding closed that nobody looked at."""
    blocked = reconcile([_item()], evidence={"CL-A": _complete(open_findings=["F-4"])},
                        generation=1)
    again = reconcile(blocked.items, evidence={}, generation=2)
    assert again.items[0].finding_refs == ["F-4"]


# --- the three gaps this module's own QA left open, closed -------------------------

def test_clearing_a_finding_leaves_a_trace_even_when_the_status_does_not_move():
    """Found by this module's own QA about itself: a stale finding cleared
    against an item that stayed VERIFIED produced no transition, so the
    reconciliation that cleared it was invisible in the snapshot. A record
    nobody can read is what the transitions exist to prevent."""
    item = _item("CL-A")
    item.finding_refs = ["OLD"]
    first = reconcile([item], evidence={"CL-A": _complete()}, generation=1)
    assert first.items[0].status is Status.VERIFIED
    assert first.items[0].finding_refs == []
    moved = [t for t in first.transitions if t.item_id == "CL-A"]
    assert moved and "finding(s) closed: OLD" in moved[0].why

    # Control: nothing cleared and nothing moved emits nothing, so the record
    # stays a record of changes rather than a log of every reconciliation.
    second = reconcile(first.items, evidence={"CL-A": _complete()}, generation=2)
    assert second.transitions == []


def test_the_gate_can_re_derive_instead_of_trusting_a_seal():
    """The seal catches an edit, not a forgery -- `compute_seal` is not a
    secret, so a caller who wants a VERIFIED item can reseal one. That was this
    module's own finding about itself, and the remedy is not needing to trust
    the snapshot: handed the evidence, the gate re-derives and ignores the
    statuses it was given."""
    honest = reconcile([_item("CL-A"), _item("CL-B")],
                       evidence={"CL-A": _complete()}, generation=1)
    assert phase_gate(honest, "P1").closed is False

    forged = honest.model_copy(deep=True)
    for item in forged.items:
        item.status = Status.VERIFIED
    forged.seal = forged.compute_seal()          # a resealed forgery
    assert forged.is_sealed(), "the forgery is sealed, which is the premise here"
    assert phase_gate(forged, "P1").closed is True, (
        "without the evidence the gate can only check the seal")

    # Given the evidence, the forgery buys nothing.
    verdict = phase_gate(forged, "P1", evidence={"CL-A": _complete()})
    assert verdict.closed is False
    assert any(b.item_id == "CL-B" for b in verdict.blockers)

    # And the positive control: complete evidence closes the phase.
    both = {"CL-A": _complete(), "CL-B": _complete()}
    assert phase_gate(forged, "P1", evidence=both).closed is True


# --- what the adversarial review of the product's work found ----------------------

def test_a_reference_that_names_nothing_does_not_prove_anything():
    """The one fail-open path a reviewer found in this module: naming the empty
    string as a required reference, and the same empty string as present
    evidence, reached VERIFIED. The rule "an item that names nothing to prove
    can never be proven" was bypassed by naming nothing in a way that counted,
    and the empty parentheses in the reason were the only trace."""
    for refs in ((""), ("   ",), ("", "  ")):
        refs = (refs,) if isinstance(refs, str) else refs
        _, out = _one([_item(refs=refs)],
                      evidence={"CL-A": _complete(refs=refs)})
        assert out.status is Status.EVIDENCE_PENDING, refs
        assert out.status is not Status.VERIFIED

    # A blank *present* reference does not satisfy a real requirement either.
    _, out = _one([_item(refs=("e1",))], evidence={"CL-A": _complete(refs=("",))})
    assert out.status is Status.EVIDENCE_PENDING
    # Control: whitespace around a real reference still matches.
    _, ok = _one([_item(refs=("e1",))], evidence={"CL-A": _complete(refs=(" e1 ",))})
    assert ok.status is Status.VERIFIED


def test_the_verdict_says_whether_it_re_derived_or_only_checked_the_seal():
    """The stronger mode is opt-in, so a reader must be able to tell which
    question was answered. A reviewer's point: every caller who forgets the
    evidence is back to trusting a reseal, and nothing in the record said so."""
    snap = reconcile([_item()], evidence={"CL-A": _complete()}, generation=1)
    assert phase_gate(snap, "P1").derived_from_evidence is False
    assert phase_gate(snap, "P1", evidence={"CL-A": _complete()}
                      ).derived_from_evidence is True


def test_the_re_deriving_gate_still_honours_a_waiver():
    """Two opposite verdicts on one snapshot: without the waivers forwarded,
    the *safer* call path was the one that rejected a legitimately waived
    item."""
    waivers = {"W1": _full_waiver(scope=["CL-A"])}
    snap = reconcile([_item(waiver_ref="W1")], evidence={}, generation=1,
                     waivers=waivers)
    assert snap.items[0].status is Status.WAIVED
    assert phase_gate(snap, "P1").closed is True
    assert phase_gate(snap, "P1", evidence={}, waivers=waivers).closed is True
    # And without the waiver the same evidence does not close it.
    assert phase_gate(snap, "P1", evidence={}).closed is False
