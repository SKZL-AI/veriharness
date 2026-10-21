"""P3-41 slice 2: `hoh run` fails over, durably, to the routing policy's declared fallback.

Slice 1 (`tests/test_routing_failover.py`) made the decision a function with a record and no
caller. These tests drive the production path -- `hoh project drive` through `cli.main`, the
driver a project registers in `driver.json`, `resumeloop.drive_governed`, the real
`HohRunLauncher.prepare` / `launch` -- and the command the launcher types, `hoh run ... --routing-policy`,
through `cli.main` in this process (the process boundary of the launcher is the only thing replaced:
its argv is executed here with `--iterations 0 --no-herdr`, so no role starts and no agent runs).
The parks that make a provider "limited" are real runs under the same root with the receipt the
controller writes. Everything is under `tmp_path`; nothing starts a process, Herdr or an agent.

Mutant ids (`F1`..) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli, dispatchers, dispatchrecovery, durable_io, health, launcher, routing
from hoh.contracts import Condition, Role, RunState, Stage
from hoh.controller import Controller
from hoh.dispatchrecovery import (
    NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id,
)
from hoh.failures import NormalizedFailure
from hoh.launcher import Dispatch, HohRunLauncher, hoh_command
from hoh.orchestrator import GateRunner, MergeResult
from hoh.project import GateOutcome, GateResult, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.routing import RoutingRefused
from hoh.store import RunStore
from hoh.succession import MasterAuthority

N = NormalizedFailure
FILE = "routing-failover.json"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(delta: timedelta) -> str:
    return (_now() + delta).isoformat()


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def _doc(**over) -> dict:
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {"qa": {"provider": "claude", "model": "opus",
                            "fallbacks": [{"provider": "codex", "model": "gpt-5"}]}}}
    doc.update(over)
    return doc


def _objects(text: str) -> list[dict]:
    """The JSON objects `hoh` printed one after another (`_print` indents them)."""
    decoder, at, found = json.JSONDecoder(), 0, []
    while (at := text.find("{", at)) != -1:
        try:
            obj, at = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        found.append(obj)
    return found


def _tree(path: Path) -> dict:
    """Every file under `path` with its bytes and mode (not `.lock`: taking a run's lock rewrites
    it whatever the pass then decides)."""
    out = {}
    for p in sorted(path.rglob("*")):
        if p.name.endswith(".lock") or p.name == "process.json":
            continue                            # the process marker of every `hoh run`
        out[str(p.relative_to(path))] = (p.read_bytes() if p.is_file() and not p.is_symlink()
                                         else b"", p.lstat().st_mode)
    return out


# --------------------------------------------------------------------------- #
# the world: a git repository, runs under one root, parks that name a provider
# --------------------------------------------------------------------------- #

def _initialised_storage_root(tmp_path: Path, monkeypatch) -> None:
    """`hoh run` refuses to start without an initialised storage state root (and, on WSL,
    without a host volume): an initialised root under `tmp_path`, through `HOH_STATE_ROOT`."""
    from hoh import storage_host
    from hoh.storage_registry import StorageRegistry

    root = tmp_path / "storage-state"
    StorageRegistry(root).init()
    if storage_host.detect_host_profile() == storage_host.PROFILE_WSL2:
        volume = tmp_path / "host-volume"
        volume.mkdir()
        (root / "storage-policy.json").write_text(
            json.dumps({"host_paths": [str(volume)]}), encoding="utf-8")
        (root / "storage-host.json").write_text(
            json.dumps({"wsl_host_volume": str(volume)}), encoding="utf-8")
    monkeypatch.setenv("HOH_STATE_ROOT", str(root))


class World:
    """Runs under `tmp_path/runs`, a spy on `build_dispatcher`, and the policy file."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        _initialised_storage_root(tmp_path, monkeypatch)
        self.tmp, self.capsys = tmp_path, capsys
        self.root = tmp_path / "runs"
        self.root.mkdir()
        self.repo = tmp_path / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.spec = tmp_path / "spec.md"
        self.spec.write_text("# spec\n", encoding="utf-8")
        self.policy_file = tmp_path / "policy.json"
        self.policy_file.write_bytes(json.dumps(_doc()).encode())
        self.seen: list[dict] = []
        self.built: list = []
        self.at_controller: list[dict] = []
        real = dispatchers.build_dispatcher

        def spy(**kw):
            self.seen.append(kw)
            self.built.append(real(**kw))
            return self.built[-1]

        monkeypatch.setattr(dispatchers, "build_dispatcher", spy)
        from hoh import controller as controller_mod

        class SpyController(controller_mod.Controller):
            def __init__(inner, store, *a, **kw):
                run_dir = store.dir
                self.at_controller.append({
                    "failover": (run_dir / FILE).exists(),
                    "master": (run_dir / "recovery" / "master.json").exists(),
                    "pin": (run_dir / cli.ROUTING_RECORD).exists()})
                super().__init__(store, *a, **kw)

        monkeypatch.setattr(controller_mod, "Controller", SpyController)
        monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)

    def use_root(self, name: str) -> None:
        """A fresh runs root: every later run, park and `hoh run` of this world is in it."""
        self.root = self.tmp / name
        self.root.mkdir()

    # a run that has not started: `hoh run` finds its state
    def new_run(self, run_id: str) -> RunStore:
        store = RunStore(self.root, run_id)
        store.write_state(Controller.new_state(
            run_id=run_id, repo_path=self.repo, project_name="project", spec_path=self.spec))
        return store

    def park(self, run_id: str, *, provider: str = "claude", account: str = "acct-a",
             failure: N = N.USAGE_LIMIT, retry: str | None = None, candidate: str = "NONE",
             receipt: bool = True, iteration: int = 1, attempts: int = 0) -> RunStore:
        """Another run of the root, parked on `failure` that resets at `retry` (default: in an
        hour), with the receipt the controller writes. A `candidate` other than NONE makes a
        receipt the reading attributes and `verify_binding` refuses."""
        retry = retry or _stamp(timedelta(hours=1))
        st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                      spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                      profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                      iteration=iteration)
        st.blocked_kind = failure.value.lower()
        st.blocked_reason = "parked"
        st.retry_after = retry
        st.resume_attempts = attempts           # 99: the resume loop gave this park up
        store = RunStore(self.root, run_id)
        if store.state_path.exists():           # the run exists: parked from where it stands
            st.write_seq = RunStore._parse_state(store.state_path.read_text()).write_seq
        store.write_state(st)
        if receipt:
            binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id,
                                  iteration=st.iteration, attempt=st.attempt,
                                  stage=st.stage.value, candidate=candidate, master=NO_MASTER)
            dispatchrecovery.append_record(store.dir, RoutingDecision(
                park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
                provenance=DispatchProvenance(
                    provider=provider, model="m-1", account_handle=account, role="developer",
                    capability_requirements=["read_repo", "write_worktree", "run_shell"]),
                retry_at=retry))
        return store

    def unpark(self, store: RunStore) -> None:
        """The park is over: the same run, active again."""
        old = RunStore._parse_state(store.state_path.read_text())
        st = old.model_copy(update={"condition": Condition.ACTIVE, "blocked_kind": None,
                                    "retry_after": None, "blocked_reason": None})
        store.write_state(st)

    def run(self, run_id: str = "r1", *extra: str):
        args = ["--root", str(self.root), "run", run_id, "--iterations", "0", "--no-herdr",
                "--routing-policy", str(self.policy_file), *extra]
        rc = cli.main(args)
        got = self.capsys.readouterr()
        return rc, got.out, got.err

    def status(self, run_id: str = "r1") -> dict:
        assert cli.main(["--root", str(self.root), "status", run_id]) == 0
        return _objects(self.capsys.readouterr().out)[0]


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


# --------------------------------------------------------------------------- #
# F1 -- the run starts on the declared fallback, and says so
# --------------------------------------------------------------------------- #

def test_hoh_run_builds_its_dispatcher_on_the_declared_fallback_when_the_provider_is_limited(world):
    """Kills F1 (the plan is not used: the dispatcher is built on the declared route), F2 (the
    record is written after the first dispatch or not at all), F3 (the decision is not shown:
    no line, no note, no status), F4 (the failed-over dispatcher carries no evidence)."""
    world.new_run("r1")
    world.park("other")                                  # claude: a verified limit, an hour out
    rc, out, err = world.run()
    assert rc == 0, err
    (kw,) = world.seen
    assert kw["routing"] is None                         # built from the plan's own tables
    assert kw["profiles"] == {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "codex"}
    d = world.built[0]
    assert d.profiles[Role.QA] == "codex" and d.models[Role.QA] == "gpt-5"
    assert d._agent_args(Role.QA) == ["--model", "gpt-5", "--effort", "high"]
    ev = d.routing_evidence
    assert ev["resolved"]["qa"]["provider"] == {
        "value": "codex", "source": "policy:failover", "declared_provider": "claude"}
    assert ev["failover"]["decisions"]["qa"]["outcome"] == "FALLBACK"
    # recorded: one create-only file, before the controller exists, after the master declaration
    record = json.loads((world.root / "r1" / FILE).read_bytes())
    assert record["schema"] == "hoh-routing-failover/2"
    assert record["policy_digest"] == routing.load(world.policy_file).digest
    assert record["decisions"]["qa"]["considered"][0]["state"] == "LIMITED"
    assert record["decisions"]["qa"]["considered"][0]["not_before"] is not None
    (at,) = world.at_controller
    assert at == {"failover": True, "master": True, "pin": True}
    # visible: a line on stdout, a line on stderr, a note in the run's history, `hoh status`
    (line,) = [o["routing_policy"] for o in _objects(out) if "routing_policy" in o]
    assert line["failover"]["qa"]["outcome"] == "FALLBACK"
    assert "routing failover: qa runs on codex/gpt-5 (LIMITED at the declared provider)" in err
    state = RunStore(world.root, "r1").read_state()
    assert any("routing failover: qa runs on codex/gpt-5" in h for h in state.history)
    shown = world.status()["routing_failover"]
    assert shown["roles"]["qa"]["outcome"] == "FALLBACK"
    assert shown["roles"]["qa"]["route"] == {"provider": "codex", "model": "gpt-5"}


def test_the_run_stays_on_the_declared_route_when_nothing_verified_limits_it(world):
    """Kills F5 (an unverified park -- a receipt `verify_binding` refuses -- moves the run), F6
    (the run's own park is evidence against itself), F7 (a policy with fallbacks and a healthy
    provider is not recorded: a later limit would move the run), F8 (the declared route is built
    differently from a run without fallbacks)."""
    world.new_run("r1")
    world.park("other", candidate="not-the-candidate")   # attributed by the reading, not bound
    rc, out, err = world.run()
    assert rc == 0, err
    d = world.built[0]
    assert d.profiles[Role.QA] == "claude" and d.models[Role.QA] == "opus"
    record = json.loads((world.root / "r1" / FILE).read_bytes())
    assert record["decisions"]["qa"]["outcome"] == "DECLARED"
    assert record["decisions"]["qa"]["considered"] == [
        {"provider": "claude", "model": "opus", "state": "NO_EVIDENCE", "not_before": None}]
    assert "routing failover" not in err
    # a park of the run itself is no evidence about it (iteration 0: a run that has not begun, the
    # only run that plans -- the exclusion is the function's own guard, G5 and G13 pin it)
    world.new_run("r2")
    own = world.park("r2", iteration=0)
    assert RunStore(world.root, "r2").state_path.exists() and own.run_id == "r2"
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run("r2")
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude"
    assert json.loads((world.root / "r2" / FILE).read_bytes())[
        "decisions"]["qa"]["outcome"] == "DECLARED"


# --------------------------------------------------------------------------- #
# F2 -- one decision per run; the next run returns when the provider is back
# --------------------------------------------------------------------------- #

def test_a_resumed_run_replays_its_decision_and_the_next_run_returns_to_the_declared_route(world):
    """Kills F9 (a resumed `hoh run` decides again: it flips back when the park ends), F10 (a
    replay rewrites or re-notes the record: it is not quiet), F11 (the next run keeps the
    fallback after the provider is back)."""
    world.new_run("r1")
    other = world.park("other")
    assert world.run()[0] == 0
    path = world.root / "r1" / FILE
    first = path.read_bytes()
    world.unpark(other)                                   # claude is back
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run()                            # the same run, resumed
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "codex"    # replayed: no flip back inside a run
    assert path.read_bytes() == first                     # quiet
    assert "routing failover:" not in err                 # not announced again
    # the next run decides afresh and is on the declared route
    world.new_run("r2")
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run("r2")
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude"
    assert json.loads((world.root / "r2" / FILE).read_bytes())[
        "decisions"]["qa"]["outcome"] == "DECLARED"


def test_the_return_waits_for_the_dwell_after_a_limit_resets(world):
    """The hysteresis. A limit whose stated reset passed less than 600 seconds ago is `SETTLING`:
    the run that starts inside the window stays on the fallback; one whose reset is older than
    the window returns to the declared route. Kills F12 (no dwell: a return the second the
    reset passes), F13 (the dwell is another bound), F14 (a settling limit is recorded as a
    plain reset)."""
    cases = [("r1", -timedelta(seconds=300), "codex", "SETTLING"),
             ("r2", -timedelta(seconds=599), "codex", "SETTLING"),
             ("r3", -timedelta(seconds=660), "claude", "RESET_DUE"),
             ("r4", -timedelta(hours=2), "claude", "RESET_DUE")]
    # one reading per run: every run of a root sees every park of it, so each case has a root
    for run_id, ago, provider, state in cases:
        sub = world.tmp / f"root-{run_id}"
        sub.mkdir()
        RunStore(sub, run_id).write_state(Controller.new_state(
            run_id=run_id, repo_path=world.repo, project_name="project", spec_path=world.spec))
        st = RunState(run_id=f"p-{run_id}", repo_path="/arena/repo", project_name="demo",
                      spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                      profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                      iteration=1)
        st.blocked_kind, st.blocked_reason, st.retry_after = "usage_limit", "parked", _stamp(ago)
        parked = RunStore(sub, f"p-{run_id}")
        parked.write_state(st)
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=st.run_id, iteration=1,
                              attempt=st.attempt, stage=st.stage.value, candidate="NONE",
                              master=NO_MASTER)
        dispatchrecovery.append_record(parked.dir, RoutingDecision(
            park_id=park_id(binding, N.USAGE_LIMIT, st.retry_after), reason="USAGE_LIMIT",
            binding=binding, provenance=DispatchProvenance(
                provider="claude", model="m-1", account_handle="acct-a", role="developer",
                capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=st.retry_after))
        world.seen.clear(), world.built.clear()
        rc = cli.main(["--root", str(sub), "run", run_id, "--iterations", "0", "--no-herdr",
                       "--routing-policy", str(world.policy_file)])
        world.capsys.readouterr()
        assert rc == 0
        assert world.built[0].profiles[Role.QA] == provider, (run_id, ago)
        record = json.loads((sub / run_id / FILE).read_bytes())
        assert record["decisions"]["qa"]["considered"][0]["state"] == state


def test_a_stated_reset_beyond_seventy_two_hours_is_no_evidence(world):
    """A forged or mistyped stamp must not send every new run to the fallback for good: a park
    whose stated reset is further than `health.MAX_HOLD` (72 h) away is not evidence of the
    verified reading; one 71 h away is. Kills F27 (the ceiling is dropped from the verified
    reading)."""
    for name, hours, expected in (("far", 100, "claude"), ("near", 71, "codex")):
        world.use_root(name)
        world.new_run("r1")
        world.park("other", retry=_stamp(timedelta(hours=hours)))
        world.seen.clear(), world.built.clear()
        rc, out, err = world.run()
        assert rc == 0, err
        assert world.built[0].profiles[Role.QA] == expected, (name, hours)


def test_the_seventy_two_hour_ceiling_is_inclusive_by_the_clock_the_function_is_given(world):
    """`read_verified_health(now=)`: a limit that resets exactly 72 h after `now` is evidence, one
    second later is not. Kills F29 (the ceiling is exclusive or another bound)."""
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    for seconds, counted in ((72 * 3600, True), (72 * 3600 + 1, False)):
        world.use_root(f"ceiling-{seconds}")
        world.park("other", retry=(now + timedelta(seconds=seconds)).isoformat())
        report = health.read_verified_health(world.root, now=now)
        assert [g.state.value for g in report.providers] == (["LIMITED"] if counted else []), seconds


def test_hoh_status_shows_a_damaged_failover_record_as_unreadable_and_does_not_raise(world):
    """`hoh status` is read-only and never raises on the record: a record that is not what the
    module writes (a link; bytes that are not its own digest) is `unreadable`, the rest of the
    status is still printed. Kills F30 (status raises or hides the damage)."""
    world.new_run("r1")
    path = world.root / "r1" / FILE
    path.write_text(json.dumps({"schema": "hoh-routing-failover/2", "decisions": {}, "digest": "x"}))
    shown = world.status()
    assert "unreadable" in shown["routing_failover"] and shown["run_id"] == "r1"
    # a record that is its own digest and not the shape the module writes
    import hashlib
    body = {"schema": "hoh-routing-failover/2", "policy_digest": "0" * 64,
            "decided_at": "2026-10-15T00:00:00+00:00", "decisions": "oops"}
    body["digest"] = hashlib.sha256(json.dumps(
        {k: v for k, v in body.items() if k != "digest"}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()
    path.write_text(json.dumps(body))
    shown = world.status()
    assert "unreadable" in shown["routing_failover"] and shown["run_id"] == "r1"
    path.rename(path.with_name("moved"))
    path.symlink_to(path.with_name("moved"))
    shown = world.status()
    assert "unreadable" in shown["routing_failover"] and shown["run_id"] == "r1"


def test_a_decision_that_loses_the_create_race_is_refused_by_a_caller_that_built_on_it(world):
    """`commit()` after another resolution of the run recorded a **different** decision: a caller
    that has built its dispatcher on the plan is refused (it would run on a route the record does
    not name); the one-shot `resolve_failover`, which has built nothing, takes the winner's.
    Kills F28 (a lost race is adopted by a caller that has already built)."""
    world.new_run("r1")
    world.park("other")
    policy = routing.load(world.policy_file)
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    plan = routing.plan_failover(policy, run_dir=world.root / "r1", profiles=profiles,
                                 reader=health.read_verified_health)
    try:
        assert plan.pending and plan.resolved.profiles[Role.QA] == "codex"
        # another resolver, seeing a healthy provider, records "stay" first
        nothing = health.HealthReport(at=_now().isoformat(), providers=(), unattributed=(),
                                      problems=(), runs_seen=0)
        won = routing.resolve_failover(policy, run_dir=world.root / "r1", profiles=profiles,
                                       reader=lambda root, **kw: nothing)
        assert won.profiles[Role.QA] == "claude"
        record = (world.root / "r1" / FILE).read_bytes()
        with pytest.raises(RoutingRefused) as err:
            plan.commit()
        assert "another resolution of this run recorded a different" in str(err.value)
        assert (world.root / "r1" / FILE).read_bytes() == record
    finally:
        plan.close()


def test_the_dwell_is_six_hundred_seconds_by_the_clock_the_function_is_given(world):
    """`plan_failover(now=)` with a reading in which the declared provider's limit reset exactly
    599 / 600 / 601 seconds before `now`: 599 is `SETTLING` (the fallback is taken), 600 and 601
    are `RESET_DUE` (the declared route). Kills F13 (the bound is 599, 601 or the clock's)."""
    world.new_run("r1")
    policy = routing.load(world.policy_file)
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    got = {}
    for ago in (599, 600, 601):
        reset = (now - timedelta(seconds=ago)).isoformat()

        def reader(root, *, now, exclude_runs=(), _reset=reset):
            return health.HealthReport(
                at=now.isoformat(), providers=(health.ProviderHealth(
                    provider="claude", account_handle="acct-a", state=health.HealthState.RESET_DUE,
                    not_before=_reset, runs=("x",), failures=(("USAGE_LIMIT", 1),)),),
                unattributed=(), problems=(), runs_seen=1)

        plan = routing.plan_failover(policy, run_dir=world.root / "r1", profiles=profiles,
                                     now=now, reader=reader)
        try:
            d = plan.resolved.evidence["failover"]["decisions"]["qa"]
            got[ago] = (d["outcome"], d["considered"][0]["state"], d["considered"][0]["not_before"],
                        reset)
        finally:
            plan.close()
    assert got[599][:2] == ("FALLBACK", "SETTLING") and got[599][2] == got[599][3]
    assert got[600][:2] == ("DECLARED", "RESET_DUE")
    assert got[601][:2] == ("DECLARED", "RESET_DUE")


# --------------------------------------------------------------------------- #
# F3 -- fail closed, and every end is terminal and quiet
# --------------------------------------------------------------------------- #

def test_no_healthy_route_starts_the_run_on_its_declared_route_and_records_nothing(world):
    """A provider stop is never a halt. Every declared route has a pending limit (or the declared
    provider has just reset and the fallback is limited): `hoh run` exits 0, builds the dispatcher
    on the declared route exactly as the base does (`routing=policy`, the run's profiles), records
    **nothing**, says on stderr and in its JSON that no decision was made, and writes no line the
    launcher reads as UNDETERMINED; the limit is then met the way every run meets it (a park with a
    timer). A second `hoh run` changes no file. Kills G10b (the stay is a refusal again, exit 2),
    G22 (a stay is built from the plan with `routing=None`)."""
    world.new_run("r1")
    world.park("a", provider="claude")
    world.park("b", provider="codex", account="acct-b", failure=N.RATE_LIMIT)
    rc, out, err = world.run()
    assert rc == 0, err
    (kw,) = world.seen
    assert kw["routing"] is not None and kw["profiles"][Role.QA] == "claude"
    assert world.built[0].models[Role.QA] == "opus"
    assert not (world.root / "r1" / FILE).exists()
    assert "routing failover: not decided (qa: no route the policy declares" in err
    assert launcher.routing_refusal(err) is None          # nothing the launcher turns into a halt
    (line,) = [o["routing_policy"] for o in _objects(out) if "routing_policy" in o]
    assert "no route the policy declares" in line["failover_not_decided"] and "failover" not in line
    assert "routing_failover" not in world.status()
    after_first = _tree(world.root / "r1")
    world.seen.clear(), world.built.clear()
    rc2, out2, err2 = world.run()
    assert rc2 == 0 and _tree(world.root / "r1") == after_first    # quiet: nothing is written
    # the provider has just reset (5 minutes: SETTLING, it answers) and the fallback is limited:
    # the same, on the declared route
    world.use_root("settling")
    world.new_run("r1")
    world.park("a", provider="claude", retry=_stamp(-timedelta(minutes=5)))
    world.park("b", provider="codex", account="acct-b", failure=N.RATE_LIMIT)
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude" and not (world.root / "r1" / FILE).exists()


def test_a_run_that_has_begun_keeps_its_declared_route_and_a_recorded_decision_is_replayed(
        world, monkeypatch):
    """No record and iteration history (a run of before this module, or one that began under a
    policy without fallbacks): the run keeps its declared route, nothing is read and nothing is
    recorded -- it is never moved in the middle of its life. A run that has a record replays it
    whatever its history. Kills G26 (a begun run is planned afresh and moved), G27 (the history
    switches the replay off)."""
    store = world.new_run("r1")
    old = store.read_state()
    store.write_state(old.model_copy(update={"iteration": 3, "stage": Stage.DEVELOPING}))
    world.park("other")                                   # claude: a verified limit
    calls = []
    real = health.read_health
    monkeypatch.setattr(health, "read_health", lambda *a, **k: calls.append(1) or real(*a, **k))
    rc, out, err = world.run()
    monkeypatch.setattr(health, "read_health", real)
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude" and calls == []
    assert not (world.root / "r1" / FILE).exists()
    assert "not decided (the run has already begun" in err
    # a run that decided before it began, then has history: the record replays
    world.new_run("r2")
    assert world.run("r2")[0] == 0 and world.built[-1].profiles[Role.QA] == "codex"
    store2 = RunStore(world.root, "r2")
    store2.write_state(store2.read_state().model_copy(update={"iteration": 2}))
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run("r2")
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "codex" and "not decided" not in err


def test_a_decision_that_cannot_be_recorded_starts_nothing_and_a_half_record_is_replayed(
        world, monkeypatch):
    """The recording path: the create fails -> exit 2 with the cause, no record, no pin, the
    controller never built; the create is linked and its directory fsync fails
    (`DurableWriteNotSynced`) -> exit 2 with the record in place, and the next `hoh run` replays
    it, reading no health. Kills F18 (a failed record is swallowed and the run goes on), F19
    (a refusal leaves a pin or a partial file), F20 (the in-place record is not replayed)."""
    world.new_run("r1")
    other = world.park("other")
    real = durable_io.durable_create

    def enospc(path, payload, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(routing, "durable_create", enospc)
    rc, out, err = world.run()
    assert rc == 2 and "routing policy refused: the failover decision cannot be recorded" in err
    assert not (world.root / "r1" / FILE).exists()
    assert not (world.root / "r1" / cli.ROUTING_RECORD).exists()
    assert world.at_controller == []
    # linked in, directory fsync fails after it
    def not_synced(path, payload, **kw):
        real(path, payload, **kw)
        raise durable_io.DurableWriteNotSynced(path, OSError(5, "injected"))

    monkeypatch.setattr(routing, "durable_create", not_synced)
    rc, out, err = world.run()
    assert rc == 2 and "in place but could not be made durable" in err
    record = (world.root / "r1" / FILE).read_bytes()
    assert world.at_controller == []
    monkeypatch.setattr(routing, "durable_create", real)
    # the same run again, the provider back, health unreadable: the record decides
    world.unpark(other)
    broken = world.root / "broken"
    broken.mkdir()
    (broken / "state.json").write_text("{")
    rc, out, err = world.run()
    assert rc == 0, err
    assert (world.root / "r1" / FILE).read_bytes() == record
    assert world.built[-1].profiles[Role.QA] == "codex"


def test_an_invocation_refused_after_the_plan_leaves_no_failover_record(world):
    """The record is committed last: after every refusal that can follow the plan. A recovery
    declaration that is refused (`--role-account` with no handle) is such a refusal: exit 2, no
    record, no pin, no controller. Kills F3b (the decision is recorded when it is planned)."""
    world.new_run("r1")
    world.park("other")
    rc, out, err = world.run("r1", "--role-account", "qa")
    assert rc == 2 and "recovery declaration refused" in err
    assert not (world.root / "r1" / FILE).exists()
    assert not (world.root / "r1" / cli.ROUTING_RECORD).exists()
    assert world.at_controller == []
    rc, out, err = world.run()                 # the corrected invocation decides, once
    assert rc == 0, err
    assert (world.root / "r1" / FILE).is_file() and len(world.at_controller) == 1


def test_health_that_cannot_be_read_completely_starts_the_run_on_its_declared_route(world):
    """An incomplete reading (a `latest -> run-ok` symlink in the runs root; a damaged
    `state.json`) is no evidence of health, and no reason to halt: the run starts on its declared
    route, nothing is recorded, stderr says why. A readable limited run sits beside the damage,
    so a reading that accepted the incomplete report would fail over. Kills G10 (an incomplete
    report is accepted), G10b (the incomplete reading is a refusal again)."""
    world.new_run("r1")
    world.new_run("run-ok")
    (world.root / "latest").symlink_to(world.root / "run-ok", target_is_directory=True)
    world.park("other")                                   # a readable, verified claude limit
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude"
    assert not (world.root / "r1" / FILE).exists()
    assert "routing failover: not decided (provider health is incomplete" in err
    world.use_root("damaged")
    world.new_run("r1")
    world.park("other")
    broken = world.root / "broken"
    broken.mkdir()
    (broken / "state.json").write_text("{")
    world.seen.clear(), world.built.clear()
    rc, out, err = world.run()
    assert rc == 0 and world.built[0].profiles[Role.QA] == "claude"
    assert not (world.root / "r1" / FILE).exists() and "provider health is incomplete" in err


def test_a_stale_master_cannot_fail_over_and_leaves_no_record(world):
    """A5 (P2): a stale master cannot fail over. The project owning `r1` is at epoch 2; `hoh run`
    under epoch 1 is refused with nothing written (`declare_master` refuses it too -- the
    function itself refuses before: kills F22, the fence of `plan_failover` dropped), and the
    current master decides normally."""
    world.new_run("r1")
    world.park("other")
    project = ProjectStore(world.root, "p")
    project.create(ProjectState(project_id="p", repo_path=str(world.repo),
                                nodes=[TaskNode(id="a", run_id="r1")]))
    s1 = MasterAuthority(project.dir, "S1", serialize=project.lock)
    s1.acquire()
    s1.release(reason="replaced")
    s2 = MasterAuthority(project.dir, "S2", serialize=project.lock)
    s2.acquire()
    policy = routing.load(world.policy_file)
    stale = dispatchrecovery.MasterBinding(kind="GOVERNED", session="S1", epoch=1)
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    with pytest.raises(RoutingRefused) as err:
        routing.plan_failover(policy, run_dir=world.root / "r1", profiles=profiles, master=stale,
                              reader=health.read_verified_health)
    assert "stale master cannot fail over" in str(err.value)
    rc, out, e = world.run("r1", "--master-session", "S1", "--master-epoch", "1")
    assert rc == 2 and world.seen == [] and not (world.root / "r1" / FILE).exists()
    rc, out, e = world.run("r1", "--master-session", "S2", "--master-epoch", str(s2.epoch))
    assert rc == 0, e
    assert world.built[-1].profiles[Role.QA] == "codex"


def test_a_governed_park_counts_only_while_its_master_is_live_and_current(world):
    """The binding of the evidence (A6 of slice 1's review). A park of a governed run is evidence
    only while its master holds the project's current epoch under a live lease. Kills F23 (a
    superseded master's park moves a new run: the reading is unverified)."""
    world.new_run("r1")
    project = ProjectStore(world.root, "p")
    project.create(ProjectState(project_id="p", repo_path=str(world.repo),
                                nodes=[TaskNode(id="g", run_id="g-run")]))
    s1 = MasterAuthority(project.dir, "S1", serialize=project.lock)
    s1.acquire()
    st = RunState(run_id="g-run", repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                  iteration=1)
    retry = _stamp(timedelta(hours=1))
    st.blocked_kind, st.blocked_reason, st.retry_after = "usage_limit", "parked", retry
    gstore = RunStore(world.root, "g-run")
    gstore.write_state(st)
    (gstore.dir / "recovery").mkdir(parents=True, exist_ok=True)
    (gstore.dir / "recovery" / "master.json").write_text(json.dumps(
        {"kind": "GOVERNED", "epoch": s1.epoch, "session": "S1"}))
    master = dispatchrecovery.MasterBinding(kind="GOVERNED", epoch=s1.epoch, session="S1")
    binding = ParkBinding(programme="NONE", program_epoch=0, run_id="g-run", iteration=1,
                          attempt=st.attempt, stage=st.stage.value, candidate="NONE",
                          master=master)
    dispatchrecovery.append_record(gstore.dir, RoutingDecision(
        park_id=park_id(binding, N.USAGE_LIMIT, retry), reason="USAGE_LIMIT", binding=binding,
        provenance=DispatchProvenance(
            provider="claude", model="m-1", account_handle="acct-a", role="developer",
            capability_requirements=["read_repo", "write_worktree", "run_shell"]),
        retry_at=retry))
    now = _now()
    live = health.read_verified_health(world.root, now=now)
    assert [g.state.value for g in live.providers] == ["LIMITED"]       # master live and current
    s1.release(reason="replaced")
    MasterAuthority(project.dir, "S2", serialize=project.lock).acquire()
    gone = health.read_verified_health(world.root, now=now)
    assert gone.providers == ()                                          # superseded: no evidence
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "claude"


# --------------------------------------------------------------------------- #
# F4 -- the launcher's own path: `hoh project drive` -> the node run, fails over
# --------------------------------------------------------------------------- #

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
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class RunInProcess(HohRunLauncher):
    """The real launcher. Its process boundary is replaced: `hoh worktree` and `hoh start` as in
    the routing tests, and **`hoh run` executed here through `cli.main`** with the argv the
    launcher typed (iterations 0, no Herdr: no role starts)."""

    def __init__(self, root, repo, **kw):
        kw.setdefault("planner", "claude")
        kw.setdefault("developer", "claude")
        kw.setdefault("qa", "claude")
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.calls: list[list[str]] = []
        self.merged: list[str] = []
        self.answers: list[tuple[int, str, str]] = []

    def _run_cli(self, argv):
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
            state = RunState(run_id=run_id, repo_path=argv[argv.index("--repo") + 1],
                             project_name="p", spec_path=argv[argv.index("--spec") + 1],
                             spec_digest="s" * 16, policy_digest="p" * 16,
                             profile_digest="f" * 16, stage=Stage.PLANNING,
                             condition=Condition.ACTIVE)
            store = RunStore(self.root, run_id)
            store.write_state(state)
            if "--routing-policy" in argv:       # `hoh start --routing-policy` pins, as the real one
                (store.dir / cli.ROUTING_RECORD).write_text(json.dumps(
                    cli._routing_pin_record(
                        routing.load(argv[argv.index("--routing-policy") + 1]), "hoh start")))
            return Dispatch(0, "", "")
        if "run" in argv:
            words = argv[len(hoh_command()):]
            words[words.index("--iterations") + 1] = "0"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main([*words, "--no-herdr"])
            self.answers.append((rc, out.getvalue(), err.getvalue()))
            return Dispatch(rc, out.getvalue(), err.getvalue())
        raise AssertionError(f"unexpected cli call {argv}")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40,
                           candidate=outcome.candidate or "")


def build_driver(store):
    """The `driver.json` factory: the real launcher; `hoh run` runs in this process."""
    launcher_ = RunInProcess(CONFIG["root"], CONFIG["repo"])
    CONFIG["launcher"] = launcher_
    return launcher_, Gates()


def test_hoh_project_drive_starts_the_next_node_run_on_the_declared_fallback(world, capsys):
    """The production path the register probe names: `hoh project drive` -> `drive_governed` ->
    `launcher.bind_routing` -> `prepare` / `launch` -> the `hoh run` the launcher typed, executed
    through `cli.main` -> `plan_failover` -> the dispatcher the run builds. Another run of the
    root is parked on the declared provider; the node's run starts on the declared fallback, the
    record is in its run directory, and the launcher's argv carries the policy. Kills F24 (the
    launcher's run is not planned: the failover exists only for a hand-typed `hoh run`), F1."""
    world.park("other")
    spec = world.tmp / "n1.md"
    spec.write_text("# n1\n", encoding="utf-8")
    project_root = world.root                    # the project lives under the runs root
    store = ProjectStore(project_root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(world.repo),
                              nodes=[TaskNode(id="n1", spec_path=str(spec), writes=["n1.txt"])]))
    (store.dir / launcher.ROUTING_POLICY_FILE).write_bytes(world.policy_file.read_bytes())
    CONFIG.clear()
    CONFIG.update(root=project_root, repo=world.repo)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_failover_run:build_driver",
        "path": str(Path(__file__).resolve().parent)}))
    cli.main(["--root", str(project_root), "project", "drive", "p", "--session", "sA"])
    capsys.readouterr()
    launcher_ = CONFIG["launcher"]
    runs = [c for c in launcher_.calls if "run" in c[len(hoh_command()):][:3]]
    assert runs and "--routing-policy" in runs[0]
    (kw, *_) = world.seen
    assert kw["routing"] is None and kw["profiles"][Role.QA] == "codex"
    record = json.loads((project_root / "n1" / FILE).read_bytes())
    assert record["decisions"]["qa"]["outcome"] == "FALLBACK"
    assert record["decisions"]["qa"]["route"] == {"provider": "codex", "model": "gpt-5"}


# --------------------------------------------------------------------------- #
# F5 -- a policy without fallbacks is exactly what it was
# --------------------------------------------------------------------------- #

def test_a_policy_without_fallbacks_reads_no_health_and_writes_no_record(world, monkeypatch):
    """Kills F25 (every routed run reads the root and writes a record), F26 (the dispatcher of a
    policy without fallbacks is built from the plan, not as it was): `routing=` is the policy,
    the profiles are the run's."""
    world.policy_file.write_bytes(json.dumps(_doc(roles={"qa": {"provider": "kimi",
                                                               "model": "opus"}})).encode())
    world.new_run("r1")
    world.park("other")

    def boom(*a, **k):
        raise AssertionError("the provider health was read")

    monkeypatch.setattr(health, "read_health", boom)
    rc, out, err = world.run()
    assert rc == 0, err
    (kw,) = world.seen
    assert kw["routing"] is not None and kw["models"] == {}
    assert kw["profiles"][Role.QA] == "kimi"
    assert not (world.root / "r1" / FILE).exists()
    assert "failover" not in [k for o in _objects(out) for k in o.get("routing_policy", {})]
    assert "routing_failover" not in world.status()


# --------------------------------------------------------------------------- #
# what the record says: which stamp, which state, which route status shows
# --------------------------------------------------------------------------- #

LATE = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
PROFILES = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}


def _groups_reader(*groups):
    """A reading made of `(provider, account, state, not_before)` groups: complete, no problems."""
    def reader(root, *, now, exclude_runs=()):
        return health.HealthReport(
            at=now.isoformat(), unattributed=(), problems=(), runs_seen=len(groups),
            providers=tuple(health.ProviderHealth(
                provider=p, account_handle=a, state=st, not_before=nb, runs=("x",),
                failures=(("USAGE_LIMIT", 1),)) for p, a, st, nb in groups))
    return reader


def _decision(world, *groups, run_id="r1"):
    world.new_run(run_id)
    plan = routing.plan_failover(routing.load(world.policy_file), run_dir=world.root / run_id,
                                 profiles=dict(PROFILES), now=LATE, reader=_groups_reader(*groups))
    try:
        return plan.resolved.evidence["failover"]["decisions"]["qa"]
    finally:
        plan.close()


def test_the_recorded_reset_is_the_latest_of_the_decisive_accounts_and_status_shows_the_declared_one(
        world):
    """Two accounts of the declared provider are `LIMITED` with different stated resets, in either
    order of the accounts: the record names the **later** (the group that decided), not the first
    read; and `hoh status` shows the declared route's reset, not the chosen fallback's. Kills G28
    (the earlier stamp wins), G29 (the status shows the last route looked at)."""
    early, late = (LATE + timedelta(hours=1)).isoformat(), (LATE + timedelta(hours=3)).isoformat()
    L = health.HealthState.LIMITED
    for order in ((early, late), (late, early)):
        d = _decision(world, ("claude", "acct-a", L, order[0]), ("claude", "acct-b", L, order[1]),
                      run_id=f"r-{order[0][11:13]}")
        assert d["considered"][0]["not_before"] == late and d["outcome"] == "FALLBACK"
    # the fallback's own reading (`RESET_DUE` long ago) differs from the declared route's
    world.use_root("status")
    world.new_run("r1")
    old = (LATE - timedelta(hours=2)).isoformat()
    routing.resolve_failover(routing.load(world.policy_file), run_dir=world.root / "r1",
                             profiles=dict(PROFILES), now=LATE, reader=_groups_reader(
                                 ("claude", "acct-a", L, late),
                                 ("codex", "acct-a", health.HealthState.RESET_DUE, old)))
    shown = routing.failover_summary(world.root / "r1")
    assert shown["roles"]["qa"]["not_before"] == late
    record = json.loads((world.root / "r1" / FILE).read_bytes())
    assert record["decisions"]["qa"]["considered"][1]["not_before"] == old


@pytest.mark.parametrize("states,reason", [
    (("DEGRADED", "SETTLING"), "DEGRADED"), (("SETTLING", "DEGRADED"), "DEGRADED"),
    (("LIMITED", "SETTLING"), "LIMITED"), (("SETTLING", "LIMITED"), "LIMITED"),
    (("SETTLING", "RESET_DUE"), "SETTLING"), (("RESET_DUE", "SETTLING"), "SETTLING"),
    (("SETTLING", "GIVEN_UP"), "SETTLING"), (("GIVEN_UP", "SETTLING"), "SETTLING"),
])
def test_the_precedence_puts_a_settling_limit_between_an_outage_and_a_plain_reset(
        world, states, reason):
    """`LIMITED > DEGRADED > SETTLING > RESET_DUE > GIVEN_UP`, in either order of the accounts: a
    recently reset limit (SETTLING) is below an outage and above a plain elapsed reset. The
    recorded reason and reset are the decisive group's. Kills G30 (SETTLING above DEGRADED), G31
    (SETTLING below RESET_DUE)."""
    stamp = {"DEGRADED": (LATE + timedelta(hours=1)).isoformat(),
             "LIMITED": (LATE + timedelta(hours=2)).isoformat(),
             "SETTLING": (LATE - timedelta(seconds=100)).isoformat(),
             "RESET_DUE": (LATE - timedelta(hours=3)).isoformat(),
             "GIVEN_UP": None}
    state = {"DEGRADED": health.HealthState.DEGRADED, "LIMITED": health.HealthState.LIMITED,
             "SETTLING": health.HealthState.RESET_DUE, "RESET_DUE": health.HealthState.RESET_DUE,
             "GIVEN_UP": health.HealthState.GIVEN_UP}
    d = _decision(world, *(("claude", f"acct-{i}", state[x], stamp[x]) for i, x in enumerate(states)))
    assert d["reason"] == reason == d["considered"][0]["state"]
    assert d["considered"][0]["not_before"] == stamp[reason]


def test_the_dwell_reads_a_naive_stamp_as_utc_and_a_stamp_that_is_no_time_as_a_plain_reset(world):
    """A stated reset without a zone is UTC (`health._when`'s rule), 100 s old it is `SETTLING`; a
    stamp that is no time at all is a plain `RESET_DUE`, not a crash. Kills G32 (the naive stamp
    is compared with an aware clock), G33 (an unparseable stamp raises)."""
    R = health.HealthState.RESET_DUE
    naive = (LATE - timedelta(seconds=100)).replace(tzinfo=None).isoformat()
    assert _decision(world, ("claude", "acct-a", R, naive), run_id="r1")["reason"] == "SETTLING"
    assert _decision(world, ("claude", "acct-a", R, "garbage"), run_id="r2")["reason"] == "RESET_DUE"


def test_an_equal_winner_of_the_create_race_lets_the_caller_continue(world):
    """`commit()` after another resolution recorded the **same** decisions first: the caller that
    built on the plan continues and the winner's bytes stand. Kills G34 (an equal winner is
    refused like a different one)."""
    world.new_run("r1")
    world.park("other")
    policy = routing.load(world.policy_file)
    plan = routing.plan_failover(policy, run_dir=world.root / "r1", profiles=dict(PROFILES),
                                 reader=health.read_verified_health)
    try:
        assert plan.pending and plan.resolved.profiles[Role.QA] == "codex"
        won = routing.resolve_failover(policy, run_dir=world.root / "r1",
                                       profiles=dict(PROFILES),
                                       reader=health.read_verified_health)
        assert won.profiles[Role.QA] == "codex"
        record = (world.root / "r1" / FILE).read_bytes()
        assert plan.commit().profiles[Role.QA] == "codex"
        assert (world.root / "r1" / FILE).read_bytes() == record
    finally:
        plan.close()


def test_the_verified_reading_keeps_what_decides_nothing_and_a_park_with_no_stamp(world):
    """`read_verified_health` drops a wait only for failing verification or the ceiling: an
    unattributed park (no receipt) stays listed, a park the loop gave up on stays a `GIVEN_UP`
    group, and a bound park whose stated reset is no time stays a `RESET_DUE` group with no
    stamp. Kills G35 (unattributed or given-up waits are dropped), G36 (a wait with no stamp is
    dropped)."""
    world.park("orphan", receipt=False)
    world.park("gone", provider="codex", account="acct-g", attempts=99)
    world.park("nostamp", provider="kimi", account="acct-k", retry="garbage")
    report = health.read_verified_health(world.root, now=_now())
    assert [w.run_id for w in report.unattributed] == ["orphan"]
    got = {(g.provider, g.account_handle): (g.state.value, g.not_before) for g in report.providers}
    assert got[("codex", "acct-g")][0] == "GIVEN_UP"
    assert got[("kimi", "acct-k")] == ("RESET_DUE", None)

