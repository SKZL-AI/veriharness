"""`hoh project continue` (V3.3 P3-32, slice 1): policy-backed continuation.

What a wave of a project may continue under its autonomy contract, within the contract's horizon
and budget, written down once. It consumes the routing records of `hoh project route-decisions`
(P3-31) as a *way out* for the decision they name -- never as an approval -- and holds the wave, with
the reason, when the contract is not pinned by the project's runs, a limit is reached, a
non-terminal node has no way out, or the wave itself must hold (P3-28's `plan_continuation`).

Every case goes through the product's own commands (`cli.main`): the routing records are made by
`route-decisions`, the run pins by `governance.bind`, the parked branches by `Governor.decide`, the
governed projects by `MasterAuthority`. Nothing starts a process; a FIFO is guarded so that a hang
is a failure. The clock is `governance.utcnow`, fixed by the tests that need it.

Groups: C = what continues, H = what holds, A = authority and the record, F = refusals,
P = the production path and the module's arguments.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from hoh import cli
from hoh.autonomy import (
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    NeedsYou,
    OnLimit,
    ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.epoch import EpochStore
from hoh.governance import Governor, bind
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.succession import MasterAuthority

READY, BLOCKED, RUNNING = Lifecycle.READY, Lifecycle.BLOCKED, Lifecycle.RUNNING
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL
NOW = "2026-10-14T12:00:00Z"
CREATED = "2026-10-14T11:50:00Z"                      # ten minutes (600 s) before NOW


def _gov():
    import hoh.governance as module
    return module


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(_gov(), "utcnow", lambda: NOW)


class Hang(BaseException):
    pass


class Guard:
    def __init__(self, seconds: int = 10):
        self.seconds = seconds

    def __enter__(self):
        def alarm(signum, frame):
            raise Hang(f"did not return within {self.seconds} s: it blocked")

        self.old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)
        return False


# --- builders -----------------------------------------------------------------------------

_ESCALATION = {"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
               "release": Escalation.PARK_BRANCH_AND_CONTINUE,
               "scope": Escalation.PARK_BRANCH_AND_CONTINUE}


def _contract(external=Escalation.SYSTEM_TWO, *, needs_you=NeedsYou.DIGEST_ONLY,
              max_dispatches=None, max_wallclock=None, on_limit=OnLimit.PARK,
              max_concurrency=4, contract_id="c-pol", mode=AutonomyMode.PLAN_AUTONOMOUS,
              **kw) -> AutonomyContract:
    """PLAN_AUTONOMOUS, EXTERNAL allowed and routed `external`, with a horizon."""
    return AutonomyContract(
        contract_id=contract_id, mode=mode, needs_you=needs_you,
        release_policy=ReleasePolicy.AUTO_PREPARE_ONLY,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={**_ESCALATION, "EXTERNAL": external},
        horizon=Horizon(max_wallclock_seconds=max_wallclock, max_concurrency=max_concurrency,
                        on_limit=on_limit),
        budgets=Budgets(max_dispatches=max_dispatches), **kw)


def _unattended(external=Escalation.SYSTEM_TWO, **kw) -> AutonomyContract:
    return AutonomyContract(
        contract_id="c-un", mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.OFF,
        scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600),
        release_policy=ReleasePolicy.NEVER_AUTO_PUBLISH,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={**_ESCALATION, "EXTERNAL": external}, **kw)


def _file(tmp_path: Path, contract: AutonomyContract, name="contract.json") -> Path:
    path = tmp_path / name
    path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    return path


def _node(node_id, lifecycle=READY, deps=(), action=INTERNAL, **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps),
                    action_class=action, **kw)


def _push(node_id="push", note="push the tag v1 to origin", **kw) -> TaskNode:
    return _node(node_id, action=EXTERNAL, note=note, **kw)


def _project(root: Path, *nodes, project_id="p", created_at=CREATED) -> ProjectStore:
    store = ProjectStore(root, project_id)
    store.create(ProjectState(project_id=project_id, repo_path=str(root), nodes=list(nodes),
                              created_at=created_at))
    return store


def _pin(root: Path, run_id: str, contract: AutonomyContract) -> Path:
    """The run's own pin, through the product's writer."""
    run = root / run_id
    run.mkdir(exist_ok=True)
    bind(run, contract=contract)
    return run


def _spend(root: Path, run_id: str, dispatches) -> Path:
    path = root / run_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"usage": {"dispatches": dispatches}}))
    return path


def _cli(capsys, root: Path, command: str, project: str, *argv: str):
    try:
        with Guard():
            code = cli.main(["--root", str(root), "project", command, project, *argv])
    except SystemExit as exc:
        code = exc.code
    got = capsys.readouterr()
    return code, (json.loads(got.out) if got.out.strip() else None), got.err


def _continue(capsys, root: Path, contract_path: Path, *argv: str, wave="w1", project="p"):
    return _cli(capsys, root, "continue", project, "--autonomy", str(contract_path),
                "--wave", wave, *argv)


def _route(capsys, root: Path, contract_path: Path, project="p"):
    return _cli(capsys, root, "route-decisions", project, "--autonomy", str(contract_path))


def _record(store: ProjectStore, wave="w1") -> dict:
    return json.loads((store.dir / "policy-continuations" / f"{wave}.json").read_text())


def _records(store: ProjectStore) -> list[Path]:
    d = store.dir / "policy-continuations"
    return sorted(d.iterdir()) if d.is_dir() else []


def _tree(root: Path) -> dict:
    import hashlib
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            digest = None
            if stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, digest)
    return seen


def _ready_project(tmp_path, capsys, *, contract=None, nodes=None, pin=True, route=True,
                   created_at=CREATED, **kw):
    """A project with two independent internal nodes and a routed EXTERNAL decision, its run
    pinned under the contract. Returns (store, contract, contract file)."""
    contract = contract or _contract(**kw)
    path = _file(tmp_path, contract)
    nodes = nodes if nodes is not None else [
        _node("i1", run_id="r-i"), _node("i2"), _push()]
    store = _project(tmp_path, *nodes, created_at=created_at)
    if pin:
        _pin(tmp_path, "r-i", contract)
    if route:
        code, _doc, err = _route(capsys, tmp_path, path)
        assert code == 0, err
    return store, contract, path


# --- C: what continues ---------------------------------------------------------------------

def test_the_independent_work_continues_beside_a_decision_handed_to_a_resolver(tmp_path, capsys):
    store, contract, path = _ready_project(tmp_path, capsys)
    before_state = store.read_state().model_dump_json()
    code, doc, err = _continue(capsys, tmp_path, path)
    assert (code, err) == (0, "")
    assert (doc["outcome"], doc["reason"], doc["continued"]) == ("CONTINUE", "", ["i1", "i2"])
    assert doc["contract"] == {"id": "c-pol", "digest": contract.digest(),
                               "mode": "PLAN_AUTONOMOUS", "program_epoch": 0}
    assert doc["ways_out"] == {"i1": "next_action", "i2": "next_action",
                               "push": "machine_decision_path"}
    assert doc["approves"] is False and doc["stuck"] == []
    assert [r["node"] for r in doc["routings"]] == ["push"]
    assert "push" not in doc["continued"], "a decision handed to a resolver is not work to run"
    assert store.read_state().node("push").lifecycle is READY
    assert store.read_state().model_dump_json() == before_state
    record = _record(store)
    assert record == {k: v for k, v in doc.items() if k != "dry_run"}
    (path_,) = _records(store)
    assert stat.S_IMODE(os.stat(path_).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path_.parent).st_mode) == 0o700
    assert path_.read_bytes().isascii()


def test_the_record_binds_the_state_it_was_made_against(tmp_path, capsys):
    store, _, path = _ready_project(tmp_path, capsys)
    state = store.read_state()
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["state"] == {"write_seq": state.write_seq, "master_epoch": state.master_epoch,
                            "program_generation": state.program_generation,
                            "authority_epoch": 0}
    assert doc["at"] == NOW and doc["project_id"] == "p" and doc["wave_id"] == "w1"


def test_the_concurrency_of_the_horizon_caps_the_wave(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, max_concurrency=1)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["continued"] == ["i1"]
    assert doc["withheld"] == [{"node": "i2", "reason": "CONCURRENCY_CAP", "blocked_by": ""}]
    assert doc["limits"]["concurrency"] == {"max": 1, "running": 0}


def test_a_node_the_launchers_rule_cannot_run_beside_the_waiting_branch_is_withheld(
        tmp_path, capsys):
    nodes = [_node("i1", run_id="r-i", writes=["a"]), _node("i2", semantic_reads=["a"]),
             _push(writes=["a"])]
    _store, _, path = _ready_project(tmp_path, capsys, nodes=nodes)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["continued"] == []
    assert {w["node"]: w["blocked_by"] for w in doc["withheld"]} == {"i1": "push", "i2": "push"}


def test_a_wave_id_is_used_once_and_the_first_record_stays(tmp_path, capsys):
    store, _, path = _ready_project(tmp_path, capsys)
    assert _continue(capsys, tmp_path, path)[0] == 0
    first = _records(store)[0].read_bytes()
    code, doc, err = _continue(capsys, tmp_path, path)
    assert code == 1 and doc is None and err.startswith("not recorded: ")
    assert _records(store)[0].read_bytes() == first
    assert _continue(capsys, tmp_path, path, wave="w2")[0] == 0
    assert [p.name for p in _records(store)] == ["w1.json", "w2.json"]


def test_a_dry_run_decides_and_reports_and_writes_nothing(tmp_path, capsys):
    store, _, path = _ready_project(tmp_path, capsys)
    before = _tree(tmp_path)
    code, doc, _ = _continue(capsys, tmp_path, path, "--dry-run")
    assert code == 0 and doc["dry_run"] is True and doc["outcome"] == "CONTINUE"
    assert _tree(tmp_path) == before
    assert _records(store) == []


def test_a_decision_a_contract_parked_is_a_parked_branch_reason_and_the_rest_continues(
        tmp_path, capsys):
    contract = _unattended()
    _project(tmp_path, _node("a", BLOCKED, run_id="r-a"), _node("i1"),
                     _node("b", deps=["a"]))
    run = _pin(tmp_path, "r-a", contract)
    Governor(contract=bind(run).contract, run_dir=run).decide(
        decision="a question", kind="decision", delegable=False)
    code, doc, err = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert (code, err) == (0, "")
    assert doc["ways_out"]["a"] == "parked_branch_reason"
    assert doc["continued"] == ["i1"]


# --- H: what holds -------------------------------------------------------------------------

def _holds(doc, reason):
    assert doc["outcome"] == "HOLD" and doc["reason"] == reason, doc
    assert doc["continued"] == []


def test_a_contract_that_no_run_is_pinned_to_holds_the_wave(tmp_path, capsys):
    store, _, path = _ready_project(tmp_path, capsys, pin=False)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_NOT_PINNED")
    assert _record(store)["reason"] == "CONTRACT_NOT_PINNED", "a hold is recorded too"


def test_a_contract_that_differs_from_the_runs_pin_holds_the_wave(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, pin=False)
    _pin(tmp_path, "r-i", _contract(contract_id="another"))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_PIN_MISMATCH")


def test_one_run_pinned_to_another_contract_is_enough_to_hold(tmp_path, capsys):
    nodes = [_node("i1", run_id="r-i"), _node("i2", run_id="r-j"), _push()]
    _store, contract, path = _ready_project(tmp_path, capsys, nodes=nodes, pin=False)
    _pin(tmp_path, "r-i", contract)
    _pin(tmp_path, "r-j", _contract(contract_id="another"))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_PIN_MISMATCH")


@pytest.mark.parametrize("damage", ["junk", "a link", "witness removed"])
def test_a_pin_that_cannot_be_trusted_holds_the_wave(tmp_path, capsys, damage):
    _store, _, path = _ready_project(tmp_path, capsys)
    pinned = tmp_path / "r-i" / "autonomy" / "contract.json"
    if damage == "junk":
        pinned.write_text("{not a contract")
    elif damage == "a link":
        other = tmp_path / "elsewhere.json"
        other.write_text(pinned.read_text())
        staged = tmp_path / "staged-link"
        staged.symlink_to(other)
        os.replace(staged, pinned)                       # the pin's name now is a link
    else:
        (tmp_path / "r-i" / "approval" / "autonomy.json").write_text("{torn")
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_PIN_MISMATCH")


def test_a_run_that_is_not_a_run_id_holds_the_wave(tmp_path, capsys):
    _store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("bad", run_id=".."), _push()])
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_PIN_MISMATCH")


@pytest.mark.parametrize("on_limit", list(OnLimit))
def test_a_spent_dispatch_budget_is_met_by_the_policy_and_continues_nothing(
        tmp_path, capsys, on_limit):
    _store, _, path = _ready_project(tmp_path, capsys, max_dispatches=5, on_limit=on_limit)
    _spend(tmp_path, "r-i", 3)
    _spend(tmp_path, "r-j", 2)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")
    assert doc["limit_policy"] == on_limit.value
    assert doc["limits"]["dispatches"] == {"max": 5, "spent": 5}


def test_the_dispatch_budget_is_exact(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, max_dispatches=5)
    _spend(tmp_path, "r-i", 4)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["limits"]["dispatches"] == {"max": 5, "spent": 4}
    _spend(tmp_path, "r-i", 5)
    code, doc, _ = _continue(capsys, tmp_path, path, wave="w2")
    assert code == 1 and doc["reason"] == "LIMIT_REACHED"


@pytest.mark.parametrize("how", ["junk", "a list", "negative", "a boolean", "a FIFO"])
def test_a_spend_that_cannot_be_read_is_not_a_spend_of_zero(tmp_path, capsys, how):
    _store, _, path = _ready_project(tmp_path, capsys, max_dispatches=50)
    state = tmp_path / "r-x" / "state.json"
    state.parent.mkdir()
    if how == "junk":
        state.write_text("{nope")
    elif how == "a list":
        state.write_text("[1]")
    elif how == "negative":
        state.write_text(json.dumps({"usage": {"dispatches": -1}}))
    elif how == "a boolean":
        state.write_text(json.dumps({"usage": {"dispatches": True}}))
    else:
        os.mkfifo(state)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")


def test_no_budget_named_is_no_limit(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys)
    _spend(tmp_path, "r-i", 10_000)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["limits"]["dispatches"] == {"max": None, "spent": 10_000}


def test_the_wallclock_horizon_is_exact(tmp_path, capsys):
    """The project's record is 600 s old at NOW."""
    inside = tmp_path / "inside"
    inside.mkdir()
    _, _, path = _ready_project(inside, capsys, max_wallclock=601)
    code, doc, _ = _continue(capsys, inside, path)
    assert code == 0 and doc["limits"]["wallclock_seconds"]["max"] == 601
    assert doc["limits"]["wallclock_seconds"]["elapsed"] == 600
    at = tmp_path / "at"
    at.mkdir()
    _, _, path = _ready_project(at, capsys, max_wallclock=600)
    code, doc, _ = _continue(capsys, at, path)
    assert code == 1 and doc["reason"] == "LIMIT_REACHED"
    assert doc["limits"]["wallclock_seconds"]["max"] == 600
    assert doc["limits"]["wallclock_seconds"]["elapsed"] == 600


def test_a_horizon_that_has_passed_holds_the_wave_under_the_on_limit_policy(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, max_wallclock=3600, on_limit=OnLimit.STOP,
                                    created_at="2026-10-14T10:00:00Z")
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")
    assert doc["limit_policy"] == "STOP"
    assert doc["limits"]["wallclock_seconds"]["max"] == 3600
    assert doc["limits"]["wallclock_seconds"]["elapsed"] == 7200


def test_a_state_budget_wallclock_stands_in_when_the_horizon_names_none(tmp_path, capsys):
    _store, _, path = _ready_project(
        tmp_path, capsys, created_at="2026-10-14T10:00:00Z",
        contract=_contract().model_copy(update={"budgets": Budgets(max_wallclock_seconds=60)}))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1 and doc["reason"] == "LIMIT_REACHED"
    assert doc["limits"]["wallclock_seconds"]["max"] == 60


def test_a_decision_with_no_valid_record_is_a_node_with_no_way_out(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, route=False)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "PROGRAM_STUCK")
    assert [s["node"] for s in doc["stuck"]] == ["push"]
    assert "valid routing record" in doc["stuck"][0]["why"]
    assert "push" not in doc["ways_out"]


@pytest.mark.parametrize("how", ["note edited", "approves true", "approves a string",
                                 "a field added", "another contract's name", "deleted",
                                 "a link", "oversize"])
def test_a_routing_record_that_is_not_exactly_what_the_contract_writes_is_no_way_out(
        tmp_path, capsys, how):
    store, _, path = _ready_project(tmp_path, capsys)
    (record,) = sorted((store.dir / "decision-routings").iterdir())
    if how == "note edited":
        state = store.read_state()
        state.node("push").note = "push to another remote"
        store.write_state(state)
    elif how in ("approves true", "approves a string", "a field added"):
        body = json.loads(record.read_text())
        if how == "approves true":
            body["approves"] = True
        elif how == "approves a string":
            body["approves"] = "no"
        else:
            body["note"] = "x"
        record.write_text(json.dumps(body, sort_keys=True, indent=1) + "\n")
    elif how == "another contract's name":
        digest, _ = record.name.split("-")
        record.rename(record.with_name(f"{digest}-{'0' * 16}.json"))
    elif how == "deleted":
        record.rename(tmp_path / "moved-away")
    elif how == "a link":
        other = tmp_path / "elsewhere"
        other.write_bytes(record.read_bytes())
        staged = tmp_path / "staged-link"
        staged.symlink_to(other)
        os.replace(staged, record)                       # the record's name now is a link
    else:
        record.write_bytes(record.read_bytes() + b" " * (1 << 14))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1, how
    _holds(doc, "PROGRAM_STUCK")
    assert [s["node"] for s in doc["stuck"]] == ["push"], how


def test_a_routing_record_never_makes_the_decision_runnable(tmp_path, capsys):
    """An EXTERNAL node is not work to run even when its record is valid, and a record that says
    anything but `approves: false` is not read at all."""
    store, _, path = _ready_project(tmp_path, capsys)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert "push" not in doc["continued"] and "push" not in [w["node"] for w in doc["withheld"]]
    state = store.read_state()
    assert state.node("push").lifecycle is READY
    assert [d.id for d in state.decisions] == [], "nothing was decided or unblocked"


@pytest.mark.parametrize("make,why", [
    pytest.param(lambda: _node("h", BLOCKED, blocked_kind="human", note="legal review"),
                 "no routing, no timer", id="a person's block, no person expected"),
    pytest.param(lambda: _node("t", BLOCKED, blocked_kind="usage_limit"), "no retry time",
                 id="a timed wait with no retry time"),
    pytest.param(lambda: _node("x", BLOCKED), "no routing", id="a block with no kind and no note"),
])
def test_a_waiting_node_with_no_way_out_is_the_programme_being_stuck(tmp_path, capsys, make, why):
    contract = _unattended()
    _project(tmp_path, make(), _node("i1", run_id="r-i"))
    _pin(tmp_path, "r-i", contract)
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert code == 1
    _holds(doc, "PROGRAM_STUCK")
    assert len(doc["stuck"]) == 1 and why in doc["stuck"][0]["why"]


def _blocked_person(root, contract, name="c.json"):
    block = _node("h", BLOCKED, blocked_kind="human", note="legal review")
    _project(root, block, _node("i1", run_id="r-i"))
    _pin(root, "r-i", contract)
    return _file(root, contract, name)


def test_a_person_is_a_way_out_where_a_person_is_expected(tmp_path, capsys):
    path = _blocked_person(tmp_path, _contract(needs_you=NeedsYou.DIGEST_ONLY))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["ways_out"]["h"] == "human_gate"


def test_a_person_is_a_way_out_where_the_contract_names_a_hard_gate(tmp_path, capsys):
    path = _blocked_person(tmp_path, _unattended(hard_human_gates=("decision",)))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["ways_out"]["h"] == "human_gate"


def test_a_person_is_no_way_out_where_none_is_expected_and_no_gate_is_named(tmp_path, capsys):
    path = _blocked_person(tmp_path, _unattended())
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1 and doc["reason"] == "PROGRAM_STUCK" and doc["stuck"][0]["node"] == "h"


def test_a_timed_wait_with_a_retry_time_has_a_way_out(tmp_path, capsys):
    contract = _unattended()
    _project(tmp_path, _node("t", BLOCKED, blocked_kind="usage_limit",
                                     retry_after="2026-10-14T13:00:00Z"),
                     _node("i1", run_id="r-i"))
    _pin(tmp_path, "r-i", contract)
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert code == 0 and doc["ways_out"]["t"] == "retry_at"


def test_a_stop_of_the_programme_holds_the_wave(tmp_path, capsys):
    contract = _unattended().model_copy(update={
        "escalation": {**_ESCALATION, "EXTERNAL": Escalation.SYSTEM_TWO,
                       "decision": Escalation.STOP_PROGRAM}})
    _project(tmp_path, _node("p", RUNNING, run_id="r-p"), _node("i1"))
    run = _pin(tmp_path, "r-p", contract)
    assert Governor(contract=contract, run_dir=run).decide(
        decision="d", kind="decision", delegable=False).handling is Handling.STOP_PROGRAM
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert code == 1
    _holds(doc, "PROGRAMME_STOPPED")
    assert [(w["node"], w["reason"]) for w in doc["withheld"]] == [("i1", "PROGRAMME_STOPPED")]


def test_an_unreadable_routing_log_is_a_node_with_no_way_out(tmp_path, capsys):
    contract = _unattended()
    _project(tmp_path, _node("a", BLOCKED, run_id="r-a"), _node("i1"))
    run = _pin(tmp_path, "r-a", contract)
    log = run / "autonomy" / "routings.jsonl"
    log.parent.mkdir(exist_ok=True)
    log.write_bytes(b"{torn\n")
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert code == 1
    _holds(doc, "PROGRAM_STUCK")
    assert doc["stuck"][0]["node"] == "a" and "cannot be read" in doc["stuck"][0]["why"]


def test_the_pin_is_decided_before_the_limit(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, pin=False, route=False, max_dispatches=1)
    _spend(tmp_path, "r-i", 9)
    _, doc, _ = _continue(capsys, tmp_path, path)
    assert doc["reason"] == "CONTRACT_NOT_PINNED"


def test_the_limit_is_decided_before_the_stuck_node(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, route=False, max_dispatches=1)
    _spend(tmp_path, "r-i", 9)
    _, doc, _ = _continue(capsys, tmp_path, path)
    assert doc["reason"] == "LIMIT_REACHED"
    _spend(tmp_path, "r-i", 0)
    _, doc, _ = _continue(capsys, tmp_path, path, wave="w2")
    assert doc["reason"] == "PROGRAM_STUCK"


def test_the_stuck_node_is_decided_before_the_wave(tmp_path, capsys):
    """A stop in a log and a node with no way out at once: the stuck node is the reason."""
    contract = _unattended().model_copy(update={
        "escalation": {**_ESCALATION, "EXTERNAL": Escalation.SYSTEM_TWO,
                       "decision": Escalation.STOP_PROGRAM}})
    _project(tmp_path, _node("p", RUNNING, run_id="r-p"), _node("x", BLOCKED), _node("i1"))
    run = _pin(tmp_path, "r-p", contract)
    Governor(contract=contract, run_dir=run).decide(decision="d", kind="decision",
                                                    delegable=False)
    _, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert doc["reason"] == "PROGRAM_STUCK" and doc["stuck"][0]["node"] == "x"


# --- H2: the pin follows the launcher's run id, the limits are all limits, the ways out ---------

def test_a_node_with_no_run_id_is_judged_by_the_run_directory_of_its_id(tmp_path, capsys):
    """`node.run_id or node.id`: the launcher's rule and the log reader's. A RUNNING node
    `legacy` with no run id whose directory is pinned to another contract is a mismatch."""
    _store, _unused, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("legacy", RUNNING), _push()])
    _pin(tmp_path, "legacy", _contract(contract_id="another"))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "CONTRACT_PIN_MISMATCH")


def test_a_node_with_no_run_id_pinned_to_the_same_contract_is_no_mismatch(tmp_path, capsys):
    _store, contract, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("legacy", RUNNING), _push()])
    _pin(tmp_path, "legacy", contract)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["ways_out"]["legacy"] == "next_action"


def test_a_node_id_that_is_not_a_run_id_is_not_a_run(tmp_path, capsys):
    """`projects` and `backups` are names the runs root keeps for itself: a node called that has
    no run to be pinned, and is not a mismatch."""
    _store, _unused, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("projects"), _push()])
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0, doc


@pytest.mark.parametrize("horizon,budget", [(3600, 100), (100, 3600)],
                         ids=["the budget is stricter", "the horizon is stricter"])
def test_the_stricter_of_the_horizons_and_the_budgets_wallclock_binds(
        tmp_path, capsys, horizon, budget):
    """The project's record is 600 s old at NOW, one of the two limits is 100 s."""
    contract = _contract(max_wallclock=horizon).model_copy(
        update={"budgets": Budgets(max_wallclock_seconds=budget)})
    _store, _, path = _ready_project(tmp_path, capsys, contract=contract)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")
    assert doc["limits"]["wallclock_seconds"]["max"] == 100
    assert doc["limits"]["wallclock_seconds"]["horizon"] == horizon
    assert doc["limits"]["wallclock_seconds"]["budget"] == budget


@pytest.mark.parametrize("created", ["not a time", "2026-13-45T99:99:99Z", "", "2026-10-14"])
def test_a_creation_time_that_cannot_be_read_is_a_limit_that_cannot_be_judged(
        tmp_path, capsys, created):
    _store, _, path = _ready_project(tmp_path, capsys, max_wallclock=3600, created_at=created)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")
    assert doc["limits"]["wallclock_seconds"]["elapsed"] is None


def test_a_creation_time_in_the_future_is_not_just_created(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys, max_wallclock=3600,
                                    created_at="2026-10-14T12:00:01Z")
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "LIMIT_REACHED")
    assert doc["limits"]["wallclock_seconds"]["elapsed"] is None


def test_a_creation_time_nobody_needs_is_not_read(tmp_path, capsys):
    """No wallclock limit named: an unreadable creation time holds nothing."""
    _store, _, path = _ready_project(tmp_path, capsys, created_at="not a time")
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["limits"]["wallclock_seconds"]["max"] is None


def test_the_nodes_already_running_count_against_the_concurrency(tmp_path, capsys):
    nodes = [_node("run", RUNNING, run_id="r-i"), _node("i1"), _node("i2"), _push()]
    _store, _, path = _ready_project(tmp_path, capsys, nodes=nodes, max_concurrency=2)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["continued"] == ["i1"]
    assert doc["withheld"] == [{"node": "i2", "reason": "CONCURRENCY_CAP", "blocked_by": ""}]
    assert doc["limits"]["concurrency"] == {"max": 2, "running": 1}


def test_a_full_house_continues_nothing_and_says_so(tmp_path, capsys):
    nodes = [_node("run", RUNNING, run_id="r-i"), _node("i1"), _push()]
    _store, _, path = _ready_project(tmp_path, capsys, nodes=nodes, max_concurrency=1)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["outcome"] == "CONTINUE" and doc["continued"] == []
    assert [w["reason"] for w in doc["withheld"]] == ["CONCURRENCY_CAP"]


# -- ways out of nodes nobody routed: what the contract would do, and what is recorded ----------

def _external_only(tmp_path, capsys, contract):
    store = _project(tmp_path, _node("i1", run_id="r-i"), _push())
    _pin(tmp_path, "r-i", contract)
    return store, _file(tmp_path, contract)


@pytest.mark.parametrize("contract,way,handling,would", [
    pytest.param(_contract(Escalation.HUMAN_IF_AVAILABLE, needs_you=NeedsYou.ON),
                 "human_gate", "ASK_NOW", "CONTRACT_WOULD_ASK", id="ask, a person expected"),
    pytest.param(_contract(Escalation.HUMAN_IF_AVAILABLE, needs_you=NeedsYou.DIGEST_ONLY),
                 "human_gate", "COLLECT_FOR_DIGEST", "CONTRACT_WOULD_COLLECT",
                 id="collect, in the digest"),
    pytest.param(_unattended(Escalation.HUMAN_IF_AVAILABLE),
                 "parked_branch_reason", "PARK_BRANCH", "CONTRACT_WOULD_PARK",
                 id="park, none expected"),
    pytest.param(_contract(Escalation.PARK_BRANCH_AND_CONTINUE),
                 "parked_branch_reason", "PARK_BRANCH", "CONTRACT_WOULD_PARK", id="park"),
    pytest.param(_contract(Escalation.ABORT_BRANCH),
                 "terminal", "ABORT_BRANCH", "CONTRACT_WOULD_ABORT", id="abort"),
])
def test_an_external_decision_nobody_routed_has_the_way_out_the_contract_names(
        tmp_path, capsys, contract, way, handling, would):
    _store, path = _external_only(tmp_path, capsys, contract)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0, doc
    assert doc["ways_out"]["push"] == way
    assert doc["handlings"]["push"] == handling
    assert {"node": "push", "why": would} in doc["not_enacted"]
    assert "push" not in doc["continued"] and doc["continued"] == ["i1"]
    assert doc["routings"] == []


def test_a_contract_that_routes_an_external_decision_to_stop_stops_the_wave(tmp_path, capsys):
    _store, path = _external_only(tmp_path, capsys, _contract(Escalation.STOP_PROGRAM))
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "PROGRAMME_STOPPED")
    assert doc["ways_out"]["push"] == "terminal" and doc["handlings"]["push"] == "STOP_PROGRAM"
    assert [w["node"] for w in doc["withheld"]] == ["i1"]


def test_an_external_node_behind_unfinished_work_is_not_yet_a_decision_and_is_not_routed(
        tmp_path, capsys):
    contract = _contract(Escalation.STOP_PROGRAM)       # would stop, if it were reachable
    nodes = [_node("i1", run_id="r-i"), _push(deps=["i1"])]
    _store = _project(tmp_path, *nodes)
    _pin(tmp_path, "r-i", contract)
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, contract))
    assert code == 0, doc
    assert doc["ways_out"] == {"i1": "next_action", "push": "next_action"}
    assert doc["handlings"] == {} and doc["continued"] == ["i1"]


def test_a_routed_decision_records_the_resolver_handover_as_its_handling(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["handlings"] == {"push": "HANDED_TO_RESOLVER"} and doc["not_enacted"] == []
    assert "RESOLVED_BY_POLICY" not in json.dumps(doc)


@pytest.mark.parametrize("stop_under,gates,expect", [
    pytest.param(AutonomyMode.SUPERVISED, (), "stuck",
                 id="a logged ask where the pin allows no human gate"),
    pytest.param(AutonomyMode.SUPERVISED, ("decision",), "human_gate",
                 id="a logged ask where the pin names a hard gate"),
])
def test_a_logged_ask_is_a_way_out_only_where_the_pinned_contract_allows_a_person(
        tmp_path, capsys, stop_under, gates, expect):
    from hoh.autonomy import compile_profile

    asked = compile_profile(stop_under, contract_id="sup")
    pinned = _unattended(hard_human_gates=gates)
    _project(tmp_path, _node("a", BLOCKED, run_id="r-a"), _node("i1"))
    run = _pin(tmp_path, "r-a", pinned)
    got = Governor(contract=asked, run_dir=run).decide(decision="q", kind="decision",
                                                       delegable=False)
    assert got.handling is Handling.ASK_NOW
    code, doc, _ = _continue(capsys, tmp_path, _file(tmp_path, pinned))
    if expect == "stuck":
        assert code == 1
        _holds(doc, "PROGRAM_STUCK")
        assert doc["stuck"][0]["node"] == "a" and "no human gate" in doc["stuck"][0]["why"]
    else:
        assert code == 0 and doc["ways_out"]["a"] == "human_gate"
        assert doc["handlings"]["a"] == "ASK_NOW" and doc["not_enacted"] == []


def test_a_contingent_node_is_named_as_waiting_on_a_trigger_nobody_checked(tmp_path, capsys):
    nodes = [_node("i1", run_id="r-i"), _node("c", Lifecycle.CONTINGENT), _push()]
    _store, _, path = _ready_project(tmp_path, capsys, nodes=nodes)
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0
    assert doc["ways_out"]["c"] == "next_action"
    assert {"node": "c", "why": "NO_TRIGGER_CHECKED"} in doc["not_enacted"]


def test_a_hold_carries_what_it_was_decided_from(tmp_path, capsys):
    """A stuck wave names its ways out and the routings it did consume; a hold of the wave
    itself names what it withheld."""
    _store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("x", BLOCKED), _push()])
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 1
    _holds(doc, "PROGRAM_STUCK")
    assert doc["ways_out"]["i1"] == "next_action" and doc["ways_out"]["push"] == \
        "machine_decision_path"
    assert [r["node"] for r in doc["routings"]] == ["push"]
    assert doc["handlings"]["push"] == "HANDED_TO_RESOLVER"


# -- the options of the command ------------------------------------------------------------------

def test_a_dry_run_on_a_governed_project_takes_no_authority(tmp_path, capsys):
    _store, _, path = _governed(tmp_path, capsys, live=True)
    before = _tree(tmp_path)
    code, doc, err = _continue(capsys, tmp_path, path, "--dry-run")
    assert (code, err) == (0, "") and doc["dry_run"] is True
    assert _tree(tmp_path) == before
    code, _, err = _continue(capsys, tmp_path, path, wave="w2")
    assert code == 5 and "a live master" in err, "the same call without --dry-run needs one"


def test_a_programme_epoch_given_is_the_epoch_the_contract_is_judged_at(tmp_path, capsys):
    pinned = _contract().pin(3)
    _store = _project(tmp_path, _node("i1", run_id="r-i"), _push())
    _pin(tmp_path, "r-i", pinned)
    path = _file(tmp_path, _contract())                 # the file is at epoch 0
    code, doc, _ = _continue(capsys, tmp_path, path, wave="w0")
    assert code == 1 and doc["reason"] == "CONTRACT_PIN_MISMATCH"
    code, doc, _ = _continue(capsys, tmp_path, path, "--program-epoch", "3", wave="w3")
    assert doc["contract"]["program_epoch"] == 3 and doc["contract"]["digest"] == pinned.digest()
    assert doc["reason"] == "PROGRAM_STUCK", "pinned at epoch 3 the contract is the pin; nothing routed yet"


@pytest.mark.parametrize("epoch", ["-1", str(2 ** 40 + 1)])
def test_a_programme_epoch_out_of_range_is_refused(tmp_path, capsys, epoch):
    _ready_project(tmp_path, capsys)
    code, doc, err = _cli(capsys, tmp_path, "continue", "p", "--autonomy",
                          str(tmp_path / "contract.json"), "--wave", "w1",
                          "--program-epoch", epoch)
    assert code == 2 and doc is None and err.startswith("refused: ")


# --- A: authority and the record ----------------------------------------------------------

def _governed(tmp_path, capsys, *, live: bool, **kw):
    store, _, path = _ready_project(tmp_path, capsys, **kw)
    authority = MasterAuthority(store.dir, "sA", serialize=store.lock)
    authority.acquire()
    if not live:
        authority.release(reason="orderly halt")
    return store, authority, path


def test_a_live_master_refuses_an_anonymous_continuation_and_nothing_is_recorded(
        tmp_path, capsys):
    store, _, path = _governed(tmp_path, capsys, live=True)
    code, doc, err = _continue(capsys, tmp_path, path)
    assert code == 5 and doc is None and "a live master holds this project" in err
    assert _records(store) == []


def test_the_live_masters_credential_authorises_it(tmp_path, capsys):
    store, authority, path = _governed(tmp_path, capsys, live=True)
    code, doc, err = _continue(capsys, tmp_path, path, "--session", "sA", "--epoch", "1",
                               "--token", authority.token)
    assert (code, err) == (0, "") and doc["outcome"] == "CONTINUE"
    assert len(_records(store)) == 1


def test_a_master_that_goes_stale_under_the_lock_is_refused_and_nothing_is_recorded(
        tmp_path, capsys, monkeypatch):
    from hoh.succession import StaleMaster

    store, authority, path = _governed(tmp_path, capsys, live=True)
    asked = []

    def stale(self, *args, **kwargs):
        asked.append(1)
        raise StaleMaster("superseded while the command waited for the lock")

    monkeypatch.setattr(MasterAuthority, "check", stale)
    code, doc, err = _continue(capsys, tmp_path, path, "--session", "sA", "--epoch", "1",
                               "--token", authority.token)
    assert asked and code == 5 and doc is None and err.startswith("refused: ")
    assert _records(store) == []


def test_an_old_epoch_with_its_old_token_is_refused_after_a_takeover(tmp_path, capsys):
    store, old, path = _governed(tmp_path, capsys, live=False)
    token = old.token
    MasterAuthority(store.dir, "sB", serialize=store.lock).acquire()
    code, _, err = _continue(capsys, tmp_path, path, "--session", "sA", "--epoch", "1",
                             "--token", token)
    assert code == 5 and "stale master" in err and _records(store) == []


def test_without_a_live_master_an_operator_epoch_is_taken_and_released(tmp_path, capsys):
    store, _, path = _governed(tmp_path, capsys, live=False)
    code, doc, err = _continue(capsys, tmp_path, path)
    assert (code, err) == (0, "") and doc["outcome"] == "CONTINUE"
    assert [e.reason.value for e in EpochStore(store.dir).chain()] == ["INITIAL", "OPERATOR"]
    assert doc["state"]["master_epoch"] == 0, "the record binds the state as it was read"
    assert store.read_state().master_epoch == 0, "continuing is not a state write"


@pytest.mark.parametrize("field", ["write_seq", "program_generation", "master_epoch"])
def test_a_state_that_moved_while_the_wave_was_planned_records_nothing(
        tmp_path, capsys, monkeypatch, field):
    """One field at a time, and not by counting reads: whatever the implementation reads after the
    planner returned sees the moved field."""
    store, _, path = _ready_project(tmp_path, capsys)
    planned = []
    real_plan, real_read = _gov().plan_continuation, ProjectStore.read_state

    def plan(*args, **kwargs):
        out = real_plan(*args, **kwargs)
        planned.append(1)
        return out

    def moving(self):
        state = real_read(self)
        if planned:
            setattr(state, field, getattr(state, field) + 1)
        return state

    monkeypatch.setattr(_gov(), "plan_continuation", plan)
    monkeypatch.setattr(ProjectStore, "read_state", moving)
    code, doc, err = _continue(capsys, tmp_path, path)
    assert planned and code == 1 and doc is None and "STATE_MOVED" in err
    assert _records(store) == []


def test_an_authority_that_changed_hands_while_the_wave_was_planned_records_nothing(
        tmp_path, capsys, monkeypatch):
    """The lease's epoch is not the state's `master_epoch` (the last writer's): the record binds
    both, and a takeover between the plan and the create moves only the first. Whatever is read
    of the epoch chain after the planner returned sees the next epoch."""
    from types import SimpleNamespace

    from hoh.epoch import EpochStore

    store, authority, path = _governed(tmp_path, capsys, live=True)
    planned = []
    real_plan, real_current = _gov().plan_continuation, EpochStore.current

    def plan(*args, **kwargs):
        out = real_plan(*args, **kwargs)
        planned.append(1)
        return out

    def current(self):
        got = real_current(self)
        return SimpleNamespace(epoch=got.epoch + 1) if planned and got else got

    monkeypatch.setattr(_gov(), "plan_continuation", plan)
    monkeypatch.setattr(EpochStore, "current", current)
    code, doc, err = _continue(capsys, tmp_path, path, "--session", "sA", "--epoch", "1",
                               "--token", authority.token)
    assert planned and code == 1 and doc is None and "STATE_MOVED" in err, (code, err)
    assert _records(store) == []


def test_the_record_binds_the_authoritys_epoch_beside_the_states(tmp_path, capsys):
    _store, authority, path = _governed(tmp_path, capsys, live=True)
    code, doc, _ = _continue(capsys, tmp_path, path, "--session", "sA", "--epoch", "1",
                             "--token", authority.token)
    assert code == 0
    assert doc["state"]["authority_epoch"] == 1 and doc["state"]["master_epoch"] == 0


def test_the_record_size_limit_is_exact(tmp_path, capsys, monkeypatch):
    store, _, path = _ready_project(tmp_path, capsys)
    code, doc, _ = _continue(capsys, tmp_path, path, "--dry-run")
    size = len((json.dumps({k: v for k, v in doc.items() if k != "dry_run"},
                           sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode())
    monkeypatch.setattr(_gov(), "MAX_POLICY_RECORD_BYTES", size - 1)
    code, _, err = _continue(capsys, tmp_path, path, wave="over")
    assert code == 1 and err.startswith("not recorded: ") and _records(store) == []
    monkeypatch.setattr(_gov(), "MAX_POLICY_RECORD_BYTES", size)
    assert _continue(capsys, tmp_path, path, wave="at")[0] == 0


@pytest.mark.parametrize("how", ["a file", "a link"])
def test_a_records_directory_that_is_not_one_fails_the_command(tmp_path, capsys, how):
    store, _, path = _ready_project(tmp_path, capsys)
    target = store.dir / "policy-continuations"
    if how == "a file":
        target.write_text("x")
    else:
        away = tmp_path / "away"
        away.mkdir()
        target.symlink_to(away)
    code, _doc, err = _continue(capsys, tmp_path, path)
    assert code == 1 and err.startswith("not recorded: ")
    if how == "a link":
        assert list(away.iterdir()) == []


def test_the_command_changes_no_state_no_run_and_no_routing_record(tmp_path, capsys):
    _store, _, path = _ready_project(tmp_path, capsys)
    before = {k: v for k, v in _tree(tmp_path).items() if "policy-continuations" not in k}
    assert _continue(capsys, tmp_path, path)[0] == 0
    after = {k: v for k, v in _tree(tmp_path).items() if "policy-continuations" not in k}
    assert after == before


# --- F: refusals ---------------------------------------------------------------------------

def _refused(capsys, tmp_path, *argv, project="p"):
    code, doc, err = _cli(capsys, tmp_path, "continue", project, *argv)
    assert code == 2 and doc is None and err.startswith("refused: "), (code, err)
    return err


@pytest.mark.parametrize("wave", ["", "../x", "a/b", "x\n", " w", ".w", "ü", "w" * 65])
def test_a_wave_id_a_record_cannot_hold_is_refused_and_nothing_is_read(
        tmp_path, capsys, monkeypatch, wave):
    store, _, path = _ready_project(tmp_path, capsys)

    def read(*a, **k):
        raise AssertionError("a log was read for a wave that cannot be recorded")

    monkeypatch.setattr(_gov(), "read_branch_stops", read)
    _refused(capsys, tmp_path, "--autonomy", str(path), "--wave", wave)
    assert _records(store) == []


def test_a_wave_id_of_exactly_64_characters_is_recorded(tmp_path, capsys):
    store, _, path = _ready_project(tmp_path, capsys)
    assert _continue(capsys, tmp_path, path, wave="w" * 64)[0] == 0
    assert _records(store)[0].name == "w" * 64 + ".json"


@pytest.mark.parametrize("what", ["missing", "junk", "a FIFO", "a directory", "empty"])
def test_a_contract_file_that_is_not_a_contract_is_refused_and_nothing_is_written(
        tmp_path, capsys, what):
    _ready_project(tmp_path, capsys)
    target = tmp_path / "input"
    if what == "junk":
        target.write_bytes(b"{nope")
    elif what == "a FIFO":
        os.mkfifo(target)
    elif what == "a directory":
        target.mkdir()
    elif what == "empty":
        target.write_bytes(b"")
    before = _tree(tmp_path)
    _refused(capsys, tmp_path, "--autonomy", str(target), "--wave", "w1")
    assert _tree(tmp_path) == before


def test_an_unsettled_node_the_record_cannot_name_refuses_the_command(tmp_path, capsys):
    store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node("i1", run_id="r-i"), _node("a b"), _push()])
    err = _refused(capsys, tmp_path, "--autonomy", str(path), "--wave", "w1")
    assert "cannot be named" in err and _records(store) == []


def test_a_node_id_of_exactly_128_characters_is_recorded(tmp_path, capsys):
    name = "n" * 128
    _store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node(name, run_id="r-i"), _push()])
    code, doc, _ = _continue(capsys, tmp_path, path)
    assert code == 0 and doc["continued"] == [name]


def test_a_node_id_of_129_characters_is_refused(tmp_path, capsys):
    store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node("n" * 129, run_id="r-i"), _push()], route=False)
    err = _refused(capsys, tmp_path, "--autonomy", str(path), "--wave", "w1")
    assert "cannot be named" in err and _records(store) == []


def test_a_graph_without_a_frontier_is_refused_through_the_command(tmp_path, capsys):
    _store, _, path = _ready_project(
        tmp_path, capsys, nodes=[_node("twin", run_id="r-i"), _node("twin")], route=False)
    before = _tree(tmp_path)
    err = _refused(capsys, tmp_path, "--autonomy", str(path), "--wave", "w1")
    assert "no frontier" in err and _tree(tmp_path) == before


def test_a_project_that_is_not_there_is_refused(tmp_path, capsys):
    path = _file(tmp_path, _contract())
    code, doc, err = _continue(capsys, tmp_path, path, project="nope")
    assert code == 2 and doc is None and "no state" in err


# --- P: the production path and the module's arguments -------------------------------------

def test_route_then_continue_is_the_whole_path_through_the_product_commands(
        tmp_path, capsys, monkeypatch):
    """`hoh project continue` -> `cmd_project` -> `_project_continue` ->
    `governance.continue_by_policy`; the record it consumes is the one `route-decisions` made."""
    from hoh import governance

    _store, _, path = _ready_project(tmp_path, capsys, route=False)
    stuck = _continue(capsys, tmp_path, path, wave="before")
    assert stuck[0] == 1 and stuck[1]["reason"] == "PROGRAM_STUCK"
    assert _route(capsys, tmp_path, path)[0] == 0
    seen = []
    real = governance.continue_by_policy

    def spy(*args, **kwargs):
        seen.append((args[0].project_id, kwargs.get("wave_id"), kwargs.get("dry_run")))
        return real(*args, **kwargs)

    monkeypatch.setattr(governance, "continue_by_policy", spy)
    code, doc, _ = _continue(capsys, tmp_path, path, wave="after")
    assert code == 0 and seen == [("p", "after", False)]
    assert doc["ways_out"]["push"] == "machine_decision_path"


@pytest.mark.parametrize("contract", [None, {}, "c"], ids=["None", "a dict", "a str"])
def test_the_module_refuses_a_contract_that_is_not_one(tmp_path, contract):
    store = _project(tmp_path, _push())
    with pytest.raises(_gov().RoutingRefused):
        _gov().continue_by_policy(store, contract, tmp_path, wave_id="w")


@pytest.mark.parametrize("runs", [None, 7, ["x"], b"x", "nowhere", "a\x00b"],
                         ids=["None", "an int", "a list", "bytes", "missing", "a NUL"])
def test_the_module_refuses_a_runs_directory_that_is_not_one(tmp_path, runs):
    store = _project(tmp_path, _push())
    if runs == "nowhere":
        runs = str(tmp_path / "nowhere")
    with pytest.raises(_gov().RoutingRefused):
        _gov().continue_by_policy(store, _contract(), runs, wave_id="w")


@pytest.mark.parametrize("wave", [None, 1, "", "a b", b"w"])
def test_the_module_refuses_a_wave_id_that_is_not_one(tmp_path, wave):
    store = _project(tmp_path, _push())
    with pytest.raises(_gov().RoutingRefused):
        _gov().continue_by_policy(store, _contract(), tmp_path, wave_id=wave)


@pytest.mark.parametrize("update", [
    pytest.param({"contract_id": "\u00fc"}, id="a non-ASCII contract id"),
    pytest.param({"contract_id": "c" * 129}, id="a contract id of 129 characters"),
    pytest.param({"contract_id": ""}, id="an empty contract id"),
    pytest.param({"program_epoch": 2 ** 40 + 1}, id="an epoch above the bound"),
])
def test_the_module_refuses_a_contract_a_record_cannot_name(tmp_path, update):
    store = _project(tmp_path, _push())
    named = _contract().model_copy(update=update)
    with pytest.raises(_gov().RoutingRefused):
        _gov().continue_by_policy(store, named, tmp_path, wave_id="w")
    assert not (store.dir / "policy-continuations").exists()


@pytest.mark.parametrize("update", [{"contract_id": "c" * 128}, {"program_epoch": 2 ** 40}],
                         ids=["128 characters", "the largest epoch"])
def test_the_largest_contract_a_record_can_name_is_accepted(tmp_path, capsys, update):
    contract = _contract().model_copy(update=update)
    store, _, _path = _ready_project(tmp_path, capsys, contract=contract, route=False)
    result = _gov().continue_by_policy(store, contract, tmp_path, wave_id="w", dry_run=True)
    assert result.outcome == "HOLD"                 # not for want of a name: the pin (not run)
    assert result.contract["id"] == contract.contract_id
