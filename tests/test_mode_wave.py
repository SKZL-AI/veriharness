"""The two autonomy modes at wave level (V3.3 P3-28 PLAN_AUTONOMOUS, P3-29 UNATTENDED_LONG_HORIZON).

`hoh project continue` decides what a wave may continue beside a parked branch
(`governance.continue_by_policy`). Slice 1 of P3-28 left the mode out of that decision: it was
recorded and never read. One table (`governance.MODE_WAVE`) now says, per mode, how many nodes a
pass continues and whether a HOLD arms a REVIEW_DUE timer; a review that comes due, or timers that
cannot be read, hold a wave that would continue.

Every case that is about behaviour goes through the product's own commands (`cli.main`): the
routing records are made by `route-decisions`, the pins by `governance.bind`, the review timers
by the continue pass itself and the existing timer machinery (`timers.TimerStore`,
`sentinel.sweep`). Nothing starts a process; a FIFO is guarded so that a hang is a failure. The
clock is `governance.utcnow`, set by each test.

Groups: T = the table, C = the width, R = the review timer, F = files that cannot be read.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import types
from pathlib import Path

import pytest

from hoh import cli
from hoh.autonomy import (
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Horizon,
    NeedsYou,
    ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.governance import bind
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

READY = Lifecycle.READY
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL
CREATED = "2026-10-14T11:50:00Z"
NOW = "2026-10-14T12:00:00Z"
REVIEW_AFTER = 900                                    # the unattended contract's review interval
DUE = "2026-10-14T12:15:00Z"                          # NOW + REVIEW_AFTER
EARLY = "2026-10-14T12:14:59Z"
M = AutonomyMode


def _gov():
    import hoh.governance as module
    return module


@pytest.fixture
def clock(monkeypatch):
    """`governance.utcnow` as a settable clock; nothing may start a process."""
    now = {"at": NOW}

    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(_gov(), "utcnow", lambda: now["at"])

    def set_at(stamp: str) -> None:
        now["at"] = stamp

    return set_at


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
               "scope": Escalation.PARK_BRANCH_AND_CONTINUE, "EXTERNAL": Escalation.SYSTEM_TWO}
_NEEDS_YOU = {M.SUPERVISED: NeedsYou.ON, M.POLICY_GOVERNED: NeedsYou.ON,
              M.PLAN_AUTONOMOUS: NeedsYou.DIGEST_ONLY, M.UNATTENDED_LONG_HORIZON: NeedsYou.OFF}


def _contract(mode: AutonomyMode, *, width=3, review_after=REVIEW_AFTER, epoch=0,
              contract_id="c-mode") -> AutonomyContract:
    """`mode`, EXTERNAL allowed and handed to a resolver, a horizon of `width` nodes."""
    unattended = mode is M.UNATTENDED_LONG_HORIZON
    return AutonomyContract(
        contract_id=contract_id, mode=mode, needs_you=_NEEDS_YOU[mode], program_epoch=epoch,
        release_policy=(ReleasePolicy.NEVER_AUTO_PUBLISH if unattended
                        else ReleasePolicy.AUTO_PREPARE_ONLY),
        allowed_action_classes=(INTERNAL, EXTERNAL), escalation=dict(_ESCALATION),
        scope=("src/hoh/",) if unattended else (),
        horizon=Horizon(max_concurrency=width,
                        max_wallclock_seconds=86400 if unattended else None,
                        review_after_seconds=review_after if unattended else None),
        budgets=Budgets(max_dispatches=50 if unattended else None,
                        max_wallclock_seconds=86400 if unattended else None))


def _file(tmp_path: Path, contract: AutonomyContract) -> Path:
    path = tmp_path / "contract.json"
    path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    return path


def _node(node_id, **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=READY, dependencies=[], action_class=INTERNAL, **kw)


def _project(root: Path, project_id="p", independent=4) -> ProjectStore:
    """`independent` READY internal nodes (the first one's run is `r-i`) and one EXTERNAL
    decision: the decision is routed to a resolver and parked, the rest may continue."""
    nodes = [_node(f"i{k}", **({"run_id": "r-i"} if k == 1 else {}))
             for k in range(1, independent + 1)]
    nodes.append(TaskNode(id="push", lifecycle=READY, dependencies=[], action_class=EXTERNAL,
                          note="push the tag v1 to origin"))
    store = ProjectStore(root, project_id)
    store.create(ProjectState(project_id=project_id, repo_path=str(root), nodes=nodes,
                              created_at=CREATED))
    return store


def _pin(root: Path, contract: AutonomyContract) -> None:
    """The project's run pinned to the contract, through the product's writer."""
    run = root / "r-i"
    run.mkdir(exist_ok=True)
    bind(run, contract=contract)


def _cli(capsys, root: Path, command: str, project: str, *argv: str):
    try:
        with Guard():
            code = cli.main(["--root", str(root), "project", command, project, *argv])
    except SystemExit as exc:
        code = exc.code
    got = capsys.readouterr()
    return code, (json.loads(got.out) if got.out.strip() else None), got.err


def _setup(tmp_path, capsys, contract, *, pin=True, project="p", independent=4):
    """A project whose decision is routed; returns (store, contract file)."""
    path = _file(tmp_path, contract)
    store = _project(tmp_path, project, independent)
    if pin:
        _pin(tmp_path, contract)
    code, _doc, err = _cli(capsys, tmp_path, "route-decisions", project, "--autonomy", str(path))
    assert code == 0, err
    return store, path


def _continue(capsys, root: Path, path: Path, wave: str, *argv: str, project="p"):
    return _cli(capsys, root, "continue", project, "--autonomy", str(path), "--wave", wave, *argv)


def _timers(root: Path) -> list[dict]:
    path = root / "timers.json"
    return json.loads(path.read_text())["timers"] if path.exists() else []


def _review(doc) -> dict:
    return doc["mode_wave"]["review"]


# --- T: the table --------------------------------------------------------------------------

def test_the_table_has_a_row_for_every_mode_and_for_nothing_else():
    gov = _gov()
    assert set(gov.MODE_WAVE) == set(AutonomyMode), "a mode added later needs a decided row"
    for row in gov.MODE_WAVE.values():
        assert type(row) is gov.ModeWave
        assert row.ceiling is None or (type(row.ceiling) is int and row.ceiling >= 1)
        assert type(row.hold_review) is bool
    assert gov.MOST_CONSERVATIVE_MODE in gov.MODE_WAVE


def test_the_table_says_who_is_serial_and_who_reviews_a_hold():
    gov = _gov()
    ceilings = {m.value: r.ceiling for m, r in gov.MODE_WAVE.items()}
    assert ceilings == {"SUPERVISED": 1, "POLICY_GOVERNED": None, "PLAN_AUTONOMOUS": None,
                        "UNATTENDED_LONG_HORIZON": None}
    assert {m.value for m, r in gov.MODE_WAVE.items() if r.hold_review} == {
        "UNATTENDED_LONG_HORIZON"}
    strictest = gov.MODE_WAVE[gov.MOST_CONSERVATIVE_MODE]
    assert strictest.ceiling == 1 and strictest.hold_review is False
    assert all(strictest.ceiling <= (r.ceiling or 10**9) for r in gov.MODE_WAVE.values())


@pytest.mark.parametrize("mode,width,expected", [
    (M.SUPERVISED, 4, 1), (M.SUPERVISED, 1, 1), (M.POLICY_GOVERNED, 4, 4),
    (M.PLAN_AUTONOMOUS, 4, 4), (M.PLAN_AUTONOMOUS, 2, 2), (M.UNATTENDED_LONG_HORIZON, 7, 7),
    # a width that cannot be read is one node, whatever the mode
    (M.PLAN_AUTONOMOUS, 0, 1), (M.PLAN_AUTONOMOUS, -3, 1), (M.PLAN_AUTONOMOUS, True, 1),
    (M.UNATTENDED_LONG_HORIZON, "4", 1), (M.UNATTENDED_LONG_HORIZON, None, 1),
    (M.PLAN_AUTONOMOUS, 2.5, 1), (M.PLAN_AUTONOMOUS, float("inf"), 1)])
def test_the_width_is_the_horizon_through_the_mode_and_one_when_unreadable(mode, width, expected):
    contract = types.SimpleNamespace(mode=mode, horizon=types.SimpleNamespace(
        max_concurrency=width))
    assert _gov().mode_width(contract) == expected


@pytest.mark.parametrize("mode", ["PLAN_AUTONOMOUS", "nonsense", None, 3, ["SUPERVISED"], {}])
def test_a_mode_that_is_not_a_mode_is_held_to_the_most_conservative_row(mode):
    gov = _gov()
    contract = types.SimpleNamespace(mode=mode, horizon=types.SimpleNamespace(max_concurrency=9))
    name, row, width, fell_back = gov._profile(contract)
    assert (name, row, width, fell_back) == (gov.MOST_CONSERVATIVE_MODE.value,
                                            gov.MODE_WAVE[gov.MOST_CONSERVATIVE_MODE], 1, True)
    assert gov.mode_width(contract) == 1
    assert gov.mode_width(types.SimpleNamespace()) == 1, "no mode and no horizon at all"


def test_a_mode_the_table_lost_falls_back_and_the_record_says_so(tmp_path, capsys, clock,
                                                                  monkeypatch):
    gov = _gov()
    contract = _contract(M.PLAN_AUTONOMOUS, width=4)
    _store, path = _setup(tmp_path, capsys, contract)
    monkeypatch.setattr(gov, "MODE_WAVE", {m: r for m, r in gov.MODE_WAVE.items()
                                           if m is not M.PLAN_AUTONOMOUS})
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 0 and doc["continued"] == ["i1"], "one node: the strictest row's"
    assert doc["mode_wave"]["mode"] == "SUPERVISED" and doc["mode_wave"]["fell_back"] is True
    assert doc["contract"]["mode"] == "PLAN_AUTONOMOUS", "the contract's own word is kept"
    assert {w["reason"] for w in doc["withheld"]} == {"CONCURRENCY_CAP"}


# --- C: the width --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,width,continued", [
    (M.SUPERVISED, 3, ["i1"]), (M.POLICY_GOVERNED, 3, ["i1", "i2", "i3"]),
    (M.PLAN_AUTONOMOUS, 3, ["i1", "i2", "i3"]), (M.PLAN_AUTONOMOUS, 2, ["i1", "i2"]),
    (M.UNATTENDED_LONG_HORIZON, 3, ["i1", "i2", "i3"]),
    (M.UNATTENDED_LONG_HORIZON, 1, ["i1"])])
def test_the_mode_decides_how_many_nodes_continue_beside_the_parked_decision(
        tmp_path, capsys, clock, mode, width, continued):
    store, path = _setup(tmp_path, capsys, _contract(mode, width=width))
    code, doc, err = _continue(capsys, tmp_path, path, "w1")
    assert (code, err) == (0, "")
    assert doc["outcome"] == "CONTINUE" and doc["continued"] == continued
    assert "push" not in doc["continued"], "the routed decision is never work to run"
    capped = [w["node"] for w in doc["withheld"] if w["reason"] == "CONCURRENCY_CAP"]
    assert capped == [f"i{k}" for k in range(len(continued) + 1, 5)]
    assert doc["mode_wave"]["mode"] == mode.value and doc["mode_wave"]["fell_back"] is False
    assert doc["mode_wave"]["width"] == len(continued)
    assert doc["limits"]["concurrency"]["max"] == width, "the horizon's own number is kept"
    record = json.loads((store.dir / "policy-continuations" / "w1.json").read_text())
    assert record == {k: v for k, v in doc.items() if k != "dry_run"}


# --- R: the review timer of a hold ---------------------------------------------------------

def test_a_hold_of_the_unattended_mode_arms_one_review_timer_for_its_project(
        tmp_path, capsys, clock):
    from hoh.sentinel import sweep
    from hoh.timers import TimerStore

    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    store, path = _setup(tmp_path, capsys, contract, pin=False)
    assert _timers(tmp_path) == []
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and (doc["outcome"], doc["reason"]) == ("HOLD", "CONTRACT_NOT_PINNED")
    (timer,) = _timers(tmp_path)
    assert (timer["kind"], timer["status"], timer["due_at"]) == ("review_due", "armed", DUE)
    assert (timer["program_id"], timer["program_epoch"]) == ("p", 0)
    assert timer["run_id"] is None and timer["node_id"] is None
    assert "CONTRACT_NOT_PINNED" in timer["reason"] and "w1" in timer["reason"]
    assert _review(doc) == {"state": "ARMED", "timer": timer["timer_id"], "due_at": DUE,
                            "needs_you": False, "detail": ""}
    record = json.loads((store.dir / "policy-continuations" / "w1.json").read_text())
    assert record == {k: v for k, v in doc.items() if k != "dry_run"}
    quiet = sweep(TimerStore(tmp_path), now=EARLY, program_epoch=0)
    assert quiet.due == [] and quiet.refused == []
    loud = sweep(TimerStore(tmp_path), now=DUE, program_epoch=0)
    assert [(p.timer.timer_id, p.action) for p in loud.due] == [(timer["timer_id"],
                                                                 "request_review")]


@pytest.mark.parametrize("seconds,due", [(1, "2026-10-14T12:00:01Z"),
                                         (3600, "2026-10-14T13:00:00Z"),
                                         (7 * 86400, "2026-10-21T12:00:00Z")])
def test_the_review_is_due_after_the_interval_the_contract_names(tmp_path, capsys, clock,
                                                                  seconds, due):
    _store, path = _setup(tmp_path, capsys,
                         _contract(M.UNATTENDED_LONG_HORIZON, review_after=seconds), pin=False)
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and _review(doc)["state"] == "ARMED" and _review(doc)["due_at"] == due
    assert [t["due_at"] for t in _timers(tmp_path)] == [due]


def test_a_later_hold_keeps_the_first_timer_and_writes_nothing(tmp_path, capsys, clock):
    _store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON), pin=False)
    first = _continue(capsys, tmp_path, path, "w1")[1]
    before = (tmp_path / "timers.json").read_bytes()
    clock("2026-10-14T12:10:00Z")
    code, doc, _ = _continue(capsys, tmp_path, path, "w2")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc)["state"] == "PENDING"
    assert _review(doc)["timer"] == _review(first)["timer"]
    assert _review(doc)["due_at"] == DUE, "the clock of the review starts at the first hold"
    assert (tmp_path / "timers.json").read_bytes() == before
    assert len(_timers(tmp_path)) == 1
    code, doc, err = _continue(capsys, tmp_path, path, "w2")           # the wave id is used once
    assert code == 1 and doc is None and err.startswith("not recorded: ")
    assert (tmp_path / "timers.json").read_bytes() == before
    assert len(_timers(tmp_path)) == 1


@pytest.mark.parametrize("mode", [M.SUPERVISED, M.POLICY_GOVERNED, M.PLAN_AUTONOMOUS])
def test_the_other_modes_arm_nothing_and_read_no_timer_file(tmp_path, capsys, clock, mode):
    _store, path = _setup(tmp_path, capsys, _contract(mode), pin=False)
    (tmp_path / "timers.json").write_text("{")             # unreadable on purpose
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc) == {"state": "NONE", "timer": "", "due_at": "", "needs_you": False,
                            "detail": ""}
    assert (tmp_path / "timers.json").read_text() == "{"


def test_a_review_that_is_due_holds_a_wave_that_would_continue_and_is_fired_once(
        tmp_path, capsys, clock):
    from hoh.sentinel import sweep
    from hoh.timers import TimerStore

    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract, pin=False)
    armed = _review(_continue(capsys, tmp_path, path, "w1")[1])["timer"]
    _pin(tmp_path, contract)                               # the cause of the hold is gone
    clock(EARLY)
    code, doc, _ = _continue(capsys, tmp_path, path, "w2")
    assert code == 0 and doc["continued"] == ["i1", "i2", "i3"], "not due: it continues"
    assert _review(doc)["state"] == "PENDING"
    clock(DUE)                                             # the boundary second is due
    code, doc, _ = _continue(capsys, tmp_path, path, "w3")
    assert code == 1
    assert (doc["outcome"], doc["reason"], doc["continued"]) == ("HOLD", "REVIEW_DUE", [])
    assert {(w["node"], w["reason"]) for w in doc["withheld"]} >= {
        ("i1", "REVIEW_DUE"), ("i2", "REVIEW_DUE"), ("i3", "REVIEW_DUE")}
    assert _review(doc) == {"state": "DUE", "timer": armed, "due_at": DUE, "needs_you": True,
                            "detail": ""}
    (timer,) = _timers(tmp_path)
    assert timer["status"] == "fired" and timer["fired_at"], "fired after the record"
    assert sweep(TimerStore(tmp_path), now=DUE, program_epoch=0).due == []
    again = (tmp_path / "timers.json").read_bytes()
    clock("2026-10-14T12:20:00Z")
    code, doc, _ = _continue(capsys, tmp_path, path, "w4")
    assert code == 0 and doc["continued"] == ["i1", "i2", "i3"], "the review hold is one pass"
    assert _review(doc)["state"] == "IDLE"
    assert (tmp_path / "timers.json").read_bytes() == again, "a further pass writes no timer"
    assert len(_timers(tmp_path)) == 1


def test_a_review_whose_record_cannot_be_written_is_not_consumed(tmp_path, capsys, clock):
    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract, pin=False)
    armed = _review(_continue(capsys, tmp_path, path, "w1")[1])["timer"]
    _pin(tmp_path, contract)
    clock(DUE)
    before = (tmp_path / "timers.json").read_bytes()
    code, doc, err = _continue(capsys, tmp_path, path, "w1")             # w1 exists: no record
    assert code == 1 and doc is None and err.startswith("not recorded: ")
    assert (tmp_path / "timers.json").read_bytes() == before, "it is fired after the record only"
    code, doc, _ = _continue(capsys, tmp_path, path, "w2")
    assert doc["reason"] == "REVIEW_DUE" and _review(doc)["timer"] == armed
    assert _timers(tmp_path)[0]["status"] == "fired"


@pytest.mark.parametrize("due_at", ["soon", "", "2026-10-14 12:00:00", "2026-10-14T12:00:00+00:00",
                                    "2026-10-14T12:00:00Z\n"])
def test_a_review_whose_due_time_cannot_be_read_is_due(tmp_path, capsys, clock, due_at):
    from hoh.timers import SCHEMA, Timer, TimerKind

    _store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON))
    row = Timer(timer_id="deadbeef-review_due", due_at="2030-01-01T00:00:00Z",
                kind=TimerKind.REVIEW_DUE, program_id="p", program_epoch=0)
    body = json.loads(row.model_dump_json())
    body["due_at"] = due_at                           # a hand-written row: arm() would refuse it
    (tmp_path / "timers.json").write_text(json.dumps({"schema_version": SCHEMA, "timers": [body]}))
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "REVIEW_DUE" and doc["continued"] == []
    assert _review(doc)["state"] == "DUE" and _review(doc)["timer"] == "deadbeef-review_due"
    assert _timers(tmp_path)[0]["status"] == "fired"


def test_a_hold_that_is_itself_due_keeps_its_reason_and_fires_the_review(tmp_path, capsys,
                                                                          clock):
    _store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON), pin=False)
    armed = _review(_continue(capsys, tmp_path, path, "w1")[1])["timer"]
    clock("2026-10-14T13:00:00Z")
    code, doc, _ = _continue(capsys, tmp_path, path, "w2")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc)["state"] == "DUE" and _review(doc)["timer"] == armed
    assert _timers(tmp_path)[0]["status"] == "fired"
    code, doc, _ = _continue(capsys, tmp_path, path, "w3")
    assert doc["reason"] == "CONTRACT_NOT_PINNED" and _review(doc)["state"] == "ARMED", (
        "the hold goes on, so it gets a new review")
    assert [t["status"] for t in _timers(tmp_path)] == ["fired", "armed"]


def test_a_review_that_cannot_be_fired_holds_again_and_stops_at_the_first_that_can(
        tmp_path, capsys, clock, monkeypatch):
    from hoh.timers import TimerStore, TimerStoreError

    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract, pin=False)
    armed = _review(_continue(capsys, tmp_path, path, "w1")[1])["timer"]
    _pin(tmp_path, contract)
    clock(DUE)
    real = TimerStore.fire

    def broken(self, timer_id):
        raise TimerStoreError("no space left")

    monkeypatch.setattr(TimerStore, "fire", broken)
    for wave in ("w2", "w3"):                              # never a continue while it is due
        code, doc, _ = _continue(capsys, tmp_path, path, wave)
        assert code == 1 and doc["reason"] == "REVIEW_DUE" and doc["continued"] == []
        assert _timers(tmp_path)[0]["status"] == "armed"
    monkeypatch.setattr(TimerStore, "fire", real)
    code, doc, _ = _continue(capsys, tmp_path, path, "w4")
    assert code == 1 and doc["reason"] == "REVIEW_DUE" and _review(doc)["timer"] == armed
    assert _timers(tmp_path)[0]["status"] == "fired"
    assert _continue(capsys, tmp_path, path, "w5")[0] == 0


def test_a_timer_that_cannot_be_armed_is_said_in_the_record_and_the_hold_stays_a_hold(
        tmp_path, capsys, clock, monkeypatch):
    from hoh.timers import TimerStore, TimerStoreError

    store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON), pin=False)

    def broken(self, **kw):
        raise TimerStoreError("no space left")

    monkeypatch.setattr(TimerStore, "arm_new", broken)
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc)["state"] == "NOT_ARMED" and _review(doc)["needs_you"] is True
    assert "TimerStoreError" in _review(doc)["detail"] and _review(doc)["timer"] == ""
    assert _timers(tmp_path) == []
    assert (store.dir / "policy-continuations" / "w1.json").is_file(), "the hold is recorded"


def test_an_interval_no_timer_can_name_is_not_armed_and_says_so(tmp_path, capsys, clock):
    _store, path = _setup(tmp_path, capsys,
                         _contract(M.UNATTENDED_LONG_HORIZON, review_after=10**13), pin=False)
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc)["state"] == "NOT_ARMED" and _review(doc)["needs_you"] is True
    assert _timers(tmp_path) == []


def test_a_review_of_another_project_or_epoch_is_not_this_ones(tmp_path, capsys, clock):
    from hoh.timers import TimerKind, TimerStore

    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract)
    other = TimerStore(tmp_path)
    for kind, project, epoch in ((TimerKind.REVIEW_DUE, "q", 0), (TimerKind.REVIEW_DUE, "p", 1),
                                 (TimerKind.REVIEW_DUE, "P", 0), (TimerKind.HEARTBEAT, "p", 0)):
        other.arm_new(kind=kind, due_at="2026-10-14T11:00:00Z", program_id=project,
                      program_epoch=epoch)
    before = (tmp_path / "timers.json").read_bytes()
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 0 and doc["outcome"] == "CONTINUE"
    assert _review(doc)["state"] == "IDLE"
    assert (tmp_path / "timers.json").read_bytes() == before, "nothing fired, nothing armed"
    assert [t.status.value for t in TimerStore(tmp_path).read()] == ["armed"] * 4


def test_a_dry_run_decides_what_the_pass_would_and_writes_nothing(tmp_path, capsys, clock):
    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    store, path = _setup(tmp_path, capsys, contract, pin=False)
    code, doc, _ = _continue(capsys, tmp_path, path, "d1", "--dry-run")
    assert code == 1 and _review(doc)["state"] == "WOULD_ARM" and doc["dry_run"] is True
    assert not (tmp_path / "timers.json").exists()
    assert not (store.dir / "policy-continuations").exists()
    _continue(capsys, tmp_path, path, "w1")
    _pin(tmp_path, contract)
    clock(DUE)
    before = (tmp_path / "timers.json").read_bytes()
    code, doc, _ = _continue(capsys, tmp_path, path, "d2", "--dry-run")
    assert code == 1 and doc["reason"] == "REVIEW_DUE" and _review(doc)["state"] == "DUE"
    assert (tmp_path / "timers.json").read_bytes() == before, "a dry run fires nothing"
    assert _timers(tmp_path)[0]["status"] == "armed"


def test_every_review_that_is_due_is_fired_and_the_oldest_is_named(tmp_path, capsys, clock):
    from hoh.timers import TimerKind, TimerStore

    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract)
    timers = TimerStore(tmp_path)
    late = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-10-14T11:40:00Z", program_id="p")
    early = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-10-14T11:10:00Z",
                           program_id="p")
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "REVIEW_DUE"
    assert _review(doc)["timer"] == early.timer_id, "the oldest is the one the record names"
    assert {t["timer_id"]: t["status"] for t in _timers(tmp_path)} == {
        early.timer_id: "fired", late.timer_id: "fired"}
    assert _continue(capsys, tmp_path, path, "w2")[0] == 0


@pytest.mark.parametrize("seconds", [None, 0, -5, True, "900", 1.5])
def test_a_contract_that_names_no_whole_interval_arms_nothing_and_says_so(tmp_path, seconds):
    gov = _gov()
    contract = types.SimpleNamespace(
        mode=M.UNATTENDED_LONG_HORIZON, program_epoch=0,
        horizon=types.SimpleNamespace(max_concurrency=2, review_after_seconds=seconds))
    held = gov.PolicyContinuation(
        project_id="p", wave_id="w1", at=NOW, contract={}, state={}, outcome="HOLD",
        reason="CONTRACT_NOT_PINNED", limit_policy="", continued=(), withheld=(), routings=(),
        ways_out={}, stuck=(), limits={}, handlings={}, not_enacted=())
    out, due = gov.apply_mode(contract, tmp_path, held, NOW, dry_run=False)
    assert due == [] and out.outcome == "HOLD" and out.reason == "CONTRACT_NOT_PINNED"
    assert out.mode_wave["review"]["state"] == "NOT_ARMED"
    assert out.mode_wave["review"]["needs_you"] is True
    assert not (tmp_path / "timers.json").exists()


def test_a_caller_that_skips_a_decided_wave_must_ask_review_is_due_first(tmp_path, capsys, clock):
    """P3-30's loop names a wave by the state it decides and ends the pass when that record
    exists; `write_seq` does not move when a record is written, so a review that comes due while
    the programme sits held is never seen unless the loop asks first and decides again."""
    gov = _gov()
    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    _store, path = _setup(tmp_path, capsys, contract, pin=False)
    assert _review(_continue(capsys, tmp_path, path, "c7-d1")[1])["state"] == "ARMED"
    _pin(tmp_path, contract)                              # the cause of the hold is gone
    clock(EARLY)
    assert gov.review_is_due(contract, tmp_path, "p") is False
    clock(DUE)
    assert gov.review_is_due(contract, tmp_path, "p") is True
    code, doc, err = _continue(capsys, tmp_path, path, "c7-d1")         # the same state, as the loop
    assert code == 1 and doc is None and err.startswith("not recorded: ")
    assert _timers(tmp_path)[0]["status"] == "armed", "a record that exists decides nothing again"
    code, doc, _ = _continue(capsys, tmp_path, path, "c7-d1-r1")        # decided again, new name
    assert code == 1 and doc["reason"] == "REVIEW_DUE" and _timers(tmp_path)[0]["status"] == "fired"
    assert gov.review_is_due(contract, tmp_path, "p") is False
    assert _continue(capsys, tmp_path, path, "c7-d1-r2")[0] == 0


def test_review_is_due_is_asked_of_this_mode_project_and_epoch_and_fails_closed(tmp_path, capsys,
                                                                              clock):
    from hoh.timers import TimerKind, TimerStore

    gov = _gov()
    contract = _contract(M.UNATTENDED_LONG_HORIZON)
    TimerStore(tmp_path).arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-10-14T11:00:00Z",
                                 program_id="p")
    assert gov.review_is_due(contract, tmp_path, "p", NOW) is True
    assert gov.review_is_due(contract, tmp_path, "q", NOW) is False, "another project"
    assert gov.review_is_due(_contract(M.UNATTENDED_LONG_HORIZON, epoch=1), tmp_path, "p",
                             NOW) is False, "another programme epoch"
    assert gov.review_is_due(contract, tmp_path, "p", "2026-10-14T10:59:59Z") is False
    assert gov.review_is_due(_contract(M.PLAN_AUTONOMOUS), tmp_path, "p", NOW) is False
    assert gov.review_is_due(types.SimpleNamespace(mode="nonsense"), tmp_path, "p", NOW) is False
    (tmp_path / "timers.json").write_text("{")
    assert gov.review_is_due(contract, tmp_path, "p", NOW) is True, "unreadable: decide again"
    assert gov.review_is_due(_contract(M.PLAN_AUTONOMOUS), tmp_path, "p", NOW) is False


# --- F: timer files that cannot be read ----------------------------------------------------

def _padded(path: Path, size: int) -> None:
    """A valid, empty timer set of exactly `size` bytes (blanks after the document)."""
    body = b'{"timers": []}'
    path.write_bytes(body + b" " * (size - len(body)))


def _make_unreadable(tmp_path: Path, how: str) -> None:
    path = tmp_path / "timers.json"
    if how == "not json":
        path.write_text("{")
    elif how == "not an object":
        path.write_text("[]")
    elif how == "no timer set":
        path.write_text('{"timers": 3}')
    elif how == "a row that is not a timer":
        path.write_text('{"timers": [{"x": 1}]}')
    elif how == "not utf-8":
        path.write_bytes(b"\xff\xfe{\x00")
    elif how == "a directory":
        path.mkdir()
    elif how == "a fifo":
        os.mkfifo(path)
    elif how == "a link":
        real = tmp_path / "real-timers.json"
        real.write_text('{"timers": []}')
        os.symlink(real, path)
    elif how == "a dangling link":
        os.symlink(tmp_path / "nowhere.json", path)
    elif how == "a second name":
        path.write_text('{"timers": []}')
        os.link(path, tmp_path / "second-name.json")
    elif how == "oversize":
        _padded(path, _gov().MAX_TIMER_FILE_BYTES + 1)
    elif how == "deeply nested":
        path.write_text("[" * 200000)
    elif how == "mode 000":
        path.write_text('{"timers": []}')
        os.chmod(path, 0)
    else:
        raise AssertionError(how)


UNREADABLE = ["not json", "not an object", "no timer set", "a row that is not a timer",
              "not utf-8", "a directory", "a fifo", "a link", "a dangling link",
              "a second name", "oversize", "deeply nested", "mode 000"]


@pytest.mark.parametrize("how", UNREADABLE)
def test_timers_that_cannot_be_read_hold_a_wave_that_would_continue(tmp_path, capsys, clock, how):
    if how == "mode 000" and os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON))
    _make_unreadable(tmp_path, how)
    seen = sorted(p.name for p in tmp_path.iterdir())
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1
    assert (doc["outcome"], doc["reason"], doc["continued"]) == ("HOLD", "REVIEW_UNREADABLE", [])
    assert _review(doc)["state"] == "UNREADABLE" and _review(doc)["needs_you"] is True
    assert _review(doc)["detail"] != ""
    assert (store.dir / "policy-continuations" / "w1.json").is_file(), "the hold is recorded"
    assert sorted(p.name for p in tmp_path.iterdir()) == seen, "nothing was written beside it"


def test_a_timer_file_of_exactly_the_most_that_is_read_is_read(tmp_path, capsys, clock):
    _store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON))
    _padded(tmp_path / "timers.json", _gov().MAX_TIMER_FILE_BYTES)
    assert _gov().MAX_TIMER_FILE_BYTES == 1 << 24
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 0 and doc["outcome"] == "CONTINUE" and _review(doc)["state"] == "IDLE"


@pytest.mark.parametrize("how", ["not json", "a fifo", "a link", "oversize"])
def test_a_hold_with_timers_that_cannot_be_read_keeps_its_own_reason(tmp_path, capsys, clock,
                                                                      how):
    _store, path = _setup(tmp_path, capsys, _contract(M.UNATTENDED_LONG_HORIZON), pin=False)
    _make_unreadable(tmp_path, how)
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 1 and doc["reason"] == "CONTRACT_NOT_PINNED"
    assert _review(doc)["state"] == "UNREADABLE" and _review(doc)["needs_you"] is True


@pytest.mark.parametrize("how", ["not json", "a fifo", "a directory", "oversize"])
def test_the_other_modes_continue_whatever_the_timer_file_is(tmp_path, capsys, clock, how):
    _store, path = _setup(tmp_path, capsys, _contract(M.PLAN_AUTONOMOUS))
    _make_unreadable(tmp_path, how)
    code, doc, _ = _continue(capsys, tmp_path, path, "w1")
    assert code == 0 and doc["continued"] == ["i1", "i2", "i3"]
