"""H1: the contracts. The handoff explicitly requires tests with *invalid*
role outputs -- those are what keep the defence against invented successes
standing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.contracts import (
    AcceptanceCheck,
    Budgets,
    Candidate,
    CheckVerdict,
    DevelopmentPlan,
    Outcome,
    Receipt,
    RoleResult,
    Role,
    RunState,
    Stage,
    Usage,
)


def make_candidate(**kw) -> Candidate:
    base = dict(
        candidate_id="c1",
        repo_path="/tmp/repo",
        commit="a" * 40,
        tree_clean=True,
        tree_digest="deadbeef",
    )
    return Candidate(**{**base, **kw})


def make_check(check_id: str = "K1") -> AcceptanceCheck:
    return AcceptanceCheck(check_id=check_id, description="does something", command="true")


def make_plan(**kw) -> DevelopmentPlan:
    base = dict(
        run_id="r1",
        iteration=1,
        base_candidate_id="c0",
        spec_digest="spec00",
        objective="one observable increment",
        targets=["module/a.py"],
        acceptance_checks=[make_check()],
    )
    return DevelopmentPlan(**{**base, **kw})


# --- Candidate binding ----------------------------------------------------- #


def test_the_binding_distinguishes_a_dirty_tree():
    clean_ = make_candidate(tree_clean=True)
    dirty_ = make_candidate(tree_clean=False)
    assert clean_.binding() != dirty_.binding()


def test_the_binding_distinguishes_content_at_the_same_commit():
    """The same commit with a different working tree is a different object
    under test."""
    a = make_candidate(tree_digest="aaa")
    b = make_candidate(tree_digest="bbb")
    assert a.commit == b.commit
    assert a.binding() != b.binding()


# --- Invalid role outputs -------------------------------------------------- #


def test_an_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        DevelopmentPlan(
            run_id="r1",
            iteration=1,
            base_candidate_id="c0",
            spec_digest="s",
            objective="x",
            targets=["a"],
            acceptance_checks=[make_check()],
            hallucinated_field="ja",  # type: ignore[call-arg]
        )


def test_a_plan_without_an_acceptance_criterion_is_rejected():
    with pytest.raises(ValidationError):
        make_plan(acceptance_checks=[])


def test_a_plan_without_a_target_is_rejected():
    with pytest.raises(ValidationError):
        make_plan(targets=[])


def test_duplicate_check_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate check_id"):
        make_plan(acceptance_checks=[make_check("K1"), make_check("K1")])


def test_repair_only_without_a_justification_is_rejected():
    with pytest.raises(ValidationError, match="repair_reason"):
        make_plan(repair_only=True)


def test_repair_only_with_a_justification_is_valid():
    plan = make_plan(repair_only=True, repair_reason="Regression in K1")
    assert plan.repair_only


def test_iteration_zero_is_rejected():
    with pytest.raises(ValidationError):
        make_plan(iteration=0)


# --- The core defence: PASS without a receipt ------------------------------ #


def test_pass_without_a_receipt_is_inadmissible():
    with pytest.raises(ValidationError, match="PASS without a receipt"):
        CheckVerdict(check_id="K1", outcome=Outcome.PASS)


def test_fail_without_a_receipt_is_allowed():
    """A failure may be reported without evidence -- it claims nothing."""
    v = CheckVerdict(check_id="K1", outcome=Outcome.FAIL, note="crashes at startup")
    assert v.outcome is Outcome.FAIL


def test_inconclusive_without_a_receipt_is_allowed():
    v = CheckVerdict(check_id="K1", outcome=Outcome.INCONCLUSIVE, note="runner not startable")
    assert v.outcome is Outcome.INCONCLUSIVE


def test_a_role_result_accepts_only_on_all_pass():
    r = RoleResult(
        run_id="r1",
        iteration=1,
        attempt=1,
        role=Role.QA,
        candidate_id="c1",
        plan_digest="p",
        spec_digest="s",
        verdicts=[
            CheckVerdict(check_id="K1", outcome=Outcome.PASS, receipt_id="rc1"),
            CheckVerdict(check_id="K2", outcome=Outcome.INCONCLUSIVE),
        ],
    )
    assert not r.accepted(), "INCONCLUSIVE must never count as an acceptance"


def test_a_role_result_without_verdicts_is_not_an_acceptance():
    r = RoleResult(
        run_id="r1",
        iteration=1,
        attempt=1,
        role=Role.QA,
        candidate_id="c1",
        plan_digest="p",
        spec_digest="s",
    )
    assert not r.accepted(), "zero executed mandatory checks are not a success (A05)"


def test_the_receipt_outcome_hangs_on_the_exit_code():
    rc = Receipt(
        receipt_id="rc1",
        run_id="r1",
        iteration=1,
        attempt=1,
        check_id="K1",
        candidate_binding="b",
        command="true",
        exit_code=0,
        started_at="2026-09-07T00:00:00Z",
        ended_at="2026-09-07T00:00:01Z",
        stdout_digest="d",
        runner_identity="host/pid1",
    )
    assert rc.outcome(0) is Outcome.PASS
    assert rc.outcome(1) is Outcome.FAIL


# --- Budgets --------------------------------------------------------------- #


def make_state(**kw) -> RunState:
    base = dict(
        run_id="r1",
        repo_path="/tmp/repo",
        project_name="demo",
        spec_path="/tmp/spec.md",
        spec_digest="s",
        policy_digest="p",
        profile_digest="h",
    )
    return RunState(**{**base, **kw})


def test_the_iteration_budget():
    s = make_state(budgets=Budgets(max_iterations=2), usage=Usage(iterations=2))
    assert "iteration budget" in (s.budget_exhausted() or "")


def test_the_progress_budget():
    s = make_state(
        budgets=Budgets(max_loops_without_progress=3),
        usage=Usage(loops_without_progress=3),
    )
    assert "without traceable progress" in (s.budget_exhausted() or "")


def test_the_deadline_budget():
    s = make_state(budgets=Budgets(deadline="2000-01-01T00:00:00Z"))
    assert "deadline exceeded" in (s.budget_exhausted() or "")


def test_an_unconstrained_budget():
    assert make_state().budget_exhausted() is None


def test_the_default_delivery_is_conservative():
    """HoH must not derive any additional merge rights (handoff §6)."""
    s = make_state()
    assert s.yolo == "off"
    assert s.delivery_mode == "local-only"
    assert s.stage is Stage.NEW
