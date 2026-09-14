"""Regression tests for the findings of the adversarial dual review (2026-09-07).

Two independent reviewers attacked this state and both said "do not release".
Every proven finding gets a test here -- otherwise the gate would have achieved
nothing, and the same gap would come back with the next rebuild.

The numbers follow the reports: A* = reviewer A (correctness/integrity),
B* = reviewer B (attacker).

**Safety boundary of this suite (captain instruction 2026-09-07):** no
destructive commands appear here, not even as bare strings. The guard never
executes test inputs -- but a later refactoring could change that, and the
damage would be irreversible. The denylist coverage for deleting commands lives
in `~/.agents/hooks/test-guard.sh`, where inputs are held against patterns
exclusively via grep. `nvidia-smi` serves as the stand-in for the guard here:
same mechanics, no potential for damage.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from hoh import stages
from hoh.contracts import (
    AcceptanceCheck,
    Budgets,
    Candidate,
    CheckVerdict,
    Condition,
    DevelopmentPlan,
    Outcome,
    Receipt,
    Role,
    RunState,
    Stage,
)
from hoh.controller import NO_QA_VERDICT, Controller
from hoh.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from hoh.runner import PolicyUnavailable, assert_command_allowed, run_check
from hoh.store import RunStore, StoreError
from hoh.workspace import materialize, snapshot, unchanged

from test_controller import FakeDispatcher, build, plan_for, qa_pass  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@e.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text("# Spec\nadd(a,b) adds.\n", encoding="utf-8")
    return p


def make_state(**kw) -> RunState:
    base = dict(
        run_id="r1", repo_path="/tmp/repo", project_name="demo",
        spec_path="/tmp/spec.md", spec_digest="s", policy_digest="p", profile_digest="h",
    )
    return RunState(**{**base, **kw})


# =========================================================================== #
# A1 / B3 -- the candidate binding was blind to unstaged changes
# =========================================================================== #


def test_A1_binding_sees_an_unstaged_change_on_an_already_dirty_tree(repo: Path):
    """The real case: the developer edits without staging, then the freeze
    happens, then someone changes the source. `git write-tree` was blind to
    that, because it describes the index, not the working tree."""
    (repo / "app.py").write_text("def add(a, b):\n    return a + b  # dev\n", encoding="utf-8")
    candidate = snapshot(repo, "c1")
    assert candidate.tree_clean is False, "precondition: the tree is already dirty"

    (repo / "app.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")

    assert not unchanged(repo, candidate), (
        "an unstaged change to a tracked file MUST break the binding"
    )


def test_A1_two_different_contents_never_have_the_same_binding(repo: Path):
    (repo / "app.py").write_text("VERSION = 'A'\n", encoding="utf-8")
    a = snapshot(repo, "c1")
    (repo / "app.py").write_text("VERSION = 'B'\n", encoding="utf-8")
    b = snapshot(repo, "c1")
    assert a.binding() != b.binding()


def test_A1_binding_and_arena_agree(repo: Path, tmp_path: Path):
    """What gets materialized has to be the same thing that is bound."""
    (repo / "app.py").write_text("CONTENT = 'verified'\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    candidate = snapshot(repo, "c1")

    arena = materialize(candidate, tmp_path / "arena")
    assert (arena / "app.py").read_text(encoding="utf-8") == "CONTENT = 'verified'\n"
    assert (arena / "new.txt").exists(), "untracked files belong to the verified object"


# =========================================================================== #
# B7 -- materialize dereferenced symlinks
# =========================================================================== #


def test_B7_symlink_stays_a_symlink_instead_of_being_dereferenced(repo: Path, tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("SECRET\n", encoding="utf-8")
    os.symlink(secret_file, repo / "link.txt")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")

    arena = materialize(snapshot(repo, "c1"), tmp_path / "arena")
    target = arena / "link.txt"
    assert target.is_symlink(), "a symlink must not become a real file"
    # Honestly named (reviewer C): the link stays reachable, because there is
    # no OS sandbox. The gain is a different one -- the verified object is now
    # the same as in the repo, and the target enters the binding.
    assert os.readlink(target) == str(secret_file), "the link target is preserved unchanged"


def test_B7_symlink_target_enters_the_binding(repo: Path, tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A\n", encoding="utf-8")
    b.write_text("B\n", encoding="utf-8")
    os.symlink(a, repo / "link.txt")
    before = snapshot(repo, "c1")

    (repo / "link.txt").unlink()
    os.symlink(b, repo / "link.txt")
    assert snapshot(repo, "c1").binding() != before.binding()


# =========================================================================== #
# A2 -- QA staying silent produced an accepted checkpoint
# =========================================================================== #


def test_A2_prose_instead_of_json_never_leads_to_acceptance(tmp_path, repo, spec):
    class Mute(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.QA:
                self.calls.append(role)
                return "I was unfortunately not able to verify that. Sorry!"
            return super().dispatch(role, prompt, state=state)

    d = Mute(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)

    assert not out.accepted, "unreadable QA output must never become a checkpoint"
    assert state.stage is not Stage.CHECKPOINTED
    assert all(v.outcome is not Outcome.PASS for v in out.verdicts)


def test_A2_empty_verdicts_never_lead_to_acceptance(tmp_path, repo, spec):
    d = FakeDispatcher(
        repo=repo,
        plan_json=plan_for,
        qa_json=lambda s: {"verdicts": [], "open_gaps": [], "summary": "nothing verified"},
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert all(v.outcome is Outcome.INCONCLUSIVE for v in out.verdicts)


def test_A2_runner_can_refute_but_not_confirm(tmp_path, repo, spec):
    """The runner delivers facts. An acceptance presupposes a spoken assessment
    by the independent role (paper §3.4.3)."""
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="exit 1"),
        qa_json=lambda s: {"verdicts": [], "open_gaps": [], "summary": ""},
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    verdict = next(v for v in out.verdicts if v.check_id == "K1")
    assert verdict.outcome is Outcome.FAIL, "a red runner receipt stays a fact"


# =========================================================================== #
# A3 -- expect_exit turned guard violations and infrastructure errors into PASS
# =========================================================================== #


def test_A3_expect_exit_cannot_expect_an_infrastructure_code():
    for code in (124, 126, 127, 200):
        with pytest.raises(ValidationError):
            AcceptanceCheck(
                check_id="K1", description="x", command="true",
                expect_exit=code, expect_reason="trick",
            )


def test_A3_non_zero_expectation_needs_a_reason():
    with pytest.raises(ValidationError, match="expect_reason"):
        AcceptanceCheck(check_id="K1", description="x", command="false", expect_exit=1)
    AcceptanceCheck(
        check_id="K1", description="x", command="false",
        expect_exit=1, expect_reason="verifies that the error path takes effect",
    )


def test_A3_timeout_is_never_pass():
    r = Receipt(
        receipt_id="rc", run_id="r", iteration=1, attempt=1, check_id="K1",
        candidate_binding="b", command="sleep 99", exit_code=124,
        started_at="2026-09-07T00:00:00Z", ended_at="2026-09-07T00:01:00Z",
        stdout_digest="d", runner_identity="host", runner_ok=False,
    )
    assert r.outcome(124) is Outcome.INCONCLUSIVE
    assert r.outcome(0) is Outcome.INCONCLUSIVE


def test_A3_unstartable_process_is_never_pass(tmp_path: Path):
    check = AcceptanceCheck(check_id="K1", description="x", command="does-not-exist-xyz")
    receipt, _ = run_check(
        check,
        Candidate(candidate_id="c", repo_path=str(tmp_path), commit="a" * 40,
                  tree_clean=True, tree_digest="d"),
        run_id="r1", iteration=1, attempt=1, cwd=tmp_path,
    )
    assert receipt.exit_code == 127
    assert receipt.outcome(127) is Outcome.INCONCLUSIVE


def test_A3_guard_refusal_never_turns_green(tmp_path, repo, spec):
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="nvidia-smi -l 1"),
        qa_json=qa_pass,
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert all(v.outcome is not Outcome.PASS for v in out.verdicts)


# =========================================================================== #
# A4 -- the controller never took the lock
# =========================================================================== #


def test_A4_iteration_holds_the_controller_lock(tmp_path, repo, spec):
    observed: list[bool] = []

    class Spy(FakeDispatcher):
        def __init__(self, store, **kw):
            super().__init__(**kw)
            self.store = store

        def dispatch(self, role, prompt, *, state):
            if role is Role.PLANNER:
                second_store = RunStore(self.store.root, self.store.run_id)
                try:
                    with second_store.lock():
                        observed.append(False)  # the lock was free -- bad
                except Exception:
                    observed.append(True)       # taken -- right
            return super().dispatch(role, prompt, state=state)

    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="p", spec_path=spec,
    )
    store.write_state(state)
    d = Spy(store, repo=repo, plan_json=plan_for, qa_json=qa_pass)
    Controller(store, d, spec_path=spec).run_iteration(state)

    assert observed == [True], "during the iteration the lock has to be taken"


# =========================================================================== #
# A5 -- after a rejection the loop was dead
# =========================================================================== #


def test_A5_second_iteration_after_a_rejection_runs(tmp_path, repo, spec):
    round_ = {"n": 0}

    def qa(state):
        round_["n"] += 1
        if round_["n"] == 1:
            return {"verdicts": [{"check_id": "K1", "outcome": "FAIL", "note": "red"}],
                    "open_gaps": [], "summary": "rejected"}
        return qa_pass(state)

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    first = ctrl.run_iteration(state)
    assert not first.accepted
    assert state.stage is Stage.PLANNING

    second = ctrl.run_iteration(state)   # must not die with a TransitionError
    assert second.accepted, "the REPLAN branch has to be passable"


def test_A5_iteration_is_not_counted_twice(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)
    assert state.usage.iterations == 1, "one iteration driven consumes exactly one"


def test_A5_loopoutcome_reports_the_iteration_that_was_worked(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert out.iteration == 1
    assert out.candidate_id == "r1-i1"


def test_A5_three_rejections_reach_the_progress_limit(tmp_path, repo, spec):
    d = FakeDispatcher(
        repo=repo,
        plan_json=plan_for,
        qa_json=lambda s: {"verdicts": [{"check_id": "K1", "outcome": "FAIL"}],
                           "open_gaps": [], "summary": ""},
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=10, max_loops_without_progress=3)

    for _ in range(3):
        ctrl.run_iteration(state)
    assert state.usage.loops_without_progress == 3

    last = ctrl.run_iteration(state)
    assert not last.accepted
    assert state.condition is Condition.BLOCKED
    assert "traceable progress" in (state.blocked_reason or "")


# =========================================================================== #
# A10 -- BLOCKED was a dead end
# =========================================================================== #


def test_A10_a_block_can_be_lifted_with_a_reason():
    s = make_state()
    stages.block(s, "budget exhausted")
    stages.unblock(s, "budget raised by the captain")
    assert s.condition is Condition.ACTIVE
    assert s.blocked_reason is None
    assert any("UNBLOCKED" in h for h in s.history)


def test_A10_unblock_demands_a_reason():
    s = make_state()
    stages.block(s, "unclear")
    with pytest.raises(stages.TransitionError):
        stages.unblock(s, "   ")


def test_A10_unblock_only_out_of_blocked():
    s = make_state()
    with pytest.raises(stages.TransitionError, match="only out of BLOCKED"):
        stages.unblock(s, "without a reason")


# =========================================================================== #
# A6 -- evidence.json was never checked against the digest
# =========================================================================== #


def test_A6_manipulated_evidence_is_detected(tmp_path: Path):
    store = RunStore(tmp_path / "runs", "r1")
    bundle = EvidenceBundle(
        run_id="r1", iteration=1,
        items=[EvidenceItem(item_id="E-1", status=EvidenceStatus.GAP, claim="broken",
                            first_seen_iteration=1, last_seen_iteration=1)],
    )
    dig = store.write_evidence(bundle)

    data = json.loads(store.evidence_path.read_text(encoding="utf-8"))
    data["items"][0]["status"] = "VERIFIED"
    store.evidence_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises(StoreError, match="deviates from the state"):
        store.read_evidence(expect_digest=dig)


def test_A6_missing_evidence_despite_a_pointer_is_an_error(tmp_path: Path):
    store = RunStore(tmp_path / "runs", "r1")
    store.ensure()
    with pytest.raises(StoreError, match="is missing"):
        store.read_evidence(expect_digest="anything")


# =========================================================================== #
# A12 -- evidence lost information across loop boundaries
# =========================================================================== #


def test_A12_a_regression_stays_a_regression():
    """VERIFIED -> INSUFFICIENT -> GAP must not end up as a harmless GAP."""
    e = EvidenceBundle(
        run_id="r",
        items=[EvidenceItem(item_id="E-1", status=EvidenceStatus.VERIFIED, claim="c",
                            first_seen_iteration=1, last_seen_iteration=1, was_verified=True)],
    )
    e = e.carry_forward(
        [EvidenceItem(item_id="E-1", status=EvidenceStatus.INSUFFICIENT, claim="c",
                      first_seen_iteration=1, last_seen_iteration=2)], 2)
    assert e.items[0].status is EvidenceStatus.REGRESSION

    e = e.carry_forward(
        [EvidenceItem(item_id="E-1", status=EvidenceStatus.GAP, claim="c",
                      first_seen_iteration=1, last_seen_iteration=3)], 3)
    assert e.items[0].status is EvidenceStatus.REGRESSION, "the priority must not expire"
    assert e.to_repair()[0].item_id == "E-1"


def test_A12_a_stale_receipt_does_not_stick_to_a_red_item():
    e = EvidenceBundle(
        run_id="r",
        items=[EvidenceItem(item_id="E-1", status=EvidenceStatus.VERIFIED, claim="c",
                            receipt_ids=["rc-i1"], first_seen_iteration=1,
                            last_seen_iteration=1, was_verified=True)],
    )
    e = e.carry_forward(
        [EvidenceItem(item_id="E-1", status=EvidenceStatus.GAP, claim="c",
                      first_seen_iteration=1, last_seen_iteration=2)], 2)
    assert e.items[0].receipt_ids == [], "a red finding must not look substantiated"


# =========================================================================== #
# B6 -- check_id was not validated against a character set
# =========================================================================== #


@pytest.mark.parametrize(
    "bad", ["../../../etc/evil", "K1\n../evil", "Ｋ1", "K 1", "", "x" * 80]
)
def test_B6_exotic_check_id_fails_as_a_contract_violation(bad: str):
    with pytest.raises(ValidationError):
        AcceptanceCheck(check_id=bad, description="x", command="true")


# =========================================================================== #
# B5 -- unbounded output buffering
# =========================================================================== #


def test_B5_output_flood_kills_the_process_instead_of_eating_memory(tmp_path: Path):
    """C10: the earlier truncation was cosmetic.

    `communicate()` buffered the full output in the controller (measured
    +430 MB with 150 MB of check output) and truncated only afterwards. Now it
    is streamed and the process is terminated when the limit is exceeded --
    and that is an infrastructure error, not a product verdict.
    """
    check = AcceptanceCheck(
        check_id="K1", description="unbounded producer",
        command="cat /dev/zero | tr '\\0' 'x'",
    )
    receipt, log = run_check(
        check,
        Candidate(candidate_id="c", repo_path=str(tmp_path), commit="a" * 40,
                  tree_clean=True, tree_digest="d"),
        run_id="r1", iteration=1, attempt=1, cwd=tmp_path,
        max_output_bytes=200_000, timeout=30,
    )
    assert receipt.truncated, "the limit has to take effect"
    assert receipt.runner_ok is False, "a truncated run is an infrastructure error"
    assert receipt.outcome(0) is Outcome.INCONCLUSIVE
    assert len(log) < 2_000_000, "the transcript is returned truncated"


def test_B5_forking_check_runs(tmp_path: Path):
    """C9, critical: RLIMIT_NPROC counts ALL tasks of the UID, not the children
    of this run. Set to 512 it made every pipeline, every pytest, every make
    fail -- and that was booked as FAIL instead of as an infrastructure
    error."""
    check = AcceptanceCheck(
        check_id="K1", description="pipeline", command="echo a | wc -l",
    )
    receipt, log = run_check(
        check,
        Candidate(candidate_id="c", repo_path=str(tmp_path), commit="a" * 40,
                  tree_clean=True, tree_digest="d"),
        run_id="r1", iteration=1, attempt=1, cwd=tmp_path,
    )
    assert receipt.exit_code == 0, f"a simple pipeline has to run: {log[:400]}"
    assert "1" in log


def test_B5_timeout_also_ends_an_escaped_grandchild(tmp_path: Path):
    """C11: a grandchild with a session of its own held the pipe open, and
    `communicate()` without a timeout waited for it -- under the controller
    lock."""
    check = AcceptanceCheck(
        check_id="K1", description="grandchild escapes",
        command="setsid sleep 20 & exec sleep 60",
    )
    t0 = time.monotonic()
    receipt, _ = run_check(
        check,
        Candidate(candidate_id="c", repo_path=str(tmp_path), commit="a" * 40,
                  tree_clean=True, tree_digest="d"),
        run_id="r1", iteration=1, attempt=1, cwd=tmp_path, timeout=1,
    )
    duration = time.monotonic() - t0
    assert receipt.exit_code == 124
    assert receipt.runner_ok is False
    assert duration < 12, f"the runner must not wait for the grandchild (took {duration:.1f}s)"


# =========================================================================== #
# B1 -- guard circumventions
# =========================================================================== #


@pytest.mark.parametrize(
    "cmd",
    [
        "FOO=1 nvidia-smi",
        "eval nvidia-smi",
        "exec nvidia-smi",
        "command nvidia-smi",
        "env nvidia-smi",
        'bash -c "nvidia-smi"',
        "A=1 timeout 5 nvidia-smi",
        "n=nvidia-smi; $n",
    ],
)
def test_B1_circumventions_are_blocked(cmd: str):
    from hoh.runner import HouseRuleViolation

    with pytest.raises(HouseRuleViolation):
        assert_command_allowed(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -rn nvidia-smi src/",
        'grep -rn "nvidia-smi" docs/',
        "pytest -q",
        "ls -la",
        "python3 -m pytest tests/",
    ],
)
def test_B1_harmless_commands_stay_allowed(cmd: str):
    assert_command_allowed(cmd)


def test_B1_runner_fails_closed_without_a_policy(monkeypatch, tmp_path: Path):
    """Unlike the shell hooks: here it is about model-generated commands, and
    without a policy there is no reason to trust them."""
    import hoh.runner as r

    empty = tmp_path / "empty"
    empty.mkdir()
    # All **three** locations, not two. Until 2026-09-08 this test left
    # `_PACKAGE_PATTERNS` pointing at the real package and therefore asserted
    # something else than it claimed: "the package ships no patterns". The
    # moment the packaging half of K6 lands, that becomes false and the test
    # fails *because the increment is correct*. The `d1` planner found this
    # while writing its criteria, respected that `tests/` was out of its
    # scope, and reported it as an open gap instead of reaching in -- so the
    # repair belongs here.
    monkeypatch.setattr(r, "_HOME_PATTERNS", empty)
    monkeypatch.setattr(r, "_PACKAGE_PATTERNS", empty)
    monkeypatch.setattr(r, "_REPO_PATTERNS", empty)
    with pytest.raises(PolicyUnavailable):
        r.assert_command_allowed("echo harmless")


def test_O32_the_counter_is_not_migrated_across_the_fix_and_that_is_deliberate():
    """O32, persistence half: a state written **before** the fix keeps its
    inflated counter, and there is deliberately no migration.

    `usage.iterations` is persisted state, not a derived quantity. The fix
    changed *when* an iteration is charged, so it cannot retroactively
    un-charge what an older version already booked. Measured on the real runs
    of this campaign: `d2c` carries `iterations=2` with `max_iterations=2`
    after **one** real iteration, and `d2c2` carries `3/3` after **two** --
    both therefore report `iteration budget exhausted` on resume, and stay
    blocked.

    **Cross-version resume of the iteration budget is not supported in
    v0.1.0.** That is a decision, and this test is where it is written down
    rather than assumed:

    - Healing it would mean *decreasing* a persisted counter, which is the one
      thing this project's whole discipline is against.
    - The true count is not reliably recoverable either: `dispatches // 3` is
      a heuristic, and `d1` spent seven dispatches on two iterations because
      of an outage retry. A migration would have to guess.
    - Both affected runs are finished; neither needs to resume.

    If a later version does add a migration, this test fails and the decision
    gets revisited on purpose instead of drifting.
    """
    from hoh.contracts import Budgets, Usage

    state = make_state(budgets=Budgets(max_iterations=2))
    # Exactly the shape `d2c` has on disk: charged twice, one iteration's
    # worth of dispatches.
    state.usage = Usage(iterations=2, dispatches=3)

    message = state.budget_exhausted()
    assert message is not None, (
        "the persisted counter must still govern -- no silent recomputation"
    )
    assert "iteration budget exhausted (2/2)" in message

    # And the dispatch count is *not* used to second-guess it. If someone
    # wires that in, they change the contract and this assertion says so.
    assert "3" not in message.split("(")[1], (
        "budget_exhausted() must not derive the count from dispatches -- "
        "that would be a migration by the back door"
    )


def test_O32_a_rejection_does_not_charge_the_next_iteration_in_advance():
    """O32: `max_iterations = N` allowed only N-1 iterations after a rejection.

    The REPLAN edge transitions to PLANNING, and that transition used to raise
    `usage.iterations`. So the budget was charged at **rejection** time, before
    the next iteration existed. Measured on run `d2c`: `max_iterations=2`,
    three dispatches -- one real iteration -- rejected once, then
    `iteration budget exhausted (2/2)` with no second iteration ever starting.

    The charge now sits in `begin_iteration`, the only permitted way to begin
    one, so it is still counted exactly once but at the moment work starts.
    """
    from hoh.contracts import Budgets, Stage

    state = make_state(budgets=Budgets(max_iterations=2))

    # Iteration 1 begins and is charged once.
    stages.begin_iteration(state, reason="first")
    assert state.iteration == 1
    assert state.usage.iterations == 1
    assert state.budget_exhausted() is None

    # It is rejected. The iteration *number* rises; the budget does not.
    state.stage = Stage.VERIFYING
    stages.reject_candidate(state, reason="not accepted: K1")
    assert state.iteration == 2, "the replan edge must still raise the label"
    assert state.usage.iterations == 1, (
        "a rejection must not charge an iteration that has not begun -- "
        "this is the assertion that fails without the fix"
    )
    assert state.budget_exhausted() is None, (
        "with max_iterations=2 and one iteration done, the budget cannot be "
        "exhausted; d2c was blocked here"
    )

    # Iteration 2 begins -- and is charged now.
    stages.begin_iteration(state, reason="second")
    assert state.usage.iterations == 2
    assert state.budget_exhausted() is not None, (
        "after two begun iterations the bound is reached -- the fix must not "
        "make the budget unbounded"
    )

    # And the recovery path charges nothing: it keeps the same iteration.
    recovered = make_state(budgets=Budgets(max_iterations=5), stage=Stage.DEVELOPING)
    recovered.iteration = 3
    recovered.usage.iterations = 3
    stages.begin_iteration(recovered, reason="recovery")
    assert recovered.stage is Stage.PLANNING
    assert recovered.iteration == 3, "a recovery replans the same iteration"
    assert recovered.usage.iterations == 3, "so it must not be charged again"


def test_O21_the_exhaustion_message_says_when_outages_ate_the_budget():
    """O21: "iteration budget exhausted (3/3)" was misleading after outages.

    The accounting itself is right and stays untouched. `max_iterations` bounds
    **spend**: the counter rises when an iteration starts, and by the time an
    outage swallows the verdict the planner has already run and been paid for.
    `max_loops_without_progress` bounds **futility**, and an unanswered QA is
    a missing verdict rather than a failed one -- `stages.reject` already
    skips the progress counter for it.

    What was wrong was the sentence at the end. "Iteration budget exhausted"
    reads as "we tried three times and failed on the substance" when the truth
    may be "the infrastructure fell over three times". For a tool built
    against claims that look supported, making that claim itself is the one
    inexcusable version of this bug.
    """
    from hoh.contracts import Budgets, Stage
    from hoh.stages import UNVERIFIED

    # An outage: it must not touch the progress counter, but must be recorded.
    state = make_state(stage=Stage.VERIFYING, budgets=Budgets(max_iterations=2))
    stages.reject_candidate(state, reason=f"{UNVERIFIED} (quota)")
    assert state.usage.outages == 1
    assert state.usage.loops_without_progress == 0, (
        "an outage is not a failed attempt -- that separation predates this test"
    )

    # A substantive rejection: the other way round.
    state.stage = Stage.VERIFYING
    stages.reject_candidate(state, reason="not accepted: K3")
    assert state.usage.loops_without_progress == 1
    assert state.usage.outages == 1, "a real rejection is not an outage"

    # Now exhaust the iteration budget and read the sentence.
    state.usage.iterations = 2
    message = state.budget_exhausted()
    assert message is not None
    assert "iteration budget exhausted (2/2)" in message
    assert "outage" in message, f"the message hides why the budget went: {message}"

    # Negative control: without outages the clause must be absent, otherwise
    # the assertion above would pass on any wording at all.
    clean = make_state(budgets=Budgets(max_iterations=1))
    clean.usage.iterations = 1
    clean_message = clean.budget_exhausted()
    assert clean_message is not None
    assert "outage" not in clean_message, clean_message


def test_O30_git_in_the_arena_cannot_bind_to_the_ancestor_repo(tmp_path: Path):
    """O30: the arena has no `.git`, so git used to climb out of it.

    A frozen candidate is materialized with `git archive` and carries no
    repository of its own. Git's discovery walk then reached the **ancestor**
    checkout -- whose `.gitignore` excludes the `runs/` subtree the arena
    lives under, so arena files came back as "untracked" and a criterion that
    diffed the candidate against its base silently compared something else.
    Reproduced 2026-09-08: `git rev-parse --show-toplevel` inside an arena
    answered with the project checkout.

    The test carries its own negative control: the same command without the
    runner's environment still climbs out. Without that half, the assertion
    below could pass for the wrong reason -- for instance because `git` is
    missing from the machine.
    """
    import subprocess

    import hoh.runner as r
    from hoh.contracts import AcceptanceCheck, Candidate

    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    arena = outer / "runs" / "_arenas" / "abc123"
    arena.mkdir(parents=True)

    # Negative control: unprotected, git binds to the ancestor.
    unprotected = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=arena, capture_output=True, text=True,
    )
    assert unprotected.returncode == 0, "control failed -- is git available?"
    assert Path(unprotected.stdout.strip()).resolve() == outer.resolve(), (
        "the control did not reproduce the climb, so the assertion below "
        "would prove nothing"
    )

    check = AcceptanceCheck(
        check_id="K1",
        description="where does git think it is",
        command="git rev-parse --show-toplevel",
        expect_exit=0,
        preserves=False,
    )
    candidate = Candidate(
        candidate_id="c",
        repo_path=str(arena),
        commit="0" * 40,
        tree_clean=True,
        tree_digest="d" * 40,
        created_at="2026-09-08T00:00:00Z",
    )
    receipt, transcript = r.run_check(
        check, candidate, run_id="x", iteration=1, attempt=1, cwd=arena
    )

    assert receipt.exit_code != 0, (
        "git still resolved a repository from inside the arena: " + transcript
    )
    assert "not a git repository" in transcript.lower()
    # The ancestor must not appear in what the *command produced*. Checking the
    # whole transcript would be impossible rather than strict: it records
    # `# cwd=<arena>`, and the arena path has the ancestor's path as a prefix.
    # An assertion that cannot hold is not a strong assertion, it is a broken
    # one -- exactly the vacuous-check class this file collects (see K9).
    output = transcript.split("--- output ---", 1)[1]
    assert str(outer) not in output, (
        "git answered with the ancestor checkout: " + output
    )


def test_O16_the_substituted_arena_path_cannot_smuggle_anything(tmp_path: Path):
    """O16: the guard checks a different string than the shell runs.

    `assert_stays_in_arena` replaces `{ARENA}` with the literal `"ARENA"`; the
    runner replaces it with the real absolute path. The asymmetry is not a
    defect but a necessity -- checking the *resolved* form would make every
    legitimate `{ARENA}` use fail `_ABSOLUTE_PATH`, since the arena lives
    under a home directory.

    What that leaves open is a bounded question: can the substituted path
    itself carry something the guard would have rejected? It cannot, and the
    reason is the two components it is built from -- a validated run id and a
    hex digest. That reason was an argument in a comment; this test makes it a
    checked property, because an argument does not fail when someone changes
    the naming.
    """
    import re as _re

    from hoh.store import RunStore, StoreError

    # 1. The run id cannot contribute anything. These are what an operator
    #    could plausibly type or paste.
    for hostile in ("../escaped", "a b; echo pwned", "x/../../y", "a|b", "a$(id)"):
        with pytest.raises(StoreError):
            RunStore(tmp_path, hostile)

    # 2. The arena's own directory name is a digest, so its alphabet is fixed.
    from hoh.store import digest

    name = digest("secret:1:1:")[:12]
    assert _re.fullmatch(r"[0-9a-f]{12}", name), name

    # 3. Therefore the whole path is free of everything the guard looks for.
    store = RunStore(tmp_path, "run-1")
    arena = store.arenas_dir / name
    text = str(arena)
    assert ".." not in text
    for metachar in (";", "|", "&", "$", "`", "\n", "(", ")", "<", ">", "*", "?", "'", '"'):
        assert metachar not in text, f"{metachar!r} would reach the shell via {{ARENA}}"


def test_O14_an_unwritable_sink_becomes_a_receipt_not_an_exception(tmp_path: Path):
    """O14: creating the output buffer sat outside every `try`.

    A read-only parent, an exhausted inode table or -- the case that matters
    -- **a full disk** let `run_check` leave through an `OSError` instead of
    through a receipt. The contract says such a thing is an *infrastructure
    error*: `runner_ok=False`, therefore `INCONCLUSIVE`, therefore a visible
    non-verdict. What the controller actually got was a raw `PermissionError`.

    This is the shape A08 (disk full) takes, which is why it is worth more
    than its size suggests: it is the difference between "the run stopped" and
    "the run says why".
    """
    import os

    import hoh.runner as r
    from hoh.contracts import AcceptanceCheck, Candidate, Outcome

    if os.geteuid() == 0:
        pytest.skip("root ignores the write bit, so the failure cannot be provoked")

    arena = tmp_path / "arena"
    arena.mkdir()
    check = AcceptanceCheck(
        check_id="K1", description="d", command="true", expect_exit=0, preserves=False
    )
    candidate = Candidate(
        candidate_id="c",
        repo_path=str(arena),
        commit="0" * 40,
        tree_clean=True,
        tree_digest="d" * 40,
        created_at="2026-09-08T00:00:00Z",
    )

    os.chmod(tmp_path, 0o500)          # the sink's parent -- not the arena
    try:
        receipt, transcript = r.run_check(
            check, candidate, run_id="x", iteration=1, attempt=1, cwd=arena
        )
    finally:
        os.chmod(tmp_path, 0o700)

    assert receipt.runner_ok is False
    assert receipt.outcome(0) is Outcome.INCONCLUSIVE
    # And the cause has to be readable, not merely encoded in a flag.
    assert "buffer could not be created" in transcript


def test_O15_a_broken_pattern_line_fails_closed(monkeypatch, tmp_path: Path):
    """O15: a pattern that does not compile used to disappear in silence.

    `_load_patterns` swallowed the `re.error` with `continue`. The guard then
    ran with a **smaller** denylist and reported nothing -- reproduced with a
    file of three rules of which one was malformed: two loaded, no indication
    that a rule was gone. A guard that is quietly weaker than its own file is
    the exact shape of claim this project refuses elsewhere, so it now fails
    closed and names the offending line.
    """
    import hoh.runner as r

    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "house-rules-patterns.txt").write_text("# empty\n", encoding="utf-8")
    (policy / "dangerous-patterns.txt").write_text(
        "harmless_rule\nrm[[:space:]]+-rf[[:space:]]+/(\n", encoding="utf-8"
    )
    monkeypatch.setattr(r, "_HOME_PATTERNS", policy)
    monkeypatch.setattr(r, "_PACKAGE_PATTERNS", policy)
    monkeypatch.setattr(r, "_REPO_PATTERNS", policy)

    with pytest.raises(PolicyUnavailable) as exc:
        r.assert_command_allowed("echo harmless")
    message = str(exc.value)
    # The line has to be *named*: an operator who only learns "policy broken"
    # is no better off than before.
    assert "rm[[:space:]]+-rf[[:space:]]+/(" in message
    assert "dangerous-patterns.txt" in message

    # And the intact half is not what saves it: the harmless rule loaded fine.
    patterns, broken = r._load_patterns(policy / "dangerous-patterns.txt")
    assert [name for name, _ in patterns] == ["harmless_rule"]
    assert len(broken) == 1


def test_O15_the_shipped_pattern_files_compile_completely():
    """The counterpart: fail-closed only helps if we never ship a broken line.

    Without this, the fix above turns an authoring typo in the packaged
    policy into a runner that refuses every command -- correct behaviour,
    discovered at the worst possible moment. This is the cheap place to
    discover it instead.
    """
    import hoh.runner as r

    for name in ("dangerous-patterns.txt", "house-rules-patterns.txt"):
        path = r._pattern_file(name)
        assert path.exists(), f"{name} is not shipped at {path}"
        patterns, broken = r._load_patterns(path)
        assert broken == [], f"{name} carries lines that do not compile: {broken}"
        assert patterns, f"{name} compiled to no patterns at all"


def test_B1_repo_fallback_carries_the_policy(monkeypatch, tmp_path: Path):
    """C17: the guard guarantee hung on two unversioned files in the home
    directory -- on every other machine the runner failed closed."""
    import hoh.runner as r

    no_home = tmp_path / "no-home"
    no_home.mkdir()
    monkeypatch.setattr(r, "_HOME_PATTERNS", no_home)
    r.assert_command_allowed("pytest -q")          # the repo fallback carries
    with pytest.raises(r.HouseRuleViolation):
        r.assert_command_allowed("nvidia-smi")


def test_B1_no_login_profile_and_no_real_home(tmp_path: Path):
    """A check must not get at ~/.ssh or ~/.claude."""
    check = AcceptanceCheck(
        check_id="K1", description="environment", command="echo \"HOME=$HOME\"",
    )
    receipt, log = run_check(
        check,
        Candidate(candidate_id="c", repo_path=str(tmp_path), commit="a" * 40,
                  tree_clean=True, tree_digest="d"),
        run_id="r1", iteration=1, attempt=1, cwd=tmp_path,
    )
    assert receipt.exit_code == 0
    assert str(tmp_path) in log
    assert str(Path.home()) not in log.split("HOME=")[1].split("\n")[0]


# =========================================================================== #
# D1 -- preservation requirements were not enforced (reviewer D, critical)
# =========================================================================== #


def test_D1_omitted_check_still_runs_again(tmp_path, repo, spec):
    """The heaviest finding of the second round.

    Iteration 1 validates CORE. In iteration 2 the developer destroys the
    feature and the planner simply leaves CORE out. Previously that was
    accepted as a checkpoint, while `hoh report` kept listing CORE as
    "validated" -- the report claimed a guarantee that was false for the
    accepted candidate.
    """
    # feature.sh does NOT exist in the initial state -- otherwise CORE would
    # already be green there and would be downgraded as non-discriminating.
    round_ = {"n": 0}

    def plan(state: RunState) -> dict:
        round_["n"] += 1
        p = plan_for(state, check_id="CORE", command="sh feature.sh")
        if round_["n"] >= 2:
            # The planner leaves CORE out and plans something entirely different
            p["acceptance_checks"] = [
                {"check_id": "NEW", "description": "something else", "command": "true",
                 "expect_exit": 0, "preserves": False}
            ]
        return p

    def qa(state: RunState) -> dict:
        cid = "CORE" if round_["n"] == 1 else "NEW"
        return qa_pass(state, cid)

    def develop(r: Path) -> None:
        if round_["n"] == 1:
            (r / "feature.sh").write_text("exit 0\n", encoding="utf-8")   # builds the feature
        else:
            (r / "feature.sh").write_text("exit 1\n", encoding="utf-8")   # destroys it

    d = FakeDispatcher(repo=repo, plan_json=plan, qa_json=qa, develop=develop)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    first = ctrl.run_iteration(state)
    assert first.accepted
    assert "CORE" in store.read_checks(), "a passed check becomes a preservation requirement"

    second = ctrl.run_iteration(state)

    executed = {p.stem.rsplit("-", 1)[-1] for p in store.receipts()}
    assert "CORE" in executed, "the preservation suite runs again on EVERY candidate"
    assert not second.accepted, "a real regression must not become a checkpoint"


def test_D1_preservation_check_is_sharpened_by_the_planner_not_dropped(tmp_path, repo, spec):
    """A planner may sharpen a criterion -- but not drop it."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    suite = store.read_checks()
    assert suite["K1"].preserves is True


# =========================================================================== #
# D3 -- the plan binding was not checked
# =========================================================================== #


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "A-COMPLETELY-DIFFERENT-RUN"),
        ("iteration", 99),
        ("spec_digest", "0000000000000000"),
        ("base_candidate_id", "candidate-that-never-existed"),
    ],
)
def test_D3_unbound_plan_is_rejected(tmp_path, repo, spec, field, value):
    def plan(state: RunState) -> dict:
        p = plan_for(state)
        p[field] = value
        return p

    d = FakeDispatcher(repo=repo, plan_json=plan, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    from hoh.controller import DispatchError

    with pytest.raises((DispatchError, Exception)) as exc:
        ctrl.run_iteration(state)
    assert ("not bound to this run" in str(exc.value)
            or "output contract" in str(exc.value))


# =========================================================================== #
# D2 -- a stale controller destroyed the accepted checkpoint
# =========================================================================== #


def test_D2_stale_controller_does_not_destroy_the_checkpoint(tmp_path, repo, spec):
    """Reviewer D reproduced it with a genuinely parked state.json.v1."""
    from hoh.store import StaleWrite

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)
    assert state.stage is Stage.CHECKPOINTED
    accepted = state.last_accepted_candidate.candidate_id

    # A controller that still holds the initial state in its hand
    stale = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="project", spec_path=spec,
    )
    with pytest.raises(StaleWrite):
        store.write_state(stale)

    assert store.read_state().last_accepted_candidate.candidate_id == accepted


# =========================================================================== #
# C1 -- files with an umlaut were invisible to the binding (reviewer C, critical)
# =========================================================================== #


def test_C1_non_ascii_file_enters_the_binding(repo: Path):
    """Without `-z` git quotes non-ASCII paths. The quoted path does not exist
    on disk -- the content dropped out of the binding, and `unchanged()` was
    blind again on an already dirty tree. In a German-language project that is
    no edge case.

    The non-ASCII file names in this test are the object under verification and
    stay as they are; translating them away would remove the coverage.
    """
    target = repo / "prüfung.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "umlaut")

    (repo / "app.py").write_text("# already dirty\n", encoding="utf-8")
    candidate = snapshot(repo, "c1")
    assert candidate.tree_clean is False

    target.write_text("VALUE = 999\n", encoding="utf-8")
    assert not unchanged(repo, candidate), "the change MUST break the binding"


def test_C1_non_ascii_file_lands_in_the_arena(repo: Path, tmp_path: Path):
    (repo / "größe.txt").write_text("content\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "umlaut")

    arena = materialize(snapshot(repo, "c1"), tmp_path / "arena")
    assert (arena / "größe.txt").exists(), "QA must not verify an incomplete tree"


def test_C1_newline_in_the_file_name(repo: Path, tmp_path: Path):
    name = "strange\nname.txt"
    (repo / name).write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "newline")

    arena = materialize(snapshot(repo, "c1"), tmp_path / "arena")
    assert (arena / name).exists()


# =========================================================================== #
# C2 -- the file mode was missing from the binding
# =========================================================================== #


def test_C2_chmod_breaks_the_binding(repo: Path):
    script = repo / "run.sh"
    script.write_text("echo hello\n", encoding="utf-8")
    script.chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "script")

    (repo / "app.py").write_text("# dirty\n", encoding="utf-8")
    candidate = snapshot(repo, "c1")

    script.chmod(0o644)   # take the x bit away after the freeze
    assert not unchanged(repo, candidate), "materialize copies the mode along"


# =========================================================================== #
# C12 -- the ERE translation was broken
# =========================================================================== #


def test_C12_posix_classes_get_translated():
    from hoh.runner import ere_to_python

    assert ere_to_python("[^[:space:]]") == "[^ \\t\\n\\r\\f\\v]"
    assert ere_to_python("[[:alnum:]]+") == "[a-zA-Z0-9]+"


@pytest.mark.parametrize(
    "cmd",
    [
        "/usr/bin/nvidia-smi",
        "A=1 B=2 /usr/local/bin/nvidia-smi",
        "./nvidia-smi",
        "SEED=abc nvidia-smi",
    ],
)
def test_C12_path_and_env_circumventions_are_blocked(cmd: str):
    """Reviewer C got these four through: the translation left
    `[^[:space:]]` standing, and an absolute path was nowhere covered."""
    from hoh.runner import HouseRuleViolation

    with pytest.raises(HouseRuleViolation):
        assert_command_allowed(cmd)


# =========================================================================== #
# C5 -- pause/cancel were impossible during a running iteration
# =========================================================================== #


def test_C5_pause_takes_effect_at_the_next_safe_boundary(tmp_path, repo, spec):
    """The lock fix had taken from the operator exactly the capability that
    handoff §7 demands: `pause` tried the same lock non-blocking and gave up
    with "is already held". The request is now stored lock-free."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    store.request_stop("pause", "captain is calling it a day")
    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert state.condition is Condition.PAUSED
    assert "calling it a day" in (state.stop_reason or "")
    assert store.read_stop_request() is None, "a completed request is not carried out twice"


def test_C5_cancel_takes_effect_just_as_well(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    store.request_stop("cancel", "order withdrawn")
    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert state.condition is Condition.CANCELLED


def test_C5_stop_request_needs_no_lock(tmp_path, repo, spec):
    """The core of the finding: the request has to be storable WHILE the
    controller holds the lock."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    _, _, store = build(tmp_path, repo, spec, d)

    with store.lock():
        store.request_stop("pause", "while the lock is held")
    assert store.read_stop_request()["kind"] == "pause"


# =========================================================================== #
# C7 -- an abort in the developer step wedged the run permanently
# =========================================================================== #


def test_C7_abort_in_developing_can_be_replanned(tmp_path, repo, spec):
    """DEVELOPING is the LLM step and therefore the most likely place to abort.
    `DEVELOPING -> PLANNING` was forbidden, `unblock` did not help -- every
    restart died on a TransitionError."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    stages.transition(state, Stage.PLANNING)
    stages.transition(state, Stage.DEVELOPING)   # the run aborts here

    stages.begin_iteration(state, reason="restart")
    assert state.stage is Stage.PLANNING
    assert any("RECOVERY" in h for h in state.history)

    out = ctrl.run_iteration(state)
    assert out.accepted, "after the recovery the loop has to run again"


def test_C7_abort_in_verifying_can_be_replanned():
    s = make_state()
    stages.transition(s, Stage.PLANNING)
    stages.transition(s, Stage.DEVELOPING)
    stages.transition(s, Stage.VERIFYING)
    stages.begin_iteration(s, reason="restart")
    assert s.stage is Stage.PLANNING
    assert s.attempt == 0


# =========================================================================== #
# C6 -- the fencing failed silently on an unreadable state
# =========================================================================== #


def test_C6_unreadable_state_blocks_instead_of_failing_open(tmp_path: Path):
    store = RunStore(tmp_path / "runs", "r1")
    state = make_state()
    store.write_state(state)
    store.state_path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(StoreError, match="is unreadable"):
        store.write_state(make_state())


# =========================================================================== #
# C16 -- PolicyUnavailable tore the run down with a traceback
# =========================================================================== #


def test_C16_guard_receipt_is_not_a_product_verdict(tmp_path, repo, spec):
    """The receipt of a refused check carried runner_ok=True; only the exit
    code 126 saved it."""
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="nvidia-smi"),
        qa_json=qa_pass,
    )
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    receipt = json.loads((store.receipts_dir / f"{state.run_id}-i1-a1-K1.json").read_text())
    assert receipt["runner_ok"] is False
    assert receipt["exit_code"] == 126


# =========================================================================== #
# D4 -- there was no operator path that drives an iteration
# =========================================================================== #


def test_D4_cli_knows_run(tmp_path, repo, spec):
    """`hoh start` created a run that stayed in NEW; `run_iteration` was called
    exclusively from tests."""
    from hoh.cli import build_parser

    args = build_parser().parse_args(
        ["--root", str(tmp_path), "run", "r1", "--iterations", "2", "--no-herdr"]
    )
    assert args.cmd == "run"
    assert args.iterations == 2
    assert args.qa == "kimi", "by default QA runs on a different model"


def test_D4_start_rejects_a_non_git_directory(tmp_path, spec, capsys):
    from hoh.cli import main

    no_repo = tmp_path / "empty"
    no_repo.mkdir()
    rc = main(["--root", str(tmp_path / "runs"), "start",
               "--repo", str(no_repo), "--spec", str(spec), "--run-id", "x"])
    assert rc == 2
    assert "is not a git worktree" in capsys.readouterr().err


def test_D4_start_rejects_a_missing_directory(tmp_path, spec, capsys):
    from hoh.cli import main

    rc = main(["--root", str(tmp_path / "runs"), "start",
               "--repo", str(tmp_path / "does-not-exist"), "--spec", str(spec),
               "--run-id", "x"])
    assert rc == 2
    assert "Not a directory" in capsys.readouterr().err


def test_D4_herdr_requirement_is_not_silently_circumventable(monkeypatch, tmp_path):
    """A01: no silent tmux fallback. Without Herdr the adapter has to refuse."""
    from hoh.contracts import Role
    from hoh.controller import DispatchError
    from hoh.dispatchers import build_dispatcher

    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(DispatchError, match="Herdr is not available"):
        build_dispatcher(
            answers_dir=tmp_path / "a",
            profiles={Role.PLANNER: "claude"},
            cwd=tmp_path,
            prefer_herdr=True,
        )


def test_D4_without_herdr_only_deliberately(monkeypatch, tmp_path):
    from hoh.contracts import Role
    from hoh.dispatchers import HarnessDispatcher, build_dispatcher

    monkeypatch.delenv("HERDR_ENV", raising=False)
    d = build_dispatcher(
        answers_dir=tmp_path / "a",
        profiles={Role.PLANNER: "claude"},
        cwd=tmp_path,
        prefer_herdr=False,
    )
    assert isinstance(d, HarnessDispatcher)
    assert "subprocess" not in d.endpoint_evidence(Role.PLANNER), "no run yet"


# =========================================================================== #
# V1 -- HoH adopted foreign sessions (noticed by the captain in production)
# =========================================================================== #


def test_V1_dispatcher_does_not_adopt_a_foreign_session(monkeypatch, tmp_path):
    """The captain noticed that his running research session received a
    foreign role prompt.

    Cause: `reuse_idle=True` called `herdr.find_agent(kind=..., idle_only=True)`
    -- that returns the FIRST matching agent anywhere in Herdr. Convenience
    was no reason to write into a session that does not belong to us.
    """
    import inspect

    from hoh import dispatchers

    source = inspect.getsource(dispatchers.HerdrDispatcher)
    # Check for the CALL, not for the word -- the name appears in the comment
    # deliberately, so that nobody builds the adoption back in.
    calls = [line for line in source.splitlines()
             if "find_agent(" in line and not line.strip().startswith("#")]
    assert not calls, f"the dispatcher must not search for foreign sessions: {calls}"
    assert "reuse_idle" not in inspect.signature(
        dispatchers.HerdrDispatcher.__init__
    ).parameters, "the adoption option has been dropped without replacement"


def test_V1_only_self_started_panes_get_closed(tmp_path):
    """And the other direction: foreign panes are never closed."""
    import inspect

    from hoh import dispatchers

    source = inspect.getsource(dispatchers.HerdrDispatcher.close_own)
    assert "own_tabs" in source


# =========================================================================== #
# H3 -- crash and restart (A06, A09)
# =========================================================================== #


def test_H3_start_intent_is_persisted_before_the_dispatch(tmp_path, repo, spec):
    """Handoff §7: a unique attempt key and a persisted start intent prevent
    duplicate worker starts after a crash. `active_tasks` previously had no
    writer at all."""
    seen: list[int] = []

    class Observer(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            # During the dispatch the intent has to be on disk.
            seen.append(len(store_ref[0].read_state().active_tasks))
            return super().dispatch(role, prompt, state=state)

    store_ref = []
    d = Observer(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    store_ref.append(store)

    ctrl.run_iteration(state)

    assert max(seen) >= 1, "during a role run the intent is persisted"
    assert state.active_tasks == [], "after the run it gets cleaned up again"


def test_H3_attempt_key_is_unique_per_role_and_attempt(tmp_path, repo, spec):
    keys: list[str] = []

    class Collector(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            keys.extend(t.attempt_key for t in state.active_tasks)
            return super().dispatch(role, prompt, state=state)

    d = Collector(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    assert len(set(keys)) == len(keys), "no attempt key appears twice"


def test_H3_resume_blocks_on_an_unclear_state(tmp_path, repo, spec, capsys):
    """Do not restart blindly: a start intent without a confirmed endpoint is
    not decidable."""
    from hoh.cli import main
    from hoh.contracts import TaskRef

    _, state, store = build(tmp_path, repo, spec, FakeDispatcher(repo=repo))
    state.active_tasks.append(
        TaskRef(task_id="r1-i1-a1-developer", role=Role.DEVELOPER,
                harness="claude", attempt_key="k1")
    )
    stages.pause(state, "crash simulated")
    store.write_state(state)

    rc = main(["--root", str(tmp_path / "runs"), "resume", "r1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not decidable" in err
    assert store.read_state().condition is Condition.BLOCKED


def test_H3_resume_with_force_cleans_up_deliberately(tmp_path, repo, spec, capsys):
    from hoh.cli import main
    from hoh.contracts import TaskRef

    _, state, store = build(tmp_path, repo, spec, FakeDispatcher(repo=repo))
    state.active_tasks.append(
        TaskRef(task_id="r1-i1-a1-developer", role=Role.DEVELOPER,
                harness="claude", attempt_key="k1")
    )
    stages.pause(state, "crash simulated")
    store.write_state(state)

    rc = main(["--root", str(tmp_path / "runs"), "resume", "r1", "--force"])
    assert rc == 0
    fresh = store.read_state()
    assert fresh.active_tasks == []
    assert fresh.condition is Condition.ACTIVE
    assert any("resume forced" in h for h in fresh.history)


def test_H3_resume_reports_an_interrupted_iteration(tmp_path, repo, spec, capsys):
    from hoh.cli import main

    _, state, store = build(tmp_path, repo, spec, FakeDispatcher(repo=repo))
    stages.transition(state, Stage.PLANNING)
    stages.transition(state, Stage.DEVELOPING)
    stages.pause(state, "crashed in the middle of it")
    store.write_state(state)

    rc = main(["--root", str(tmp_path / "runs"), "resume", "r1"])
    assert rc == 0
    assert "in the middle of an iteration" in capsys.readouterr().out


# =========================================================================== #
# E1 -- the planner could plan itself green (reviewer E, sharpest finding)
# =========================================================================== #


def test_E1_criterion_already_green_before_carries_no_pass(tmp_path, repo, spec):
    """The developer cannot write itself green -- that held. But the acceptance
    criteria were written by the planner model, and runner as well as QA
    executed them faithfully. The entire apparatus of evidence therefore stood
    below the one decision that determines the verdict.

    Countermove: a criterion that was already green on the predecessor state
    does not demonstrate the increment.

    Correction provenance (A02 finding 10, 2026-09-07): the original version
    of this test additionally demanded `outcome is INCONCLUSIVE`. That was too
    sharp and was refuted in the real run: it also hit **preservation
    criteria**, whose whole job consists of being green on both states. Two
    complete A02 loops with a proven increment (K2/K3/K4/K5/K8 red on the base
    state) were rejected because of it. The core statement of the test remains
    unchanged and is still verified here: **a plan made up entirely of
    vacuous criteria carries no checkpoint.** Only the mechanism is a
    different one -- no longer a devaluation of the verdict, but the
    requirement of at least one discriminating criterion.
    """
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="true"),   # green on every state
        qa_json=qa_pass,
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted, "a vacuous criterion must not carry a checkpoint"
    v = next(v for v in out.verdicts if v.check_id == "K1")
    assert v.discriminates is False, "it does not demonstrate the increment"
    assert "demonstrates no increment" in (v.note or "")


def test_E1_discriminating_criterion_still_carries(tmp_path, repo, spec):
    """The counter-check: a criterion that measures the difference counts."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


def test_E1_preservation_checks_are_not_examined(tmp_path, repo, spec):
    """Only NEW criteria have to discriminate. A preservation check is green on
    the predecessor state by its very nature -- that is its purpose."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    ctrl.run_iteration(state)
    assert "K1" in store.read_checks()

    second = ctrl.run_iteration(state)
    v = next((v for v in second.verdicts if v.check_id == "K1"), None)
    assert v is not None
    assert "demonstrates no increment" not in (v.note or ""), (
        "a preservation check must not be downgraded as non-discriminating"
    )


# =========================================================================== #
# E2 -- materialize read the live tree instead of the bound content
# =========================================================================== #


def test_E2_old_candidate_is_restored_correctly(repo: Path, tmp_path: Path):
    """The hand-rolled copy read the *current* working tree. A candidate from
    an earlier iteration therefore came out silently wrong -- and the check
    'was the criterion already green before' compared against the new state.
    `git archive` works on the tree object."""
    (repo / "app.py").write_text("STATE = 'old'\n", encoding="utf-8")
    old = snapshot(repo, "c-old")

    (repo / "app.py").write_text("STATE = 'new'\n", encoding="utf-8")
    new = snapshot(repo, "c-new")

    arena_old = materialize(old, tmp_path / "old")
    arena_new = materialize(new, tmp_path / "new")

    assert (arena_old / "app.py").read_text(encoding="utf-8") == "STATE = 'old'\n"
    assert (arena_new / "app.py").read_text(encoding="utf-8") == "STATE = 'new'\n"


def test_E2_the_real_index_stays_untouched(repo: Path):
    """The temporary index must not change `git status` in the project."""
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    before = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    snapshot(repo, "c1")
    after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout
    assert before == after


# =========================================================================== #
# A10 -- delivery was an enum value without effect
# =========================================================================== #


def test_A10_without_approval_nothing_is_delivered(repo: Path):
    from hoh.delivery import DeliveryRefused, deliver

    with pytest.raises(DeliveryRefused, match="No approval"):
        deliver(repo=repo, mode="local-only", yolo="off", approved=False,
                target_branch="master", candidate_id="c1")


def test_A10_without_an_accepted_candidate_nothing_is_delivered(repo: Path):
    from hoh.delivery import DeliveryRefused, deliver

    with pytest.raises(DeliveryRefused, match="no accepted candidate"):
        deliver(repo=repo, mode="local-only", yolo="off", approved=True,
                target_branch="master", candidate_id=None)


def test_A10_yolo_on_is_refused(repo: Path):
    """HoH does not derive merge autonomy for itself."""
    from hoh.delivery import DeliveryRefused, deliver

    with pytest.raises(DeliveryRefused, match="not provided for"):
        deliver(repo=repo, mode="local-only", yolo="on", approved=True,
                target_branch="master", candidate_id="c1")


def test_A10_pr_modes_do_not_deliver_themselves(repo: Path):
    """direct-PR and no-mistakes belong to firstmate and to the captain."""
    from hoh.delivery import deliver

    for mode in ("direct-PR", "no-mistakes"):
        r = deliver(repo=repo, mode=mode, yolo="off", approved=True,
                    target_branch="master", candidate_id="c1")
        assert not r.delivered
        assert "derives no rights" in r.description


def test_A10_dirty_tree_is_refused(repo: Path):
    from hoh.delivery import DeliveryRefused, deliver

    git(repo, "checkout", "-qb", "work")
    (repo / "app.py").write_text("# not landed\n", encoding="utf-8")
    with pytest.raises(DeliveryRefused, match="is not clean"):
        deliver(repo=repo, mode="local-only", yolo="off", approved=True,
                target_branch="master", candidate_id="c1")


def test_A10_no_fast_forward_is_refused(repo: Path):
    from hoh.delivery import DeliveryRefused, deliver

    git(repo, "checkout", "-qb", "work")
    (repo / "new.py").write_text("x=1\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "work")
    git(repo, "checkout", "-q", "master")
    (repo / "other.py").write_text("y=2\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "diverged")
    git(repo, "checkout", "-q", "work")

    with pytest.raises(DeliveryRefused, match="not a direct descendant"):
        deliver(repo=repo, mode="local-only", yolo="off", approved=True,
                target_branch="master", candidate_id="c1")


def test_A10_clean_fast_forward_goes_through(repo: Path):
    from hoh.delivery import deliver

    git(repo, "checkout", "-qb", "work")
    (repo / "new.py").write_text("x=1\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "increment")

    r = deliver(repo=repo, mode="local-only", yolo="off", approved=True,
                target_branch="master", candidate_id="c1")
    assert r.delivered
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "master"],
                            capture_output=True, text=True).stdout.strip()
    now = subprocess.run(["git", "-C", str(repo), "rev-parse", "work"],
                         capture_output=True, text=True).stdout.strip()
    assert before == now, "master now points at the work state"


# =========================================================================== #
# §7 -- rotation and retention limit
# =========================================================================== #


def test_rotation_parks_old_states_without_touching_receipts(tmp_path: Path):
    store = RunStore(tmp_path / "runs", "r1")
    state = make_state()
    for i in range(30):
        state.iteration = i
        store.write_state(state)

    from hoh.contracts import Candidate

    receipt = Candidate(candidate_id="c", repo_path="/tmp", commit="a" * 40,
                        tree_clean=True, tree_digest="d")
    store.write_receipt("rc-1", receipt)
    store.write_log("rc-1", "output")

    pruned = store.prune(keep_states=5)

    assert len(pruned["states"]) > 0
    assert len(store.parked_states()) == 5
    assert (store.dir / "attic" / "states").is_dir(), "parked, not deleted"
    assert len(store.receipts()) == 1, "receipts are never rotated"
    assert (store.logs_dir / "rc-1.txt").exists(), "logs are never rotated"


def test_disk_usage_is_measured(tmp_path: Path):
    store = RunStore(tmp_path / "runs", "r1")
    store.write_state(make_state())
    b = store.usage_bytes()
    assert set(b) == {"arena", "attic", "receipts", "results", "states"}
    assert b["states"] > 0


# =========================================================================== #
# A05 -- a manipulated log was unguarded
# =========================================================================== #


def test_A05_manipulated_log_is_detected(tmp_path, repo, spec):
    """`verify_log` was built, but without a caller -- a decorative protection."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    from hoh.runner import verify_log

    receipt_file = store.receipts()[0]
    from hoh.contracts import Receipt

    receipt = Receipt.model_validate_json(receipt_file.read_text(encoding="utf-8"))
    assert verify_log(receipt, "completely different content") is not None
    genuine = (store.logs_dir / f"{receipt.receipt_id}.txt").read_text(encoding="utf-8")
    assert verify_log(receipt, genuine) is None


# =========================================================================== #
# A13 -- the repair budget was a run counter instead of one per role output
# =========================================================================== #


def test_A13_every_role_gets_its_own_repair_round(tmp_path, repo, spec):
    """Handoff §8 means "one schema repair per role output". The counter was
    never reset: after ONE repair in the whole run, every further form error
    became a hard abort immediately -- which happened in the real A02 run.
    """
    broken = {"planner": 0, "qa": 0}

    def plan(state: RunState):
        broken["planner"] += 1
        if broken["planner"] == 1:
            return {"objective": "incomplete"}          # first form error
        return plan_for(state)

    def qa(state: RunState):
        broken["qa"] += 1
        if broken["qa"] == 1:
            return "this is not JSON"                   # second form error
        return qa_pass(state)

    class FormError(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.QA:
                self.calls.append(role)
                answer = qa(state)
                return answer if isinstance(answer, str) else json.dumps(answer)
            return super().dispatch(role, prompt, state=state)

    d = FormError(repo=repo, plan_json=plan, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)
    assert out.accepted, (
        "two different roles may each get one repair: "
        f"{out.reason}"
    )


def test_repeated_iteration_does_not_collide_with_its_own_artifacts(tmp_path, repo, spec):
    """Noticed in the real A02 run: after a blocked attempt the restart failed
    on `plan-i1.json already exists`. Results are immutable for good reason --
    the name has to carry the attempt."""
    round_ = {"n": 0}

    def qa(state: RunState):
        round_["n"] += 1
        if round_["n"] == 1:
            return {"verdicts": [{"check_id": "K1", "outcome": "FAIL"}],
                    "open_gaps": [], "summary": "rejected"}
        return qa_pass(state)

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    ctrl.run_iteration(state)          # rejected -> replanning
    second = ctrl.run_iteration(state)  # must not die on ImmutableViolation

    assert second.accepted, second.reason
    names = {p.stem for p in store.results()}
    assert len(names) >= 4, f"every attempt has artifacts of its own: {names}"


# =========================================================================== #
# A06 -- crash boundaries: resume without a lost state, without a double start
# =========================================================================== #


@pytest.mark.parametrize("boundary", ["after_start_intent", "after_plan", "after_freeze", "in_qa"])
def test_A06_crash_at_every_boundary_can_be_resumed(
    tmp_path, repo, spec, boundary
):
    """Handoff §11: process abort after the start intent, after the spawn, after
    the freeze and before/after the state commit; resume without a lost
    accepted state and without a duplicate worker."""

    class Crash(RuntimeError):
        pass

    class Topples(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if boundary == "after_start_intent" and role is Role.PLANNER:
                raise Crash("topples right after the start intent")
            if boundary == "after_plan" and role is Role.DEVELOPER:
                raise Crash("topples after the plan")
            if boundary == "in_qa" and role is Role.QA:
                raise Crash("topples during QA")
            return super().dispatch(role, prompt, state=state)

    d = Topples(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    if boundary == "after_freeze":
        # Normal up to the freeze, then topple
        ctrl_ok = Controller(store, FakeDispatcher(repo=repo, plan_json=plan_for,
                                                   qa_json=qa_pass), spec_path=spec)
        ctrl_ok.run_iteration(state)
        assert state.stage is Stage.CHECKPOINTED
        stages.transition(state, Stage.PLANNING)
        stages.transition(state, Stage.DEVELOPING)
        store.write_state(state)
    else:
        with pytest.raises(Crash):
            ctrl.run_iteration(state)

    # The state on disk is readable and not self-contradictory
    fresh = store.read_state()
    assert fresh.run_id == state.run_id

    # Resume: conservative recovery, no double start
    healthy = Controller(
        store, FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass),
        spec_path=spec,
    )
    stages.begin_iteration(fresh, reason="restart after crash")
    out = healthy.run_iteration(fresh)
    # Three outcomes are fine, one is not: carry on, block visibly, or reject
    # with a **named reason**. What would not be fine is a silent or
    # unexplained abort.
    #
    # Correction provenance (reviewer finding 1, 2026-09-07): the previous
    # version did not allow the reasoned rejection. It failed as soon as the
    # discrimination check became correct -- because the resumed run repeats
    # the very same plan and therefore no longer demonstrates an increment.
    assert (
        out.accepted
        or fresh.condition is Condition.BLOCKED
        or "not accepted" in (out.reason or "")
    ), (
        f"after the crash at boundary '{boundary}' the run has to carry on, "
        f"block visibly or reject with a reason: {out.reason}"
    )
    assert "no verdicts" not in (out.reason or ""), (
        "a rejection with verdicts present must not claim 'no verdicts': "
        f"{out.reason}"
    )


def test_A06_accepted_state_survives_a_crash(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)
    accepted = state.last_accepted_candidate.candidate_id

    # "Crash": a state freshly loaded from disk
    after_restart = store.read_state()
    assert after_restart.last_accepted_candidate.candidate_id == accepted
    assert after_restart.evidence_digest is not None
    store.read_evidence(expect_digest=after_restart.evidence_digest)


# =========================================================================== #
# A12 -- backup and restore
# =========================================================================== #


def test_A12_backup_can_be_restored(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    archive = store.backup()
    assert archive.exists() and archive.stat().st_size > 0

    target = store.restore(archive)
    restored = RunStore(store.root, target.name)
    restored_state = restored.read_state()

    assert restored_state.last_accepted_candidate.candidate_id == \
        state.last_accepted_candidate.candidate_id
    assert len(restored.receipts()) == len(store.receipts()), "receipts are backed up too"
    restored.read_evidence(expect_digest=restored_state.evidence_digest)


def test_A12_restore_does_not_overwrite_unasked(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)
    archive = store.backup()

    target = store.restore(archive)
    assert target.name != store.run_id, "the existing run stays untouched"


# =========================================================================== #
# §8 -- cost budget and honest disclosure
# =========================================================================== #


def test_cost_approval_is_not_invented():
    s = make_state()
    text = s.cost_disclosure()
    assert "No cost approval" in text
    assert "unknown" in text, "unknown usage is not booked as zero"
    assert "still incur cost" in text


def test_cost_approval_is_reproduced():
    s = make_state(budgets=Budgets(cost_budget_note="up to 20 Euro, captain 2026-09-07"))
    assert "20 Euro" in s.cost_disclosure()


def test_dispatch_budget_takes_effect(tmp_path, repo, spec):
    """`max_dispatches=2` buys two provider calls, and the third is refused.

    Correction provenance (2026-09-13, `tools/budget_evidence.py`): this used
    to assert only that *something* raised. It passed against an off-by-one --
    the counter was raised before the budget was consulted, so a ceiling of
    two bought **one** call and refused the second. The test could not see the
    difference because it never looked at how many calls got through. It does
    now, which is the number the benchmark's matched-budget premise rests on.
    """
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=5, max_dispatches=2)

    out = ctrl.run_iteration(state)

    assert [r.value for r in d.calls] == ["planner", "developer"]
    assert state.usage.dispatches == 2
    assert not out.accepted
    # The refusal is *named* in the outcome. A run that stops on its own
    # ceiling and reports "no QA verdict" without saying why reads like an
    # outage, and an outage is waited out.
    assert "dispatch budget exhausted (2/2)" in out.reason
    assert state.budget_exhausted() == "dispatch budget exhausted (2/2)"


def test_a_refused_dispatch_reaches_the_caller_where_nothing_handles_it(
        tmp_path, repo, spec):
    """QA's refusal becomes an unverified iteration; the developer's raises.

    Both are the same refusal at different points of the loop, and both are
    worth a test: the swallowed one must still carry its reason (above), and
    the raised one must say "budget", not "provider".
    """
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=5, max_dispatches=1)

    from hoh.controller import DispatchError

    with pytest.raises(DispatchError, match="dispatch budget exhausted"):
        ctrl.run_iteration(state)
    assert [r.value for r in d.calls] == ["planner"]
    assert state.usage.dispatches == 1


def test_wallclock_budget_takes_effect(tmp_path, repo, spec):
    """The runtime limit computes with the wall clock, not with `monotonic()`.

    Correction provenance (reviewer finding 7, 2026-09-07): formerly
    `started_monotonic` held a `time.monotonic()` value that was
    **persisted**. That clock counts from boot. After a restart the difference
    yielded a negative value -- in the real a02 state `6762 - 82007 = -75245`
    -- and the limit could never trigger again, silently and without a
    message. The test set the value in-process and could not see that; it
    therefore now checks with a persistable timestamp.
    """
    from datetime import datetime, timedelta, timezone

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=5, max_wallclock_seconds=1)
    earlier = datetime.now(timezone.utc) - timedelta(seconds=5)
    state.started_at_wall = earlier.strftime("%Y-%m-%dT%H:%M:%SZ")

    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert "wallclock budget" in (state.blocked_reason or "")

    # And the core of the finding: the value survives a serialization without
    # losing its meaning. `monotonic()` did not do that.
    reloaded = RunState.model_validate_json(state.model_dump_json())
    assert "wallclock budget" in (reloaded.budget_exhausted() or ""), (
        "the limit has to take effect even after a restart"
    )


# =========================================================================== #
# A03 -- the planner gets a copy of its own, not the live tree
# =========================================================================== #


def test_A03_planner_does_not_work_in_the_live_tree(tmp_path, repo, spec):
    """Handoff §5 rejects prompt-only: 'writing read-only into a prompt is not
    enforcement when a shell is freely available.'"""
    seen: dict[str, str] = {}

    class Remembers(FakeDispatcher):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.role_cwd: dict[Role, str] = {}

        def dispatch(self, role, prompt, *, state):
            seen[role.value] = self.role_cwd.get(role, "(live)")
            return super().dispatch(role, prompt, state=state)

    d = Remembers(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    assert seen["planner"] != "(live)", "the planner gets a copy of its own"
    assert str(repo) not in seen["planner"]
    assert seen["qa"] != "(live)", "QA verifies in the arena"
    assert seen["developer"] == "(live)", "the developer is the only writer"


# --------------------------------------------------------------------------- #
# A02 finding 6: wrapper deadline shorter than the requested Herdr deadline
# --------------------------------------------------------------------------- #


def test_wrapper_deadline_exceeds_the_herdr_deadline():
    """The subprocess has to wait longer than the command it waits for.

    Real operational defect from the A02 run: `_cli` had a fixed timeout of
    180 s but passed `--timeout 900000` to Herdr. The developer step died after
    three minutes on a deadline nobody had requested, and the run got blocked.
    """
    from hoh.dispatchers import HerdrDispatcher as H

    args = ["herdr", "agent", "prompt", "target", "text", "--wait", "--timeout", "900000"]
    deadline = H._wrapper_timeout(args)
    assert deadline > 900.0, "the wrapper deadline has to exceed the Herdr deadline"
    assert deadline == 900.0 + H.WRAPPER_OVERHEAD_S

    # Agent start: 240 s requested -> the wrapper has to wait longer as well
    start = ["herdr", "agent", "start", "n", "--kind", "claude", "--timeout", "240000"]
    assert H._wrapper_timeout(start) == 240.0 + H.WRAPPER_OVERHEAD_S

    # Without --timeout the minimum stays
    assert H._wrapper_timeout(["herdr", "api", "snapshot"]) == 180.0
    # A broken --timeout falls back to the minimum instead of raising
    assert H._wrapper_timeout(["herdr", "x", "--timeout", "abc"]) == 180.0
    assert H._wrapper_timeout(["herdr", "x", "--timeout"]) == 180.0


# --------------------------------------------------------------------------- #
# A02 finding 7: a QA outage was reported as a QA verdict
# --------------------------------------------------------------------------- #


def test_qa_outage_is_not_a_qa_verdict():
    """A QA agent that dropped out must not sound like one that verified.

    Real operational defect from the A02 run: the QA agent ran out of quota in
    the middle of iteration 2, wrote no answer, the dispatch failed -- and the
    controller discarded the reason. The run reported "not accepted: K1..K9",
    as if QA had checked all nine criteria and acknowledged none. In truth not
    a single one had been assessed.

    That is the mirror image of "a test that was not executed counts as
    passed": a statement about a verification that never happened.
    """
    from hoh.contracts import AcceptanceCheck, Outcome

    ctrl = object.__new__(Controller)

    checks = [
        AcceptanceCheck(check_id=f"K{i}", description=f"criterion {i}",
                        command="true", expect_exit=0)
        for i in (1, 2)
    ]
    plan = DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="b", spec_digest="d",
        objective="z", targets=["t"], acceptance_checks=checks,
    )
    runner = {
        c.check_id: CheckVerdict(check_id=c.check_id, outcome=Outcome.PASS,
                                 receipt_id=f"r-{c.check_id}", note="Runner: exit 0")
        for c in checks
    }

    verdicts, gaps, summary = Controller._parse_qa(
        ctrl, "", plan=plan, receipts=[], state=None, candidate=None,
        runner_verdicts=runner, checked=checks, blind={},
        outage="qa dispatch failed: usage limit reached",
    )

    # No criterion turns green -- the runner may refute, never confirm.
    assert all(v.outcome is not Outcome.PASS for v in verdicts)
    # But the reason is preserved and names the outage, not a verdict.
    assert "usage limit" in summary
    assert summary.startswith(NO_QA_VERDICT)
    assert any("usage limit" in (v.note or "") for v in verdicts)
    assert any("did not answer" in g for g in gaps)

    # And without an outage the old, correct wording stays in place.
    _, _, s2 = Controller._parse_qa(
        ctrl, "", plan=plan, receipts=[], state=None, candidate=None,
        runner_verdicts=runner, checked=checks, blind={}, outage="",
    )
    assert not s2.startswith(NO_QA_VERDICT)


def test_qa_outage_does_not_count_as_a_failed_attempt():
    """A quota outage must not consume the progress budget.

    In the A02 run the QA outage consumed one of three "without progress"
    slots and one of four iterations. The run would have died on a quota
    problem and would have looked as if the work was not making headway on
    the merits.
    """
    from hoh import stages

    def _state():
        return make_state(stage=Stage.VERIFYING, iteration=1)

    # A genuine rejection on the merits: it counts.
    s = _state()
    before = s.usage.loops_without_progress
    stages.reject_candidate(s, "not accepted: K1, K2")
    assert s.usage.loops_without_progress == before + 1

    # Outage without a verdict: it does not count.
    s = _state()
    before = s.usage.loops_without_progress
    stages.reject_candidate(
        s, f"{stages.UNVERIFIED}: {NO_QA_VERDICT} -- usage limit")
    assert s.usage.loops_without_progress == before, (
        "an outage is not a failed attempt"
    )
    # The finding is in the log nonetheless.
    assert any("unverified" in n for n in s.history)


# --------------------------------------------------------------------------- #
# A02 finding 8: the cause of a block was cleaned away with the tab
# --------------------------------------------------------------------------- #


def test_block_reads_the_dialog(monkeypatch):
    """On `blocked` the visible dialog has to go into the message.

    Twice an A02 run came to a halt at an approval, and both times it was
    afterwards not determinable which one: the error path closed the tabs, and
    the pane content was gone. A blocker without a cause is not a finding.
    """
    from hoh import dispatchers, herdr as herdr_mod
    from hoh.controller import DispatchError

    d = object.__new__(dispatchers.HerdrDispatcher)
    d.answers_dir = Path("/tmp")
    d.profiles = {}
    d.endpoints = {}
    d.role_cwd = {}
    d.timeout_ms = 1000

    monkeypatch.setattr(d, "_ensure_agent", lambda role, state: "target", raising=False)
    monkeypatch.setattr(
        d, "_cli",
        lambda args, purpose: {"result": {"agent": {
            "pane_id": "wW:pS", "workspace_id": "wW", "tab_id": "wW:tX",
            "agent": "claude", "agent_status": "blocked", "cwd": "/tmp",
        }}},
        raising=False,
    )
    monkeypatch.setattr(
        herdr_mod, "read_agent",
        lambda target, lines=120: "Do you trust the files in this folder?\n1. Yes  2. No",
    )

    st = make_state(iteration=1)
    with pytest.raises(DispatchError) as exc:
        d.dispatch(Role.PLANNER, "prompt", state=st)

    text = str(exc.value)
    assert "wW:pS" in text
    assert "Do you trust the files in this folder?" in text, (
        "the visible dialog has to be in the message"
    )

    # And an unreadable pane must not tip the diagnosis into a second error.
    def _broken(target, lines=120):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(herdr_mod, "read_agent", _broken)
    with pytest.raises(DispatchError) as exc2:
        d.dispatch(Role.PLANNER, "prompt", state=st)
    assert "not readable" in str(exc2.value)


def test_dispatch_failure_carries_the_pane_view(monkeypatch):
    """A timeout takes the pane content with it too, not just a block.

    "timed out waiting for agent status" says nothing by itself: behind it
    there can be an exhausted quota, a crash, a question back, or genuine
    compute time. Three times an A02 run failed on a cause that was no longer
    determinable afterwards.
    """
    from hoh import dispatchers, herdr as herdr_mod
    from hoh.controller import DispatchError

    d = object.__new__(dispatchers.HerdrDispatcher)
    d.answers_dir = Path("/tmp")
    d.profiles = {}
    d.endpoints = {}
    d.role_cwd = {}
    d.timeout_ms = 1000

    def _timeout(args, purpose):
        raise DispatchError(f'{purpose} failed: {{"error":{{"code":"timeout"}}}}')

    monkeypatch.setattr(d, "_ensure_agent", lambda role, state: "target", raising=False)
    monkeypatch.setattr(d, "_cli", _timeout, raising=False)
    monkeypatch.setattr(
        herdr_mod, "read_agent",
        lambda target, lines=120: "Claude usage limit reached. Your limit will reset at 5pm.",
    )

    with pytest.raises(DispatchError) as exc:
        d.dispatch(Role.DEVELOPER, "prompt", state=make_state(iteration=1))

    text = str(exc.value)
    assert "timeout" in text, "the original error is preserved"
    assert "usage limit reached" in text, "the cause from the pane has to be added"


# --------------------------------------------------------------------------- #
# A02 finding 9: the dual review gate ran twice
# --------------------------------------------------------------------------- #


def test_roles_do_not_start_reviewers_of_their_own():
    """Every role has to know that the review gate lies outside.

    Real operational defect from the A02 run: the developer is a Claude
    session on this machine, so it loads the global house rules -- and those
    demand two independent reviewers before every commit. It obeyed correctly
    and waited for "reviewer 2", 9+ minutes and 94k tokens for a calculator
    task. HoH's independent QA *is* that gate, one level up; the review thus
    ran twice and blew the time limit.
    """
    from hoh import roles

    block = roles._HOUSE_RULES_POINTER
    assert "do not start review subagents of your own" in block
    assert "already satisfied" in block
    # The resolution has to be grounded in the house rules themselves, not
    # stand against them -- otherwise it is a silent rule violation.
    assert "concrete, current instruction" in block
    # The hard prohibitions are untouched by that.
    assert "nvidia-smi" in block
    assert "rm -rf" in block

    # And the pointer really has to arrive in the role prompts.
    plan = DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="b", spec_digest="d",
        objective="z", targets=["t"],
        acceptance_checks=[AcceptanceCheck(check_id="K1", description="d",
                                           command="true", expect_exit=0)],
    )
    dev = roles.developer_prompt(
        iteration=1, attempt=1, spec_text="s", plan=plan,
        workspace="/tmp/x", warm_start=False,
        evidence=EvidenceBundle(run_id="r", iteration=1),
    )
    assert "do not start review subagents of your own" in dev


# --------------------------------------------------------------------------- #
# A02 finding 10: preservation criteria were treated like failures
# --------------------------------------------------------------------------- #


def test_preservation_criteria_do_not_block_the_acceptance(tmp_path, repo, spec):
    """A proven increment with intact regression protection is an acceptance.

    Real operational defect: two complete A02 loops ran through without error,
    QA proved the increment (five criteria red on the base state, green on the
    candidate) -- and HoH rejected anyway, because three further criteria were
    green on both states. But that is precisely the job of a preservation
    criterion.

    The rule now reads: everything PASS **and** at least one discriminating.
    """
    from hoh.contracts import Outcome, RoleResult

    def _v(cid, *, disc):
        return CheckVerdict(check_id=cid, outcome=Outcome.PASS,
                            receipt_id=f"r-{cid}", discriminates=disc)

    def _res(*verdicts):
        return RoleResult(
            run_id="r", iteration=1, attempt=1, role=Role.QA,
            candidate_id="c", plan_digest="p", spec_digest="s",
            verdicts=list(verdicts),
        )

    # The real A02 case: five discriminating, three preserving -- acceptance.
    mixed = _res(
        *[_v(f"K{i}", disc=True) for i in (2, 3, 4, 5, 8)],
        *[_v(f"K{i}", disc=False) for i in (1, 6, 7)],
    )
    assert mixed.accepted(), (
        "a proven increment plus holding regression protection has to be accepted"
    )

    # The abuse case the rule was originally built against: nothing but
    # criteria that were already green before -- no acceptance.
    vacuous = _res(*[_v(f"K{i}", disc=False) for i in (1, 2, 3)])
    assert not vacuous.accepted(), (
        "a plan that measures nothing must not be able to plan itself green"
    )

    # And a single red criterion still overturns the acceptance.
    with_failure = _res(
        _v("K1", disc=True),
        CheckVerdict(check_id="K2", outcome=Outcome.FAIL),
    )
    assert not with_failure.accepted()


# --------------------------------------------------------------------------- #
# A02 finding 11: Herdr's session restore collided with HoH's agent start
# --------------------------------------------------------------------------- #


def _dispatcher_with_store(tmp_path):
    from hoh import dispatchers

    d = object.__new__(dispatchers.HerdrDispatcher)
    d.answers_dir = tmp_path / "runs" / "a02" / "answers"
    d.answers_dir.mkdir(parents=True, exist_ok=True)
    d.store_dir = d.answers_dir.parent
    d.arenas_dir = d.store_dir.parent / "_arenas" / d.store_dir.name
    d.profiles = {Role.PLANNER: "claude"}
    d.endpoints = {}
    d.agents = {}
    d.role_cwd = {}
    d.own_tabs = []
    d.own_panes = []
    # Modell und Aufwandsstufe je Rolle. Leer heisst "was der Harness
    # vorgibt" — genau das, was HoH bis 2026-09-08 ausschliesslich tat.
    d.models = {}
    d.efforts = {}
    d.timeout_ms = 1000
    return d


def test_restored_agent_is_adopted(tmp_path, monkeypatch):
    """After a restart HoH may find its own agent again.

    Real operational defect: after a hard reset Herdr had restored the session
    including its panes -- Herdr's own capability. HoH nevertheless blindly
    started a new agent and ran into `agent_name_taken`.
    """
    from hoh import herdr as herdr_mod

    d = _dispatcher_with_store(tmp_path)
    st = make_state(run_id="a02", iteration=6)
    name = d._agent_name(Role.PLANNER, st)

    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wW:p11", "workspace_id": "wW", "tab_id": "wW:tV",
        "agent": "claude", "agent_status": "idle", "cwd": str(d.store_dir),
    }]})
    # No tab, no start: both would raise here.
    monkeypatch.setattr(d, "_new_tab_pane", lambda *a, **k: pytest.fail("new tab"), raising=False)
    monkeypatch.setattr(d, "_start_agent", lambda *a: pytest.fail("restart"), raising=False)

    assert d._ensure_agent(Role.PLANNER, st) == name
    assert d.endpoints[Role.PLANNER] == "herdr:wW/wW:tV/wW:p11"
    assert "wW:tV" in d.own_tabs, "the adopted tab gets cleaned up with the rest"


def test_foreign_agent_with_the_same_name_is_not_adopted(tmp_path, monkeypatch):
    """The boundary: same name, but a different directory -- no access.

    That is the lesson from the `find_agent` reuse removed earlier, which had
    taken over a running research session of the captain's.
    """
    from hoh import herdr as herdr_mod
    from hoh.controller import DispatchError

    d = _dispatcher_with_store(tmp_path)
    st = make_state(run_id="a02", iteration=6)
    name = d._agent_name(Role.PLANNER, st)

    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wZ:p1", "workspace_id": "wZ", "tab_id": "wZ:t1",
        "agent": "claude", "agent_status": "idle",
        "cwd": "/home/someone/other-project",
    }]})
    with pytest.raises(DispatchError, match="does not adopt a foreign session"):
        d._ensure_agent(Role.PLANNER, st)


def test_working_agent_is_not_overwritten(tmp_path, monkeypatch):
    """A running worker is attached to, never overwritten (handoff §7)."""
    from hoh import herdr as herdr_mod
    from hoh.controller import DispatchError

    d = _dispatcher_with_store(tmp_path)
    st = make_state(run_id="a02", iteration=6)
    name = d._agent_name(Role.PLANNER, st)

    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wW:p11", "workspace_id": "wW", "tab_id": "wW:tV",
        "agent": "claude", "agent_status": "working", "cwd": str(d.store_dir),
    }]})
    with pytest.raises(DispatchError, match="is still working"):
        d._ensure_agent(Role.PLANNER, st)


def test_dead_restored_agent_is_cleaned_up(tmp_path, monkeypatch):
    """`unknown` proves no liveness -- our own dead tab gets cleaned up.

    Continuation of finding 11: after the hard reset Herdr had restored the
    pane and the name binding, but the agent process inside it was dead. The
    start failed on `agent_name_taken`, the prompting afterwards on
    `agent_not_ready`, and the pane was empty. Reuse on suspicion leads into
    this dead end; the way out is to clean up **our own** tab and start fresh.
    """
    from hoh import herdr as herdr_mod

    d = _dispatcher_with_store(tmp_path)
    st = make_state(run_id="a02", iteration=6)
    name = d._agent_name(Role.PLANNER, st)

    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wW:p11", "workspace_id": "wW", "tab_id": "wW:tV",
        "agent": "claude", "agent_status": "unknown", "cwd": str(d.store_dir),
    }]})

    closed: list[str] = []
    monkeypatch.setattr(
        d, "_cli",
        lambda args, purpose: closed.append(args[-1]) or {"result": {}},
        raising=False,
    )
    started: list[str] = []
    monkeypatch.setattr(d, "_new_tab_pane", lambda *a, **k: "wW:pNEW", raising=False)
    monkeypatch.setattr(d, "_start_agent", lambda n, k, p, extra=None: started.append(n), raising=False)

    assert d._ensure_agent(Role.PLANNER, st) == name
    assert closed == ["wW:tV"], "our own dead tab gets closed"
    assert started == [name], "afterwards a fresh start happens"


def test_blocked_restored_agent_is_not_cleaned_up(tmp_path, monkeypatch):
    """Counter-check: `blocked` waits for an approval and must not be removed."""
    from hoh import herdr as herdr_mod
    from hoh.controller import DispatchError

    d = _dispatcher_with_store(tmp_path)
    st = make_state(run_id="a02", iteration=6)
    name = d._agent_name(Role.PLANNER, st)
    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wW:p11", "workspace_id": "wW", "tab_id": "wW:tV",
        "agent": "claude", "agent_status": "blocked", "cwd": str(d.store_dir),
    }]})
    with pytest.raises(DispatchError, match="waits for an approval"):
        d._ensure_agent(Role.PLANNER, st)


def test_developer_in_its_own_worktree_counts_as_ours(tmp_path, monkeypatch):
    """The ownership test has to know both locations of a run.

    False positive of the guard from finding 11: the developer sits in the
    authorized working tree (`state.repo_path`), not in the run directory. The
    first version knew only the run directory and rejected our own developer as
    a "foreign session". An ownership test that does not know half of its own
    roles is no protection but a malfunction.
    """
    from hoh import herdr as herdr_mod

    d = _dispatcher_with_store(tmp_path)
    d.profiles[Role.DEVELOPER] = "claude"
    worktree = tmp_path / "worktrees" / "hoh-a02"
    worktree.mkdir(parents=True)
    st = make_state(run_id="a02", iteration=6, repo_path=str(worktree))
    name = d._agent_name(Role.DEVELOPER, st)

    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wW:p9", "workspace_id": "wW", "tab_id": "wW:tD",
        "agent": "claude", "agent_status": "idle", "cwd": str(worktree),
    }]})
    monkeypatch.setattr(d, "_new_tab_pane", lambda *a, **k: pytest.fail("new tab"), raising=False)
    monkeypatch.setattr(d, "_start_agent", lambda *a: pytest.fail("restart"), raising=False)

    assert d._ensure_agent(Role.DEVELOPER, st) == name
    assert d.endpoints[Role.DEVELOPER] == "herdr:wW/wW:tD/wW:p9"

    # The boundary still holds: a path outside both locations gets rejected.
    monkeypatch.setattr(herdr_mod, "snapshot", lambda: {"agents": [{
        "name": name, "pane_id": "wZ:p1", "workspace_id": "wZ", "tab_id": "wZ:t1",
        "agent": "claude", "agent_status": "idle",
        "cwd": "/home/someone/other-project",
    }]})
    d.agents.pop(Role.DEVELOPER, None)
    from hoh.controller import DispatchError
    with pytest.raises(DispatchError, match="does not adopt a foreign session"):
        d._ensure_agent(Role.DEVELOPER, st)


# --------------------------------------------------------------------------- #
# K1: cache-friendly role prompts (handoff §12) -- finding 14
# --------------------------------------------------------------------------- #


def test_role_prompts_have_a_stable_prefix():
    """The reusable part comes first, the run metadata last.

    Handoff §12 demands cache-friendly inputs independently of the backend:
    "do **not** write changing run metadata in front of the reusable prefix".
    The first version did exactly that -- `planner_prompt` began with
    "... for iteration {iteration}" in line 2, whereby the common prefix ended
    after one line and policy, house rules and output contract had to be
    processed anew on every role run.

    What is verified is the property, not the wording: two calls with different
    run data have to have a long common beginning.
    """
    from hoh import roles
    from hoh.evidence import EvidenceBundle

    def _plan(i):
        return DevelopmentPlan(
            run_id="r", iteration=i, base_candidate_id=f"c{i}", spec_digest="d",
            objective=f"goal {i}", targets=[f"t{i}"],
            acceptance_checks=[AcceptanceCheck(check_id="K1", description=f"d{i}",
                                               command="true", expect_exit=0)],
        )

    def common_prefix(a: str, b: str) -> int:
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    cases = {
        "planner": [
            roles.planner_prompt(
                iteration=i, spec_text=f"Spec {i}", evidence=EvidenceBundle(run_id="r", iteration=i),
                repo_path=f"/tmp/p{i}", base_candidate_id=f"c{i}", spec_digest="d", run_id="r",
            )
            for i in (1, 7)
        ],
        "developer": [
            roles.developer_prompt(
                iteration=i, attempt=i, spec_text=f"Spec {i}", plan=_plan(i),
                workspace=f"/tmp/w{i}", warm_start=bool(i % 2),
                evidence=EvidenceBundle(run_id="r", iteration=i),
            )
            for i in (1, 7)
        ],
        "qa": [
            roles.qa_prompt(
                iteration=i, spec_text=f"Spec {i}", plan=_plan(i),
                candidate_path=f"arena/i{i}", candidate_binding=f"bind{i}",
                receipts_summary=f"receipts {i}",
            )
            for i in (1, 7)
        ],
    }

    for role, (a, b) in cases.items():
        common = common_prefix(a, b)
        share = common / min(len(a), len(b))
        assert common > 1500, (
            f"{role}: the common prefix is only {common} characters long -- "
            "variable run data sits too far in front and destroys the "
            "reuse"
        )
        assert share > 0.45, (
            f"{role}: only {share:.0%} of the prompt is reusable"
        )
        # The house rules belong in the stable part -- they never change.
        assert "nvidia-smi" in a[:common], f"{role}: house rules not in the prefix"


# --------------------------------------------------------------------------- #
# A10/finding 15: an accepted checkpoint was not durable
# --------------------------------------------------------------------------- #


def _worktree_for(repo, tmp_path, name="hoh-work"):
    """A real linked worktree on a branch of its own.

    Since the reviewer finding, `commit_candidate` refuses the main checkout
    and every integration branch -- exactly the case the old fixture produced.
    The test therefore has to use the same shape as production.
    """
    import subprocess

    target = tmp_path / name
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", name, str(target)],
                   capture_output=True, check=True)
    return target


def test_accepted_candidate_is_committed(tmp_path, repo, spec):
    """Acceptance makes the state durable, not just the run state green.

    Real finding from a02: after six iterations and two acceptances the
    worktree carried exactly one commit ("initial state") and two modified
    files. The accepted checkpoint lived only as a dirty working tree -- a
    `git checkout` would have destroyed it, and `hoh deliver` consequently
    refused with "working tree is not clean".
    """
    from hoh.workspace import head_commit, is_clean

    wt = _worktree_for(repo, tmp_path)
    d = FakeDispatcher(repo=wt, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, wt, spec, d)
    before = head_commit(wt)

    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason

    after = head_commit(wt)
    assert after != before, "the acceptance has to produce a commit"
    assert is_clean(wt), "after the acceptance the working tree is clean"
    assert any("committed as" in h for h in state.history)


def test_commit_covers_only_what_was_verified(tmp_path, repo):
    """Nothing may have crept in between verification and commit."""
    from hoh.workspace import WorkspaceError, commit_candidate, snapshot

    wt = _worktree_for(repo, tmp_path, "hoh-verify")
    candidate = snapshot(wt, "c1")
    (Path(wt) / "smuggled_in.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="came in between"):
        commit_candidate(wt, candidate, run_id="r", iteration=1)


def test_commit_on_a_clean_tree_has_no_effect(tmp_path, repo):
    """Nothing to commit means: no empty commit, no error."""
    from hoh.workspace import commit_candidate, head_commit, snapshot

    import subprocess

    wt = _worktree_for(repo, tmp_path, "hoh-clean")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-m", "everything in"],
                   capture_output=True)
    candidate2 = snapshot(wt, "c2")
    sha = commit_candidate(wt, candidate2, run_id="r", iteration=1)
    assert sha == head_commit(wt)


def test_delivery_explains_a_checked_out_target_branch(tmp_path):
    """A target branch checked out elsewhere needs a usable answer.

    Real A10 finding: `git push . <branch>:<target>` fails when `<target>` is
    checked out in another worktree -- git protects index and working tree
    there. HoH passed the raw git message through ("refusing to update
    checked out branch"), which is correct but unusable in production: it says
    neither where the branch is nor what to do about it.

    `update-ref` would be the way around it, and that is exactly why it is
    wrong: HoH would change a checkout that does not belong to it.
    """
    import subprocess

    from hoh.delivery import DeliveryRefused, deliver

    main_repo = tmp_path / "main"
    main_repo.mkdir()
    def g(*a, cwd=main_repo):
        return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)

    g("init", "-q", "-b", "master")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (main_repo / "a.txt").write_text("1\n")
    g("add", "-A"); g("commit", "-q", "-m", "base")

    wt = tmp_path / "wt"
    g("worktree", "add", "-q", "-b", "work", str(wt))
    (wt / "a.txt").write_text("2\n")
    g("add", "-A", cwd=wt); g("commit", "-q", "-m", "work", cwd=wt)

    with pytest.raises(DeliveryRefused) as exc:
        deliver(repo=wt, mode="local-only", yolo="off", approved=True,
                target_branch="master", candidate_id="c1")

    text = str(exc.value)
    assert "checked out in" in text
    assert str(main_repo) in text, "the owning worktree has to be named"
    assert "merge --ff-only" in text, "the instruction for action has to come with it"

    # Counter-check: a branch that is not checked out can be delivered.
    g("branch", "acceptance", "master")
    result = deliver(repo=wt, mode="local-only", yolo="off", approved=True,
                     target_branch="acceptance", candidate_id="c1")
    assert result.delivered
    assert g("rev-parse", "acceptance").stdout.strip() == \
           g("rev-parse", "work").stdout.strip()


# --------------------------------------------------------------------------- #
# A03: role and permission boundaries -- negative test (was NOT_RUN)
# --------------------------------------------------------------------------- #


def test_A03_planner_does_not_reach_the_live_tree(tmp_path, repo, spec):
    """The planner works on a copy, not on the original.

    §5 explicitly rejects prompt-only: "writing read-only into a prompt is not
    enforcement when a shell is freely available". The acceptance report listed
    this negative test as NOT_RUN.

    What is verified is the enforcement, not the intention: the working
    directory the planner gets must not be the live tree, and whatever it does
    in there must not reach the live tree.
    """
    seen: dict = {}

    class SnoopDispatcher(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.PLANNER:
                seen["cwd"] = self.role_cwd.get(Role.PLANNER)
                seen["prompt"] = prompt
                # The planner does what the prompt says it may not do.
                if seen["cwd"]:
                    (Path(seen["cwd"]) / "planner_has_written.txt").write_text(
                        "trespass\n", encoding="utf-8"
                    )
            return super().dispatch(role, prompt, state=state)

    d = SnoopDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    d.role_cwd = {}
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    # 1. The planner got a directory of its own at all.
    assert seen.get("cwd"), "the planner got no working directory of its own"

    # 2. And not the live tree.
    assert Path(seen["cwd"]).resolve() != Path(repo).resolve(), (
        "the planner sat in the live tree -- per §5 prompt-only is not enforcement"
    )

    # 3. Its trespass did not arrive in the live tree.
    assert not (Path(repo) / "planner_has_written.txt").exists(), (
        "the planner was able to write into the live tree"
    )

    # 4. The prompt says so on top of that -- belt and suspenders.
    assert "you may not write there" in seen["prompt"]


# =========================================================================== #
# Adversarial dual review 2026-09-07 -- both reviewers, proven attacks
# =========================================================================== #


def test_reviewer_preservation_criterion_is_frozen(tmp_path, repo, spec):
    """The planner cannot redefine a validated requirement.

    Proven independently by **both** reviewers: the plan names `K1` again, sets
    `command` to `true`, and the regression protection is gone -- permanently,
    because `_record_preserved` wrote the watered-down version back. Since the
    check_id is not new, it moreover never ran against the base. A real
    regression went through as accepted that way.
    """
    step = {"n": 0}

    def plan(state):
        step["n"] += 1
        if step["n"] == 1:
            return plan_for(
                state, check_id="K1",
                command=f"test -f {FakeDispatcher.MARKER}",
            )
        # Second round: the same K1, but disarmed.
        return plan_for(state, check_id="K1", command="true")

    d = FakeDispatcher(repo=repo, plan_json=plan, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    ctrl.run_iteration(state)
    assert store.read_checks()["K1"].command == f"test -f {FakeDispatcher.MARKER}"

    ctrl.run_iteration(state)

    suite = store.read_checks()
    assert suite["K1"].command == f"test -f {FakeDispatcher.MARKER}", (
        "the validated version has to survive -- 'true' would let every "
        "regression through"
    )
    assert any("protected against redefinition" in h for h in state.history), (
        "the attempt has to be visible, not silently discarded"
    )


@pytest.mark.parametrize("command", [
    "cd ..",
    "(cd .. && ls)",
    "{ cd ..; ls; }",
    "x=1 cd ..",
    'eval "cd .."',
    "bash -c 'cd ..'",
    "printf '{}' > ../../checks.json",
    'cat "../../state.json"',
    "ls //home/someone/.ssh",
    "test -f /etc/passwd",
])
def test_reviewer_arena_escape_is_rejected(command):
    """Every circumvention of the arena boundary proven by the reviewer.

    The worst case was `printf '{}' > ../../checks.json`: the arena lay under
    `runs/<id>/arena/`, so the preservation suite was two levels above it. A
    criterion emptied that suite from inside the object under verification; the
    next iteration ran without regression protection and accepted the
    regression.
    """
    from hoh.runner import ArenaEscape, assert_stays_in_arena

    with pytest.raises(ArenaEscape):
        assert_stays_in_arena(command)


@pytest.mark.parametrize("command", [
    "python3 -m pytest -q",
    f"test -f {FakeDispatcher.MARKER}",
    "ls {ARENA}/src",
    "grep -r pattern .",
    'python3 -c "import app; assert app.add(2,3)==5"',
    "/usr/bin/python3 -V",
])
def test_reviewer_arena_boundary_lets_legitimate_commands_through(command):
    """The counter-check: the guard must not choke off normal criteria."""
    from hoh.runner import assert_stays_in_arena

    assert_stays_in_arena(command)   # does not raise


def test_reviewer_arena_does_not_lie_in_the_run_directory(tmp_path):
    """Structural second line: above an arena there are only arenas."""
    from hoh.store import RunStore

    s = RunStore(tmp_path / "runs", "r1")
    assert not str(s.arenas_dir).startswith(str(s.dir) + "/"), (
        "if the arena lay in the run directory, receipts, state and the "
        "preservation suite would be reachable via '..'"
    )


def test_reviewer_commit_refuses_a_foreign_checkout(tmp_path, repo):
    """`commit_candidate` never writes into the captain's live checkout.

    The reviewer reproduced that the first version committed onto `main` and
    took the captain's unfinished work along via `git add -A`.
    """
    from hoh.workspace import WorkspaceError, commit_candidate, snapshot

    # `repo` is the main checkout on an integration branch.
    (Path(repo) / "unfinished.py").write_text("# the captain's work\n", encoding="utf-8")
    candidate = snapshot(repo, "c1")

    with pytest.raises(WorkspaceError) as exc:
        commit_candidate(repo, candidate, run_id="r", iteration=1)
    text = str(exc.value)
    assert "protected branch" in text or "main checkout" in text
    assert (Path(repo) / "unfinished.py").exists(), "foreign work stays untouched"


def test_reviewer_arenaescape_does_not_blow_up_the_iteration(tmp_path, repo, spec):
    """An escaping criterion is rejected, not turned into an exception.

    Formerly the check path caught only `HouseRuleViolation`; `ArenaEscape`
    flew out of `run_iteration` as an uncaught exception, and the run was left
    standing in VERIFYING/ACTIVE without a verdict and without a block.
    """
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="cd .. && ls"),
        qa_json=qa_pass,
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)          # must not raise
    assert not out.accepted
    v = next(v for v in out.verdicts if v.check_id == "K1")
    assert v.outcome is not Outcome.PASS


def test_reviewer_backtick_nvidia_smi_is_blocked():
    """The backtick form was a real hole in the machine-wide guard.

    The anchor knew only `$( )`. ``nvidia-smi`` came through -- and the same
    pattern file is the denylist for Claude, Codex, Kimi and Pi.

    (The line-break variants additionally reported by the reviewer were a test
    artifact: he checked with Python `re` without MULTILINE, whereas the guard
    uses `grep -E` and works line by line. They are blocked.)
    """
    from hoh.runner import HouseRuleViolation, assert_command_allowed

    for form in ("`nvidia-smi`", "X=`nvidia-smi -L`"):
        with pytest.raises(HouseRuleViolation):
            assert_command_allowed(form)

    # Allow cases stay untouched -- the more important part.
    for harmless in ("grep -rn nvidia-smi src/", "echo 'see nvidia-smi docs'"):
        assert_command_allowed(harmless)


# =========================================================================== #
# Second round of the reviewer findings -- evidence for the corrections
# =========================================================================== #


def test_control_run_is_not_recognizable_from_the_path(tmp_path, repo, spec):
    """A criterion must not be able to tell control and treatment apart.

    The worst finding of reviewer 2: the control arena was named `iN-aM-basis`,
    and `case "$PWD" in *-basis) exit 1 ;; *) exit 0 ;; esac` was enough to get
    an iteration accepted **without any change at all**. The mechanism that is
    supposed to prove that something has changed was blind against exactly the
    attacker it was built against.
    """
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.arena_secret = "secret"

    candidate = ctrl._arena(state)
    base = ctrl._arena(state, suffix="basis")
    planner = ctrl._arena(state, suffix="planner")

    for a in (candidate, base, planner):
        assert "basis" not in a.name and "planner" not in a.name, (
            f"the directory name {a.name!r} gives away the role of the run"
        )
        assert str(state.iteration) not in a.name or len(a.name) == 12
    assert len({candidate.name, base.name, planner.name}) == 3, "unique nonetheless"

    # Stable: the same run yields the same name (repeatable runs).
    assert ctrl._arena(state, suffix="basis").name.split(".v")[0] == base.name


def test_parked_state_is_never_overwritten(tmp_path):
    """On a name collision the versioning continues right away.

    The `except OSError` branch formerly wrote over an existing parked state
    with `write_bytes` -- a silent loss exactly where "nothing gets deleted"
    is promised.
    """
    from hoh.store import RunStore

    s = RunStore(tmp_path / "runs", "r1")
    s.ensure()
    target = s.dir / "probe.json"
    contents = ["first", "second", "third", "fourth"]
    for content in contents:
        # Like every real writer of this store: replace atomically, do not
        # write in place. `_park` creates a hardlink; an in-place write would
        # change the parked state along with it, because it shares the same
        # inode.
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        import os as _os
        _os.replace(tmp, target)
        s._park(target)

    parked = sorted(s.dir.glob("probe.json.v*"))
    found = {p.read_text(encoding="utf-8") for p in parked}
    assert set(contents) <= found, (
        f"every state has to be preserved; found: {sorted(found)}"
    )
    assert len(parked) == len(contents), (
        "four park operations, four states -- nothing overwritten"
    )


def test_ownership_test_has_a_path_boundary():
    """`startswith` also hit sibling directories."""
    from hoh.dispatchers import HerdrDispatcher as H

    roots = ["/home/someone/hoh/runs/a02"]
    assert H._is_under("/home/someone/hoh/runs/a02/arena/x", roots)
    assert H._is_under("/home/someone/hoh/runs/a02", roots)
    for foreign in ("/home/someone/hoh/runs/a02-other/x", "/home/someone/hoh/runs/a020",
                    "/home/someone/hoh-lookalike", "/tmp/other"):
        assert not H._is_under(foreign, roots), f"{foreign} is not our own"


def test_delivery_binds_to_the_verified_content(tmp_path):
    """What gets delivered is what was verified -- not what happens to sit there."""
    import subprocess

    from hoh.delivery import DeliveryRefused, deliver

    main_repo = tmp_path / "h"
    main_repo.mkdir()

    def g(*a, cwd=main_repo):
        return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)

    g("init", "-q", "-b", "master")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (main_repo / "a.txt").write_text("1\n")
    g("add", "-A"); g("commit", "-q", "-m", "base")
    g("branch", "acceptance")

    wt = tmp_path / "wt"
    g("worktree", "add", "-q", "-b", "work", str(wt))
    (wt / "a.txt").write_text("2\n")
    g("add", "-A", cwd=wt); g("commit", "-q", "-m", "candidate", cwd=wt)
    verified = g("rev-parse", "HEAD^{tree}", cwd=wt).stdout.strip()

    # After the acceptance something else comes on top -- the developer sits there.
    (wt / "later.txt").write_text("added afterwards\n")
    g("add", "-A", cwd=wt); g("commit", "-q", "-m", "afterwards", cwd=wt)

    with pytest.raises(DeliveryRefused, match="added here"):
        deliver(repo=wt, mode="local-only", yolo="off", approved=True,
                target_branch="acceptance", candidate_id="c1", tree_digest=verified)


def test_ownership_list_knows_every_own_role_location(tmp_path, monkeypatch):
    """The guard has to know every location at which one of our own roles sits.

    Twice an enumerated ownership list rejected one of our own roles as a
    foreign session: first the developer's working tree was missing (finding
    13), then the arena root, into which planner and QA moved after the
    separation from the evidence directory. A protection that does not know
    its own roles is none -- it is a malfunction that disguises itself as
    security. The list is therefore derived.
    """
    from hoh import herdr as herdr_mod

    d = _dispatcher_with_store(tmp_path)
    d.profiles[Role.PLANNER] = "claude"
    wt = tmp_path / "worktree"
    wt.mkdir()
    st = make_state(run_id="a03", iteration=1, repo_path=str(wt))
    name = d._agent_name(Role.PLANNER, st)

    for location in (d.store_dir, d.arenas_dir, wt):
        Path(location).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(herdr_mod, "snapshot", lambda o=location: {"agents": [{
            "name": name, "pane_id": "wW:p1", "workspace_id": "wW", "tab_id": "wW:tA",
            "agent": "claude", "agent_status": "idle", "cwd": str(o),
        }]})
        monkeypatch.setattr(d, "_new_tab_pane", lambda *a, **k: pytest.fail("new tab"),
                            raising=False)
        monkeypatch.setattr(d, "_start_agent", lambda *a: pytest.fail("restart"), raising=False)
        d.agents.pop(Role.PLANNER, None)
        assert d._ensure_agent(Role.PLANNER, st) == name, f"{location} must count as our own"


# --------------------------------------------------------------------------- #
# O87 -- a valid answer whose own string values contain a markdown fence
# --------------------------------------------------------------------------- #


def test_valid_json_answer_survives_a_fence_inside_its_own_strings():
    """`extract_json` must parse a well-formed answer before unwrapping it.

    Run `d7y`'s planner produced a schema-valid answer -- 23103 bytes, `{` to
    `}`, twelve fields, ten acceptance checks -- one of whose string values
    quoted a shell code block out of `examples/minimal/README.md`, the very
    file the run was repairing. `_FENCE` matched that fence *inside the JSON*,
    the whole answer was replaced by the 276 bytes between the backticks, and
    the run was blocked with "no JSON object found in the answer". The schema
    repair asked again, got the same correct answer, and failed identically:
    **a retry cannot fix a defect in the reader.**

    The three shapes below must all work, and the first is the regression.
    """
    from hoh.roles import extract_json

    fence = "```"
    with_inner_fence = json.dumps(
        {
            "objective": "reword the README's promise",
            "note": f"the file quotes {fence}sh\nhoh worktree --repo x\n{fence} here",
            "targets": ["examples/minimal/README.md"],
        }
    )
    assert with_inner_fence.lstrip().startswith("{")
    recovered = extract_json(with_inner_fence)
    assert recovered["targets"] == ["examples/minimal/README.md"]
    assert "hoh worktree" in recovered["note"]

    # Still tolerant of the two presentation shapes the leniency exists for.
    assert extract_json(f"here you go:\n{fence}json\n" '{"a": 1}' f"\n{fence}\n") == {"a": 1}
    assert extract_json('prose before {"b": 2} prose after') == {"b": 2}


def test_an_earlier_irrelevant_fence_does_not_displace_later_valid_json():
    """O87, one step further out: EVERY fence is tried, not the first.

    The role prompts say "Emit **exclusively** a JSON object, with no
    surrounding text and no code fence", so an answer of prose plus several
    fences is **outside the contract** and no role may rely on it. But the
    leniency that exists to rescue a fence-wrapped payload must not *destroy*
    one: `_FENCE.search` returned the first match, so "prose, a ```sh block,
    then the ```json block" lost its payload -- the shell block became the
    whole text and the brace fallback searched that instead of the answer.

    Same failure family as the defect this file's previous test covers, and it
    was found by asking the contract question rather than by a run paying for
    it a second time.
    """
    from hoh.roles import extract_json, RoleOutputError

    fence = "```"
    gemischt = (f"First, the command I ran:\n{fence}sh\nhoh worktree --repo x\n{fence}\n"
                f"And here is the answer:\n{fence}json\n" '{"objective": "x", "n": 3}'
                f"\n{fence}\n")
    assert extract_json(gemischt) == {"objective": "x", "n": 3}

    # The brace fallback reads the ORIGINAL text, not a fence's content.
    assert extract_json(f"{fence}sh\nls\n{fence}\n" '{"b": 2}') == {"b": 2}

    # And nothing became lenient about content: a non-object still fails.
    with pytest.raises(RoleOutputError):
        extract_json(f"{fence}json\n[1, 2]\n{fence}")
    with pytest.raises(RoleOutputError):
        extract_json("no json at all, only prose")
