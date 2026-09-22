"""What a fresh orchestrator session decides, holding nothing but the state file.

This is the test that matters for the orchestration layer, and the reason is
worth stating rather than assuming. The campaign that produced this project's
own release ran its orchestration as an agent session's behaviour: the session
knew which runs existed, what each had produced, what the gates had said and
what still had to happen. None of that was written anywhere a *second* session
could read. If the session had ended, recovery meant a person reconstructing
intent from git history.

So every test below kills the session at a different point and asks the same
question of what survives on disk: `resume / retry / evaluate / block / repair
/ closed`. A state that cannot answer it unambiguously is a state that needs a
human, and the tests say so rather than picking a plausible answer.

Two rules are load-bearing throughout:

* An unclear state **blocks**. It does not restart. Restarting on an unclear
  state is how work gets done twice or silently lost.
* `NOT_RUN` is never green. A closure that checked nothing has established
  nothing, and an empty gate set is the purest form of that mistake.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from hoh.project import (
    ActionClass,
    DecisionKind,
    DecisionRecord,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
    digest_obj,
)
from hoh.projectstore import (
    BLOCK,
    CLOSED,
    EVALUATE,
    REPAIR,
    RESUME,
    RETRY,
    ProjectStore,
    list_projects,
    resume_decision,
)
from hoh.store import LockBusy, StaleWrite, StoreError


def projekt(tmp_path: Path, **kw) -> ProjectState:
    return ProjectState(project_id=kw.pop("project_id", "p"), repo_path=str(tmp_path), **kw)


def gate(name: str, outcome: GateOutcome = GateOutcome.GREEN, subject: str = "abc1234") -> GateResult:
    return GateResult(name=name, outcome=outcome, subject=subject, exit_code=0 if outcome is GateOutcome.GREEN else 1)


# --------------------------------------------------------------------------- #
# The ten fault-injection points
# --------------------------------------------------------------------------- #

def test_a_kill_during_planning_leaves_the_node_ready(tmp_path):
    """1. Killed before the run started: the node is still READY, resume it.

    Nothing was dispatched, so there is nothing to evaluate and nothing to
    undo. This is the only kill point with an unambiguous cheap answer.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a")]
    verdict, why = resume_decision(s)
    assert verdict == RESUME, why
    assert "a" in why


@pytest.mark.parametrize("phase", ["development", "qa", "kurz vor dem Merge"])
def test_a_kill_during_a_run_is_evaluated_not_guessed(tmp_path, phase):
    """2, 3, 4. Killed mid-run: RUNNING with no live controller is ambiguous.

    Whether the work finished, crashed halfway through a merge, or is still
    going in a process this session cannot see is *not* readable from project
    state. All three kill points therefore produce the same verdict, and it is
    EVALUATE rather than RESUME: re-dispatching a run that may already have
    merged is how a candidate gets applied twice.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.RUNNING, run_id="a", note=phase)]
    verdict, why = resume_decision(s)
    assert verdict == EVALUATE, why
    assert "RUNNING" in why
    assert "crashed" in why or "finished" in why


def test_a_kill_after_the_merge_and_before_global_closure_demands_a_repair(tmp_path):
    """5. Killed after the merge, before global closure ran.

    Every node has settled, so the DAG is terminal -- and this is exactly the
    state that must not be read as done. No gate has run, so nothing has been
    established about the merged result. `DAG_TERMINAL != RC_CLOSED`.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.dag_terminal()
    assert not s.rc_closed()
    verdict, why = resume_decision(s)
    assert verdict == REPAIR, why
    assert "no global gate has run" in why


def test_a_kill_while_the_repair_node_is_being_created(tmp_path):
    """6. Killed while creating a repair node: the half-made node is READY.

    The gate that produced it is on record as red, the repair node exists and
    has not run. The next session picks it up rather than re-deriving whether
    a repair is needed -- the decision record is what makes that safe.
    """
    s = projekt(tmp_path)
    s.nodes = [
        TaskNode(id="a", lifecycle=Lifecycle.MERGED),
        TaskNode(id="rep1", repair_of="a", closure_generation=1),
    ]
    s.gates = [gate("union", GateOutcome.RED)]
    s.closure_generation = 1
    verdict, why = resume_decision(s)
    assert verdict == RESUME, why
    assert "rep1" in why
    assert not s.rc_closed(), "an outstanding repair node is not a closed release"


def test_a_fresh_orchestrator_with_no_context_decides_the_same(tmp_path):
    """7. Restart with a completely fresh context.

    The point of the whole module: a session that has never seen this project
    reads the file and reaches the same verdict as the one that wrote it. No
    conversation, no memory, no inference from git.
    """
    load_ = ProjectStore(tmp_path, "p")
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED), TaskNode(id="b", dependencies=["a"])]
    load_.create(s)

    # A different store object, as a different process would construct it.
    fresh_ = ProjectStore(tmp_path, "p").read_state()
    assert resume_decision(fresh_) == resume_decision(s)
    assert resume_decision(fresh_)[0] == RESUME


def test_a_second_orchestrator_start_is_refused(tmp_path):
    """8. Two controllers: the second is refused, not queued behind the first."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    with s.lock():
        second_one = ProjectStore(tmp_path, "p")
        with pytest.raises(LockBusy) as exc:
            with second_one.lock():
                pass
    assert "already held" in str(exc.value)
    # Released afterwards -- a crashed orchestrator must not wedge the project.
    with ProjectStore(tmp_path, "p").lock():
        pass


def test_a_provider_outage_is_not_a_verdict(tmp_path):
    """9. A provider 429/529 leaves the node BLOCKED, and blocked is not failed.

    The distinction matters because a quota exhaustion looks, from inside the
    loop, exactly like a role that broke its contract. Recording it as a
    rejection would burn the node's retry budget on an outage.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="provider 429")]
    verdict, why = resume_decision(s)
    assert verdict == BLOCK, why
    assert "outside the orchestrator" in why
    assert s.nodes[0].rejections == 0, "an outage is not a rejection"


def test_a_damaged_state_blocks_instead_of_overwriting(tmp_path):
    """10. Corrupted or incomplete state: unreadable blocks, it is not replaced."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    progress_ = s.read_state()
    progress_.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.write_state(progress_)          # now a predecessor exists to fall back to

    s.state_path.write_text('{"project_id": "p", "nodes": [ {"id": ', encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        s.read_state()
    assert "unreadable" in str(exc.value)

    # The damaged state blocks; it is not silently replaced. And the way back
    # is on disk, because writing parks the predecessor rather than deleting
    # it. Note what is *not* claimed: the very first write of a project has no
    # predecessor, so "there is always a way back" would be false. What holds
    # is that no write ever destroys one that existed.
    parked = s.parked_states()
    assert parked, "the parked predecessor is the way back"
    back = ProjectState.model_validate_json(parked[-1].read_text())
    assert back.project_id == "p"


# --------------------------------------------------------------------------- #
# Storage discipline
# --------------------------------------------------------------------------- #

def test_a_stale_writer_is_refused(tmp_path):
    """The lock serializes; only the sequence number catches a stale writer.

    A crashed orchestrator releases its lock when its process dies, so the next
    session can hold the lock legitimately while carrying a state it read
    before the crash. Fencing is what makes that safe.
    """
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    old = s.read_state()          # what a crashed session would still hold
    fresh = s.read_state()
    fresh.nodes = [TaskNode(id="a")]
    s.write_state(fresh)            # the live session advances
    old.nodes = [TaskNode(id="z")]
    with pytest.raises(StaleWrite) as exc:
        s.write_state(old)
    assert "reload instead of overwriting" in str(exc.value)
    assert [n.id for n in s.read_state().nodes] == ["a"]


def test_a_write_never_deletes_its_predecessor(tmp_path):
    """Replace by versioned rename, never delete -- and the counter never
    collides, even when earlier versions are moved away."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    for i in range(3):
        st = s.read_state()
        st.nodes = [TaskNode(id=f"n{i}")]
        s.write_state(st)
    parked = s.parked_states()
    assert len(parked) == 3, parked
    versions_ = sorted(int(p.name.split(".v")[1].split(".")[0]) for p in parked)
    assert versions_ == [1, 2, 3]

    # Archive the earliest, as pruning would, then write again: a count-based
    # counter would now reuse v3 and collide.
    archiv = s.dir / "attic"
    archiv.mkdir()
    parked[0].rename(archiv / parked[0].name)
    st = s.read_state()
    st.nodes = [TaskNode(id="danach")]
    s.write_state(st)
    fresh = [int(p.name.split(".v")[1].split(".")[0]) for p in s.parked_states()]
    assert max(fresh) == 4, f"counter jumped backwards: {fresh}"


def test_start_is_idempotent(tmp_path):
    """A retried start returns the existing project rather than replacing it.

    Retrying a command is the most ordinary thing a resuming session does; a
    start that overwrote would make it data loss.
    """
    s = ProjectStore(tmp_path, "p")
    first_ = projekt(tmp_path)
    first_.nodes = [TaskNode(id="a")]
    s.create(first_)
    wieder = s.create(projekt(tmp_path, project_id="p"))
    assert [n.id for n in wieder.nodes] == ["a"]


def _hold_lock(path: str, ready_, further):
    store = ProjectStore(path, "p")
    with store.lock():
        ready_.set()
        further.wait(timeout=10)


def test_the_lock_holds_across_process_boundaries(tmp_path):
    """The lock is a real file lock, not an in-process convention."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    ctx = multiprocessing.get_context("fork")
    ready_, further = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready_, further))
    p.start()
    try:
        assert ready_.wait(timeout=10), "child never took the lock"
        with pytest.raises(LockBusy):
            with s.lock():
                pass
    finally:
        further.set()
        p.join(timeout=10)
    with s.lock():
        pass


# --------------------------------------------------------------------------- #
# Closure is a fixpoint, not the end of a list
# --------------------------------------------------------------------------- #

def test_a_terminal_dag_is_not_a_closed_release(tmp_path):
    """The distinction this project had to learn twice, as an assertion."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.dag_terminal()
    assert not s.rc_closed()

    s.gates = [gate("union"), gate("claims")]
    s.measurement_head = "abc1234"
    assert s.rc_closed()
    assert resume_decision(s)[0] == CLOSED


def test_not_run_never_counts_as_green(tmp_path):
    """A gate that did not run is not a gate that passed."""
    g = gate("union", GateOutcome.NOT_RUN)
    assert not g.counts_as_green
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.gates = [g]
    assert not s.gates_green()
    assert not s.rc_closed()
    verdict, why = resume_decision(s)
    assert verdict == REPAIR
    assert "union" in why


def test_an_empty_gate_set_is_not_green(tmp_path):
    """Zero gates is not green. Returning True for an empty set is the purest
    form of the failure this project exists to prevent."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.gates == []
    assert not s.gates_green()


def test_an_open_repair_node_prevents_closure(tmp_path):
    """Green gates plus an outstanding repair node is still not closed."""
    s = projekt(tmp_path)
    s.nodes = [
        TaskNode(id="a", lifecycle=Lifecycle.MERGED),
        TaskNode(id="rep1", repair_of="a", lifecycle=Lifecycle.READY),
    ]
    s.gates = [gate("union")]
    assert not s.dag_terminal()
    assert not s.rc_closed()
    s.nodes[1].lifecycle = Lifecycle.MERGED
    assert s.rc_closed()


def test_only_the_most_recent_result_per_gate_counts(tmp_path):
    """A gate that was red and is now green counts as green -- and the reverse."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.gates = [gate("union", GateOutcome.RED), gate("union", GateOutcome.GREEN)]
    assert s.gates_green()
    s.gates.append(gate("union", GateOutcome.RED))
    assert not s.gates_green()


# --------------------------------------------------------------------------- #
# The DAG itself
# --------------------------------------------------------------------------- #

def test_an_unknown_dependency_blocks_instead_of_starting(tmp_path):
    """A dependency on a node that does not exist reads, to a scheduler,
    exactly like a satisfied one. It must not."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="b", dependencies=["gibtesnicht"])]
    assert s.ready() == []
    assert s.unknown_dependencies() == {"b": ["gibtesnicht"]}
    verdict, why = resume_decision(s)
    assert verdict == BLOCK
    assert "gibtesnicht" in why


def test_an_abandoned_node_does_not_block_its_successors(tmp_path):
    """ABANDONED settles a node: a dependent may proceed.

    Otherwise one abandoned node wedges every path behind it, and the only way
    out is editing state by hand.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.ABANDONED), TaskNode(id="b", dependencies=["a"])]
    assert [n.id for n in s.ready()] == ["b"]


def test_a_rejection_is_input_to_the_next_iteration(tmp_path):
    """A rejected node returns as READY, and the verdict says retry, not resume."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", rejections=2)]
    verdict, why = resume_decision(s)
    assert verdict == RETRY, why
    assert "not an endpoint" in why


def test_an_external_action_stays_recognisable_as_one(tmp_path):
    """The action class survives a restart: it is state, not a judgement the
    next session has to make again."""
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="push", action_class=ActionClass.EXTERNAL)]
    s.create(st)
    assert s.read_state().nodes[0].action_class is ActionClass.EXTERNAL


# --------------------------------------------------------------------------- #
# Decisions are records, not remembered intentions
# --------------------------------------------------------------------------- #

def test_a_decision_is_bound_to_its_content(tmp_path):
    """A decision record's digest is derived from its payload, and a mismatch
    is refused: a rebound record is a new record, not an edited one."""
    d = DecisionRecord(
        id="D1",
        kind=DecisionKind.MERGE_RELEASE,
        actor="orchestrator",
        reason="every criterion passed and one was proven red on the predecessor",
        payload={"node": "a", "candidate": "deadbeef"},
    )
    assert d.payload_digest == digest_obj({"node": "a", "candidate": "deadbeef"})

    with pytest.raises(ValueError) as exc:
        DecisionRecord(
            id="D2",
            kind=DecisionKind.MERGE_RELEASE,
            actor="orchestrator",
            reason="x",
            payload={"node": "a"},
            payload_digest="0000000000000000",
        )
    assert "does not match its payload" in str(exc.value)


def test_the_digest_is_order_independent(tmp_path):
    """Same decision, different key order, same digest -- otherwise binding is
    decorative."""
    assert digest_obj({"a": 1, "b": 2}) == digest_obj({"b": 2, "a": 1})
    assert digest_obj({"a": 1}) != digest_obj({"a": 2})


def test_decisions_survive_the_restart(tmp_path):
    """The record of *why* survives, not just the resulting state.

    This is what a fresh session cannot otherwise recover: the tree shows that
    a node was abandoned, never that it was abandoned because its premise had
    been disproved.
    """
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.ABANDONED)]
    st.decisions = [
        DecisionRecord(
            id="D1",
            kind=DecisionKind.ABANDON_NODE,
            actor="orchestrator",
            reason="premise disproved by the d8b gate failure; re-running would repeat it",
            payload={"node": "a"},
            evidence=["gate:union@abc1234"],
        )
    ]
    s.create(st)
    back = ProjectStore(tmp_path, "p").read_state()
    assert back.decisions[0].reason.startswith("premise disproved")
    assert back.decisions[0].payload_digest == st.decisions[0].payload_digest


def test_a_gate_result_carries_its_subject(tmp_path):
    """A gate result names the commit it ran against.

    A green recorded without its subject cannot later be told apart from a
    green that has gone stale -- this project shipped a release whose figures
    were wrong for exactly that reason.
    """
    g = gate("union", subject="30ff053")
    assert g.subject == "30ff053"
    with pytest.raises(Exception):
        GateResult(name="union", outcome=GateOutcome.GREEN)  # type: ignore[call-arg]


def test_projects_are_listed(tmp_path):
    assert list_projects(tmp_path) == []
    ProjectStore(tmp_path, "eins").create(projekt(tmp_path, project_id="eins"))
    ProjectStore(tmp_path, "zwei").create(projekt(tmp_path, project_id="zwei"))
    assert list_projects(tmp_path) == ["eins", "zwei"]


def test_the_state_is_plain_json_and_can_be_read_back(tmp_path):
    """The state file is readable by something that is not this code.

    A durable state that only its own writer can parse is a slightly more
    durable conversation.
    """
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", writes=["README.md"], semantic_reads=["tools/x.py"])]
    st.gates = [gate("union")]
    s.create(st)
    raw_ = json.loads(s.state_path.read_text())
    assert raw_["nodes"][0]["writes"] == ["README.md"]
    assert raw_["gates"][0]["subject"] == "abc1234"
    assert raw_["schema_version"] >= 1


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #

def test_the_cli_reports_an_unreadable_state_with_its_own_exit_code(tmp_path, capsys):
    """A script reading `hoh project` must not see 0 for a damaged state.

    Three distinct codes, because the remedies differ: 0 answered, 2 no such
    project, 3 the state exists and cannot be trusted.
    """
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 0
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    assert main(["--root", str(tmp_path), "project", "status", "gibtesnicht"]) == 2

    progress_ = s.read_state()
    progress_.nodes = [TaskNode(id="a")]
    s.write_state(progress_)
    s.state_path.write_text("{ not json", encoding="utf-8")
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 3


def test_cli_resume_is_machine_readable(tmp_path, capsys):
    """`resume` prints JSON: the verdict is for a caller, not only a reader."""
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    st.gates = [gate("union")]
    st.measurement_head = "abc1234"
    s.create(st)

    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    data_ = json.loads(capsys.readouterr().out)
    assert data_["verdict"] == CLOSED
    assert data_["rc_closed"] is True
    assert data_["measurement_head"] == "abc1234"


def test_cli_list_shows_every_project_with_its_verdict(tmp_path, capsys):
    from hoh.cli import main

    ProjectStore(tmp_path, "eins").create(projekt(tmp_path, project_id="eins"))
    ProjectStore(tmp_path, "zwei").create(projekt(tmp_path, project_id="zwei"))
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "list"]) == 0
    output = capsys.readouterr().out
    assert "eins" in output and "zwei" in output


# --------------------------------------------------------------------------- #
# A blocked node needs a way back, and it has to leave a record
# --------------------------------------------------------------------------- #

def test_unblock_demands_a_reason_and_writes_it_down(tmp_path):
    """Refusing to decide was right; leaving no way back is not.

    Without this the only route out of a blocked project is editing the state
    file by hand -- the one thing a durable, digest-bound state exists to make
    unnecessary. The reason is required, because "someone unblocked it" with no
    why is exactly the unrecoverable intent this layer was built to stop losing.
    """
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED,
                         note="accepted but the candidate did not land")]
    s.create(st)

    after = unblock(s, "a", "untracked build output removed; the merge can land now")
    n = after.node("a")
    assert n.lifecycle is Lifecycle.READY
    # The original reason stays readable: what it was blocked for matters after
    # it is running again.
    assert "did not land" in n.note
    assert "untracked build output" in n.note
    d = after.decisions[-1]
    assert d.actor == "human"
    assert "untracked build output" in d.reason
    assert d.payload["was_blocked_for"].startswith("accepted but")


def test_unblock_refuses_what_is_not_blocked(tmp_path):
    """Unblocking something that is not blocked would hide whatever it is
    actually doing."""
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]
    s.create(st)
    with pytest.raises(StoreError) as exc:
        unblock(s, "a", "because I said so")
    assert "not BLOCKED" in str(exc.value)


def test_unblock_does_not_know_unknown_nodes(tmp_path):
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    with pytest.raises(StoreError):
        unblock(s, "gibtesnicht", "x")


def test_cli_unblock_has_its_own_exit_code_for_a_refusal(tmp_path, capsys):
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="stuck")]
    s.create(st)

    assert main(["--root", str(tmp_path), "project", "unblock", "p", "a",
                 "--reason", "the obstruction is gone"]) == 0
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    # Doing it twice is refused, and not with 0.
    assert main(["--root", str(tmp_path), "project", "unblock", "p", "a",
                 "--reason", "again"]) == 4


# --------------------------------------------------------------------------- #
# Repository changes made outside the loop
# --------------------------------------------------------------------------- #

def test_an_external_change_is_recorded_with_both_heads(tmp_path):
    """The gap the first real end-to-end run exposed.

    A merge conflict was resolved by a person, in git, and nothing about it
    reached project state: the tree changed, a later closure measured the
    changed tree, and the state carried no trace of why it looked that way.
    """
    from hoh.projectstore import record_external_action

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED)]
    s.create(st)

    after = record_external_action(
        s, actor="human", reason="resolved a modify/delete conflict on build output",
        node="a", head_before="abc1234", head_after="def5678",
    )
    r = after.external_actions[-1]
    assert r.actor == "human"
    assert r.head_before == "abc1234" and r.head_after == "def5678"
    assert r.changed_the_tree is True
    assert r.node == "a"
    # Placed in the state's own history, not only in wall-clock time.
    assert r.write_seq >= 1


def test_external_changes_do_not_sit_among_the_decisions(tmp_path):
    """A decision is something this system chose; an external action is
    something that happened to it. Collapsing them would let the record imply
    authorship it does not have."""
    from hoh.projectstore import record_external_action

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    after = record_external_action(s, actor="operator", reason="cleaned build output")
    assert len(after.external_actions) == 1
    assert after.decisions == []


def test_a_change_that_moves_nothing_is_still_a_change(tmp_path):
    """An aborted merge is worth recording, and it is a different thing from
    one that landed -- a reader should not have to compare digits to find out.
    """
    from hoh.projectstore import record_external_action

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    after = record_external_action(
        s, actor="operator", reason="attempted a merge, aborted it",
        head_before="abc1234", head_after="abc1234",
    )
    assert after.external_actions[-1].changed_the_tree is False


def test_an_external_change_to_an_unknown_node_is_refused(tmp_path):
    from hoh.projectstore import record_external_action

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    with pytest.raises(StoreError):
        record_external_action(s, actor="human", reason="x", node="gibtesnicht")


def test_external_changes_survive_the_restart(tmp_path):
    from hoh.projectstore import record_external_action

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    record_external_action(s, actor="human", reason="resolved a conflict by hand",
                          head_before="a1", head_after="b2")
    back = ProjectStore(tmp_path, "p").read_state()
    assert back.external_actions[-1].reason == "resolved a conflict by hand"
    assert back.external_actions[-1].action_id == "X0001"


def test_cli_record_action_measures_the_head_itself(tmp_path, capsys):
    """The head after is measured, not typed: a record whose numbers came from
    the person being recorded proves nothing."""
    import subprocess

    from hoh.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    real_one = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()

    s = ProjectStore(tmp_path, "p")
    st = ProjectState(project_id="p", repo_path=str(repo))
    s.create(st)

    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "record-action", "p",
                 "--actor", "human", "--reason", "resolved a conflict",
                 "--head-before", "0000000"]) == 0
    r = s.read_state().external_actions[-1]
    assert r.head_after == real_one, f"{r.head_after} != {real_one}"
    assert r.head_before == "0000000"


# --------------------------------------------------------------------------- #
# A node that should never have been started
# --------------------------------------------------------------------------- #


def _project_with_nodes(tmp_path, lifecycle):
    from hoh.project import ActionClass, ProjectState, TaskNode
    from hoh.projectstore import ProjectStore

    store = ProjectStore(tmp_path / "root", "p")
    store.write_state(ProjectState(
        project_id="p", repo_path=str(tmp_path / "repo"),
        nodes=[TaskNode(
            id="repair-1-1", spec_path="s.md", spec_digest="d",
            lifecycle=lifecycle, action_class=ActionClass.INTERNAL,
            repair_of="a", note="the suite failed on the merged state",
        )],
    ))
    return store


def test_a_node_can_be_abandoned_with_a_reason(tmp_path):
    """The orchestrator abandons a node by itself only after repeated
    rejections. That covers work that keeps failing; it does not cover work
    that should never have been started -- a repair node created for a gate
    failure that came from the harness measuring the wrong tree."""
    from hoh.project import Lifecycle
    from hoh.projectstore import abandon

    store = _project_with_nodes(tmp_path, Lifecycle.READY)
    st = abandon(store, "repair-1-1",
                 "the gate failure it repairs came from the driver, not the "
                 "merged state, which was green", actor="main-session")
    task = st.node("repair-1-1")
    assert task.lifecycle is Lifecycle.ABANDONED
    assert task.settled
    assert "abandoned" in task.note
    assert "the suite failed" in task.note, "the original note was overwritten"


def test_abandoning_records_who_and_why(tmp_path):
    from hoh.project import DecisionKind, Lifecycle
    from hoh.projectstore import abandon

    store = _project_with_nodes(tmp_path, Lifecycle.BLOCKED)
    st = abandon(store, "repair-1-1", "no defect to repair", actor="captain")
    e = st.decisions[-1]
    assert e.kind is DecisionKind.ABANDON_NODE
    assert e.actor == "captain"
    assert e.reason == "no defect to repair"
    assert e.payload["was"] == "BLOCKED"


def test_a_settled_node_is_not_re_settled(tmp_path):
    """Abandoning something already merged would rewrite a decision that has
    already had effects in the repository."""
    from hoh.project import Lifecycle
    from hoh.projectstore import StoreError, abandon

    store = _project_with_nodes(tmp_path, Lifecycle.MERGED)
    with pytest.raises(StoreError, match="already MERGED"):
        abandon(store, "repair-1-1", "x")


def test_abandoning_an_unknown_node_is_refused(tmp_path):
    from hoh.project import Lifecycle
    from hoh.projectstore import StoreError, abandon

    store = _project_with_nodes(tmp_path, Lifecycle.READY)
    with pytest.raises(StoreError, match="has no node"):
        abandon(store, "nope", "x")


def test_an_abandoned_node_no_longer_blocks_closure(tmp_path):
    """`rc_closed` requires every repair node to be settled. ABANDONED is a
    settled state -- that is what makes this a disposition rather than a way
    of ignoring the node."""
    from hoh.project import GateOutcome, GateResult, Lifecycle
    from hoh.projectstore import abandon

    store = _project_with_nodes(tmp_path, Lifecycle.READY)
    st = store.read_state()
    st.nodes[0].lifecycle = Lifecycle.READY
    st.gates.append(GateResult(name="suite", subject="a" * 12,
                               outcome=GateOutcome.GREEN, exit_code=0))
    store.write_state(st)
    assert not store.read_state().rc_closed()

    st = abandon(store, "repair-1-1", "the premise was false")
    assert st.rc_closed(), "an abandoned repair node still blocked closure"


def test_an_unevaluable_gate_is_not_green():
    """A gate that ran and could not be answered -- no `runs/` tree in the
    checkout, no namespace on the machine -- is a fact about the environment.
    It is not a failure and it is certainly not a pass: a project whose gate
    cannot be evaluated has not been shown to be closed."""
    from hoh.project import GateOutcome, GateResult

    for result in (GateOutcome.NOT_RUN, GateOutcome.UNSUPPORTED_ENVIRONMENT,
                     GateOutcome.RED):
        g = GateResult(name="claims", subject="a" * 12, outcome=result,
                       exit_code=1)
        assert not g.counts_as_green, result.value
    assert GateResult(name="claims", subject="a" * 12,
                      outcome=GateOutcome.GREEN, exit_code=0).counts_as_green


def test_an_unevaluable_gate_blocks_closure(tmp_path):
    from hoh.project import GateOutcome, GateResult, ProjectState

    st = ProjectState(project_id="p", repo_path=str(tmp_path))
    st.gates.append(GateResult(name="claims", subject="a" * 12,
                               outcome=GateOutcome.UNSUPPORTED_ENVIRONMENT,
                               exit_code=1))
    assert not st.gates_green()


# --------------------------------------------------------------------------- #
# A gate that no longer runs must not keep voting
# --------------------------------------------------------------------------- #


def _with_gates(tmp_path, *gates):
    from hoh.project import GateResult, ProjectState

    st = ProjectState(project_id="p", repo_path=str(tmp_path))
    for name, outcome, gen in gates:
        st.gates.append(GateResult(name=name, subject="a" * 12, outcome=outcome,
                                   exit_code=0, generation=gen))
    return st


def test_a_renamed_gate_does_not_block_closure_for_ever(tmp_path):
    """Measured on this project's own self-dogfood campaign, at closure
    generation 14. A gate called `claims` was split into four narrower gates.
    Its last result was RED, `gates_green` read the latest result per *name*
    across all of history, and the project could never close again -- blocked
    by a check that no longer ran."""
    from hoh.project import GateOutcome as G

    st = _with_gates(
        tmp_path,
        ("claims", G.RED, 1),
        ("suite", G.GREEN, 1),
        ("claims-schema", G.GREEN, 2),
        ("claims-ids", G.GREEN, 2),
        ("suite", G.GREEN, 2),
    )
    assert st.gates_green()
    assert st.retired_gates() == ["claims"], (
        "the gate that stopped running has to be named, not silently dropped"
    )


def test_a_red_gate_in_the_latest_pass_still_blocks(tmp_path):
    """The control. Scoping to the latest pass must not become a way for a
    failing gate to age out of relevance."""
    from hoh.project import GateOutcome as G

    st = _with_gates(tmp_path, ("suite", G.GREEN, 1), ("suite", G.RED, 2))
    assert not st.gates_green()


def test_results_from_before_the_field_existed_behave_as_they_did(tmp_path):
    """Everything written earlier carries generation 0. The fallback keeps
    those states reading exactly as they used to."""
    from hoh.project import GateOutcome as G

    st = _with_gates(tmp_path, ("suite", G.RED, 0), ("suite", G.GREEN, 0))
    assert st.gates_green(), "the later result still wins within one generation"
    assert st.retired_gates() == []

    st2 = _with_gates(tmp_path, ("suite", G.GREEN, 0), ("claims", G.RED, 0))
    assert not st2.gates_green()


def test_nothing_is_retired_when_every_gate_still_reports(tmp_path):
    from hoh.project import GateOutcome as G

    st = _with_gates(tmp_path, ("suite", G.GREEN, 1), ("suite", G.GREEN, 2))
    assert st.retired_gates() == []


def test_the_orchestrator_stamps_the_pass_on_every_result(tmp_path):
    """The field is only useful if the thing that writes gates fills it."""
    import inspect

    from hoh import orchestrator

    source = inspect.getsource(orchestrator)
    assert "g.generation = next_" in source
