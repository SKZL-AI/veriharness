"""H1: evidence state E_t -- the second state that crosses loops (paper §3.3).

The central question of these tests: does a later loop really inherit more
than the code? The four failures the paper names -- open requirements
overlooked, refuted paths repeated, unresolved defects forgotten, validated
behaviour regressed -- each have to have a test here.
"""

from __future__ import annotations

from hoh.contracts import CheckVerdict, Outcome, Role, RoleResult
from hoh.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus, normalize


def item(item_id: str, status: EvidenceStatus, it: int = 1, claim: str = "c") -> EvidenceItem:
    return EvidenceItem(
        item_id=item_id,
        status=status,
        claim=claim,
        first_seen_iteration=it,
        last_seen_iteration=it,
    )


def qa_result(verdicts: list[CheckVerdict], *, iteration: int = 1, gaps=None) -> RoleResult:
    return RoleResult(
        run_id="r1",
        iteration=iteration,
        attempt=1,
        role=Role.QA,
        candidate_id="c1",
        plan_digest="p",
        spec_digest="s",
        verdicts=verdicts,
        open_gaps=gaps or [],
    )


# --- The views the planner needs ------------------------------------------ #


def test_verified_becomes_a_preservation_requirement():
    b = EvidenceBundle(
        run_id="r1",
        items=[item("E-1", EvidenceStatus.VERIFIED), item("E-2", EvidenceStatus.GAP)],
    )
    assert [i.item_id for i in b.to_preserve()] == ["E-1"]


def test_regressions_come_before_gaps():
    """The paper's planning policy: blockers and regressions first."""
    b = EvidenceBundle(
        run_id="r1",
        items=[
            item("E-gap", EvidenceStatus.GAP),
            item("E-reg", EvidenceStatus.REGRESSION),
        ],
    )
    assert [i.item_id for i in b.to_repair()] == ["E-reg", "E-gap"]


def test_older_gaps_first_at_equal_status():
    b = EvidenceBundle(
        run_id="r1",
        items=[
            item("E-new", EvidenceStatus.GAP, it=3),
            item("E-old", EvidenceStatus.GAP, it=1),
        ],
    )
    assert [i.item_id for i in b.to_repair()] == ["E-old", "E-new"]


def test_insufficient_is_not_a_success():
    b = EvidenceBundle(run_id="r1", items=[item("E-1", EvidenceStatus.INSUFFICIENT)])
    assert b.to_preserve() == []
    assert [i.item_id for i in b.insufficient()] == ["E-1"]
    assert b.has_open_work()


# --- The four failures from paper §3.3 ------------------------------------ #


def test_an_open_requirement_is_not_lost():
    """Failure 1: unmet requirements overlooked."""
    e0 = EvidenceBundle(run_id="r1", items=[item("E-open", EvidenceStatus.GAP, it=1)])
    e1 = e0.carry_forward([item("E-other", EvidenceStatus.VERIFIED, it=2)], iteration=2)
    assert "E-open" in {i.item_id for i in e1.items}
    assert e1.has_open_work()


def test_an_unresolved_defect_is_not_forgotten():
    """Failure 3: unresolved failures forgotten."""
    e0 = EvidenceBundle(run_id="r1", items=[item("E-bug", EvidenceStatus.GAP, it=1)])
    e2 = e0.carry_forward([], iteration=2).carry_forward([], iteration=3)
    bug = next(i for i in e2.items if i.item_id == "E-bug")
    assert bug.status is EvidenceStatus.GAP
    assert bug.first_seen_iteration == 1, "the origin stays visible"


def test_a_regression_is_detected_and_promoted():
    """Failure 4: validated behaviour regressed -- the core of A04."""
    e0 = EvidenceBundle(run_id="r1", items=[item("E-1", EvidenceStatus.VERIFIED, it=1)])
    e1 = e0.carry_forward([item("E-1", EvidenceStatus.GAP, it=2)], iteration=2)
    got = next(i for i in e1.items if i.item_id == "E-1")
    assert got.status is EvidenceStatus.REGRESSION
    assert got.first_seen_iteration == 1
    assert got.last_seen_iteration == 2


def test_insufficient_after_verified_is_a_regression_too():
    e0 = EvidenceBundle(run_id="r1", items=[item("E-1", EvidenceStatus.VERIFIED, it=1)])
    e1 = e0.carry_forward([item("E-1", EvidenceStatus.INSUFFICIENT, it=2)], iteration=2)
    assert next(i for i in e1.items).status is EvidenceStatus.REGRESSION


def test_a_repair_clears_the_gap_again():
    e0 = EvidenceBundle(run_id="r1", items=[item("E-1", EvidenceStatus.GAP, it=1)])
    e1 = e0.carry_forward([item("E-1", EvidenceStatus.VERIFIED, it=2)], iteration=2)
    assert next(i for i in e1.items).status is EvidenceStatus.VERIFIED
    assert not e1.has_open_work()


def test_carry_forward_never_deletes():
    e = EvidenceBundle(run_id="r1", items=[item(f"E-{n}", EvidenceStatus.GAP) for n in range(5)])
    e2 = e.carry_forward([], iteration=2)
    assert len(e2.items) == 5


def test_carry_forward_does_not_mutate_the_original():
    e0 = EvidenceBundle(run_id="r1", items=[item("E-1", EvidenceStatus.VERIFIED, it=1)])
    e0.carry_forward([item("E-1", EvidenceStatus.GAP, it=2)], iteration=2)
    assert e0.items[0].status is EvidenceStatus.VERIFIED, "E_{t-1} stays untouched"


# --- Deterministic normalization (paper A.2) ------------------------------ #


def test_normalization_maps_the_verdicts():
    result = qa_result(
        [
            CheckVerdict(check_id="K1", outcome=Outcome.PASS, receipt_id="rc1"),
            CheckVerdict(check_id="K2", outcome=Outcome.FAIL),
            CheckVerdict(check_id="K3", outcome=Outcome.INCONCLUSIVE),
        ]
    )
    e = normalize(result, prior=EvidenceBundle(run_id="r1"))
    by_id = {i.item_id: i for i in e.items}
    assert by_id["E-K1"].status is EvidenceStatus.VERIFIED
    assert by_id["E-K1"].receipt_ids == ["rc1"]
    assert by_id["E-K2"].status is EvidenceStatus.GAP
    assert by_id["E-K3"].status is EvidenceStatus.INSUFFICIENT


def test_open_gaps_from_qa_are_not_lost():
    result = qa_result([], gaps=["audio never checked", "startup time unclear"])
    e = normalize(result, prior=EvidenceBundle(run_id="r1"))
    assert len(e.insufficient()) == 2


def test_normalization_across_two_loops_detects_a_regression():
    e1 = normalize(
        qa_result([CheckVerdict(check_id="K1", outcome=Outcome.PASS, receipt_id="rc1")]),
        prior=EvidenceBundle(run_id="r1"),
    )
    e2 = normalize(
        qa_result([CheckVerdict(check_id="K1", outcome=Outcome.FAIL)], iteration=2),
        prior=e1,
    )
    assert next(i for i in e2.items if i.item_id == "E-K1").status is EvidenceStatus.REGRESSION


# --- Progressive Disclosure ------------------------------------------------ #


def test_the_packet_is_empty_in_iteration_one():
    assert "no evidence" in EvidenceBundle(run_id="r1").packet()


def test_the_packet_is_grouped_by_category():
    b = EvidenceBundle(
        run_id="r1",
        items=[
            item("E-ok", EvidenceStatus.VERIFIED, claim="login works"),
            item("E-bug", EvidenceStatus.GAP, claim="logout crashes"),
            item("E-unclear", EvidenceStatus.INSUFFICIENT, claim="performance unchecked"),
        ],
    )
    text = b.packet()
    assert "To preserve" in text and "login works" in text
    assert "To repair" in text and "logout crashes" in text
    assert "Insufficiently evidenced" in text and "performance unchecked" in text


def test_the_packet_is_bounded():
    """Progressive disclosure instead of a flood of context: the index is capped."""
    b = EvidenceBundle(
        run_id="r1",
        items=[item(f"E-{n:03d}", EvidenceStatus.GAP) for n in range(100)],
    )
    text = b.packet(max_items=5)
    assert "more, see evidence.json" in text
    assert text.count("- E-") <= 8
