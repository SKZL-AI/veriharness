"""Is the capability policy actually installed, or only well written?

`tests/test_capability.py` attacks the instrument: it builds a policy by hand,
plants eleven kinds of write, and checks each is caught. Every one of those
tests passes with the controller handing every role an empty protected set --
an adversarial review proved exactly that by setting `protected=()` for the
planner and watching 840 tests stay green.

So this file tests the *installation*. It asks `Controller._policy_for` which
paths it protects, names them, and then reproduces the two escapes the review
found end to end:

* a planner that empties `checks.json` during its own dispatch removes the
  preservation suite, and a genuine regression is accepted as a checkpoint;
* a planner that writes a file into the live worktree has it land inside the
  accepted candidate, because `snapshot()` binds the working tree.

Both produced zero violations and zero notes before the run directory and the
repository were added to the protected set. The positive controls matter as
much: the developer must still be able to write, and QA must still be able to
work in the arena it is reviewing.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from hoh.contracts import Budgets, Role, RunState, Stage
from hoh.controller import Controller


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
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    return p


MARKER = "done.txt"


def plan_for(state: RunState, *, command: str = f"test -f {MARKER}",
             check_id: str = "K1") -> dict:
    base = state.last_accepted_candidate or state.base_candidate
    return {
        "run_id": state.run_id, "iteration": state.iteration,
        "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
        "spec_digest": state.spec_digest, "objective": "keep add() correct",
        "targets": ["app.py"], "preserve": [],
        "acceptance_checks": [{
            "check_id": check_id, "description": "the increment is there",
            "command": command, "expect_exit": 0, "preserves": False,
        }],
        "out_of_scope": [], "evidence_refs": [],
        "repair_only": False, "repair_reason": None,
    }


def qa_pass(state: RunState, check_id: str = "K1") -> dict:
    rid = f"{state.run_id}-i{state.iteration}-a{state.attempt}-{check_id}"
    return {
        "verdicts": [{"check_id": check_id, "outcome": "PASS", "receipt_id": rid,
                      "reproduction": "pytest", "note": "green"}],
        "open_gaps": [], "summary": "all good",
    }


class Dispatcher:
    """A dispatcher whose roles can be given one side effect each.

    The side effect runs *inside* the dispatch, which is the whole point: a
    write that happens between dispatches is part of the next witness's
    starting state and is a different situation.
    """

    def __init__(self, *, repo: Path, plan_json=plan_for, qa_json=qa_pass,
                 develop=None, planner_effect=None, qa_effect=None):
        self.repo = repo
        self.plan_json = plan_json
        self.qa_json = qa_json
        self.develop = develop or (
            lambda r: (r / MARKER).write_text("ok\n", encoding="utf-8"))
        self.planner_effect = planner_effect
        self.qa_effect = qa_effect
        self.calls: list[Role] = []
        self.prompts: dict[Role, str] = {}

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role)
        self.prompts[role] = prompt
        if role is Role.PLANNER:
            if self.planner_effect:
                self.planner_effect(state)
            m = re.search(r'"base_candidate_id":\s*"([^"]+)"', prompt)
            _ = m.group(1) if m else "base"
            return json.dumps(self.plan_json(state))
        if role is Role.DEVELOPER:
            self.develop(self.repo)
            return "done"
        if self.qa_effect:
            self.qa_effect(state)
        return json.dumps(self.qa_json(state))

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def build(tmp_path: Path, repo: Path, spec: Path, dispatcher):
    from hoh.store import RunStore

    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="project", spec_path=spec,
        budgets=Budgets(max_iterations=5),
    )
    store.write_state(state)
    return Controller(store, dispatcher, spec_path=spec), state, store


# --------------------------------------------------------------------------- #
# What the installed policy actually protects
# --------------------------------------------------------------------------- #


def _policy(ctrl, role, state):
    return ctrl._policy_for(role, state)


def test_the_planner_protects_the_run_directory_and_the_repository(
        tmp_path, repo, spec):
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    p = _policy(ctrl, Role.PLANNER, state)
    geschuetzt = {str(x) for x in p.protected}
    assert str(store.dir / "state.json") in geschuetzt
    assert str(repo) in geschuetzt


def test_the_preservation_suite_is_protected_once_it_exists(tmp_path, repo, spec):
    """`checks.json` is the file whose removal bought a false checkpoint."""
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    (store.dir / "checks.json").write_text('{"checks": []}')
    p = _policy(ctrl, Role.PLANNER, state)
    assert str(store.dir / "checks.json") in {str(x) for x in p.protected}


def test_no_role_protects_the_directory_it_answers_through(tmp_path, repo, spec):
    """Protecting `answers/` would make every role's own answer a violation."""
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    for rolle in (Role.PLANNER, Role.DEVELOPER, Role.QA):
        p = _policy(ctrl, rolle, state)
        assert str(store.dir / "answers") not in {str(x) for x in p.protected}


def test_the_developer_is_the_one_role_whose_workspace_is_not_protected(
        tmp_path, repo, spec):
    ctrl, state, _ = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    entwickler = {str(x) for x in _policy(ctrl, Role.DEVELOPER, state).protected}
    assert str(repo) not in entwickler
    for rolle in (Role.PLANNER, Role.QA):
        assert str(repo) in {str(x) for x in _policy(ctrl, rolle, state).protected}


def test_the_planner_and_developer_watch_the_arena_root_for_a_new_child(
        tmp_path, repo, spec):
    """A staging directory created during a dispatch and moved in afterwards."""
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    store.arenas_dir.mkdir(parents=True, exist_ok=True)
    for rolle in (Role.PLANNER, Role.DEVELOPER):
        p = _policy(ctrl, rolle, state)
        assert str(store.arenas_dir) in {str(x) for x in p.protected_shallow}


def test_qa_does_not_watch_the_listing_of_its_own_working_directory(
        tmp_path, repo, spec):
    """QA's cwd *is* the arena root, and running the candidate writes there.

    `pytest` leaves `.pytest_cache` beside the arenas, and a listing watch over
    a role's own working directory fires on the role doing its job. The
    alternative -- a list of tool names to ignore -- is the growing exclusion
    list this project rejects elsewhere. Every arena but QA's own stays
    protected in full; limit 12d carries what is given up.
    """
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    store.arenas_dir.mkdir(parents=True, exist_ok=True)
    p = _policy(ctrl, Role.QA, state)
    assert p.protected_shallow == ()
    assert p.protected, "QA still protects the trees it must not touch"


def test_an_unknown_role_is_denied_everything(tmp_path, repo, spec):
    ctrl, state, _ = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    p = _policy(ctrl, Role.GATE if hasattr(Role, "GATE") else Role.QA, state)
    assert p.protected                      # never an empty protected set


def test_the_output_channel_is_the_file_the_dispatcher_will_write(
        tmp_path, repo, spec):
    """It named `answers/{role}.json`, which no dispatch ever creates.

    `_answered()` decides whether a role delivered by looking at that path, so
    a channel pointing at a file that never exists cannot decide anything.
    """
    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    state.iteration, state.attempt = 3, 2
    p = _policy(ctrl, Role.PLANNER, state)
    assert p.output_channel == store.dir / "answers" / "i3-a2-planner.json"


# --------------------------------------------------------------------------- #
# The two escapes, reproduced end to end
# --------------------------------------------------------------------------- #


def test_a_planner_that_empties_the_preservation_suite_is_caught(
        tmp_path, repo, spec):
    """The first escape, and the one with real consequences.

    Iteration 1 establishes `K1` as a preservation requirement. In iteration 2
    the planner empties `checks.json` during its own dispatch, the developer
    removes the marker `K1` looks for, and the regression used to be accepted
    as a checkpoint with nothing noted anywhere.
    """
    zustand = {"n": 0}

    def sabotage(state: RunState) -> None:
        zustand["n"] += 1
        if zustand["n"] >= 2:
            (store.dir / "checks.json").write_text('{"checks": []}')

    def entwickeln(r: Path) -> None:
        if zustand["n"] >= 2:
            (r / MARKER).unlink(missing_ok=True)
        else:
            (r / MARKER).write_text("ok\n", encoding="utf-8")

    d = Dispatcher(repo=repo, planner_effect=sabotage, develop=entwickeln)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    erste = ctrl.run_iteration(state)
    assert erste.accepted, erste.reason

    zweite = ctrl.run_iteration(state)
    assert not zweite.accepted
    assert "capability violation" in zweite.reason
    assert "checks.json" in zweite.reason
    assert state.condition.value == "BLOCKED"


def test_a_planner_that_writes_into_the_live_worktree_is_caught(
        tmp_path, repo, spec):
    """The second escape: `snapshot()` binds the working tree, not the commit.

    A file the planner dropped into the repository during its own dispatch was
    therefore inside the candidate the run then accepted.
    """
    def hintertuer(state: RunState) -> None:
        (repo / "backdoor.py").write_text("# planted by the planner\n")

    d = Dispatcher(repo=repo, planner_effect=hintertuer)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert "capability violation" in out.reason
    assert str(repo) in out.reason


def test_a_planner_that_writes_into_a_candidate_arena_is_caught(
        tmp_path, repo, spec):
    """O125 in its original words, against the installed policy."""
    zustand = {"n": 0}

    def sabotage(state: RunState) -> None:
        zustand["n"] += 1
        if zustand["n"] < 2:
            return
        for d in sorted(store.arenas_dir.glob("*")):
            if d.is_dir() and d.name != "planner" and any(d.iterdir()):
                (d / "planted.py").write_text("def add(a, b): return 0\n")
                return

    d = Dispatcher(repo=repo, planner_effect=sabotage)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    assert ctrl.run_iteration(state).accepted
    zweite = ctrl.run_iteration(state)

    assert not zweite.accepted
    assert "capability violation" in zweite.reason


def test_qa_may_not_reach_the_arena_of_another_iteration(tmp_path, repo, spec):
    """QA's working directory is still the arena root, so its siblings are
    one name away. They are protected rather than merely out of reach."""
    zustand = {"n": 0}

    def sabotage(state: RunState) -> None:
        zustand["n"] += 1
        if zustand["n"] < 2:
            return
        eigene = getattr(ctrl, "_qa_arena", None)
        for d in sorted(store.arenas_dir.glob("*")):
            if (d.is_dir() and d.name != "planner" and any(d.iterdir())
                    and (eigene is None or d.resolve() != eigene.resolve())):
                (d / "qa-notes.md").write_text("# planted\n")
                return

    d = Dispatcher(repo=repo, qa_effect=sabotage)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    assert ctrl.run_iteration(state).accepted
    zweite = ctrl.run_iteration(state)
    assert not zweite.accepted
    assert "capability violation" in zweite.reason


# --------------------------------------------------------------------------- #
# Positive controls: the boundary must not be so tight that work stops
# --------------------------------------------------------------------------- #


def test_an_ordinary_iteration_is_still_accepted(tmp_path, repo, spec):
    d = Dispatcher(repo=repo)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert state.stage is Stage.CHECKPOINTED
    assert d.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA]


def test_two_ordinary_iterations_in_a_row_are_still_accepted(tmp_path, repo, spec):
    """The second iteration is where a protected set that is too wide shows up.

    By then arenas, receipts and evidence all exist, and a policy that
    protected the wrong one of them would fail a run that did nothing wrong.
    """
    def plan(state: RunState) -> dict:
        # A new criterion per iteration, because a check that was already
        # green on the predecessor demonstrates no increment and is rightly
        # refused -- which would fail this test for a reason that has nothing
        # to do with capabilities.
        return plan_for(state, command=f"test -f step{state.iteration}.txt",
                        check_id=f"K{state.iteration}")

    def entwickeln(r: Path) -> None:
        n = 1 + len(list(r.glob("step*.txt")))
        (r / f"step{n}.txt").write_text("ok\n", encoding="utf-8")

    def qa(state: RunState) -> dict:
        # Preservation requirements accumulate, and QA has to answer all of
        # them -- an unanswered criterion is a missing verdict, not a pass.
        alle = [f"K{i}" for i in range(1, state.iteration + 1)]
        return {
            "verdicts": [{
                "check_id": k, "outcome": "PASS",
                "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-{k}",
                "reproduction": "pytest", "note": "green",
            } for k in alle],
            "open_gaps": [], "summary": "all good",
        }

    d = Dispatcher(repo=repo, plan_json=plan, develop=entwickeln, qa_json=qa)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    zweite = ctrl.run_iteration(state)
    assert zweite.accepted, zweite.reason


def test_qa_writing_in_the_arena_it_reviews_is_not_a_violation(tmp_path, repo, spec):
    """Bytecode from running the thing is the ordinary case, not an attack."""
    def arbeiten(state: RunState) -> None:
        eigene = getattr(ctrl, "_qa_arena", None)
        if eigene is not None:
            (eigene / "__pycache__").mkdir(exist_ok=True)
            (eigene / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
        # ... and the cache a test runner leaves in the directory QA works from
        (ctrl.store.arenas_dir / ".pytest_cache").mkdir(exist_ok=True)

    d = Dispatcher(repo=repo, qa_effect=arbeiten)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


# --------------------------------------------------------------------------- #
# The prompt and the policy, held against each other
# --------------------------------------------------------------------------- #


def test_what_the_prompt_forbids_and_the_policy_grants_is_named_as_such(
        tmp_path, repo, spec):
    """`capability.py` says "where the two disagree, one of them is wrong".

    They do disagree, and neither is wrong. The prompt says *test nothing*;
    the policy grants `process_exec`, because an agent in a pane has a shell
    and declaring otherwise would be a claim this code cannot back. The
    discrepancy is legitimate and must be **visible**, which is what
    `declared_but_unenforced` is for -- an unexplained mismatch between a
    prompt and a policy is how O125 looked before anyone measured it.
    """
    d = Dispatcher(repo=repo)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)

    prompt = d.prompts[Role.PLANNER]
    p = _policy(ctrl, Role.PLANNER, state)

    assert "test nothing" in prompt, "the prompt no longer forbids running things"
    assert p.process_exec is True, "the policy no longer grants it"
    assert "process_exec" in p.declared_but_unenforced, (
        "the prompt forbids what the policy grants, and nothing says so")


def test_every_capability_a_role_is_granted_but_not_held_to_is_declared(
        tmp_path, repo, spec):
    """A policy may over-grant, but never quietly.

    `process_exec` and `network` cannot be withheld from an agent in a pane by
    anything in this process. Granting them is honest; granting them without
    saying so would make the policy read as enforcement it is not.
    """
    ctrl, state, _ = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    for rolle in (Role.PLANNER, Role.DEVELOPER, Role.QA):
        p = _policy(ctrl, rolle, state)
        assert p.declared_but_unenforced, f"{rolle.value} declares nothing"
        for name in p.declared_but_unenforced:
            assert hasattr(p, name), f"{rolle.value} names an unknown capability"


# --------------------------------------------------------------------------- #
# Telemetry, read back off a whole iteration rather than from `note_dispatch`
# --------------------------------------------------------------------------- #


def _telemetrie(store):
    import json as _json

    pfad = store.dir / "telemetry.jsonl"
    return [_json.loads(z) for z in pfad.read_text().splitlines() if z.strip()]


def test_an_iteration_records_one_dispatch_per_role_with_its_identity(
        tmp_path, repo, spec):
    """The installation, not the instrument.

    Six tests once checked `note_dispatch` in isolation with a controller built
    by `__new__` and a dispatcher of `object()`. That cannot tell a correct
    implementation from one that reads a method which does not exist -- which
    is what it was doing. This drives a real iteration and reads the log back.
    """
    from hoh.contracts import Role as R

    d = Dispatcher(repo=repo)
    d.profiles = {R.PLANNER: "claude", R.DEVELOPER: "claude", R.QA: "claude"}
    d.models = {R.PLANNER: "claude-opus-5", R.DEVELOPER: "claude-opus-5",
                R.QA: "claude-sonnet-5"}
    d.efforts = {R.PLANNER: "high", R.DEVELOPER: "high", R.QA: "high"}
    ctrl, state, store = build(tmp_path, repo, spec, d)

    assert ctrl.run_iteration(state).accepted
    saetze = _telemetrie(store)

    assert [s["role"] for s in saetze] == ["planner", "developer", "qa"]
    assert {s["provider"] for s in saetze} == {"claude"}
    assert saetze[0]["model"] == "claude-opus-5"
    assert saetze[2]["model"] == "claude-sonnet-5"
    assert {s["effort"] for s in saetze} == {"high"}


def test_a_dispatcher_that_names_no_model_says_not_available(tmp_path, repo, spec):
    """The honest half of the same field: an agent in a pane picks its own."""
    from hoh.telemetry import NOT_AVAILABLE

    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    assert {s["model"] for s in _telemetrie(store)} == {NOT_AVAILABLE}


def test_the_verification_record_counts_every_receipt_the_run_wrote(
        tmp_path, repo, spec):
    """Including the baseline receipts, which were not in the candidate loop.

    A run with two receipt files reported one. That is the same defect as
    reporting zero, one step smaller, and it is the number the ledger entry
    about this quotes.
    """
    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    auf_platte = len(list((store.dir / "receipts").glob("*.json")))
    qa = next(s for s in _telemetrie(store) if s["role"] == "qa")
    assert auf_platte >= 2, "the fixture should produce a baseline receipt too"
    assert qa["receipts"] == auf_platte


def test_every_record_says_what_the_witness_covered(tmp_path, repo, spec):
    """Otherwise a violation count of zero cannot be read as evidence."""
    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    for s in _telemetrie(store):
        assert s["witnessed_trees"] is not None
        assert s["witnessed_trees"] > 0, f"{s['role']} ran unwitnessed"


def test_a_transient_retry_reaches_the_record(tmp_path, repo, spec):
    """`retries` was counted in `state.usage` and nowhere else.

    Every record read `retries: 0` on a run that had retried, in a log whose
    stated purpose is "how much did that take".
    """
    from hoh.contracts import Role as R
    from hoh.controller import DispatchError

    class Wackelig(Dispatcher):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.uebrig = 2

        def dispatch(self, role, prompt, *, state):
            if role is R.PLANNER and self.uebrig:
                self.uebrig -= 1
                raise DispatchError("provider timeout", transient=True)
            return super().dispatch(role, prompt, state=state)

    d = Wackelig(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    planer = next(s for s in _telemetrie(store) if s["role"] == "planner")
    assert planer["retries"] == 2
    assert state.usage.transient_retries == 2


def test_a_retry_on_one_role_is_not_charged_to_the_next(tmp_path, repo, spec):
    """The counter is reset per role attempt, not left standing."""
    from hoh.contracts import Role as R
    from hoh.controller import DispatchError

    class Wackelig(Dispatcher):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.uebrig = 1

        def dispatch(self, role, prompt, *, state):
            if role is R.PLANNER and self.uebrig:
                self.uebrig -= 1
                raise DispatchError("provider timeout", transient=True)
            return super().dispatch(role, prompt, state=state)

    d = Wackelig(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    nach_rolle = {s["role"]: s["retries"] for s in _telemetrie(store)}
    assert nach_rolle == {"planner": 1, "developer": 0, "qa": 0}


def test_a_failed_dispatch_is_classified_from_its_exception(tmp_path, repo, spec):
    """Not from the agent's terminal output.

    The classifier was only ever handed the message, and the message carries
    forty lines of whatever the agent had on screen. In a project that
    dogfoods on a tree containing `approval.py` and `contracts.py`, that
    decided the retry policy by coincidence.
    """
    from hoh.contracts import Role as R
    from hoh.controller import DispatchError
    from hoh.taxonomy import FailureClass

    class Kaputt(Dispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is R.PLANNER:
                raise DispatchError(
                    "the provider connection was lost\n"
                    "  $ grep -rn approval src/\n"
                    "  $ pytest tests/test_contracts.py\n",
                    transient=False)
            return super().dispatch(role, prompt, state=state)

    d = Kaputt(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    try:
        ctrl.run_iteration(state)
    except DispatchError:
        pass
    planer = next(s for s in _telemetrie(store) if s["role"] == "planner")
    assert planer["outcome"] == "failed"
    assert planer["failure_class"] == FailureClass.PROVIDER_TRANSIENT.value


def test_the_amendment_chain_is_protected(tmp_path, repo, spec):
    """It decides whether an acceptance may stand, and it is a file in the run
    directory.

    A role that could empty `amendments.json` would lift the revalidation gate
    the chain imposes -- the same shape as the planner that emptied
    `checks.json` and rode a real regression to a checkpoint.
    """
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    (store.dir / "amendments.json").write_text('{"run_id": "r1", '
                                               '"origin_digest": "x", '
                                               '"amendments": []}')
    for rolle in (Role.PLANNER, Role.DEVELOPER, Role.QA):
        p = _policy(ctrl, rolle, state)
        assert str(store.dir / "amendments.json") in {str(x) for x in p.protected}, (
            f"{rolle.value} could rewrite the chain that gates acceptance")


def test_a_role_cannot_create_the_amendment_chain_that_gates_acceptance(
        tmp_path, repo, spec):
    """Protected before it exists, which is when it can be forged.

    The path was in the protected set only `if p.exists()`, so on a run that
    had never been amended it was in no witness at all. A reviewer had the
    planner write itself a specification and a matching chain during its own
    dispatch: zero violations, and from the next iteration the controller
    believed a specification a role had written for itself.
    """
    def faelschen(state: RunState) -> None:
        (store.dir / "amendments.json").write_text(json.dumps({
            "run_id": "r1", "origin_digest": state.spec_digest,
            "amendments": [],
        }))

    d = Dispatcher(repo=repo, planner_effect=faelschen)
    ctrl, state, store = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert "capability violation" in out.reason
    assert "amendments.json" in out.reason


def test_a_dispatch_that_took_no_witness_reports_none(tmp_path, repo, spec):
    """A record that states a measurement it never took is a fabrication.

    A dispatch refused at the budget check raises before the witness is taken,
    and the record then carried the **previous** dispatch's coverage -- a
    planner record saying 11 trees and 0 listings, when a planner policy always
    has exactly one listing and this one had witnessed nothing at all.
    """
    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    # one dispatch left: the planner gets it, the developer is refused at the
    # budget check -- before any witness is taken
    state.budgets.max_dispatches = state.usage.dispatches + 1
    from hoh.controller import DispatchError

    with pytest.raises(DispatchError):
        ctrl.run_iteration(state)

    saetze = _telemetrie(store)
    gescheitert = [s for s in saetze if s["outcome"] != "ok"]
    assert gescheitert, [s["outcome"] for s in saetze]
    letzte = gescheitert[-1]
    assert "dispatch refused" in letzte["detail"]
    assert letzte["witnessed_trees"] is None, (
        "it reported a coverage it never measured")
    assert letzte["witnessed_listings"] is None


def test_a_field_name_that_does_not_exist_does_not_discard_the_record(
        tmp_path, repo, spec):
    """`DispatchRecord` forbids extra fields, and `rest` went straight through.

    One plausible-but-wrong keyword made the whole line vanish -- silently,
    when no state was passed. A record lost to a typo is the same defect as a
    well-typed blank.
    """
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()
    zustand = Controller.new_state(
        run_id="r", repo_path=repo, project_name="p", spec_path=spec,
        budgets=Budgets(max_iterations=1))

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", usage={},
                    state=zustand, tokens=123)

    (satz,) = c.telemetry().read()
    assert satz.role == "planner"
    assert any("no such field" in h for h in zustand.history)


def test_a_role_outside_the_enum_keeps_its_configured_identity(tmp_path, repo, spec):
    """It got `NOT_AVAILABLE` while the answer sat in the same object."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    class Versender:
        profiles = {"gate": "claude"}
        models = {"gate": "claude-opus-5"}
        efforts = {"gate": "high"}

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = Versender()

    c.note_dispatch(role="gate", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", usage={})

    (satz,) = c.telemetry().read()
    assert satz.provider == "claude"
    assert satz.model == "claude-opus-5"
    assert satz.effort == "high"


def test_the_check_commands_home_is_not_a_protected_tree(tmp_path, repo, spec):
    """Those are pip and pytest caches, measured at up to 118 MB each.

    Digesting them twice per dispatch was most of what a witness cost, and they
    are not evidence about anything.
    """
    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    arenen = store.arenas_dir
    arenen.mkdir(parents=True, exist_ok=True)
    (arenen / "aaaa1111").mkdir()
    (arenen / "aaaa1111.scratch").mkdir()
    (arenen / "aaaa1111.scratch.v20260913T000000Z").mkdir()
    (arenen / ".hoh-scratch-r1-i1-a1-K1").mkdir()

    namen = {Path(x).name for x in _policy(ctrl, Role.PLANNER, state).protected}
    assert "aaaa1111" in namen
    assert not any(".scratch" in n for n in namen), sorted(namen)


def test_a_gitignored_file_in_the_repository_is_not_a_violation(
        tmp_path, repo, spec):
    """The witness must not be stricter than the run's own freeze check.

    `unchanged()` binds through `git write-tree`, which honours `.gitignore`,
    so a background process writing `__pycache__` into the repository failed an
    iteration the freeze semantics consider untouched. Files git ignores cannot
    reach a candidate -- `materialize()` uses `git archive` -- so they cannot
    affect a measurement.
    """
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore bytecode")

    def hintergrund(state: RunState) -> None:
        (repo / "__pycache__").mkdir(exist_ok=True)
        (repo / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00\x01")

    d = Dispatcher(repo=repo, qa_effect=hintergrund)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


def test_a_retry_spends_a_dispatch_and_the_budget_sees_it(tmp_path, repo, spec):
    """"Check budget and deadline before every dispatch; count retries in."

    The counter sat outside the retry loop, so a run could call the provider
    again past a budget that had already refused it. A retry is a provider
    call, and the budget is about provider calls.
    """
    from hoh.contracts import Role as R
    from hoh.controller import DispatchError

    class Wackelig(Dispatcher):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.aufrufe = 0

        def dispatch(self, role, prompt, *, state):
            self.aufrufe += 1
            if role is R.PLANNER and self.aufrufe <= 3:
                raise DispatchError("provider timeout", transient=True)
            return super().dispatch(role, prompt, state=state)

    d = Wackelig(repo=repo)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets.max_dispatches = 2
    state.budgets.max_transient_retries = 5

    with pytest.raises(DispatchError) as exc:
        ctrl.run_iteration(state)

    assert "budget" in str(exc.value).lower()
    assert state.usage.dispatches == 2, (
        f"the retries did not spend budget: {state.usage.dispatches}")
    assert d.aufrufe <= 2, "the provider was called past a spent budget"


def test_a_non_dispatch_record_contributes_no_provider_calls(tmp_path, repo, spec):
    """"gate", "merge" and "closure" reach no provider.

    They are documented roles of `DispatchRecord` and they do not reset the
    per-role call counter, so filling `provider_calls` from it would attach
    the previous role's calls to a line that made none -- and the sum over
    the log, which is what the benchmark reports as a cell's cost, would
    count those calls twice.
    """
    import json

    d = Dispatcher(repo=repo)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl._letzte_aufrufe = 3
    ctrl.note_dispatch(role="gate", run_id=state.run_id, iteration=1,
                       attempt=1, started_at="t0", ended_at="t1", state=state)
    zeile = json.loads(
        (store.dir / "telemetry.jsonl").read_text().splitlines()[-1])
    assert zeile["role"] == "gate"
    assert zeile["provider_calls"] == 0


def test_a_retry_does_not_rebaseline_anything_but_the_state_file(
        tmp_path, repo, spec):
    """The window an adversarial review opened, closed.

    The retry branch has to persist its dispatch charge, `state.json` is
    protected, so the witness has to be told about that one write. The first
    version re-took the *whole* witness -- and a planted write into
    `checks.json` in the same window disappeared into the new baseline, the
    run continued with a hijacked acceptance suite, and nothing was recorded.
    """
    from hoh.capability import CapabilityWitness

    ctrl, state, store = build(tmp_path, repo, spec, Dispatcher(repo=repo))
    (store.dir / "checks.json").write_text('{"checks": []}', encoding="utf-8")
    p = ctrl._policy_for(Role.PLANNER, state)
    zeuge = CapabilityWitness.take(p)

    # What the controller does between attempts, and what a role must not get
    # away with in the same window.
    store.write_state(state)
    (store.dir / "checks.json").write_text('{"checks": ["hijacked"]}',
                                           encoding="utf-8")

    erneuert = zeuge.neu_bezeugen([store.state_path])
    assert erneuert == [str(store.state_path)]

    verletzt = zeuge.violations()
    assert any("checks.json" in v for v in verletzt), (
        "the write into the acceptance suite was absorbed by the re-witness")
    assert not any(v.endswith("state.json") or "state.json (" in v
                   for v in verletzt), (
        "the controller's own charge must not read as a violation")
