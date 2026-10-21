"""P3-25 and P3-26, the wiring slice: the parallelism modes take effect on the wave's admission.

A project declares a mode in `<project>/parallelism.json` (LINEAR, CONSERVATIVE, MODERATE, HIGH, MAX_SAFE or
AUTO_ADAPTIVE). Each wave round then offers the admission at most `scheduler.profile_width` of the candidates; under
AUTO_ADAPTIVE the level comes from `scheduler.adapt_profile` over the reason of P3-07's backpressure decision, and the
level in force and its stable count are kept in `<project>/parallelism-state.json`. What cannot be known is the floor
(the first node alone, `PARALLELISM_UNJUDGED`), never an empty round and never a wider one.

What is real: `ProjectController`, `ParallelWaveExecutor`, `HohRunLauncher` (only its process boundary replaced, as in
`tests/test_backpressure_wave.py`: no agent, no Herdr, no network), `hoh project drive` through `cli.main`, the project
store, a git repository with real linked worktrees, and real parked runs written by the product's own `RunStore`,
`RunState` and `dispatchrecovery.append_record`. Every test builds its world under `tmp_path`; test modules do not import
each other, so the small world is copied. Nothing is removed.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import subprocess
import threading
import types
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli, parallelmode
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import GateRunner, HaltClass, MergeResult, ProjectController, Step
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.scheduler import Adaptive, Bounds, adapt_profile
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
CONFIG: dict = {}


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
    order in which `hoh start` was called, `peak` how many `hoh run`s were executing together at most."""

    def __init__(self, root, repo, **kw):
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.starts: list[str] = []
        self.merged: list[str] = []
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _run_cli(self, argv):
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
    def __init__(self, tmp_path: Path, count: int = 3) -> None:
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
        for index in range(1, count + 1):
            spec = tmp_path / f"n{index}.md"
            spec.write_text(f"# n{index}\n", encoding="utf-8")
            nodes.append(TaskNode(id=f"n{index}", spec_path=str(spec), writes=[f"n{index}.txt"]))
        self.store = ProjectStore(self.root, "p")
        self.store.create(ProjectState(project_id="p", repo_path=str(self.repo), nodes=nodes))

    def launcher(self, **kw) -> Launcher:
        return Launcher(self.root, self.repo, **kw)

    def declare(self, **fields) -> Path:
        body = {"schema": parallelmode.SCHEMA, "mode": "MODERATE", "slots": 4, "reserve": 0, "ceiling": 4}
        body.update(fields)
        path = self.store.dir / parallelmode.FILE
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def state_file(self) -> dict:
        return json.loads((self.store.dir / parallelmode.STATE_FILE).read_text(encoding="utf-8"))

    def park(self, run_id: str, *, provider: str = "claude", account: str = "acct-a") -> None:
        """A run of another project parked on a provider limit, with the receipt the controller writes."""
        from hoh import dispatchrecovery
        from hoh.dispatchrecovery import (
            NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id)
        from hoh.failures import NormalizedFailure

        failure = NormalizedFailure.USAGE_LIMIT
        retry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="other",
                      spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                      profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED, iteration=1)
        st.blocked_kind = failure.value.lower()
        st.retry_after = retry
        st.blocked_reason = "parked"
        store = RunStore(self.root, run_id)
        store.write_state(st)
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=1,
                              attempt=st.attempt, stage=st.stage.value, candidate="NONE", master=NO_MASTER)
        dispatchrecovery.append_record(store.dir, RoutingDecision(
            park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
            provenance=DispatchProvenance(
                provider=provider, model="m-1", account_handle=account, role="developer",
                capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=retry))


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


def make_world(tmp_path, count=3) -> World:
    return World(tmp_path, count)


def gate_of(world, runs):
    """The gate the controller passes (`backpressure.admit_wave`, P3-07) bound to an empty runs root and the project's
    record directory: what `ProjectController._backpressure_admission` hands `parallelmode.admit_wave`."""
    from hoh import backpressure

    return lambda offered, decision_id: backpressure.admit_wave(
        offered, launcher=types.SimpleNamespace(root=runs), record_dir=world.store.dir / "backpressure",
        decision_id=decision_id)


def run_now(write):
    """The `fenced` the controller passes, without a master authority: the write runs as it is."""
    write()


def build_driver(store):
    """The `driver.json` factory of the CLI tests: the real launcher with a fake process boundary."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], **cfg.get("kw", {}))
    cfg["launcher"] = launcher
    return launcher, Gates()


def drive(world, capsys, *, session: str = "sA"):
    """`hoh project drive` through `cli.main`: the production path, with the declared mode and nothing else."""
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, kw={})
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_parallelism_modes:build_driver", "path": str(Path(__file__).resolve().parent)}))
    rc = cli.main(["--root", str(world.root), "project", "drive", "p", "--session", session])
    return rc, json.loads(capsys.readouterr().out)


def wave_sizes(world) -> list[int]:
    from hoh.wave import read_record
    directory = world.store.dir / "waves"
    records = [read_record(p) for p in directory.iterdir()] if directory.exists() else []
    order = lambda r: tuple(int(part) for part in r.wave_id[1:].split("-")[:2])      # noqa: E731 - w<seq>-<round>-<hex>
    return [len(r.nodes) for r in sorted(records, key=order)]


def steps_of(out, kind) -> list[str]:
    return [s for s in out["steps"] if s.split(":", 1)[0] == kind]


def merged(world) -> set[str]:
    return {n.id for n in world.store.read_state().nodes if n.lifecycle is Lifecycle.MERGED}


# --- B1 the production path: a declared profile sets the width of the waves hoh project drive launches ----------------

def test_hoh_project_drive_runs_waves_of_the_declared_profiles_width(tmp_path, capsys):
    """The production path, end to end: `hoh project drive` (cli.main) -> `_project_drive` -> `drive_governed` ->
    `ProjectController.run` -> `_wave_round` -> `_backpressure_admission` -> `parallelmode.admit_wave` ->
    `scheduler.profile_width`. Catches a profile nobody calls and a width the launcher's own default (1) still
    decides: with `HOH_WAVE_WIDTH` unset, a launcher of `wave_width` 1 and a project declaring MODERATE on 4 slots
    (70 percent of 4, rounded down: 2 lanes), five independent nodes run as waves of 2, 2 and then the last alone,
    two at a time at most, each wave round says `PARALLELISM`, the project closes and every node is merged."""
    world = make_world(tmp_path, 5)
    world.declare(mode="MODERATE", slots=4)
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert wave_sizes(world) == [2, 2]
    assert CONFIG["launcher"].peak == 2 and merged(world) == {"n1", "n2", "n3", "n4", "n5"}
    assert len(steps_of(out, "PARALLELISM")) == 2
    assert not steps_of(out, "PARALLELISM_UNJUDGED")
    assert not steps_of(out, "PARALLELISM_WAIT")             # n3..n5 are past the controller's width, not cut


def test_a_profile_narrower_than_the_outer_bound_cuts_the_rest_and_they_wait_ready(tmp_path, monkeypatch, capsys):
    """Catches a mode that only sets the controller's width: with `HOH_WAVE_WIDTH=5` as the outer bound and MODERATE
    on 4 slots (2 lanes), five nodes still run as waves of 2 (not 5), and each node past the two lanes is a
    `PARALLELISM_WAIT` step (READY, unprepared, asked again): n3, n4 and n5 in the first round, n5 in the second."""
    world = make_world(tmp_path, 5)
    world.declare(mode="MODERATE", slots=4)
    monkeypatch.setenv("HOH_WAVE_WIDTH", "5")
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert wave_sizes(world) == [2, 2] and CONFIG["launcher"].peak == 2
    assert steps_of(out, "PARALLELISM_WAIT") == ["PARALLELISM_WAIT:n3", "PARALLELISM_WAIT:n4",
                                                 "PARALLELISM_WAIT:n5", "PARALLELISM_WAIT:n5"]


def test_a_declared_linear_mode_is_the_sequential_loop_and_no_wave_round_is_asked(tmp_path, capsys):
    """Catches a LINEAR declaration that still launches waves: width of LINEAR is 1, so the controller's width is 1,
    no round is a wave, no `PARALLELISM` step is said and no wave record exists; the project closes node by node."""
    world = make_world(tmp_path, 3)
    world.declare(mode="LINEAR", slots=8)
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert wave_sizes(world) == [] and CONFIG["launcher"].peak == 1
    assert not [s for s in out["steps"] if s.startswith("PARALLELISM")]


def test_the_environment_and_an_explicit_width_stay_the_outer_bound_of_a_declared_mode(tmp_path, monkeypatch):
    """Catches a declaration that overrides what the operator typed: MAX_SAFE on 10 slots could offer 10, but
    `HOH_WAVE_WIDTH=2` is the outer bound (waves of 2) and `width=3` beats the environment (a wave of 3); the
    controller's width is the declaration's only when neither is given."""
    world = make_world(tmp_path, 4)
    world.declare(mode="MAX_SAFE", slots=10, ceiling=10)
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    launcher = world.launcher()
    controller = ProjectController(world.store, launcher, Gates())
    assert controller.width == 2
    assert controller.run().halt is HaltClass.CLOSED and launcher.peak == 2 and wave_sizes(world) == [2, 2]
    monkeypatch.delenv("HOH_WAVE_WIDTH")
    assert ProjectController(world.store, world.launcher(), Gates(), width=3).width == 3
    assert ProjectController(world.store, world.launcher(), Gates()).width == 10


# --- B2 the ladder: each profile offers its share, cut by ceiling, reserve, active and ready ---------------------------

LADDER = [
    # mode, slots, reserve, ceiling, active, ready -> offered
    ("LINEAR", 10, 0, 10, 0, 8, 1),
    ("CONSERVATIVE", 10, 0, 10, 0, 8, 4),
    ("MODERATE", 10, 0, 10, 0, 8, 7),
    ("HIGH", 10, 0, 10, 0, 8, 8),
    ("HIGH", 10, 0, 10, 0, 12, 9),
    ("MAX_SAFE", 10, 0, 10, 0, 12, 10),
    ("MAX_SAFE", 10, 0, 3, 0, 12, 3),
    ("MAX_SAFE", 10, 4, 10, 0, 12, 6),
    ("MAX_SAFE", 10, 0, 10, 2, 12, 8),
    ("MAX_SAFE", 10, 0, 10, 10, 12, 1),
    ("MODERATE", 1, 0, 10, 0, 5, 1),
    ("MAX_SAFE", 10, 0, 10, 0, 2, 2),
    ("MODERATE", 10, 10, 10, 0, 5, 1),
    ("MODERATE", 4, 0, 2, 2, 5, 1),
    ("MAX_SAFE", 64, 0, 64, 0, 16, 16),
]


@pytest.mark.parametrize("mode,slots,reserve,ceiling,active,ready,offered", LADDER)
def test_each_profile_offers_its_share_of_the_slots_cut_by_ceiling_reserve_active_and_ready(
        tmp_path, mode, slots, reserve, ceiling, active, ready, offered):
    """Catches a wrong percentage, a ceiling, a reserve or an active count that is not subtracted, a width that
    passes the ready nodes, a lane lost to the floor and a round left empty: the answer is the first `offered`
    candidates, in the order given, the head always among them (the liveness floor: with the ceiling reached the
    scheduler's width is 0 and the head is still offered, one above the declared ceiling)."""
    world = make_world(tmp_path, 1)
    world.declare(mode=mode, slots=slots, reserve=reserve, ceiling=ceiling)
    ids = tuple(f"c{index}" for index in range(ready))
    runs = tmp_path / "runs"
    runs.mkdir()
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-aa", active=active)
    assert modal.declared and modal.admitted == ids[:offered]
    assert modal.gate is not None and modal.gate.judged
    assert [kind for kind, _, _ in modal.notes][:1] == ["PARALLELISM"]
    assert [node for kind, node, _ in modal.notes if kind == "PARALLELISM_WAIT"] == list(ids[offered:])


def test_the_controller_counts_the_running_nodes_of_the_project_as_active(tmp_path, monkeypatch):
    """Catches an `active` that is always 0: with MAX_SAFE on 4 slots and two of the project's nodes RUNNING, a
    round that asks for three nodes is cut to the two free lanes (the controller passes the RUNNING count)."""
    world = make_world(tmp_path, 5)
    world.declare(mode="MAX_SAFE", slots=4, ceiling=4)
    state = world.store.read_state()
    for node in state.nodes[3:]:
        node.lifecycle = Lifecycle.RUNNING
    controller = ProjectController(world.store, world.launcher(), Gates())
    steps: list[Step] = []
    kept = controller._backpressure_admission(state, state.nodes[:3], None, 1, steps)
    assert kept == {"n1", "n2"}
    assert [s.node for s in steps if s.kind == "PARALLELISM_WAIT"] == ["n3"]


def test_the_controller_applies_the_adapted_level_and_not_only_the_gate_it_asked(tmp_path):
    """Catches a controller that keeps what the gate admitted instead of what the mode admitted: the state says
    MAX_SAFE (5 lanes on 5 slots) and the declared top is MODERATE, so the offer to the gate is 5 nodes and the gate
    admits all 5, but the adaptation brings the level to MODERATE at once and the round keeps 3; n4 and n5 are
    `PARALLELISM_WAIT` steps (READY, unprepared)."""
    world = make_world(tmp_path, 5)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="MODERATE", up_after=2)
    (world.store.dir / parallelmode.STATE_FILE).write_text(json.dumps({
        "schema": parallelmode.STATE_SCHEMA, "level": "MAX_SAFE", "stable": 0, "last_change": None,
        "decision_id": "b0-0-aa", "at": "2026-10-18T00:00:00Z"}), encoding="utf-8")
    state = world.store.read_state()
    controller = ProjectController(world.store, world.launcher(), Gates())
    steps: list[Step] = []
    kept = controller._backpressure_admission(state, list(state.nodes), None, 1, steps)
    assert kept == {"n1", "n2", "n3"}
    assert [s.node for s in steps if s.kind == "PARALLELISM_WAIT"] == ["n4", "n5"]
    assert not [s for s in steps if s.kind.startswith("BACKPRESSURE_WAIT")]


def test_without_a_declaration_the_gate_is_asked_about_every_candidate_and_nothing_is_said(tmp_path):
    """Catches a mode that narrows or speaks without being asked: no `parallelism.json` is no declaration, the
    answer is the backpressure gate's over all candidates and there is no note."""
    world = make_world(tmp_path, 1)
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = ("a", "b", "c", "d")
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-aa", active=0)
    assert not modal.declared and modal.admitted == ids and modal.notes == ()
    assert modal.gate is not None and modal.gate.decision.planned == 4
    assert parallelmode.outer_width(world.store.dir) is None


# --- B3 AUTO_ADAPTIVE through the production path ----------------------------------------------------------------------

def test_hoh_project_drive_adapts_down_at_once_behind_a_limited_provider_and_the_state_says_so(tmp_path, capsys):
    """Catches an adaptation that waits for slow evidence to narrow and a state nobody keeps: AUTO_ADAPTIVE on 5
    slots starts at MODERATE (3 lanes); the provider of the launcher's runs is LIMITED by another project's run, so
    P3-07 says width 1 and `adapt_profile` goes to the floor (LINEAR) in the same round; the wave of that round is
    1, so the nodes run one at a time, the fourth node (past the three lanes) is a `PARALLELISM_WAIT`, the state
    file says level LINEAR with last change MODERATE -> LINEAR for `floor`, and the project closes."""
    world = make_world(tmp_path, 4)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    world.park("other-run")
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert CONFIG["launcher"].peak == 1 and merged(world) == {"n1", "n2", "n3", "n4"}
    assert "PARALLELISM_WAIT:n4" in out["steps"]
    state = world.state_file()
    assert (state["schema"], state["level"], state["stable"]) == (parallelmode.STATE_SCHEMA, "LINEAR", 0)
    assert {k: v for k, v in state["last_change"].items() if k != "at"} == {
        "from": "MODERATE", "to": "LINEAR", "reason": "floor"}
    assert state["decision_id"].startswith("b") and (world.store.dir / "backpressure").is_dir()


def test_hoh_project_drive_widens_one_step_after_the_stable_waves_and_never_in_the_wave_that_decided(
        tmp_path, capsys):
    """Catches a widening that is immediate, skips a rung or has no bound: AUTO_ADAPTIVE on 5 slots, top HIGH,
    up_after 1, no pressure, eight nodes. The first wave runs at MODERATE (3), its stable reading makes the step
    to HIGH but cannot widen that wave; the second runs at HIGH (4) and stays there (the top); the last node is
    alone. State: HIGH, stable 1, last change MODERATE -> HIGH for `up`."""
    world = make_world(tmp_path, 8)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=1)
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert wave_sizes(world) == [3, 4] and CONFIG["launcher"].peak == 4
    state = world.state_file()
    assert (state["level"], state["stable"]) == ("HIGH", 1)
    assert {k: v for k, v in state["last_change"].items() if k != "at"} == {
        "from": "MODERATE", "to": "HIGH", "reason": "up"}


def test_an_adaptive_project_widens_only_after_up_after_stable_waves_and_one_rung_at_a_time(tmp_path):
    """Catches a widening that is early, skips a rung or widens the wave that decided it: AUTO_ADAPTIVE on 10 slots,
    top MAX_SAFE, up_after 3, twelve ready nodes and no pressure. The waves are 7 (MODERATE), 7, 7 (the third reading
    steps up to HIGH, the wave stays), then 9, 9, 9 (the step to MAX_SAFE), then 10, 10 (the top); the stable count
    runs 1, 2, 0, 1, 2, 0, 1, 2, the state names each change and carries the id of the round's decision, whose
    record is on disk under that id."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=10, ceiling=10, top="MAX_SAFE", up_after=3)
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = tuple(f"c{index}" for index in range(12))
    sizes, stables, levels = [], [], []
    for call in range(8):
        modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                        decision_id=f"b{call}-1-aa", active=0)
        sizes.append(len(modal.admitted))
        state = world.state_file()
        stables.append(state["stable"])
        levels.append(state["level"])
        assert state["decision_id"] == f"b{call}-1-aa"                       # the id of this round's decision ...
        assert (world.store.dir / "backpressure" / f"backpressure-b{call}-1-aa.json").is_file()   # ... names its record
    assert sizes == [7, 7, 7, 9, 9, 9, 10, 10]
    assert stables == [1, 2, 0, 1, 2, 0, 1, 2]
    assert levels == ["MODERATE", "MODERATE", "HIGH", "HIGH", "HIGH", "MAX_SAFE", "MAX_SAFE", "MAX_SAFE"]
    assert world.state_file()["last_change"]["reason"] == "up"


def test_the_bound_top_brings_a_level_above_it_down_at_once(tmp_path):
    """Catches a `top` that only limits climbing: the state says MAX_SAFE, the declaration's top is MODERATE, the
    round offers by the level in force and the wave is already the MODERATE width (3 of 5 slots), the state says
    MODERATE for `above_top`."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="MODERATE", up_after=2)
    (world.store.dir / parallelmode.STATE_FILE).write_text(json.dumps({
        "schema": parallelmode.STATE_SCHEMA, "level": "MAX_SAFE", "stable": 0, "last_change": None,
        "decision_id": "b0-0-aa", "at": "2026-10-18T00:00:00Z"}), encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = tuple(f"c{index}" for index in range(5))
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-bb", active=0)
    assert modal.admitted == ids[:3]
    assert modal.gate.decision.planned == 5 and modal.gate.decision.width == 5
    state = world.state_file()
    assert (state["level"], state["last_change"]["reason"]) == ("MODERATE", "above_top")


def test_a_wave_is_never_wider_than_the_decision_just_made(tmp_path, monkeypatch):
    """Catches a wave that follows the adapted profile and ignores the backpressure decision of the same round:
    the state says HIGH, the decision of the round is DEGRADED (it halves 4 to 2) and the adaptation goes one step
    down to MODERATE (3); the wave is the lesser of the two, 2."""
    from hoh import backpressure
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    (world.store.dir / parallelmode.STATE_FILE).write_text(json.dumps({
        "schema": parallelmode.STATE_SCHEMA, "level": "HIGH", "stable": 0, "last_change": None,
        "decision_id": "b0-0-aa", "at": "2026-10-18T00:00:00Z"}), encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    real = backpressure.admit_wave

    def halved(ids, **kw):
        gate = real(ids, **kw)
        decision = dataclasses.replace(gate.decision, reason=backpressure.Reason.DEGRADED, width=2)
        return dataclasses.replace(gate, admitted=tuple(ids[:2]), waits=tuple((n, "cut") for n in ids[2:]),
                                   decision=decision)

    monkeypatch.setattr(backpressure, "admit_wave", halved)
    ids = tuple(f"c{index}" for index in range(5))
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-cc", active=0)
    assert modal.admitted == ids[:2]
    assert world.state_file()["level"] == "MODERATE"


# --- B4 fail closed: the declaration -----------------------------------------------------------------------------------

VALID = [
    # fields, the controller width the declaration makes (the widest wave it can offer, at most 16)
    ({"mode": "LINEAR", "slots": 1, "reserve": 0, "ceiling": 1}, 1),
    ({"mode": "MAX_SAFE", "slots": 64, "reserve": 64, "ceiling": 64}, 1),
    ({"mode": "MAX_SAFE", "slots": 64, "reserve": 0, "ceiling": 64}, 16),
    ({"mode": "MAX_SAFE", "slots": 20, "reserve": 0, "ceiling": 20}, 16),
    ({"mode": "MODERATE", "slots": 4, "reserve": 0, "ceiling": 4}, 2),
    ({"mode": "AUTO_ADAPTIVE", "slots": 8, "reserve": 1, "ceiling": 8, "top": "HIGH", "up_after": 16}, 7),
    ({"mode": "AUTO_ADAPTIVE", "slots": 8, "reserve": 0, "ceiling": 8, "top": "LINEAR", "up_after": 1}, 1),
]


@pytest.mark.parametrize("fields,width", VALID)
def test_a_declaration_with_exactly_the_keys_of_its_mode_is_read_and_makes_its_width(tmp_path, fields, width):
    """Catches a reader that refuses what it should read, and a width that is not the widest the declaration can
    offer: the bounds are inclusive (slots 1 to 64, reserve 0 to 64, ceiling 1 to 64, up_after 1 to 16), an adaptive
    declaration carries `top` and `up_after` and is as wide as `top`, and no declaration makes a wave wider than 16."""
    world = make_world(tmp_path, 1)
    world.declare(**fields)
    declared = parallelmode.read_declaration(world.store.dir)
    assert declared is not None and declared.mode == fields["mode"]
    assert parallelmode.outer_width(world.store.dir) == width


def test_a_declaration_or_a_state_of_exactly_4096_bytes_is_read_and_one_byte_more_is_refused(tmp_path):
    """Catches a size bound that moved: the bound is the literal 4096 for both files, so a valid document padded
    to 4096 bytes is read and the same document at 4097 bytes is refused before it is parsed."""
    from hoh.scheduler import Bounds
    world = make_world(tmp_path, 1)
    declaration = world.store.dir / parallelmode.FILE
    declaration.write_bytes(_bad().ljust(4096))
    assert parallelmode.read_declaration(world.store.dir) is not None
    declaration.write_bytes(_bad().ljust(4097))
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.read_declaration(world.store.dir)
    state = world.store.dir / parallelmode.STATE_FILE
    state.write_bytes(_state().ljust(4096))
    assert parallelmode.read_state(world.store.dir, Bounds("HIGH", 2)) is not None
    state.write_bytes(_state().ljust(4097))
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.read_state(world.store.dir, Bounds("HIGH", 2))


def _bad(**fields):
    base = {"schema": parallelmode.SCHEMA, "mode": "MODERATE", "slots": 4, "reserve": 0, "ceiling": 4}
    base.update(fields)
    return json.dumps(base).encode()


def _adaptive(**fields):
    return _bad(**{"mode": "AUTO_ADAPTIVE", "top": "HIGH", "up_after": 2, **fields})


REFUSED = {
    "not json": b"{",
    "an array": b"[]",
    "null": b"null",
    "empty": b"",
    "not utf-8": b"\xff\xfe\x00{",
    "utf-16": '{"mode": "LINEAR"}'.encode("utf-16"),
    "a NUL": b"\x00",
    "oversize": b" " * (parallelmode.MAX_BYTES + 1),
    "a duplicate key": b'{"schema": "hoh-parallelism/1", "mode": "LINEAR", "mode": "HIGH", "slots": 1, '
                       b'"reserve": 0, "ceiling": 1}',
    "NaN": _bad().replace(b"4, \"reserve\"", b"NaN, \"reserve\""),
    "a lone surrogate": b'{"schema": "hoh-parallelism/1", "mode": "\\ud800", "slots": 1, "reserve": 0, '
                        b'"ceiling": 1}',
    "deeply nested": b"[" * 3000,
    "lower case": _bad(mode="moderate"),
    "a leading space": _bad(mode=" MODERATE"),
    "a trailing newline": _bad(mode="MODERATE\n"),
    "an unknown mode": _bad(mode="MAXIMUM"),
    "a wrong schema": _bad(schema="hoh-parallelism/2"),
    "a missing key": json.dumps({"schema": parallelmode.SCHEMA, "mode": "LINEAR", "slots": 1, "ceiling": 1}).encode(),
    "an extra key": _bad(extra=1),
    "top on a fixed mode": _bad(top="HIGH"),
    "adaptive without top": json.dumps({"schema": parallelmode.SCHEMA, "mode": "AUTO_ADAPTIVE", "slots": 4,
                                        "reserve": 0, "ceiling": 4, "up_after": 2}).encode(),
    "adaptive without up_after": json.dumps({"schema": parallelmode.SCHEMA, "mode": "AUTO_ADAPTIVE", "slots": 4,
                                             "reserve": 0, "ceiling": 4, "top": "HIGH"}).encode(),
    "top is the rule": _adaptive(top="AUTO_ADAPTIVE"),
    "a bool slots": _bad(slots=True),
    "a float slots": _bad(slots=3.0),
    "a string slots": _bad(slots="3"),
    "zero slots": _bad(slots=0),
    "65 slots": _bad(slots=65),
    "65 reserve": _bad(reserve=65),
    "negative reserve": _bad(reserve=-1),
    "zero ceiling": _bad(ceiling=0),
    "65 ceiling": _bad(ceiling=65),
    "zero up_after": _adaptive(up_after=0),
    "17 up_after": _adaptive(up_after=17),
    "a huge number": _bad(slots=10 ** 400),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_declaration_that_is_not_exactly_one_is_refused(tmp_path, name):
    """Catches a reader that guesses: every malformed, odd or out-of-range declaration is `ParallelismError`,
    never a default and never a width (`outer_width` refuses too, so the controller halts by name)."""
    world = make_world(tmp_path, 1)
    (world.store.dir / parallelmode.FILE).write_bytes(REFUSED[name])
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.read_declaration(world.store.dir)
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.outer_width(world.store.dir)


def _place_odd_file(path: Path, kind: str) -> None:
    if kind == "a directory":
        path.mkdir()
    elif kind == "a dangling link":
        path.symlink_to(path.parent / "nowhere")
    elif kind == "a link to a good file":
        good = path.parent / "good.json"
        good.write_bytes(_bad())
        path.symlink_to(good)
    elif kind == "a fifo":
        os.mkfifo(path)
    elif kind == "a second name":
        other = path.parent / "other-name.json"
        other.write_bytes(_bad())
        os.link(other, path)
    elif kind == "mode 000":
        path.write_bytes(_bad())
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("this process can read a mode 000 file (it is root)")


ODD = ["a directory", "a dangling link", "a link to a good file", "a fifo", "a second name", "mode 000"]


@pytest.mark.parametrize("kind", ODD)
def test_a_declaration_that_is_not_one_regular_file_with_one_name_is_refused(tmp_path, kind):
    """Catches a name that is followed, waited on or half-trusted: a directory, a link (dangling or to a good
    file), a FIFO (which must not hang the open), a file with two names and an unreadable file are refused, and
    `hoh project drive` over any of them halts by name with nothing started."""
    world = make_world(tmp_path, 2)
    _place_odd_file(world.store.dir / parallelmode.FILE, kind)
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.read_declaration(world.store.dir)
    with pytest.raises(ValueError, match="parallelism declaration is refused"):
        ProjectController(world.store, world.launcher(), Gates())


@pytest.mark.parametrize("name", ["not json", "a bool slots", "a trailing newline"])
def test_hoh_project_drive_over_a_refused_declaration_halts_by_name_and_starts_nothing(tmp_path, capsys, name):
    """Catches a refused declaration that is a silent width 1 or a wave anyway: the drive ends AMBIGUOUS with the
    reason naming the declaration, no run was started and every node is still READY."""
    world = make_world(tmp_path, 3)
    (world.store.dir / parallelmode.FILE).write_bytes(REFUSED[name])
    rc, out = drive(world, capsys)
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    assert "the parallelism declaration is refused" in out["reason"]
    assert CONFIG["launcher"].starts == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}


def test_a_declaration_that_becomes_unreadable_between_rounds_is_the_floor_for_that_round(tmp_path):
    """Catches a declaration read once: the file is read at every wave round, so a declaration corrupted after the
    controller was built is the floor (the first node alone, `PARALLELISM_UNJUDGED` naming the file) and the other
    nodes wait; repaired, the next round offers the profile's width again."""
    world = make_world(tmp_path, 1)
    path = world.declare(mode="MAX_SAFE", slots=8, ceiling=8)
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = tuple(f"c{index}" for index in range(6))
    path.write_bytes(b"{")
    floor = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now, decision_id="b1-1-aa",
                                    active=0)
    assert floor.admitted == ids[:1] and floor.gate is None
    assert [k for k, _, _ in floor.notes][:1] == ["PARALLELISM_UNJUDGED"] and parallelmode.FILE in floor.notes[0][2]
    assert [n for k, n, _ in floor.notes if k == "PARALLELISM_WAIT"] == list(ids[1:])
    world.declare(mode="MAX_SAFE", slots=8, ceiling=8)
    again = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now, decision_id="b1-2-aa",
                                    active=0)
    assert again.admitted == ids


# --- B5 fail closed: the adaptation's state ---------------------------------------------------------------------------

def _state(**fields):
    base = {"schema": parallelmode.STATE_SCHEMA, "level": "MODERATE", "stable": 1, "last_change": None,
            "decision_id": "b3-2-ab", "at": "2026-10-18T10:00:00Z"}
    base.update(fields)
    return json.dumps(base).encode()


STATE_REFUSED = {
    "not json": b"{",
    "an array": b"[]",
    "oversize": b" " * (parallelmode.MAX_BYTES + 1),
    "a wrong schema": _state(schema="hoh-parallelism-state/2"),
    "an unknown level": _state(level="AUTO_ADAPTIVE"),
    "a lower case level": _state(level="moderate"),
    "stable above 16": _state(stable=17),
    "a negative stable": _state(stable=-1),
    "a bool stable": _state(stable=True),
    "an extra key": _state(extra=1),
    "a bad change": _state(last_change={"at": "x"}),
    "a change to nowhere": _state(last_change={"at": "2026-10-18T10:00:00Z", "from": "HIGH", "to": "UP",
                                                "reason": "up"}),
    "a bad decision id": _state(decision_id="../b"),
    "a decision id with a newline": _state(decision_id="b3-2-ab\n"),
    "a bad time": _state(at="yesterday"),
    "a time with a newline": _state(at="2026-10-18T10:00:00Z\n"),
    "a level with a newline": _state(level="MODERATE\n"),
}


@pytest.mark.parametrize("name", sorted(STATE_REFUSED))
def test_a_state_file_that_is_not_exactly_what_write_state_produces_is_refused(tmp_path, name):
    """Catches a state read loosely: `read_state` refuses every one of these, and accepts what `write_state`
    wrote (round trip, below) -- a state that cannot be trusted is never widened from."""
    world = make_world(tmp_path, 1)
    bounds = Bounds("HIGH", 2)
    (world.store.dir / parallelmode.STATE_FILE).write_bytes(STATE_REFUSED[name])
    with pytest.raises(parallelmode.ParallelismError):
        parallelmode.read_state(world.store.dir, bounds)


def test_write_state_and_read_state_round_trip_and_keep_the_last_change_until_the_next_change(tmp_path):
    """Catches a last change that is lost on a quiet reading or invented on one: a change is recorded with its
    reason, a following stable reading keeps it and counts, and the file is replaced whole each time."""
    world = make_world(tmp_path, 1)
    bounds = Bounds("HIGH", 2)
    assert parallelmode.read_state(world.store.dir, bounds) is None
    held = Adaptive("MODERATE", 0)
    down = adapt_profile(held, "DEGRADED", bounds)
    parallelmode.write_state(world.store.dir, held, down, "b1-1-aa")
    assert parallelmode.read_state(world.store.dir, bounds) == Adaptive("CONSERVATIVE", 0)
    first = world.state_file()
    assert first["last_change"]["reason"] == "down" and first["decision_id"] == "b1-1-aa"
    quiet = adapt_profile(down.state, "NONE", bounds)
    parallelmode.write_state(world.store.dir, down.state, quiet, "b2-1-ab")
    second = world.state_file()
    assert (second["level"], second["stable"], second["last_change"]) == ("CONSERVATIVE", 1, first["last_change"])


@pytest.mark.parametrize("name", ["not json", "stable above 16", "an unknown level"])
def test_hoh_project_drive_with_an_unreadable_state_runs_at_the_floor_and_leaves_the_file_as_found(
        tmp_path, capsys, name):
    """Catches an unreadable state that is rewritten, trusted or fatal: every wave round is the floor (the head
    alone, one run at a time), says `PARALLELISM_UNJUDGED` naming the state file, the file's bytes are unchanged
    and the project still closes -- no round is empty, so the rounds are bounded by the nodes."""
    world = make_world(tmp_path, 3)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    path = world.store.dir / parallelmode.STATE_FILE
    path.write_bytes(STATE_REFUSED[name])
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert CONFIG["launcher"].peak == 1 and merged(world) == {"n1", "n2", "n3"}
    assert steps_of(out, "PARALLELISM_UNJUDGED") and not steps_of(out, "PARALLELISM")
    assert path.read_bytes() == STATE_REFUSED[name]


def test_a_state_that_cannot_be_written_is_the_floor_and_says_so_and_the_project_closes(tmp_path, monkeypatch):
    """Catches an adaptation that is applied but not kept, or a write that fails the drive: with the state write
    refused (no room), every wave round is the floor, says the state cannot be written, no state file appears
    and the project closes node by node (each round runs at least its head, so three nodes need three rounds)."""
    def full(*_args, **_kw):
        raise OSError(errno.ENOSPC, "no room")

    monkeypatch.setattr(parallelmode, "write_state", full)
    world = make_world(tmp_path, 3)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates()).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert launcher.peak == 1 and merged(world) == {"n1", "n2", "n3"} and result.rounds <= 4
    unjudged = [s for s in result.steps if s.kind == "PARALLELISM_UNJUDGED"]
    assert unjudged and all("cannot be written" in s.detail and parallelmode.STATE_FILE in s.detail
                            for s in unjudged)
    assert not (world.store.dir / parallelmode.STATE_FILE).exists()


def test_an_unreadable_state_names_its_file_and_a_declaration_names_its_own(tmp_path):
    """Catches a floor that does not say why: the note of a state that is refused names `parallelism-state.json` and
    says it is left as found; the first node alone is admitted and the others are `PARALLELISM_WAIT`."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    (world.store.dir / parallelmode.STATE_FILE).write_bytes(b"{")
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = ("a", "b", "c")
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-aa", active=0)
    assert modal.admitted == ("a",) and modal.gate is None
    assert modal.notes[0][0] == "PARALLELISM_UNJUDGED"
    assert parallelmode.STATE_FILE in modal.notes[0][2] and "left as found" in modal.notes[0][2]
    assert [n for k, n, _ in modal.notes if k == "PARALLELISM_WAIT"] == ["b", "c"]


def test_a_further_drive_of_a_closed_project_writes_nothing_of_the_modes(tmp_path, capsys):
    """Catches a terminal record that is churned: after the drive that closes the project, a second drive ends
    CLOSED and leaves the declaration, the state file and the backpressure records byte for byte as they were."""
    world = make_world(tmp_path, 3)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=1)
    rc, out = drive(world, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out

    def snapshot():
        files = [world.store.dir / parallelmode.FILE, world.store.dir / parallelmode.STATE_FILE,
                 *sorted((world.store.dir / "backpressure").glob("*.json"))]
        return {p.name: p.read_bytes() for p in files}

    before = snapshot()
    assert parallelmode.STATE_FILE in before
    rc, out = drive(world, capsys, session="sB")
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert snapshot() == before


# --- B6 review p325wspec: the unjudged decision, a raising gate, a typed width, a lowered up_after, the fenced write ---------

def _unjudged_gate(world, tmp_path):
    """The gate when no decision can be taken: `backpressure.admit_wave` over a runs root that is not there answers the
    floor (`judged=False`, no decision)."""
    from hoh import backpressure

    return lambda offered, decision_id: backpressure.admit_wave(
        offered, launcher=types.SimpleNamespace(root=tmp_path / "no-such-runs-root"),
        record_dir=world.store.dir / "backpressure", decision_id=decision_id)


def test_a_decision_that_could_not_be_taken_steps_the_level_down_and_never_up(tmp_path):
    """Catches an unreadable pressure read as a stable reading (fail-open: it would count towards, and make, a step
    up): AUTO_ADAPTIVE on 8 slots, top HIGH, up_after 2, six candidates and a gate that cannot judge. The wave is the head
    alone; the state goes MODERATE -> CONSERVATIVE with the reason `unreadable`, then LINEAR, and stays there on every
    further round (it never widens) -- the stable count stays 0."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=8, ceiling=8, top="HIGH", up_after=2)
    ids = ("a", "b", "c", "d", "e", "f")
    gate = _unjudged_gate(world, tmp_path)
    levels, stables = [], []
    for call in range(4):
        modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate, fenced=run_now,
                                        decision_id=f"b{call}-1-aa", active=0)
        assert modal.admitted == ("a",), (call, modal.admitted)
        state = world.state_file()
        levels.append(state["level"])
        stables.append(state["stable"])
        if call == 0:
            change = state["last_change"]
            assert change is not None and (change["from"], change["reason"]) == ("MODERATE", "unreadable")
    assert levels == ["CONSERVATIVE", "LINEAR", "LINEAR", "LINEAR"] and stables == [0, 0, 0, 0]


def test_a_gate_that_raises_is_the_floor_not_a_crash(tmp_path):
    """Catches a judging error that escapes into the loop: whatever the gate raises, the round is the head alone with a
    `PARALLELISM_UNJUDGED` note naming the exception, and no state is written for it."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=8, ceiling=8, top="HIGH", up_after=2)

    def blows_up(offered, decision_id):
        raise RuntimeError("the gate blew up")

    modal = parallelmode.admit_wave(("a", "b", "c"), project_dir=world.store.dir, gate=blows_up, fenced=run_now,
                                    decision_id="b1-1-aa", active=0)
    assert modal.admitted == ("a",) and modal.gate is None
    assert modal.notes[0][0] == "PARALLELISM_UNJUDGED" and "RuntimeError" in modal.notes[0][2]
    assert not (world.store.dir / parallelmode.STATE_FILE).exists()


def test_a_typed_width_with_a_refused_declaration_is_the_floor_and_not_a_halt(tmp_path):
    """Catches a declaration read eagerly even when the operator typed a width: with `width=3` and a declaration that
    is refused, the controller is built (no `ValueError`), the declaration is the floor at the wave round
    (`PARALLELISM_UNJUDGED`), one run at a time, and the project closes."""
    world = make_world(tmp_path, 3)
    (world.store.dir / parallelmode.FILE).write_bytes(b"{")
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert launcher.peak == 1 and merged(world) == {"n1", "n2", "n3"}
    assert any(s.kind == "PARALLELISM_UNJUDGED" and parallelmode.FILE in s.detail for s in result.steps)


def test_a_stored_stable_count_above_a_lowered_up_after_is_clamped_and_not_stranded(tmp_path):
    """Catches a state that the operator's own edit locks out: the state says stable 5 (written under up_after 8), the
    declaration is lowered to up_after 2; the round is judged, not floored; the count is clamped to 2, so the stable
    reading steps the level up (MODERATE -> HIGH) and the state is rewritten with stable 0."""
    world = make_world(tmp_path, 1)
    world.declare(mode="AUTO_ADAPTIVE", slots=10, ceiling=10, top="HIGH", up_after=2)
    (world.store.dir / parallelmode.STATE_FILE).write_bytes(_state(level="MODERATE", stable=5))
    runs = tmp_path / "runs"
    runs.mkdir()
    ids = tuple(f"c{index}" for index in range(8))
    modal = parallelmode.admit_wave(ids, project_dir=world.store.dir, gate=gate_of(world, runs), fenced=run_now,
                                    decision_id="b1-1-aa", active=0)
    assert modal.notes[0][0] == "PARALLELISM" and modal.admitted == ids[:7]
    state = world.state_file()
    assert (state["level"], state["stable"], state["last_change"]["reason"]) == ("HIGH", 0, "up")


class _Authority:
    """A master authority stub: `check` raises `StaleMaster` when `stale`."""

    def __init__(self, stale):
        self.stale = stale
        self.checked = 0

    def check(self):
        from hoh.succession import StaleMaster
        self.checked += 1
        if self.stale:
            raise StaleMaster("superseded")


def test_a_superseded_master_writes_no_adaptation_state_and_the_current_one_does(tmp_path):
    """Catches a shared state file written outside the master fence (a superseded master could overwrite its
    successor's adaptation): with an authority that is stale, the admission raises `StaleMaster` (the controller turns it
    into the SUPERSEDED halt), no state file exists and no node is admitted; with a current authority the epoch is
    checked once and the state is written."""
    from hoh.succession import StaleMaster

    world = make_world(tmp_path, 5)
    world.declare(mode="AUTO_ADAPTIVE", slots=5, ceiling=5, top="HIGH", up_after=2)
    state = world.store.read_state()
    stale = _Authority(True)
    controller = ProjectController(world.store, world.launcher(), Gates(), authority=stale)
    with pytest.raises(StaleMaster):
        controller._backpressure_admission(state, list(state.nodes), None, 1, [])
    assert stale.checked == 1 and not (world.store.dir / parallelmode.STATE_FILE).exists()
    current = _Authority(False)
    controller = ProjectController(world.store, world.launcher(), Gates(), authority=current)
    controller._backpressure_admission(state, list(state.nodes), None, 1, [])
    assert current.checked == 1 and (world.store.dir / parallelmode.STATE_FILE).is_file()


def test_the_register_reads_p3_25_and_p3_26_proven_on_this_tree():
    """Catches an omitted or wrong register edit (the behaviour nodes cannot): `capability_matrix.classify`, with this
    tree's own symbols, collected tests and import graph and its own `REQUIREMENTS.json`, reads P3-25 and P3-26 PROVEN,
    neither probe carries an `equivalent`, and each names a node of this file that is collected."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("capability_matrix_p325w", root / "tools" / "capability_matrix.py")
    matrix = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = matrix
    spec.loader.exec_module(matrix)
    register = json.loads((root / "program" / "v3_3" / "REQUIREMENTS.json").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in register["requirements"]}
    symbols, collection, graph = matrix._symbols(), matrix._collected(), matrix._import_graph()
    for row_id, symbol in (("P3-25", "profile_width"), ("P3-26", "adapt_profile")):
        probe = rows[row_id]["probe"]
        assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", symbol, "scheduler.py")
        assert "equivalent" not in probe and probe["test"].startswith("tests/test_parallelism_modes.py::")
        verdict = matrix.classify(rows[row_id], symbols, collection, {}, graph)
        assert verdict["status"] == "PROVEN", (row_id, verdict["status"], verdict["evidence"])
