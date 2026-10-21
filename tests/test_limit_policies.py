"""P3-32 slice 3: the loop acts on the limit policies DEGRADE and REPLAN.

`governance.continue_by_policy` has recorded a reached limit since slice 1: HOLD `LIMIT_REACHED`, carrying
the contract's `horizon.on_limit`. Of the four policies (REPLAN, DEGRADE, PARK, STOP) the loop's pass
(`ProjectController._continue_beside_parked`, P3-30 slice 2) now acts on two, after the hold is written and
before anything is dispatched (`governance.act_on_limit`):

* DEGRADE: the wave continues at width one instead of the mode's, at most `MAX_DEGRADED_WAVES` times per
  contract, each one recorded (`limit-acts/<wave>.degrade.json`) before its node is dispatched;
* REPLAN: nothing continues but one replanning step -- a node `replan-<contract digest>` the pass adds to
  the graph, with its specification, and dispatches in the next wave; then the limit holds again.

PARK and STOP are the hold itself and are not acted on. `hoh project continue` records the hold and acts on
nothing, whatever the policy. An answered external decision stays what it was: an answer that approves and
executes nothing (a stated limit of the slice, not a gap).

What is real: `cli.main` (`hoh project drive`), the registered driver, `drive_governed`, `ProjectController`,
`governance`, `bind` and `Governor`. What is replaced is the process boundary: the launcher is a fake that
"runs" a node by returning a verdict; one test uses the real `HohRunLauncher.prepare` against a throw-away
git repository to show it takes the replanning node as written. A test reads nothing outside `tmp_path` and
this directory (the driver factory is imported from here, as `driver.json` registers it) and never removes
anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hoh import cli, governance, orchestrator
from hoh.autonomy import (
    AutonomyContract, AutonomyMode, Escalation, Horizon, NeedsYou, OnLimit, ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.governance import Governor, bind
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import (
    GateRunner, HaltClass, MergeFailure, MergeResult, ProjectController, RunOutcome, RunVerdict,
)
from hoh.project import ActionClass, DecisionKind, GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

READY, BLOCKED, MERGED = Lifecycle.READY, Lifecycle.BLOCKED, Lifecycle.MERGED
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL
NOW = "2026-10-18T12:00:00Z"
CREATED = "2026-10-18T11:50:00Z"
HERE = Path(__file__).resolve().parent
AUTONOMY_FILE = "autonomy-contract.json"
#: the driver factory reads its world from here (the module is imported by name through `driver.json`)
CONFIG: dict = {}
#: the three numbers of the slice, spelled out here: they are part of the contract (a test of the allowance
#: reads them from the product as well)
DEGRADED_WIDTH, MAX_DEGRADED_WAVES, CONTINUATION_PASSES = 1, 3, 3
#: the contract's dispatch ceiling in these tests; every dispatch of the fake launcher books one on the run `base`
CEILING = 10


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(governance, "utcnow", lambda: NOW)


# --- the world -------------------------------------------------------------------------------

def _contract(on_limit=OnLimit.DEGRADE, *, concurrency=4, max_dispatches=10,
              contract_id="c-limit") -> AutonomyContract:
    """UNATTENDED_LONG_HORIZON: a decision a run cannot take parks its branch; a dispatch ceiling of
    `max_dispatches` (the tests spend all of it) whose breach is met by `on_limit`."""
    return AutonomyContract(
        contract_id=contract_id, mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.OFF,
        scope=("src/hoh/",), note="stop the whole programme is meant where the policy is STOP",
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900,
                        max_concurrency=concurrency, on_limit=on_limit),
        budgets=Budgets(max_dispatches=max_dispatches, max_wallclock_seconds=3600),
        release_policy=ReleasePolicy.NEVER_AUTO_PUBLISH,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
                    "release": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "scope": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "EXTERNAL": Escalation.SYSTEM_TWO})


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class Launcher:
    """The `RunLauncher` protocol without a process. `script[node]` is "ok" (accepts) or "park" (the run
    stops on a decision it cannot take: it writes its own pin and the contract's routing, as `hoh run`
    does, and ends blocked). `launched` is the order of every dispatch; `acts_at_launch` the act records
    that existed when a node was dispatched."""

    wave_width = 1

    def __init__(self, root: Path, contract: AutonomyContract, script=None):
        self.root, self.contract, self.script = Path(root), contract, dict(script or {})
        self.launched: list[str] = []
        self.merged: list[str] = []
        self.acts_at_launch: dict[str, list[str]] = {}

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

    def _book(self):
        """A dispatch is spent where the product reads the spend: `usage.dispatches` of a run state."""
        path = self.root / "base" / "state.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["usage"]["dispatches"] += 1
            path.write_text(json.dumps(doc), encoding="utf-8")
        except (OSError, ValueError, KeyError, TypeError):
            pass                     # a world that has no readable spend books nothing

    def launch(self, node):
        self.launched.append(node.id)
        self._book()
        acts = self.root / "projects" / "p" / governance.LIMIT_ACTS_DIR
        self.acts_at_launch[node.id] = sorted(p.name for p in acts.iterdir()) if acts.is_dir() else []
        if self.script.get(node.id, "ok") == "park":
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


def _base() -> TaskNode:
    return _node("base", MERGED, run_id="base")


def _push() -> TaskNode:
    """An EXTERNAL action at the head of the READY frontier: every drive halts there at once, so a drive
    over a state that was decided before dispatches nothing of its own."""
    return _node("push", action=EXTERNAL, note="push the tag v1 to origin", writes=[])


def _spend(root: Path, run_id: str, dispatches) -> Path:
    path = root / run_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"usage": {"dispatches": dispatches}}), encoding="utf-8")
    return path


def _mode_contract(mode, on_limit=OnLimit.DEGRADE, **over) -> AutonomyContract:
    body = _contract(on_limit).model_dump(mode="json")
    body.update(mode=mode, contract_id=f"c-{mode.lower()}", **over)
    return AutonomyContract.model_validate(body)


def _world(tmp_path, nodes, *, script=None, contract=None, spent="auto", created_at=CREATED):
    """The project `p` under `root`, its driver and contract registered, the run `base` pinned and
    `spent` dispatches booked on it (the contract's ceiling is 10)."""
    contract = contract or _contract()
    if spent == "auto":              # a run that parks is the first dispatch: it books the last one of ten
        spent = CEILING - 1 if "park" in (script or {}).values() else CEILING
    root = tmp_path / "root"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=list(nodes),
                              created_at=created_at))
    (store.dir / AUTONOMY_FILE).write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    run = root / "base"
    run.mkdir(parents=True)
    bind(run, contract=contract)
    if spent is not None:
        _spend(root, "base", spent)
    launcher = Launcher(root, contract, script)
    CONFIG.clear()
    CONFIG["launcher"] = launcher
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_limit_policies:build_driver", "path": str(HERE)}), encoding="utf-8")
    return root, store, launcher, contract


def _drive(capsys, root):
    code = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    return code, json.loads(capsys.readouterr().out)


def _lifecycles(store) -> dict:
    return {n.id: n.lifecycle for n in store.read_state().nodes}


def _files(store, directory) -> list[Path]:
    d = store.dir / directory
    return sorted(d.iterdir()) if d.is_dir() else []


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _acts(store, kind=None) -> list[dict]:
    found = [_read(p) for p in _files(store, governance.LIMIT_ACTS_DIR)
             if kind is None or p.name.endswith(f".{kind}.json")]
    return sorted(found, key=lambda r: r["state"]["write_seq"])


def _holds(store) -> list[dict]:
    return sorted((_read(p) for p in _files(store, governance.POLICY_DIR)),
                  key=lambda r: r["state"]["write_seq"])


def _steps(out, kind) -> list[str]:
    return [s for s in out["steps"] if s.split(":")[0] == kind]


def _tree(path: Path) -> dict:
    seen = {}
    for base, dirs, files in os.walk(path):
        for name in ["", *dirs, *files]:
            p = os.path.join(base, name) if name else base
            info = os.lstat(p)
            digest = hashlib.sha256(Path(p).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            seen[os.path.relpath(p, path)] = (info.st_mode, info.st_size, digest)
    return seen


def _records_tree(store) -> dict:
    """Everything a pass writes beside the state: the continuation records, the routings, the acts and the
    specifications (a drive also takes the lease, which is not a record of the pass)."""
    return {d: _tree(store.dir / d) for d in (governance.POLICY_DIR, governance.ROUTINGS_DIR,
                                              governance.LIMIT_ACTS_DIR, governance.REPLAN_SPECS_DIR)
            if (store.dir / d).exists()}


def _replan_id(contract) -> str:
    return f"replan-{contract.digest()}"


# --- D: DEGRADE ------------------------------------------------------------------------------

def test_hoh_project_drive_acts_on_the_limit_policies(tmp_path, capsys):
    """The central claim, through the product's command, both policies. A dispatch ceiling that is spent
    held every wave since slice 1. Under DEGRADE the wave now continues one node at a time; under REPLAN
    the one thing that continues is a replanning step the pass adds to the graph; under PARK nothing."""
    nodes = [_base(), _node("a"), _node("b"), _node("c")]
    # DEGRADE: width four becomes width one
    root, store, launcher, contract = _world(tmp_path / "d", nodes, script={"a": "park"})
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", "b", "c"] and launcher.merged == ["b", "c"], out
    assert [r["continued"] for r in _acts(store, "degrade")] == [["b"], ["c"]]
    assert {r["act"]["width"] for r in _acts(store, "degrade")} == {DEGRADED_WIDTH}
    # REPLAN: nothing but the replanning step runs
    root, store, launcher, contract = _world(tmp_path / "r", nodes, script={"a": "park"},
                                             contract=_contract(OnLimit.REPLAN))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", _replan_id(contract)], out
    assert _lifecycles(store) == {"base": MERGED, "a": BLOCKED, "b": READY, "c": READY,
                                  _replan_id(contract): MERGED}
    # PARK: the hold is all there is
    root, store, launcher, contract = _world(tmp_path / "p", nodes, script={"a": "park"},
                                             contract=_contract(OnLimit.PARK))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and not _files(store, governance.LIMIT_ACTS_DIR), out
    assert code == 1


def test_degrade_narrows_the_wave_to_one_node_and_records_each_wave_before_it_is_dispatched(tmp_path, capsys):
    root, store, launcher, contract = _world(
        tmp_path, [_base(), _node("a"), _node("b"), _node("c"), _node("d")], script={"a": "park"})
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", "b", "c", "d"] and launcher.merged == ["b", "c", "d"], out
    holds, acts = _holds(store), _acts(store, "degrade")
    assert len(holds) == 3 and len(acts) == 3
    for hold in holds:                                    # slice 1's record is untouched
        assert (hold["outcome"], hold["reason"], hold["limit_policy"]) == ("HOLD", "LIMIT_REACHED", "DEGRADE")
        assert hold["continued"] == [] and "act" not in hold
    assert [a["continued"] for a in acts] == [["b"], ["c"], ["d"]]
    assert [a["act"] for a in acts] == [
        {"kind": "DEGRADE", "from_width": 4, "width": 1, "degraded_wave": n, "max_degraded_waves": 3}
        for n in (1, 2, 3)]
    for hold, act in zip(holds, acts):
        assert act["wave_id"] == hold["wave_id"] and act["state"] == hold["state"]
        assert (act["outcome"], act["limit_policy"], act["approves"]) == ("CONTINUE", "DEGRADE", False)
        assert act["contract"] == hold["contract"] and act["contract"]["digest"] == contract.digest()
    # the withheld say why: width one, not independence
    assert {(w["node"], w["reason"]) for w in acts[0]["withheld"]} == {("c", "CONCURRENCY_CAP"), ("d", "CONCURRENCY_CAP")}
    # the record is the counter and exists before the dispatch it counts
    assert launcher.acts_at_launch["a"] == []
    for node, act in zip("bcd", acts):
        assert f"{act['wave_id']}.degrade.json" in launcher.acts_at_launch[node], node
    assert [s for s in out["steps"] if s.startswith("CONTINUATION_PASSES_USED")]
    assert _steps(out, "LIMIT_ACT") == ["LIMIT_ACT:DEGRADE"] * 3


def test_the_degraded_allowance_is_three_waves_per_contract_and_then_the_limit_holds(tmp_path, capsys):
    assert governance.MAX_DEGRADED_WAVES == MAX_DEGRADED_WAVES and governance.DEGRADED_WIDTH == DEGRADED_WIDTH
    nodes = [_base(), _push()] + [_node(n) for n in "bcdefg"]
    root, store, launcher, contract = _world(tmp_path, nodes)
    _drive(capsys, root)
    assert launcher.launched == ["b", "c", "d"], "three degraded waves of one node"
    code, out = _drive(capsys, root)
    assert launcher.launched == ["b", "c", "d"], "the allowance is spent: nothing is dispatched"
    [spent] = _acts(store, "none")
    assert "LIMIT_ACT:NONE:DEGRADE_SPENT" in out["steps"], "the terminal state is named in what the drive prints"
    assert spent["act"] == {"kind": "NONE", "reason": "DEGRADE_SPENT", "policy": "DEGRADE"}
    assert (spent["outcome"], spent["reason"], spent["continued"]) == ("HOLD", "LIMIT_REACHED", [])
    assert len(_acts(store, "degrade")) == 3
    assert all(_lifecycles(store)[n] is READY for n in "efg")
    # terminal and quiet: a further drive over the same state writes and dispatches nothing
    before = _records_tree(store)
    code, out = _drive(capsys, root)
    assert launcher.launched == ["b", "c", "d"] and _records_tree(store) == before
    assert _steps(out, "CONTINUATION_DECIDED") and not _steps(out, "LIMIT_ACT")


def test_a_contract_with_a_different_digest_has_its_own_allowance(tmp_path):
    """The count is by the contract digest (the programme epoch is part of it), not for the project."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("a"), _node("b")])
    acts = store.dir / governance.LIMIT_ACTS_DIR
    acts.mkdir()
    other = _contract(contract_id="c-other")
    for n in (1, 2, 3):
        (acts / f"c{n}-{other.digest()}.degrade.json").write_text("{}")
    made, why = governance._acts_made(acts, contract.digest())
    assert (made["degrade"], why) == (0, "")
    made, why = governance._acts_made(acts, other.digest())
    assert (made["degrade"], why) == (3, "")
    (acts / f"c9-{contract.digest()}-t1.degrade.json").write_text("{}")      # a wave named after a review
    (acts / f"c9-{contract.digest()}.degrade.json.tmp").write_text("{}")     # not a record's name
    (acts / f"x9-{contract.digest()}.degrade.json").write_text("{}")         # not a wave's name
    assert governance._acts_made(acts, contract.digest())[0]["degrade"] == 1


def test_what_counts_as_an_act_is_a_name_nothing_is_opened_and_the_ceiling_is_exact(tmp_path, monkeypatch):
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("a")])
    acts = tmp_path / "acts"
    acts.mkdir()
    digest = contract.digest()
    os.mkfifo(acts / f"c1-{digest}.degrade.json")                       # a FIFO is counted, never opened
    (acts / f"c2-{digest}.degrade.json").mkdir()                        # so is a directory
    (acts / f"c3-{digest}.degrade.json\n").write_text("{}")             # a name with a trailing newline is not one
    (acts / f"c4-{digest}.degrade.json ").write_text("{}")              # nor one with a trailing space
    (acts / f"c5-{digest}.DEGRADE.json").write_text("{}")               # nor one in another case
    assert governance._acts_made(acts, digest) == ({"degrade": 2, "replan": 0, "none": 0}, "")
    monkeypatch.setattr(governance, "MAX_LIMIT_ACT_ENTRIES", 5)
    assert governance._acts_made(acts, digest)[1] == "", "exactly the ceiling is read"
    monkeypatch.setattr(governance, "MAX_LIMIT_ACT_ENTRIES", 4)
    assert "more than 4 entries" in governance._acts_made(acts, digest)[1]


@pytest.mark.parametrize("on_limit", [OnLimit.DEGRADE, OnLimit.REPLAN])
def test_the_operators_command_records_the_hold_and_acts_on_nothing(tmp_path, capsys, on_limit):
    """`hoh project continue` is slice 1's command: under DEGRADE and REPLAN it holds, carries the policy and
    leaves the graph, the launches and the acts directory alone. Only the loop acts."""
    contract = _contract(on_limit)
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")], contract=contract)
    before = store.read_state().model_dump_json()
    code = cli.main(["--root", str(root), "project", "continue", "p", "--autonomy",
                     str(store.dir / AUTONOMY_FILE), "--wave", "w1"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and (doc["outcome"], doc["reason"], doc["limit_policy"]) == ("HOLD", "LIMIT_REACHED", on_limit.value)
    assert launcher.launched == [] and store.read_state().model_dump_json() == before
    assert not (store.dir / governance.LIMIT_ACTS_DIR).exists() and not (store.dir / governance.REPLAN_SPECS_DIR).exists()
    assert [p.name for p in _files(store, governance.POLICY_DIR)] == ["w1.json"]


@pytest.mark.parametrize("on_limit", [OnLimit.PARK, OnLimit.STOP])
def test_park_and_stop_are_the_hold_and_are_not_acted_on(tmp_path, capsys, on_limit):
    root, store, launcher, contract = _world(
        tmp_path, [_base(), _node("a"), _node("b"), _node("c")], script={"a": "park"},
        contract=_contract(on_limit))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1, out
    assert not (store.dir / governance.LIMIT_ACTS_DIR).exists()
    assert not (store.dir / governance.REPLAN_SPECS_DIR).exists()
    [hold] = _holds(store)
    assert (hold["reason"], hold["limit_policy"]) == ("LIMIT_REACHED", on_limit.value)
    assert all(_lifecycles(store)[n] is READY for n in "bc") and not _steps(out, "LIMIT_ACT")


def test_a_wave_that_is_not_wider_than_one_has_nothing_to_narrow(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")], script={"a": "park"},
                                      contract=_contract(OnLimit.DEGRADE, concurrency=1))
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1
    [none] = _acts(store)
    assert none["act"]["reason"] == "NOTHING_TO_NARROW" and none["continued"] == []


def test_a_limit_below_the_ceiling_is_no_limit_and_is_not_degraded(tmp_path, capsys):
    """The slice changes nothing for a wave the limit does not hold: width four, no act, no record."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b"), _node("c")],
                                      script={"a": "park"}, spent=CEILING - 2)
    _drive(capsys, root)       # `a` books the ninth dispatch of ten and parks: the limit is not reached
    assert launcher.launched == ["a", "b", "c"]
    [wave] = _holds(store)
    assert wave["outcome"] == "CONTINUE" and wave["continued"] == ["b", "c"]
    assert not (store.dir / governance.LIMIT_ACTS_DIR).exists()


@pytest.mark.parametrize("mode,over,narrows", [
    ("PLAN_AUTONOMOUS", {"needs_you": "DIGEST_ONLY", "release_policy": "AUTO_PREPARE_ONLY"}, True),
    ("SUPERVISED", {"needs_you": "ON", "release_policy": "HUMAN_APPROVAL"}, False)])
def test_degrade_narrows_what_the_mode_would_have_run_and_only_that(tmp_path, capsys, mode, over, narrows):
    """The width a wave is narrowed *from* is the mode's (`mode_width`), not the horizon's number: under
    SUPERVISED the captain steers one node at a time already, so there is nothing to degrade and the
    hold stands; under PLAN_AUTONOMOUS the horizon's four become one."""
    contract = _mode_contract(mode, **over)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b"), _node("c")], contract=contract)
    _drive(capsys, root)
    if narrows:
        assert launcher.launched == ["b", "c"]
        assert [a["act"]["from_width"] for a in _acts(store, "degrade")] == [4, 4]
    else:
        assert launcher.launched == []
        [none] = _acts(store)
        assert none["act"]["reason"] == "NOTHING_TO_NARROW" and none["mode_wave"]["width"] == 1


def test_a_wallclock_that_ran_out_is_a_limit_that_degrades_too(tmp_path, capsys):
    contract = _contract(OnLimit.DEGRADE)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b"), _node("c")],
                                      contract=contract, spent=0, created_at="2026-10-18T09:00:00Z")
    _drive(capsys, root)
    assert launcher.launched == ["b", "c"]
    [hold, _second] = _holds(store)
    assert hold["limits"]["dispatches"] == {"max": 10, "spent": 0}
    assert hold["limits"]["wallclock_seconds"]["elapsed"] == 10800 and hold["reason"] == "LIMIT_REACHED"


def test_a_creation_time_that_cannot_be_read_is_never_an_allowance(tmp_path, capsys):
    """The limit is only reached because the elapsed time is unknown (the project claims to be created in
    the future): the hold stands, and the act says why it is not one."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b")], spent=0,
                                      created_at="2027-01-01T00:00:00Z")
    _drive(capsys, root)
    assert launcher.launched == []
    [none] = _acts(store)
    assert none["act"]["reason"] == "LIMIT_UNREADABLE"
    assert none["limits"]["wallclock_seconds"]["elapsed"] is None


# --- P: the plain rounds (revision 1) --------------------------------------------------------

#: policy -> everything the drives may launch beside a branch that parked, the ceiling spent by `a` itself
_LATER_DRIVE = {
    OnLimit.REPLAN: ["a", "replan"],
    OnLimit.STOP: ["a"],
    OnLimit.PARK: ["a"],
    OnLimit.DEGRADE: ["a", "b", "c", "d"],
}


@pytest.mark.parametrize("policy", list(OnLimit))
def test_a_later_drive_does_not_launch_past_the_spent_ceiling_whatever_the_policy(tmp_path, capsys, policy):
    """Reviewer A's reproduction. Drive one: `a` books the last dispatch and parks, the pass acts on the policy.
    Drive two starts with `a` BLOCKED and READY work at the head of the frontier: before the revision its plain
    rounds launched it, the ceiling still spent, under every policy. Now they ask the contract first."""
    contract = _contract(policy)
    nodes = [_base(), _node("a")] + [_node(n) for n in "bcdefg"]
    root, store, launcher, _ = _world(tmp_path, nodes, script={"a": "park"}, contract=contract)
    expect = [_replan_id(contract) if n == "replan" else n for n in _LATER_DRIVE[policy]]
    code, out = _drive(capsys, root)
    assert launcher.launched == expect and code == 1, out
    code, out = _drive(capsys, root)
    assert launcher.launched == expect, "the second drive launched past the ceiling"
    assert out["halt"] == "BUDGET_EXHAUSTED" and _steps(out, "LIMIT_HELD") == [f"LIMIT_HELD:{policy.value}"], out
    assert all(_lifecycles(store)[n] is READY for n in "efg")
    before = _records_tree(store)
    code, out = _drive(capsys, root)           # a state that was decided is not decided again: quiet
    assert launcher.launched == expect and _records_tree(store) == before and _steps(out, "CONTINUATION_DECIDED")


@pytest.mark.parametrize("policy", [OnLimit.PARK, OnLimit.STOP, OnLimit.DEGRADE])
def test_a_limit_the_drive_itself_reaches_stops_the_next_round_with_no_branch_parked(tmp_path, capsys, policy):
    """No parked branch, no open decision: the pass never started for this before. Two dispatches are left;
    each plain round books one; the third round asks, and the policy answers (PARK, STOP: the hold; DEGRADE:
    three waves of one node, then the hold)."""
    contract = _contract(policy)
    nodes = [_base()] + [_node(f"n{i}") for i in range(1, 8)]
    root, store, launcher, _ = _world(tmp_path, nodes, contract=contract, spent=CEILING - 2)
    code, out = _drive(capsys, root)
    plain = ["n1", "n2"]
    expect = plain + (["n3", "n4", "n5"] if policy is OnLimit.DEGRADE else [])
    assert launcher.launched == expect and code == 1 and out["halt"] == "BUDGET_EXHAUSTED", out
    assert _steps(out, "LIMIT_HELD") == [f"LIMIT_HELD:{policy.value}"]
    assert [n for n in "34567" if _lifecycles(store)[f"n{n}"] is READY] == list("34567")[len(expect) - 2:]
    [first, *_] = _holds(store)
    assert (first["outcome"], first["reason"], first["limit_policy"]) == ("HOLD", "LIMIT_REACHED", policy.value)
    assert first["limits"]["dispatches"] == {"max": CEILING, "spent": CEILING}
    code, out = _drive(capsys, root)           # the state the last wave left is decided once (a recorded hold)
    assert launcher.launched == expect
    before = _records_tree(store)
    code, out = _drive(capsys, root)
    assert launcher.launched == expect and _records_tree(store) == before, "the next drive is quiet"


def test_a_contract_that_names_no_limit_never_holds_the_plain_rounds(tmp_path, capsys):
    contract = _mode_contract("PLAN_AUTONOMOUS", needs_you="DIGEST_ONLY", release_policy="AUTO_PREPARE_ONLY",
                              budgets=Budgets().model_dump(mode="json"),
                              horizon=Horizon(max_concurrency=4).model_dump(mode="json"))
    assert contract.budgets.max_dispatches is None and contract.horizon.max_wallclock_seconds is None
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("n1"), _node("n2"), _node("n3")],
                                      contract=contract, spent=10**6)
    code, out = _drive(capsys, root)
    assert launcher.launched == ["n1", "n2", "n3"] and not _steps(out, "LIMIT_HELD"), out
    assert out["halt"] != "BUDGET_EXHAUSTED" and not (store.dir / governance.LIMIT_ACTS_DIR).exists()


def test_only_an_external_action_ready_halts_as_it_always_did_even_at_the_ceiling(tmp_path, capsys):
    root, store, launcher, _ = _world(tmp_path, [_base(), _push()])
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and out["halt"] == "BLOCKED_EXTERNAL" and not _steps(out, "LIMIT_HELD"), out


def test_a_drive_restricted_to_a_records_nodes_is_not_asked_again(tmp_path):
    """The record is the policy's answer: the restricted drive dispatches what it names with the ceiling spent."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("b"), _node("c")])
    held = governance.continue_by_policy(store, contract, root, wave_id="w1", depends_on=launcher.depends_on)
    assert held.reason == "LIMIT_REACHED"
    act = governance.act_on_limit(store, contract, root, held, depends_on=launcher.depends_on)
    controller = ProjectController(store, launcher, Gates())
    controller._allowed = frozenset(act.continued)
    result = controller._drive()
    assert launcher.launched == ["b"] and result.continuation_spent and not result.limit_held
    controller._allowed = None
    result = controller._drive()                                       # the plain round: asked, and held
    assert launcher.launched == ["b"] and result.limit_held and result.halt is HaltClass.BUDGET_EXHAUSTED


@pytest.mark.parametrize("spent,elapsed_start,reached", [
    (CEILING - 1, CREATED, False), (CEILING, CREATED, True), (CEILING + 1, CREATED, True),
    (0, "2026-10-18T11:00:01Z", False), (0, "2026-10-18T11:00:00Z", True), (0, "2026-10-18T10:59:59Z", True),
    (0, "2027-01-01T00:00:00Z", True)])
def test_the_gate_reads_the_limits_exactly_as_the_pass_does(tmp_path, spent, elapsed_start, reached):
    """`limit_reached` is `continue_by_policy`'s own reading: the ceiling is reached AT the number (not above
    it), the wallclock AT its limit (3600 s from NOW), and a creation time in the future is not measurable."""
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("b")], spent=spent, created_at=elapsed_start)
    state = store.read_state()
    assert governance.limit_reached(state, contract, root) is reached
    hold = governance.continue_by_policy(store, contract, root, wave_id="w1", depends_on=launcher.depends_on,
                                         dry_run=True)
    assert (hold.reason == "LIMIT_REACHED") is reached, "the gate and the pass disagree"


def test_a_spend_that_cannot_be_read_holds_the_gate_and_no_limit_named_holds_nothing(tmp_path):
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("b")], spent=None)
    (root / "base" / "state.json").write_text("[1]", encoding="utf-8")
    assert governance.limit_reached(store.read_state(), contract, root) is True
    open_ = _mode_contract("PLAN_AUTONOMOUS", needs_you="DIGEST_ONLY", release_policy="AUTO_PREPARE_ONLY",
                           budgets=Budgets().model_dump(mode="json"),
                           horizon=Horizon(max_concurrency=4).model_dump(mode="json"))
    assert governance.limit_reached(store.read_state(), open_, root) is False


@pytest.mark.parametrize("policy", [OnLimit.DEGRADE, OnLimit.REPLAN])
def test_a_drive_held_at_the_limit_keeps_the_halt_of_the_restricted_drive_it_superseded(tmp_path, capsys, policy):
    """The drive ends `BUDGET_EXHAUSTED` whatever the restricted drives did -- but a restricted drive that ended on
    something of its own (here a merge conflict of the node the policy let through) is not hidden behind that class:
    its halt is in the reason and the step `LIMIT_SUPERSEDED_HALT:<halt>`. Without a node of its own to report (the
    drive ended because its nodes are done) nothing is added."""
    contract = _contract(policy)
    first = _replan_id(contract) if policy is OnLimit.REPLAN else "n1"
    root, store, launcher, _ = _world(tmp_path / "bad", [_base(), _node("n1"), _node("n2")],
                                      script={first: "conflict"}, contract=contract)
    code, out = _drive(capsys, root)
    assert launcher.launched == [first] and code == 1, out
    assert out["halt"] == "BUDGET_EXHAUSTED" and "MERGE_CONFLICT" in out["reason"], out
    assert out["reason"].startswith("a limit of the autonomy contract is reached")
    assert "LIMIT_SUPERSEDED_HALT:MERGE_CONFLICT" in out["steps"] and "LIMIT_HELD:" + policy.value in out["steps"]
    assert any(st.startswith("MERGE_CONFLICT:") for st in out["steps"]), "the conflict is still a step"
    root, store, launcher, _ = _world(tmp_path / "ok", [_base(), _node("n1"), _node("n2")], contract=contract)
    code, out = _drive(capsys, root)
    assert out["halt"] == "BUDGET_EXHAUSTED" and "restricted to the policy" not in out["reason"], out
    assert not [st for st in out["steps"] if st.startswith("LIMIT_SUPERSEDED_HALT")]


# --- R: REPLAN -------------------------------------------------------------------------------

def test_replan_requests_one_step_dispatches_it_and_then_the_limit_holds(tmp_path, capsys):
    root, store, launcher, contract = _world(
        tmp_path, [_base(), _node("a"), _node("b"), _node("c")], script={"a": "park"},
        contract=_contract(OnLimit.REPLAN))
    step_id = _replan_id(contract)
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a", step_id] and launcher.merged == [step_id], out
    state = store.read_state()
    assert [n.id for n in state.nodes] == ["base", "a", "b", "c", step_id]
    step = state.node(step_id)
    assert (step.lifecycle, step.action_class, step.repair_of, step.run_id) == (MERGED, INTERNAL, None, None)
    assert step.writes == [f"PLAN_REVISION_{contract.digest()}.md"]
    # the request is a decision of the project, and says what it is
    [decision] = [d for d in state.decisions if d.kind is DecisionKind.POLICY_DISPOSITION]
    assert decision.payload["node"] == step_id and decision.payload["policy"] == "REPLAN"
    # the specification: written before the node, in the project's own directory, naming what remains
    spec = Path(step.spec_path)
    assert spec == store.dir / governance.REPLAN_SPECS_DIR / f"{step_id}.md"
    text = spec.read_text(encoding="ascii")
    assert hashlib.sha256(text.encode()).hexdigest() == step.spec_digest
    assert "- a (BLOCKED)" in text and "- b (READY)" in text and "- c (READY)" in text
    assert "dispatches: at most 10" in text and "wallclock (horizon): at most 3600 seconds" in text
    assert "base" not in text.split("## The work that remains")[1].split("##")[0], "settled work is not listed"
    assert stat.S_IMODE(os.stat(spec).st_mode) == 0o600 and stat.S_IMODE(os.stat(spec.parent).st_mode) == 0o700
    # three passes: the request, the step, the end of the allowance
    assert [s.split(":")[0] for s in out["steps"] if s.split(":")[0] in
            ("REPLAN_REQUESTED", "LIMIT_ACT", "CONTINUATION")].count("LIMIT_ACT") == 3
    [taken] = _acts(store, "replan")
    assert taken["act"] == {"kind": "REPLAN_STEP", "node": step_id} and taken["continued"] == [step_id]
    assert {(w["node"], w["reason"]) for w in taken["withheld"]} == {("b", "REPLAN_PENDING"), ("c", "REPLAN_PENDING")}
    assert launcher.acts_at_launch[step_id] == [f"{taken['wave_id']}.replan.json"]
    [spent] = _acts(store, "none")
    assert spent["act"]["reason"] == "REPLAN_SPENT" and spent["continued"] == []
    assert _lifecycles(store)["b"] is READY and _lifecycles(store)["c"] is READY
    assert code == 1


def test_a_replan_is_one_step_per_contract_and_the_drive_after_it_is_quiet(tmp_path, capsys):
    root, store, launcher, contract = _world(
        tmp_path, [_base(), _push(), _node("b")], contract=_contract(OnLimit.REPLAN))
    _drive(capsys, root)
    assert launcher.launched == [_replan_id(contract)]
    before = _records_tree(store)
    for _ in range(2):
        code, out = _drive(capsys, root)
        assert code == 1 and launcher.launched == [_replan_id(contract)]
        assert _records_tree(store) == before
        assert _steps(out, "CONTINUATION_DECIDED") and not _steps(out, "REPLAN_REQUESTED")
    assert len([n for n in store.read_state().nodes if n.id.startswith("replan-")]) == 1


def test_a_replan_step_that_is_not_ready_is_in_hand_and_nothing_else_continues(tmp_path, capsys):
    contract = _contract(OnLimit.REPLAN)
    step = _node(_replan_id(contract), Lifecycle.CONTINGENT)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b"), step], contract=contract)
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and code == 1, out
    [none] = _acts(store, "none")
    assert none["act"]["reason"] == "REPLAN_NOT_READY" and none["continued"] == []
    assert _lifecycles(store)["b"] is READY


@pytest.mark.parametrize("policy", [OnLimit.REPLAN, OnLimit.DEGRADE])
def test_a_spend_that_cannot_be_read_is_never_an_allowance(tmp_path, capsys, policy):
    root, store, launcher, contract = _world(
        tmp_path, [_base(), _node("a"), _node("b"), _node("c")], script={"a": "park"},
        contract=_contract(policy), spent=None)
    (root / "base" / "state.json").write_text("{not json", encoding="utf-8")
    code, out = _drive(capsys, root)
    # a spend that cannot be read is a limit reached: the plain round dispatches nothing, not even `a`
    assert launcher.launched == [] and code == 1 and out["halt"] == "BUDGET_EXHAUSTED", out
    assert "LIMIT_HELD:" + policy.value in out["steps"]
    [none] = _acts(store)
    assert none["act"]["reason"] == "LIMIT_UNREADABLE" and none["continued"] == []
    assert [n.id for n in store.read_state().nodes] == ["base", "a", "b", "c"]


# --- E: the crash windows and the failures ---------------------------------------------------

def test_a_hold_whose_act_was_never_recorded_is_acted_on_by_the_next_drive(tmp_path, capsys, monkeypatch):
    """The pass can end between the hold (written first) and the act. A state version with a record is
    otherwise decided once and never again -- this one is not final."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b"), _node("c")])
    real = governance.act_on_limit

    def refuses(*a, **kw):
        raise governance.ContinuationError("the act could not be recorded")

    monkeypatch.setattr(governance, "act_on_limit", refuses)
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and _steps(out, "CONTINUATION_REFUSED"), out
    assert len(_holds(store)) == 1 and not _files(store, governance.LIMIT_ACTS_DIR)
    hold_bytes = _files(store, governance.POLICY_DIR)[0].read_bytes()
    monkeypatch.setattr(governance, "act_on_limit", real)
    code, out = _drive(capsys, root)
    assert launcher.launched == ["b", "c"], out
    assert _files(store, governance.POLICY_DIR)[0].read_bytes() == hold_bytes, "the hold is not rewritten"
    assert [a["continued"] for a in _acts(store, "degrade")] == [["b"], ["c"]]


def test_a_hold_that_is_decided_again_is_never_dispatched_from_when_its_limit_is_gone(tmp_path, capsys, monkeypatch):
    """The recovery of the window decides again without writing (there is a record already). If the limit
    is not reached any more -- a spend that was booked has gone -- that unrecorded decision continues
    nothing."""
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b"), _node("c")])
    real = governance.act_on_limit
    monkeypatch.setattr(governance, "act_on_limit", lambda *a, **kw: (_ for _ in ()).throw(
        governance.ContinuationError("the act could not be recorded")))
    _drive(capsys, root)
    assert len(_holds(store)) == 1 and not _files(store, governance.LIMIT_ACTS_DIR)
    monkeypatch.setattr(governance, "act_on_limit", real)
    _spend(root, "base", 0)                                  # the limit is not reached any more
    before = _records_tree(store)
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and _steps(out, "CONTINUATION_REFUSED"), out
    assert _records_tree(store) == before and not _files(store, governance.LIMIT_ACTS_DIR)


def test_a_replan_request_that_could_not_be_made_is_said_and_made_by_the_next_drive(tmp_path, capsys,
                                                                                    monkeypatch):
    contract = _contract(OnLimit.REPLAN)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b")], contract=contract)
    monkeypatch.setattr(ProjectController, "_request_replan", lambda self, act, wave: "the store is gone")
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and _steps(out, "REPLAN_NOT_REQUESTED"), out
    assert [n.id for n in store.read_state().nodes] == ["base", "push", "b"]
    spec = store.dir / governance.REPLAN_SPECS_DIR / f"{_replan_id(contract)}.md"
    spec_bytes = spec.read_bytes()
    monkeypatch.undo()
    monkeypatch.setattr(governance, "utcnow", lambda: NOW)
    code, out = _drive(capsys, root)
    assert launcher.launched == [_replan_id(contract)], out
    assert spec.read_bytes() == spec_bytes, "the same state version gives the same specification"


def test_a_replanning_specification_that_exists_with_other_content_is_refused(tmp_path, capsys):
    contract = _contract(OnLimit.REPLAN)
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")], script={"a": "park"},
                                      contract=contract)
    specs = store.dir / governance.REPLAN_SPECS_DIR
    specs.mkdir(mode=0o700)
    (specs / f"{_replan_id(contract)}.md").write_text("someone else's text", encoding="ascii")
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and _steps(out, "CONTINUATION_REFUSED"), out
    assert [n.id for n in store.read_state().nodes] == ["base", "a", "b"]


@pytest.mark.parametrize("how", ["a file", "a link", "a dangling link as the file"])
def test_a_replanning_specification_that_cannot_be_written_requests_nothing(tmp_path, capsys, how):
    contract = _contract(OnLimit.REPLAN)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b")], contract=contract)
    specs = store.dir / governance.REPLAN_SPECS_DIR
    if how == "a file":
        specs.write_text("not a directory", encoding="utf-8")
    elif how == "a link":
        (tmp_path / "elsewhere").mkdir()
        specs.symlink_to(tmp_path / "elsewhere")
    else:
        specs.mkdir(mode=0o700)
        (specs / f"{_replan_id(contract)}.md").symlink_to(tmp_path / "nowhere.md")
    code, out = _drive(capsys, root)
    assert launcher.launched == [] and code == 1 and _steps(out, "CONTINUATION_REFUSED"), out
    assert [n.id for n in store.read_state().nodes] == ["base", "push", "b"]
    assert not (tmp_path / "elsewhere").exists() or not list((tmp_path / "elsewhere").iterdir())


@pytest.mark.parametrize("how", ["a file", "a link", "too many entries"])
def test_a_directory_of_acts_that_cannot_be_read_gives_no_allowance(tmp_path, capsys, monkeypatch, how):
    root, store, launcher, _ = _world(tmp_path, [_base(), _node("a"), _node("b")], script={"a": "park"})
    acts = store.dir / governance.LIMIT_ACTS_DIR
    if how == "a file":
        acts.write_text("not a directory", encoding="utf-8")
    elif how == "a link":
        (tmp_path / "elsewhere").mkdir()
        acts.symlink_to(tmp_path / "elsewhere")
    else:
        acts.mkdir()
        monkeypatch.setattr(governance, "MAX_LIMIT_ACT_ENTRIES", 2)
        for n in range(3):
            (acts / f"junk{n}").write_text("x")
    code, out = _drive(capsys, root)
    assert launcher.launched == ["a"] and code == 1, out
    assert _steps(out, "CONTINUATION_REFUSED")
    assert all(_lifecycles(store)[n] is READY for n in "b")


def test_a_master_that_lost_the_lease_before_the_replanning_step_is_added_adds_nothing(tmp_path):
    from hoh.succession import MasterAuthority

    contract = _contract(OnLimit.REPLAN)
    root, store, launcher, _ = _world(tmp_path, [_base(), _push(), _node("b")], contract=contract)

    class Superseded(ProjectController):
        def _request_replan(self, act, wave_id):      # a successor takes the lease just before the write
            self.authority.release(reason="test: handed over")
            MasterAuthority(store.dir, "sB", serialize=lambda: orchestrator.write_lock(store)).acquire(
                program_generation=0, note="successor")
            return super()._request_replan(act, wave_id)

    authority = MasterAuthority(store.dir, "sA", serialize=lambda: orchestrator.write_lock(store))
    authority.acquire(program_generation=0, note="the old master")
    result = Superseded(store, launcher, Gates(), authority=authority).run()
    assert result.halt is HaltClass.SUPERSEDED and launcher.launched == []
    assert [n.id for n in store.read_state().nodes] == ["base", "push", "b"]


# --- A: act_on_limit as a function -----------------------------------------------------------

def _held(store, contract, root, launcher, wave="w1"):
    return governance.continue_by_policy(store, contract, root, wave_id=wave, depends_on=launcher.depends_on,
                                         dry_run=True)


def _parked_world(tmp_path, contract=None):
    contract = contract or _contract()
    root, store, launcher, contract = _world(tmp_path, [_base(), _node("a"), _node("b"), _node("c")],
                                             contract=contract)
    return root, store, launcher, contract


def test_act_on_limit_acts_only_on_a_hold_for_a_reached_limit_of_its_own_contract(tmp_path):
    root, store, launcher, contract = _parked_world(tmp_path)
    held = _held(store, contract, root, launcher)
    assert (held.outcome, held.reason) == ("HOLD", "LIMIT_REACHED")

    def refused(contract_, held_, **kw):
        with pytest.raises(governance.ContinuationError, match="NOT_A_LIMIT_ACT|STATE_MOVED|PIN"):
            governance.act_on_limit(store, contract_, root, held_, depends_on=launcher.depends_on, **kw)

    refused(contract, replace(held, reason="PROGRAM_STUCK"))
    refused(contract, replace(held, outcome="CONTINUE"))
    refused(contract, replace(held, limit_policy="PARK"))
    refused(contract, replace(held, project_id="q"))
    refused(contract, replace(held, contract={**held.contract, "digest": "0" * 16}))
    refused(contract, replace(held, wave_id="no spaces allowed"))
    refused(contract, replace(held, limits={**held.limits, "dispatches": {"max": 10, "spent": 9}}))
    refused(contract, replace(held, limits={}))
    refused(contract, replace(held, state={**held.state, "write_seq": held.state["write_seq"] + 1}))
    refused(_contract(OnLimit.PARK), held)                       # the contract says PARK now
    with pytest.raises(governance.ContinuationError):
        governance.act_on_limit(store, contract, root, "not a record")
    with pytest.raises(governance.ContinuationError):
        governance.act_on_limit(store, contract, root / "missing", held)
    assert not (store.dir / governance.LIMIT_ACTS_DIR).exists(), "a refused call writes nothing"


@pytest.mark.parametrize("review", [{"state": "DUE", "needs_you": True}, {"state": "UNREADABLE", "needs_you": True},
                                    {"state": "NOT_ARMED", "needs_you": True}, {}, None])
def test_a_review_that_is_due_calls_a_person_before_anything_is_degraded(tmp_path, review):
    """Due, unreadable, not armed, an empty block, no block: the hold's own review block must say
    `needs_you: false` for the act to go on."""
    root, store, launcher, contract = _parked_world(tmp_path)
    held = _held(store, contract, root, launcher)
    mode_wave = {k: v for k, v in held.mode_wave.items() if k != "review"}
    if review is not None:
        mode_wave["review"] = review
    act = governance.act_on_limit(store, contract, root, replace(held, mode_wave=mode_wave),
                                  depends_on=launcher.depends_on)
    assert (act.kind, act.reason, act.continued) == ("NONE", "REVIEW_PENDING", ())
    assert _acts(store, "none")[0]["act"]["reason"] == "REVIEW_PENDING"


def test_the_record_of_an_act_is_bound_ascii_and_create_only(tmp_path):
    root, store, launcher, contract = _parked_world(tmp_path)
    held = _held(store, contract, root, launcher)
    act = governance.act_on_limit(store, contract, root, held, depends_on=launcher.depends_on)
    assert act.kind == "DEGRADE" and act.continued == ("a",)
    path = store.dir / governance.LIMIT_ACTS_DIR / "w1.degrade.json"
    assert path.read_bytes() == act.plan.to_bytes() and path.read_bytes().isascii()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert _read(path)["state"] == held.state and _read(path)["approves"] is False
    with pytest.raises(governance.ContinuationError, match="exists"):
        governance.act_on_limit(store, contract, root, held, depends_on=launcher.depends_on)


@pytest.mark.parametrize("policy", [OnLimit.DEGRADE, OnLimit.REPLAN])
def test_a_state_that_moved_after_the_hold_is_not_acted_on(tmp_path, policy):
    """For DEGRADE the record's own write would catch it; for REPLAN the act writes a specification and
    asks the caller to add a node, so the act itself has to look at the state."""
    root, store, launcher, contract = _parked_world(tmp_path, _contract(policy))
    held = _held(store, contract, root, launcher)
    state = store.read_state()
    state.nodes.append(_node("late"))
    store.write_state(state)
    with pytest.raises(governance.ContinuationError, match="STATE_MOVED"):
        governance.act_on_limit(store, contract, root, held, depends_on=launcher.depends_on)
    assert not (store.dir / governance.LIMIT_ACTS_DIR).exists()
    assert not (store.dir / governance.REPLAN_SPECS_DIR).exists()


def test_a_record_without_an_act_is_byte_identical_to_slice_one(tmp_path):
    root, store, launcher, contract = _parked_world(tmp_path, _contract(OnLimit.PARK))
    held = _held(store, contract, root, launcher)
    assert b'"act"' not in held.to_bytes()
    assert b'"act"' in replace(held, act={"kind": "NONE"}).to_bytes()


@pytest.mark.parametrize("damage", ["garbage", "a list", "another wave", "another reason", "oversize", "a link",
                                    "a FIFO", "a second name"])
def test_a_hold_record_that_is_not_plainly_its_own_is_not_unfinished(tmp_path, damage):
    root, store, launcher, contract = _parked_world(tmp_path)
    wave = "w1"
    held = governance.continue_by_policy(store, contract, root, wave_id=wave, depends_on=launcher.depends_on)
    path = store.dir / governance.POLICY_DIR / f"{wave}.json"
    assert governance.limit_act_unfinished(store, contract, wave) is True
    good = path.read_bytes()
    body = json.loads(good)
    if damage == "garbage":
        path.write_bytes(b"\xff\xfe not json")
    elif damage == "a list":
        path.write_bytes(b"[1]")
    elif damage == "another wave":
        path.write_bytes(json.dumps({**body, "wave_id": "w2"}).encode())
    elif damage == "another reason":
        path.write_bytes(json.dumps({**body, "reason": "PROGRAM_STUCK"}).encode())
    elif damage == "oversize":
        path.write_bytes(good + b" " * (governance.MAX_POLICY_RECORD_BYTES + 1))
    elif damage == "a link":
        aside = tmp_path / "aside.json"
        aside.write_bytes(good)
        os.replace(path, tmp_path / "moved.json")
        path.symlink_to(aside)
    elif damage == "a FIFO":
        os.replace(path, tmp_path / "moved.json")
        os.mkfifo(path)
    else:
        os.link(path, tmp_path / "second-name.json")
    assert governance.limit_act_unfinished(store, contract, wave) is False
    assert held.reason == "LIMIT_REACHED"


def test_a_hold_with_an_act_recorded_is_final_and_so_is_a_contract_that_does_not_act(tmp_path):
    root, store, launcher, contract = _parked_world(tmp_path)
    governance.continue_by_policy(store, contract, root, wave_id="w1", depends_on=launcher.depends_on)
    assert governance.limit_act_unfinished(store, contract, "w1") is True
    for kind in ("degrade", "replan", "none"):
        acts = store.dir / governance.LIMIT_ACTS_DIR
        acts.mkdir(exist_ok=True)
        (acts / f"w1.{kind}.json").write_text("{}")
        assert governance.limit_act_unfinished(store, contract, "w1") is False, kind
        os.replace(acts / f"w1.{kind}.json", tmp_path / f"w1.{kind}.json.aside")
    assert governance.limit_act_unfinished(store, _contract(OnLimit.STOP), "w1") is False
    assert governance.limit_act_unfinished(store, contract, "w9") is False
    assert governance.limit_act_unfinished(store, contract, 7) is False


def test_the_replanning_specification_is_a_function_of_the_graph_and_the_contract(tmp_path):
    root, store, launcher, contract = _parked_world(tmp_path, _contract(OnLimit.REPLAN))
    state = store.read_state()
    one = governance._replan_request(store, state, contract, _replan_id(contract))
    two = governance._replan_request(store, state, contract, _replan_id(contract))
    assert one[2] == two[2] and one[2].isascii()
    [first, *_] = one[2].decode().splitlines()
    assert first == f"# Replan: {_replan_id(contract)}"
    many = [_node(f"n{i}") for i in range(governance.MAX_REPLAN_LISTED + 5)]
    state.nodes.extend(many)
    text = governance._replan_request(store, state, contract, _replan_id(contract))[2].decode()
    assert text.count("\n- n") == governance.MAX_REPLAN_LISTED - 3 and "and 8 more, not listed" in text


def test_the_real_launcher_prepares_the_replanning_node_as_written(tmp_path):
    """`HohRunLauncher.prepare` takes a node that has a specification and no `repair_of`: it starts a run on
    the specification the pass wrote and writes no repair specification of its own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    for argv in (["init", "-q", "-b", "master"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        assert subprocess.run([*git, *argv], capture_output=True).returncode == 0

    class Grants:
        name = "test"

        def may_approve(self, worktree):
            return True

        def approve(self, worktree, repo_):
            from hoh.approval import Approval
            return Approval(granted=True, worktree=worktree, provider=self.name)

    class Spy(HohRunLauncher):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls: list = []

        def _run_cli(self, argv):
            self.calls.append(list(argv))
            if "worktree" in argv:
                path = tmp_path / "worktrees" / argv[argv.index("--branch") + 1]
                path.mkdir(parents=True, exist_ok=True)
                return Dispatch(0, json.dumps({"path": str(path)}), "")
            return Dispatch(0, "", "")

    contract = _contract(OnLimit.REPLAN)
    root, store, _launcher, _ = _world(tmp_path, [_base(), _node("a")], contract=contract)
    state = store.read_state()
    node, path, data = governance._replan_request(store, state, contract, _replan_id(contract))
    governance._write_replan_spec(path, data)
    launcher = Spy(root, repo, approvals=Grants(), planner="claude", developer="claude", qa="claude")
    assert launcher.prepare(node) is None, "nothing to dispatch is the answer for a node with no specification"
    [start] = [c for c in launcher.calls if "start" in c]
    assert start[start.index("--spec") + 1] == node.spec_path and start[start.index("--run-id") + 1] == node.id
    assert not (root / node.id / f"repair-{node.id}.md").exists()


def test_the_register_row_of_this_slice_names_the_probe_the_matrix_reads():
    """P3-32 as slice 3 left it (R-1), read where slice 4 kept it by addition: the symbol and the node exist, the
    probe carried the one thing that was left -- the executor of slice 4 -- as its exact equivalent, and the
    earlier probe is kept under it. (tests/test_external_executor.py reads the row as it stands.)"""
    register = json.loads((HERE.parent / "program" / "v3_3" / "REQUIREMENTS.json").read_text(encoding="utf-8"))
    row = {r["id"]: r for r in register["requirements"]}["P3-32"]
    probe = row["previous_probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "act_on_limit", "governance.py")
    path, node = probe["test"].split("::")
    assert path == "tests/test_limit_policies.py" and hasattr(sys.modules[__name__], node) and callable(governance.act_on_limit)
    equivalent = probe["equivalent"]
    assert "recorded, not executed yet" in equivalent and "slice 4" in equivalent and "NEVER_AUTO_PUBLISH" in equivalent
    assert "REPLAN and DEGRADE" not in equivalent, "the policies are acted on: the equivalent no longer says they are not"
    assert "approves and executes nothing" in probe["note"] and "plain rounds" in probe["note"]
    assert "dispatch-budget.json" in probe["note"], "bounded counts nodes and runs, not dispatches: said"
    assert probe["replaced_on"] == "2026-10-20"
    previous = probe["previous_probe"]
    assert (previous["symbol"], previous["replaced_on"]) == ("continue_by_policy", "2026-10-19")
    assert "REPLAN" in previous["equivalent"], "the equivalent of slice two is kept, by addition"
