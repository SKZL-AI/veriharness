"""The PM boundary: two systems, one authority per state class.

P2-29, P2-30. The failure this guards against is not a crash. It is two
mutable sources of project truth -- an external plan that says a work package
is done, a verdict here that says it is not, and nobody able to say which one
the release rests on. The plan's rule is one sentence, and it is the last case
in this file: external DONE cannot override VeriHarness NOT VERIFIED.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.projectcontract import (
    OWNERSHIP,
    AmendmentImpact,
    Authority,
    ProjectContract,
    ProjectContractAmendment,
    UnknownState,
    WorkPackage,
    authority_for,
    conflicts,
    impact_of,
)


def _contract(**kw) -> ProjectContract:
    fields = dict(
        project_contract_id="p1", source_system="some-pm", source_revision="r1",
        objective="ship the thing",
        milestones={"m1": "2026-10-01"},
        work_packages=(WorkPackage(work_package_id="w1", title="first"),
                       WorkPackage(work_package_id="w2", title="second",
                                   depends_on=("w1",), milestone="m1")),
    )
    fields.update(kw)
    return ProjectContract(**fields)


# --- the ownership table (P2-30) --------------------------------------------------

def test_every_state_class_has_exactly_one_authority():
    """A state with two authorities is a state with none, and the table is the
    ADR rather than a paraphrase of it."""
    assert authority_for("run_acceptance") is Authority.VERIHARNESS
    assert authority_for("milestone_deadline") is Authority.EXTERNAL_PM
    assert authority_for("process_mechanics") is Authority.RUNTIME_ADAPTER
    assert all(isinstance(a, Authority) for a in OWNERSHIP.values())


def test_the_things_with_no_authority_are_named_rather_than_omitted():
    """"This has no authority" is the useful half of the table: a UI, a
    checklist rendering and a recommendation are projections, and a reader who
    cannot find them would assume they were forgotten."""
    for projection in ("ui_projection", "checklist_projection",
                       "system_one_recommendation", "external_finding"):
        assert authority_for(projection) is Authority.NONE


def test_an_undeclared_state_class_is_refused_rather_than_guessed():
    with pytest.raises(UnknownState) as caught:
        authority_for("who_decides_the_colour_of_the_button")
    assert "no authority is declared" in str(caught.value)


def test_verification_and_acceptance_are_never_the_external_systems():
    """The one direction that matters: an external plan may own dates and
    budgets, never whether something passed."""
    for state in ("run_acceptance", "evidence_validity", "integration_closure",
                  "program_dag", "ready_frontier", "session_authority"):
        assert authority_for(state) is Authority.VERIHARNESS


# --- the contract itself (P2-29) --------------------------------------------------

def test_a_contract_with_a_dangling_dependency_is_refused():
    """An imported plan whose dependencies do not resolve would produce a DAG
    nobody can execute, and the place to find that is the import."""
    with pytest.raises(ValidationError) as caught:
        _contract(work_packages=(WorkPackage(work_package_id="w1", title="first",
                                             depends_on=("nowhere",)),))
    assert "does not contain" in str(caught.value)


def test_a_contract_naming_a_milestone_it_does_not_define_is_refused():
    with pytest.raises(ValidationError):
        _contract(work_packages=(WorkPackage(work_package_id="w1", title="first",
                                             milestone="m404"),))


def test_two_work_packages_may_not_share_an_id():
    with pytest.raises(ValidationError) as caught:
        _contract(work_packages=(WorkPackage(work_package_id="w1", title="a"),
                                 WorkPackage(work_package_id="w1", title="b")))
    assert "two different things" in str(caught.value)


def test_the_digest_covers_the_content_and_not_the_moment_of_import():
    a = _contract()
    b = a.model_copy(update={"imported_at": "2020-01-01T00:00:00Z"})
    assert a.digest() == b.digest()
    assert a.digest() != _contract(objective="ship something else").digest()


def test_an_amendment_goes_forward_only():
    """A version that has been imported is immutable; an amendment proposes the
    next one and may not rewrite the last."""
    with pytest.raises(ValidationError):
        ProjectContractAmendment(amendment_id="a1", project_contract_id="p1",
                                 from_version=3, to_version=2, source_revision="r2",
                                 reason="the PM changed the scope")


# --- what an amendment would disturb ----------------------------------------------

def test_an_amendment_is_described_rather_than_applied():
    """The plan lists five questions an amendment is evaluated for. This
    answers them; the programme decides what to do about the answers."""
    before = _contract()
    after = _contract(work_packages=(
        WorkPackage(work_package_id="w1", title="first, rewritten"),
        WorkPackage(work_package_id="w3", title="new work")))
    impact = impact_of(before, after,
                       evidence_for={"w1": before.digest()},
                       active_work={"w2"})
    assert "+w3" in impact.scope_changes and "-w2" in impact.scope_changes
    assert "~w1" in impact.scope_changes
    assert impact.evidence_invalidated == ("w1",)
    assert impact.active_work_affected == ("w2",)
    assert impact.disturbs_anything


def test_an_unchanged_package_does_not_invalidate_its_evidence():
    """The negative control. Without it, every amendment would look like it
    invalidated everything, and the signal would be worth nothing."""
    before = _contract()
    after = _contract(objective="ship the thing, by October")
    impact = impact_of(before, after,
                       evidence_for={"w1": before.digest(), "w2": before.digest()})
    assert impact.evidence_invalidated == ()
    assert impact.scope_changes == ()
    assert not impact.disturbs_anything


def test_a_scope_change_beyond_the_autonomy_contract_is_named():
    before = _contract()
    after = _contract(work_packages=(*before.work_packages,
                                     WorkPackage(work_package_id="w9", title="more")))
    assert impact_of(before, after, autonomy_allows_scope_change=False).beyond_autonomy
    assert not impact_of(before, after, autonomy_allows_scope_change=True).beyond_autonomy


def test_a_change_to_released_work_is_named():
    before = _contract()
    after = _contract(work_packages=(WorkPackage(work_package_id="w1", title="rewritten"),))
    assert impact_of(before, after, released={"w1"}).release_affected


def test_two_different_contracts_are_not_two_versions_of_one():
    with pytest.raises(ValueError, match="two different contracts"):
        impact_of(_contract(), _contract(project_contract_id="other"))


def test_an_impact_that_disturbs_nothing_says_so():
    assert not AmendmentImpact().disturbs_anything


# --- the sentence the whole boundary exists for -----------------------------------

def test_external_done_does_not_override_not_verified():
    """The plan's rule, measured. A project manager marking a package done is a
    statement about a plan; a verdict here is a statement about evidence, and
    the second does not become false because the first was written."""
    contract = _contract(work_packages=(
        WorkPackage(work_package_id="w1", title="first", external_status="DONE"),
        WorkPackage(work_package_id="w2", title="second", external_status="IN_PROGRESS")))
    found = conflicts(contract, internal={"w1": "NOT_VERIFIED", "w2": "NOT_VERIFIED"})
    assert [c.work_package_id for c in found] == ["w1"]
    assert found[0].authoritative == "VERIHARNESS"
    assert "the verdict here stands" in str(found[0])


def test_agreement_is_not_reported_as_a_conflict():
    contract = _contract(work_packages=(
        WorkPackage(work_package_id="w1", title="first", external_status="DONE"),))
    assert conflicts(contract, internal={"w1": "VERIFIED"}) == []
    assert conflicts(contract, internal={"w1": "ACCEPTED"}) == []


def test_a_package_this_product_has_never_heard_of_counts_as_not_verified():
    """Absence is not agreement: an external DONE for something with no verdict
    here is exactly the case the rule is about."""
    contract = _contract(work_packages=(
        WorkPackage(work_package_id="w1", title="first", external_status="complete"),))
    found = conflicts(contract, internal={})
    assert len(found) == 1 and found[0].internal_status == "NOT_VERIFIED"


def test_an_imported_contract_cannot_be_edited_in_place():
    """Immutable after import, enforced for field assignment: a changed external
    plan is a new version, never an edit of the one already imported. The contents
    of `milestones` are a plain dict and stay mutable -- freezing them would change
    a field type, which this does not do."""
    contract = _contract()
    with pytest.raises(ValidationError):
        contract.objective = "something else"
    with pytest.raises(ValidationError):
        contract.work_packages[0].title = "renamed"
    assert contract.objective == "ship the thing"
    assert contract.work_packages[0].title == "first"
