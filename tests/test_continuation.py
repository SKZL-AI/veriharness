"""Does a parked branch let the independent work of its wave carry on? (V3.3 P3-28, first slice)

`route()` says a parked branch "waits while independent READY work continues", and
`hoh.decisionfrontier` reads which work stands behind which decision. Nothing turned
that into a decision for a wave, recorded it, or noticed that one handling is not
about a branch at all: STOP_PROGRAM, which the plan words as "the whole programme
stops". These tests pin `hoh.autonomy.continues_past` (the one table) and
`hoh.governance.plan_continuation` / `continue_wave` / `read_continuation` (the
reader, the decision and the per-wave record).

Groups: P = the handling table, C = what a wave continues, L = a routing log that
cannot be read, I = independence from the nodes that wait, R = the record.
Every new name is imported inside a helper, so on a tree without the change each
test fails by its own name instead of the file failing to collect. Tests write only
under `tmp_path`, start no agent and read nothing outside it.
"""
from __future__ import annotations

import functools
import json
import os
import threading
from pathlib import Path

import pytest

from hoh.autonomy import (
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    NeedsYou,
    compile_profile,
)
from hoh.contracts import Budgets, RunState
from hoh.governance import Governor, block_under_contract
from hoh.launcher import HohRunLauncher
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode

READY, RUNNING, BLOCKED, MERGED = (Lifecycle.READY, Lifecycle.RUNNING, Lifecycle.BLOCKED,
                                   Lifecycle.MERGED)

UNATTENDED = dict(scope=("src/hoh/",),
                  horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
                  budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))

#: The product's own measured rule (`launcher.py` `depends_on`), not a stand-in.
depends_on = functools.partial(HohRunLauncher.depends_on, None)


def _gov():
    import hoh.governance as module
    return module


def _aut():
    import hoh.autonomy as module
    return module


def _contract(**kw):
    return compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un",
                           **{**UNATTENDED, **kw})


def _stopping(handling: str):
    """A contract whose routing of a non-delegable `decision` is `handling`."""
    if handling == "ASK_NOW":
        return compile_profile(AutonomyMode.SUPERVISED, contract_id="sup")
    if handling == "COLLECT_FOR_DIGEST":
        return compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="plan")
    if handling == "PARK_BRANCH":
        return _contract()
    if handling == "ABORT_BRANCH":
        return _contract(escalation={"decision": Escalation.ABORT_BRANCH})
    if handling == "STOP_PROGRAM":
        return _contract(escalation={"decision": Escalation.STOP_PROGRAM})
    raise AssertionError(handling)


def _node(node_id, lifecycle=READY, deps=(), **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps), **kw)


def _state(*nodes) -> ProjectState:
    return ProjectState(project_id="p", repo_path="/x", nodes=list(nodes))


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    runs, records = tmp_path / "runs", tmp_path / "records"
    runs.mkdir()
    records.mkdir()
    return runs, records


def _stop(runs: Path, run_id: str, contract=None, *, kind="decision", delegable=False):
    """One routing written by the product's own writer into `runs/<run_id>`."""
    governor = Governor(contract=contract or _contract(), run_dir=runs / run_id)
    return governor.decide(decision=f"{run_id} needs an answer", kind=kind,
                           delegable=delegable)


def _log(runs: Path, run_id: str) -> Path:
    path = runs / run_id / "autonomy" / "routings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _graph(**parked_kw):
    """A parked branch `P`, the node behind it, and two independent nodes."""
    return _state(_node("P", BLOCKED, run_id="rp", **parked_kw),
                  _node("D", deps=["P"]), _node("I1"), _node("I2"))


def _plan(state, contract, runs, **kw):
    return _gov().plan_continuation(state, contract, wave_id=kw.pop("wave_id", "w1"),
                                    runs_dir=runs, depends_on=kw.pop("depends_on", depends_on),
                                    **kw)


def _partition(result, runnable):
    """Every runnable node is continued or withheld, and never both."""
    withheld = [w.node_id for w in result.withheld]
    assert sorted([*result.continued, *withheld]) == sorted(runnable)
    assert not set(result.continued) & set(withheld)


# --- P: the one table --------------------------------------------------------------------

def test_every_handling_is_decided_and_only_the_whole_programme_stop_does_not_continue():
    a = _aut()
    assert a.CONTINUES_PAST == {Handling.RESOLVED_BY_POLICY, Handling.ASK_NOW,
                                Handling.COLLECT_FOR_DIGEST, Handling.PARK_BRANCH,
                                Handling.ABORT_BRANCH}
    assert a.STOPS_EVERYTHING == {Handling.STOP_PROGRAM}
    assert a.CONTINUES_PAST | a.STOPS_EVERYTHING == set(Handling), \
        "a handling nobody classified must fail this test, not fall through"
    assert not a.CONTINUES_PAST & a.STOPS_EVERYTHING
    for handling in Handling:
        assert a.continues_past(handling) is (handling is not Handling.STOP_PROGRAM)


@pytest.mark.parametrize("value", [None, "", "park_branch", "PARK_BRANCH ", "STOP_PROGRAM",
                                   "HALT", 0, ("PARK_BRANCH",), ["PARK_BRANCH"], {}, set(),
                                   {"PARK_BRANCH": 1}])
def test_a_value_that_is_not_a_continuing_handling_does_not_continue(value):
    assert _aut().continues_past(value) is False


# --- C: what a wave continues ---------------------------------------------------------------

def test_a_parked_branch_does_not_stop_the_independent_nodes_of_its_wave(tmp_path):
    runs, _ = _roots(tmp_path)
    routing = _stop(runs, "rp")
    assert routing.handling is Handling.PARK_BRANCH
    state = _graph()
    got = _plan(state, _contract(), runs)
    assert got.outcome == "CONTINUE" and got.reason == "" and got.causes == ()
    assert got.continued == ("I1", "I2"), "D waits behind P and is not READY-runnable"
    assert got.withheld == ()
    assert [(d.node_id, d.handling) for d in got.decisions] == [("P", "PARK_BRANCH")]
    assert (got.mode, got.contract_id) == ("UNATTENDED_LONG_HORIZON", "un")
    assert got.contract_digest == _contract().digest() and got.program_epoch == 0


@pytest.mark.parametrize("handling", ["ASK_NOW", "COLLECT_FOR_DIGEST", "PARK_BRANCH",
                                      "ABORT_BRANCH"])
def test_every_handling_that_is_about_one_branch_leaves_the_rest_of_the_wave_running(
        tmp_path, handling):
    runs, _ = _roots(tmp_path)
    contract = _stopping(handling)
    routing = _stop(runs, "rp", contract)
    assert routing.handling.value == handling and routing.blocks_branch, \
        "the control: the writer really produced this handling"
    got = _plan(_graph(), contract, runs)
    assert got.outcome == "CONTINUE" and got.continued == ("I1", "I2")
    assert got.decisions[0].handling == handling
    assert got.mode == contract.mode.value


def test_a_stop_programme_routing_stops_the_whole_wave_and_says_which_node_did(tmp_path):
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    assert _stop(runs, "rp", contract).handling is Handling.STOP_PROGRAM
    got = _plan(_graph(), contract, runs)
    assert got.outcome == "HOLD" and got.reason == "PROGRAMME_STOPPED"
    assert got.causes == ("P",) and got.continued == ()
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("I1", "PROGRAMME_STOPPED", ""), ("I2", "PROGRAMME_STOPPED", "")]
    assert [(d.node_id, d.handling) for d in got.decisions] == [("P", "STOP_PROGRAM")]


def test_the_nodes_own_kind_saying_contract_stop_stops_the_wave_without_a_log(tmp_path):
    runs, _ = _roots(tmp_path)
    stopped = _plan(_graph(blocked_kind="contract_stop"), _contract(), runs)
    assert stopped.outcome == "HOLD" and stopped.reason == "PROGRAMME_STOPPED"
    assert stopped.causes == ("P",)
    abandoned = _plan(_graph(blocked_kind="contract_abort"), _contract(), runs)
    assert abandoned.outcome == "CONTINUE" and abandoned.continued == ("I1", "I2"), \
        "an abandoned branch is one branch"


def test_a_stop_earlier_in_the_log_still_stops_the_programme_after_a_later_park(tmp_path):
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    assert _stop(runs, "rp", contract, kind="decision").handling is Handling.STOP_PROGRAM
    assert _stop(runs, "rp", contract, kind="scope").handling is Handling.PARK_BRANCH
    got = _plan(_graph(), contract, runs)
    assert got.outcome == "HOLD" and got.reason == "PROGRAMME_STOPPED" and got.causes == ("P",)
    # What the frontier shows is the last stop: the wave is stricter than that.
    assert got.decisions[0].handling == "PARK_BRANCH"


def test_a_stop_in_the_log_of_a_node_that_is_no_longer_blocked_is_history(tmp_path):
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    _stop(runs, "rp", contract)
    answered = _state(_node("P", READY, run_id="rp"), _node("I1"))
    got = _plan(answered, contract, runs)
    assert got.outcome == "CONTINUE" and got.continued == ("P", "I1")


def test_a_wave_under_a_contract_that_does_not_hold_together_continues_nothing(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    broken = _contract().model_copy(update={"needs_you": NeedsYou.ON})  # no validation
    assert _gov().coherence(broken), "the control: this really is incoherent"
    got = _plan(_graph(), broken, runs)
    assert got.outcome == "HOLD" and got.reason == "CONTRACT_INCOHERENT" and got.causes == ()
    assert [w.node_id for w in got.withheld] == ["I1", "I2"] and got.continued == ()


def test_the_wave_level_reasons_are_decided_in_a_fixed_order(tmp_path):
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    _stop(runs, "rp", contract)
    _log(runs, "rq").write_bytes(b"{torn\n")
    state = _state(_node("P", BLOCKED, run_id="rp"), _node("Q", BLOCKED, run_id="rq"),
                   _node("I1"))
    both = _plan(state, contract, runs)
    assert both.reason == "PROGRAMME_STOPPED" and both.causes == ("P",)
    broken = contract.model_copy(update={"needs_you": NeedsYou.ON})
    assert _plan(state, broken, runs).reason == "CONTRACT_INCOHERENT"


def test_an_external_node_and_a_node_behind_a_timed_wait_do_not_change_what_continues(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp"),
                   _node("X", action_class=ActionClass.EXTERNAL),
                   _node("T", BLOCKED, blocked_kind="usage_limit"),
                   _node("A", deps=["T"]), _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("I1",), "an EXTERNAL node is a decision, never continued work"
    assert {d.node_id: d.handling for d in got.decisions} == {
        "P": "PARK_BRANCH", "X": "EXTERNAL_ACTION"}


def test_a_parked_run_with_no_log_is_a_held_branch_and_not_a_stop(tmp_path):
    """The control for L: no log at all means "never routed", which is what a node
    blocked for a person looks like. It holds its branch and nothing else."""
    runs, _ = _roots(tmp_path)
    state = _state(_node("P", BLOCKED, run_id="never-ran", blocked_kind="human"),
                   _node("D", deps=["P"]), _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.outcome == "CONTINUE" and got.continued == ("I1",)
    assert [(d.node_id, d.handling) for d in got.decisions] == [("P", "UNKNOWN")]
    assert list(runs.iterdir()) == [], "reading creates nothing"


def test_the_product_path_that_parks_a_run_is_what_the_wave_reads(tmp_path):
    runs, _ = _roots(tmp_path)
    governor = Governor(contract=_contract(), run_dir=runs / "rp")
    state = RunState(run_id="rp", repo_path="/x", project_name="p", spec_path="/x/s.md",
                     spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16)
    block_under_contract(state, "the developer needs folder trust", governor)
    assert state.blocked_kind == "parked_by_contract"
    # `_settle` leaves the node BLOCKED with no kind: the reader goes by the log.
    got = _plan(_graph(blocked_kind=None), _contract(), runs)
    assert got.outcome == "CONTINUE" and got.continued == ("I1", "I2")
    assert got.decisions[0].handling == "PARK_BRANCH"


def test_a_wave_changes_neither_the_state_nor_the_runs(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    before = {p: p.read_bytes() for p in runs.rglob("*") if p.is_file()}
    state = _graph()
    snapshot = state.model_dump()
    _plan(state, _contract(), runs)
    assert state.model_dump() == snapshot
    assert {p: p.read_bytes() for p in runs.rglob("*") if p.is_file()} == before
    assert sorted(p.name for p in runs.rglob("*")) == sorted(
        ["rp", "autonomy", "routings.jsonl"])


def test_a_stop_in_the_log_of_a_running_node_stops_the_wave(tmp_path):
    """A run that STOPs is CANCELLED and the resume loop halts leaving its node RUNNING:
    the log of a RUNNING node is where that stop is."""
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    assert _stop(runs, "rp", contract).handling is Handling.STOP_PROGRAM
    state = _state(_node("P", RUNNING, run_id="rp"), _node("I1"))
    got = _plan(state, contract, runs)
    assert got.outcome == "HOLD" and got.reason == "PROGRAMME_STOPPED"
    assert got.causes == ("P",) and got.continued == ()
    parked = _state(_node("P", RUNNING, run_id="rq"), _node("I1"))
    _stop(runs, "rq", _contract())                         # the control: a park, not a stop
    assert _plan(parked, _contract(), runs).continued == ("I1",)


def test_a_stop_behind_a_timed_kind_still_stops_the_programme(tmp_path):
    """The kind says what the node waits for; a stop the log says it was given is a stop
    all the same (a stop is a stop, in any position of the log, D6)."""
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    _stop(runs, "rp", contract)
    state = _state(_node("P", BLOCKED, run_id="rp", blocked_kind="usage_limit"), _node("I1"))
    got = _plan(state, contract, runs)
    assert got.outcome == "HOLD" and got.reason == "PROGRAMME_STOPPED" and got.causes == ("P",)


@pytest.mark.parametrize("kind", ["contract_stopped", "Contract_Stop", "CONTRACT_STOP",
                                  "xcontract_stop", "contract_stop_", "contract-stop"])
def test_a_kind_is_compared_exactly_and_only_contract_stop_stops(tmp_path, kind):
    runs, _ = _roots(tmp_path)
    got = _plan(_graph(blocked_kind=kind), _contract(), runs)
    assert got.outcome == "CONTINUE" and got.continued == ("I1", "I2"), kind


# --- L: a routing log that cannot be read --------------------------------------------------

def _break(runs: Path, how: str) -> None:
    path = _log(runs, "rp")
    if how == "torn":
        path.write_bytes(b'{"decision": "x", "kind"\n')
    elif how == "not UTF-8":
        path.write_bytes(b"\xff\xfe{}\n")
    elif how == "not an object":
        path.write_bytes(b"[1, 2]\n")
    elif how == "fabricated approval":
        path.write_text(json.dumps({"decision": "d", "kind": "decision",
                                    "handling": "RESOLVED_BY_POLICY", "why": "w",
                                    "delegable": False, "blocks_branch": True}) + "\n")
    elif how == "directory":
        path.mkdir()
    elif how == "dangling link":
        path.symlink_to(runs / "nowhere.jsonl")
    elif how == "mode 000":
        path.write_text("\n")
        path.chmod(0)
        if os.access(path, os.R_OK):
            path.chmod(0o600)
            pytest.skip("this process can read a mode 000 file")
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["torn", "not UTF-8", "not an object", "fabricated approval",
                                 "directory", "dangling link", "mode 000"])
def test_a_log_that_cannot_be_read_holds_the_whole_wave(tmp_path, how):
    runs, _ = _roots(tmp_path)
    _break(runs, how)
    try:
        got = _plan(_graph(), _contract(), runs)
    finally:
        if how == "mode 000":
            _log(runs, "rp").chmod(0o600)
    assert got.outcome == "HOLD" and got.reason == "ROUTING_LOG_UNREADABLE"
    assert got.causes == ("P",) and got.continued == ()
    assert [w.node_id for w in got.withheld] == ["I1", "I2"]
    assert [(d.node_id, d.handling) for d in got.decisions] == [("P", "UNREADABLE")]


def test_the_same_graph_continues_once_the_log_can_be_read(tmp_path):
    """The control for the whole group: only the log differs."""
    runs, _ = _roots(tmp_path)
    _log(runs, "rp").write_bytes(b"{torn\n")
    assert _plan(_graph(), _contract(), runs).outcome == "HOLD"
    _log(runs, "rp").write_bytes(b"")
    assert _plan(_graph(), _contract(), runs).outcome == "CONTINUE"


def test_a_link_in_place_of_the_autonomy_directory_or_the_run_directory_holds_the_wave(tmp_path):
    runs, _ = _roots(tmp_path)
    good = tmp_path / "elsewhere"
    (good / "autonomy").mkdir(parents=True)
    (good / "autonomy" / "routings.jsonl").write_text("")
    (runs / "ra").symlink_to(good)                        # the run directory itself
    (runs / "rb").mkdir()
    (runs / "rb" / "autonomy").symlink_to(good / "autonomy")
    for run_id in ("ra", "rb"):
        state = _state(_node("P", BLOCKED, run_id=run_id), _node("I1"))
        got = _plan(state, _contract(), runs)
        assert got.outcome == "HOLD" and got.reason == "ROUTING_LOG_UNREADABLE", run_id
        assert got.causes == ("P",)


def test_a_run_directory_that_is_a_file_holds_the_wave(tmp_path):
    runs, _ = _roots(tmp_path)
    (runs / "rp").write_text("not a run")
    got = _plan(_graph(), _contract(), runs)
    assert got.outcome == "HOLD" and got.causes == ("P",)


@pytest.mark.parametrize("run_id", ["..", ".", ".hidden", "_arenas", "backups", "a/b",
                                    "a b", "a\nb", "ü"])
def test_a_run_id_that_is_not_one_holds_the_wave_and_reads_nothing_outside(tmp_path, run_id):
    runs, _ = _roots(tmp_path)
    (tmp_path / "autonomy").mkdir()
    (tmp_path / "autonomy" / "routings.jsonl").write_text("")   # what '..' would reach
    state = _state(_node("P", BLOCKED, run_id=run_id), _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.outcome == "HOLD" and got.reason == "ROUTING_LOG_UNREADABLE"
    assert got.causes == ("P",)


def test_a_node_without_a_run_id_is_read_under_its_own_id(tmp_path):
    runs, _ = _roots(tmp_path)
    _log(runs, "P").write_bytes(b"{torn\n")
    state = _state(_node("P", BLOCKED), _node("I1"))
    assert _plan(state, _contract(), runs).reason == "ROUTING_LOG_UNREADABLE"


def test_a_fifo_in_place_of_a_log_holds_the_wave_and_does_not_block(tmp_path):
    runs, _ = _roots(tmp_path)
    os.mkfifo(_log(runs, "rp"))
    outcome: list = []

    def attempt():
        try:
            outcome.append(_plan(_graph(), _contract(), runs))
        except BaseException as exc:                      # noqa: BLE001 - reported below
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    blocked = worker.is_alive()
    if blocked:
        opener = os.open(_log(runs, "rp"), os.O_RDWR)
        worker.join(10)
        os.close(opener)
    assert not blocked, "the wave waited for a writer on a FIFO"
    assert len(outcome) == 1 and outcome[0].reason == "ROUTING_LOG_UNREADABLE"


def test_an_oversize_log_holds_the_wave(tmp_path, monkeypatch):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    size = _log(runs, "rp").stat().st_size
    monkeypatch.setattr(_gov(), "MAX_ROUTING_LOG_BYTES", size)
    assert _plan(_graph(), _contract(), runs).outcome == "CONTINUE"
    monkeypatch.setattr(_gov(), "MAX_ROUTING_LOG_BYTES", size - 1)
    assert _plan(_graph(), _contract(), runs).reason == "ROUTING_LOG_UNREADABLE"


def test_a_timed_wait_is_not_read_so_a_torn_log_behind_it_holds_nothing(tmp_path):
    """A usage limit or a storage park waits for a time; its node's own kind is what it
    waits for now (`decisionfrontier.TIMED_KINDS`), so its log is not consulted."""
    runs, _ = _roots(tmp_path)
    _log(runs, "rt").write_bytes(b"{torn\n")
    state = _state(_node("T", BLOCKED, run_id="rt", blocked_kind="usage_limit"),
                   _node("S", BLOCKED, run_id="rt", blocked_kind="storage_pressure"),
                   _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.outcome == "CONTINUE" and got.continued == ("I1",) and got.decisions == ()
    held = _state(_node("T", BLOCKED, run_id="rt", blocked_kind="human"), _node("I1"))
    assert _plan(held, _contract(), runs).reason == "ROUTING_LOG_UNREADABLE", \
        "the control: the same log behind a node that waits for a person does hold the wave"


def test_a_run_id_the_filesystem_cannot_name_holds_the_wave(tmp_path):
    runs, _ = _roots(tmp_path)
    state = _state(_node("P", BLOCKED, run_id="r" * 300), _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.outcome == "HOLD" and got.reason == "ROUTING_LOG_UNREADABLE"
    assert got.causes == ("P",)


@pytest.mark.parametrize("what", ["missing", "a file", "a dangling link"])
def test_a_runs_directory_that_is_not_one_is_refused_before_anything_is_read(tmp_path, what):
    target = tmp_path / "runs"
    if what == "a file":
        target.write_text("x")
    elif what == "a dangling link":
        target.symlink_to(tmp_path / "nowhere")
    with pytest.raises(_gov().ContinuationError):
        _plan(_graph(), _contract(), target)


def test_a_runs_directory_reached_through_a_link_is_the_directory_it_names(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    alias = tmp_path / "alias"
    alias.symlink_to(runs)
    assert _plan(_graph(), _contract(), alias).continued == ("I1", "I2")


def test_a_graph_without_a_frontier_is_refused_and_nothing_is_recorded(tmp_path):
    runs, records = _roots(tmp_path)
    twins = _state(_node("I1"), _node("I1"))
    with pytest.raises(ValueError):                        # FrontierError is a ValueError
        _gov().continue_wave(twins, _contract(), wave_id="w1", runs_dir=runs,
                             depends_on=depends_on, record_dir=records)
    assert list(records.iterdir()) == []


@pytest.mark.parametrize("name", ["contract", "state", "wave_id"])
def test_arguments_that_are_not_what_they_must_be_are_refused(tmp_path, name):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    args = {"state": _graph(), "contract": _contract(), "wave_id": "w1"}
    args[name] = {"contract": _contract().model_dump(), "state": _graph().model_dump(),
                  "wave_id": 1}[name]
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(args["state"], args["contract"], wave_id=args["wave_id"],
                             runs_dir=runs, depends_on=depends_on, record_dir=records)
    assert list(records.iterdir()) == []
    with pytest.raises(_gov().ContinuationError):
        _gov().record_continuation(records, {"wave_id": "w1"})


def test_an_unreadable_log_of_a_running_node_holds_the_wave(tmp_path):
    runs, _ = _roots(tmp_path)
    _log(runs, "rp").write_bytes(b"{torn\n")
    state = _state(_node("P", RUNNING, run_id="rp"), _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.outcome == "HOLD" and got.reason == "ROUTING_LOG_UNREADABLE"
    assert got.causes == ("P",) and got.continued == ()
    never = _state(_node("P", RUNNING, run_id="never-ran"), _node("I1"))
    assert _plan(never, _contract(), runs).continued == ("I1",), \
        "the control: a run with no directory was never routed"


@pytest.mark.parametrize("what", ["a non-ASCII contract id", "a contract id of 129 characters",
                                  "an epoch above the record's bound"])
def test_a_contract_the_record_cannot_name_is_refused_before_a_log_is_read(
        tmp_path, monkeypatch, what):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    base = _contract()
    if what == "a non-ASCII contract id":
        contract = base.model_copy(update={"contract_id": "ünattended"})
    elif what == "a contract id of 129 characters":
        contract = base.model_copy(update={"contract_id": "c" * 129})
    else:
        contract = base.model_copy(update={"program_epoch": 2**40 + 1})

    def read(*args, **kwargs):
        raise AssertionError("a routing log was read for a wave that cannot be recorded")

    monkeypatch.setattr(_gov(), "read_branch_stops", read)
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(_graph(), contract, wave_id="w1", runs_dir=runs,
                             depends_on=depends_on, record_dir=records)
    assert list(records.iterdir()) == []


# --- I: independence from the nodes that wait -----------------------------------------------

def test_a_node_that_writes_what_the_parked_branch_writes_is_withheld(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp", writes=["src/a.py"]),
                   _node("W", writes=["src/a.py"]), _node("I1", writes=["src/b.py"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("I1",)
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("W", "SEMANTIC_DEPENDENCY", "P")]
    _partition(got, ["W", "I1"])


def test_a_semantic_read_withholds_in_both_directions(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp", writes=["a"], semantic_reads=["c"]),
                   _node("R", semantic_reads=["a"]),      # reads what P will write
                   _node("S", writes=["c"]),              # writes what P's result depends on
                   _node("I1", writes=["z"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("I1",)
    assert {w.node_id: w.blocked_by for w in got.withheld} == {"R": "P", "S": "P"}


def test_a_running_node_withholds_like_a_parked_one_and_a_settled_one_does_not(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp"),
                   _node("G", RUNNING, writes=["a"]),
                   _node("M", MERGED, writes=["b"]),
                   _node("RA", writes=["a"]), _node("RB", writes=["b"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("RB",)
    assert [(w.node_id, w.blocked_by) for w in got.withheld] == [("RA", "G")]


def test_a_ready_external_node_withholds_what_is_not_independent_of_it(tmp_path):
    """It is a decision today and runs the moment somebody answers it: work that reads
    what it writes must not have been continued in the meantime."""
    runs, _ = _roots(tmp_path)
    state = _state(_node("X", action_class=ActionClass.EXTERNAL, writes=["a"]),
                   _node("RA", semantic_reads=["a"]), _node("RB", writes=["b"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("RB",)
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("RA", "SEMANTIC_DEPENDENCY", "X")]
    assert [(d.node_id, d.handling) for d in got.decisions] == [("X", "EXTERNAL_ACTION")]


@pytest.mark.parametrize("answer", [None, True, 1, "no", [], 0])
def test_an_answer_that_is_not_exactly_false_is_a_dependency(tmp_path, answer):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    got = _plan(_graph(), _contract(), runs, depends_on=lambda a, b: answer)
    assert got.continued == () and [w.node_id for w in got.withheld] == ["I1", "I2"]
    assert {w.blocked_by for w in got.withheld} == {"P"}


def test_a_callable_that_raises_is_a_dependency(tmp_path):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")

    def boom(a, b):
        raise RuntimeError("cannot say")

    got = _plan(_graph(), _contract(), runs, depends_on=boom)
    assert got.continued == () and len(got.withheld) == 2


@pytest.mark.parametrize("direction", ["waiting-first", "candidate-first"])
def test_both_directions_of_the_question_are_asked(tmp_path, direction):
    """A rule that answers True for one order only must still withhold."""
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")

    def one_way(a, b):
        parked_first = a.id == "P"
        return parked_first if direction == "waiting-first" else not parked_first

    state = _state(_node("P", BLOCKED, run_id="rp"), _node("I1"), _node("I2"))
    got = _plan(state, _contract(), runs, depends_on=one_way)
    assert got.continued == ()


def test_the_launchers_own_rule_is_what_the_tests_above_use():
    a, b = _node("a", writes=["x"]), _node("b", semantic_reads=["x"])
    assert depends_on(a, b) is True and depends_on(b, a) is True
    assert depends_on(_node("c"), _node("d")) is False


def test_a_callable_that_is_not_one_is_refused(tmp_path):
    runs, _ = _roots(tmp_path)
    with pytest.raises(_gov().ContinuationError):
        _plan(_graph(), _contract(), runs, depends_on=None)


def test_a_node_held_behind_the_parked_branch_still_withholds(tmp_path):
    """`D` waits behind `P`, is not runnable, and will write `out/d.txt` the moment `P`
    is answered: what reads it must not have been continued in the meantime."""
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp"),
                   _node("D", deps=["P"], writes=["out/d.txt"]),
                   _node("I1", semantic_reads=["out/d.txt"]), _node("I2", writes=["z"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("I2",)
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("I1", "SEMANTIC_DEPENDENCY", "D")]


def test_a_contingent_node_withholds_like_any_node_that_is_not_settled(tmp_path):
    runs, _ = _roots(tmp_path)
    state = _state(_node("C", Lifecycle.CONTINGENT, writes=["a"]),
                   _node("RA", semantic_reads=["a"]), _node("RB", writes=["b"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("RB",)
    assert [(w.node_id, w.blocked_by) for w in got.withheld] == [("RA", "C")]


def test_a_timed_wait_withholds_what_is_not_independent_of_it(tmp_path):
    """Its log is not read, but it resumes and writes: it is one of the nodes a continued
    node must be independent of, whatever it waits for."""
    runs, _ = _roots(tmp_path)
    state = _state(_node("W", BLOCKED, blocked_kind="usage_limit", writes=["a"]),
                   _node("RA", semantic_reads=["a"]), _node("RB", writes=["b"]))
    got = _plan(state, _contract(), runs)
    assert got.continued == ("RB",)
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("RA", "SEMANTIC_DEPENDENCY", "W")]
    assert got.decisions == ()


@pytest.mark.parametrize("order", [("G", "P"), ("P", "G")])
def test_the_first_node_it_is_not_independent_of_is_named_in_graph_order(tmp_path, order):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    make = {"G": lambda: _node("G", RUNNING, writes=["a"]),
            "P": lambda: _node("P", BLOCKED, run_id="rp", writes=["a"])}
    state = _state(*(make[k]() for k in order), _node("RA", semantic_reads=["a"]))
    got = _plan(state, _contract(), runs)
    assert [(w.node_id, w.blocked_by) for w in got.withheld] == [("RA", order[0])]


# --- R: the record ----------------------------------------------------------------------------

def _wave(tmp_path, *, wave_id="w1", contract=None, state=None):
    runs, records = tmp_path / "runs", tmp_path / "records"
    runs.mkdir(exist_ok=True)
    records.mkdir(exist_ok=True)
    contract = contract or _contract()
    if not (runs / "rp").exists():
        _stop(runs, "rp", contract)
    return _gov().continue_wave(state or _graph(), contract, wave_id=wave_id, runs_dir=runs,
                                depends_on=depends_on, record_dir=records), records


def test_the_continuation_is_on_disk_before_it_is_returned_and_reads_back_equal(tmp_path):
    got, records = _wave(tmp_path)
    path = records / "continuation-w1.json"
    assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
    assert _gov().read_continuation(records, "w1") == got
    assert _gov().parse_continuation(got.to_bytes()) == got
    assert path.read_bytes() == got.to_bytes() and path.read_bytes().isascii()
    assert [p.name for p in records.iterdir()] == ["continuation-w1.json"]


def test_the_record_names_the_contract_that_decided(tmp_path):
    contract = _contract().pin(7)
    got, records = _wave(tmp_path, contract=contract)
    assert (got.contract_id, got.contract_digest) == ("un", contract.digest())
    assert (got.mode, got.program_epoch) == ("UNATTENDED_LONG_HORIZON", 7)
    on_disk = json.loads((records / "continuation-w1.json").read_text())
    assert on_disk["schema"] == "hoh-wave-continuation/1"
    assert on_disk["contract_digest"] == contract.digest() != _contract().digest()


def test_a_waves_continuation_is_recorded_once_and_the_first_stays(tmp_path):
    _, records = _wave(tmp_path)
    first = (records / "continuation-w1.json").read_bytes()
    runs = tmp_path / "runs"
    _stop(runs, "rq")                                     # something else to say
    again = _state(_node("P", BLOCKED, run_id="rp"), _node("Q", BLOCKED, run_id="rq"),
                   _node("I1"))
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(again, _contract(), wave_id="w1", runs_dir=runs,
                             depends_on=depends_on, record_dir=records)
    assert (records / "continuation-w1.json").read_bytes() == first


@pytest.mark.parametrize("what", ["missing", "a file", "a link to a directory"])
def test_a_record_directory_that_is_not_one_of_its_own_continues_nothing(tmp_path, what):
    runs, _ = _roots(tmp_path)
    _stop(runs, "rp")
    target = tmp_path / "target"
    if what == "a file":
        target.write_text("x")
    elif what == "a link to a directory":
        (tmp_path / "real").mkdir()
        target.symlink_to(tmp_path / "real")
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(_graph(), _contract(), wave_id="w1", runs_dir=runs,
                             depends_on=depends_on, record_dir=target)
    if what == "a link to a directory":
        assert list((tmp_path / "real").iterdir()) == []


@pytest.mark.parametrize("wave_id", ["", "../x", "a/b", "x\n", " w", "w ", ".w", "ü", None, 7,
                                     "w" * 65])
def test_a_wave_id_a_record_cannot_hold_is_refused_before_anything_is_read(
        tmp_path, monkeypatch, wave_id):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")

    def read(*args, **kwargs):
        raise AssertionError("a routing log was read for a wave that cannot be recorded")

    monkeypatch.setattr(_gov(), "read_branch_stops", read)
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(_graph(), _contract(), wave_id=wave_id, runs_dir=runs,
                             depends_on=depends_on, record_dir=records)
    assert list(records.iterdir()) == []


def test_a_wave_id_of_exactly_64_characters_is_accepted(tmp_path):
    got, records = _wave(tmp_path, wave_id="w" * 64)
    assert (records / ("continuation-" + "w" * 64 + ".json")).is_file() and got.wave_id == "w" * 64


def test_a_continuation_of_a_hold_is_recorded_too(tmp_path):
    contract = _stopping("STOP_PROGRAM")
    runs = tmp_path / "runs"
    runs.mkdir()
    _stop(runs, "rp", contract)
    got, records = _wave(tmp_path, contract=contract)
    assert got.outcome == "HOLD"
    assert _gov().read_continuation(records, "w1") == got


def test_two_callers_of_one_wave_id_get_one_record_and_one_refusal(tmp_path):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    barrier = threading.Barrier(2)
    outcomes: list = []

    def call():
        barrier.wait(10)
        try:
            outcomes.append(_gov().continue_wave(
                _graph(), _contract(), wave_id="w1", runs_dir=runs, depends_on=depends_on,
                record_dir=records))
        except _gov().ContinuationError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    kinds = sorted(type(o).__name__ for o in outcomes)
    assert kinds == ["ContinuationError", "WaveContinuation"], kinds
    assert [p.name for p in records.iterdir()] == ["continuation-w1.json"]


def test_no_record_is_none_and_a_record_of_another_wave_is_refused(tmp_path):
    got, records = _wave(tmp_path)
    assert _gov().read_continuation(records, "w2") is None
    (records / "continuation-w2.json").write_bytes(got.to_bytes())     # w1's words under w2
    with pytest.raises(_gov().ContinuationError):
        _gov().read_continuation(records, "w2")


def _valid() -> dict:
    return {
        "schema": "hoh-wave-continuation/1", "wave_id": "w1", "contract_id": "un",
        "contract_digest": "0123456789abcdef", "mode": "UNATTENDED_LONG_HORIZON",
        "program_epoch": 0, "outcome": "CONTINUE", "reason": "", "causes": [],
        "continued": ["I1"], "withheld": [{"node_id": "W", "reason": "SEMANTIC_DEPENDENCY",
                                           "blocked_by": "P"}],
        "decisions": [{"node_id": "P", "handling": "PARK_BRANCH"}]}


def _hold(**kw) -> dict:
    return {**_valid(), "outcome": "HOLD", "reason": "PROGRAMME_STOPPED", "causes": ["P"],
            "continued": [], "withheld": [{"node_id": "I1", "reason": "PROGRAMME_STOPPED",
                                           "blocked_by": ""}], **kw}


def test_the_valid_shapes_used_below_are_valid():
    parse = _gov().parse_continuation
    assert parse(_canon(_valid())).continued == ("I1",)
    assert parse(_canon(_hold())).reason == "PROGRAMME_STOPPED"


def _canon(obj: dict) -> bytes:
    """What the writer would write for these values (its own serialisation)."""
    return (json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _mut(base, **kw):
    return {**base(), **kw}


@pytest.mark.parametrize("record", [
    pytest.param(_mut(_valid, schema="hoh-wave-continuation/2"), id="another schema"),
    pytest.param({k: v for k, v in _valid().items() if k != "causes"}, id="a missing field"),
    pytest.param(_mut(_valid, surprise=1), id="an unknown field"),
    pytest.param(_mut(_valid, wave_id="../w"), id="a wave id with a slash"),
    pytest.param(_mut(_valid, contract_id="un\x00"), id="a NUL in the contract id"),
    pytest.param(_mut(_valid, contract_id="ünî"), id="a non-ASCII contract id"),
    pytest.param(_mut(_valid, contract_digest="XYZ"), id="a digest that is not hex"),
    pytest.param(_mut(_valid, contract_digest="0123456789ABCDEF"), id="an upper-case digest"),
    pytest.param(_mut(_valid, mode="SELF_DRIVING"), id="an unknown mode"),
    pytest.param(_mut(_valid, program_epoch=True), id="a boolean epoch"),
    pytest.param(_mut(_valid, program_epoch=-1), id="a negative epoch"),
    pytest.param(_mut(_valid, outcome="MAYBE"), id="an unknown outcome"),
    pytest.param(_mut(_valid, reason="PROGRAMME_STOPPED"), id="CONTINUE with a reason"),
    pytest.param(_mut(_valid, causes=["P"]), id="CONTINUE with causes"),
    pytest.param(_mut(_valid, continued=["I1", "I1"]), id="a node continued twice"),
    pytest.param(_mut(_valid, continued=["W"]), id="a node continued and withheld"),
    pytest.param(_mut(_valid, continued=["a b"]), id="a node id with a space"),
    pytest.param(_mut(_valid, continued="I1"), id="continued as a string"),
    pytest.param(_mut(_valid, withheld=[{"node_id": "W", "reason": "SEMANTIC_DEPENDENCY",
                                         "blocked_by": ""}]), id="a dependency with no blocker"),
    pytest.param(_mut(_valid, withheld=[{"node_id": "W", "reason": "PROGRAMME_STOPPED",
                                         "blocked_by": "P"}]), id="a stop reason in a CONTINUE"),
    pytest.param(_mut(_valid, withheld=[{"node_id": "W", "reason": "SEMANTIC_DEPENDENCY",
                                         "blocked_by": "P", "x": 1}]), id="a row with a field more"),
    pytest.param(_mut(_valid, decisions=[{"node_id": "P", "handling": "WAVED_THROUGH"}]),
                 id="an unknown handling"),
    pytest.param(_mut(_valid, decisions=[{"node_id": "P", "handling": "UNKNOWN"},
                                         {"node_id": "P", "handling": "UNKNOWN"}]),
                 id="a decision twice"),
    pytest.param(_hold(continued=["I2"]), id="HOLD with continued nodes"),
    pytest.param(_hold(reason="BECAUSE"), id="HOLD with an unknown reason"),
    pytest.param(_hold(reason=""), id="HOLD with no reason"),
    pytest.param(_hold(causes=[]), id="a stop with no node that stopped"),
    pytest.param(_hold(reason="CONTRACT_INCOHERENT"), id="an incoherent contract with causes"),
    pytest.param(_hold(withheld=[{"node_id": "I1", "reason": "ROUTING_LOG_UNREADABLE",
                                  "blocked_by": ""}]), id="HOLD withholding for another reason"),
])
def test_a_record_that_is_not_exactly_what_the_writer_makes_is_refused(record):
    """In the writer's own serialisation, so that it is the value and invariant checks that
    refuse and not the byte comparison that stands behind them."""
    with pytest.raises(_gov().ContinuationError):
        _gov().parse_continuation(_canon(record))


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty"),
    pytest.param(b"{", id="a torn file"),
    pytest.param(b"[]", id="a JSON list"),
    pytest.param(b"null", id="JSON null"),
    pytest.param(b'"CONTINUE"', id="a JSON string"),
    pytest.param(b"\xff\xfe", id="not UTF-8"),
    pytest.param(b"[" * 200000 + b"]" * 200000, id="a deeply nested array"),
    pytest.param(_canon(_valid()).replace(b'"wave_id": "w1",',
                                                       b'"wave_id": "w1", "wave_id": "w2",'),
                 id="a repeated key"),
    pytest.param(_canon(_valid()).replace(b'"program_epoch": 0',
                                                       b'"program_epoch": NaN'), id="NaN"),
    pytest.param(_canon(_valid()) + b"\x00", id="a NUL after the document"),
])
def test_bytes_that_are_not_a_record_are_refused(payload):
    with pytest.raises(_gov().ContinuationError):
        _gov().parse_continuation(payload)


@pytest.mark.parametrize("name", ["directory", "dangling link", "link to a good file",
                                  "second name", "fifo"])
def test_a_record_file_that_is_not_a_plain_single_file_is_refused(tmp_path, name):
    got, records = _wave(tmp_path)
    good = tmp_path / "good.json"
    good.write_bytes(got.to_bytes())
    path = records / "continuation-w2.json"
    if name == "directory":
        path.mkdir()
    elif name == "dangling link":
        path.symlink_to(tmp_path / "missing.json")
    elif name == "link to a good file":
        path.symlink_to(good)
    elif name == "second name":
        os.link(good, path)
    else:
        os.mkfifo(path)
    outcome: list = []

    def attempt():
        try:
            outcome.append(_gov().read_continuation(records, "w2"))
        except BaseException as exc:                      # noqa: BLE001 - reported below
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the reader waited on a FIFO"
    assert len(outcome) == 1 and isinstance(outcome[0], _gov().ContinuationError)


def test_the_size_limit_is_exact_for_writing_and_for_reading(tmp_path, monkeypatch):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    plan = _plan(_graph(), _contract(), runs)
    size = len(plan.to_bytes())
    assert _gov().MAX_CONTINUATION_BYTES == 1 << 20
    monkeypatch.setattr(_gov(), "MAX_CONTINUATION_BYTES", size - 1)
    with pytest.raises(_gov().ContinuationError):
        _gov().record_continuation(records, plan)
    assert list(records.iterdir()) == []
    with pytest.raises(_gov().ContinuationError):
        _gov().parse_continuation(plan.to_bytes())
    monkeypatch.setattr(_gov(), "MAX_CONTINUATION_BYTES", size)
    path = _gov().record_continuation(records, plan)
    assert _gov().read_continuation(records, "w1") == plan and path.stat().st_size == size


def test_an_oversize_record_is_refused_without_being_read(tmp_path, monkeypatch):
    _, records = _roots(tmp_path)
    monkeypatch.setattr(_gov(), "MAX_CONTINUATION_BYTES", 10)
    (records / "continuation-w1.json").write_bytes(b"x" * 11)

    def read(path):
        raise AssertionError("an oversize record was read before it was refused")

    monkeypatch.setattr(_gov().durable_io, "read_single_link", read)
    with pytest.raises(_gov().ContinuationError):
        _gov().read_continuation(records, "w1")


@pytest.mark.parametrize("field,value,ok", [
    ("program_epoch", 2**40, True), ("program_epoch", 2**40 + 1, False),
    ("contract_id", "c" * 128, True), ("contract_id", "c" * 129, False),
    ("continued", ["n" * 128], True), ("continued", ["n" * 129], False),
])
def test_the_bounds_of_a_record_are_exact(field, value, ok):
    record = _canon({**_valid(), field: value})
    if ok:
        assert _gov().parse_continuation(record)
    else:
        with pytest.raises(_gov().ContinuationError):
            _gov().parse_continuation(record)


def test_a_wave_of_nodes_the_record_cannot_name_is_refused_not_cut(tmp_path):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    state = _state(_node("P", BLOCKED, run_id="rp"), _node("a b"), _node("I1"))
    with pytest.raises(_gov().ContinuationError):
        _gov().continue_wave(state, _contract(), wave_id="w1", runs_dir=runs,
                             depends_on=depends_on, record_dir=records)
    assert list(records.iterdir()) == []


def test_the_record_is_deterministic(tmp_path):
    first, _ = _wave(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    second, _ = _wave(other)
    assert first.to_bytes() == second.to_bytes()


# --- F: the reader is exact; arguments are refused with the module's own error ----------------
# (repair slice P3-28f: every shape below passed, or raised another exception, on 55f0db80)

def _with(base, path, value):
    """`base()` with the value at `path` replaced; a path element is a key or an index."""
    obj = json.loads(json.dumps(base()))
    cursor = obj
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return obj


_WRONG = [("null", None), ("false", False), ("true", True), ("zero", 0), ("one", 1),
          ("a list", []), ("a dict", {}), ("a non-empty list", ["x"]), ("a float", 1.5)]
_SHAPES = []
for _label, _value in _WRONG:
    _SHAPES += [
        pytest.param(_with(_valid, ["mode"], _value), id=f"mode {_label}"),
        pytest.param(_with(_valid, ["outcome"], _value), id=f"outcome {_label}"),
        pytest.param(_with(_valid, ["decisions", 0, "handling"], _value),
                     id=f"decision handling {_label}"),
        pytest.param(_with(_valid, ["decisions", 0, "node_id"], _value),
                     id=f"decision node {_label}"),
        pytest.param(_with(_valid, ["withheld", 0, "reason"], _value),
                     id=f"withheld reason {_label}"),
        pytest.param(_with(_valid, ["withheld", 0, "node_id"], _value),
                     id=f"withheld node {_label}"),
        pytest.param(_with(_hold, ["reason"], _value), id=f"HOLD reason {_label}"),
        pytest.param(_with(_hold, ["causes", 0], _value), id=f"cause {_label}"),
        pytest.param(_with(_valid, ["continued", 0], _value), id=f"continued {_label}"),
        pytest.param(_with(_valid, ["wave_id"], _value), id=f"wave id {_label}"),
        pytest.param(_with(_valid, ["contract_id"], _value), id=f"contract id {_label}"),
        pytest.param(_with(_valid, ["contract_digest"], _value), id=f"digest {_label}"),
        pytest.param(_with(_valid, ["schema"], _value), id=f"schema {_label}"),
    ]
_SHAPES += [
    # the two the reviewer found: falsy values in place of an empty string
    pytest.param(_with(_valid, ["reason"], None), id="CONTINUE reason null"),
    pytest.param(_with(_valid, ["reason"], False), id="CONTINUE reason false"),
    pytest.param(_with(_valid, ["reason"], 0), id="CONTINUE reason zero"),
    pytest.param(_with(_valid, ["reason"], []), id="CONTINUE reason empty list"),
    pytest.param(_with(_valid, ["reason"], {}), id="CONTINUE reason empty dict"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], None), id="blocked_by null"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], False), id="blocked_by false"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], 0), id="blocked_by zero"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], []), id="blocked_by empty list"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], {}), id="blocked_by empty dict"),
    pytest.param(_with(_hold, ["withheld", 0, "blocked_by"], None),
                 id="HOLD row blocked_by null"),
    pytest.param(_with(_hold, ["withheld", 0, "blocked_by"], False),
                 id="HOLD row blocked_by false"),
    # a blocker outside the node-id grammar on a SEMANTIC_DEPENDENCY row, and a blocker on a
    # row that is not one
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], "a b"), id="blocker with a space"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], "b" * 129),
                 id="blocker of 129 characters"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], "../x"), id="blocker with a slash"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], "\u00fc"), id="blocker non-ASCII"),
    pytest.param(_with(_valid, ["withheld", 0, "blocked_by"], ".P"), id="blocker with a dot first"),
    pytest.param(_with(_hold, ["withheld", 0, "blocked_by"], "P"),
                 id="a blocker on a wave-level row"),
    # numbers
    pytest.param(_with(_valid, ["program_epoch"], 0.0), id="epoch 0.0"),
    pytest.param(_with(_valid, ["program_epoch"], 1.0), id="epoch 1.0"),
    pytest.param(_with(_valid, ["program_epoch"], "0"), id="epoch a string"),
    pytest.param(_with(_valid, ["program_epoch"], None), id="epoch null"),
    pytest.param(_with(_valid, ["program_epoch"], False), id="epoch false"),
    # a node continued in a CONTINUE and withheld for a wave-level reason (M-3)
    pytest.param(_with(_valid, ["withheld", 0], {"node_id": "W", "reason": "PROGRAMME_STOPPED",
                                                  "blocked_by": ""}),
                 id="CONTINUE with a wave-level withheld reason"),
    pytest.param(_with(_valid, ["withheld", 0], {"node_id": "W",
                                                  "reason": "ROUTING_LOG_UNREADABLE",
                                                  "blocked_by": ""}),
                 id="CONTINUE with an unreadable-log withheld reason"),
    pytest.param(_with(_valid, ["withheld", 0], {"node_id": "W", "reason": "CONTRACT_INCOHERENT",
                                                  "blocked_by": ""}),
                 id="CONTINUE with an incoherent withheld reason"),
]


@pytest.mark.parametrize("record", _SHAPES)
def test_a_record_whose_values_are_not_exactly_the_writers_is_refused_with_its_own_error(
        record):
    """Type-exact and value-exact: a value of another type, a falsy stand-in for "", an
    unhashable value (`TypeError` before), is `ContinuationError` -- never accepted and
    never another exception. The bytes are the writer's serialisation of those values, so
    it is the value checks that refuse, not the byte comparison behind them."""
    with pytest.raises(_gov().ContinuationError):
        _gov().parse_continuation(_canon(record))


def _spellings():
    valid = _valid()
    text = _canon(valid).decode()
    return [
        pytest.param(json.dumps(valid, sort_keys=True, indent=2).encode() + b"\n",
                     id="another indent"),
        pytest.param(json.dumps(valid, sort_keys=True, separators=(",", ":")).encode()
                     + b"\n", id="compact separators"),
        pytest.param(json.dumps(valid, sort_keys=False, indent=1).encode() + b"\n",
                     id="keys not sorted"),
        pytest.param(text.rstrip("\n").encode(), id="no final newline"),
        pytest.param((text + "\n").encode(), id="two final newlines"),
        pytest.param(text.replace('"w1"', '"\\u0077\\u0031"').encode(),
                     id="an escaped spelling of the wave id"),
        pytest.param(text.replace('"program_epoch": 0', '"program_epoch": -0').encode(),
                     id="epoch -0"),
        pytest.param(text.replace('"program_epoch": 0', '"program_epoch": 0e0').encode(),
                     id="epoch 0e0"),
        pytest.param(text.replace("\n ", "\r\n ").encode(), id="CRLF line ends"),
        pytest.param(b" " + text.encode(), id="a leading space"),
        pytest.param(text.encode().replace(b'"I1"', b'"I1" '), id="a space before a comma"),
    ]


@pytest.mark.parametrize("payload", _spellings())
def test_a_record_that_is_valid_but_not_the_bytes_the_writer_writes_is_refused(payload):
    parsed = json.loads(payload.decode())
    assert parsed == _valid() or parsed["program_epoch"] == 0, "the control: the same values"
    with pytest.raises(_gov().ContinuationError):
        _gov().parse_continuation(payload)


def test_the_writers_own_bytes_are_read_back_to_the_same_bytes():
    g = _gov()
    shapes = [json.loads(_canon(_valid())), json.loads(_canon(_hold()))]
    shapes.append({**_hold(), "reason": "ROUTING_LOG_UNREADABLE",
                   "withheld": [{"node_id": "I1", "reason": "ROUTING_LOG_UNREADABLE",
                                 "blocked_by": ""}]})
    shapes.append({**_hold(), "reason": "CONTRACT_INCOHERENT", "causes": [],
                   "withheld": [{"node_id": "I1", "reason": "CONTRACT_INCOHERENT",
                                 "blocked_by": ""}]})
    for obj in shapes:
        data = _canon(obj)
        assert g.parse_continuation(data).to_bytes() == data


def test_a_record_file_that_is_not_the_writers_bytes_is_refused_by_the_reader(tmp_path):
    g = _gov()
    _, records = _roots(tmp_path)
    (records / "continuation-w1.json").write_bytes(
        json.dumps(_valid(), sort_keys=True, indent=2).encode() + b"\n")
    with pytest.raises(g.ContinuationError):
        g.read_continuation(records, "w1")
    (records / "continuation-w2.json").write_bytes(
        _canon({**_valid(), "wave_id": "w2", "mode": ["UNATTENDED_LONG_HORIZON"]}))
    with pytest.raises(g.ContinuationError):
        g.read_continuation(records, "w2")


class _S(str):
    pass


class _I(int):
    pass


class _T(tuple):
    pass


def _record(**kw):
    g = _gov()
    base = {"wave_id": "w1", "contract_id": "un", "contract_digest": "0123456789abcdef",
            "mode": "UNATTENDED_LONG_HORIZON", "program_epoch": 0, "outcome": "CONTINUE",
            "continued": ("I1",), "withheld": (g.Withheld("W", "SEMANTIC_DEPENDENCY", "P"),),
            "decisions": (g.StoppedDecision("P", "PARK_BRANCH"),)}
    return g.WaveContinuation(**{**base, **kw})


def test_the_record_type_is_exact_a_subclass_of_a_builtin_is_not_the_builtin():
    g = _gov()
    assert _record().to_bytes()                         # the control: the same values, exact
    for kw in ({"wave_id": _S("w1")}, {"contract_id": _S("un")}, {"mode": _S("UNATTENDED_LONG_HORIZON")},
               {"contract_digest": _S("0123456789abcdef")}, {"program_epoch": _I(0)},
               {"outcome": _S("CONTINUE")}, {"continued": _T(("I1",))},
               {"continued": (_S("I1"),)}, {"causes": _T(())},
               {"withheld": (g.Withheld(_S("W"), "SEMANTIC_DEPENDENCY", "P"),)},
               {"withheld": (g.Withheld("W", _S("SEMANTIC_DEPENDENCY"), "P"),)},
               {"withheld": (g.Withheld("W", "SEMANTIC_DEPENDENCY", _S("P")),)},
               {"decisions": (g.StoppedDecision("P", _S("PARK_BRANCH")),)},
               {"decisions": (g.StoppedDecision(_S("P"), "PARK_BRANCH"),)}):
        with pytest.raises(g.ContinuationError):
            _record(**kw)
    class _Row(g.Withheld):
        pass

    class _Decision(g.StoppedDecision):
        pass

    with pytest.raises(g.ContinuationError):
        _record(withheld=(_Row("W", "SEMANTIC_DEPENDENCY", "P"),))
    with pytest.raises(g.ContinuationError):
        _record(decisions=(_Decision("P", "PARK_BRANCH"),))
    with pytest.raises(g.ContinuationError):
        _record(withheld=("W",))
    with pytest.raises(g.ContinuationError):
        _record(decisions=(("P", "PARK_BRANCH"),))


@pytest.mark.parametrize("value", [[], {}, None, 1, ["UNATTENDED_LONG_HORIZON"]])
def test_an_unhashable_or_foreign_value_in_a_constructed_record_is_the_modules_error(value):
    g = _gov()
    for kw in ({"mode": value}, {"outcome": value}, {"reason": value}, {"wave_id": value},
               {"decisions": (g.StoppedDecision("P", value),)},
               {"withheld": (g.Withheld("W", value, "P"),)},
               {"withheld": (g.Withheld("W", "SEMANTIC_DEPENDENCY", value),)}):
        with pytest.raises(g.ContinuationError):
            _record(**kw)


# -- arguments (M-1) ---------------------------------------------------------------------------

_BAD_DIRS = [pytest.param(None, id="None"), pytest.param(7, id="an int"),
             pytest.param(b"records", id="bytes"), pytest.param("a\x00b", id="a NUL"),
             pytest.param(["records"], id="a list"), pytest.param("", id="empty")]


@pytest.mark.parametrize("bad", _BAD_DIRS)
def test_a_directory_argument_that_is_not_a_path_is_refused_with_the_modules_error(
        tmp_path, bad):
    g = _gov()
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    with pytest.raises(g.ContinuationError):
        g.read_continuation(bad, "w1")
    plan = _plan(_graph(), _contract(), runs)
    with pytest.raises(g.ContinuationError):
        g.record_continuation(bad, plan)
    with pytest.raises(g.ContinuationError):
        g.plan_continuation(_graph(), _contract(), wave_id="w1", runs_dir=bad,
                            depends_on=depends_on)
    with pytest.raises(g.ContinuationError):
        g.continue_wave(_graph(), _contract(), wave_id="w1", runs_dir=bad,
                        depends_on=depends_on, record_dir=records)
    with pytest.raises(g.ContinuationError):
        g.continue_wave(_graph(), _contract(), wave_id="w1", runs_dir=runs,
                        depends_on=depends_on, record_dir=bad)
    assert list(records.iterdir()) == []


@pytest.mark.parametrize("wave_id", [_S("w1"), b"w1", ("w1",)])
def test_a_wave_id_that_is_not_exactly_a_str_is_refused(tmp_path, wave_id):
    runs, records = _roots(tmp_path)
    with pytest.raises(_gov().ContinuationError):
        _gov().read_continuation(records, wave_id)
    with pytest.raises(_gov().ContinuationError):
        _plan(_graph(), _contract(), runs, wave_id=wave_id)


# -- causes with more than one node (M-2) ----------------------------------------------------------

def test_every_node_that_stopped_the_programme_is_a_cause_in_graph_order(tmp_path):
    runs, _ = _roots(tmp_path)
    contract = _stopping("STOP_PROGRAM")
    for run_id in ("rp1", "rp2", "rp3"):
        _stop(runs, run_id, contract)
    _stop(runs, "rok", _contract())                    # a park: not a cause
    state = _state(_node("S2", BLOCKED, run_id="rp2"), _node("Q", BLOCKED, run_id="rok"),
                   _node("S1", BLOCKED, run_id="rp1"), _node("S3", RUNNING, run_id="rp3"),
                   _node("I1"))
    got = _plan(state, contract, runs)
    assert got.reason == "PROGRAMME_STOPPED"
    assert got.causes == ("S2", "S1", "S3")
    assert [w.node_id for w in got.withheld] == ["I1"]


def test_every_node_whose_log_could_not_be_read_is_a_cause_in_graph_order(tmp_path):
    runs, _ = _roots(tmp_path)
    for run_id in ("ru1", "ru2", "ru3"):
        _log(runs, run_id).write_bytes(b"{torn\n")
    _stop(runs, "rok")
    state = _state(_node("U2", BLOCKED, run_id="ru2"), _node("Q", BLOCKED, run_id="rok"),
                   _node("U1", BLOCKED, run_id="ru1"), _node("U3", RUNNING, run_id="ru3"),
                   _node("I1"))
    got = _plan(state, _contract(), runs)
    assert got.reason == "ROUTING_LOG_UNREADABLE"
    assert got.causes == ("U2", "U1", "U3")
    recorded = _gov().parse_continuation(got.to_bytes())
    assert recorded.causes == got.causes


# -- N-1 / N-2 of the narrow check: huge integers in messages, descriptors, a subclass wave id ----

_HUGE = 10 ** 5000          # `str()` of it raises ValueError: a message must not be where it escapes


def _huge_calls(runs, records):
    g = _gov()
    other = _contract().model_copy(update={"program_epoch": 0})
    return [
        pytest.param(lambda: g.plan_continuation(
            _graph(), _contract().model_copy(update={"program_epoch": _HUGE}), wave_id="w1",
            runs_dir=runs, depends_on=depends_on), id="a contract epoch of 5001 digits"),
        pytest.param(lambda: _record(program_epoch=_HUGE), id="a record epoch of 5001 digits"),
        pytest.param(lambda: _record(contract_id=_HUGE), id="a contract id that is a huge int"),
        pytest.param(lambda: _record(reason=_HUGE), id="a reason that is a huge int"),
        pytest.param(lambda: _record(outcome="HOLD", reason=_HUGE), id="a HOLD reason huge int"),
        pytest.param(lambda: g.read_continuation(records, _HUGE), id="a wave id that is a huge int"),
        pytest.param(lambda: g.plan_continuation(
            _graph(), other, wave_id=_HUGE, runs_dir=runs, depends_on=depends_on),
            id="plan: a wave id that is a huge int"),
        pytest.param(lambda: g.plan_continuation(
            _graph(), other, wave_id="w1", runs_dir=_HUGE, depends_on=depends_on),
            id="a runs directory that is a huge int"),
        pytest.param(lambda: g.read_continuation(_HUGE, "w1"),
                     id="a record directory that is a huge int"),
        pytest.param(lambda: g.parse_continuation(_canon(_valid()).replace(
            b'"program_epoch": 0', b'"program_epoch": 1' + b"0" * 5000)),
            id="a record whose epoch has 5001 digits"),
    ]


def test_a_huge_integer_is_refused_with_the_modules_error_and_never_escapes_a_message(tmp_path):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    for case in _huge_calls(runs, records):
        with pytest.raises(_gov().ContinuationError):
            case.values[0]()


def test_an_open_descriptor_is_not_a_directory_argument(tmp_path):
    """`os.stat(3)` is the status of descriptor 3: an open directory would have been taken
    for `runs_dir` / `record_dir`."""
    g = _gov()
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")
    fd = os.open(runs, os.O_RDONLY)
    try:
        with pytest.raises(g.ContinuationError):
            g.plan_continuation(_graph(), _contract(), wave_id="w1", runs_dir=fd,
                                depends_on=depends_on)
        with pytest.raises(g.ContinuationError):
            g.read_continuation(fd, "w1")
        with pytest.raises(g.ContinuationError):
            g.record_continuation(fd, _plan(_graph(), _contract(), runs))
    finally:
        os.close(fd)
    assert list(records.iterdir()) == []


def test_a_wave_id_subclass_is_refused_before_any_log_is_read(tmp_path, monkeypatch):
    runs, records = _roots(tmp_path)
    _stop(runs, "rp")

    def read(*args, **kwargs):
        raise AssertionError("a routing log was read for a wave id that is not exactly a str")

    monkeypatch.setattr(_gov(), "read_branch_stops", read)
    with pytest.raises(_gov().ContinuationError):
        _plan(_graph(), _contract(), runs, wave_id=_S("w1"))
    assert list(records.iterdir()) == []
