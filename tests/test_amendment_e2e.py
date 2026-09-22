"""Does an amendment actually reach a decision, or only a record?

`tests/test_amendment.py` has 22 tests, and every one of them is about
`amendment.py` in isolation: the chain validates, the kinds behave, a
`CLARIFY` that names affected criteria is refused. None of them touches a run.
The module was, until now, not called from anywhere -- a rule layer nothing
invokes is exactly the failure mode `assurance.py` describes one level up.

So this drives the whole path on a real controller:

    old spec -> candidate accepted on its evidence
             -> an acceptance-affecting amendment naming a criterion
             -> that criterion's old receipts stop counting
             -> the next candidate is refused until it is measured again
             -> it is measured again
             -> and only then does acceptance stand.

The negative control is the fifth step: a candidate that would otherwise pass,
whose plan quietly leaves the amended criterion out, must not be accepted.
Without that, "revalidation required" is a sentence rather than a gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hoh.amendment import AmendmentKind, AmendmentLedger, park_and_amend
from hoh.contracts import Budgets, Role, RunState, Stage
from hoh.controller import Controller
from hoh.store import RunStore

OLD = "# Spec\nadd(a, b) must add correctly.\n"
FRESH = "# Spec\nadd(a, b) must add correctly, and must reject a string.\n"


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
    p.write_text(OLD, encoding="utf-8")
    return p


class Dispatcher:
    """Roles whose plan and verdict the test decides, iteration by iteration."""

    def __init__(self, *, repo: Path, checks, develop=None, store=None):
        self.repo = repo
        self.checks = checks               # iteration -> list of check ids
        #: Preservation requirements accumulate, and QA has to answer every
        #: one of them -- an unanswered criterion is a missing verdict, not a
        #: pass. Without this the tests below would fail on preservation and
        #: never reach the gate they are about.
        self.store = store
        self.develop = develop or (
            lambda r, n: (r / f"step{n}.txt").write_text("ok\n", encoding="utf-8"))
        self.iterations_ = 0

    def _plan(self, state: RunState) -> dict:
        base = state.last_accepted_candidate or state.base_candidate
        ids = self.checks(state.iteration)
        return {
            "run_id": state.run_id, "iteration": state.iteration,
            "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
            "spec_digest": state.spec_digest, "objective": "keep add() correct",
            "targets": ["app.py"], "preserve": [],
            "acceptance_checks": [{
                "check_id": cid, "description": f"criterion {cid}",
                "command": f"test -f step{state.iteration}.txt",
                "expect_exit": 0, "preserves": False,
            } for cid in ids],
            "out_of_scope": [], "evidence_refs": [],
            "repair_only": False, "repair_reason": None,
        }

    def _qa(self, state: RunState) -> dict:
        ids = list(self.checks(state.iteration))
        if self.store is not None:
            for cid in self.store.read_checks():
                if cid not in ids:
                    ids.append(cid)
        return {
            "verdicts": [{
                "check_id": cid, "outcome": "PASS",
                "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-{cid}",
                "reproduction": "pytest", "note": "green",
            } for cid in ids],
            "open_gaps": [], "summary": "all good",
        }

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        if role is Role.PLANNER:
            return json.dumps(self._plan(state))
        if role is Role.DEVELOPER:
            self.iterations_ += 1
            self.develop(self.repo, state.iteration)
            return "done"
        return json.dumps(self._qa(state))

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def build(tmp_path: Path, repo: Path, spec: Path, dispatcher):
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="project", spec_path=spec,
        budgets=Budgets(max_iterations=9),
    )
    store.write_state(state)
    dispatcher.store = store
    return Controller(store, dispatcher, spec_path=spec), state, store


def amend(store, state, spec: Path, *, kind=AmendmentKind.CORRECT,
          affects=("K1",), amendment_id="A1"):
    """What `hoh amend` does, without the argument parsing."""
    chain = store.read_amendments(origin_digest=state.spec_digest)
    a = park_and_amend(
        spec, FRESH, run_id=state.run_id, amendment_id=amendment_id, kind=kind,
        actor="the captain", reason="the old text was wrong about strings",
        affected_criteria=list(affects),
        after_acceptance=state.last_accepted_candidate is not None,
        write_seq=state.write_seq,
    )
    chain = AmendmentLedger(run_id=chain.run_id, origin_digest=chain.origin_digest,
                            amendments=[*chain.amendments, a])
    store.write_amendments(chain)
    from hoh.amendment import text_digest

    state.spec_digest = text_digest(FRESH)
    store.write_state(state)
    return a


# --------------------------------------------------------------------------- #
# The path, end to end
# --------------------------------------------------------------------------- #


def test_the_whole_path_from_an_old_spec_to_a_correct_final_decision(
        tmp_path, repo, spec):
    """Six steps, and the fifth is the one that has to hold."""
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"] if i == 1 else ["K1", "K2"])
    ctrl, state, store = build(tmp_path, repo, spec, d)

    # 1. the old text, a candidate, accepted on its own evidence
    first = ctrl.run_iteration(state)
    assert first.accepted, first.reason
    assert state.last_accepted_candidate is not None
    old_receipts = sorted(p.stem for p in (store.dir / "receipts").glob("*.json"))
    assert any("K1" in n for n in old_receipts)

    # 2. an acceptance-affecting amendment naming K1
    a = amend(store, state, spec)
    assert a.acceptance_affecting()
    assert a.after_acceptance, "it reopens a decision already made, and says so"

    # 3. K1's old receipts stop counting
    chain = store.read_amendments()
    invalidated = chain.invalidated_receipts(old_receipts)
    assert invalidated, "the amendment invalidated nothing"
    assert all(v == "A1" for v in invalidated.values())
    assert chain.revalidation_needed() == {"K1"}

    # 4. the specification on disk is the new one, and the old one is readable
    assert spec.read_text() == FRESH
    assert Path(a.from_path).read_text() == OLD

    # 5. a candidate that does not re-measure K1 is refused
    d.checks = lambda i: ["K2"]
    second = ctrl.run_iteration(state)
    assert not second.accepted
    assert "K1" in second.reason
    assert "amended" in second.reason

    # 6. one that does re-measure it is accepted
    d.checks = lambda i: ["K1", "K2"]
    third = ctrl.run_iteration(state)
    assert third.accepted, third.reason
    assert state.stage is Stage.CHECKPOINTED
    assert state.last_accepted_candidate.candidate_id != first.candidate_id


# --------------------------------------------------------------------------- #
# The negative control: without revalidation, no acceptance may stand
# --------------------------------------------------------------------------- #


def test_a_plan_that_leaves_the_amended_criterion_out_is_not_accepted(
        tmp_path, repo, spec):
    """The gate, stated as its own test.

    Everything else about this candidate is fine -- the developer wrote, the
    check passed, QA agreed. It is refused for one reason, and the reason names
    the criterion.
    """
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    amend(store, state, spec)
    d.checks = lambda i: ["K9"]

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert "acceptance withheld" in out.reason
    assert "K1" in out.reason
    assert state.stage is not Stage.CHECKPOINTED


def test_measuring_the_criterion_without_planning_it_is_not_enough(
        tmp_path, repo, spec):
    """A verdict for a criterion the plan does not contain is not an answer.

    A plan that drops the criterion and a QA that mentions it anyway would
    otherwise satisfy the gate by talking about it -- the same shape as the
    preservation suite that went missing in O129.
    """
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    amend(store, state, spec)

    d.checks = lambda i: ["K2"]
    d._qa = lambda state: {
        "verdicts": [
            {"check_id": "K2", "outcome": "PASS",
             "receipt_id": f"r1-i{state.iteration}-a{state.attempt}-K2",
             "reproduction": "pytest", "note": "green"},
            {"check_id": "K1", "outcome": "PASS",
             "receipt_id": f"r1-i{state.iteration}-a{state.attempt}-K1",
             "reproduction": "claimed", "note": "still fine, honestly"},
        ],
        "open_gaps": [], "summary": "all good",
    }

    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert "K1" in out.reason


def test_an_amendment_that_affects_nothing_withholds_nothing(tmp_path, repo, spec):
    """A `CLARIFY` changes wording, and the run carries on.

    The positive control for the gate: if every amendment stopped a run, the
    feature would be one people route around, which is worse than not having
    it.
    """
    d = Dispatcher(repo=repo, checks=lambda i: [f"K{i}"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    amend(store, state, spec, kind=AmendmentKind.CLARIFY, affects=())
    assert store.read_amendments().revalidation_needed() == set()

    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


# --------------------------------------------------------------------------- #
# The run must not mistake a recorded amendment for a silent edit
# --------------------------------------------------------------------------- #


def test_an_unrecorded_edit_still_blocks_the_run(tmp_path, repo, spec):
    """The old protection has to survive the new feature.

    Editing the file without recording an amendment is the case immutability
    was defending against, and it still blocks.
    """
    d = Dispatcher(repo=repo, checks=lambda i: [f"K{i}"])
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    spec.write_text(FRESH, encoding="utf-8")          # no amendment recorded

    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert "specification has changed" in out.reason
    assert state.condition.value == "BLOCKED"


def test_a_recorded_amendment_does_not_block_the_run(tmp_path, repo, spec):
    """And the recorded one does not, which is the whole point."""
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    amend(store, state, spec)

    out = ctrl.run_iteration(state)
    assert "specification has changed" not in out.reason
    assert state.condition.value != "BLOCKED"


def test_the_chain_says_what_the_run_promised_at_each_point(tmp_path, repo, spec):
    """Two amendments, and the old text still readable at both ends."""
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    first = amend(store, state, spec, amendment_id="A1")
    spec.write_text(FRESH, encoding="utf-8")
    second_text = FRESH + "\nAnd it must be fast.\n"
    chain = store.read_amendments()
    second = park_and_amend(
        spec, second_text, run_id="r1", amendment_id="A2",
        kind=AmendmentKind.WIDEN, actor="the captain", reason="speed matters",
        affected_criteria=["K2"], after_acceptance=True, write_seq=state.write_seq)
    chain = AmendmentLedger(run_id="r1", origin_digest=chain.origin_digest,
                            amendments=[*chain.amendments, second])
    store.write_amendments(chain)

    assert chain.current_digest() == second.to_digest
    assert chain.revalidation_needed() == {"K1", "K2"}
    assert Path(first.from_path).read_text() == OLD
    assert Path(second.from_path).read_text() == FRESH
    assert "A1" in chain.report() and "A2" in chain.report()


# --------------------------------------------------------------------------- #
# What an adversarial review found after the first version of this file passed
# --------------------------------------------------------------------------- #


def test_the_amended_criterion_can_actually_be_measured_against_the_new_text(
        tmp_path, repo, spec):
    """The finding that made the first version of the gate hollow.

    `_checks_for` freezes a preserved criterion against redefinition, and an
    amendment's affected criteria are by construction ones that already
    passed -- so they are all preserved. The planner would re-plan the
    criterion with a new command, the frozen version would silently apply, and
    the receipt would record the **pre-amendment** command. The gate was
    satisfied by the old measurement wearing a new name.
    """
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    old = store.read_checks()["K1"].command
    assert old == "test -f step1.txt"

    amend(store, state, spec)

    # the chain is read at the top of the next iteration, which reopens K1;
    # the planner's new definition is then the one that counts
    d.checks = lambda i: ["K1"]
    assert ctrl.run_iteration(state).accepted

    assert any("reopened by amendment" in h for h in state.history)
    fresh = store.read_checks()["K1"].command
    assert fresh != old, (
        "the criterion was re-measured with its pre-amendment command, so the "
        "gate was answered by the measurement the amendment invalidated")
    assert not any("protected against redefinition: K1" in h
                   for h in state.history)


def test_the_planner_is_told_which_criteria_an_amendment_reopened(
        tmp_path, repo, spec):
    """The gate keys on the plan containing the criterion, and nothing said so.

    A real planner had to guess -- and the one thing it could not guess is that
    the criterion's old definition no longer applies.
    """
    seen_ = {}

    class Remembering(Dispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.PLANNER:
                seen_[state.iteration] = prompt
            return super().dispatch(role, prompt, state=state)

    d = Remembering(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    assert "reopened-by-an-amendment" not in seen_[1]

    amend(store, state, spec)
    ctrl.run_iteration(state)

    assert "reopened-by-an-amendment" in seen_[2]
    assert "K1" in seen_[2].split("/reopened-by-an-amendment")[1][:400]


def test_the_obligation_clears_once_it_has_been_answered(tmp_path, repo, spec):
    """It was recomputed from the chain every iteration, so it never cleared.

    A criterion re-measured in iteration 2 was demanded again in iteration 3,
    and forever after.
    """
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    amend(store, state, spec)

    d.checks = lambda i: ["K1"]
    assert ctrl.run_iteration(state).accepted
    assert "K1" in state.revalidated

    d.checks = lambda i: ["K7"]
    third = ctrl.run_iteration(state)
    assert "has not been measured against the new text" not in third.reason


def test_a_qa_outage_is_not_reported_as_an_unmeasured_criterion(
        tmp_path, repo, spec):
    """A quota problem is a missing verdict, not stalled substance.

    The gate ran ahead of the outage branch, so an amended run turned every
    provider failure into "the amendment's criterion was not measured".
    """
    from hoh.controller import DispatchError

    class Outage(Dispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.QA:
                raise DispatchError("quota exhausted", transient=False)
            return super().dispatch(role, prompt, state=state)

    d = Outage(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    ctrl.run_iteration(state)          # iteration 1 already cannot be accepted

    chain = AmendmentLedger(run_id="r1", origin_digest=state.spec_digest)
    store.write_amendments(chain)
    amend(store, state, spec)

    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert "has not been measured against the new text" not in out.reason


def test_a_chain_naming_another_run_is_ignored(tmp_path, repo, spec):
    """It is an unsigned file in a directory the roles can reach."""
    from hoh.amendment import text_digest

    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    a = amend(store, state, spec)
    foreign = AmendmentLedger(run_id="a-completely-different-run",
                            origin_digest=a.from_digest, amendments=[a])
    store.write_amendments(foreign)
    state.spec_digest = a.from_digest        # as if nothing had been amended
    store.write_state(state)

    out = ctrl.run_iteration(state)
    assert any("names run" in h for h in state.history)
    assert not out.accepted, "the edited spec is unaccounted for, so the run blocks"
    assert text_digest(spec.read_text()) != state.spec_digest


def test_a_chain_whose_parked_text_is_missing_is_ignored(tmp_path, repo, spec):
    """"The old text stays readable" is a promise, so it is checked."""
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    a = amend(store, state, spec)
    Path(a.from_path).rename(Path(a.from_path).with_suffix(".moved"))

    ctrl.run_iteration(state)
    assert any("superseded text it names is not there" in h for h in state.history)


def test_a_refused_amendment_leaves_the_specification_alone(tmp_path, repo, spec):
    """"Refused" has to be true of the filesystem too.

    A CLARIFY naming affected criteria is refused by the record's own
    validator -- and the specification had already been replaced by then, so
    the run blocked on a text nobody had amended and the obvious retry was
    refused for being identical to what the failed attempt had written.
    """
    before = spec.read_text()
    with pytest.raises(ValueError):
        park_and_amend(spec, FRESH, run_id="r1", amendment_id="A1",
                       kind=AmendmentKind.CLARIFY, actor="the captain",
                       reason="wording", affected_criteria=["K1"],
                       after_acceptance=False, write_seq=0)
    assert spec.read_text() == before
    assert not list(spec.parent.glob(f"{spec.name}.v*"))


# --------------------------------------------------------------------------- #
# The command itself, which no test executed
# --------------------------------------------------------------------------- #


def test_the_amend_command_records_a_chain_and_parks_the_old_text(
        tmp_path, repo, spec, capsys):
    """33 of the 35 changed lines of `cmd_amend` were executed by nothing.

    The tests drove the controller; the user-facing command was never run, so
    every argument name, every refusal path and the report it prints were
    unchecked.
    """
    from hoh import cli

    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    fresh = tmp_path / "neu.md"
    fresh.write_text(FRESH, encoding="utf-8")
    rc = cli.main([
        "--root", str(tmp_path / "runs"), "amend", "r1",
        "--spec-file", str(fresh), "--amendment-id", "A1", "--kind", "correct",
        "--actor", "the captain", "--reason", "the old text was wrong",
        "--affects", "K1",
    ])
    assert rc == 0
    output = capsys.readouterr().out
    assert "A1" in output
    assert "K1" in output

    chain = store.read_amendments()
    assert [a.amendment_id for a in chain.amendments] == ["A1"]
    assert chain.revalidation_needed() == {"K1"}
    assert spec.read_text() == FRESH
    assert Path(chain.amendments[0].from_path).read_text() == OLD


def test_the_amend_command_refuses_a_clarify_that_names_criteria(
        tmp_path, repo, spec, capsys):
    """And leaves the specification alone while refusing."""
    from hoh import cli

    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    before = spec.read_text()

    fresh = tmp_path / "neu.md"
    fresh.write_text(FRESH, encoding="utf-8")
    rc = cli.main([
        "--root", str(tmp_path / "runs"), "amend", "r1",
        "--spec-file", str(fresh), "--amendment-id", "A1", "--kind", "clarify",
        "--actor", "the captain", "--reason", "wording", "--affects", "K1",
    ])

    assert rc == 2
    assert "refused" in capsys.readouterr().err
    assert spec.read_text() == before
    assert not store.amendments_path.exists()


def test_the_amend_command_refuses_a_duplicate_id(tmp_path, repo, spec, capsys):
    from hoh import cli

    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    for text, rc_expected in ((FRESH, 0), (FRESH + "\nmore\n", 2)):
        fresh = tmp_path / "neu.md"
        fresh.write_text(text, encoding="utf-8")
        rc = cli.main([
            "--root", str(tmp_path / "runs"), "amend", "r1",
            "--spec-file", str(fresh), "--amendment-id", "A1", "--kind", "widen",
            "--actor", "the captain", "--reason", "more", "--affects", "K2",
        ])
        assert rc == rc_expected, capsys.readouterr()
    assert len(store.read_amendments().amendments) == 1


def test_removing_the_chain_does_not_remove_the_obligation(tmp_path, repo, spec):
    """The chain is one unsigned file in a directory.

    Renaming it aside -- a prune, a park, a hand -- made the next iteration
    accept a candidate that never planned or measured the amended criterion,
    while the run's digest still carried the amended text. A thinner chain was
    indistinguishable from no chain, so the run counts what it has seen.
    """
    d = Dispatcher(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted

    amend(store, state, spec)
    ctrl.run_iteration(state)                    # the run sees the amendment
    assert state.amendments_seen == 1

    store.amendments_path.rename(
        store.amendments_path.with_suffix(".json.parked"))

    out = ctrl.run_iteration(state)

    assert not out.accepted
    assert "incomplete" in out.reason
    assert state.condition.value == "BLOCKED"


def test_a_run_that_never_saw_an_amendment_is_unaffected(tmp_path, repo, spec):
    """The counter must not turn an ordinary run into a blocked one."""
    d = Dispatcher(repo=repo, checks=lambda i: [f"K{i}"])
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    assert state.amendments_seen == 0
    assert ctrl.run_iteration(state).accepted


def test_the_planner_stops_being_told_about_a_criterion_it_has_answered(
        tmp_path, repo, spec):
    """A prompt that asks for work already done is one a role has to guess
    its way past."""
    seen_ = {}

    class Remembering(Dispatcher):
        def dispatch(self, role, prompt, *, state):
            if role is Role.PLANNER:
                seen_[state.iteration] = prompt
            return super().dispatch(role, prompt, state=state)

    d = Remembering(repo=repo, checks=lambda i: ["K1"])
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    amend(store, state, spec)

    assert ctrl.run_iteration(state).accepted          # answers K1
    assert "K1" in state.revalidated

    d.checks = lambda i: ["K5"]
    ctrl.run_iteration(state)

    last_ = max(seen_)
    assert "reopened-by-an-amendment" not in seen_[last_]
