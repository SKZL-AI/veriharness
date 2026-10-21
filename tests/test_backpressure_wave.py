"""P3-07 slice 2: the wave's admission asks the backpressure decision, per provider.

Before the project loop prepares and launches a wave, the nodes it would start (after the capacity gate) go
through `backpressure.admit_wave`: the health of the runs root is read, the decision is scoped to the providers the
launcher's runs execute their roles on, written to `<project>/backpressure/backpressure-<id>.json` and only then
used. A node past the decided width waits READY and unprepared with its reason; the first node always runs; what
cannot be judged is the floor, width 1, and says so.

What is real: `ProjectController`, `ParallelWaveExecutor`, `HohRunLauncher` (only its process boundary replaced,
as in `tests/test_wave_capacity.py`: no agent, no Herdr, no network), `hoh project drive` through `cli.main`, the
project store, a git repository with real linked worktrees, and **real parked runs** written by the product's own
`RunStore`, `RunState` and `dispatchrecovery.append_record` under the runs root. Every test builds its world under
`tmp_path` and removes nothing.
"""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import GateRunner, HaltClass, MergeResult, ProjectController
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
CONFIG: dict = {}
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-12T12:00:00Z"


def _bp():
    from hoh import backpressure
    return backpressure


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
    def __init__(self, tmp_path: Path, names=("n1", "n2", "n3")) -> None:
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
            nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"]))
        self.store = ProjectStore(self.root, "p")
        self.store.create(ProjectState(project_id="p", repo_path=str(self.repo), nodes=nodes))

    def launcher(self, **kw) -> Launcher:
        return Launcher(self.root, self.repo, **kw)

    def park(self, run_id: str, *, provider: str = "claude", account: str = "acct-a",
             failure=None, retry: str | None = None, receipt: bool = True) -> None:
        """A run of another project parked on a provider limit, with the receipt the controller writes
        (`receipt=False`: a park no receipt describes -- `health` reads it as an unattributed wait)."""
        from hoh import dispatchrecovery
        from hoh.dispatchrecovery import (
            NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id)
        from hoh.failures import NormalizedFailure

        failure = NormalizedFailure.USAGE_LIMIT if failure is None else failure
        retry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() if retry is None else retry
        st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="other",
                      spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                      profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED, iteration=1)
        st.blocked_kind = failure.value.lower()
        st.retry_after = retry
        st.blocked_reason = "parked"
        store = RunStore(self.root, run_id)
        store.write_state(st)
        if not receipt:
            return
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=1,
                              attempt=st.attempt, stage=st.stage.value, candidate="NONE", master=NO_MASTER)
        dispatchrecovery.append_record(store.dir, RoutingDecision(
            park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
            provenance=DispatchProvenance(
                provider=provider, model="m-1", account_handle=account, role="developer",
                capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=retry))

    def records(self) -> list:
        directory = self.store.dir / "backpressure"
        if not directory.exists():
            return []
        return [_bp().read_decision(p) for p in sorted(directory.iterdir())]


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


def build_driver(store):
    """The `driver.json` factory of the CLI tests: the real launcher with a fake process boundary."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], **cfg.get("kw", {}))
    cfg["launcher"] = launcher
    return launcher, Gates()


def _drive(world, monkeypatch, capsys, *, width: str | None = "3", **launcher_kw):
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, kw=launcher_kw)
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_backpressure_wave:build_driver", "path": str(Path(__file__).resolve().parent)}))
    if width is not None:
        monkeypatch.setenv("HOH_WAVE_WIDTH", width)
    rc = cli.main(["--root", str(world.root), "project", "drive", "p", "--session", "sA"])
    return rc, json.loads(capsys.readouterr().out)


# --- B1 the production path: a limited provider narrows the wave, and the project still closes --------------------

def test_hoh_project_drive_narrows_a_wave_behind_a_limited_provider_and_still_closes(world, monkeypatch, capsys):
    """The production path, end to end: `hoh project drive` (cli.main) -> `resumeloop.drive_governed` ->
    `ProjectController.run` -> `_wave_round` -> `_backpressure_admission` -> `backpressure.admit_wave` ->
    `BackpressureController.admit_width` -> `decide`. Catches a gate only the controller's own tests reach, a
    wave that spends dispatches on a provider that has said no, and a gate that loops or halts: with the launcher's
    provider (claude) limited by another project's run, three independent nodes at width 3 run **one per round**,
    each cut node is a `BACKPRESSURE_WAIT` step (READY, never prepared), a decision file per wave round says
    LIMITED, width 1 of 3, provider claude, scope claude, and the project closes with every node merged."""
    world.park("other-run")
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    steps = out["steps"]
    assert [s for s in steps if s.startswith("BACKPRESSURE:")] == ["BACKPRESSURE:", "BACKPRESSURE:"]
    assert [s for s in steps if s.startswith("BACKPRESSURE_WAIT:")] == [
        "BACKPRESSURE_WAIT:n2", "BACKPRESSURE_WAIT:n3", "BACKPRESSURE_WAIT:n3"]    # round 1: both; round 2: n3 again
    assert not [s for s in steps if s.startswith("BACKPRESSURE_UNJUDGED")]
    launcher = CONFIG["launcher"]
    assert sorted(launcher.merged) == ["n1", "n2", "n3"] and launcher.peak == 1
    assert [len(w.nodes) for w in waves(world)] == [1, 1]            # n1 alone, then n2 alone, then n3 sequentially
    first, second = world.records()
    for rec, planned in ((first, 3), (second, 2)):
        d = rec.decision
        assert (d.reason.value, d.width, d.planned, d.provider, d.account_handle) \
            == ("LIMITED", 1, planned, "claude", "acct-a")
        assert d.scope == ("claude",) and d.groups == 1 and d.ignored == 0 and d.report_at is not None
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.MERGED}


def test_a_cut_node_is_never_prepared_holds_nothing_and_is_asked_again(world):
    """Catches a refusal that is a block, a hold or a lost node: with the provider limited and width 3 the first
    round names n1 only, `prepared_hold`/`prepare` are asked for n1 alone (a cut node has no hold and no run),
    n2 and n3 stay READY and are prepared in later rounds in plan order, once each; the project closes in three
    rounds of one node (no round is empty, no node is skipped for good)."""
    world.park("other-run")
    launcher = world.launcher()
    touched: list[str] = []
    original = launcher.prepared_hold
    launcher.prepared_hold = lambda node: (touched.append(node.id), original(node))[1]
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert touched[0] == "n1" and touched.count("n1") == 1 and touched.count("n2") == 1
    assert launcher.starts == ["n1", "n2", "n3"]
    assert kinds(result, "BACKPRESSURE_WAIT")[0].node == "n2" and kinds(result, "BACKPRESSURE_WAIT")[0].round == 1
    assert "LIMITED" in kinds(result, "BACKPRESSURE_WAIT")[0].detail
    assert result.rounds <= 4


# --- B2 per provider: another provider's limit narrows nothing --------------------------------------------------------

def test_a_limit_on_a_provider_the_runs_do_not_use_narrows_nothing_and_is_recorded_as_ignored(world, monkeypatch,
                                                                                            capsys):
    """Catches a gate that is per root and not per provider: the same world with the limit on `kimi` (the launcher
    runs its roles on claude) launches all three nodes in the first wave, there is no `BACKPRESSURE` step, and the
    decision file says NONE, width 3 of 3, scope claude, one group ignored: the evidence that was not used is
    in the record."""
    world.park("other-run", provider="kimi")
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert not [s for s in out["steps"] if s.startswith("BACKPRESSURE")]
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1", "n2", "n3"]
    [rec] = world.records()
    d = rec.decision
    assert (d.reason.value, d.width, d.planned, d.scope, d.groups, d.ignored) \
        == ("NONE", 3, 3, ("claude",), 0, 1)


def test_the_routing_policy_provider_joins_the_scope_of_the_launchers_runs(world, tmp_path):
    """Catches a scope read from the launcher's words only: a policy that routes the developer to codex puts
    codex in scope beside claude (planner and qa), so a limit on codex narrows the wave while the same limit on
    kimi does not; a policy that cannot be read is no scope at all (every provider counts)."""
    bp = _bp()
    policy = tmp_path / "routing.json"
    policy.write_text(json.dumps({"schema": "hoh-routing-policy/1",
                                  "roles": {"developer": {"provider": "codex", "model": "gpt-5", "effort": "high"}}}))
    routed = world.launcher(routing_policy=policy)
    assert bp.providers_of(routed) == ("claude", "codex")
    assert bp.providers_of(world.launcher()) == ("claude",)
    assert bp.providers_of(world.launcher(planner="kimi", developer="pi", qa="kimi")) == ("kimi", "pi")
    world.park("limited-codex", provider="codex")
    result = ProjectController(world.store, routed, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert [n.node for n in kinds(result, "BACKPRESSURE_WAIT")] == ["n2", "n3", "n3"]
    first = world.records()[0].decision
    assert first.scope == ("claude", "codex") and first.provider == "codex" and first.width == 1
    model_only = tmp_path / "model-only.json"            # a route with no provider keeps the launcher's word
    model_only.write_text(json.dumps({"schema": "hoh-routing-policy/1",
                                      "roles": {"developer": {"model": "gpt-5", "effort": "high"}}}))
    assert bp.providers_of(world.launcher(routing_policy=model_only, developer="kimi")) == ("claude", "kimi")
    policy.write_bytes(b"{not json")
    assert bp.providers_of(routed) is None
    assert bp.providers_of(object()) is None
    assert bp.providers_of(world.launcher(planner="bad name\n")) is None


# --- B3 the capacity gate and this one compose: both only narrow, in one order -----------------------------------

def test_capacity_and_backpressure_both_cut_the_same_prefix_and_neither_reads_the_others_facts(world):
    """Catches a duplicated or reordered gate: a registry of 2 sessions admits n1 and n2 (n3 is a `CAPACITY_WAIT`),
    the limited provider then cuts that to n1 (n2 is a `BACKPRESSURE_WAIT`): the first wave names n1 alone, the
    step order in round 1 is CAPACITY_WAIT, CAPACITY, BACKPRESSURE, BACKPRESSURE_WAIT; and with no registry the
    capacity opt-out is said and only backpressure cuts."""
    from hoh import capacity
    from hoh.storage_registry import StorageRegistry
    state = world.tmp / "state"
    StorageRegistry(state).init()
    capacity.write((state / "capacity-registry.json"), [capacity.Entry(
        kind="machine", name="host.sessions", limit=2, unit="sessions", source="declared",
        as_of=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))])
    world.park("other-run")
    launcher = world.launcher(storage_root=state)
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    round_one = [(s.kind, s.node) for s in result.steps if s.round == 1 and s.kind.startswith(("CAPACITY", "BACKPRESS"))]
    assert round_one == [("CAPACITY_WAIT", "n3"), ("CAPACITY", None), ("BACKPRESSURE", None),
                         ("BACKPRESSURE_WAIT", "n2")]
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1"]
    d = world.records()[0].decision                      # planned is the wave about to run (2 after capacity), not 3
    assert (d.planned, d.width, d.reason.value) == (2, 1, "LIMITED")
    other = World(world.tmp / "again")
    other.park("other-run")
    result = ProjectController(other.store, other.launcher(), Gates(), width=3).run()
    assert [s.kind for s in result.steps if s.round == 1 and s.kind.startswith(("CAPACITY", "BACKPRESS"))] == [
        "CAPACITY_OPT_OUT", "BACKPRESSURE", "BACKPRESSURE_WAIT", "BACKPRESSURE_WAIT"]


# --- B4 what cannot be judged is the floor, visibly -------------------------------------------------------------

def test_evidence_that_cannot_be_read_and_a_record_that_cannot_be_written_are_the_floor_and_say_so(world, tmp_path):
    """Catches a fail-open and a stall: a run directory under the runs root whose `state.json` is damaged makes
    the reading incomplete, so the wave is width 1 with the decision `INCOMPLETE` (judged, on disk); a record
    directory that is a file (nothing can be written) is `BACKPRESSURE_UNJUDGED` with width 1 -- the first node
    runs, the others wait READY -- and in both worlds the project closes with every node merged."""
    broken = world.root / "broken-run"
    broken.mkdir()
    (broken / "state.json").write_text("{not json")
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert sorted(launcher.merged) == ["n1", "n2", "n3"] and launcher.peak == 1
    d = world.records()[0].decision
    assert (d.reason.value, d.width, d.planned, d.problems) == ("INCOMPLETE", 1, 3, 1)
    assert "INCOMPLETE" in kinds(result, "BACKPRESSURE")[0].detail

    aside = World(tmp_path / "aside")                    # a link to a directory is not a record directory either
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (aside.store.dir / "backpressure").symlink_to(elsewhere)
    launcher = aside.launcher()
    result = ProjectController(aside.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED and kinds(result, "BACKPRESSURE_UNJUDGED"), result.reason
    assert list(elsewhere.iterdir()) == [] and launcher.peak == 1

    again = World(tmp_path / "again")
    (again.store.dir / "backpressure").write_text("not a directory")
    launcher = again.launcher()
    result = ProjectController(again.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert [s.node for s in kinds(result, "BACKPRESSURE_WAIT")][:2] == ["n2", "n3"]
    [unjudged, *_] = kinds(result, "BACKPRESSURE_UNJUDGED")
    assert "not a directory" in unjudged.detail and unjudged.round == 1
    assert sorted(launcher.merged) == ["n1", "n2", "n3"] and launcher.peak == 1


# --- the scoped gate still counts a park nobody can name ------------------------------------------------------------

def test_a_park_no_receipt_names_narrows_a_scoped_wave_and_a_limit_on_another_provider_still_does_not(world):
    """Catches the fail-open of a scope that also drops what cannot be attributed (review B, B1): another project's
    run parked on a limit with **no receipt** (so `health` cannot name its provider) beside a run parked on kimi,
    the launcher on claude, width 3: the wave is cut to n1 (a decision LIMITED, no provider, `unattributed` 1, scope
    claude, the kimi group ignored) -- while the kimi park alone (previous nodes) narrowed nothing; the project closes."""
    world.park("nameless", receipt=False)
    world.park("on-kimi", provider="kimi")
    launcher = world.launcher()
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert [n.node for n in kinds(result, "BACKPRESSURE_WAIT")] == ["n2", "n3", "n3"] and launcher.peak == 1
    d = world.records()[0].decision
    assert (d.reason.value, d.width, d.planned, d.provider, d.unattributed, d.groups, d.ignored, d.scope) \
        == ("LIMITED", 1, 3, "", 1, 0, 1, ("claude",))
    assert "(LIMITED); providers claude" in kinds(result, "BACKPRESSURE")[0].detail      # no provider is named


def test_a_second_drive_of_one_project_takes_its_own_decisions(world):
    """Catches a decision id that is only the round (review B, MINOR 2): two drives of one project of a round each
    (`max_rounds=1`, the round counter restarts) both write a record and neither is `BACKPRESSURE_UNJUDGED`."""
    world.park("other-run")
    for _ in range(2):
        result = ProjectController(world.store, world.launcher(), Gates(), width=3, max_rounds=1).run()
        assert not kinds(result, "BACKPRESSURE_UNJUDGED"), result.steps
        assert kinds(result, "BACKPRESSURE")
    records = world.records()
    assert len(records) == 2 and all(r.decision.reason.value == "LIMITED" for r in records)
    assert len({r.decision_id for r in records}) == 2


# --- B5 the gate itself ---------------------------------------------------------------------------------------------

def test_admit_wave_is_a_prefix_never_empty_never_raises_and_names_what_it_judged(world):
    """Catches a gate that can answer nobody, a non-prefix, or an exception into the loop: no pressure admits all
    in order with the record named; a limit on the launcher's provider admits the first alone; lists that are
    empty, hold a non-string, a duplicate, or 17 ids; a runs root that is not there or is a file; and a launcher
    with no `root` are answered, never raised, with the first node alone and `judged` False (an empty or
    non-string list admits nothing); two ids that no scope can tell apart are kept apart."""
    bp = _bp()
    launcher = world.launcher()
    record_dir = world.store.dir / "backpressure"

    def gate(ids, n, **kw):
        return bp.admit_wave(ids, launcher=kw.pop("launcher", launcher), record_dir=kw.pop("record_dir", record_dir),
                             decision_id=f"t{n}", **kw)

    ok = gate(["a", "b", "c"], 1)
    assert (ok.admitted, ok.waits, ok.judged, ok.record) == (("a", "b", "c"), (), True, "backpressure-t1.json")
    assert ok.decision.reason is bp.Reason.NONE and (record_dir / "backpressure-t1.json").is_file()
    world.park("other-run")
    cut = gate(["a", "b", "c"], 2)
    assert cut.admitted == ("a",) and [n for n, _ in cut.waits] == ["b", "c"] and cut.judged
    assert cut.why.startswith("backpressure: width 1 of 3 (LIMITED claude/acct-a); providers claude")
    assert all(w.startswith("BACKPRESSURE/LIMITED: width 1 of 3 claude/acct-a") for _, w in cut.waits)
    assert bp.read_decision(record_dir / cut.record).decision == cut.decision

    for n, ids in enumerate([[], (), ["a", 7], ["a", None]], start=3):
        g = gate(ids, n)
        assert (g.admitted, g.waits, g.judged, g.decision, g.record) == ((), (), False, None, None), ids
    assert gate(object(), 8).admitted == ()
    for n, ids in enumerate([["a", "a"], [f"n{i}" for i in range(17)]], start=9):
        g = gate(ids, n)
        assert g.admitted == (ids[0],) and not g.judged and g.decision is None and g.record is None
        assert [w for _, w in g.waits] == [g.waits[0][1]] * len(g.waits) if g.waits else True
    assert gate([f"n{i}" for i in range(16)], 11).judged and len(
        gate([f"m{i}" for i in range(16)], 12).admitted) == 1
    assert gate(["a", "b"], 13, record_dir=world.tmp / "no-such" / "dir").admitted == ("a",)
    nul = gate(["a", "b"], 20, record_dir=str(record_dir) + "\x00x")        # a NUL in a path is a floor, not a ValueError
    assert nul.admitted == ("a",) and not nul.judged and nul.decision is None
    assert gate(["a", "b"], 14, launcher=object()).judged is False
    lone = world.tmp / "a-file"
    lone.write_text("x")
    import stat as stat_mod
    assert stat_mod.S_IMODE(record_dir.stat().st_mode) == 0o700       # created by the gate, owner only
    link = world.tmp / "root-link"
    link.symlink_to(world.root)                           # a runs root that is a link is not read (lstat, not stat)
    linked = type("L", (), {"root": link, "planner": "claude", "developer": "claude", "qa": "claude"})()
    g = gate(["a", "b"], 21, launcher=linked)
    assert g.admitted == ("a",) and not g.judged and "not a directory" in g.why
    assert gate(["a", "b"], 22, launcher=type("L", (), {"root": world.root, "planner": "kimi",
                "developer": "kimi", "qa": "kimi"})()).judged is True     # the same root, not a link: judged
    fake = type("L", (), {"root": lone, "planner": "claude", "developer": "claude", "qa": "claude"})()
    g = gate(["a", "b"], 15, launcher=fake)
    assert g.admitted == ("a",) and not g.judged and "not a directory" in g.why
    missing = type("L", (), {"root": world.tmp / "no-root", "planner": "claude", "developer": "claude",
                             "qa": "claude"})()
    assert gate(["a", "b"], 16, launcher=missing).judged is False
    assert gate(["a", "b"], 17, now=lambda: "yesterday").judged is False
    assert gate(["a", "b"], 17, now=lambda: NOW.replace(tzinfo=None)).judged is False
    assert gate(["a", "b"], 18, launcher=world.launcher(planner="kimi")).admitted == ("a",)        # claude is still in scope
    assert gate(["a", "b"], 19, launcher=world.launcher(planner="kimi", developer="kimi", qa="kimi")
                ).admitted == ("a", "b")                                  # the limit is on claude: out of scope
    assert not (record_dir / "backpressure-t9.json").exists()


def test_a_provider_is_in_scope_only_by_its_exact_name_and_the_rest_of_the_reading_always_counts(world):
    """Catches a scope that is looser or stricter than its words: a group of `Claude`, `claude ` or `claude-2` is
    outside the scope `claude` (ignored, counted, not a pressure); an unattributed wait, a reading with a problem
    and a stale reading narrow whatever the scope; a scope of a string, an empty list, 17 names, a non-name, or
    a `bytes` is a `BackpressureError`; a set, a tuple and a list of names are one scope, sorted and distinct."""
    from hoh.health import HealthReport, HealthState, ProviderHealth
    bp = _bp()

    def group(provider, state=HealthState.LIMITED):
        return ProviderHealth(provider=provider, account_handle="a", state=state, not_before=None,
                              runs=("r",), failures=(("USAGE_LIMIT", 1),))

    def report(groups=(), problems=(), at=None, loose=()):
        return HealthReport(at=NOW.isoformat() if at is None else at, providers=tuple(groups),
                            unattributed=tuple(loose), problems=tuple(problems), runs_seen=0)

    def wait(**kw):
        from hoh.failures import NormalizedFailure
        from hoh.health import Evidence, Wait
        base = dict(run_id="u", failure=NormalizedFailure.USAGE_LIMIT, evidence=Evidence.LIMIT, retry_at=None,
                    due=False, provider="", account_handle="", model="", unattributed="no receipt describes this park",
                    given_up=False)
        base.update(kw)
        return Wait(**base)

    for variant in ("Claude", "claude ", "claude-2", "CLAUDE", "clau"):
        got = bp.decide(8, report([group(variant)]), now=NOW, providers={"claude"})
        assert (got.reason, got.width, got.groups, got.ignored, got.scope) \
            == (bp.Reason.NONE, 8, 0, 1, ("claude",)), variant
    assert bp.decide(8, report([group("claude\u00a0")]), now=NOW, providers={"claude"}).reason \
        is bp.Reason.INCOMPLETE                      # not a name at all: the reading is not trusted
    inside = bp.decide(8, report([group("claude"), group("kimi", HealthState.DEGRADED)]), now=NOW,
                       providers=["claude", "codex", "claude"])
    assert (inside.reason, inside.width, inside.provider, inside.groups, inside.ignored, inside.scope) \
        == (bp.Reason.LIMITED, 1, "claude", 1, 1, ("claude", "codex"))
    assert bp.decide(8, report([group("claude")]), now=NOW).scope is None
    assert bp.decide(8, report([group("kimi")]), now=NOW, providers=("claude",)).width == 8
    # an unattributed wait may be the provider the wave uses: it counts whatever the scope (review B, B1)
    from hoh.health import Evidence
    for kw, reason, width in (({}, bp.Reason.LIMITED, 1), (dict(evidence=Evidence.AVAILABILITY), bp.Reason.DEGRADED, 4),
                              (dict(due=True), bp.Reason.RESET_DUE, 4), (dict(given_up=True), bp.Reason.GIVEN_UP, 1)):
        got = bp.decide(8, report([group("kimi")], loose=[wait(**kw)]), now=NOW, providers={"claude"})
        assert (got.reason, got.width, got.provider, got.unattributed, got.groups, got.ignored, got.scope) \
            == (reason, width, "", 1, 0, 1, ("claude",)), kw
    mixed = bp.decide(8, report([group("claude", HealthState.DEGRADED)], loose=[wait()]), now=NOW, providers=["claude"])
    assert (mixed.reason, mixed.width, mixed.groups, mixed.unattributed) == (bp.Reason.LIMITED, 1, 1, 1)
    problem = bp.decide(8, report([group("kimi")], problems=[("r", "damaged")]), now=NOW, providers={"claude"})
    assert (problem.reason, problem.width, problem.groups, problem.ignored) == (bp.Reason.INCOMPLETE, 1, 0, 1)
    stale = bp.decide(8, report(at="2026-10-12T11:00:00+00:00"), now=NOW, providers={"claude"})
    assert (stale.reason, stale.width, stale.scope) == (bp.Reason.STALE, 1, ("claude",))
    odd = HealthReport(at=NOW.isoformat(), providers=(group("kimi"), "not a group"), unattributed=(),
                       problems=(), runs_seen=0)
    assert bp.decide(8, odd, now=NOW, providers={"claude"}).reason is bp.Reason.INCOMPLETE
    for bad in ("claude", b"claude", [], (), {""}, {"a\nb"}, {7}, [None], {f"p{i}" for i in range(17)},
                {"x" * 129}, 5, {"claude": 1}.keys() | {5}):
        with pytest.raises(bp.BackpressureError):
            bp.decide(8, report(), now=NOW, providers=bad)
    assert bp.decide(8, report(), now=NOW, providers={f"p{i}" for i in range(16)}).scope \
        == tuple(sorted(f"p{i}" for i in range(16)))


# --- B6 the record carries its scope ---------------------------------------------------------------------------------

def test_the_record_names_its_scope_and_what_it_ignored_and_is_refused_when_they_do_not_fit(tmp_path):
    """Catches a record that cannot say what it was decided for: scope and ignored round-trip, schema /2 only;
    a scope that is not a sorted, distinct list of 1 to 16 names, `ignored` without a scope, a named provider
    outside the scope, a stale reading with counts, are refused; 16 names of 128 characters with a 128-character
    provider and account still fit the 4096 bytes."""
    bp = _bp()

    def body(**over):
        doc = {"schema": "hoh-backpressure-decision/2", "decision_id": "w1", "decided_at": STAMP,
               "report_at": STAMP, "planned_width": 4, "width": 1, "reason": "LIMITED", "provider": "claude",
               "account_handle": "a", "groups": 1, "unattributed": 0, "problems": 0, "scope": ["claude"],
               "ignored": 2}
        doc.update(over)
        return (json.dumps(doc, sort_keys=True, indent=1) + "\n").encode()

    rec = bp.parse(body())
    assert rec.decision.scope == ("claude",) and rec.decision.ignored == 2
    assert bp.parse(rec.to_bytes()) == rec
    assert bp.parse(body(scope=None, ignored=0)).decision.scope is None
    for over in (dict(scope=[]), dict(scope="claude"), dict(scope=["codex", "claude"]),
                 dict(scope=["claude", "claude"]),
                 dict(scope=[7]), dict(scope=["a\n"]), dict(scope=[f"p{i}" for i in range(17)]),
                 dict(scope=["codex"]), dict(scope=None, ignored=1), dict(ignored=-1), dict(ignored="2"),
                 dict(ignored=2**31 + 1), dict(schema="hoh-backpressure-decision/1"),
                 dict(reason="STALE", width=1, provider="", account_handle="", groups=0, ignored=1)):
        with pytest.raises(bp.BackpressureError):
            bp.parse(body(**over))
    assert bp.parse(body(ignored=2**31)).decision.ignored == 2**31
    names = sorted(f"{chr(97 + i)}" + "x" * 127 for i in range(16))
    big = bp.parse(body(scope=names, provider=names[0], account_handle="y" * 128))
    assert len(big.to_bytes()) <= 4096 and big.decision.scope == tuple(names)
    d = tmp_path / "records"
    d.mkdir()
    ctl = bp.BackpressureController(d, clock=lambda: STAMP)
    from hoh.health import HealthReport
    got = ctl.admit_width("s1", 5, HealthReport(at=NOW.isoformat(), providers=(), unattributed=(), problems=(),
                                                runs_seen=0), providers={"kimi", "claude"})
    assert bp.read_decision(d / "backpressure-s1.json").decision == got and got.scope == ("claude", "kimi")


# --- B7 the default drive never asks ------------------------------------------------------------------------------

def test_the_default_drive_is_linear_and_never_consults_the_gate(world, monkeypatch, capsys):
    """Catches a gate that changes the width-1 loop (the default, `HOH_WAVE_WIDTH` unset and the launcher's
    `wave_width` 1): with a limited provider `hoh project drive` runs one node per round exactly as before, prints
    no backpressure step and writes no record directory -- the floor already is the sequential loop."""
    world.park("other-run")
    rc, out = _drive(world, monkeypatch, capsys, width=None)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert not [s for s in out["steps"] if "BACKPRESSURE" in s and s.startswith("BACKPRESSURE")]
    assert not (world.store.dir / "backpressure").exists() and not (world.store.dir / "waves").exists()
    assert sorted(CONFIG["launcher"].merged) == ["n1", "n2", "n3"]
