"""Proof classes and the checks that keep one from being chosen freely.

A proof class is an escape hatch unless it is justified against the plan's own
words: "this is only a schema, so no consumer is needed" restores every one of
the twelve P2 overclaims in better vocabulary. So each rule here has a case and,
beside it, a negative control that changes only the condition under test and
shows the verdict flipping. Most controls are the *good* side -- a verbatim
quote is accepted, the mapped plan item is accepted, a register with one valid
declaration per capability is clean. Without them, a checker that refused
everything would pass every refusal in this file.

Revision 2 adds the escape hatches the integration review of run
20260925t1540z-pproof proved open: word-fragment quotes ("base" out of
"baseline"), a declaration choosing its own plan item inside `fsck`, one-word
quotes, unvalidated instances, the inventory's `id` key and leaving SUBSTITUTE.

Plain pytest: the real register and plan are never read, nothing is written.
"""
from __future__ import annotations

import sys
import unicodedata

import pytest
from pydantic import ValidationError

from hoh.proofclass import (
    EVIDENCE_REMOVAL_VOCABULARY,
    EVIDENCE_REMOVING_CLASSES,
    LOWERS_THE_BAR,
    MAX_PLAN_ITEM_TEXT_CHARS,
    MIN_QUOTE_WORDS,
    RAISES_THE_BAR,
    WHAT_WOULD_SETTLE_IT,
    WORD_JOINERS,
    ProofClass,
    ProofDeclaration,
    assignment_is_a_relaxation,
    fsck,
    justification_problems,
    normalise,
)

DIGEST = "plan-digest-1"
SCHEMA_ITEM = {"id": "P2-01", "text": "Define the  Database Schema for run records."}
RESUME_ITEM = {"id": "P2-02", "text": "Resume the run unattended after a crash."}
FRAGMENT_ITEM = {"id": "P2-03", "text": ("Migrate the database to the new baseline; we "
                                         "remodel the store and pay the contractor.")}
ITEMS = [SCHEMA_ITEM, RESUME_ITEM, FRAGMENT_ITEM]


def _decl(**overrides) -> ProofDeclaration:
    fields = dict(
        requirement_id="R1", plan_item_id="P2-01", proof_class=ProofClass.ARTEFACT,
        quoted_plan_text="the database schema for run records",
        why="the plan item promises a schema, not a runtime consumer",
        declared_at="2026-09-25T16:45:00Z", declared_against_plan_digest=DIGEST,
    )
    fields.update(overrides)
    return ProofDeclaration(**fields)


def _problems(item=SCHEMA_ITEM, digest=DIGEST, **overrides) -> list[str]:
    return justification_problems(_decl(**overrides), plan_item=item, plan_digest=digest)


def _fsck(declarations, **overrides) -> list[str]:
    args = dict(plan_items=ITEMS, plan_digest=DIGEST, capability_ids={"R1"},
                requirement_plan_items={"R1": "P2-01"})
    args.update(overrides)
    return fsck(declarations, **args)


# --------------------------------------------------------------------------- #
# the classes and what settles them
# --------------------------------------------------------------------------- #


def test_the_classes_are_exactly_the_six_specified_in_order():
    """A member added, dropped or reordered silently changes what a serialised
    register means; the spec fixes the list, so it is pinned here."""
    assert [c.value for c in ProofClass] == [
        "ARTEFACT", "INTERNAL_RUNTIME", "CONTROL_PLANE", "BOUNDARY_CONTRACT",
        "COMPOSITE", "SUBSTITUTE"]
    assert all(c.value == c.name for c in ProofClass)


def test_every_class_has_exactly_one_settle_text_and_the_mapping_is_read_only():
    """A class without a settle text is a class nobody can prove; a mutable
    mapping lets a caller rewrite what settles a class at runtime."""
    assert set(WHAT_WOULD_SETTLE_IT) == set(ProofClass)
    assert all(WHAT_WOULD_SETTLE_IT[c].strip() for c in ProofClass)
    with pytest.raises(TypeError):
        WHAT_WOULD_SETTLE_IT[ProofClass.ARTEFACT] = "anything"  # type: ignore[index]


def test_internal_runtime_says_an_import_statement_is_not_sufficient():
    """The finding this increment exists to encode: an import under `if False:`
    satisfies an import check. Asserted as a string so the sentence cannot be
    softened without this test failing."""
    text = WHAT_WOULD_SETTLE_IT[ProofClass.INTERNAL_RUNTIME]
    assert "An import statement is not sufficient" in text
    assert "if False:" in text
    assert "removing the wiring makes a behavioural probe fail" in text
    # control: the sentence is specific to INTERNAL_RUNTIME, not boilerplate
    assert "import statement" not in WHAT_WOULD_SETTLE_IT[ProofClass.ARTEFACT].lower()


def test_composite_says_independently_tested_components_are_not_the_capability():
    """Without this sentence a loop is 'proven' by the unit tests of its parts --
    the unattended-resumption chain being the example the spec names."""
    text = WHAT_WOULD_SETTLE_IT[ProofClass.COMPOSITE].lower()
    assert "a set of independently tested components is not the composite capability" in text
    assert "unattended-resumption chain" in text
    # control: another class does not carry the composite doctrine
    assert "independently tested" not in WHAT_WOULD_SETTLE_IT[ProofClass.ARTEFACT].lower()


def test_substitute_boundary_artefact_and_control_plane_texts_state_their_doctrine():
    """Each settle text is the evidence rule a reviewer checks against; one that
    drifts from the spec settles a different claim."""
    assert "PARTIAL, always, everywhere" in WHAT_WOULD_SETTLE_IT[ProofClass.SUBSTITUTE]
    boundary = WHAT_WOULD_SETTLE_IT[ProofClass.BOUNDARY_CONTRACT]
    for phrase in ("internal importer is not required", "manufactured importer does not count",
                   "contract validity", "authority and ownership", "deterministic conflict",
                   "fail-closed behaviour on unknown or invalid external state"):
        assert phrase in boundary, phrase
    assert "No runtime consumer is required" in WHAT_WOULD_SETTLE_IT[ProofClass.ARTEFACT]
    assert "tool's own path is the evidence" in WHAT_WOULD_SETTLE_IT[ProofClass.CONTROL_PLANE]


def test_raises_and_lowers_the_bar_partition_the_classes_exactly():
    """A class in neither set escapes relaxation checks; a class in both makes
    every move look like a relaxation and none."""
    assert RAISES_THE_BAR == {ProofClass.SUBSTITUTE, ProofClass.INTERNAL_RUNTIME,
                              ProofClass.COMPOSITE}
    assert LOWERS_THE_BAR == {ProofClass.ARTEFACT, ProofClass.CONTROL_PLANE,
                              ProofClass.BOUNDARY_CONTRACT}
    assert isinstance(RAISES_THE_BAR, frozenset) and isinstance(LOWERS_THE_BAR, frozenset)
    assert RAISES_THE_BAR.isdisjoint(LOWERS_THE_BAR)
    assert RAISES_THE_BAR | LOWERS_THE_BAR == set(ProofClass)
    assert EVIDENCE_REMOVING_CLASSES == {ProofClass.ARTEFACT, ProofClass.BOUNDARY_CONTRACT}


# --------------------------------------------------------------------------- #
# relaxation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("previous,current", [
    (ProofClass.INTERNAL_RUNTIME, ProofClass.ARTEFACT),
    (ProofClass.SUBSTITUTE, ProofClass.BOUNDARY_CONTRACT),
    (ProofClass.COMPOSITE, ProofClass.CONTROL_PLANE),
])
def test_a_move_from_raising_to_lowering_is_a_relaxation_and_its_reverse_is_not(previous, current):
    """The laundering move -- reclassifying a runtime claim as an artefact -- must
    be visible; the control is the reverse move, which tightens the bar."""
    assert assignment_is_a_relaxation(previous, current) is True
    assert assignment_is_a_relaxation(current, previous) is False


def test_leaving_substitute_is_a_relaxation_and_entering_it_is_not():
    """R2-6: a substitute is capped at PARTIAL, so SUBSTITUTE -> COMPOSITE lifts
    the cap even though both raise the bar. The control is the reverse."""
    assert assignment_is_a_relaxation(ProofClass.SUBSTITUTE, ProofClass.COMPOSITE) is True
    assert assignment_is_a_relaxation(ProofClass.SUBSTITUTE, ProofClass.INTERNAL_RUNTIME) is True
    assert assignment_is_a_relaxation(ProofClass.COMPOSITE, ProofClass.SUBSTITUTE) is False


def test_a_no_op_and_moves_within_one_side_are_not_relaxations():
    """A checker that flagged every edit would make the real relaxations
    invisible in the noise."""
    for c in ProofClass:
        assert assignment_is_a_relaxation(c, c) is False
    assert assignment_is_a_relaxation(ProofClass.ARTEFACT, ProofClass.CONTROL_PLANE) is False
    assert assignment_is_a_relaxation(ProofClass.ARTEFACT, ProofClass.INTERNAL_RUNTIME) is False


def test_the_relaxation_truth_table_has_exactly_the_eleven_documented_moves():
    """The docstring lists the relaxing moves; this pins the function to it."""
    relaxing = {(a, b) for a in ProofClass for b in ProofClass
                if assignment_is_a_relaxation(a, b)}
    raising_to_lowering = {(a, b) for a in RAISES_THE_BAR for b in LOWERS_THE_BAR}
    leaving_substitute = {(ProofClass.SUBSTITUTE, ProofClass.INTERNAL_RUNTIME),
                          (ProofClass.SUBSTITUTE, ProofClass.COMPOSITE)}
    assert relaxing == raising_to_lowering | leaving_substitute
    assert len(relaxing) == 11
    doc = assignment_is_a_relaxation.__doc__
    for line in ("INTERNAL_RUNTIME -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT",
                 "COMPOSITE        -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT",
                 "SUBSTITUTE       -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT",
                 "INTERNAL_RUNTIME, COMPOSITE"):
        assert line in doc


# --------------------------------------------------------------------------- #
# the declaration model
# --------------------------------------------------------------------------- #


def test_a_complete_declaration_constructs_and_parses_an_iso_timestamp():
    """Control for every construction refusal below: the good declaration
    must construct, or every refusal test is vacuous."""
    d = _decl()
    assert d.declared_at.year == 2026 and d.proof_class is ProofClass.ARTEFACT


@pytest.mark.parametrize("field,value", [
    ("why", ""), ("why", "   "), ("quoted_plan_text", ""),
    ("requirement_id", " "), ("plan_item_id", ""), ("declared_against_plan_digest", ""),
])
def test_construction_refuses_an_empty_why_quote_id_or_digest(field, value):
    """An empty `why` is a class chosen without a reason; an empty quote is
    justified by nothing; a blank id or digest names nothing."""
    with pytest.raises(ValidationError):
        _decl(**{field: value})


def test_construction_refuses_a_quote_longer_than_any_plan_item_can_be():
    """A 'quote' longer than a plan item's text pastes a section of the plan
    and quotes no single item. Control: a quote at the bound constructs."""
    with pytest.raises(ValidationError, match="characters"):
        _decl(quoted_plan_text="word " * 5000)
    at_bound = ("abcd " * (MAX_PLAN_ITEM_TEXT_CHARS // 5))[:MAX_PLAN_ITEM_TEXT_CHARS].strip()
    assert len(_decl(quoted_plan_text=at_bound).quoted_plan_text) <= MAX_PLAN_ITEM_TEXT_CHARS
    assert MAX_PLAN_ITEM_TEXT_CHARS <= 2000


def test_the_declaration_is_frozen_and_refuses_unknown_fields():
    """A frozen register entry cannot be edited after it was checked; an
    unknown field would carry a claim no rule reads."""
    d = _decl()
    with pytest.raises(ValidationError):
        d.why = "changed after the check"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _decl(status="PROVEN")
    assert d.why == "the plan item promises a schema, not a runtime consumer"


# --------------------------------------------------------------------------- #
# rule 1: verbatim, whole words, in order
# --------------------------------------------------------------------------- #


def test_a_verbatim_quote_of_the_plan_item_is_accepted():
    """Control for every refusal of rule 1: the plan's own words pass."""
    assert _problems() == []


def test_a_verbatim_quote_with_one_word_the_plan_lacks_is_refused():
    """A justification that quotes words the plan does not contain is prose."""
    problems = _problems(quoted_plan_text="the database schema for all run records")
    assert len(problems) == 1 and "verbatim" in problems[0]


def test_a_verbatim_quote_differing_only_in_case_and_whitespace_is_accepted():
    """Line wrapping and capitalisation must not decide whether a quote is the
    plan's words; the item text itself has a double space."""
    assert _problems(quoted_plan_text="DEFINE THE   database\n SCHEMA") == []
    assert normalise("  A\tB\n\nC  ") == "a b c"


def test_a_paraphrase_is_not_verbatim_and_is_refused():
    """Same meaning, different words: prose, not the plan."""
    assert _problems(quoted_plan_text="the schema of the database")


def test_the_same_words_in_a_different_order_are_not_verbatim():
    """Verbatim means order too: a word-set check would accept this."""
    assert _problems(quoted_plan_text="schema database the define")
    assert _problems(quoted_plan_text="define the database schema") == []


@pytest.mark.parametrize("quote,cls", [
    ("to the new base", ProofClass.CONTROL_PLANE),      # base out of baseline
    ("migrate the data", ProofClass.CONTROL_PLANE),     # data out of database
    ("we model the store", ProofClass.CONTROL_PLANE),   # not in the item at all
    ("model the store", ProofClass.ARTEFACT),           # model out of remodel
    ("pay the contract", ProofClass.BOUNDARY_CONTRACT),  # contract out of contractor
])
def test_a_word_fragment_quote_is_refused(quote, cls):
    """R2-1: a quote cut out of the middle of a plan word ("base" from
    "baseline") met the vocabulary burden in the first run."""
    problems = _problems(item=FRAGMENT_ITEM, plan_item_id="P2-03", proof_class=cls,
                         quoted_plan_text=quote)
    assert any("whole words" in p for p in problems), problems


def test_a_fragment_control_whole_words_are_accepted():
    """Controls for the fragment refusals: the same regions quoted as whole
    words pass, and 'the database schema' passes for ARTEFACT."""
    for quote in ("to the new baseline", "migrate the database", "we remodel the store",
                  "pay the contractor"):
        assert _problems(item=FRAGMENT_ITEM, plan_item_id="P2-03",
                         proof_class=ProofClass.CONTROL_PLANE, quoted_plan_text=quote) == []
    assert _problems(quoted_plan_text="the database schema") == []


def test_a_fragment_does_not_count_as_a_vocabulary_word():
    """'database' and 'baseline' contain 'base' only as characters; the burden
    is checked on the quote's words. Control: 'schema' counts."""
    for quote in ("migrate the database", "to the new baseline"):
        problems = _problems(item=FRAGMENT_ITEM, plan_item_id="P2-03", quoted_plan_text=quote)
        assert len(problems) == 1 and "removes an evidence requirement" in problems[0]
    assert _problems(quoted_plan_text="the database schema") == []


# --------------------------------------------------------------------------- #
# R2-3: a quote long enough to mean something
# --------------------------------------------------------------------------- #


def test_a_short_quote_of_two_words_is_refused_at_construction():
    """R2-3: 'e' and 'migrate the' satisfy 'verbatim' and say nothing."""
    assert MIN_QUOTE_WORDS == 3
    for quote in ("migrate the", "e", "the - end"):
        with pytest.raises(ValidationError, match="word"):
            _decl(quoted_plan_text=quote, proof_class=ProofClass.CONTROL_PLANE)


def test_a_short_quote_control_three_words_are_accepted():
    """Control: the minimum itself passes, so the bound is not off by one."""
    assert _problems(item=FRAGMENT_ITEM, plan_item_id="P2-03",
                     proof_class=ProofClass.CONTROL_PLANE,
                     quoted_plan_text="we remodel the") == []


def test_a_short_quote_on_an_unvalidated_instance_is_still_refused():
    """Rule 5 is re-checked in justification_problems, so model_construct
    cannot skip the word minimum."""
    good = _decl(quoted_plan_text="the database schema")
    short = ProofDeclaration.model_construct(**{**good.model_dump(), "quoted_plan_text":
                                                "database schema"})
    problems = justification_problems(short, plan_item=SCHEMA_ITEM, plan_digest=DIGEST)
    assert any("at least 3" in p for p in problems)
    assert justification_problems(good, plan_item=SCHEMA_ITEM, plan_digest=DIGEST) == []


# --------------------------------------------------------------------------- #
# rules 2 and 3: the plan item and the plan digest
# --------------------------------------------------------------------------- #


def test_a_declaration_against_a_different_plan_digest_is_refused():
    """A declaration made against another revision of the plan is not a
    declaration about this one. Control: the matching digest passes."""
    problems = _problems(declared_against_plan_digest="plan-digest-0")
    assert len(problems) == 1 and "digest" in problems[0]
    assert _problems(declared_against_plan_digest=DIGEST) == []


def test_a_declaration_naming_another_plan_item_than_the_one_handed_in_is_refused():
    """Rule 2: the item handed in is the requirement's; the declaration may
    not name a different one. Control: the same id passes."""
    problems = _problems(plan_item_id="P2-99")
    assert len(problems) == 1 and "P2-99" in problems[0] and "P2-01" in problems[0]
    assert _problems(plan_item_id="P2-01") == []


# --------------------------------------------------------------------------- #
# rule 4: the vocabulary burden of the evidence-removing classes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", [ProofClass.ARTEFACT, ProofClass.BOUNDARY_CONTRACT])
def test_an_evidence_removing_class_without_a_vocabulary_word_is_refused(cls):
    """ARTEFACT and BOUNDARY_CONTRACT remove an evidence requirement; without
    the burden, any runtime item could be declared an artefact. The message
    names every word that would have satisfied it."""
    problems = _problems(item=RESUME_ITEM, plan_item_id="P2-02", proof_class=cls,
                         quoted_plan_text="resume the run unattended")
    assert len(problems) == 1
    assert cls.value in problems[0]
    assert all(word in problems[0] for word in EVIDENCE_REMOVAL_VOCABULARY)


@pytest.mark.parametrize("cls", [ProofClass.ARTEFACT, ProofClass.BOUNDARY_CONTRACT])
def test_an_evidence_removing_class_with_a_vocabulary_word_is_accepted(cls):
    """Control: one vocabulary word in a verbatim quote meets the burden."""
    assert _problems(proof_class=cls, quoted_plan_text="the database schema") == []


@pytest.mark.parametrize("cls", [ProofClass.INTERNAL_RUNTIME, ProofClass.COMPOSITE,
                                 ProofClass.CONTROL_PLANE, ProofClass.SUBSTITUTE])
def test_the_vocabulary_burden_does_not_apply_to_other_classes(cls):
    """Control: the same quote is accepted for every class that does not
    remove an evidence requirement, so the burden is class-specific."""
    assert _problems(item=RESUME_ITEM, plan_item_id="P2-02", proof_class=cls,
                     quoted_plan_text="resume the run unattended") == []


def test_the_vocabulary_is_the_stated_seven_words():
    """The list is reviewable only if it is pinned."""
    assert EVIDENCE_REMOVAL_VOCABULARY == {
        "schema", "contract", "interface", "identifier", "projection", "base", "model"}


# --------------------------------------------------------------------------- #
# R2-5: the inventory's id key
# --------------------------------------------------------------------------- #


def test_a_plan_item_keyed_by_the_id_key_is_found():
    """The real inventory uses `id`; the first run read only `item_id` and
    would have failed every real declaration."""
    assert _problems(item={"id": "P2-01", "text": SCHEMA_ITEM["text"]}) == []


def test_a_plan_item_keyed_item_id_is_found_as_the_id_key_alias():
    """`item_id` stays accepted as an alias."""
    assert _problems(item={"item_id": "P2-01", "text": SCHEMA_ITEM["text"]}) == []


def test_a_plan_item_with_no_id_key_is_refused_by_name():
    """An item with neither key cannot be the item the requirement maps to;
    the message names it by its text. Also: conflicting keys are refused."""
    problems = _problems(item={"text": SCHEMA_ITEM["text"]})
    assert problems and any(SCHEMA_ITEM["text"] in p for p in problems)
    assert _problems(item={"id": "P2-01", "item_id": "P2-02", "text": SCHEMA_ITEM["text"]})


def test_a_malformed_plan_item_is_a_problem_not_an_exception():
    """justification_problems must never raise on bad input."""
    assert _problems(item="not a mapping")
    assert _problems(item={"id": "P2-01"})


# --------------------------------------------------------------------------- #
# fsck
# --------------------------------------------------------------------------- #


def test_fsck_accepts_a_clean_register():
    """Control for every fsck refusal: one valid declaration per capability,
    justified against its mapped item, is clean."""
    assert _fsck([_decl()]) == []


def test_fsck_reports_a_capability_with_a_missing_declaration():
    """Unknown proof class is a failure, never a default."""
    problems = _fsck([])
    assert len(problems) == 1 and "R1" in problems[0] and "no proof declaration" in problems[0]


def test_fsck_missing_declaration_control_the_declaration_clears_it():
    """Control: the same register with R1 declared is clean."""
    assert _fsck([_decl()]) == []


def test_fsck_reports_a_duplicate_declaration_and_one_clears_it():
    """Two declarations for one requirement leave its class undecided."""
    problems = _fsck([_decl(), _decl()])
    assert any("declared 2 times" in p for p in problems)
    assert _fsck([_decl()]) == []


def test_fsck_reports_a_declaration_for_a_non_capability_and_the_capability_clears_it():
    """A declaration for a requirement that is not a capability is register
    noise that could later be promoted silently."""
    problems = _fsck([_decl()], capability_ids=set())
    assert problems == ["'R1': declared, but not a capability requirement"]
    assert _fsck([_decl()], capability_ids={"R1"}) == []


def test_fsck_reports_a_declaration_whose_own_plan_item_is_not_in_the_plan():
    """The spec's original rule: a declared plan item id that no plan item has."""
    problems = _fsck([_decl(plan_item_id="P2-77")])
    assert any("P2-77" in p and "not in the plan" in p for p in problems)


def test_fsck_surfaces_justification_problems_prefixed_with_the_requirement():
    """fsck is the register check; a justification failure it hides is a
    failure nobody sees."""
    problems = _fsck([_decl()], plan_digest="plan-digest-0")
    assert len(problems) == 1 and problems[0].startswith("'R1': ") and "digest" in problems[0]


def test_fsck_mapping_refuses_a_declaration_that_borrows_another_items_words():
    """R2-2: requirement R2 belongs to the resume item but declares ARTEFACT
    against the schema item. In the first run fsck looked the item up from the
    declaration itself, so rule 2 compared an id with itself and passed."""
    borrowed = _decl(requirement_id="R2")  # plan_item_id P2-01, quote about a schema
    problems = _fsck([_decl(), borrowed], capability_ids={"R1", "R2"},
                     requirement_plan_items={"R1": "P2-01", "R2": "P2-02"})
    assert problems and all(p.startswith("'R2': ") for p in problems)
    assert any("maps to plan item 'P2-02'" in p for p in problems)


def test_fsck_mapping_control_the_mapped_item_is_accepted():
    """Control: R2 declared against its own mapped item is clean."""
    own = _decl(requirement_id="R2", plan_item_id="P2-02",
                proof_class=ProofClass.INTERNAL_RUNTIME,
                quoted_plan_text="resume the run unattended")
    assert _fsck([_decl(), own], capability_ids={"R1", "R2"},
                 requirement_plan_items={"R1": "P2-01", "R2": "P2-02"}) == []


def test_fsck_mapping_refuses_a_requirement_absent_from_the_mapping():
    """No mapped item means nothing can justify the class; it must not fall
    back to the declaration's own choice."""
    problems = _fsck([_decl()], requirement_plan_items={})
    assert problems == ["'R1': requirement is not in requirement_plan_items, so no plan "
                        "item can justify it"]


def test_fsck_mapping_refuses_a_mapped_item_that_is_not_in_the_plan():
    """A mapping to an item the plan lacks is as good as no mapping."""
    problems = _fsck([_decl()], requirement_plan_items={"R1": "P2-77"})
    assert any("P2-77" in p for p in problems)


def test_fsck_model_construct_declaration_with_empty_why_is_refused():
    """R2-4: an instance built without validation skipped every construction
    rule in the first run and passed fsck clean."""
    good = _decl()
    bad = ProofDeclaration.model_construct(**{**good.model_dump(), "why": ""})
    problems = _fsck([bad])
    assert any("does not re-validate" in p and "why" in p for p in problems)


def test_fsck_model_construct_declaration_with_a_missing_field_is_refused():
    """R2-4: a missing field is the same hole as an empty one."""
    fields = _decl().model_dump()
    del fields["declared_against_plan_digest"]
    problems = _fsck([ProofDeclaration.model_construct(**fields)])
    assert any("declared_against_plan_digest" in p for p in problems)


def test_fsck_model_construct_control_a_validated_declaration_passes():
    """Control: re-validation must not refuse a good declaration, including
    one produced by model_construct from a valid dump."""
    good = _decl()
    assert _fsck([good]) == []
    assert _fsck([ProofDeclaration.model_construct(**good.model_dump())]) == []


def test_fsck_reports_a_plan_item_without_an_id_key_and_accepts_the_alias():
    """R2-5 at the register level: an id-less plan item is named; an item keyed
    `item_id` is found."""
    orphan = {"text": "an orphan plan item without identifier"}
    problems = _fsck([_decl()], plan_items=[SCHEMA_ITEM, orphan])
    assert any("orphan plan item" in p for p in problems)
    alias = {"item_id": "P2-01", "text": SCHEMA_ITEM["text"]}
    assert _fsck([_decl()], plan_items=[alias]) == []


def test_fsck_never_raises_on_malformed_arguments():
    """A string where a collection belongs (capability_ids="R1" iterates as
    characters) or a non-declaration must be reported, not crash the check."""
    problems = fsck(["junk"], plan_items="x", plan_digest=DIGEST, capability_ids="R1",
                    requirement_plan_items=None)  # type: ignore[arg-type]
    assert any("capability_ids" in p for p in problems)
    assert any("requirement_plan_items" in p for p in problems)
    assert any("plan_items" in p for p in problems)
    assert any("not a ProofDeclaration" in p for p in problems)


def test_fsck_output_is_independent_of_input_order():
    """Deterministic: the same register in a different order gives the same
    report, so a diff of two reports means a change in the register."""
    a = _decl(requirement_id="R1")
    b = _decl(requirement_id="R3")
    c = _decl(requirement_id="R2", plan_item_id="P2-99")
    kw = dict(capability_ids={"R1", "R2", "R4"},
              requirement_plan_items={"R1": "P2-01", "R2": "P2-01"})
    assert _fsck([a, b, c], **kw) == _fsck([c, b, a], **kw)
    assert _fsck([a, b, c], **kw)


# --------------------------------------------------------------------------- #
# iteration 2: a hyphen is part of a word (E-gap-b5d32ce8)
# --------------------------------------------------------------------------- #

HYPHEN_ITEM = {"id": "P2-04",
               "text": 'Keep "the run records" for the base-line interface stable.'}


def _hyphen_problems(quote, cls=ProofClass.CONTROL_PLANE, item=HYPHEN_ITEM, item_id="P2-04"):
    return _problems(item=item, plan_item_id=item_id, proof_class=cls, quoted_plan_text=quote)


@pytest.mark.parametrize("quote", [
    "for the base",                            # ends inside base-line
    "line interface stable",                   # starts inside base-line
    'keep "the run records" for the base',     # a long quote ending inside it
])
def test_a_hyphen_fragment_of_a_hyphenated_word_is_refused(quote):
    """A hyphen counted as a word boundary in iteration 1, so 'for the base' was
    a 'whole-word' quote of '... for the base-line interface' and met the
    'base' vocabulary burden. CONTROL_PLANE isolates rule 1 from rule 4."""
    problems = _hyphen_problems(quote)
    assert len(problems) == 1 and "whole words" in problems[0], problems


def test_a_hyphen_control_the_whole_hyphenated_word_is_accepted():
    """Control: the same regions quoted up to the end of the hyphenated word
    pass, so the fix did not make hyphenated text unquotable."""
    assert _hyphen_problems("for the base-line interface", cls=ProofClass.ARTEFACT) == []
    assert _hyphen_problems('the run records" for the base-line') == []
    assert _hyphen_problems("base-line interface stable") == []


@pytest.mark.parametrize("quote,without_the_stray_character", [
    ("-line interface stable", "base-line interface stable"),
    ('the run records" for the base-', 'the run records" for the'),
])
def test_a_hyphen_or_punctuation_led_or_ended_quote_cannot_splice_into_a_word(
        quote, without_the_stray_character):
    """Iteration 1 applied the boundary lookarounds only when the quote began
    or ended with a word character, so a stray leading or trailing hyphen let a
    quote start or stop inside a word. Control: the same quote starting or
    ending on the word boundary is accepted."""
    problems = _hyphen_problems(quote)
    assert len(problems) == 1 and "whole words" in problems[0], problems
    assert _hyphen_problems(without_the_stray_character) == []


def test_a_hyphen_control_punctuation_on_a_real_boundary_is_accepted():
    """Control: leading or trailing punctuation is fine where the plan has it
    on a word boundary -- the rule refuses splicing, not punctuation."""
    assert _hyphen_problems('"the run records"') == []
    assert _hyphen_problems('keep "the run records"') == []


def test_a_hyphenated_base_line_is_not_the_vocabulary_word_base():
    """With a hyphen word-internal, 'base-line' is one word; counting it as
    'base' would let ARTEFACT be declared on the strength of a fragment.
    Control: 'interface' in the same item still counts."""
    problems = _hyphen_problems('the run records" for the base-line', cls=ProofClass.ARTEFACT)
    assert len(problems) == 1 and "removes an evidence requirement" in problems[0]
    assert _hyphen_problems("for the base-line interface", cls=ProofClass.ARTEFACT) == []


def test_a_hyphenated_data_model_is_not_the_vocabulary_word_model():
    """The same for 'model': 'data-model' is one word. Control: the item with a
    space instead of the hyphen -- the only change -- meets the burden."""
    hyphenated = {"id": "P2-06", "text": "Publish the data-model export format."}
    spaced = {"id": "P2-06", "text": "Publish the data model export format."}
    problems = _hyphen_problems("publish the data-model", cls=ProofClass.ARTEFACT,
                                item=hyphenated, item_id="P2-06")
    assert len(problems) == 1 and "removes an evidence requirement" in problems[0]
    assert _hyphen_problems("publish the data model", cls=ProofClass.ARTEFACT,
                            item=spaced, item_id="P2-06") == []


def test_fsck_refuses_a_hyphen_fragment_through_the_mapped_item():
    """fsck judges against the mapped item, so the hyphen rule must hold there
    too. Control: the whole hyphenated quote is clean."""
    kw = dict(plan_items=[HYPHEN_ITEM], requirement_plan_items={"R1": "P2-04"})
    fragment = _decl(plan_item_id="P2-04", quoted_plan_text="for the base")
    whole = _decl(plan_item_id="P2-04", quoted_plan_text="for the base-line interface")
    assert _fsck([fragment], **kw)
    assert _fsck([whole], **kw) == []


# --------------------------------------------------------------------------- #
# iteration 2: a quote no longer than the item it is handed (E-gap-38c76065)
# --------------------------------------------------------------------------- #

SHORT_ITEM = {"id": "P2-05", "text": "Define the  schema."}


def test_a_quote_longer_than_its_item_is_refused_and_says_so():
    """The construction bound is a static ceiling (MAX_PLAN_ITEM_TEXT_CHARS);
    'longer than the plan item's own text could be' is only decidable against
    the item, and iteration 1 checked it only implicitly through rule 1."""
    problems = _problems(item=SHORT_ITEM, plan_item_id="P2-05",
                         quoted_plan_text="define the schema for run records")
    longer = [p for p in problems if "longer" in p]
    assert len(longer) == 1
    assert "'P2-05'" in longer[0] and "33" in longer[0] and "18" in longer[0]


def test_the_whole_item_quoted_is_not_longer_than_its_item_and_is_accepted():
    """Control: a quote exactly as long as the (normalised) item text -- the
    whole item quoted -- passes, so the bound is not off by one."""
    quote = "DEFINE the schema."
    assert len(normalise(quote)) == len(normalise(SHORT_ITEM["text"]))
    assert _problems(item=SHORT_ITEM, plan_item_id="P2-05", quoted_plan_text=quote) == []


# --------------------------------------------------------------------------- #
# iteration 3: every dash-like character joins words (E-gap-73de3b9d)
# --------------------------------------------------------------------------- #

HYPHEN_2010, EN_DASH, SOFT_HYPHEN, MINUS = "\u2010", "\u2013", "\u00ad", "\u2212"


def _dashed_item(dash):
    return {"id": "P2-07",
            "text": f"Keep the run records for the base{dash}line interface stable."}


def _dash_problems(quote, dash, cls=ProofClass.CONTROL_PLANE):
    return _problems(item=_dashed_item(dash), plan_item_id="P2-07", proof_class=cls,
                     quoted_plan_text=quote)


@pytest.mark.parametrize("dash", [HYPHEN_2010, EN_DASH], ids=["U+2010", "U+2013"])
def test_a_unicode_dash_splice_is_refused(dash):
    """The ASCII-only rule of iteration 2 let 'for the base' out of
    'base\u2010line' count as whole words -- the same fragment escape through
    another character. Control: with a space where the dash was (the only
    change), 'for the base' is whole words and accepted."""
    problems = _dash_problems("for the base", dash)
    assert len(problems) == 1 and "whole words" in problems[0], problems
    assert _dash_problems("for the base", " ") == []


def test_every_unicode_dash_in_word_joiners_refuses_the_splice():
    """One missed dash is one open escape; every joiner is tried, not a sample.
    Control: the whole dashed compound is accepted for every joiner."""
    for dash in sorted(WORD_JOINERS):
        tag = f"U+{ord(dash):04X}"
        assert _dash_problems("for the base", dash), tag
        assert _dash_problems(f"for the base{dash}line interface", dash) == [], tag


@pytest.mark.parametrize("dash", [HYPHEN_2010, EN_DASH], ids=["U+2010", "U+2013"])
def test_a_unicode_dash_compound_is_not_the_word_base_for_artefact(dash):
    """With the dash word-internal for the tokeniser too, 'base\u2010line' is one
    word; splitting it would meet the 'base' burden with a fragment. Control:
    'interface' in the same item still counts, and the compound itself is a
    whole word for a class without the burden."""
    compound = f"the run records for the base{dash}line"
    problems = _dash_problems(compound, dash, cls=ProofClass.ARTEFACT)
    assert len(problems) == 1 and "removes an evidence requirement" in problems[0], problems
    assert _dash_problems(f"for the base{dash}line interface", dash,
                          cls=ProofClass.ARTEFACT) == []
    assert _dash_problems(compound, dash) == []


def test_a_spaced_unicode_dash_still_separates_words():
    """Joining words must not swallow a dash used as punctuation between
    spaces. Control: the same en dash unspaced joins 'base' to 'define', so
    'keep the base' is then a fragment and refused."""
    spaced = {"id": "P2-08", "text": f"Keep the base {EN_DASH} define the schema, then the model."}
    unspaced = {"id": "P2-08", "text": f"Keep the base{EN_DASH}define the schema, then the model."}
    kw = dict(plan_item_id="P2-08", quoted_plan_text="keep the base")
    assert _problems(item=spaced, **kw) == []
    assert _problems(item=spaced, plan_item_id="P2-08", quoted_plan_text="then the model") == []
    assert _problems(item=unspaced, **kw)


def test_unicode_dash_word_joiners_cover_every_pd_character_soft_hyphen_and_minus():
    """A literal list of dashes goes stale with the next Unicode version; the
    constant must hold every Pd character of the running interpreter plus the
    soft hyphen and the minus sign. Control: ordinary separators -- space,
    comma, slash, full stop -- are not joiners, so words still separate."""
    pd = {chr(n) for n in range(sys.maxunicode + 1) if unicodedata.category(chr(n)) == "Pd"}
    assert len(pd) >= 20
    assert WORD_JOINERS == pd | {SOFT_HYPHEN, MINUS}
    assert {"-", HYPHEN_2010, EN_DASH, "\uff0d"} <= WORD_JOINERS
    assert isinstance(WORD_JOINERS, frozenset)
    assert not {" ", ",", "/", ".", "_"} & WORD_JOINERS
