"""The ready frontier: the planner's contract, and the loop that follows it.

Two things are pinned here, and each test says which one it is about.

The planner (`hoh.frontier`) is a pure function from the READY nodes and a
measured `depends_on` to a plan. Its tests make the answer move: a conflict
that is only the second node's to declare, a launcher that says `None`, an
order that changes which node is chosen.

The loop (`ProjectController`) must dispatch what the plan says, must keep the
one-at-a-time behaviour and the `SERIALISED` step stream it had before the
planner existed, and must refuse a plan that names a node the state did not
offer -- including a planner that tries to make its own answer pass.

`hoh.frontier` is imported inside the helper, not at module level, so that on a
tree without the module every test here fails by name instead of the whole file
failing to collect.
"""

from __future__ import annotations

import copy
import dataclasses
import random
from types import SimpleNamespace

import pytest

from hoh.launcher import HohRunLauncher
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
from hoh.projectstore import RESUME, ProjectStore, resume_decision


def _frontier():
    import hoh.frontier as frontier
    return frontier


def _node(node_id, *, writes=(), reads=(), deps=()):
    return TaskNode(id=node_id, lifecycle=Lifecycle.READY, dependencies=list(deps),
                    writes=list(writes), semantic_reads=list(reads))


def _conflicts(*pairs):
    """A symmetric `depends_on` over node ids; the pairs are the conflicts."""
    table = {frozenset(p) for p in pairs}

    def depends_on(a, b):
        return frozenset((a.id, b.id)) in table
    return depends_on


def _plan(nodes, depends_on):
    return _frontier().ReadyFrontierPlanner().plan(nodes, depends_on)


# --------------------------------------------------------------------------- #
# The planner
# --------------------------------------------------------------------------- #

def test_independent_nodes_are_all_selected_in_the_given_order():
    nodes = [_node("c"), _node("a"), _node("b")]
    plan = _plan(nodes, _conflicts())
    assert plan.selected_ids == ("c", "a", "b")
    assert plan.deferred == ()
    assert plan.head is nodes[0]


def test_the_first_ready_node_is_always_selected_whatever_it_conflicts_with():
    nodes = [_node("a"), _node("b"), _node("c")]
    plan = _plan(nodes, _conflicts(("a", "b"), ("a", "c"), ("b", "c")))
    assert plan.selected_ids == ("a",)
    assert plan.head is nodes[0]
    assert [(d.node.id, d.blocked_by.id) for d in plan.deferred] == [("b", "a"), ("c", "a")]


def test_the_selection_is_greedy_in_order_and_maximal_not_head_only():
    # a-b and b-c conflict, a-c do not: the frontier is {a, c}, b waits behind a.
    nodes = [_node("a"), _node("b"), _node("c")]
    plan = _plan(nodes, _conflicts(("a", "b"), ("b", "c")))
    assert plan.selected_ids == ("a", "c")
    assert [(d.node.id, d.blocked_by.id) for d in plan.deferred] == [("b", "a")]


def test_the_order_given_decides_which_of_two_conflicting_nodes_is_chosen():
    a, b = _node("a"), _node("b")
    conflict = _conflicts(("a", "b"))
    assert _plan([a, b], conflict).selected_ids == ("a",)
    assert _plan([b, a], conflict).selected_ids == ("b",)


def test_a_deferred_node_names_the_selected_node_it_conflicts_with():
    # b is deferred behind a; c conflicts only with the deferred b, so it is
    # selected; d conflicts with the selected c, so it waits behind c, not a.
    nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
    plan = _plan(nodes, _conflicts(("a", "b"), ("b", "c"), ("c", "d")))
    assert plan.selected_ids == ("a", "c")
    assert [(d.node.id, d.blocked_by.id) for d in plan.deferred] == [("b", "a"), ("d", "c")]


def test_a_conflict_declared_in_one_direction_only_still_serialises():
    a, b = _node("a"), _node("b")
    first_only = lambda x, y: (x.id, y.id) == ("a", "b")      # noqa: E731
    second_only = lambda x, y: (x.id, y.id) == ("b", "a")     # noqa: E731
    assert _plan([a, b], first_only).selected_ids == ("a",)
    assert _plan([a, b], second_only).selected_ids == ("a",)


def test_only_an_exact_false_is_independence():
    a, b = _node("a"), _node("b")
    for answer in (None, 0, "", [], "no", 1, object()):
        plan = _plan([a, b], lambda x, y, answer=answer: answer)
        assert plan.selected_ids == ("a",), repr(answer)
        assert [d.node.id for d in plan.deferred] == ["b"], repr(answer)
    assert _plan([a, b], lambda x, y: False).selected_ids == ("a", "b")


def test_an_empty_frontier_has_no_head_and_asks_nothing():
    asked = []
    plan = _plan([], lambda x, y: asked.append((x.id, y.id)) or False)
    assert plan.head is None
    assert plan.selected == () and plan.deferred == ()
    assert asked == []


def test_a_single_node_is_selected_without_asking_anyone():
    asked = []
    only = _node("a")
    plan = _plan([only], lambda x, y: asked.append((x.id, y.id)) or True)
    assert plan.selected == (only,) and plan.head is only
    assert asked == []


def test_the_plan_holds_the_given_node_objects_and_changes_none_of_them():
    nodes = [_node("a", writes=["x"]), _node("b", writes=["x"]), _node("c")]
    before = [copy.deepcopy(n.model_dump()) for n in nodes]
    given = list(nodes)
    plan = _plan(nodes, lambda a, b: HohRunLauncher.depends_on(None, a, b))
    assert nodes == given and all(x is y for x, y in zip(nodes, given))
    assert [n.model_dump() for n in nodes] == before
    assert plan.selected[0] is nodes[0] and plan.selected[1] is nodes[2]


def test_the_same_input_gives_the_same_plan():
    nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
    conflict = _conflicts(("a", "c"), ("b", "d"))
    assert _plan(nodes, conflict) == _plan(nodes, conflict)
    assert _plan(nodes, conflict).selected_ids == ("a", "b")


def test_a_node_that_conflicts_with_two_selected_nodes_waits_behind_the_first():
    # a-d and c-d conflict: d is checked against the selected {a, c} in order
    # and waits behind a, the head, not behind the later c.
    nodes = [_node("a"), _node("c"), _node("d")]
    plan = _plan(nodes, _conflicts(("a", "d"), ("c", "d")))
    assert plan.selected_ids == ("a", "c")
    assert len(plan.deferred) == 1
    assert plan.deferred[0].node is nodes[2]
    assert plan.deferred[0].blocked_by is nodes[0]


def test_nodes_with_one_id_are_told_apart_by_object_not_by_id():
    first, second = _node("a"), _node("a", writes=["x"])
    other = _node("x", writes=["x"])
    plan = _plan([first, second, other],
                 lambda a, b: HohRunLauncher.depends_on(None, a, b))
    assert plan.selected == (first, second)
    assert plan.deferred[0].node is other
    assert plan.deferred[0].blocked_by is second          # not `first`, whose id it shares


def test_the_plan_agrees_with_a_reference_over_seeded_random_graphs():
    for seed in range(300):
        rnd = random.Random(seed)
        nodes = [_node(f"n{i}") for i in range(rnd.randint(0, 8))]
        pairs = {frozenset((a.id, b.id)) for a in nodes for b in nodes
                 if a.id < b.id and rnd.random() < 0.4}

        def dep(a, b, pairs=pairs):
            return frozenset((a.id, b.id)) in pairs

        plan = _plan(nodes, dep)
        ids = [n.id for n in nodes]
        chosen = plan.selected_ids
        waiting = [d.node.id for d in plan.deferred]
        # the head is the first node, and its recorded conflicts are exactly
        # the ones the one-at-a-time loop of the pin asked about
        assert plan.head is (nodes[0] if nodes else None), seed
        assert [d.node.id for d in plan.deferred if d.blocked_by is plan.head] \
            == [n.id for n in nodes[1:] if dep(nodes[0], n)], seed
        # a partition of the input, each half in the given order
        assert sorted(chosen + tuple(waiting)) == sorted(ids), seed
        assert list(chosen) == [i for i in ids if i in chosen], seed
        assert waiting == [i for i in ids if i in waiting], seed
        # pairwise independent
        for i, one in enumerate(plan.selected):
            for two in plan.selected[i + 1:]:
                assert not dep(one, two), seed
        # maximal for the order, and each node waits behind the FIRST selected
        # node it conflicts with
        for d in plan.deferred:
            first = next((c for c in plan.selected if dep(c, d.node)), None)
            assert first is not None and d.blocked_by is first, (seed, d.node.id)


def test_the_plan_and_its_deferrals_are_immutable():
    nodes = [_node("a"), _node("b")]
    plan = _plan(nodes, _conflicts(("a", "b")))
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.selected = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.deferred[0].reason = "OTHER"
    assert isinstance(plan.selected, tuple) and isinstance(plan.deferred, tuple)


def test_a_depends_on_that_raises_is_not_swallowed():
    def boom(a, b):
        raise RuntimeError("the measurement failed")
    with pytest.raises(RuntimeError, match="the measurement failed"):
        _plan([_node("a"), _node("b")], boom)


# --------------------------------------------------------------------------- #
# The planner on the product's own measurement
# --------------------------------------------------------------------------- #

def _measured(nodes):
    # `depends_on` reads only the two nodes it is given; the real launcher is
    # not constructed (that would need a repository).
    return _plan(nodes, lambda a, b: HohRunLauncher.depends_on(None, a, b))


def test_a_write_write_overlap_is_not_in_one_frontier():
    plan = _measured([_node("a", writes=["src/x.py"]), _node("b", writes=["src/x.py"])])
    assert plan.selected_ids == ("a",)


def test_a_write_against_a_semantic_read_is_not_in_one_frontier_either_way():
    writer, reader = _node("w", writes=["gate.py"]), _node("r", reads=["gate.py"])
    assert _measured([writer, reader]).selected_ids == ("w",)
    assert _measured([reader, writer]).selected_ids == ("r",)


def test_disjoint_writes_and_shared_reads_are_one_frontier():
    plan = _measured([_node("a", writes=["a.py"], reads=["common.py"]),
                      _node("b", writes=["b.py"], reads=["common.py"]),
                      _node("c", reads=["common.py"])])
    assert plan.selected_ids == ("a", "b", "c")


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

class _Launcher(RunLauncher):
    """A scripted outside world: every launch is accepted and merges."""

    def __init__(self, conflicts=(), classes=None):
        self._depends = _conflicts(*conflicts)
        self._classes = dict(classes or {})
        self.launched: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.prepared: list[str] = []
        self.evaluated: list[str] = []

    def action_class(self, node):
        return self._classes.get(node.id, ActionClass.INTERNAL)

    def depends_on(self, a, b):
        self.asked.append((a.id, b.id))
        return self._depends(a, b)

    def launch(self, node):
        self.launched.append(node.id)
        return RunOutcome(verdict=RunVerdict.ACCEPTED, candidate=f"{node.id}-cand")

    def accepted_baseline(self, node):
        return None

    def prepare(self, node):
        self.prepared.append(node.id)
        return None

    def evaluate(self, node):
        self.evaluated.append(node.id)
        return RunOutcome(verdict=RunVerdict.UNDETERMINED, detail="not run",
                          candidate=f"{node.id}-cand")

    def merge(self, node, outcome):
        return True


class _Measured(_Launcher):
    """The product launcher's own measurement, with the scripted outside world."""

    def depends_on(self, a, b):
        return HohRunLauncher.depends_on(None, a, b)


class _Green(GateRunner):
    def subject(self):
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


def _drive(tmp_path, nodes, launcher, **kw):
    store = ProjectStore(tmp_path, "p")
    state = ProjectState(project_id="p", repo_path=str(tmp_path))
    state.nodes = nodes
    store.create(state)
    return ProjectController(store, launcher, _Green(), **kw).run()


class _Chooses:
    """A planner that answers with whatever `pick(ready)` returns as the head."""

    def __init__(self, pick):
        self.pick = pick
        self.seen: list[list[str]] = []

    def plan(self, ready, depends_on):
        self.seen.append([n.id for n in ready])
        head = self.pick(list(ready))
        return SimpleNamespace(head=head, deferred=(),
                               selected=() if head is None else (head,))


def test_the_loop_dispatches_the_head_of_the_plan_not_the_first_ready_node(tmp_path):
    launcher = _Launcher()
    planner = _Chooses(lambda ready: ready[-1])
    result = _drive(tmp_path, [_node("a"), _node("b"), _node("c")], launcher,
                    planner=planner)
    assert launcher.launched[0] == "c", launcher.launched
    assert planner.seen[0] == ["a", "b", "c"]
    assert result.halt is HaltClass.CLOSED, result.reason


def test_a_plan_with_no_head_for_a_non_empty_frontier_halts_and_launches_nothing(tmp_path):
    launcher = _Launcher()
    result = _drive(tmp_path, [_node("a"), _node("b")], launcher,
                    planner=_Chooses(lambda ready: None))
    assert result.halt is HaltClass.AMBIGUOUS, result.reason
    assert launcher.launched == [] and launcher.prepared == [] and launcher.evaluated == []
    assert "nothing" in result.reason and "2 ready" in result.reason
    assert result.steps[-1].kind == HaltClass.AMBIGUOUS and result.steps[-1].round == 1


def test_a_plan_naming_a_node_that_is_not_ready_halts_and_launches_nothing(tmp_path):
    launcher = _Launcher()
    stranger = _node("a")                   # same id, but not the state's own node
    result = _drive(tmp_path, [_node("a"), _node("b")], launcher,
                    planner=_Chooses(lambda ready: stranger))
    assert result.halt is HaltClass.AMBIGUOUS, result.reason
    assert launcher.launched == [] and launcher.prepared == [] and launcher.evaluated == []
    assert "node a" in result.reason and "2 ready" in result.reason
    assert result.steps[-1].kind == HaltClass.AMBIGUOUS and result.steps[-1].node == "a"


def test_a_plan_cannot_hand_the_loop_a_node_that_is_not_ready(tmp_path):
    launcher = _Launcher()
    nodes = [_node("a"), _node("b", deps=["a"])]
    # b waits for a: only a is ready, so b is not a node of the frontier.
    result = _drive(tmp_path, nodes, launcher,
                    planner=_Chooses(lambda ready: nodes[1]))
    assert result.halt is HaltClass.AMBIGUOUS, result.reason
    assert launcher.launched == []


def test_a_planner_cannot_vouch_for_its_own_answer_by_adding_to_the_list(tmp_path):
    # A settled node `z` is not READY. The planner tries to append a READY copy
    # of it to the list it was given and to return that: the loop must have
    # handed it a list it cannot extend, and must still refuse the answer.
    settled = TaskNode(id="z", lifecycle=Lifecycle.MERGED)
    stranger = _node("z")
    attempts = []

    def pick(ready):
        try:
            ready.append(stranger)
            attempts.append("appended")
        except AttributeError:
            attempts.append("refused")
        return stranger

    class Appends:
        def plan(self, ready, depends_on):
            head = pick(ready)
            return SimpleNamespace(head=head, deferred=(), selected=(head,))

    launcher = _Launcher()
    result = _drive(tmp_path, [settled, _node("a")], launcher, planner=Appends(),
                    max_rounds=3)
    assert result.halt is HaltClass.AMBIGUOUS, (result.halt, result.reason)
    assert launcher.launched == []
    assert attempts == ["refused"]


def test_a_head_that_stopped_being_ready_while_the_planner_ran_is_refused(tmp_path):
    nodes = [_node("a"), _node("b")]

    class Settles:
        def plan(self, ready, depends_on):
            ready[1].lifecycle = Lifecycle.MERGED       # a node object, not the list
            return SimpleNamespace(head=ready[1], deferred=(), selected=(ready[1],))

    launcher = _Launcher()
    result = _drive(tmp_path, nodes, launcher, planner=Settles(), max_rounds=3)
    assert result.halt is HaltClass.AMBIGUOUS, (result.halt, result.reason)
    assert launcher.launched == []


def test_the_loop_records_the_plan_s_deferrals_not_a_second_measurement(tmp_path):
    # The launcher says everything is independent, so only the plan can be the
    # source of a SERIALISED step; and only a SEMANTIC_DEPENDENCY deferral
    # whose blocker is the head is one.
    fr = _frontier()
    a, b, c, d = (_node(i) for i in "abcd")

    class Planned:
        def plan(self, ready, depends_on):
            return fr.FrontierPlan(
                selected=(ready[0],),
                deferred=(fr.Deferral(ready[1], ready[0]),                    # recorded
                          fr.Deferral(ready[2], ready[0], "WIDTH_LIMIT"),     # another reason
                          fr.Deferral(ready[3], ready[1])))                   # behind a non-head

    result = _drive(tmp_path, [a, b, c, d], _Launcher(), planner=Planned(), max_rounds=1)
    serial = [(s.round, s.node) for s in result.steps if s.kind == "SERIALISED"]
    assert serial == [(1, "a<->b")], serial


def test_the_default_loop_still_runs_ready_nodes_in_the_persisted_order(tmp_path):
    launcher = _Launcher(conflicts=[("a", "c"), ("b", "c")])
    result = _drive(tmp_path, [_node("a"), _node("b"), _node("c"), _node("d")], launcher)
    assert launcher.launched == ["a", "b", "c", "d"]
    assert result.halt is HaltClass.CLOSED, result.reason


def test_serialised_steps_are_the_head_s_measured_conflicts_and_only_those(tmp_path):
    # A chain a-b, b-c, c-d. Round 1: a is the head, b waits behind a, c is
    # selected beside a and d waits behind c (not behind the head): only the
    # head's own conflict, a<->b, is recorded. c<->d becomes the head's in
    # round 3.
    launcher = _Launcher(conflicts=[("a", "b"), ("b", "c"), ("c", "d")])
    result = _drive(tmp_path, [_node("a"), _node("b"), _node("c"), _node("d")], launcher)
    serial = [(s.round, s.node, s.detail) for s in result.steps if s.kind == "SERIALISED"]
    assert serial == [(1, "a<->b", "measured semantic dependency"),
                      (2, "b<->c", "measured semantic dependency"),
                      (3, "c<->d", "measured semantic dependency")]
    assert launcher.launched == ["a", "b", "c", "d"]


def test_the_head_s_conflict_is_recorded_when_a_later_selected_node_conflicts_too(tmp_path):
    # a-d and c-d: d conflicts with the head a and with the selected c. The
    # stream the pin wrote for round 1 names the head's conflict, a<->d.
    launcher = _Launcher(conflicts=[("a", "d"), ("c", "d")])
    result = _drive(tmp_path, [_node("a"), _node("c"), _node("d")], launcher)
    serial = [(s.round, s.node) for s in result.steps if s.kind == "SERIALISED"]
    assert serial == [(1, "a<->d"), (2, "c<->d")], serial
    assert launcher.launched == ["a", "c", "d"]


def test_a_second_node_with_the_head_s_id_does_not_become_the_head_s_conflict(tmp_path):
    # a, a (writes x), x (writes x): the head's own measurement against x is
    # empty, so round 1 records nothing, although the plan's selected node
    # `a`(writes x) shares the head's id and conflicts with x.
    nodes = [_node("a"), _node("a", writes=["x"]), _node("x", writes=["x"])]
    result = _drive(tmp_path, nodes, _Measured(), max_rounds=2)
    first_round = [(s.round, s.node) for s in result.steps
                   if s.kind == "SERIALISED" and s.round == 1]
    assert first_round == [], first_round


def test_the_state_advisor_names_the_node_the_loop_dispatches_first(tmp_path):
    # `resume_decision` reads ready()[0] on its own; the head invariant is what
    # keeps the advisor and the loop on one node.
    nodes = [_node("a"), _node("b"), _node("c")]
    store = ProjectStore(tmp_path / "advice", "p")
    state = ProjectState(project_id="p", repo_path=str(tmp_path))
    state.nodes = nodes
    store.create(state)
    verdict, why = resume_decision(store.read_state())
    assert verdict == RESUME and "node a is READY" in why, why
    launcher = _Launcher(conflicts=[("a", "b"), ("a", "c")])
    _drive(tmp_path / "loop", nodes, launcher)
    assert launcher.launched[0] == "a"


def test_the_head_is_checked_for_an_external_action_before_it_is_dispatched(tmp_path):
    launcher = _Launcher(classes={"a": ActionClass.EXTERNAL})
    result = _drive(tmp_path, [_node("a"), _node("b")], launcher)
    assert result.halt is HaltClass.BLOCKED_EXTERNAL
    assert launcher.launched == []
