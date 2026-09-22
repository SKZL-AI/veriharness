"""The product controller reaches the prototype's decisions, and then some.

`~/hoh-operator-tools/autopilot.py` is the loop that actually drove this
project's release campaign. It is the oracle: whatever else the product
control plane does, it must not decide *differently* from the thing that has
already been used in anger.

So this module runs both against the same synthetic scenarios and requires
their halt classes to agree, under an explicit mapping. Where they cannot
agree, the divergence is asserted deliberately and its reason stated -- a
parity suite that quietly tolerates a difference is worth nothing.

The prototype lives outside the package and does not ship. In the export
regime it is simply absent, and the parity tests skip rather than fail: an
oracle you do not have is not a failure, it is a missing oracle. The
product-only tests below do not skip, because they are about the shipped code.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from hoh.orchestrator import (
    GateRunner,
    HaltClass,
    ProjectController,
    RunLauncher,
    RunOutcome,
    RunVerdict,
)
from hoh.project import (
    ActionClass,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
)
from hoh.projectstore import ProjectStore

PROTOTYPE = Path(os.path.expanduser("~/hoh-operator-tools/autopilot.py"))

#: The prototype's vocabulary, in the product's terms. Written out rather than
#: inferred: an implicit mapping is where a parity claim quietly stops meaning
#: anything.
EQUIVALENT = {
    "FIXPUNKT": HaltClass.CLOSED,
    "HALT_EXTERN": HaltClass.BLOCKED_EXTERNAL,
    "HALT_BUDGET": HaltClass.BLOCKED_PROVIDER,
    "HALT_RUNDEN": HaltClass.ROUND_LIMIT,
    "HALT_TROCKEN": HaltClass.NOT_RUN,
    "HALT_UNKLAR": HaltClass.AMBIGUOUS,
}


def prototype_():
    if not PROTOTYPE.exists():
        pytest.skip(f"prototype oracle not present at {PROTOTYPE} (export regime)")
    spec = importlib.util.spec_from_file_location("autopilot_oracle", PROTOTYPE)
    module_ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module_)
    return module_


# --------------------------------------------------------------------------- #
# Fixtures shared by both sides
# --------------------------------------------------------------------------- #

class Start(RunLauncher):
    """A scripted outside world, identical in shape to the prototype's."""

    def __init__(self, verdicts=None, classes=None, depends=False, merge_lands=True,
                 evaluations=None):
        self.verdicts = dict(verdicts or {})
        self.classes = dict(classes or {})
        self.depends = depends
        self.merge_lands = merge_lands
        # What an already-dispatched run is found to have done. The default is
        # UNDETERMINED: a fixture that has dispatched nothing genuinely cannot
        # say, and defaulting to anything else would let the resume path look
        # more decisive than it is.
        self.evaluations = dict(evaluations or {})
        self.launched: list[str] = []
        self.merged: list[str] = []
        self.evaluated: list[str] = []

    def action_class(self, node: TaskNode) -> ActionClass:
        return self.classes.get(node.id, ActionClass.INTERNAL)

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        return self.depends

    def launch(self, node: TaskNode) -> RunOutcome:
        self.launched.append(node.id)
        v = self.verdicts.get(node.id, RunVerdict.ACCEPTED)
        if isinstance(v, list):
            v = v.pop(0) if v else RunVerdict.ACCEPTED
        return RunOutcome(verdict=v, candidate=f"{node.id}-cand")

    def accepted_baseline(self, node: TaskNode) -> str | None:
        return None

    def evaluate(self, node: TaskNode) -> RunOutcome:
        self.evaluated.append(node.id)
        v = self.evaluations.get(node.id, RunVerdict.UNDETERMINED)
        return RunOutcome(verdict=v, detail="from the recorded run state",
                          candidate=f"{node.id}-cand")

    def merge(self, node: TaskNode, outcome: RunOutcome) -> bool:
        if not self.merge_lands:
            return False
        self.merged.append(node.id)
        return True


class Gates(GateRunner):
    def __init__(self, sequence_=None, subject="abc1234"):
        self.sequence_ = list(sequence_ or [GateOutcome.GREEN])
        self._subject = subject
        self.calls = 0

    def subject(self) -> str:
        return self._subject

    def run(self, subject: str) -> list[GateResult]:
        i = min(self.calls, len(self.sequence_) - 1)
        self.calls += 1
        outcome_ = self.sequence_[i]
        return [GateResult(name="union", outcome=outcome_, subject=subject,
                           exit_code=0 if outcome_ is GateOutcome.GREEN else 1,
                           detail="" if outcome_ is GateOutcome.GREEN else "composition defect")]


def projekt(tmp_path, nodes, pid="p") -> ProjectStore:
    s = ProjectStore(tmp_path, pid)
    st = ProjectState(project_id=pid, repo_path=str(tmp_path))
    st.nodes = nodes
    s.create(st)
    return s


def fahre(tmp_path, nodes, *, verdicts=None, classes=None, gates=None,
          max_rounds=40, max_repairs=5, merge_lands=True, evaluations=None):
    s = projekt(tmp_path, nodes)
    start = Start(verdicts, classes, merge_lands=merge_lands, evaluations=evaluations)
    g = Gates(gates)
    c = ProjectController(s, start, g, max_rounds=max_rounds, max_repairs=max_repairs)
    return c.run(), start, s


# --------------------------------------------------------------------------- #
# Parity: the eight scenarios the prototype is exercised on
# --------------------------------------------------------------------------- #

def _prototype_runs(module_, node_list, *, verdict_list=None, gate_sequence=None, classes_=None,
                     max_runden=40):
    class Sim(module_.Ausfuehrer):
        def __init__(self):
            self.verdict_list = dict(verdict_list or {})
            self.gate_sequence = list(gate_sequence or [0])
            self.classes_ = dict(classes_ or {})
            self.started_ = []
            self.gate_calls = 0

        def klassifiziere(self, k):
            return self.classes_.get(k["id"], "INTERN")

        def semantische_abhaengigkeit(self, a, b):
            return False

        def starte_lauf(self, k):
            self.started_.append(k["id"])
            v = self.verdict_list.get(k["id"], "ACCEPTED")
            if isinstance(v, list):
                v = v.pop(0) if v else "ACCEPTED"
            return {"verdikt": v}

        def globale_gates(self):
            i = min(self.gate_calls, len(self.gate_sequence) - 1)
            self.gate_calls += 1
            return self.gate_sequence[i], "simuliert"

    sim = Sim()
    pilot = module_.Autopilot({"nodes": [dict(k) for k in node_list]}, sim,
                            module_.Protokoll(None), max_runden=max_runden)
    return pilot.fahre(), sim


@pytest.mark.parametrize(
    "name,node_list,verdict_list,gates_proto,gates_prod,classes_,expected",
    [
        ("S1 durchlauf",
         [{"id": "a", "status": "READY", "dependencies": []},
          {"id": "b", "status": "READY", "dependencies": ["a"]}],
         None, [0], [GateOutcome.GREEN], None, "FIXPUNKT"),
        ("S2 rotes gate erzeugt reparatur, danach gruen",
         [{"id": "a", "status": "READY", "dependencies": []}],
         None, [1, 0], [GateOutcome.RED, GateOutcome.GREEN], None, "FIXPUNKT"),
        ("S3 externe aktion",
         [{"id": "push", "status": "READY", "dependencies": []}],
         None, [0], [GateOutcome.GREEN], {"push": "EXTERN"}, "HALT_EXTERN"),
        ("S5 dauerhaft rotes gate",
         [{"id": "a", "status": "READY", "dependencies": []}],
         None, [1], [GateOutcome.RED], None, "HALT_RUNDEN"),
        ("S8 provider weg",
         [{"id": "a", "status": "READY", "dependencies": []}],
         {"a": "BUDGET"}, [0], [GateOutcome.GREEN], None, "HALT_BUDGET"),
    ],
)
def test_parity_with_the_prototype(tmp_path, name, node_list, verdict_list, gates_proto,
                                   gates_prod, classes_, expected):
    """Both loops reach the same halt class on the same scenario."""
    module_ = prototype_()
    proto_end, proto_sim = _prototype_runs(
        module_, node_list, verdict_list=verdict_list, gate_sequence=gates_proto, classes_=classes_
    )
    assert proto_end == expected, f"oracle itself changed: {name} -> {proto_end}"

    product_verdicts = None
    if verdict_list:
        product_verdicts = {
            k: (RunVerdict.PROVIDER_UNAVAILABLE if v == "BUDGET" else RunVerdict.ACCEPTED)
            for k, v in verdict_list.items()
        }
    product_classes = None
    if classes_:
        product_classes = {
            k: (ActionClass.EXTERNAL if v == "EXTERN" else ActionClass.INTERNAL)
            for k, v in classes_.items()
        }
    result, start, _ = fahre(
        tmp_path,
        [TaskNode(id=k["id"], dependencies=k["dependencies"]) for k in node_list],
        verdicts=product_verdicts, classes=product_classes, gates=gates_prod,
    )
    assert result.halt == EQUIVALENT[proto_end], (
        f"{name}: oracle says {proto_end} -> {EQUIVALENT[proto_end]}, "
        f"product says {result.halt} ({result.reason})"
    )


def test_parity_a_rejection_is_not_a_dead_end(tmp_path):
    """S4: two rejections then acceptance, both loops close.

    Split out because the scripted verdict list is consumed by the run, so the
    two sides need their own copies.
    """
    module_ = prototype_()
    proto_end, proto_sim = _prototype_runs(
        module_, [{"id": "a", "status": "READY", "dependencies": []}],
        verdict_list={"a": ["REJECTED", "REJECTED", "ACCEPTED"]}, gate_sequence=[0],
    )
    assert proto_end == "FIXPUNKT"
    assert proto_sim.started_ == ["a", "a", "a"]

    result, start, _ = fahre(
        tmp_path, [TaskNode(id="a")],
        verdicts={"a": [RunVerdict.REJECTED, RunVerdict.REJECTED, RunVerdict.ACCEPTED]},
        gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    assert start.launched == ["a", "a", "a"], "a rejection is input to the next iteration"


def test_parity_an_external_action_demonstrably_starts_nothing(tmp_path):
    """S3, the half that matters: neither loop dispatches before the gate."""
    module_ = prototype_()
    proto_end, proto_sim = _prototype_runs(
        module_, [{"id": "push", "status": "READY", "dependencies": []}],
        classes_={"push": "EXTERN"}, gate_sequence=[0],
    )
    assert proto_end == "HALT_EXTERN"
    assert proto_sim.started_ == []

    result, start, _ = fahre(
        tmp_path, [TaskNode(id="push")],
        classes={"push": ActionClass.EXTERNAL}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.BLOCKED_EXTERNAL
    assert start.launched == [], "nothing may be dispatched before a captain gate"


def test_divergence_an_unknown_state_is_prevented_not_detected(tmp_path):
    """S6 is the one scenario where the two sides *must* differ, and why.

    The prototype reads node status from JSON, so a state it does not know --
    `"VIELLEICHT"` -- is a runtime discovery, and it halts AMBIGUOUS. The
    product types the lifecycle as an enum, so such a state cannot be
    constructed at all: it is refused at the boundary, and the halt never
    happens because the state never exists.

    That is a strict improvement, and it is asserted here rather than papered
    over, because "the two agree everywhere" would otherwise be false.
    """
    module_ = prototype_()
    proto_end, _ = _prototype_runs(
        module_, [{"id": "a", "status": "VIELLEICHT", "dependencies": []}], gate_sequence=[0]
    )
    assert proto_end == "HALT_UNKLAR", "oracle should detect the unknown state at runtime"

    with pytest.raises(Exception) as exc:
        TaskNode(id="a", lifecycle="VIELLEICHT")  # type: ignore[arg-type]
    assert "lifecycle" in str(exc.value).lower() or "VIELLEICHT" in str(exc.value)


def test_parity_an_unknown_verdict_halts_both(tmp_path):
    """S7: a verdict neither loop can classify halts it, in both."""
    module_ = prototype_()
    proto_end, _ = _prototype_runs(
        module_, [{"id": "a", "status": "READY", "dependencies": []}],
        verdict_list={"a": "FERTIG?"}, gate_sequence=[0],
    )
    assert proto_end == "HALT_UNKLAR"

    result, _, store = fahre(
        tmp_path, [TaskNode(id="a")],
        verdicts={"a": RunVerdict.UNDETERMINED}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.AMBIGUOUS, result.reason
    # And the product records why, where the next session will read it.
    assert store.read_state().node("a").lifecycle is Lifecycle.BLOCKED


# --------------------------------------------------------------------------- #
# Product-only: what the prototype never had, because it had no durable state
# --------------------------------------------------------------------------- #

def test_a_stale_writer_halts_the_loop(tmp_path):
    """A second orchestrator that advanced the state fences this one out."""
    s = projekt(tmp_path, [TaskNode(id="a")])

    class InBetween(Start):
        def launch(self, node):
            # Somebody else writes while this round is in flight.
            foreign = ProjectStore(tmp_path, "p")
            st = foreign.read_state()
            st.nodes.append(TaskNode(id="fremd"))
            foreign.write_state(st)
            return super().launch(node)

    c = ProjectController(s, InBetween(), Gates([GateOutcome.GREEN]))
    result = c.run()
    assert result.halt is HaltClass.AMBIGUOUS
    assert "advanced this project" in result.reason


def test_a_second_orchestrator_start_is_refused(tmp_path):
    s = projekt(tmp_path, [TaskNode(id="a")])
    with s.lock():
        second_one = ProjectController(ProjectStore(tmp_path, "p"),
                                    Start(), Gates([GateOutcome.GREEN]))
        result = second_one.run()
    assert result.halt is HaltClass.BLOCKED_DEPENDENCY
    assert "another orchestrator" in result.reason


def test_a_crash_before_the_status_change_loses_nothing(tmp_path):
    """Killed before the lifecycle was written: the node is still READY."""
    s = projekt(tmp_path, [TaskNode(id="a")])

    class Dies(Start):
        def launch(self, node):
            raise KeyboardInterrupt("killed mid-dispatch")

    c = ProjectController(s, Dies(), Gates([GateOutcome.GREEN]))
    with pytest.raises(KeyboardInterrupt):
        c.run()
    # RUNNING was persisted before the dispatch, which is the point: the next
    # session sees an in-flight node and evaluates rather than re-dispatching.
    assert s.read_state().node("a").lifecycle is Lifecycle.RUNNING
    further = ProjectController(ProjectStore(tmp_path, "p"), Start(), Gates([GateOutcome.GREEN]))
    result = further.run()
    assert result.halt is HaltClass.AMBIGUOUS
    assert "RUNNING" in result.reason


def test_a_crash_after_the_merge_is_not_merged_twice(tmp_path):
    """Killed after MERGED was persisted: the next session does not re-merge."""
    s = projekt(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.MERGED)])
    start = Start()
    c = ProjectController(s, start, Gates([GateOutcome.GREEN]))
    result = c.run()
    assert result.halt is HaltClass.CLOSED
    assert start.launched == [] and start.merged == [], "already merged work is not redone"


def test_accepted_but_not_landed_is_not_guessed(tmp_path):
    """Accepted, merge did not land: nothing is resolved automatically.

    This fixture's launcher returns a bare `False`, the way an older launcher
    would, and says nothing about why. That is the one case that genuinely
    stays AMBIGUOUS -- the halt is honest about the tool having been silent,
    rather than inventing a category for it.
    """
    result, start, store = fahre(
        tmp_path, [TaskNode(id="a")], gates=[GateOutcome.GREEN], merge_lands=False
    )
    assert result.halt is HaltClass.AMBIGUOUS
    assert "did not say why" in result.reason
    assert store.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_a_terminal_dag_with_a_red_closure_creates_a_repair(tmp_path):
    result, _, store = fahre(
        tmp_path, [TaskNode(id="a")],
        gates=[GateOutcome.RED, GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    st = store.read_state()
    rep = [n for n in st.nodes if n.repair_of]
    assert len(rep) == 1, [n.id for n in st.nodes]
    assert rep[0].lifecycle is Lifecycle.MERGED
    assert st.closure_generation == 2, "closure ran twice: once red, once green"


def test_a_repair_is_recorded_as_a_decision(tmp_path):
    """Why a repair node exists survives the session that created it."""
    _, _, store = fahre(tmp_path, [TaskNode(id="a")],
                        gates=[GateOutcome.RED, GateOutcome.GREEN])
    st = store.read_state()
    reasons = [d for d in st.decisions if d.kind.value == "CREATE_REPAIR_NODE"]
    assert reasons, [d.kind for d in st.decisions]
    assert "union" in reasons[0].reason
    assert reasons[0].evidence and reasons[0].evidence[0].startswith("gate:union@")


def test_a_real_fixpoint_names_its_subject(tmp_path):
    result, _, store = fahre(tmp_path, [TaskNode(id="a")], gates=[GateOutcome.GREEN])
    assert result.halt is HaltClass.CLOSED
    st = store.read_state()
    assert st.measurement_head == "abc1234"
    assert st.rc_closed()
    assert "abc1234" in result.reason


def test_gates_that_did_not_run_are_not_a_fixpoint(tmp_path):
    result, _, store = fahre(tmp_path, [TaskNode(id="a")], gates=[GateOutcome.NOT_RUN])
    assert result.halt is HaltClass.NOT_RUN
    assert not store.read_state().rc_closed()


def test_a_damaged_state_halts_instead_of_overwriting(tmp_path):
    s = projekt(tmp_path, [TaskNode(id="a")])
    st = s.read_state()
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.write_state(st)
    s.state_path.write_text('{"project_id": "p", "nodes": [', encoding="utf-8")
    c = ProjectController(ProjectStore(tmp_path, "p"), Start(), Gates([GateOutcome.GREEN]))
    result = c.run()
    assert result.halt is HaltClass.CORRUPT_STATE
    assert s.parked_states(), "the way back is still on disk"


def test_an_unresolvable_dependency_starts_nothing(tmp_path):
    result, start, _ = fahre(
        tmp_path, [TaskNode(id="b", dependencies=["gibtesnicht"])], gates=[GateOutcome.GREEN]
    )
    assert result.halt is HaltClass.BLOCKED_DEPENDENCY
    assert "gibtesnicht" in result.reason
    assert start.launched == []


def test_every_halt_carries_a_class(tmp_path):
    """No exit leaves the loop without a name -- the measure of how far the
    automation reaches depends on it."""
    cases_ = [
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.GREEN]), HaltClass.CLOSED),
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.NOT_RUN]), HaltClass.NOT_RUN),
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.RED], max_repairs=0),
         HaltClass.ROUND_LIMIT),
        (dict(nodes=[TaskNode(id="b", dependencies=["x"])], gates=[GateOutcome.GREEN]),
         HaltClass.BLOCKED_DEPENDENCY),
    ]
    for i, (kw, expected) in enumerate(cases_):
        node_list = kw.pop("nodes")
        s = ProjectStore(tmp_path, f"f{i}")
        st = ProjectState(project_id=f"f{i}", repo_path=str(tmp_path))
        st.nodes = node_list
        s.create(st)
        c = ProjectController(s, Start(), Gates(kw.pop("gates")), **kw)
        result = c.run()
        assert result.halt is expected, f"case {i}: {result.halt} {result.reason}"
        assert result.reason, f"case {i} halted without a reason"


# --------------------------------------------------------------------------- #
# Resume: a RUNNING node is read, not re-dispatched
# --------------------------------------------------------------------------- #

def test_a_running_node_is_read_not_restarted(tmp_path):
    """The heart of exactly-once.

    A fresh process finding a node marked RUNNING must not re-dispatch it to
    find out what happened -- that would repeat an acceptance that may already
    have landed. It reads the run's own recorded state instead, which costs
    nothing and is decisive when the run finished.
    """
    result, start, store = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        evaluations={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    assert start.evaluated == ["a"], "the run state must be read"
    assert start.launched == [], "and the run must not be dispatched again"
    assert start.merged == ["a"], "exactly one merge"
    assert store.read_state().node("a").lifecycle is Lifecycle.MERGED


def test_a_running_node_with_no_recognisable_result_halts(tmp_path):
    """When the run state cannot say, the loop stops rather than guessing."""
    result, start, _ = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.AMBIGUOUS
    assert "could repeat an acceptance" in result.reason
    assert start.launched == []


def test_a_running_node_with_a_rejection_goes_back_into_the_loop(tmp_path):
    """A crashed dispatch whose run was rejected resumes as ordinary work."""
    result, start, store = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        evaluations={"a": RunVerdict.REJECTED},
        verdicts={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    # Evaluated twice: once on resume, and once again before the dispatch. The
    # second read is what stops the loop paying for work a previous dispatch
    # already had accepted -- it costs nothing and it is the whole mechanism
    # behind exactly-once.
    assert start.evaluated == ["a", "a"], start.evaluated
    assert start.launched == ["a"], "after the rejection it is dispatched once, normally"
    assert store.read_state().node("a").rejections == 1


def test_the_baseline_is_persisted_before_the_dispatch(tmp_path):
    """What was accepted *before* a dispatch goes into state, not memory.

    A process that dies during the dispatch is exactly when it is needed: it is
    the only thing that lets the next process tell an acceptance from a run
    that ended where it started.
    """
    s = projekt(tmp_path, [TaskNode(id="a")])

    class MerktBasislinie(Start):
        def accepted_baseline(self, node):
            return "a-i1"

        def launch(self, node):
            # What is on disk at the moment of dispatch is what a crash leaves.
            self.seen_ = ProjectStore(tmp_path, "p").read_state().node("a").accepted_before
            return super().launch(node)

    start = MerktBasislinie()
    c = ProjectController(s, start, Gates([GateOutcome.GREEN]))
    result = c.run()
    assert result.halt is HaltClass.CLOSED
    assert start.seen_ == "a-i1", "the baseline must be durable before the dispatch"


def test_an_already_accepted_candidate_is_not_dispatched_again(tmp_path):
    """A node whose run already accepted something is merged, not re-run.

    The state this covers is real and was found by the first end-to-end run: a
    dispatch succeeded, the merge was blocked by untracked build output, the
    obstruction was cleared, and the node came back READY with its acceptance
    still sitting in the run record. Dispatching again would pay a second time
    for verified work -- and could accept a second, different candidate for the
    same node.
    """
    result, start, store = fahre(
        tmp_path, [TaskNode(id="a")],
        evaluations={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    assert start.launched == [], "nothing may be dispatched when the work is already accepted"
    assert start.merged == ["a"], "exactly one merge"
    assert any(s.kind == "ALREADY_ACCEPTED" for s in result.steps), \
        [s.kind for s in result.steps]


def test_a_node_that_never_started_becomes_work_again(tmp_path):
    """A RUNNING node whose run never started goes back to READY and is then
    dispatched -- rather than halting the loop on every round."""
    result, start, store = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        evaluations={"a": RunVerdict.NOT_STARTED},
        verdicts={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert result.halt is HaltClass.CLOSED, result.reason
    assert start.launched == ["a"], "it must actually be dispatched, exactly once"
    assert any(s.kind == "RESET_TO_READY" for s in result.steps), \
        [s.kind for s in result.steps]
    assert store.read_state().node("a").lifecycle is Lifecycle.MERGED
