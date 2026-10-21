"""Which open decisions hold which ready work (V3.3 P3-27, first slice).

`route()` says a parked branch "waits while independent READY work continues".
Nothing at the level of the project graph read that back: these tests pin the
read model (`hoh.decisionfrontier.derive_frontier`) and the one reader that feeds it
(`hoh.governance.read_branch_stops`). The frontier is pure and takes the
project's nodes; the reader is read-only and refuses a log it cannot trust whole.

Groups: F = the frontier over a graph, L = the log reader, P = the product's own
writer read back through both.
"""
from __future__ import annotations

import dataclasses
import json
import os
import random
import threading
from pathlib import Path

import pytest

from hoh.autonomy import (
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    NeedsYou,
    Routing,
    compile_profile,
    route,
)
from hoh.contracts import Budgets, RunState
from hoh.governance import bind, block_under_contract, record_override
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode

READY, RUNNING, BLOCKED = Lifecycle.READY, Lifecycle.RUNNING, Lifecycle.BLOCKED
MERGED, CONTINGENT = Lifecycle.MERGED, Lifecycle.CONTINGENT

UNATTENDED = dict(scope=("src/hoh/",),
                  horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
                  budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))


def _contract(**kw):
    return compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un",
                           **{**UNATTENDED, **kw})


def _routing(text="needs a licence answer", *, kind="decision", delegable=False,
             **contract_kw) -> Routing:
    return route(_contract(**contract_kw), decision=text, kind=kind, delegable=delegable)


def _node(node_id, lifecycle=READY, deps=(), **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps), **kw)


def _state(*nodes) -> ProjectState:
    return ProjectState(project_id="p", repo_path="/x", nodes=list(nodes))


def _parked(node_id, deps=()) -> TaskNode:
    return _node(node_id, BLOCKED, deps, blocked_kind="parked_by_contract")


def _df():
    """The new module, imported at call time: on a tree without it every test fails
    by its own name instead of the file failing to collect."""
    import hoh.decisionfrontier as module
    return module


def _gov():
    import hoh.governance as module
    return module


def frontier(*args, **kw):
    return _df().derive_frontier(*args, **kw)


def read_branch_stops(*args, **kw):
    return _gov().read_branch_stops(*args, **kw)


def _by_id(result):
    return {d.node_id: d for d in result.decisions}


# --- F: the frontier ---------------------------------------------------------------

def test_a_parked_decision_holds_the_ready_work_behind_it_and_not_the_rest():
    result = frontier(_state(_parked("A"), _node("B", deps=["A"]), _node("C")))
    assert [d.node_id for d in result.decisions] == ["A"]
    assert result.decisions[0].holds == ("B",)
    assert result.runnable == ("C",)
    assert result.other_blocked == () and result.unrunnable == ()


def test_the_work_only_this_decision_stands_in_front_of_is_freed_by_it():
    result = frontier(_state(
        _parked("A"), _node("E", RUNNING),
        _node("B", deps=["A"]), _node("D", deps=["A", "E"])))
    a = _by_id(result)["A"]
    assert a.holds == ("B", "D")
    assert a.frees == ("B",), "D also waits for a node that is still running"


def test_queued_work_is_not_a_cause_so_work_behind_it_is_held_and_freed_transitively():
    result = frontier(_state(_parked("A"), _node("B", deps=["A"]), _node("C", deps=["B"])))
    a = _by_id(result)["A"]
    assert a.holds == ("B", "C") and a.frees == ("B", "C")


def test_work_behind_two_open_decisions_is_freed_by_neither_alone():
    result = frontier(_state(_parked("A"), _parked("B"), _node("W", deps=["A", "B"])))
    by = _by_id(result)
    assert by["A"].holds == ("W",) and by["B"].holds == ("W",)
    assert by["A"].frees == () and by["B"].frees == ()


def test_decisions_are_ranked_by_what_they_free_then_hold_then_graph_order():
    nodes = [
        _parked("A"), _node("a1", deps=["A"]),                        # frees 1, holds 1
        _parked("B"), _node("b1", deps=["B"]), _node("b2", deps=["B"]),  # frees 2
        _parked("C"), _node("c1", deps=["C"]), _node("c2", deps=["C"]),
        _node("r", RUNNING), _node("c3", deps=["C", "r"]),            # frees 2, holds 3
        _parked("D"),                                                  # frees 0, holds 0
        _parked("E"),                                                  # frees 0, holds 0
    ]
    result = frontier(_state(*nodes))
    assert [d.node_id for d in result.decisions] == ["C", "B", "A", "D", "E"]


def test_an_external_action_node_is_a_decision_and_is_not_runnable_work():
    result = frontier(_state(
        _node("X", action_class=ActionClass.EXTERNAL),
        _node("Y", deps=["X"]), _node("Z")))
    x = _by_id(result)["X"]
    assert x.handling == _df().EXTERNAL_ACTION and x.resolvable
    assert x.holds == ("Y",) and x.frees == ("Y",)
    assert result.runnable == ("Z",)


def test_a_timed_wait_is_not_a_decision_and_what_waits_behind_it_is_not_runnable():
    timed = _node("A", BLOCKED, blocked_kind="usage_limit", retry_after="2026-10-12T12:00:00Z")
    stale = _routing()                                       # a park from an earlier time
    result = frontier(_state(timed, _node("B", deps=["A"])), stops={"A": [stale]})
    assert result.decisions == ()
    assert result.other_blocked == ("A",)
    assert result.runnable == ()


def test_a_blocked_node_nobody_can_explain_is_neither_a_decision_nor_independent():
    result = frontier(_state(_node("A", BLOCKED, note="merge conflict"),
                             _node("B", deps=["A"]), _node("C")))
    assert result.decisions == ()
    assert result.other_blocked == ("A",)
    assert result.runnable == ("C",)


@pytest.mark.parametrize("spelling", ["Parked_By_Contract", "parked-by-contract", "USAGE_LIMIT"])
def test_a_blocked_kind_spelled_another_way_is_held_not_guessed_at(spelling):
    result = frontier(_state(_node("A", BLOCKED, blocked_kind=spelling),
                             _node("B", deps=["A"]), _node("C")))
    assert result.decisions == () and result.other_blocked == ("A",)
    assert result.runnable == ("C",)


def test_a_stopping_routing_names_the_decision_the_node_waits_for():
    routing = _routing("publish 0.4.0")
    result = frontier(_state(_node("A", BLOCKED, blocked_kind="parked_by_contract",
                                   retry_after="2026-10-12T12:00:00Z")),
                      stops={"A": [routing]})
    d = result.decisions[0]
    assert d.handling == "PARK_BRANCH" and d.kind == "decision"
    assert d.decision == "publish 0.4.0" and d.why == routing.why
    assert d.retry_after == "2026-10-12T12:00:00Z" and d.resolvable


def test_the_last_stopping_routing_speaks_and_a_routing_that_did_not_stop_is_ignored():
    first = _routing("first question")
    second = _routing("second question")
    resolved = route(_contract(), decision="answered", kind="decision", delegable=True)
    assert resolved.handling is Handling.RESOLVED_BY_POLICY and not resolved.blocks_branch
    result = frontier(_state(_parked("A")), stops={"A": [first, second, resolved]})
    assert result.decisions[0].decision == "second question"
    only_resolved = frontier(_state(_parked("A")), stops={"A": [resolved]})
    assert only_resolved.decisions[0].handling == _df().UNKNOWN


def test_a_parked_node_with_no_routing_is_still_a_decision_of_unknown_handling():
    result = frontier(_state(_parked("A"), _node("B", deps=["A"])))
    a = result.decisions[0]
    assert a.handling == _df().UNKNOWN and "parked_by_contract" in a.why
    assert a.holds == ("B",)


def test_a_branch_the_contract_ended_is_listed_and_frees_nothing():
    abort = _routing("a licence question", escalation={"decision": Escalation.ABORT_BRANCH})
    assert abort.handling is Handling.ABORT_BRANCH
    result = frontier(_state(_node("A", BLOCKED, blocked_kind="contract_abort"),
                             _node("B", deps=["A"])), stops={"A": [abort]})
    a = result.decisions[0]
    assert a.handling == "ABORT_BRANCH" and not a.resolvable
    assert a.holds == ("B",) and a.frees == ()


def test_an_unanswerable_decision_ranks_below_one_that_frees_nothing():
    abort = _routing("a", escalation={"decision": Escalation.ABORT_BRANCH})
    result = frontier(
        _state(_node("A", BLOCKED, blocked_kind="contract_abort"), _node("B", deps=["A"]),
               _parked("C")), stops={"A": [abort]})
    assert [d.node_id for d in result.decisions] == ["C", "A"]


def test_a_node_whose_log_could_not_be_read_is_held_as_unreadable():
    result = frontier(_state(_node("A", BLOCKED), _node("B", deps=["A"])),
                      unreadable={"A": "routings.jsonl:3: not a JSON object"})
    a = result.decisions[0]
    assert a.handling == _df().UNREADABLE and "not a JSON object" in a.why
    assert a.holds == ("B",) and result.runnable == ()


def test_a_settled_node_is_no_blocker_and_is_not_walked_through():
    # M depends on the parked A but is MERGED: it satisfies B, whatever A says.
    result = frontier(_state(_parked("A"), _node("M", MERGED, deps=["A"]),
                             _node("B", deps=["M"])))
    assert _by_id(result)["A"].holds == ()
    assert result.runnable == ("B",)


def test_graph_defects_are_unrunnable_and_are_never_freed_by_a_decision():
    result = frontier(_state(
        _parked("A"),
        _node("G", deps=["A", "ghost"]),                  # unknown dependency
        _node("C", deps=["A", "D"]), _node("D", deps=["C"]),   # a cycle behind A
        _node("S", deps=["S"]),                           # depends on itself
        _node("OK", deps=["A"])))
    a = _by_id(result)["A"]
    assert a.holds == ("G", "C", "D", "OK")
    assert a.frees == ("OK",)
    assert result.unrunnable == ("G", "C", "D", "S")
    assert result.runnable == ()


def test_only_queued_nodes_are_unrunnable_and_an_external_one_on_a_cycle_is_listed():
    result = frontier(_state(
        _parked("C1", deps=["C2"]), _node("C2", deps=["C1"]),            # a cycle through a parked node
        _node("E1", deps=["E2"], action_class=ActionClass.EXTERNAL),     # a cycle of external work
        _node("E2", deps=["E1"])))
    assert result.unrunnable == ("C2", "E1", "E2"), "a BLOCKED node is never queued work"
    assert [d.node_id for d in result.decisions] == ["C1"]
    assert result.decisions[0].holds == ("C2",)
    assert result.decisions[0].frees == (), "C2 can never run: it is on the cycle"
    assert result.runnable == ()


def test_an_external_node_waiting_for_other_work_is_queued_and_not_yet_a_decision():
    waiting = _state(_node("r", RUNNING),
                     _node("X", deps=["r"], action_class=ActionClass.EXTERNAL),
                     _node("Y", deps=["X"]), _node("Z"))
    result = frontier(waiting)
    assert result.decisions == () and result.other_blocked == ()
    assert result.runnable == ("Z",)
    # The same graph once the running node has merged: X is reachable, so it is asked.
    done = _state(_node("r", MERGED),
                  _node("X", deps=["r"], action_class=ActionClass.EXTERNAL),
                  _node("Y", deps=["X"]), _node("Z"))
    after = frontier(done)
    x = _by_id(after)["X"]
    assert x.handling == _df().EXTERNAL_ACTION and x.holds == ("Y",) and x.frees == ("Y",)
    assert after.runnable == ("Z",)


def test_an_external_node_behind_a_parked_decision_is_held_by_it_and_frees_nothing():
    result = frontier(_state(_parked("A"),
                             _node("X", deps=["A"], action_class=ActionClass.EXTERNAL),
                             _node("Y", deps=["X"]), _node("W", deps=["A"])))
    a = _by_id(result)["A"]
    assert [d.node_id for d in result.decisions] == ["A"]
    assert a.holds == ("X", "Y", "W")
    assert a.frees == ("W",), "an external node is asked, not run; Y stands behind it too"


@pytest.mark.parametrize("kind", ["contract_abort", "contract_stop"])
@pytest.mark.parametrize("evidence", ["an unreadable log", "a parking routing"])
def test_a_branch_the_nodes_own_kind_says_is_ended_frees_nothing(kind, evidence):
    node = _node("A", BLOCKED, blocked_kind=kind)
    kw = ({"unreadable": {"A": "routings.jsonl:1: damaged"}} if evidence == "an unreadable log"
          else {"stops": {"A": [_routing()]}})
    result = frontier(_state(node, _node("B", deps=["A"])), **kw)
    a = result.decisions[0]
    assert not a.resolvable
    assert a.holds == ("B",) and a.frees == ()


def test_the_lists_follow_the_order_of_the_plan_and_not_the_order_of_the_ids():
    result = frontier(_state(
        _node("z_run", RUNNING),
        _node("x_odd", BLOCKED, note="merge conflict"),
        _node("d_odd", BLOCKED, note="budget"),
        _parked("v_dec"), _node("y_hold", deps=["v_dec"]), _node("b_hold", deps=["v_dec"]),
        _parked("f_dec"), _node("t_hold", deps=["f_dec"]), _node("a_hold", deps=["f_dec"]),
        _node("y_free"), _node("c_free"),
        _node("w_ghost", deps=["nowhere"]), _node("e_ghost", deps=["nowhere"])))
    assert result.runnable == ("y_free", "c_free")
    assert result.other_blocked == ("x_odd", "d_odd")
    assert result.unrunnable == ("w_ghost", "e_ghost")
    assert [d.node_id for d in result.decisions] == ["v_dec", "f_dec"], "equal rank: plan order"
    by = _by_id(result)
    assert by["v_dec"].holds == ("y_hold", "b_hold") and by["v_dec"].frees == ("y_hold", "b_hold")
    assert by["f_dec"].holds == ("t_hold", "a_hold")


def test_a_timed_wait_and_an_unreadable_log_are_decided_in_a_fixed_order():
    timed = frontier(_state(_node("A", BLOCKED, blocked_kind="usage_limit"),
                            _node("B", deps=["A"])), unreadable={"A": "damaged"})
    assert timed.decisions == () and timed.other_blocked == ("A",), \
        "a node that waits for a time is not asked, whatever its log looks like"
    both = frontier(_state(_parked("A")), stops={"A": [_routing("trusted?")]},
                    unreadable={"A": "damaged"})
    assert both.decisions[0].handling == _df().UNREADABLE
    assert "damaged" in both.decisions[0].why and both.decisions[0].decision != "trusted?", \
        "a log that could not be read as a whole is not partly believed"


@pytest.mark.parametrize("case", ["duplicate id", "stops for a stranger",
                                  "unreadable for a stranger", "stops of a string",
                                  "a dict is not a routing", "a reason that is not text"])
def test_input_that_cannot_be_described_is_refused_not_guessed_at(case):
    nodes = [_parked("A"), _node("B")]
    kw = {}
    if case == "duplicate id":
        nodes.append(_node("B"))
    elif case == "stops for a stranger":
        kw["stops"] = {"nope": [_routing()]}
    elif case == "unreadable for a stranger":
        kw["unreadable"] = {"nope": "why"}
    elif case == "stops of a string":
        kw["stops"] = {"A": "PARK_BRANCH"}
    elif case == "a dict is not a routing":
        kw["stops"] = {"A": [{"handling": "PARK_BRANCH", "blocks_branch": True}]}
    else:
        kw["unreadable"] = {"A": 3}
    with pytest.raises(_df().FrontierError):
        frontier(_state(*nodes), **kw)


def test_the_answer_is_immutable_and_the_state_is_not_touched():
    state = _state(_parked("A"), _node("B", deps=["A"]), _node("C"))
    before = state.model_dump_json()
    result = frontier(state)
    assert state.model_dump_json() == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.runnable = ()                                  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.decisions[0].frees = ()                        # type: ignore[misc]
    assert isinstance(result.decisions, tuple) and isinstance(result.decisions[0].holds, tuple)


def test_the_answer_does_not_depend_on_the_order_of_the_mappings():
    nodes = [_parked("A"), _parked("B"), _node("W", deps=["A"]), _node("V", deps=["B"])]
    r1, r2 = _routing("one"), _routing("two")
    first = frontier(_state(*nodes), stops={"A": [r1], "B": [r2]},
                     unreadable={"W": "x"})
    second = frontier(_state(*nodes), stops={"B": [r2], "A": [r1]},
                      unreadable={"W": "x"})
    assert first == second


def _reference(nodes: list[TaskNode]):
    """The same questions answered by other means: holds by walking dependencies
    upwards from each queued node; frees by running the queued work to a fixpoint
    with one decision treated as answered."""
    known = {n.id: n for n in nodes}

    def waits_on(node_id, target, seen):
        for d in known[node_id].dependencies:
            if d in known and not known[d].settled and d not in seen:
                seen.add(d)
                if d == target or waits_on(d, target, seen):
                    return True
        return False

    def runs_after_answer(answered):
        # The answered gate itself only counts once its own dependencies are done.
        done = {n.id for n in nodes if n.settled}
        moved = True
        while moved:
            moved = False
            for n in nodes:
                runs = n.id == answered or (n.lifecycle is READY
                                            and n.action_class is ActionClass.INTERNAL)
                if n.id not in done and runs and all(d in done for d in n.dependencies):
                    done.add(n.id)
                    moved = True
        return done

    def reachable(n):
        return all(d in known and known[d].settled for d in n.dependencies)

    queued = [n for n in nodes if n.lifecycle is READY]
    internal = [n for n in queued if n.action_class is ActionClass.INTERNAL]
    gates = [n.id for n in nodes if n.lifecycle is BLOCKED
             or (n.lifecycle is READY and n.action_class is ActionClass.EXTERNAL
                 and reachable(n))]
    out = {}
    for g in gates:
        done = runs_after_answer(g)
        out[g] = (tuple(w.id for w in queued if waits_on(w.id, g, set())),
                  tuple(w.id for w in internal if w.id in done and waits_on(w.id, g, set())))
    return out


def test_the_frontier_agrees_with_a_reference_over_seeded_random_graphs():
    rng = random.Random(27)
    for _ in range(300):
        size = rng.randint(1, 9)
        ids = [f"n{i}" for i in range(size)]
        nodes = []
        for i, node_id in enumerate(ids):
            deps = [d for d in ids[:i] if rng.random() < 0.35]
            if rng.random() < 0.08:
                deps.append("ghost")
            if rng.random() < 0.06:
                deps.append(rng.choice(ids))               # may close a cycle
            life = rng.choice([READY, READY, READY, BLOCKED, RUNNING, MERGED, CONTINGENT])
            ext = ActionClass.EXTERNAL if rng.random() < 0.15 else ActionClass.INTERNAL
            nodes.append(_node(node_id, life, deps, action_class=ext,
                               blocked_kind="parked_by_contract" if life is BLOCKED else None))
        result = frontier(_state(*nodes))
        got = {d.node_id: (d.holds, d.frees) for d in result.decisions}
        want = _reference(nodes)
        known = {n.id: n for n in nodes}
        free_to_run = tuple(
            n.id for n in nodes if n.lifecycle is READY
            and n.action_class is ActionClass.INTERNAL
            and all(d in known and known[d].settled for d in n.dependencies))
        assert result.runnable == free_to_run
        assert not set(result.runnable) & {w for h, _ in got.values() for w in h}
        # A cycle or an unknown dependency means nothing is freed; the reference's
        # fixpoint says the same because such a node never gets all dependencies done.
        assert got == want, [(n.id, n.lifecycle.value, n.dependencies) for n in nodes]


# --- L: the log reader ---------------------------------------------------------------

def _log(run_dir: Path) -> Path:
    path = run_dir / "autonomy" / "routings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _line(routing: Routing, **extra) -> str:
    return json.dumps({"at": "2026-10-12T00:00:00Z", "contract_digest": "d" * 16,
                       "program_epoch": 1, **json.loads(routing.model_dump_json()), **extra})


def test_the_reader_returns_what_the_governor_wrote_and_only_the_stops(tmp_path):
    governor = bind(tmp_path, contract=_contract())
    parked = governor.decide(decision="needs a licence answer", delegable=False)
    governor.decide(decision="routine", kind="decision", delegable=True)   # resolved by policy
    second = governor.decide(decision="needs a release answer", kind="release")
    record_override(governor, routing=parked, by="hoh unblock", what="answered")
    assert parked.blocks_branch and second.blocks_branch
    got = read_branch_stops(tmp_path)
    assert got == [parked, second]
    assert all(isinstance(r, Routing) for r in got)


def test_a_run_that_never_routed_has_no_stops_and_reading_creates_nothing(tmp_path):
    assert read_branch_stops(tmp_path) == []
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "autonomy").mkdir()
    assert read_branch_stops(tmp_path) == []
    assert list((tmp_path / "autonomy").iterdir()) == []


_GOOD = _line(_routing())


@pytest.mark.parametrize("payload", [
    pytest.param((_GOOD + "\n" + _GOOD[:40] + "\n" + _GOOD).encode(), id="a torn fragment"),
    pytest.param(b"{this is not json}\n", id="not JSON"),
    pytest.param(b"[1, 2]\n", id="a JSON list"),
    pytest.param(b'"PARK_BRANCH"\n', id="a JSON string"),
    pytest.param(b"null\n", id="JSON null"),
    pytest.param(b"[" * 200000 + b"]" * 200000 + b"\n", id="a deeply nested array"),
    pytest.param(_GOOD.replace("licence", "licençe").encode("latin-1"), id="Latin-1 bytes"),
    pytest.param(_GOOD.encode("utf-16"), id="UTF-16 with a BOM"),
    pytest.param(b"\xef\xbb\xbf" + _GOOD.encode(), id="a UTF-8 BOM"),
    pytest.param(_GOOD.encode() + b"\x00\n", id="a NUL byte"),
    pytest.param(_GOOD.replace('"decision": "needs a licence answer"',
                               '"decision": "x\\ud800y"').encode(), id="a lone surrogate"),
    pytest.param(_line(_routing(), surprise=1).encode(), id="an unknown key"),
    pytest.param(_line(_routing(), handling="WAVED_THROUGH").encode(), id="an unknown handling"),
    pytest.param(b'{"at": "2026-10-12T00:00:00Z"}\n', id="a record of no known shape"),
    pytest.param((_GOOD + "\n \n").encode(), id="a non-breaking space line"),
    pytest.param(_line(_routing(), handling="RESOLVED_BY_POLICY", delegable=False).encode(),
                 id="a fabricated approval"),
])
def test_a_log_that_cannot_be_trusted_whole_is_refused(tmp_path, payload):
    _log(tmp_path).write_bytes(payload if payload.endswith(b"\n") else payload + b"\n")
    with pytest.raises(_gov().RoutingLogUnreadable):
        read_branch_stops(tmp_path)


def test_one_good_line_is_read_beside_blank_lines(tmp_path):
    _log(tmp_path).write_text("\n" + _GOOD + "\n\n  \r\n", encoding="utf-8")
    assert len(read_branch_stops(tmp_path)) == 1


@pytest.mark.parametrize("name", ["directory", "dangling link", "link to a good file",
                                  "autonomy is a file", "autonomy is a link", "mode 000"])
def test_a_log_that_is_not_a_plain_file_in_the_run_is_refused(tmp_path, name):
    good = tmp_path / "elsewhere.jsonl"
    good.write_text(_GOOD + "\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    if name == "autonomy is a file":
        (run / "autonomy").write_text("x")
    elif name == "autonomy is a link":
        real = tmp_path / "real-autonomy"
        real.mkdir()
        (real / "routings.jsonl").write_text(_GOOD + "\n", encoding="utf-8")
        (run / "autonomy").symlink_to(real)
    else:
        path = _log(run)
        if name == "directory":
            path.mkdir()
        elif name == "dangling link":
            path.symlink_to(tmp_path / "missing.jsonl")
        elif name == "link to a good file":
            path.symlink_to(good)
        else:
            path.write_text(_GOOD + "\n", encoding="utf-8")
            path.chmod(0)
            if os.access(path, os.R_OK):
                path.chmod(0o600)
                pytest.skip("this process can read a mode 000 file")
    try:
        with pytest.raises(_gov().RoutingLogUnreadable):
            read_branch_stops(run)
    finally:
        if name == "mode 000":
            _log(run).chmod(0o600)


def test_the_size_limit_is_exact(tmp_path):
    assert _gov().MAX_ROUTING_LOG_BYTES == 8 * 1024 * 1024
    base = len(_line(_routing()).encode()) + 1
    pad = _gov().MAX_ROUTING_LOG_BYTES - base
    assert pad > 0
    routing = _routing()
    padded = routing.model_copy(update={"why": routing.why + "p" * pad})
    at_limit = _line(padded) + "\n"
    assert len(at_limit.encode()) == _gov().MAX_ROUTING_LOG_BYTES
    _log(tmp_path).write_text(at_limit, encoding="utf-8")
    assert len(read_branch_stops(tmp_path)) == 1
    over = tmp_path / "over"
    _log(over).write_text(at_limit + "\n", encoding="utf-8")        # one byte more
    with pytest.raises(_gov().RoutingLogUnreadable) as caught:
        read_branch_stops(over)
    assert str(_gov().MAX_ROUTING_LOG_BYTES) in str(caught.value)


def test_only_a_newline_separates_records(tmp_path):
    """U+2028, U+2029 and U+0085 are line separators to `str.splitlines()` and to a text editor, and
    none of them ends a record here. They are written as escapes so that a copy or an editor cannot
    turn one into a space (U+2028 was, once) or into a line break."""
    decision = "before\u2028after\x85end\u2029more"
    odd = _routing().model_copy(update={"decision": decision})
    raw = json.dumps({**json.loads(odd.model_dump_json())}, ensure_ascii=False)
    assert all(ch in raw for ch in "\u2028\x85\u2029")      # the line holds all three, unescaped
    _log(tmp_path).write_text(raw + "\n", encoding="utf-8")
    got = read_branch_stops(tmp_path)
    assert [r.decision for r in got] == [decision]


def test_the_reader_reads_at_most_one_byte_more_than_the_limit(tmp_path, monkeypatch):
    limit = _gov().MAX_ROUTING_LOG_BYTES
    _log(tmp_path).write_bytes(b"x" * (limit + (1 << 20) + 5))
    real_read, taken = os.read, []

    def counting_read(fd, n):
        data = real_read(fd, n)
        taken.append(len(data))
        return data

    monkeypatch.setattr(_gov().os, "read", counting_read)
    with pytest.raises(_gov().RoutingLogUnreadable):
        read_branch_stops(tmp_path)
    monkeypatch.undo()
    assert sum(taken) == limit + 1


def test_a_fifo_in_place_of_the_log_is_refused_and_does_not_block_the_reader(tmp_path):
    """Dropping O_NONBLOCK makes `os.open` of a FIFO with no writer wait for ever. The
    reader runs in a thread so that this test fails, instead of hanging, if it does."""
    fifo = _log(tmp_path)
    os.mkfifo(fifo)
    outcome: list = []

    def attempt():
        try:
            outcome.append(read_branch_stops(tmp_path))
        except BaseException as exc:                      # noqa: BLE001 - reported below
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    blocked = worker.is_alive()
    if blocked:                                            # let the open return, then fail
        opener = os.open(fifo, os.O_RDWR)
        worker.join(10)
        os.close(opener)
    assert not blocked, "the reader waited for a writer on a FIFO"
    assert len(outcome) == 1 and isinstance(outcome[0], _gov().RoutingLogUnreadable)


# --- P: the product's own writer, read back ----------------------------------------------

def _run_state(run_id: str) -> RunState:
    return RunState(run_id=run_id, repo_path="/x", project_name="p", spec_path="/x/s.md",
                    spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16)


def test_a_branch_parked_through_the_product_path_shows_up_as_a_held_decision(tmp_path):
    parked_dir, quiet_dir = tmp_path / "r-a", tmp_path / "r-c"
    parked_dir.mkdir()
    quiet_dir.mkdir()
    governor = bind(parked_dir, contract=_contract())
    state = _run_state("r-a")
    block_under_contract(state, "the developer needs folder trust", governor)
    assert state.blocked_kind == "parked_by_contract"

    # What `_settle` writes for it (orchestrator.py: the launcher's UNDETERMINED
    # verdict "run blocked (<kind>): <reason>"): BLOCKED, no kind, the words in `note`.
    seen = f"run blocked ({state.blocked_kind}): {state.blocked_reason}"
    nodes = [_node("A", BLOCKED, run_id="r-a", blocked_kind=None,
                   note=f"node A: unclassifiable verdict UNDETERMINED -- {seen}"),
             _node("B", deps=["A"]), _node("C", run_id="r-c")]
    alone = frontier(_state(*nodes))
    assert alone.decisions == () and alone.other_blocked == ("A",), \
        "the node itself does not say it is parked: nothing is guessed from its note"
    assert alone.runnable == ("C",)

    stops = {"A": read_branch_stops(parked_dir), "C": read_branch_stops(quiet_dir)}
    result = frontier(_state(*nodes), stops=stops)
    a = result.decisions[0]
    assert a.node_id == "A" and a.handling == "PARK_BRANCH"
    assert a.decision == "the developer needs folder trust"
    assert a.holds == ("B",) and result.runnable == ("C",)
    assert result.other_blocked == ()


def test_a_ledger_that_cannot_be_read_holds_its_node_and_everything_behind_it(tmp_path):
    run = tmp_path / "r-a"
    _log(run).write_bytes(b"{torn\n")
    unreadable = {}
    try:
        read_branch_stops(run)
    except _gov().RoutingLogUnreadable as exc:
        unreadable["A"] = str(exc)
    assert "A" in unreadable
    result = frontier(_state(_node("A", BLOCKED), _node("B", deps=["A"]), _node("C")),
                      unreadable=unreadable)
    assert [d.handling for d in result.decisions] == [_df().UNREADABLE]
    assert result.runnable == ("C",)


def test_the_needs_you_handlings_all_hold_the_branch(tmp_path):
    """ASK_NOW, COLLECT_FOR_DIGEST and PARK_BRANCH are each a stop the reader returns
    and the frontier holds work behind; only RESOLVED_BY_POLICY is not."""
    attended = compile_profile(AutonomyMode.SUPERVISED, contract_id="sup")
    digest = _contract(needs_you=NeedsYou.DIGEST_ONLY)
    made = {
        "ASK_NOW": route(attended, decision="q", kind="decision", delegable=False),
        "COLLECT_FOR_DIGEST": route(digest, decision="q", kind="decision", delegable=False),
        "PARK_BRANCH": route(_contract(), decision="q", kind="decision", delegable=False),
    }
    for handling, routing in made.items():
        assert routing.handling.value == handling and routing.blocks_branch
        result = frontier(_state(_node("A", BLOCKED, blocked_kind="human"),
                                 _node("B", deps=["A"])), stops={"A": [routing]})
        assert result.decisions[0].handling == handling
        assert result.decisions[0].holds == ("B",)
