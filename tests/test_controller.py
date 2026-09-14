"""H2/H3: the loop as a whole.

Per handoff §11, unit and error tests may use fake adapters; A01/A02/A12
require real execution and run separately. The attacks live here: a lying
developer, a lying QA, tampered evidence, sources shifted underneath.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from hoh import roles
from hoh.contracts import Budgets, Outcome, Role, RunState, Stage
from hoh.controller import Controller, DispatchError, new_run_id
from hoh.evidence import EvidenceStatus
from hoh.store import RunStore


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "marker.txt").write_text("start\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    return p


class FakeDispatcher:
    """Controllable role adapter.

    `plan_json`, `qa_json` and `develop` are deliberately settable one at a
    time, so that each test can make exactly one role lie.
    """

    #: The file the default developer creates, and the file the default
    #: acceptance criterion looks for. `tests/test_review_findings.py` reads
    #: it as `FakeDispatcher.MARKER` instead of typing the name again: the two
    #: sides have to match, and a mismatch would make the criterion stop
    #: discriminating **silently** rather than fail loudly.
    MARKER = "done.txt"

    def __init__(self, *, repo: Path, plan_json=None, qa_json=None, develop=None):
        self.repo = repo
        if develop is None:
            # A criterion has to demonstrate the increment: the default
            # developer drops a marker file and the default check tests for it.
            # A "true" would be green on every state and would rightly be
            # downgraded.
            develop = lambda r: (r / self.MARKER).write_text("ok\n", encoding="utf-8")
        self.plan_json = plan_json
        self.qa_json = qa_json
        self.develop = develop
        self.calls: list[Role] = []
        self.prompts: dict[Role, str] = {}
        self.base_id = "base"
        self.fail_transient_times = 0

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role)
        self.prompts[role] = prompt

        if self.fail_transient_times > 0:
            self.fail_transient_times -= 1
            raise DispatchError("provider timeout", transient=True)

        if role is Role.PLANNER:
            m = re.search(r'"base_candidate_id":\s*"([^"]+)"', prompt)
            self.base_id = m.group(1) if m else "base"
            return json.dumps(
                self.plan_json(state) if callable(self.plan_json) else self.plan_json
            )
        if role is Role.DEVELOPER:
            if self.develop:
                self.develop(self.repo)
            return "done"
        return json.dumps(self.qa_json(state) if callable(self.qa_json) else self.qa_json)

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def plan_for(state: RunState, *, command: str = f"test -f {FakeDispatcher.MARKER}", check_id: str = "K1") -> dict:
    """A plan correctly bound to the run.

    `base_candidate_id` has to match the actual starting candidate -- the
    controller has been checking that binding since the review (handoff §6).
    """
    base = state.last_accepted_candidate or state.base_candidate
    return {
        "run_id": state.run_id,
        "iteration": state.iteration,
        "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
        "spec_digest": state.spec_digest,
        "objective": "keep add() correct",
        "targets": ["app.py"],
        "preserve": [],
        "acceptance_checks": [
            {
                "check_id": check_id,
                "description": "the addition is correct",
                "command": command,
                "expect_exit": 0,
                "preserves": False,
            }
        ],
        "out_of_scope": [],
        "evidence_refs": [],
        "repair_only": False,
        "repair_reason": None,
    }


def qa_pass(state: RunState, check_id: str = "K1") -> dict:
    rid = f"{state.run_id}-i{state.iteration}-a{state.attempt}-{check_id}"
    return {
        "verdicts": [{"check_id": check_id, "outcome": "PASS", "receipt_id": rid,
                      "reproduction": "pytest", "note": "green"}],
        "open_gaps": [],
        "summary": "all good",
    }


def build(tmp_path: Path, repo: Path, spec: Path, dispatcher) -> tuple[Controller, RunState, RunStore]:
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="project", spec_path=spec,
        budgets=Budgets(max_iterations=5),
    )
    store.write_state(state)
    return Controller(store, dispatcher, spec_path=spec), state, store


# --- The happy path -------------------------------------------------------- #


def test_an_iteration_is_accepted(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert out.accepted, out.reason
    assert state.stage is Stage.CHECKPOINTED
    assert state.last_accepted_candidate is not None
    assert d.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA], "role order from the paper"
    assert store.read_evidence().to_preserve(), (
        "a PASS becomes a preservation requirement"
    )


def test_the_roles_get_separate_prompts(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    planner = d.prompts[Role.PLANNER]
    qa = d.prompts[Role.QA]
    assert ("implement nothing, edit nothing, test nothing" in planner
            or "pure\nplanning invocation" in planner)
    assert "frozen, read-only" in qa or "frozen" in qa
    assert "Change nothing about the candidate" in qa


# --- A04: QA catches what the developer keeps quiet about ------------------ #


def test_a_lying_developer_is_stopped_by_qa(tmp_path, repo, spec):
    """The developer reports 'done', the check is red. No checkpoint."""

    def break_it(r: Path) -> None:
        (r / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="python3 -c \"import sys; sys.path.insert(0,'.'); from app import add; sys.exit(0 if add(2,2)==4 else 1)\""),
        qa_json=lambda s: {
            "verdicts": [{"check_id": "K1", "outcome": "FAIL", "note": "add(2,2) != 4"}],
            "open_gaps": [], "summary": "Regression",
        },
        develop=break_it,
    )
    ctrl, state, store = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert state.stage is Stage.PLANNING, "a rejection leads into replanning"
    assert state.last_accepted_candidate is None
    assert state.working_candidate is not None, "the failed candidate is kept"
    gaps = [i for i in store.read_evidence().items if i.status is EvidenceStatus.GAP]
    assert gaps, "the finding lands in the evidence for the next round"


def test_the_runner_refutes_a_false_pass(tmp_path, repo, spec):
    """QA claims PASS, the runner says red. The record decides."""
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="exit 1"),
        qa_json=qa_pass,  # claims PASS with a valid-looking receipt ID
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted, "a receipt with exit!=0 carries no PASS"
    verdict = next(v for v in out.verdicts if v.check_id == "K1")
    assert verdict.outcome is not Outcome.PASS


# --- A05: false evidence --------------------------------------------------- #


def test_an_invented_receipt_id_is_refused(tmp_path, repo, spec):
    d = FakeDispatcher(
        repo=repo,
        plan_json=plan_for,
        qa_json=lambda s: {
            "verdicts": [{"check_id": "K1", "outcome": "PASS", "receipt_id": "made-up"}],
            "open_gaps": [], "summary": "",
        },
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)

    assert not out.accepted
    v = next(v for v in out.verdicts if v.check_id == "K1")
    assert v.outcome is Outcome.INCONCLUSIVE
    assert "rejected" in (v.note or "")


def test_a_pass_without_a_receipt_is_downgraded(tmp_path, repo, spec):
    d = FakeDispatcher(
        repo=repo,
        plan_json=plan_for,
        qa_json=lambda s: {
            "verdicts": [{"check_id": "K1", "outcome": "PASS"}],
            "open_gaps": [], "summary": "",
        },
    )
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert next(v for v in out.verdicts).outcome is Outcome.INCONCLUSIVE


def test_a_criterion_kept_quiet_about_stays_open(tmp_path, repo, spec):
    """QA judges only one of two criteria."""

    def two_checks(s: RunState) -> dict:
        p = plan_for(s)
        p["acceptance_checks"].append(
            {"check_id": "K2", "description": "second", "command": f"test -f {FakeDispatcher.MARKER}",
             "expect_exit": 0, "preserves": False}
        )
        return p

    d = FakeDispatcher(repo=repo, plan_json=two_checks, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)

    ids = {v.check_id for v in out.verdicts}
    assert ids == {"K1", "K2"}, "no criterion may disappear"


def test_a_source_change_during_qa_breaks_the_binding(tmp_path, repo, spec):
    """A05: a source changed after the freeze.

    Two nets catch this now and the order matters. The witness fires first,
    during the dispatch, naming the tree; the binding check remains behind it
    for anything that changes outside a dispatch window. Either way the
    iteration is not accepted and the run is blocked -- which is the property
    A05 is about.
    """

    def sabotage(state: RunState) -> dict:
        (repo / "app.py").write_text("# changed during QA\n", encoding="utf-8")
        return qa_pass(state)

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=sabotage)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert ("candidate binding" in out.reason
            or "modified during QA" in out.reason
            or "capability violation" in out.reason)
    assert state.condition.value == "BLOCKED"


def test_a_source_change_during_qa_is_named_as_a_capability_violation(
        tmp_path, repo, spec):
    """The witness is what sees it, and it says so rather than crashing.

    A violation raised out of `run_iteration` would end the process; a
    violation converted into an outage would be re-read as a missing verdict
    and the loop would carry on. It is neither: a blocked run with the tree
    named in its reason.
    """

    def sabotage(state: RunState) -> dict:
        (repo / "app.py").write_text("# changed during QA\n", encoding="utf-8")
        return qa_pass(state)

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=sabotage)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)

    assert "capability violation" in out.reason
    assert str(repo) in out.reason
    assert any("capability violation" in h for h in state.history)


# --- A house rule inside an acceptance criterion --------------------------- #


def test_a_check_with_nvidia_smi_is_not_executed(tmp_path, repo, spec):
    """A plan must not get around the guards by way of a 'criterion'."""
    d = FakeDispatcher(
        repo=repo,
        plan_json=lambda s: plan_for(s, command="nvidia-smi -l 1"),
        qa_json=qa_pass,
    )
    ctrl, state, store = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)

    assert not out.accepted
    log = (store.logs_dir / f"{state.run_id}-i1-a1-K1.txt").read_text(encoding="utf-8")
    assert "HOUSE RULE VIOLATION" in log


# --- Budgets and errors ---------------------------------------------------- #


def test_a_transient_error_is_retried_within_a_limit(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    d.fail_transient_times = 2
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_transient_retries=2, max_iterations=5)

    out = ctrl.run_iteration(state)
    assert out.accepted
    assert state.usage.transient_retries == 2


def test_too_many_transient_errors_abort(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    d.fail_transient_times = 5
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_transient_retries=2)

    with pytest.raises(DispatchError):
        ctrl.run_iteration(state)


def test_an_exhausted_budget_blocks_instead_of_spinning_on(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=1)
    state.usage.iterations = 1

    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert state.condition.value == "BLOCKED"
    assert "iteration budget" in out.reason


def test_a_broken_plan_is_repaired_once(tmp_path, repo, spec):
    step = {"n": 0}

    def broken_once(s: RunState):
        step["n"] += 1
        if step["n"] == 1:
            return {"objective": "incomplete"}  # schema violation
        return plan_for(s)

    d = FakeDispatcher(repo=repo, plan_json=broken_once, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)
    assert out.accepted
    assert state.usage.schema_repairs == 1


def test_a_second_schema_violation_aborts(tmp_path, repo, spec):
    d = FakeDispatcher(repo=repo, plan_json=lambda s: {"broken": True}, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    with pytest.raises(DispatchError, match="repair budget"):
        ctrl.run_iteration(state)


def test_a_misbound_plan_is_recorded_as_a_failed_dispatch(tmp_path, repo, spec):
    """A plan that is schema-valid JSON but bound to the wrong run is
    rejected by `_assert_plan_bound` -- the telemetry record for that
    dispatch has to say so too. `note_dispatch(outcome="ok")` must not fire
    before the binding check has actually passed, or the record would claim
    success for a dispatch the controller itself refused."""

    def misbound(s: RunState) -> dict:
        bad = plan_for(s)
        bad["run_id"] = "some-other-run"
        return bad

    d = FakeDispatcher(repo=repo, plan_json=misbound, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    with pytest.raises(roles.RoleOutputError, match="not bound to this run"):
        ctrl.run_iteration(state)

    planner_records = [r for r in ctrl.telemetry().read() if r.role == "planner"]
    assert len(planner_records) == 1
    assert planner_records[0].outcome == "failed"
    assert "run_id" in planner_records[0].detail


# --- Warm start across two loops (A02) ------------------------------------- #


def test_the_second_loop_starts_warm_and_inherits_the_evidence(tmp_path, repo, spec):
    """Warm start and evidence hand-over across two accepted iterations.

    Correction provenance (reviewer finding 1, 2026-09-07): the previous
    version let **both** iterations run the same plan with the same K1 and
    expected the second one to be accepted. That was only possible because
    `discriminates` was set by default -- so the test was locking in exactly
    the gap the rule is built against: an iteration that changes nothing was
    booked as an increment. Now the second iteration brings a criterion of its
    own along, the way a real planner does (in run `a02` that was K10 in
    iteration 6).
    """
    step = {"n": 0}

    def plan_with_a_new_criterion(state):
        # Every iteration brings a criterion of its own along that is red on
        # the predecessor state: the fake developer's marker carries the
        # iteration number, so it does not exist beforehand.
        step["n"] += 1
        return plan_for(state, check_id=f"K{step['n']}",
                        command=f"test -f done-i{step['n']}.txt")

    class StepwiseDeveloper(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.DEVELOPER:
                (Path(self.repo) / f"done-i{state.iteration}.txt").write_text(
                    "ok\n", encoding="utf-8"
                )
                self.prompts[role] = prompt
                return "done"
            return super().dispatch(role, prompt, state=state)

    def qa_all(state):
        # Judges **all** criteria of the iteration, including the ones carried
        # over from the preservation suite -- otherwise the carried-over one
        # falls to "not assessed by QA" and the iteration fails on that instead
        # of on the substance.
        ids = [f"K{i}" for i in range(1, step["n"] + 1)]
        return {
            "verdicts": [
                {"check_id": cid, "outcome": "PASS",
                 "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-{cid}",
                 "reproduction": "pytest", "note": "green"}
                for cid in ids
            ],
            "open_gaps": [],
            "summary": "all good",
        }

    d = StepwiseDeveloper(
        repo=repo, plan_json=plan_with_a_new_criterion, qa_json=qa_all
    )
    ctrl, state, store = build(tmp_path, repo, spec, d)

    first_round = ctrl.run_iteration(state)
    assert first_round.accepted, first_round.reason
    assert state.stage is Stage.CHECKPOINTED
    first = state.last_accepted_candidate

    second = ctrl.run_iteration(state)
    assert second.accepted, second.reason

    # The new criterion demonstrates the increment, the carried-over one does not.
    by_kind = {v.check_id: v.discriminates for v in second.verdicts}
    assert by_kind.get("K2") is True, "the new criterion was run against the base"
    assert by_kind.get("K1") is False, (
        "a carried-over preservation criterion demonstrates no increment"
    )

    assert state.iteration == 2
    assert state.last_accepted_candidate is not None
    dev_prompt = d.prompts[Role.DEVELOPER]
    assert "/warm-start" in dev_prompt, "the second loop inherits the artifact"
    planner_prompt = d.prompts[Role.PLANNER]
    assert "To preserve" in planner_prompt, "and the evidence of the previous round"
    assert first is not None


def test_run_id_is_unique():
    assert new_run_id() != new_run_id()


def test_a_failed_read_copy_really_does_fall_back_to_the_live_path(
    tmp_path, repo, spec, monkeypatch
):
    """The note says "falling back to live path" -- then that is what it has to be.

    `_planner_relpath` is only set inside the `try` and never reset. If the
    read copy succeeds in iteration 1 and fails in iteration 2, the planner
    keeps getting the **stale** path from iteration 1, even though the state
    records the fall-back to the live path. Message and behaviour then say
    different things -- exactly the quiet false statement that the evidence
    part of this project is built against.
    """
    from hoh import controller as controller_mod
    from hoh import roles

    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    d.role_cwd = {}          # without role_cwd the read copy is never even named
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    seen: list[str] = []
    real_prompt = roles.planner_prompt

    def spy(**kw):
        seen.append(kw["repo_path"])
        return real_prompt(**kw)

    monkeypatch.setattr(roles, "planner_prompt", spy)

    ctrl.run_iteration(state)
    assert seen[0] != str(repo), "iteration 1: the planner reads the copy"
    assert getattr(ctrl, "_planner_relpath", None), "the path sticks to the controller"

    # Iteration 2: only the **read copy** fails. It is the first materialize
    # call of the iteration; the verification arena after it has to succeed,
    # otherwise the test checks something other than the fall-back.
    real_materialize = controller_mod.materialize
    calls = {"n": 0}

    def the_first_one_fails(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no space left on device")
        return real_materialize(*a, **k)

    monkeypatch.setattr(controller_mod, "materialize", the_first_one_fails)
    state.stage = Stage.PLANNING
    ctrl.run_iteration(state)

    assert any("falling back to live path" in h for h in state.history), \
        "the fall-back is recorded"
    assert seen[-1] == str(repo), (
        "what was recorded is the live path, but what was handed over was "
        f"{seen[-1]!r} -- message and behaviour have to say the same thing"
    )


def test_a_blocked_qa_dialog_is_reported_route_independently(tmp_path, repo, spec):
    """K10, found by the dogfood run on itself, 2026-09-08.

    `cli.cmd_run` keeps the role tabs open when an iteration ends in an
    **exception** carrying `WaitingForApproval`, so the captain can still
    answer the dialog. But `_verify` converts exactly that exception into an
    *outage* string -- which is right: an unanswered QA is a missing verdict,
    not a failed run. The consequence was that the blocked dialog took the
    **normal** exit, where `close_own()` closes the tabs, and with them the
    one window in which the dialog could have been answered. The protection
    guarded one route while the traffic took the other.

    In `d1` iteration 1 that happened for real: the run reported
    "unverified, no QA verdict -- QA did not answer (qa waits for an approval
    in pane w17:p4)", the outage correctly did not count against the progress
    budget, and the pane was gone anyway.
    """
    from hoh.controller import WaitingForApproval

    class BlockedQA(FakeDispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.QA:
                raise WaitingForApproval(
                    "qa waits for an approval in pane wX:pY. "
                    "A blocked dialog is not answered automatically."
                )
            return super().dispatch(role, prompt, state=state)

    d = BlockedQA(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert out.waiting_for_approval, (
        "the pending dialog has to leave the controller as a typed fact -- "
        "a word in the reason is the coupling WaitingForApproval removed"
    )
    # An outage is not a failed attempt: the progress budget stays untouched.
    assert state.usage.loops_without_progress == 0
    # And it is an outage, not a content verdict.
    assert "unverified" in out.reason


def test_an_accepted_iteration_does_not_claim_a_pending_dialog(tmp_path, repo, spec):
    """The counter-check, so the flag cannot simply always be true."""
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert not out.waiting_for_approval
