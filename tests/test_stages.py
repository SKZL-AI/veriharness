"""H1: state transitions, the freeze boundary, and the working/accepted split."""

from __future__ import annotations

import pytest

from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.stages import (
    FreezeError,
    TransitionError,
    accept_checkpoint,
    assert_binding_intact,
    block,
    cancel,
    freeze,
    pause,
    reject_candidate,
    resume,
    transition,
)


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


def cand(cid="c1", digest_="d1", clean=True) -> Candidate:
    return Candidate(
        candidate_id=cid,
        repo_path="/tmp/repo",
        commit="a" * 40,
        tree_clean=clean,
        tree_digest=digest_,
    )


def advance_to(state: RunState, stage: Stage) -> RunState:
    order = [Stage.PLANNING, Stage.DEVELOPING, Stage.VERIFYING]
    for s in order:
        transition(state, s)
        if s is stage:
            return state
    return state


# --- Permitted and forbidden edges ---------------------------------------- #


def test_happy_path_to_delivery():
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    transition(s, Stage.VERIFYING)
    transition(s, Stage.CHECKPOINTED)
    transition(s, Stage.READY_FOR_DELIVERY)
    assert s.stage is Stage.READY_FOR_DELIVERY


def test_skipping_a_stage_is_refused():
    s = make_state()
    with pytest.raises(TransitionError, match="Forbidden transition"):
        transition(s, Stage.VERIFYING)


def test_delivery_is_a_terminal_state():
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    transition(s, Stage.VERIFYING)
    transition(s, Stage.CHECKPOINTED)
    transition(s, Stage.READY_FOR_DELIVERY)
    with pytest.raises(TransitionError):
        transition(s, Stage.PLANNING)


def test_replan_edge_out_of_verifying():
    s = make_state()
    advance_to(s, Stage.VERIFYING)
    transition(s, Stage.PLANNING, reason="Replanning")
    assert s.stage is Stage.PLANNING
    assert s.iteration == 2, "replanning counts as a new iteration"


def test_planning_raises_the_iteration_and_resets_the_attempt():
    s = make_state()
    transition(s, Stage.PLANNING)
    s.attempt = 5
    transition(s, Stage.DEVELOPING)
    transition(s, Stage.VERIFYING)
    transition(s, Stage.PLANNING)
    assert s.iteration == 2
    assert s.attempt == 0


# --- Cross-cutting conditions block dispatch ------------------------------ #


@pytest.mark.parametrize("halt", [pause, block, cancel])
def test_no_transition_out_of_a_halted_condition(halt):
    s = make_state()
    transition(s, Stage.PLANNING)
    halt(s, "reason")
    with pytest.raises(TransitionError, match="refused"):
        transition(s, Stage.DEVELOPING)


def test_resume_only_out_of_paused():
    s = make_state()
    block(s, "waiting for the captain")
    with pytest.raises(TransitionError, match="Resume only out of PAUSED"):
        resume(s)


def test_pause_resume_cycle():
    s = make_state()
    transition(s, Stage.PLANNING)
    pause(s, "the captain paused")
    assert s.condition is Condition.PAUSED
    resume(s)
    assert s.condition is Condition.ACTIVE
    assert s.stop_reason is None
    transition(s, Stage.DEVELOPING)


def test_a_cancelled_run_is_not_paused():
    s = make_state()
    cancel(s, "Captain")
    with pytest.raises(TransitionError):
        pause(s, "too late")


# --- FREEZE as a persisted boundary --------------------------------------- #


def test_freeze_only_out_of_developing():
    s = make_state()
    transition(s, Stage.PLANNING)
    with pytest.raises(TransitionError, match="Freeze only out of DEVELOPING"):
        freeze(s, plan_digest="p1", candidate=cand())


def test_freeze_writes_the_binding_down():
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    c = cand()
    freeze(s, plan_digest="p1", candidate=c)
    assert s.frozen_plan_digest == "p1"
    assert s.frozen_binding == c.binding()
    assert s.frozen_at is not None


def test_an_intact_binding_accepts_the_same_candidate():
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    c = cand()
    freeze(s, plan_digest="p1", candidate=c)
    assert_binding_intact(s, c)  # must not raise


def test_the_binding_is_violated_when_the_sources_change():
    """A05: a source changed after QA has to be refused."""
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    freeze(s, plan_digest="p1", candidate=cand(digest_="original"))
    with pytest.raises(FreezeError, match="candidate binding violated"):
        assert_binding_intact(s, cand(digest_="changed_afterwards"))


def test_a_binding_check_without_a_freeze_is_an_error():
    s = make_state()
    with pytest.raises(FreezeError, match="No freeze active"):
        assert_binding_intact(s, cand())


def test_replanning_releases_the_boundary():
    s = make_state()
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    freeze(s, plan_digest="p1", candidate=cand())
    transition(s, Stage.VERIFYING)
    transition(s, Stage.PLANNING)
    assert s.frozen_binding is None
    assert s.frozen_plan_digest is None


# --- Working candidate vs. accepted candidate ----------------------------- #


def test_a_checkpoint_sets_the_last_accepted_candidate():
    s = make_state()
    advance_to(s, Stage.VERIFYING)
    c = cand("c-good")
    accept_checkpoint(s, c)
    assert s.stage is Stage.CHECKPOINTED
    assert s.last_accepted_candidate is not None
    assert s.last_accepted_candidate.candidate_id == "c-good"
    assert s.usage.loops_without_progress == 0


def test_a_rejection_leaves_the_last_accepted_candidate_untouched():
    """Handoff §6: the rejected candidate goes into planning, not into the
    delivery; last_accepted_candidate stays untouched."""
    s = make_state()
    advance_to(s, Stage.VERIFYING)
    accept_checkpoint(s, cand("c-good"))
    transition(s, Stage.PLANNING)
    transition(s, Stage.DEVELOPING)
    s.working_candidate = cand("c-broken", digest_="d2")
    transition(s, Stage.VERIFYING)
    reject_candidate(s, "K1 is red")

    assert s.last_accepted_candidate.candidate_id == "c-good"
    assert s.working_candidate.candidate_id == "c-broken", "the failed candidate is kept"
    assert s.stage is Stage.PLANNING
    assert s.usage.loops_without_progress == 1


def test_checkpoint_only_out_of_verifying():
    s = make_state()
    transition(s, Stage.PLANNING)
    with pytest.raises(TransitionError, match="Checkpoint only out of VERIFYING"):
        accept_checkpoint(s, cand())


def test_the_history_keeps_being_written_on():
    s = make_state()
    transition(s, Stage.PLANNING, reason="Start")
    pause(s, "Captain")
    assert any("stage NEW -> PLANNING" in h for h in s.history)
    assert any("PAUSED" in h for h in s.history)
