"""P3-30 slice 2: the project loop continues beside a parked branch.

Until now `ProjectController` stopped at the first node that waited for a decision: an EXTERNAL node at the
head of the READY frontier, or a run the autonomy contract parked, ended the drive, and the independent READY
nodes behind it waited for a person who was not needed for them. The decision (`governance.continue_by_policy`)
and the routing of the open decisions (`governance.route_project_decisions`) existed since P3-28/31/32 slice 1,
called by two operator commands and by no loop.

A project that registers an autonomy contract (`orchestrator.AUTONOMY_FILE`, beside `driver.json`) now gets,
after a halt beside which a branch may be parked, up to `CONTINUATION_PASSES` passes: route the open decisions,
decide the wave and write it down (create-only, before anything is dispatched), then run the loop's own rounds
-- sequential or wave -- over the nodes the record names and no others. The launcher passes the same file to
every `hoh run` it starts (`--autonomy`), which is what pins the runs the decision checks.

What is real: `cli.main` (`hoh project drive`), the registered driver, `drive_governed`, `ProjectController`,
the wave round, `governance`, `bind` and `Governor` (the run pins and the parked branches), `HohRunLauncher`
(for the argument test). What is replaced is the process boundary: the launcher of the loop tests is a fake that
"runs" a node by returning a verdict (and, for a parked node, by writing the run's own pin and routing the way
`hoh run` does). A test reads nothing outside `tmp_path` except this directory (the driver factory is imported
from here, as `driver.json` registers it) and never removes anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from hoh import cli, governance
from hoh.autonomy import (
    AutonomyContract, AutonomyMode, Escalation, Horizon, NeedsYou, ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.governance import Governor, bind
from hoh.launcher import Dispatch, HohRunLauncher
from hoh import orchestrator
from hoh.orchestrator import (
    GateRunner, HaltClass, MergeFailure, MergeResult, ProjectController, Result, RunOutcome, RunVerdict,
    write_lock,
)
from hoh.project import ActionClass, GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.resumeloop import drive_governed, load_driver
from hoh.succession import MasterAuthority, StaleMaster

READY, BLOCKED, MERGED = Lifecycle.READY, Lifecycle.BLOCKED, Lifecycle.MERGED
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL
NOW = "2026-10-18T12:00:00Z"
CREATED = "2026-10-18T11:50:00Z"
HERE = Path(__file__).resolve().parent
#: spelled out here, not imported: the name is part of the contract (a project registers a file by it)
AUTONOMY_FILE = "autonomy-contract.json"
#: the ceiling is part of the contract too; the bound test also reads it from the product
CONTINUATION_PASSES = 3
#: the driver factory reads its world from here (the module is imported by name through `driver.json`)
CONFIG: dict = {}


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(governance, "utcnow", lambda: NOW)


# --- the world -------------------------------------------------------------------------------

def _contract(*, concurrency=4, contract_id="c-park") -> AutonomyContract:
    """UNATTENDED_LONG_HORIZON: a decision a run cannot take parks its branch, an EXTERNAL action is
    handed to a resolver (it is approved by nobody)."""
    return AutonomyContract(
        contract_id=contract_id, mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.OFF,
        scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900,
                        max_concurrency=concurrency),
        budgets=Budgets(max_dispatches=50, max_wallclock_seconds=3600),
        release_policy=ReleasePolicy.NEVER_AUTO_PUBLISH,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
                    "release": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "scope": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "EXTERNAL": Escalation.SYSTEM_TWO})


def _plan_contract() -> AutonomyContract:
    """PLAN_AUTONOMOUS with a person expected (Needs-You on a digest): a node blocked with a note has a
    human gate as its way out, whatever blocked it."""
    return AutonomyContract(
        contract_id="c-plan", mode=AutonomyMode.PLAN_AUTONOMOUS, needs_you=NeedsYou.DIGEST_ONLY,
        release_policy=ReleasePolicy.AUTO_PREPARE_ONLY, allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
                    "release": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "scope": Escalation.PARK_BRANCH_AND_CONTINUE, "EXTERNAL": Escalation.SYSTEM_TWO},
        horizon=Horizon(max_wallclock_seconds=3600, max_concurrency=4),
        budgets=Budgets(max_dispatches=50, max_wallclock_seconds=3600))


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class Launcher:
    """The `RunLauncher` protocol without a process. `script[node]` is "ok" (accepts) or "park" (the run
    stops on a decision it cannot take: it writes its own pin and the contract's routing, as `hoh run`
    does, and ends blocked). `launched` is the order of every dispatch."""

    wave_width = 1
    #: modes that end the run with a verdict other than an acceptance: "unclassified" is the run that
    #: ended blocked with nothing in its routing log (a node the loop cannot explain), the others are
    #: the halts that are not a parked branch
    OUTCOMES = {"unclassified": RunVerdict.UNDETERMINED, "notrun": RunVerdict.NOT_RUN,
                "inflight": RunVerdict.IN_FLIGHT, "storage": RunVerdict.STORAGE_UNAVAILABLE,
                "budget": RunVerdict.BUDGET_EXHAUSTED, "provider": RunVerdict.PROVIDER_UNAVAILABLE}

    def __init__(self, root: Path, contract: AutonomyContract, script=None):
        self.root, self.contract, self.script = Path(root), contract, dict(script or {})
        self.launched: list[str] = []
        self.merged: list[str] = []
        #: node -> the continuation records that existed when it was dispatched
        self.records_at_launch: dict[str, list[str]] = {}

    def action_class(self, node):
        return node.action_class

    def depends_on(self, a, b):
        wa, wb = set(a.writes), set(b.writes)
        return bool(wa & wb or wa & set(b.semantic_reads) or wb & set(a.semantic_reads))

    def prepare(self, node):
        return None

    def accepted_baseline(self, node):
        return None

    def evaluate(self, node):
        return RunOutcome(RunVerdict.NOT_STARTED, "no run yet")

    def launch(self, node):
        self.launched.append(node.id)
        records = self.root / "projects" / "p" / governance.POLICY_DIR
        self.records_at_launch[node.id] = sorted(p.name for p in records.iterdir()) if records.is_dir() else []
        mode = self.script.get(node.id, "ok")
        if mode in self.OUTCOMES:
            return RunOutcome(self.OUTCOMES[mode], f"{mode}: the run ended without a verdict of its own")
        if mode == "park":
            run = self.root / (node.run_id or node.id)
            run.mkdir(exist_ok=True)
            bind(run, contract=self.contract)
            Governor(contract=self.contract, run_dir=run).decide(
                decision=f"{node.id} needs an answer", kind="decision", delegable=False)
            return RunOutcome(RunVerdict.UNDETERMINED, "run blocked (parked_by_contract): an answer")
        return RunOutcome(RunVerdict.ACCEPTED, "accepted", candidate="a" * 40, candidate_tree="t" * 16)

    def merge(self, node, outcome):
        if self.script.get(node.id) == "conflict":
            return MergeResult(landed=False, failure=MergeFailure.CONFLICT, detail="a content conflict")
        self.merged.append(node.id)
        return MergeResult(landed=True, integrated="b" * 40, candidate=outcome.candidate or "")


def build_driver(store):
    return CONFIG["launcher"], Gates()


def _node(node_id, lifecycle=READY, action=INTERNAL, **kw) -> TaskNode:
    kw.setdefault("writes", [f"{node_id}.txt"])
    return TaskNode(id=node_id, lifecycle=lifecycle, action_class=action, **kw)


def _push(node_id="push") -> TaskNode:
    return _node(node_id, action=EXTERNAL, note="push the tag v1 to origin", writes=[])


def _pin(root: Path, run_id: str, contract: AutonomyContract) -> None:
    """An earlier run's own pin, through the product's writer."""
    run = root / run_id
    run.mkdir(exist_ok=True)
    bind(run, contract=contract)


def _world(tmp_path, nodes, *, script=None, contract=None, register=True, pinned=("base",)):
    """The project `p` under `root`, its driver registered, its contract registered when asked, and the runs
    named in `pinned` pinned (the first run's pin is what lets a wave be decided at all)."""
    contract = contract or _contract()
    root = tmp_path / "root"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=list(nodes),
                              created_at=CREATED))
    if register:
        (store.dir / AUTONOMY_FILE).write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    for run_id in pinned:
        _pin(root, run_id, contract)
    launcher = Launcher(root, contract, script)
    CONFIG.clear()
    CONFIG["launcher"] = launcher
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_parked_continuation:build_driver", "path": str(HERE)}), encoding="utf-8")
    return root, store, launcher, contract


def _base() -> TaskNode:
    return _node("base", MERGED, run_id="base")


def _drive(capsys, root, *argv):
    code = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA", *argv])
    return code, json.loads(capsys.readouterr().out)


def _lifecycles(store) -> dict:
    return {n.id: n.lifecycle for n in store.read_state().nodes}


def _records(store, directory) -> list[Path]:
    d = store.dir / directory
    return sorted(d.iterdir()) if d.is_dir() else []


def _tree(path: Path) -> dict:
    seen = {}
    for base, dirs, files in os.walk(path):
        for name in ["", *dirs, *files]:
            p = os.path.join(base, name) if name else base
            info = os.lstat(p)
            digest = hashlib.sha256(Path(p).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            seen[os.path.relpath(p, path)] = (info.st_mode, info.st_size, digest)
    return seen


def _waves_of(store) -> list[dict]:
    """The continuation records of the project, oldest state version first."""
    found = [json.loads(p.read_text(encoding="utf-8")) for p in _records(store, governance.POLICY_DIR)]
    return sorted(found, key=lambda r: r["state"]["write_seq"])


def _wave_of(store) -> dict:
    return _waves_of(store)[0]


# --- C: what continues -----------------------------------------------------------------------

def test_hoh_project_drive_continues_beside_a_branch_the_contract_parked(tmp_path, capsys):
    """The central claim, through the product's command. Node `a`'s run parks on a decision; before this
    slice the drive ended there with b and c READY and untouched. Now the decision is routed and decided,
    the record is written, and b and c are dispatched by the loop's own rounds and merged; `a` waits."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("a"), _node("b"), _node("c")],
                                             script={"a": "park"})
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", "b", "c"] and launcher.merged == ["b", "c"], out
    assert _lifecycles(store) == {"base": MERGED, "a": BLOCKED, "b": MERGED, "c": MERGED}
    assert out["halt"] == HaltClass.BLOCKED_EXTERNAL.value and code == 1, out
    assert "still wait for an answer" in out["reason"]
    kinds = [s.split(":")[0] for s in out["steps"]]
    assert kinds.index("AMBIGUOUS") < kinds.index("CONTINUATION") < kinds.index("MERGED")
    record = _wave_of(store)
    assert (record["outcome"], record["continued"], record["approves"]) == ("CONTINUE", ["b", "c"], False)
    assert record["contract"]["digest"] == contract.digest()
    assert record["wave_id"].endswith(contract.digest())
    # the record is the counter and exists before the dispatch it counts: not when `a` ran, before `b` and `c`
    assert launcher.records_at_launch == {"a": [], "b": [record["wave_id"] + ".json"],
                                          "c": [record["wave_id"] + ".json"]}


def test_an_external_node_at_the_head_no_longer_holds_the_independent_work_back(tmp_path, capsys):
    """The other way a drive ended beside a decision: the head of the frontier is an EXTERNAL action. It is
    routed (a record that hands it to a resolver and approves nothing), never launched; i1 and i2 run."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _push(), _node("i1"), _node("i2")])
    code, out = _drive(capsys, root)
    assert launcher.launched == ["i1", "i2"], "the external action is nobody's to launch here"
    assert _lifecycles(store) == {"base": MERGED, "push": READY, "i1": MERGED, "i2": MERGED}
    assert out["halt"] == HaltClass.BLOCKED_EXTERNAL.value and code == 1
    [routing] = _records(store, governance.ROUTINGS_DIR)
    body = json.loads(routing.read_text(encoding="utf-8"))
    assert (body["node_id"], body["disposition"], body["approves"]) == ("push", "HANDED_TO_RESOLVER", False)
    assert routing.name.endswith(f"-{contract.digest()}.json")
    assert _wave_of(store)["continued"] == ["i1", "i2"]


def test_only_the_nodes_the_record_names_are_dispatched(tmp_path, capsys):
    """`c` reads what the parked node `a` writes, so the record withholds it; the loop must not take it
    because it is READY. Catches a restricted drive that does not restrict."""
    c = _node("c", semantic_reads=["a.txt"])
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b"), c], script={"a": "park"})
    _drive(capsys, root)
    assert launcher.launched == ["a", "b"], "c is not independent of the branch that waits"
    assert _lifecycles(store)["c"] is READY
    record = _wave_of(store)
    assert record["continued"] == ["b"]
    assert [(w["node"], w["blocked_by"]) for w in record["withheld"]] == [("c", "a")]


def test_a_wave_is_dispatched_through_the_loops_own_wave_round(tmp_path):
    """There is no second dispatcher: with a width of two the continued nodes go out together through
    `_wave_round` (its record, its barrier), not one by one."""
    import test_wave_controller as twc

    repo, root, store = twc._world(tmp_path, names=("push", "n1", "n2"),
                                   by_name={"push": dict(action_class=EXTERNAL, note="push v1")})
    contract = _contract()
    state = store.read_state()
    state.nodes.insert(0, _base())
    state.created_at = CREATED
    store.write_state(state)
    (store.dir / AUTONOMY_FILE).write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    _pin(root, "base", contract)
    launcher = twc.FakeCliLauncher(root, repo, {"n1": "overlap", "n2": "overlap"},
                                   barrier=threading.Barrier(2))
    result = ProjectController(store, launcher, twc.Gates(), width=2).run()
    kinds = [s.kind for s in result.steps]
    assert result.halt is HaltClass.BLOCKED_EXTERNAL and "WAVE" in kinds, kinds
    assert kinds.index("CONTINUATION") < kinds.index("WAVE")
    starts = {e[1]: e[2] for e in launcher.events if e[0] == "start"}
    ends = {e[1]: e[2] for e in launcher.events if e[0] == "end"}
    assert sorted(starts) == ["n1", "n2"] and max(starts.values()) < min(ends.values())
    assert sorted(launcher.merged) == ["n1", "n2"]
    assert {n.id: n.lifecycle for n in store.read_state().nodes}["push"] is READY


def test_the_resume_loops_drive_continues_too(tmp_path):
    """`hoh resume-quota --drive` and `recover_projects` drive through `drive_governed`, not through the
    command: the registered contract must reach the loop there as well."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")])
    authority = MasterAuthority(store.dir, "sR", serialize=lambda: write_lock(store))
    authority.acquire(program_generation=0, note="a resume pass")
    result = drive_governed(store, authority, load_driver(store.dir))
    assert launcher.launched == ["i1"] and result.halt is HaltClass.BLOCKED_EXTERNAL, result.reason
    assert _lifecycles(store)["i1"] is MERGED


# --- N: what does not change -----------------------------------------------------------------

def test_a_project_without_a_registered_contract_drives_exactly_as_before(tmp_path, capsys):
    """Opt-in by registration. Catches a loop that routes or continues for a project that never said so."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b"), _node("c")],
                                      script={"a": "park"}, register=False)
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1
    assert out["halt"] == HaltClass.AMBIGUOUS.value
    assert "unclassifiable verdict" in out["reason"] and "raised" not in out["reason"], out["reason"]
    assert _lifecycles(store)["b"] is READY and _lifecycles(store)["c"] is READY
    assert not (store.dir / governance.POLICY_DIR).exists()
    assert not (store.dir / governance.ROUTINGS_DIR).exists()
    assert not [s for s in out["steps"] if s.startswith("CONTINUATION")]


def test_an_external_node_with_nothing_ready_behind_it_is_not_routed_by_a_pass(tmp_path, capsys):
    """The pass is for work that could carry on. An EXTERNAL node alone is the operator's: the drive halts
    as it always did and writes neither a routing nor a continuation record."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push()])
    code, out = _drive(capsys, root)
    assert code == 1 and out["halt"] == HaltClass.BLOCKED_EXTERNAL.value and launcher.launched == []
    assert not (store.dir / governance.POLICY_DIR).exists()
    assert not (store.dir / governance.ROUTINGS_DIR).exists()


# --- H: what holds, and the bound ------------------------------------------------------------

def test_a_contract_no_run_is_pinned_to_holds_the_wave_and_the_hold_is_recorded_once(tmp_path, capsys):
    """Nothing is dispatched on a HOLD; the halt is the one the drive had, with the reason in a step; the
    record exists (create-only) and a further drive over the same state writes nothing at all."""
    root, store, launcher, _ = _world(tmp_path, [_push(), _node("i1")], pinned=())
    code, first = _drive(capsys, root)
    assert launcher.launched == [] and code == 1 and first["halt"] == HaltClass.BLOCKED_EXTERNAL.value
    assert any(s.startswith("CONTINUATION:") for s in first["steps"])
    assert _wave_of(store)["reason"] == governance.CONTRACT_NOT_PINNED
    assert _lifecycles(store)["i1"] is READY
    state_before = (store.dir / "project.json").read_bytes()
    kept = {d: _tree(store.dir / d) for d in (governance.POLICY_DIR, governance.ROUTINGS_DIR)
            if (store.dir / d).exists()}
    code2, second = _drive(capsys, root)
    assert launcher.launched == [] and code2 == 1
    assert [s for s in second["steps"] if s.startswith("CONTINUATION_DECIDED")], second["steps"]
    # a further drive writes no state and nothing of the continuation (the lease files are its own)
    assert (store.dir / "project.json").read_bytes() == state_before
    assert {d: _tree(store.dir / d) for d in kept} == kept


def test_a_review_that_falls_due_while_the_state_stands_still_is_decided_once_more(tmp_path, capsys):
    """A hold is decided once per state version, and recording it moves nothing. A review timer
    (REVIEW_DUE) that falls due in the meantime is part of the wave's name: the next drive decides again --
    once for that timer, and no more -- so a review is read by the loop. The timers are those of the runs
    root, where the modes of P3-28/29 arm them (an earlier form of this node armed them in the project's
    directory, which `governance` never reads: the loop looked there too, and saw no review)."""
    from hoh.timers import TimerKind, TimerStore

    root, store, launcher, _ = _world(tmp_path, [_push(), _node("i1")], pinned=())
    _drive(capsys, root)
    assert len(_records(store, governance.POLICY_DIR)) == 1
    timers = TimerStore(root)
    not_yet = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2099-01-01T00:00:00Z", program_id="p")
    _drive(capsys, root)
    assert len(_records(store, governance.POLICY_DIR)) == 1, "a review that is not due changes nothing"
    due = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-01-01T00:00:00Z", program_id="p")
    _, second = _drive(capsys, root)
    names = [p.name for p in _records(store, governance.POLICY_DIR)]
    assert len(names) == 2 and any(n.endswith(f"-{due.timer_id}.json") for n in names), names
    assert [s for s in second["steps"] if s.startswith("CONTINUATION:")]
    _, third = _drive(capsys, root)                  # the review was read: the drive after it is a normal one
    assert len(_records(store, governance.POLICY_DIR)) == 2
    assert [s for s in third["steps"] if s.startswith("CONTINUATION_DECIDED")]
    assert launcher.launched == [] and not_yet.timer_id != due.timer_id


def test_the_passes_of_one_drive_are_bounded(tmp_path, capsys):
    """Every continued node parks again, so every pass finds more to continue: the drive stops at
    CONTINUATION_PASSES passes, says so, and leaves the rest READY for the next drive to decide."""
    assert orchestrator.CONTINUATION_PASSES == CONTINUATION_PASSES
    names = [f"n{i}" for i in range(CONTINUATION_PASSES + 3)]
    root, store, launcher, _ = _world(
        tmp_path, [_base(), _node("a"), *[_node(n) for n in names]],
        script={n: "park" for n in ["a", *names]}, contract=_contract(concurrency=1))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", *names[:CONTINUATION_PASSES]], launcher.launched
    assert len(_records(store, governance.POLICY_DIR)) == CONTINUATION_PASSES
    assert [s for s in out["steps"] if s.startswith("CONTINUATION_PASSES_USED")]
    assert all(_lifecycles(store)[n] is READY for n in names[CONTINUATION_PASSES:])
    assert code == 1


def test_a_record_that_cannot_be_written_dispatches_nothing_and_says_so(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")], script={"a": "park"})
    (store.dir / governance.POLICY_DIR).write_text("not a directory", encoding="utf-8")
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1
    assert [s for s in out["steps"] if s.startswith("CONTINUATION_REFUSED")], out["steps"]
    assert _lifecycles(store)["b"] is READY


def test_a_routing_that_cannot_be_recorded_dispatches_nothing_and_says_so(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")])
    (store.dir / governance.ROUTINGS_DIR).write_text("not a directory", encoding="utf-8")
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and code == 1
    assert [s for s in out["steps"] if s.startswith("CONTINUATION_REFUSED")], out["steps"]
    assert not (store.dir / governance.POLICY_DIR).exists()


def test_a_state_version_that_was_decided_before_is_not_decided_again(tmp_path, capsys):
    """The record is the counter and it is written before the attempt: a drive that finds the record of its
    state version dispatches nothing from it (the attempt may have died half way) and writes nothing."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _push(), _node("i1")])
    seq = store.read_state().write_seq
    governance.route_project_decisions(store, contract, root)
    governance.continue_by_policy(store, contract, root, wave_id=f"c{seq}-{contract.digest()}",
                                  depends_on=launcher.depends_on)
    before = _tree(store.dir / governance.POLICY_DIR), _tree(store.dir / governance.ROUTINGS_DIR)
    _drive(capsys, root)
    assert launcher.launched == []
    assert (_tree(store.dir / governance.POLICY_DIR), _tree(store.dir / governance.ROUTINGS_DIR)) == before


# --- X: the fences of the pass ------------------------------------------------------------------

def test_a_node_blocked_for_no_reason_a_routing_explains_is_not_a_parked_branch(tmp_path, capsys):
    """A run that ended blocked with nothing in its routing log (a merge of unknown state, a capability
    violation) is not a branch the contract parked. A contract that expects a person has a human gate as the
    way out of ANY blocked node with a note, so the policy alone would continue past it as soon as an EXTERNAL
    node supplies an open decision: the loop must not. Catches a pass that trusts the policy's human gate."""
    root, store, launcher, _ = _world(
        tmp_path, [_base(), _node("a"), _push(), _node("b"), _node("c")], script={"a": "unclassified"},
        contract=_plan_contract())
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1, out
    assert out["halt"] == HaltClass.AMBIGUOUS.value
    assert [s for s in out["steps"] if s.startswith("CONTINUATION_REFUSED")], out["steps"]
    assert _lifecycles(store)["b"] is READY and _lifecycles(store)["c"] is READY
    assert not (store.dir / governance.POLICY_DIR).exists()


def test_the_same_block_is_no_parked_branch_under_a_hard_human_gate_either(tmp_path, capsys):
    gated = _contract().model_copy(update={"hard_human_gates": ("decision",)})
    root, store, launcher, _ = _world(
        tmp_path, [_base(), _node("a"), _push(), _node("b")], script={"a": "unclassified"}, contract=gated)
    _drive(capsys, root)
    assert launcher.launched == ["a"]
    assert not (store.dir / governance.POLICY_DIR).exists()


@pytest.mark.parametrize("mode", ["notrun", "inflight", "storage", "budget", "provider", "conflict"])
def test_a_halt_that_is_not_a_parked_branch_is_not_continued_past(tmp_path, capsys, mode):
    """Provider, budget, storage, merge, capacity-like and live-run halts have owners of their own (the
    resume timers, the operator). With an open decision and READY siblings, none starts a pass. `notrun`
    and `inflight` leave no blocked node behind, so only the halt test itself keeps them out."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _push(), _node("b"), _node("c")],
                                      script={"a": mode})
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1, out
    assert out["halt"] not in (HaltClass.AMBIGUOUS.value, HaltClass.BLOCKED_EXTERNAL.value) or mode == "conflict"
    assert not (store.dir / governance.POLICY_DIR).exists()
    assert not (store.dir / governance.ROUTINGS_DIR).exists()


def test_an_ambiguous_halt_with_no_open_decision_is_not_continued(tmp_path):
    """The pass needs a decision to continue beside: an AMBIGUOUS halt over READY nodes and nothing waiting
    for an answer is the loop's own business. Catches a pass that is made on the halt alone."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")])

    class Ambiguous(ProjectController):
        def _drive(self):
            if self._allowed is None:
                return Result(HaltClass.AMBIGUOUS, "something the loop cannot classify")
            return super()._drive()

    result = Ambiguous(store, launcher, Gates()).run()
    assert result.halt is HaltClass.AMBIGUOUS and launcher.launched == []
    assert not (store.dir / governance.POLICY_DIR).exists()
    assert not (store.dir / governance.ROUTINGS_DIR).exists()


def test_a_timed_wait_beside_a_parked_branch_does_not_stop_the_pass(tmp_path, capsys):
    """The fence is for blocks nobody explains. A node waiting for a time (a usage limit with its retry time)
    is explained by its own kind, so the independent work still continues beside the parked branch."""
    waiting = _node("t", BLOCKED, blocked_kind="usage_limit", retry_after="2026-10-19T00:00:00Z",
                    note="usage limit")
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), waiting, _node("b")],
                                      script={"a": "park"})
    _drive(capsys, root)
    assert launcher.launched == ["a", "b"], launcher.launched
    assert _lifecycles(store)["t"] is BLOCKED


def test_a_contract_given_to_the_controller_is_the_one_it_continues_under(tmp_path):
    """`autonomy=` is the explicit form of the registered file: a project that registered nothing continues
    under the contract it is handed."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _push(), _node("i1")], register=False)
    result = ProjectController(store, launcher, Gates(), autonomy=contract).run()
    assert launcher.launched == ["i1"] and result.halt is HaltClass.BLOCKED_EXTERNAL
    assert ProjectController(store, launcher, Gates()).autonomy is None


# --- A: authority ----------------------------------------------------------------------------

def test_a_master_that_lost_the_lease_before_a_pass_writes_and_dispatches_nothing(tmp_path):
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")])

    class Superseded(ProjectController):
        def _drive(self):
            result = super()._drive()
            if self._allowed is None:                 # a successor takes the lease once the drive ended
                self.authority.release(reason="test: handed over")
                MasterAuthority(store.dir, "sB", serialize=lambda: write_lock(store)).acquire(
                    program_generation=0, note="successor")
            return result

    authority = MasterAuthority(store.dir, "sA", serialize=lambda: write_lock(store))
    authority.acquire(program_generation=0, note="the old master")
    result = Superseded(store, launcher, Gates(), authority=authority).run()
    assert result.halt is HaltClass.SUPERSEDED and launcher.launched == []
    assert not (store.dir / governance.POLICY_DIR).exists()
    assert not (store.dir / governance.ROUTINGS_DIR).exists()
    with pytest.raises(StaleMaster):
        authority.check()


# --- F: a registered contract that cannot be used --------------------------------------------

@pytest.mark.parametrize("damage", ["garbage", "directory", "incoherent", "fifo", "dangling link"])
def test_a_registered_contract_that_cannot_be_loaded_is_a_refusal_not_no_contract(tmp_path, capsys, damage):
    root, store, launcher, contract = _world(tmp_path, [_base(), _push(), _node("i1")])
    target = store.dir / AUTONOMY_FILE
    if damage == "garbage":
        target.write_text("{", encoding="utf-8")
    elif damage == "directory":
        target.unlink()
        target.mkdir()
    elif damage == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif damage == "dangling link":
        target.unlink()
        target.symlink_to(tmp_path / "nowhere.json")
    else:
        target.write_text(contract.model_copy(update={"needs_you": NeedsYou.ON}).model_dump_json(),
                          encoding="utf-8")
    code, out = _drive(capsys, root)
    assert code == 1 and out["halt"] == HaltClass.AMBIGUOUS.value
    assert "autonomy contract refused" in out["reason"]
    assert launcher.launched == []
    with pytest.raises(ValueError, match="autonomy contract refused"):
        ProjectController(store, launcher, Gates())


# --- L: the launcher pins the runs it starts -------------------------------------------------

class _Spy(HohRunLauncher):
    def __init__(self, root, repo, **kw):
        super().__init__(root, repo, **kw)
        self.calls: list[list[str]] = []
        self.answer = Dispatch(0, "", "")

    def _run_cli(self, argv):
        self.calls.append(list(argv))
        return self.answer


def _spy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return _Spy(tmp_path / "root", repo)


def _run_calls(launcher):
    return [c for c in launcher.calls if "run" in c]


def test_a_launcher_bound_to_a_project_with_a_contract_gives_it_to_every_hoh_run(tmp_path):
    launcher = _spy(tmp_path)
    project = tmp_path / "root" / "projects" / "p"
    project.mkdir(parents=True)
    (project / AUTONOMY_FILE).write_text(_contract().model_dump_json(indent=2), encoding="utf-8")
    launcher.bind_routing(project)
    launcher.launch(_node("n1"))
    [argv] = _run_calls(launcher)
    assert argv[argv.index("--autonomy") + 1] == str(project / AUTONOMY_FILE)
    other = tmp_path / "root" / "projects" / "q"
    other.mkdir()
    launcher.bind_routing(other)                       # a project that registers none is not carried over
    launcher.launch(_node("n2"))
    assert "--autonomy" not in _run_calls(launcher)[1]


def test_a_launcher_whose_project_registers_no_contract_starts_runs_as_before(tmp_path):
    launcher = _spy(tmp_path)
    project = tmp_path / "root" / "projects" / "p"
    project.mkdir(parents=True)
    launcher.bind_routing(project)
    launcher.launch(_node("n1"))
    assert "--autonomy" not in _run_calls(launcher)[0]


@pytest.mark.parametrize("damage", ["garbage", "directory", "dangling link"])
def test_a_registered_contract_that_cannot_be_loaded_starts_no_run(tmp_path, damage):
    launcher = _spy(tmp_path)
    project = tmp_path / "root" / "projects" / "p"
    project.mkdir(parents=True)
    if damage == "garbage":
        (project / AUTONOMY_FILE).write_text("{", encoding="utf-8")
    elif damage == "directory":
        (project / AUTONOMY_FILE).mkdir()
    else:
        (project / AUTONOMY_FILE).symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(ValueError, match="autonomy contract refused"):
        launcher.bind_routing(project)
    outcome = launcher.launch(_node("n1"))
    assert outcome.verdict is RunVerdict.UNDETERMINED and "nothing is started under it" in outcome.detail
    assert _run_calls(launcher) == []


def test_a_hoh_run_that_refuses_the_contract_is_no_rejection_of_the_node(tmp_path):
    launcher = _spy(tmp_path)
    project = tmp_path / "root" / "projects" / "p"
    project.mkdir(parents=True)
    (project / AUTONOMY_FILE).write_text(_contract().model_dump_json(indent=2), encoding="utf-8")
    launcher.bind_routing(project)
    launcher.answer = Dispatch(2, "", "autonomy contract refused: this run is pinned to contract x\n")
    outcome = launcher.launch(_node("n1"))
    assert outcome.verdict is RunVerdict.UNDETERMINED
    assert outcome.detail.startswith("autonomy contract refused")


def test_a_failure_of_hoh_run_that_only_says_the_words_is_no_autonomy_refusal(tmp_path):
    """Only an exit 2 is `hoh run` refusing the contract; the same words at another exit code are read by
    the ordinary verdict of the run."""
    launcher = _spy(tmp_path)
    project = tmp_path / "root" / "projects" / "p"
    project.mkdir(parents=True)
    (project / AUTONOMY_FILE).write_text(_contract().model_dump_json(indent=2), encoding="utf-8")
    launcher.bind_routing(project)
    launcher.answer = Dispatch(1, "", "autonomy contract refused: said in passing\n")
    outcome = launcher.launch(_node("n1"))
    assert not outcome.detail.startswith("autonomy contract refused"), outcome


# --- R: the register ---------------------------------------------------------------------------

def test_the_register_row_of_this_slice_names_the_probe_the_matrix_reads():
    """The row the slice registers is part of the slice (R-1): a change that leaves it out, or registers it
    with an equivalent, does not close P3-30. The symbol and the test must exist."""
    register = json.loads((HERE.parent / "program" / "v3_3" / "REQUIREMENTS.json").read_text(encoding="utf-8"))
    row = next(r for r in register["requirements"] if r["id"] == "P3-30")
    probe = row["probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "_continue_beside_parked", "orchestrator.py")
    assert "equivalent" not in probe and row["previous_probe"]["kind"] == "none"
    assert probe["test"] == ("tests/test_parked_continuation.py::"
                             "test_hoh_project_drive_continues_beside_a_branch_the_contract_parked")
    assert callable(getattr(ProjectController, probe["symbol"])) and hasattr(
        sys.modules[__name__], probe["test"].split("::")[1])


# =================================================================================================
# P3-31 slice 2: the resolver. The routed EXTERNAL decisions a contract has pre-authorised are answered
# inside the pass (create-only, digest-bound, counted against horizon.max_external_actions); nothing is
# executed. The same section holds the two nodes that prove the modes of P3-28/29 through the pass.
# =================================================================================================

def _authorising(external=1, **over) -> AutonomyContract:
    """The `_contract()` world with `horizon.max_external_actions = external` (the number of external
    actions the operator pre-authorises) and any other field replaced."""
    contract_id = over.pop("contract_id", "c-park")
    body = _contract(concurrency=over.pop("concurrency", 4), contract_id=contract_id).model_dump(mode="json")
    body["horizon"]["max_external_actions"] = external
    body.update(over)
    return AutonomyContract.model_validate(body)


def _resolutions(store) -> dict:
    """The resolution records of the project that are ours (a regular `.json` file holding an object), by
    file name; what a test damaged on purpose is not read."""
    found = {}
    for p in _records(store, governance.RESOLUTIONS_DIR):
        if p.suffix != ".json" or p.is_symlink() or not stat.S_ISREG(os.lstat(p).st_mode):
            continue
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(body, dict):
            found[p.name] = body
    return found


def _route(store, root, contract):
    return governance.route_project_decisions(store, contract, root)


def _resolve(store, root, contract, **kw):
    return governance.resolve_routed_decisions(store, contract, root, **kw)


def _rows(report) -> dict:
    return {d.node_id: (d.outcome, d.reason) for d in report.decisions}


def _routed_world(tmp_path, nodes, contract):
    """The project with its decisions routed (not yet answered)."""
    root, store, _, contract = _world(tmp_path, nodes, contract=contract, pinned=())
    _route(store, root, contract)
    return root, store, contract


# --- S: what the resolver answers ------------------------------------------------------------

def test_a_routed_external_decision_inside_the_preauthorization_is_answered_and_recorded(tmp_path):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    report = _resolve(store, root, contract)
    assert _rows(report) == {"push": (governance.ANSWERED, "")} and report.decisions[0].recorded
    [(name, body)] = _resolutions(store).items()
    digest = report.decisions[0].decision_digest
    assert name == f"{digest}-{contract.digest()}.json"
    assert body == {
        "schema": "hoh-decision-resolution/1", "project_id": "p", "node_id": "push", "decision_digest": digest,
        "kind": "external", "disposition": "ANSWERED_BY_POLICY", "answer": "WITHIN_PREAUTHORIZATION",
        "rule": "horizon.max_external_actions", "routing": name, "escalation": "SYSTEM_TWO",
        "resolver": "SYSTEM_TWO", "approves": False, "executes": False, "contract_id": "c-park",
        "contract_digest": contract.digest(), "mode": "UNATTENDED_LONG_HORIZON", "program_epoch": 0}
    path = store.dir / governance.RESOLUTIONS_DIR / name
    assert not any(h.value in path.read_text(encoding="ascii") for h in governance.Handling), \
        "no word an existing reader reads as a verdict"
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700
    assert _lifecycles(store)["push"] is READY, "an answer launches, unblocks and settles nothing"
    before = _tree(path.parent)
    again = _resolve(store, root, contract)
    assert _rows(again) == {"push": (governance.ANSWERED, "")} and not again.decisions[0].recorded
    assert _tree(path.parent) == before


@pytest.mark.parametrize("external", [None, 0])
def test_a_contract_that_pre_authorises_no_external_action_answers_nothing(tmp_path, external):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(external))
    report = _resolve(store, root, contract)
    assert _rows(report) == {"push": (governance.LEFT_OPEN, governance.NO_PREAUTHORIZATION)}
    assert not report.failed and not (store.dir / governance.RESOLUTIONS_DIR).exists()


def test_the_preauthorization_is_a_count_and_the_next_decision_is_left_open_when_it_is_spent(tmp_path):
    two = [_base(), _push("push"), _push("push2")]
    root, store, contract = _routed_world(tmp_path / "one", two, _authorising(1))
    report = _resolve(store, root, contract)
    assert _rows(report) == {"push": (governance.ANSWERED, ""),
                             "push2": (governance.LEFT_OPEN, governance.PREAUTHORIZATION_SPENT)}
    assert len(_resolutions(store)) == 1
    assert _rows(_resolve(store, root, contract)) == _rows(report), "a further call changes nothing"
    assert len(_resolutions(store)) == 1
    root, store, both = _routed_world(tmp_path / "two", two, _authorising(2))
    assert set(_rows(_resolve(store, root, both)).values()) == {(governance.ANSWERED, "")}
    assert len(_resolutions(store)) == 2


def test_the_count_belongs_to_the_programme_epoch_not_to_one_contract_file(tmp_path):
    """A second contract of the same epoch (another id, so another digest) shares the count; another epoch
    does not. Catches a count taken per contract digest, which a changed id would reset."""
    two = [_base(), _push("push"), _push("push2")]
    root, store, first = _routed_world(tmp_path, two, _authorising(1))
    _resolve(store, root, first)
    second = _authorising(1, contract_id="c-other")
    assert second.digest() != first.digest() and second.program_epoch == first.program_epoch
    _route(store, root, second)
    spent = (governance.LEFT_OPEN, governance.PREAUTHORIZATION_SPENT)
    assert _rows(_resolve(store, root, second)) == {"push": spent, "push2": spent}, "the count is the epoch's"
    later = _authorising(1, contract_id="c-later").pin(7)
    _route(store, root, later)
    assert _rows(_resolve(store, root, later))["push"] == (governance.ANSWERED, "")
    assert sorted(b["program_epoch"] for b in _resolutions(store).values()) == [0, 7]


def test_only_a_routed_external_decision_is_answered_never_a_branch_a_person_or_the_contract_holds(tmp_path):
    """The branches that stay in front of a person: a person's block, a branch the contract parked or ended.
    Catches a resolver that answers every open decision it is shown."""
    held = [_node("h", BLOCKED, blocked_kind="human", note="needs a person"),
            _node("p", BLOCKED, blocked_kind="parked_by_contract", note="parked"),
            _node("e", BLOCKED, blocked_kind="contract_abort", note="ended")]
    root, store, contract = _routed_world(tmp_path, [_base(), *held, _push(), _node("i1")], _authorising(5))
    rows = _rows(_resolve(store, root, contract))
    assert rows["push"] == (governance.ANSWERED, "")
    assert {k: rows[k] for k in "hp"} == {k: (governance.LEFT_OPEN, governance.NOT_EXTERNAL_ACTION) for k in "hp"}
    assert "e" not in rows, "a branch the contract ended is no open decision at all"
    assert [b["node_id"] for b in _resolutions(store).values()] == ["push"]


def test_a_decision_nobody_routed_is_not_answered_and_neither_is_one_whose_routing_was_changed(tmp_path):
    root, store, _, contract = _world(tmp_path, [_base(), _push(), _node("i1")], contract=_authorising(1),
                                      pinned=())
    assert _rows(_resolve(store, root, contract)) == {"push": (governance.LEFT_OPEN, governance.NOT_ROUTED)}
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()
    _route(store, root, contract)
    [routing] = _records(store, governance.ROUTINGS_DIR)
    routing.write_bytes(routing.read_bytes().replace(b'"approves": false', b'"approves": true'))
    assert _rows(_resolve(store, root, contract)) == {"push": (governance.LEFT_OPEN, governance.NOT_ROUTED)}
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


@pytest.mark.parametrize("change", ["gate", "not allowed"])
def test_a_contract_that_does_not_hand_the_decision_to_a_resolver_answers_nothing(tmp_path, change):
    """Routed under one contract, offered the resolver under another that no longer hands the decision over:
    the answer is the contract's decided route to a person, so nothing is recorded."""
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    if change == "gate":
        other = _authorising(1, hard_human_gates=("EXTERNAL",), contract_id="c-gate")
    else:
        other = _authorising(1, allowed_action_classes=("INTERNAL",), contract_id="c-none")
    assert _rows(_resolve(store, root, other)) == {"push": (governance.LEFT_OPEN, governance.NOT_COVERED)}
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


# --- X: a directory of resolutions that cannot be trusted answers nothing ---------------------

_STRAY = "a" * 16 + "-" + "b" * 16 + ".json"


def _damage(directory: Path, how: str) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / _STRAY
    shape = {"schema": "hoh-decision-resolution/1", "project_id": "p", "decision_digest": "a" * 16,
             "contract_digest": "b" * 16, "program_epoch": 0}
    if how == "garbage":
        target.write_bytes(b"not json")
    elif how == "not an object":
        target.write_bytes(b"[1, 2]\n")
    elif how == "wrong project":
        target.write_bytes(json.dumps({**shape, "project_id": "other"}).encode())
    elif how == "wrong schema":
        target.write_bytes(json.dumps({**shape, "schema": "hoh-decision-resolution/2"}).encode())
    elif how == "name differs from digests":
        target.write_bytes(json.dumps({**shape, "decision_digest": "0" * 16}).encode())
    elif how == "epoch not a number":
        target.write_bytes(json.dumps({**shape, "program_epoch": "0"}).encode())
    elif how == "oversize":
        target.write_bytes(b"x" * (governance.MAX_RESOLUTION_RECORD_BYTES + 1))
    elif how == "link":
        (directory / "elsewhere").write_text(json.dumps(shape), encoding="utf-8")
        target.symlink_to("elsewhere")
    elif how == "fifo":
        os.mkfifo(target)
    elif how == "hard link":
        (directory / "other-name").write_text(json.dumps(shape), encoding="utf-8")
        os.link(directory / "other-name", target)
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["garbage", "not an object", "wrong project", "wrong schema",
                                 "name differs from digests", "epoch not a number", "oversize", "link", "fifo", "hard link"])
def test_a_resolution_directory_that_cannot_be_read_answers_nothing_and_says_so(tmp_path, how):
    """A count taken from part of a directory is a count a damaged file can lower: nothing is answered."""
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    _damage(store.dir / governance.RESOLUTIONS_DIR, how)
    report = _resolve(store, root, contract)
    assert _rows(report) == {"push": (governance.LEFT_OPEN, governance.RESOLUTIONS_UNREADABLE)}
    assert report.failed
    assert not [b for b in _resolutions(store).values() if b.get("node_id") == "push"]


@pytest.mark.parametrize("how", ["a file", "a link to a directory"])
def test_a_resolution_path_that_is_not_a_directory_of_its_own_answers_nothing(tmp_path, how):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    path = store.dir / governance.RESOLUTIONS_DIR
    if how == "a file":
        path.write_text("not a directory", encoding="utf-8")
    else:
        (tmp_path / "elsewhere").mkdir()
        path.symlink_to(tmp_path / "elsewhere")
    report = _resolve(store, root, contract)
    assert _rows(report) == {"push": (governance.LEFT_OPEN, governance.RESOLUTIONS_UNREADABLE)} and report.failed
    if how != "a file":
        assert not list((tmp_path / "elsewhere").iterdir()), "nothing is written through a link"


def _stray_record(directory: Path, size: int | None = None) -> None:
    """A well-formed record of another decision (it counts for the epoch), padded to `size` bytes."""
    directory.mkdir(mode=0o700, exist_ok=True)
    body = json.dumps({"schema": "hoh-decision-resolution/1", "project_id": "p", "decision_digest": "a" * 16,
                       "contract_digest": "b" * 16, "program_epoch": 0}).encode()
    (directory / _STRAY).write_bytes(body if size is None else body + b" " * (size - len(body)))


@pytest.mark.parametrize("extra, expected", [(0, "PREAUTHORIZATION_SPENT"), (1, "RESOLUTIONS_UNREADABLE")])
def test_a_resolution_record_of_exactly_the_largest_size_is_read_and_one_byte_more_is_not(tmp_path, extra,
                                                                                         expected):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    assert governance.MAX_RESOLUTION_RECORD_BYTES == 16384
    _stray_record(store.dir / governance.RESOLUTIONS_DIR, 16384 + extra)
    assert _rows(_resolve(store, root, contract)) == {"push": (governance.LEFT_OPEN, expected)}


def test_a_directory_of_more_resolution_records_than_the_ceiling_is_not_read(tmp_path, monkeypatch):
    root, store, contract = _routed_world(tmp_path, [_base(), _push("push"), _push("push2")], _authorising(2))
    assert _rows(_resolve(store, root, contract)) == {"push": (governance.ANSWERED, ""),
                                                      "push2": (governance.ANSWERED, "")}
    monkeypatch.setattr(governance, "MAX_RESOLUTION_RECORDS", 1)
    assert set(_rows(_resolve(store, root, contract)).values()) == {
        (governance.LEFT_OPEN, governance.RESOLUTIONS_UNREADABLE)}
    monkeypatch.setattr(governance, "MAX_RESOLUTION_RECORDS", 2)
    assert set(_rows(_resolve(store, root, contract)).values()) == {(governance.ANSWERED, "")}


def test_a_name_that_is_not_a_record_is_not_one_and_a_different_record_for_the_decision_is_a_conflict(tmp_path):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(2))
    directory = store.dir / governance.RESOLUTIONS_DIR
    directory.mkdir(mode=0o700)
    (directory / "notes.txt").write_text("not a record", encoding="utf-8")
    assert _rows(_resolve(store, root, contract)) == {"push": (governance.ANSWERED, "")}
    [path] = [p for p in _records(store, governance.RESOLUTIONS_DIR) if p.suffix == ".json"]
    body = json.loads(path.read_text(encoding="ascii"))
    body["rule"] = "something else"
    path.write_text(json.dumps(body, sort_keys=True, indent=1) + "\n", encoding="ascii")
    again = _resolve(store, root, contract)
    assert _rows(again) == {"push": (governance.LEFT_OPEN, governance.RECORD_CONFLICT)} and again.failed


def test_a_dry_run_writes_nothing_and_a_refused_call_reads_nothing(tmp_path):
    root, store, contract = _routed_world(tmp_path, [_base(), _push(), _node("i1")], _authorising(1))
    report = _resolve(store, root, contract, dry_run=True)
    assert _rows(report) == {"push": (governance.ANSWERED, "")} and not report.decisions[0].recorded
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()
    for bad in (None, "contract", 3):
        with pytest.raises(governance.RoutingRefused):
            governance.resolve_routed_decisions(store, bad, root)
    for bad_root in (tmp_path / "nowhere", root / "projects" / "p" / "project.json", 7):
        with pytest.raises(governance.RoutingRefused):
            governance.resolve_routed_decisions(store, contract, bad_root)
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


# --- L: the resolver in the loop's pass ---------------------------------------------------------

def test_hoh_project_drive_answers_the_routed_decision_inside_the_pass(tmp_path, capsys):
    """Through the product's command. The external action at the head is routed, answered (the contract
    pre-authorises one), and the independent node still runs; the action itself is not launched, unblocked or
    settled, and the answer exists before anything is dispatched."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _push(), _node("i1")],
                                             contract=_authorising(1))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["i1"] and launcher.merged == ["i1"], out
    assert _lifecycles(store) == {"base": MERGED, "push": READY, "i1": MERGED}
    assert out["halt"] == HaltClass.BLOCKED_EXTERNAL.value and code == 1
    kinds = [s.split(":")[0] for s in out["steps"]]
    assert kinds.index("RESOLUTION_ANSWERED") < kinds.index("CONTINUATION") < kinds.index("MERGED"), kinds
    [(name, body)] = _resolutions(store).items()
    assert (body["node_id"], body["answer"], body["approves"], body["executes"]) == (
        "push", "WITHIN_PREAUTHORIZATION", False, False)
    assert name == _records(store, governance.ROUTINGS_DIR)[0].name, "answered from the routing of the same pass"
    assert _wave_of(store)["continued"] == ["i1"] and _wave_of(store)["approves"] is False
    assert "RESOLUTION_ANSWERED:push" in out["steps"]


def test_a_contract_without_a_preauthorization_drives_as_before(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")], contract=_authorising(None))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["i1"] and code == 1
    assert not [s for s in out["steps"] if s.startswith("RESOLUTION")]
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


def test_a_further_drive_over_the_same_state_answers_nothing_new_and_writes_nothing(tmp_path, capsys):
    """Terminal records are quiet: the state version was decided, so the resolver is not asked again."""
    root, store, launcher, _ = _world(tmp_path, [_push(), _node("i1")], contract=_authorising(1), pinned=())
    _drive(capsys, root)
    assert len(_resolutions(store)) == 1 and launcher.launched == [], "answered, and the hold dispatched nothing"
    kept = {d: _tree(store.dir / d) for d in (governance.RESOLUTIONS_DIR, governance.ROUTINGS_DIR,
                                              governance.POLICY_DIR)}
    state_before = (store.dir / "project.json").read_bytes()
    code, again = _drive(capsys, root)
    assert [s for s in again["steps"] if s.startswith("CONTINUATION_DECIDED")], again["steps"]
    assert not [s for s in again["steps"] if s.startswith("RESOLUTION")]
    assert (store.dir / "project.json").read_bytes() == state_before
    assert {d: _tree(store.dir / d) for d in kept} == kept


def test_the_pass_answers_the_open_decisions_in_the_order_the_frontier_ranks_them(tmp_path, capsys):
    """One pre-authorisation, three external decisions: the one that frees work behind it ranks first
    (`derive_frontier`), so it is the one answered -- not the first or the last in the graph, and not the first
    or the last by name (it is in the middle of both)."""
    nodes = [_base(), _push("first"), _push("second"), _push("third"),
             _node("behind", dependencies=["second"]), _node("i1")]
    root, store, launcher, _ = _world(tmp_path, nodes, contract=_authorising(1))
    _, out = _drive(capsys, root)
    assert [b["node_id"] for b in _resolutions(store).values()] == ["second"], _resolutions(store)
    assert [s for s in out["steps"] if s.startswith("RESOLUTION")] == ["RESOLUTION_ANSWERED:second"]


def test_a_branch_the_contract_parked_gets_no_answer_while_the_external_action_beside_it_does(tmp_path, capsys):
    """The branch a run parked stays in front of a person; the pre-authorised action beside it is answered."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _push(), _node("b")],
                                      script={"a": "park"}, contract=_authorising(1))
    _drive(capsys, root)
    assert _lifecycles(store)["a"] is BLOCKED and "b" in launcher.merged
    assert [b["node_id"] for b in _resolutions(store).values()] == ["push"]


def test_a_resolver_that_cannot_record_is_said_and_does_not_hold_the_independent_work_back(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")], contract=_authorising(1))
    (store.dir / governance.RESOLUTIONS_DIR).write_text("not a directory", encoding="utf-8")
    code, out = _drive(capsys, root)
    assert launcher.merged == ["i1"] and code == 1
    assert [s for s in out["steps"] if s.startswith("RESOLUTION")] == ["RESOLUTION_FAILED:push"]
    assert (store.dir / governance.RESOLUTIONS_DIR).read_text(encoding="utf-8") == "not a directory"


# --- M: the modes and the review, through the pass ------------------------------------------------

def _mode_contract(mode, **over) -> AutonomyContract:
    body = _contract().model_dump(mode="json")
    body.update(mode=mode, contract_id=f"c-{mode.lower()}", **over)
    return AutonomyContract.model_validate(body)


def test_the_mode_decides_how_many_nodes_the_loop_continues_in_a_pass(tmp_path, capsys):
    """P3-28: the contract's concurrency through the mode's row, read from the records the loop wrote. Under
    SUPERVISED one node carries on per pass (the captain steers: three passes for three nodes), under
    PLAN_AUTONOMOUS all three carry on in one."""
    nodes = [_base(), _push(), _node("i1"), _node("i2"), _node("i3")]
    supervised = _mode_contract("SUPERVISED", needs_you="ON", release_policy="HUMAN_APPROVAL")
    root, store, launcher, _ = _world(tmp_path / "s", nodes, contract=supervised)
    _drive(capsys, root)
    first = _waves_of(store)[0]
    assert first["continued"] == ["i1"] and first["mode_wave"]["mode"] == "SUPERVISED"
    assert [(w["node"], w["reason"]) for w in first["withheld"]] == [("i2", "CONCURRENCY_CAP"),
                                                                    ("i3", "CONCURRENCY_CAP")]
    assert len(_waves_of(store)) == 3 and launcher.merged == ["i1", "i2", "i3"]
    plan = _mode_contract("PLAN_AUTONOMOUS", needs_you="DIGEST_ONLY", release_policy="AUTO_PREPARE_ONLY")
    root, store, launcher, _ = _world(tmp_path / "p", nodes, contract=plan)
    _drive(capsys, root)
    [only] = _waves_of(store)
    assert only["continued"] == ["i1", "i2", "i3"] and only["mode_wave"]["width"] == 4


def test_a_review_that_the_mode_armed_falls_due_and_the_loop_decides_again_and_fires_it(tmp_path, capsys,
                                                                                       monkeypatch):
    """P3-29 through the pass, with the timer the mode itself armed. The first hold arms a REVIEW_DUE timer
    in the runs root; when it falls due while the state stands still the next drive names the wave after it,
    decides again (the record says the review is due and a person is asked) and fires the timer once; the
    drive after that is the quiet one."""
    from hoh.timers import TimerStatus, TimerStore

    root, store, launcher, contract = _world(tmp_path, [_push(), _node("i1")], pinned=())
    _drive(capsys, root)
    [armed] = TimerStore(root).read()
    assert armed.status is TimerStatus.ARMED and armed.due_at == "2026-10-18T12:15:00Z"
    assert [w["mode_wave"]["review"]["state"] for w in _waves_of(store)] == ["ARMED"]
    _drive(capsys, root)
    assert len(_waves_of(store)) == 1, "not yet due: the state version was decided"
    monkeypatch.setattr(governance, "utcnow", lambda: "2026-10-18T13:00:00Z")
    _, due = _drive(capsys, root)
    assert len(_waves_of(store)) == 2
    [review] = [w["mode_wave"]["review"] for w in _waves_of(store) if w["wave_id"].endswith(armed.timer_id)]
    assert (review["state"], review["timer"], review["needs_you"]) == ("DUE", armed.timer_id, True)
    assert [t.status for t in TimerStore(root).read()] == [TimerStatus.FIRED]
    assert [s for s in due["steps"] if s.startswith("CONTINUATION:")]
    kept = _tree(store.dir / governance.POLICY_DIR)
    _, quiet = _drive(capsys, root)
    assert [s for s in quiet["steps"] if s.startswith("CONTINUATION_DECIDED")]
    assert _tree(store.dir / governance.POLICY_DIR) == kept and launcher.launched == []


def test_a_node_id_a_record_cannot_name_is_not_answered(tmp_path):
    """The record names the node; an id outside the grammar cannot be named and is a failure, not a quiet
    refusal (without the guard the reason would only be NOT_ROUTED)."""
    root, store, contract = _routed_world(tmp_path, [_base(), _push("bad id"), _node("i1")], _authorising(1))
    report = _resolve(store, root, contract)
    assert _rows(report) == {"bad id": (governance.LEFT_OPEN, governance.NODE_NOT_NAMEABLE)} and report.failed
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


def test_an_answer_that_was_recorded_is_said_even_when_the_decision_of_the_wave_refuses(tmp_path, capsys):
    """The counter of the pre-authorisation is spent when the record is created. If what follows (the wave's own
    record cannot be written) ends the pass, the step that says so is still in the drive's answer."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1")], contract=_authorising(1))
    (store.dir / governance.POLICY_DIR).write_text("not a directory", encoding="utf-8")
    code, out = _drive(capsys, root)
    assert len(_resolutions(store)) == 1 and launcher.launched == []
    kinds = [s.split(":")[0] for s in out["steps"]]
    assert kinds.index("RESOLUTION_ANSWERED") < kinds.index("CONTINUATION_REFUSED"), kinds


def test_one_answer_is_said_once_however_many_passes_the_drive_makes(tmp_path, capsys):
    """Three passes over one answered decision: one record, one step. Catches a step said for every pass in
    which the (old) answer is found again."""
    horizon = {**_contract().model_dump(mode="json")["horizon"], "max_external_actions": 1}
    supervised = _mode_contract("SUPERVISED", needs_you="ON", release_policy="HUMAN_APPROVAL", horizon=horizon)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("i1"), _node("i2"), _node("i3")],
                                      contract=supervised)
    _, out = _drive(capsys, root)
    assert len(_waves_of(store)) == 3 and launcher.merged == ["i1", "i2", "i3"], "three passes"
    assert [s for s in out["steps"] if s.startswith("RESOLUTION")] == ["RESOLUTION_ANSWERED:push"]
    assert len(_resolutions(store)) == 1


def test_a_review_is_named_after_this_projects_earliest_due_timer_and_no_other(tmp_path, capsys):
    """The runs root is shared: the timer file holds other projects' and other programme epochs' reviews. Only this
    project's, at this contract's epoch, rename the wave -- the earliest first -- and only those are fired."""
    from hoh.timers import TimerKind, TimerStatus, TimerStore

    root, store, launcher, contract = _world(tmp_path, [_push(), _node("i1")], pinned=())
    _drive(capsys, root)                                    # the first hold arms this project's own review
    timers = TimerStore(root)
    other_project = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2025-12-01T00:00:00Z", program_id="q")
    other_epoch = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2025-12-02T00:00:00Z", program_id="p",
                                 program_epoch=3)
    late = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-02-01T00:00:00Z", program_id="p")
    early = timers.arm_new(kind=TimerKind.REVIEW_DUE, due_at="2026-01-15T00:00:00Z", program_id="p")
    assert governance.due_reviews(contract, root, "p") == (early.timer_id, late.timer_id)
    assert governance.due_reviews(contract, root, "nobody") == ()
    _drive(capsys, root)
    names = sorted(p.name for p in _records(store, governance.POLICY_DIR))
    assert len(names) == 2 and sum(n.endswith(f"-{early.timer_id}.json") for n in names) == 1, names
    status = {t.timer_id: t.status for t in TimerStore(root).read()}
    assert status[early.timer_id] is TimerStatus.FIRED and status[late.timer_id] is TimerStatus.FIRED
    assert status[other_project.timer_id] is TimerStatus.ARMED and status[other_epoch.timer_id] is TimerStatus.ARMED


# --- R: the register ---------------------------------------------------------------------------------

def test_the_register_rows_of_the_resolver_slice_name_the_probes_the_matrix_reads():
    """P3-27/28/29/31/32 as this slice leaves them (R-1): the symbols and the nodes exist, the rows that are
    satisfied carry no equivalent, the one that is not carries an exact one."""
    register = json.loads((HERE.parent / "program" / "v3_3" / "REQUIREMENTS.json").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    expect = {"P3-27": ("derive_frontier", "decisionfrontier.py"), "P3-28": ("mode_width", "governance.py"),
              "P3-29": ("apply_mode", "governance.py"), "P3-31": ("resolve_routed_decisions", "governance.py")}
    for row_id, (symbol, module) in expect.items():
        probe = rows[row_id]["probe"]
        assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", symbol, module), row_id
        path, node = probe["test"].split("::")
        assert path == "tests/test_parked_continuation.py" and hasattr(sys.modules[__name__], node), row_id
        assert "equivalent" not in probe, row_id
        assert rows[row_id]["previous_probe"]["replaced_on"] == "2026-10-19", row_id
    # P3-32 moved on in slices 3 and 4 (tests/test_limit_policies.py and tests/test_external_executor.py read its
    # row); what slice 2 left is kept by addition, two probes down
    assert "REPLAN" in rows["P3-32"]["previous_probe"]["previous_probe"]["equivalent"]
    assert callable(governance.resolve_routed_decisions)
    note31, note29 = rows["P3-31"]["probe"]["note"], rows["P3-29"]["probe"]["note"]
    assert "per programme epoch" in note31 and "no independent READY work" in note31 and "release_policy" in note31
    assert "nothing reads that flag yet" in note29, "the earlier note's limit is carried, not dropped"
    assert "not executed" in rows["P3-32"]["previous_probe"]["equivalent"]
