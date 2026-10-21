"""P3-19 slice 2: the wave's admission asks the resource scheduler.

Before the project loop prepares and launches a wave, the nodes it would start go through `scheduler.admit` with the
capacity registry of the state root and the live holds `hoh capacity check` reads (`capacitycheck.admit_wave`): the
admitted nodes are prepared and launched, the others wait READY with their reason, and anything that cannot be known
admits nobody. No registry record is no gate, and says so.

What is real: `ProjectController`, `ParallelWaveExecutor`, `HohRunLauncher` (with only its process boundary
replaced, as in `tests/test_wave_controller.py`: no agent, no Herdr, no network), the registry (`capacity.write`
into a `StorageRegistry.init()` root), run leases (`RunLeases`), the project store, a git repository with real
linked worktrees. Every test names the failure it catches and builds its world under `tmp_path`.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import GateRunner, HaltClass, MergeResult, ProjectController
from hoh.project import ActionClass, GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
CONFIG: dict = {}
FILE = "capacity-registry.json"
TIME = "%Y-%m-%dT%H:%M:%SZ"


def _capacity():
    import importlib
    return importlib.import_module("hoh.capacity")


def _check():
    import importlib
    return importlib.import_module("hoh.capacitycheck")


def utc(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime(TIME)


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject, exit_code=0, detail="")]


class Launcher(HohRunLauncher):
    """The real launcher with only the process boundary replaced; every run is accepted at once. `starts` is the
    order in which `hoh start` was called, `running` how many `hoh run`s were executing together at most."""

    def __init__(self, root, repo, *, barrier=None, **kw):
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.barrier = barrier
        self.calls: list[list[str]] = []
        self.starts: list[str] = []
        self.merged: list[str] = []
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _run_cli(self, argv):
        with self._lock:
            self.calls.append(list(argv))
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                git(self.repo_path, "worktree", "add", "-q", "-b", branch, str(path))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        if "start" in argv:
            run_id = argv[argv.index("--run-id") + 1]
            wt = argv[argv.index("--repo") + 1]
            spec = argv[argv.index("--spec") + 1]
            self.starts.append(run_id)
            RunStore(self.root, run_id).write_state(RunState(
                run_id=run_id, repo_path=wt, project_name="p", spec_path=spec, spec_digest="s" * 16,
                policy_digest="p" * 16, profile_digest="f" * 16, stage=Stage.PLANNING,
                condition=Condition.ACTIVE))
            return Dispatch(0, "", "")
        if "run" in argv:
            run_id = argv[argv.index("run") + 1]
            with self._lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                if self.barrier is not None and run_id in ("n1", "n2"):
                    self.barrier.wait(timeout=30)
                store = RunStore(self.root, run_id)
                state = store.read_state().model_copy(update={
                    "stage": Stage.CHECKPOINTED,
                    "last_accepted_candidate": Candidate(
                        candidate_id=f"{run_id}-i1", repo_path=store.read_state().repo_path,
                        commit="a" * 40, tree_clean=True, tree_digest="d" * 16)})
                store.write_state(state)
            finally:
                with self._lock:
                    self.running -= 1
            return Dispatch(0, "", "")
        raise AssertionError(f"unexpected cli call {argv}")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40,
                           candidate=outcome.candidate or "")


class World:
    def __init__(self, tmp_path: Path, names=("n1", "n2", "n3"), *, external=(), state: Path | None = None) -> None:
        from hoh.storage_registry import StorageRegistry
        tmp_path.mkdir(exist_ok=True)
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "master")
        (self.repo / "README").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "first")
        self.root = tmp_path / "root"
        nodes = []
        for name in names:
            spec = tmp_path / f"{name}.md"
            spec.write_text(f"# {name}\n", encoding="utf-8")
            kw = {"action_class": ActionClass.EXTERNAL} if name in external else {}
            nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"], **kw))
        self.store = ProjectStore(self.root, "p")
        self.store.create(ProjectState(project_id="p", repo_path=str(self.repo), nodes=nodes))
        self.state = state if state is not None else tmp_path / "state"
        self.state.parent.mkdir(parents=True, exist_ok=True)
        StorageRegistry(self.state).init()
        self.state = self.state.resolve()
        self.record = self.state / FILE

    def registry(self, *entries) -> None:
        capacity = _capacity()
        made = []
        for kind, name, limit, unit, source, *rest in entries:
            made.append(capacity.Entry(kind=kind, name=name, limit=limit, unit=unit, source=source,
                                       as_of=rest[0] if rest else utc()))
        capacity.write(self.record, made)

    def sessions(self, limit: int, as_of: str | None = None) -> None:
        self.registry(("machine", "host.sessions", limit, "sessions", "declared", *([as_of] if as_of else [])))

    def launcher(self, **kw) -> Launcher:
        kw.setdefault("storage_root", self.state)
        return Launcher(self.root, self.repo, **kw)

    def lease(self, project: str, run_id: str) -> None:
        from hoh.runlease import RunLeases
        RunLeases(self.root / "projects" / project / "run-leases").acquire(run_id, "holder", ttl_seconds=3600)

    def nodes(self):
        return {n.id: n for n in self.store.read_state().nodes}


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def kinds(result, kind):
    return [s for s in result.steps if s.kind == kind]


def waves(world) -> list:
    from hoh.wave import read_record
    directory = world.store.dir / "waves"
    return [read_record(p) for p in sorted(directory.iterdir())] if directory.exists() else []


# --- R1 the admission is the scheduler's, in the caller's order -----------------------------------------------------

def test_a_wave_is_cut_to_the_sessions_the_registry_admits_and_the_rest_wait_ready_and_unprepared(world):
    """Catches a wave that ignores the registry, or one that prepares (a run, a worktree, a budget hold) for a node it
    then refuses: three independent nodes at width 3 and room for 2 sessions: the first wave names n1 and n2 only
    (both inside `hoh run` at once), n3 is a `CAPACITY_WAIT` step (`exhausted`, 2 of 2 held) and is not started
    until the wave has settled; the project still closes and nobody ran more than 2 at once."""
    world.sessions(2)
    launcher = world.launcher(barrier=threading.Barrier(2))
    touched: list[str] = []                                  # nodes `_wave_round` took a hold-state of, in order
    original = launcher.prepared_hold
    launcher.prepared_hold = lambda node: (touched.append(node.id), original(node))[1]
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert touched == ["n1", "n2"]       # n3 waits ahead of `prepared`; it ran later, alone, in the sequential round
    [admitted] = kinds(result, "CAPACITY")
    digest = hashlib_sha(world.record)
    assert admitted.detail == f"capacity: 2 of 3 node(s) admitted on registry {digest[:12]}"
    first, *later = waves(world)
    assert [n.node_id for n in first.nodes] == ["n1", "n2"] and later == []     # n3 ran alone: no second wave
    [waiting] = [s for s in kinds(result, "CAPACITY_WAIT")]
    assert waiting.node == "n3" and waiting.round == 1
    assert "exhausted" in waiting.detail and "2 of 2 sessions" in waiting.detail
    assert launcher.starts.index("n3") > max(launcher.starts.index("n1"), launcher.starts.index("n2"))
    assert launcher.peak == 2
    assert sorted(launcher.merged) == ["n1", "n2", "n3"]


def test_other_runs_leases_are_held_sessions_and_each_node_past_the_limit_waits_with_its_figures(world):
    """Catches a plan that ignores what is already held or hides the figures: other leases hold 3 of 4 sessions, so
    one node fits; n1 is admitted and n2 and n3 each wait `exhausted` with "4 of 4 sessions" in the step (the
    admitted node's claim counted), nothing is prepared for them in that round, and the project still closes."""
    world.sessions(4)
    for run in ("a", "b", "c"):
        world.lease("other", run)
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    steps = {s.node: s.detail for s in kinds(result, "CAPACITY_WAIT") if s.round == 1}
    assert set(steps) == {"n2", "n3"}
    assert all(v.startswith("wait/exhausted") and "4 of 4 sessions" in v for v in steps.values())
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1"]
    assert launcher.starts[0] == "n1" and result.halt is HaltClass.CLOSED, result.reason


def test_the_holds_are_the_ones_hoh_capacity_check_reads(world):
    """Catches a second reader of holds that could disagree with the command: with two live run leases in another
    project and room for 3, the gate's plan holds exactly what `capacitycheck.live_holds` reads (2 sessions)
    and admits one node of three."""
    world.sessions(3)
    world.lease("other", "a")
    world.lease("other", "b")
    gate = _check().admit_wave(["n1", "n2", "n3"], runs_root=world.root, state_root=world.state, now=utc())
    held = _check().live_holds(world.root)
    assert held.sessions == 2
    assert gate.admitted == ("n1",) and [n for n, _ in gate.waits] == ["n2", "n3"]
    assert all("3 of 3 sessions" in why for _, why in gate.waits)      # 2 held + n1's claim, of a limit of 3


# --- R2 what cannot be known admits nobody ----------------------------------------------------------------------

def _break(world: World, label: str):
    """Spoils the world one way; returns what puts it right again (a rename aside, never a removal)."""
    if label == "a corrupt registry":
        world.record.write_bytes(b"{not json")
        return lambda: world.sessions(5)
    if label == "a stale declared limit":
        world.sessions(5, as_of=utc(-10 * 86400))
        return lambda: world.sessions(5)
    if label == "a capacity that is not recorded":
        world.registry(("provider", "anthropic", 5, "sessions", "declared"))
        return lambda: world.sessions(5)
    if label == "a limit of zero":
        world.sessions(0)
        return lambda: world.sessions(5)
    if label == "a unit mismatch":
        world.registry(("machine", "host.sessions", 5, "items", "declared"))
        return lambda: world.sessions(5)
    if label == "an unreadable run lease":
        directory = world.root / "projects" / "other" / "run-leases"
        directory.mkdir(parents=True)
        (directory / "r1.json").write_bytes(b"{not json")
        return lambda: (directory / "r1.json").rename(directory / "r1.json.set-aside")
    if label == "an unreadable budget ledger":
        ledger = world.root / "_budget-reservations.json"
        ledger.write_bytes(b"{not json")
        return lambda: ledger.rename(world.root / "_budget-reservations.json.set-aside")
    if label == "a state root that is not initialised":
        owner = world.state / "OWNER.json"
        kept = owner.read_bytes()
        owner.write_bytes(b"{}")
        return lambda: owner.write_bytes(kept)
    raise AssertionError(label)


BROKEN = ["a corrupt registry", "a stale declared limit", "a capacity that is not recorded", "a limit of zero",
          "a unit mismatch", "an unreadable run lease", "an unreadable budget ledger",
          "a state root that is not initialised"]


@pytest.mark.parametrize("label", BROKEN)
def test_what_cannot_be_judged_starts_nobody_blocks_nothing_and_is_not_stuck(world, label):
    """Catches a wave that launches when the registry cannot be trusted, and one that parks its nodes where the
    sentinel reads them as stuck: each broken state ends the round as `CAPACITY_WAIT` with the reason, before
    anything is prepared (no `hoh worktree`, `hoh start` or `hoh run`, no wave record), every node is still READY
    and `stuck_nodes` finds none. The control, the same world with a good registry, closes."""
    from hoh.sentinel import stuck_nodes
    world.sessions(5)
    launcher = world.launcher()
    repair = _break(world, label)
    seq = world.store.read_state().write_seq
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert world.store.read_state().write_seq == seq        # nothing persisted: `recover_projects` leaves it alone
    assert result.halt is HaltClass.CAPACITY_WAIT and result.reason.startswith("capacity"), result.reason
    assert HaltClass.CAPACITY_WAIT is not HaltClass.AMBIGUOUS
    assert launcher.calls == [] and waves(world) == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}
    assert stuck_nodes(world.store.read_state()) == []
    assert {s.node for s in kinds(result, "CAPACITY_WAIT")} == {"n1", "n2", "n3"}
    repair()
    again = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert again.halt is HaltClass.CLOSED, again.reason


def test_a_wait_is_not_a_human_decision_and_the_halt_class_is_its_own():
    """Catches a wait folded into a class that means a person is needed: `CAPACITY_WAIT` is a member of
    `HaltClass`, a string of that name, and not in `NEEDS_A_HUMAN` (as `STORAGE_WAIT`)."""
    from hoh.orchestrator import NEEDS_A_HUMAN
    assert HaltClass.CAPACITY_WAIT.value == "CAPACITY_WAIT" and HaltClass.CAPACITY_WAIT not in NEEDS_A_HUMAN


# --- R3 an absent registry is today's behaviour, said once ------------------------------------------------------------

@pytest.mark.parametrize("label", ["a state root with no record", "the default root of an unset environment"])
def test_no_registry_is_no_gate_the_wave_runs_as_before_and_the_opt_out_is_said_once(world, monkeypatch, label):
    """Catches a gate that breaks the runs of everyone who never declared a capacity, and an opt-out said every round:
    four independent nodes at width 2 (two wave rounds) run exactly as `test_wave_controller` does (the pair at once,
    two WAVE steps, CLOSED) with **one** `CAPACITY_OPT_OUT` step that names why, no `CAPACITY_WAIT`, whether the
    launcher's state root has no record or the launcher has none and the resolved default root has none."""
    fresh = World(world.tmp / "fresh", names=("n1", "n2", "n3", "n4"))
    if label == "a state root with no record":
        launcher = Launcher(fresh.root, fresh.repo, barrier=threading.Barrier(2), storage_root=fresh.state)
    else:
        launcher = Launcher(fresh.root, fresh.repo, barrier=threading.Barrier(2), storage_root=None)
        assert launcher.storage_root is None
    result = ProjectController(fresh.store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    [step] = kinds(result, "CAPACITY_OPT_OUT")
    assert "no capacity registry at capacity-registry.json" in step.detail and "not capacity-gated" in step.detail
    assert kinds(result, "CAPACITY_WAIT") == [] and len(kinds(result, "WAVE")) == 2
    assert launcher.peak == 2


def test_the_sequential_loop_is_not_gated_and_a_registry_that_would_refuse_changes_nothing_at_width_one(world):
    """Catches a gate that reaches the one-node-per-round loop (LINEAR, the default): a stale registry and width 1
    close the project exactly as with no registry, with no capacity step at all."""
    world.sessions(5, as_of=utc(-10 * 86400))
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert [s for s in result.steps if s.kind.startswith("CAPACITY")] == []


def test_a_spent_budget_is_still_the_budgets_halt_and_not_a_capacity_wait(world):
    """Catches a scheduler that takes the budget over: with a registry that admits everything and a dispatch
    budget of one, the second node still ends as the launcher decides (`prepare`/`take_hold`), the same
    outcome as with no registry, and no `CAPACITY_WAIT` step exists."""
    outcomes = []
    for with_registry in (False, True):
        fresh = World(world.tmp / f"budget-{with_registry}", names=("n1", "n2"))
        if with_registry:
            fresh.sessions(5)
        launcher = fresh.launcher(dispatch_budget=1, dispatch_reservation=1)
        result = ProjectController(fresh.store, launcher, Gates(), width=2).run()
        assert kinds(result, "CAPACITY_WAIT") == []
        outcomes.append((result.halt, {k: v.lifecycle for k, v in fresh.nodes().items()}))
    assert outcomes[0] == outcomes[1]


def test_the_policy_is_the_controllers_and_the_default_is_a_week(world):
    """Catches ages that are not forwarded: a declared limit 10 days old is stale under the default (a week, 604800
    seconds) and fresh under `capacity_policy` with 30 days; the controller keeps the policy it was given."""
    check = _check()
    world.sessions(5, as_of=utc(-10 * 86400))
    held = ProjectController(world.store, world.launcher(), Gates(), width=3)
    assert held.capacity_policy is None
    assert held.run().halt is HaltClass.CAPACITY_WAIT
    policy = check.policy(declared=30 * 86400)
    fresh = World(world.tmp / "again")
    fresh.sessions(5, as_of=utc(-10 * 86400))
    controller = ProjectController(fresh.store, fresh.launcher(), Gates(), width=3, capacity_policy=policy)
    assert controller.capacity_policy is policy and controller.run().halt is HaltClass.CLOSED


# --- R4 the production path ---------------------------------------------------------------------------------------------

def build_driver(store):
    """The `driver.json` factory of the CLI tests: the real launcher with a fake process boundary."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], barrier=cfg.get("barrier"), storage_root=cfg["state"])
    cfg["launcher"] = launcher
    return launcher, Gates()


def _drive(world, monkeypatch, capsys, width="3"):
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, state=world.state, barrier=None)
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_capacity:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", width)
    rc = cli.main(["--root", str(world.root), "project", "drive", "p", "--session", "sA"])
    return rc, json.loads(capsys.readouterr().out)


def test_hoh_project_drive_asks_the_scheduler_before_it_launches_a_wave(world, monkeypatch, capsys):
    """The production path, end to end: `hoh project drive` (cli.main) -> `resumeloop.drive_governed` ->
    `ProjectController.run` -> `_wave_round` -> `capacitycheck.admit_wave` -> `scheduler.admit`. Catches a
    gate that only the controller's own tests reach: with room for one session and three independent nodes the
    first wave names n1 alone, the other two are `CAPACITY_WAIT` steps in the printed record, and the project
    still closes."""
    world.sessions(1)
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    waits = [s for s in out["steps"] if s.startswith("CAPACITY_WAIT:")]
    assert waits == ["CAPACITY_WAIT:n2", "CAPACITY_WAIT:n3", "CAPACITY_WAIT:n3"]    # round 1: both; round 2: n3 again
    assert any(s.startswith("WAVE:") for s in out["steps"])
    assert sorted(CONFIG["launcher"].merged) == ["n1", "n2", "n3"]
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1"]


def test_hoh_project_drive_halts_capacity_wait_and_leaves_every_node_ready(world, monkeypatch, capsys):
    """Catches a wait that the drive reports as something else: a stale registry makes `hoh project drive` print the
    halt `CAPACITY_WAIT` with the reason, exit 1, launch nothing and leave the nodes READY."""
    world.sessions(5, as_of=utc(-10 * 86400))
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 1 and out["halt"] == "CAPACITY_WAIT" and "stale" in out["reason"], out
    assert CONFIG["launcher"].calls == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}


# --- R5 the adapter ---------------------------------------------------------------------------------------------------

def test_admit_wave_judges_a_good_registry_and_quotes_its_digest(world):
    """Catches an adapter that misreports what it judged: no state root and no record are `configured=False` with
    every node admitted; a registry of 2 sessions admits the first two of three in order, the third waits with its
    figures, and the gate quotes the sha256 of the record it judged."""
    check = _check()
    assert check.admit_wave(["a"], runs_root=world.root, state_root=None, now=utc()).configured is False
    absent = check.admit_wave(["a", "b"], runs_root=world.root, state_root=world.state, now=utc())
    assert absent.configured is False and absent.admitted == ("a", "b") and absent.waits == ()
    world.sessions(2)
    ok = check.admit_wave(["a", "b", "c"], runs_root=world.root, state_root=world.state, now=utc())
    assert ok.configured and ok.admitted == ("a", "b") and [n for n, _ in ok.waits] == ["c"]
    assert ok.registry == hashlib_sha(world.record) and "2 of 2 sessions" in ok.waits[0][1]


def test_now_is_taken_after_the_registry_and_the_holds_were_read(world, monkeypatch):
    """Catches a clock read before the registry (review of P3-17s2, A-M1, again here): the record is replaced while it is
    being read (its read takes 1.1 s and returns a record written at its end), so a `now` taken before the read
    is older than the fact and the decision `refuse`/`future`; taken after it, it is an admission at age 0."""
    import time
    check = _check()
    world.sessions(2, as_of=utc(-3600))
    real = check.read_registry

    def slow(path):
        time.sleep(1.1)
        world.sessions(2)                                      # as_of is now
        return real(path)
    monkeypatch.setattr(check, "read_registry", slow)
    gate = check.admit_wave(["a"], runs_root=world.root, state_root=world.state, now=lambda: utc())
    assert gate.admitted == ("a",), gate
    early = check.admit_wave(["a"], runs_root=world.root, state_root=world.state, now=utc(-5))
    assert early.admitted == () and "future" in early.waits[0][1]


def _unlistable(world):
    return {"node_ids": 42}


@pytest.mark.parametrize("label", ["a node id the scheduler refuses", "more than 64 nodes", "a node offered twice",
                                   "a now that is not a time", "a state root that is not a path",
                                   "a runs root that is not a path", "a node id that is not a string",
                                   "nodes that cannot be listed"])
def test_admit_wave_never_raises_and_admits_nobody_when_it_cannot_judge(world, label):
    """Catches an adapter that raises into the controller loop or admits on a failure of its own: each hostile
    request is a `Gate` that is `configured` and admits nobody, with the exception type or the reason in `why`,
    where a good registry of 5 sessions is present (so only the hostile input can be the cause)."""
    check = _check()
    world.sessions(5)
    kw = dict(node_ids=["a"], runs_root=world.root, state_root=world.state, now=utc())
    if label == "a node id the scheduler refuses":
        kw["node_ids"] = ["projects"]
    elif label == "more than 64 nodes":
        kw["node_ids"] = [f"n{i}" for i in range(65)]
    elif label == "a node offered twice":
        kw["node_ids"] = ["a", "a"]
    elif label == "a now that is not a time":
        kw["now"] = "not a time"
    elif label == "a state root that is not a path":
        kw["state_root"] = 42
    elif label == "a runs root that is not a path":
        kw["runs_root"] = 42
    elif label == "a node id that is not a string":
        kw["node_ids"] = [7]
    else:
        kw["node_ids"] = 42
    gate = check.admit_wave(**kw)
    assert gate.configured and gate.admitted == () and gate.why.startswith("capacity cannot be judged"), gate


def hashlib_sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- R6 the state root is the one `hoh capacity` uses -------------------------------------------------------------------

@pytest.mark.parametrize("where", ["configured on the launcher", "the environment", "the XDG default"])
def test_the_gate_resolves_the_state_root_exactly_as_hoh_capacity_does(world, monkeypatch, where):
    """Catches a second rule for the state root (the fail-open of review A): the registry the operator declared with
    `hoh capacity set` -- `--storage-root`'s place (the launcher's configured root), else `$HOH_STATE_ROOT`, else
    `$XDG_STATE_HOME/veriharness` -- is the one the wave asks. Each case declares **one** session at the resolved
    root (and room for 3 at a decoy root where one exists), so the first wave names n1 alone; `cli.resolve_storage_root`
    names the same directory."""
    from hoh.cli import resolve_storage_root
    xdg = world.tmp / "xdg"
    decoy = World(world.tmp / "decoy")
    decoy.sessions(3)
    if where == "configured on the launcher":
        world.sessions(1)
        monkeypatch.setenv("HOH_STATE_ROOT", str(decoy.state))
        launcher = world.launcher()
        assert launcher.storage_root == world.state
        target = world.state
    elif where == "the environment":
        world.sessions(1)
        monkeypatch.setenv("HOH_STATE_ROOT", str(world.state))
        launcher = Launcher(world.root, world.repo)
        target = world.state
    else:
        home = World(world.tmp / "home-root", state=xdg / "veriharness")
        home.sessions(1)
        launcher = Launcher(world.root, world.repo)
        assert launcher.storage_root is None
        target = xdg / "veriharness"
    assert resolve_storage_root(str(launcher.storage_root) if launcher.storage_root else None) == target
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert {s.node for s in kinds(result, "CAPACITY_WAIT") if s.round == 1} == {"n2", "n3"}
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1"]
    assert kinds(result, "CAPACITY_OPT_OUT") == []


# --- R7 only a missing record is no record ----------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["the state root is unreadable (mode 000)", "the state root is a link to itself",
                                   "the state root is a file", "the record is a link to itself"])
def test_only_a_missing_record_is_no_record_anything_else_admits_nobody(world, label):
    """Catches `lexists`-style opt-out (review A's fail-open): a permission error, a link loop, a path that is not a
    directory and a record that is a link are *cannot know*, not "nobody declared limits": each ends the round as
    `CAPACITY_WAIT` with the exception or reason named, nothing prepared, every node READY -- where a missing
    state root (`ENOENT`) is the opt-out."""
    import stat
    root = world.state
    undo = None
    if label == "the state root is unreadable (mode 000)":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode 000 directory")
        world.sessions(5)
        os.chmod(root, 0)
        undo = lambda: os.chmod(root, stat.S_IRWXU)            # noqa: E731 - so the temporary tree can be cleaned up
    elif label == "the state root is a link to itself":
        root = world.tmp / "loop"
        root.symlink_to(root)
    elif label == "the state root is a file":
        root = world.tmp / "plain-file"
        root.write_bytes(b"x")
    else:
        world.record.symlink_to(world.record)
    try:
        launcher = world.launcher(storage_root=root)
        result = ProjectController(world.store, launcher, Gates(), width=3).run()
    finally:
        if undo:
            undo()
    assert result.halt is HaltClass.CAPACITY_WAIT, (result.halt, result.reason)
    assert launcher.calls == [] and waves(world) == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}
    assert kinds(result, "CAPACITY_OPT_OUT") == []
    assert {s.node for s in kinds(result, "CAPACITY_WAIT")} == {"n1", "n2", "n3"}


def test_a_missing_state_root_is_the_opt_out_and_nothing_is_created(world):
    """Catches an opt-out that turns into a refusal for the one answer that means "nobody declared limits": a state
    root path that does not exist (ENOENT) runs the wave as before, with the step, and creates nothing there."""
    gone = world.tmp / "never-made"
    fresh = World(world.tmp / "fresh", names=("n1", "n2"))
    launcher = Launcher(fresh.root, fresh.repo, barrier=threading.Barrier(2), storage_root=gone)
    result = ProjectController(fresh.store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert len(kinds(result, "CAPACITY_OPT_OUT")) == 1 and not gone.exists()


# --- R8 what the wave names, bounds and says ---------------------------------------------------------------------------

def test_a_non_head_external_node_is_not_asked_for_and_takes_no_session(tmp_path):
    """Catches a candidate list that includes what the loop skips: n2 carries an irreversible external action and is
    not the head, so with room for 2 sessions the wave is n1 and n3 (both admitted, `2 of 2`), n2 is never a
    `CAPACITY_WAIT` and is the head of a later round."""
    fresh = World(tmp_path / "w", names=("n1", "n2", "n3"), external=("n2",))
    fresh.sessions(2)
    launcher = fresh.launcher(barrier=None)
    result = ProjectController(fresh.store, launcher, Gates(), width=3).run()
    first = waves(fresh)[0]
    assert [n.node_id for n in first.nodes] == ["n1", "n3"]
    assert [s for s in kinds(result, "CAPACITY_WAIT") if s.node == "n2"] == []
    [step] = kinds(result, "CAPACITY")
    assert "2 of 2 node(s) admitted" in step.detail


def test_only_the_nodes_within_the_width_are_asked_for(world):
    """Catches a candidate list wider than the wave: width 2, three nodes, one session: round 1 names n2 as the only
    waiting node -- n3 is past the width, is not offered, and is never `CAPACITY_WAIT` in that round."""
    world.sessions(1)
    result = ProjectController(world.store, world.launcher(), Gates(), width=2).run()
    assert {s.node for s in kinds(result, "CAPACITY_WAIT") if s.round == 1} == {"n2"}


def test_the_reasons_name_the_remedy_the_operator_runs(world):
    """Catches a reason that says verdict and no way out: a registry without `machine/host.sessions` halts with
    the `hoh capacity set machine host.sessions N sessions` remedy in the halt reason and in each step; a stale figure
    says that `hoh capacity set` refreshes it."""
    world.registry(("provider", "anthropic", 5, "sessions", "declared"))
    result = ProjectController(world.store, world.launcher(), Gates(), width=3).run()
    assert result.halt is HaltClass.CAPACITY_WAIT
    assert "hoh capacity set machine host.sessions N sessions" in result.reason
    assert all("hoh capacity set machine host.sessions N sessions" in s.detail for s in kinds(result, "CAPACITY_WAIT"))
    world.sessions(5, as_of=utc(-10 * 86400))
    result = ProjectController(world.store, world.launcher(), Gates(), width=3).run()
    assert "hoh capacity set refreshes it" in result.reason and "refuse/stale" in result.reason


def test_ids_the_wave_record_accepts_and_the_scheduler_refuses_wait_and_are_never_prepared(tmp_path):
    """Catches a reclassification nobody decided (review B F4): `projects` and `backups` pass the wave record's id
    grammar and are refused by the scheduler's; with a registry the wave halts `CAPACITY_WAIT` (on the pin the same
    world halted `CORRUPT_STATE`, the human class, when the node's run id was refused at `prepare`), prepares
    nothing, blocks nothing, and the reason names the scheduler's refusal."""
    fresh = World(tmp_path / "w", names=("projects", "n2"))
    fresh.sessions(5)
    launcher = fresh.launcher()
    result = ProjectController(fresh.store, launcher, Gates(), width=2).run()
    assert result.halt is HaltClass.CAPACITY_WAIT and "SchedulerError" in result.reason, result.reason
    assert launcher.calls == [] and {n.lifecycle for n in fresh.store.read_state().nodes} == {Lifecycle.READY}
