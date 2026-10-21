"""P3-41 slice 3: a run parked on a provider wait resumes on its declared fallback, durably.

Slice 2 (`tests/test_failover_run.py`) decides a run's route when it starts. These tests drive the
rest of the path through the entry point the operator and the continuation unit use --
`hoh resume-quota` through `cli.main` -- and the command that follows it, `hoh run`, also through
`cli.main` (with `--iterations 0 --no-herdr`: no role starts, no agent runs, nothing leaves
`tmp_path`). The parks that make a provider "limited" are real runs under the same root with the
receipt the controller writes; the timer of a park is the one `Controller._arm_quota_timer` arms.

Mutant ids (`P1`..) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_failure_classes import QA_LIMIT, _fresh_repo, answer_missing, build

from hoh import cli, dispatchers, dispatchrecovery, health, resumeloop, routing
from hoh.contracts import Condition, Role, RunState, Stage
from hoh.controller import Controller
from hoh.dispatchrecovery import (
    NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id,
)
from hoh.failures import NormalizedFailure
from hoh.orchestrator import node_timer_key
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.resumeloop import _timer_form, resume_runs
from hoh.routing import RoutingRefused
from hoh.store import RunStore
from hoh.succession import MasterAuthority
from hoh.timers import TimerKind, TimerStatus, TimerStore

N = NormalizedFailure
PARK1 = "routing-failover-park-1.json"
START = "routing-failover.json"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: the moment the unit-level tests pass to the resume loop and to the routing functions
AT = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _doc(*fallbacks, **over) -> dict:
    chain = list(fallbacks) or [{"provider": "codex", "model": "gpt-5"}]
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {"qa": {"provider": "claude", "model": "opus", "fallbacks": chain}}}
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
    """Every file under `path` with its bytes and mode (not `.lock` files and the process marker
    of a `hoh run`: taking a run's lock rewrites it whatever the pass then decides)."""
    out = {}
    for p in sorted(path.rglob("*")):
        if p.name.endswith(".lock") or p.name == "process.json":
            continue
        out[str(p.relative_to(path))] = (p.read_bytes() if p.is_file() and not p.is_symlink()
                                         else b"", p.lstat().st_mode)
    return out


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
        for args in (("init", "-q"),):
            subprocess.run([*GIT, "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        for args in (("add", "-A"), ("commit", "-qm", "init")):
            subprocess.run([*GIT, "-C", str(self.repo), *args], check=True, capture_output=True)
        self.spec = tmp_path / "spec.md"
        self.spec.write_text("# spec\n", encoding="utf-8")
        self.policy_file = tmp_path / "policy.json"
        self.policy(_doc())
        self.built: list = []
        real = dispatchers.build_dispatcher

        def spy(**kw):
            self.built.append(real(**kw))
            return self.built[-1]

        monkeypatch.setattr(dispatchers, "build_dispatcher", spy)
        monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)

    def policy(self, doc: dict) -> None:
        self.policy_file.write_bytes(json.dumps(doc).encode())

    # a run that has started once under the policy (so it is pinned) and then parked mid-life
    def started_run(self, run_id: str) -> RunStore:
        store = RunStore(self.root, run_id)
        store.write_state(Controller.new_state(
            run_id=run_id, repo_path=self.repo, project_name="project", spec_path=self.spec))
        assert self.run(run_id)[0] == 0
        self.built.clear()
        return store

    def park(self, run_id: str, *, provider: str = "claude", role: str = "qa",
             failure: N = N.USAGE_LIMIT, retry: datetime | None = None, receipt: bool = True,
             candidate: str = "NONE", attempts: int = 0, timer: bool = True,
             reason: str = "parked") -> RunStore:
        """The run `run_id`, parked from where it stands on `failure` that resets at `retry` (by
        default three hours after the real now), with the receipt and the timer the controller
        writes. A `candidate` other than NONE makes a receipt `verify_binding` refuses."""
        retry_at = _iso(retry or datetime.now(timezone.utc) + 3 * HOUR)
        store = RunStore(self.root, run_id)
        if store.state_path.exists():
            old = RunStore._parse_state(store.state_path.read_text())
        else:
            old = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                           spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                           profile_digest="h")
        stage = {"planner": Stage.PLANNING, "developer": Stage.DEVELOPING,
                 "qa": Stage.VERIFYING}[role]
        st = old.model_copy(update={
            "condition": Condition.BLOCKED, "blocked_kind": failure.value.lower(),
            "blocked_reason": reason, "retry_after": retry_at, "stage": stage, "iteration": 1,
            "resume_attempts": attempts})
        store.write_state(st)
        if receipt:
            binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id,
                                  iteration=st.iteration, attempt=st.attempt,
                                  stage=st.stage.value, candidate=candidate, master=NO_MASTER)
            dispatchrecovery.append_record(store.dir, RoutingDecision(
                park_id=park_id(binding, failure, retry_at), reason=failure.value,
                binding=binding,
                provenance=DispatchProvenance(
                    provider=provider, model="m-1", account_handle="acct-a", role=role,
                    capability_requirements=["read_repo", "write_worktree", "run_shell"]),
                retry_at=retry_at))
        if timer:
            TimerStore(self.root).arm_new(kind=TimerKind.PROVIDER_RETRY,
                                          due_at=_timer_form(retry_at), reason="t",
                                          program_epoch=0, run_id=run_id)
        return store

    def other(self, run_id: str = "other", *, provider: str = "codex",
              retry: datetime | None = None) -> RunStore:
        """Another run of the root, parked on `provider`'s limit (its own state; no timer)."""
        return self.park(run_id, provider=provider, role="developer", retry=retry, timer=False)

    def run(self, run_id: str = "r1", *extra: str):
        args = ["--root", str(self.root), "run", run_id, "--iterations", "0", "--no-herdr",
                "--routing-policy", str(self.policy_file), *extra]
        rc = cli.main(args)
        got = self.capsys.readouterr()
        return rc, got.out, got.err

    def resume(self) -> dict:
        assert cli.main(["--root", str(self.root), "resume-quota"]) == 0
        return json.loads(self.capsys.readouterr().out)

    def result(self, run_id: str = "r1", **where) -> dict:
        (rec,) = [r for r in self.resume()["results"] if r["target"] == run_id]
        return rec

    def timers(self) -> list:
        return TimerStore(self.root).read()


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


# --------------------------------------------------------------------------- #
# P-A -- the production path: `hoh resume-quota`, then `hoh run`
# --------------------------------------------------------------------------- #

def test_hoh_resume_quota_resumes_a_quota_parked_run_on_the_declared_fallback_and_hoh_run_builds_on_it(world):
    """Kills P1 (`plan_failover` does not read the park record: the dispatcher is built on the
    declared route), P2 (the park is no evidence about its own provider: the declared route reads
    healthy and nothing is lifted), P10 (the timer of the lifted park stays armed), P12 (the
    start-time record is rewritten or replaced instead of overlaid)."""
    store = world.started_run("r1")
    start = (store.dir / START).read_bytes()
    assert json.loads(start)["decisions"]["qa"]["outcome"] == "DECLARED"
    world.park("r1")                                       # claude hit its limit, three hours out
    first = world.result()
    assert first["action"] == "resumed_failover", first
    assert first["route"] == {"provider": "codex", "model": "gpt-5"}
    assert first["failover_record"] == PARK1 and first["attempt"] == 1
    assert first["claim"] == "written"
    state = RunStore(world.root, "r1").read_state()
    assert state.condition is Condition.ACTIVE and state.resume_attempts == 1
    assert any("declared fallback codex/gpt-5" in h for h in state.history)
    (timer,) = world.timers()
    assert timer.status is TimerStatus.DISARMED
    record = json.loads((store.dir / PARK1).read_bytes())
    assert record["schema"] == "hoh-routing-park-failover/1" and record["number"] == 1
    assert record["policy_digest"] == routing.load(world.policy_file).digest
    assert record["decisions"]["qa"]["outcome"] == "FALLBACK"
    assert record["decisions"]["qa"]["considered"][0]["state"] == "LIMITED"
    assert (store.dir / START).read_bytes() == start       # the start record is history
    # the command the resume loop names, through the entry point
    rc, out, err = world.run()
    assert rc == 0, err
    d = world.built[0]
    assert d.profiles[Role.QA] == "codex" and d.models[Role.QA] == "gpt-5"
    assert d.profiles[Role.PLANNER] == "claude"            # only the parked role moved
    seen = d.routing_evidence["failover"]
    assert seen["decisions"]["qa"]["outcome"] == "FALLBACK"
    assert [(f["file"], f["park_id"]) for f in seen["park_failovers"]] == [(PARK1, record["park_id"])]
    assert (store.dir / PARK1).stat().st_mode & 0o777 == 0o600      # Q1: the record is private
    assert (store.dir / START).read_bytes() == start and (store.dir / PARK1).exists()
    # terminal and quiet: the lifted park, its record and its timer are not processed again
    before = _tree(world.root)
    assert [r for r in world.resume()["results"] if r["target"] == "r1"] == []
    assert _tree(world.root) == before


def test_a_park_with_no_healthy_declared_fallback_waits_for_its_reset_and_a_later_pass_is_quiet(world):
    """Kills P7 (the first fallback is taken without asking its provider), P3 (something is
    written or lifted before the decision is made), and the quiet-terminal rule (a later pass
    rewrites a terminal answer)."""
    store = world.started_run("r1")
    world.park("r1")
    world.other("o", provider="codex", retry=datetime.now(timezone.utc) + 2 * HOUR)
    before = _tree(world.root)
    first = world.result()
    assert first["verdict"] == "NOT_DUE" and first["action"] == "refused"
    assert "no route the policy declares for the role is healthy" in first["failover_not_decided"]
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED
    assert not (store.dir / PARK1).exists()
    assert [t.status for t in world.timers()] == [TimerStatus.ARMED]
    assert _tree(world.root) == before                     # nothing written, nothing lifted
    second = world.result()
    assert second["failover_not_decided"] == first["failover_not_decided"]
    assert _tree(world.root) == before                     # a further pass: the same bytes


def test_the_first_healthy_declared_route_is_taken_in_the_declared_order(world):
    """Kills P7 again at the second step: the fallbacks are tried in order and a limited one is
    passed over (`not_before` of both passed-over routes bounds the return)."""
    world.policy(_doc({"provider": "codex", "model": "gpt-5"},
                      {"provider": "kimi", "model": "k2"}))
    store = world.started_run("r1")
    declared_reset = datetime.now(timezone.utc) + 5 * HOUR
    world.park("r1", retry=declared_reset)
    other_reset = datetime.now(timezone.utc) + 2 * HOUR
    world.other("o", provider="codex", retry=other_reset)
    rec = world.result()
    assert rec["route"] == {"provider": "kimi", "model": "k2"}
    record = json.loads((store.dir / PARK1).read_bytes())
    assert record["decisions"]["qa"]["index"] == 1
    # the return is bound by the route the run RETURNS TO (claude, the declared one, back in five
    # hours), not by the earliest route passed over (codex, back in two): the record is not spent
    # while the run would return to a limited provider
    assert datetime.fromisoformat(record["return_at"]) == declared_reset + timedelta(
        seconds=routing.RETURN_DWELL_SECONDS)


# --------------------------------------------------------------------------- #
# P-B -- the return, the bounds, the crash order
# --------------------------------------------------------------------------- #

def test_the_run_returns_to_the_route_it_began_on_when_the_record_is_spent(world, monkeypatch):
    """Kills P6 (the dwell is not added, or the spent boundary is `<=`: a record that returns at
    T is still read at T), P1 (the record is never spent: the run stays on the fallback)."""
    store = world.started_run("r1")
    retry = datetime.now(timezone.utc) + 3 * HOUR
    world.park("r1", retry=retry)
    world.resume()
    record = json.loads((store.dir / PARK1).read_bytes())
    returns = retry + timedelta(seconds=routing.RETURN_DWELL_SECONDS)
    assert datetime.fromisoformat(record["return_at"]) == returns
    policy = routing.load(world.policy_file)
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}

    def plan(now):
        p = routing.plan_failover(policy, run_dir=store.dir, profiles=profiles, now=now,
                                  begun=True)
        try:
            return p.resolved.profiles[Role.QA], p.decided
        finally:
            p.close()

    assert plan(returns - timedelta(seconds=1)) == ("codex", True)
    assert plan(returns) == ("claude", True)               # spent: the start record (DECLARED)
    assert plan(returns + 3 * HOUR) == ("claude", True)
    # the same boundary from the other side: a begun run with no start record stays declared
    bare = RunStore(world.root, "bare")
    bare.write_state(Controller.new_state(run_id="bare", repo_path=world.repo,
                                          project_name="p", spec_path=world.spec))
    (bare.dir / PARK1).write_bytes((store.dir / PARK1).read_bytes())
    p = routing.plan_failover(policy, run_dir=bare.dir, profiles=profiles,
                              now=returns + timedelta(seconds=1), begun=True)
    try:
        assert p.stays and p.resolved.profiles[Role.QA] == "claude"
    finally:
        p.close()
    # and through the command: `hoh run` after `return_at` builds on the route it began on
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            m = returns + timedelta(seconds=1)
            return cls(m.year, m.month, m.day, m.hour, m.minute, m.second, m.microsecond,
                       tzinfo=m.tzinfo)

    monkeypatch.setattr(routing, "datetime", Later)
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.built[-1].profiles[Role.QA] == "claude"
    assert "park_failover" not in world.built[-1].routing_evidence["failover"]


def test_the_decision_is_recorded_before_the_park_is_lifted_and_a_finished_attempt_is_reused(world, monkeypatch):
    """Kills P3 (the lift comes first: a record that cannot be written leaves a lifted park with
    no decision), P11 (an attempt that crashed after the record writes a second record instead of
    finishing the first), P4 (a refused record still costs a resume attempt)."""
    store = world.started_run("r1")
    world.park("r1")
    real = routing.durable_create

    def broken(path, data, **kw):
        if Path(path).name.startswith("routing-failover-park-"):
            raise OSError(28, "No space left on device")
        return real(path, data, **kw)

    monkeypatch.setattr(routing, "durable_create", broken)
    rec = world.result()
    assert "cannot be recorded" in rec["failover_not_decided"]
    st = RunStore(world.root, "r1").read_state()
    assert st.condition is Condition.BLOCKED and st.resume_attempts == 0
    assert not (store.dir / PARK1).exists()
    monkeypatch.undo()
    monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)
    # the record is written, then the process dies before the lift: the next pass finishes it
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    routing.record_park_failover(routing.load(world.policy_file), run_dir=store.dir,
                                 receipt=receipt)
    assert (store.dir / PARK1).exists()
    rec = world.result()
    assert rec["action"] == "resumed_failover" and rec["failover_record"] == PARK1
    assert not (store.dir / "routing-failover-park-2.json").exists()
    assert RunStore(world.root, "r1").read_state().condition is Condition.ACTIVE


def test_the_attempts_and_the_records_of_a_run_are_bounded(world):
    """Kills P4 (a park failover is not a resume attempt: five unproductive ones are not the
    limit), P5 (`MAX_PARK_FAILOVERS` is raised or not checked: a ninth record is written), P13
    (the count of records is not read back: a second pass writes more)."""
    assert (routing.MAX_PARK_FAILOVERS, routing.RETURN_DWELL_SECONDS) == (8, 600)
    store = world.started_run("r1")
    for cycle in range(1, routing.MAX_PARK_FAILOVERS + 1):
        world.park("r1", retry=datetime.now(timezone.utc) + (2 + cycle) * HOUR, timer=False)
        rec = world.result()
        assert rec["action"] == "resumed_failover" and rec["failover_record"] == \
            f"routing-failover-park-{cycle}.json", rec
        st = RunStore(world.root, "r1").read_state()
        assert st.resume_attempts == 1                    # unproductive attempts are counted ...
        RunStore(world.root, "r1").write_state(st.model_copy(update={"resume_attempts": 0}))
    # ... and a productive iteration (attempts back to 0) does not lift the record budget
    world.park("r1", retry=datetime.now(timezone.utc) + 20 * HOUR, timer=False)
    rec = world.result()
    assert rec["action"] == "waiting" and "park failovers" in rec["failover_not_decided"]
    assert not (store.dir / "routing-failover-park-9.json").exists()
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED
    before = _tree(world.root)
    assert world.result()["failover_not_decided"] == rec["failover_not_decided"]
    assert _tree(world.root) == before                    # terminal and quiet


def test_five_unproductive_park_failovers_are_the_limit_of_the_resume_loop(world):
    """Kills P4 (a park failover does not count as a resume attempt, or the bound is not read: a
    sixth one is made) -- on the timer path and on the legacy path, which end differently."""
    store = world.started_run("r1")
    for attempt in range(1, 6):
        world.park("r1", retry=datetime.now(timezone.utc) + (2 + attempt) * HOUR,
                   attempts=attempt - 1, timer=False)
        assert world.result()["action"] == "resumed_failover"
        assert RunStore(world.root, "r1").read_state().resume_attempts == attempt
    world.park("r1", retry=datetime.now(timezone.utc) + 9 * HOUR, attempts=5)
    rec = world.result()                                   # a timer: the park is not due
    assert rec["verdict"] == "NOT_DUE" and "5 resume attempts are used" in rec[
        "failover_not_decided"]
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED
    assert not (store.dir / "routing-failover-park-6.json").exists()
    other = world.started_run("r2")                        # no timer: the legacy path
    world.park("r2", retry=datetime.now(timezone.utc) + 9 * HOUR, attempts=5, timer=False)
    assert world.result("r2")["action"] == "given_up"      # the loop's own terminal state
    assert not (other.dir / PARK1).exists()


# --------------------------------------------------------------------------- #
# P-C -- a park that cannot be verified, or that is not ours, waits
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("what", ["foreign_candidate", "no_receipt", "held_by_violation",
                                  "role_timeout", "undeclared_role"])
def test_a_park_that_does_not_verify_or_says_nothing_about_a_provider_waits(world, what):
    """Kills P8 (the park is not verified before it is lifted: a forged or stale receipt moves
    the run), P14 (a failure class that says nothing about a provider is taken as a limit), P15
    (a capability-violation hold is lifted by a fallback)."""
    world.policy(_doc())
    store = world.started_run("r1")
    kw = {"foreign_candidate": {"candidate": "not-the-candidate"},
          "no_receipt": {"receipt": False},
          "held_by_violation": {"reason": "capability violation: wrote outside scope"},
          "role_timeout": {"failure": N.ROLE_TIMEOUT},
          "undeclared_role": {"role": "planner"}}[what]
    world.park("r1", **kw)
    before = _tree(world.root)
    rec = world.result()
    assert rec["action"] == "refused" or rec["verdict"] != "RESUME", rec
    assert rec.get("action") != "resumed_failover"
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED
    assert not (store.dir / PARK1).exists()
    after = _tree(world.root)
    # `hoh resume-quota` also runs the double-down (P3-42, on by default): a hold only a person can
    # lift gets a create-only dossier under `<run>/dd/` and nothing else moves -- the run, its park,
    # its timer and the root stay byte-identical; a block that waits on its bound gets none
    kept = {k: v for k, v in after.items() if k.split("/")[:2] != ["r1", "dd"]}
    assert kept == before
    assert (kept != after) == (what == "held_by_violation")


def test_a_run_without_a_pinned_policy_or_without_fallbacks_is_reported_exactly_as_before(world):
    """Kills P16 (the new field appears for every run: the shape of every other pass changes)."""
    bare = RunStore(world.root, "b1")
    bare.write_state(Controller.new_state(run_id="b1", repo_path=world.repo, project_name="p",
                                          spec_path=world.spec))
    world.park("b1")
    rec = world.result("b1")
    assert (rec["verdict"], rec["action"]) == ("NOT_DUE", "refused")
    assert "failover_not_decided" not in rec
    world.policy({"schema": routing.SCHEMA, "default": {"model": "sonnet"},
                  "roles": {"qa": {"provider": "claude", "model": "opus"}}})
    world.started_run("r2")
    world.park("r2")
    rec = world.result("r2")
    assert "failover_not_decided" not in rec and rec["action"] == "refused"


def test_the_reset_ceiling_and_a_past_reset(world):
    """Kills P17 (the ceiling is not `quota.MAX_STATED_DATE_WAIT`, or not inclusive), P18 (a
    park whose reset has passed is failed over instead of resumed by the ordinary path)."""
    from hoh import quota

    policy = routing.load(_write(world.tmp / "p.json", _doc()))
    store = world.started_run("r1")
    world.park("r1", retry=AT + quota.MAX_STATED_DATE_WAIT, timer=False)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    made = routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt, now=AT)
    assert made.number == 1                                # exactly at the ceiling: a limit
    world.park("r1", retry=AT + quota.MAX_STATED_DATE_WAIT + timedelta(seconds=1), timer=False)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    with pytest.raises(RoutingRefused, match="further away"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt, now=AT)
    world.park("r1", retry=AT - timedelta(seconds=1), timer=False)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    with pytest.raises(RoutingRefused, match="past"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt, now=AT)
    world.park("r1", retry=AT, timer=False)               # a reset that is exactly now has come
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    with pytest.raises(RoutingRefused, match="past"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt, now=AT)
    with pytest.raises(RoutingRefused, match="timezone-aware"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt,
                                     now=AT.replace(tzinfo=None))


def _write(path: Path, doc: dict) -> Path:
    path.write_bytes(json.dumps(doc).encode())
    return path


# --------------------------------------------------------------------------- #
# P-D -- damaged and hostile records fail closed
# --------------------------------------------------------------------------- #

def _good_record(world) -> tuple:
    store = world.started_run("r1")
    world.park("r1")
    assert world.result()["action"] == "resumed_failover"
    return store, json.loads((store.dir / PARK1).read_bytes())


def _redo(doc: dict) -> bytes:
    body = {k: v for k, v in doc.items() if k != "digest"}
    import hashlib
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode("ascii")).hexdigest()
    return (json.dumps({**body, "digest": digest}, sort_keys=True) + "\n").encode()


def _damages(doc: dict) -> dict:
    other = json.loads(json.dumps(doc))
    other["policy_digest"] = "0" * 64
    wrong_number = json.loads(json.dumps(doc))
    wrong_number["number"] = 2
    declared = json.loads(json.dumps(doc))
    declared["decisions"]["qa"].update(outcome="DECLARED", index=None)
    naive = json.loads(json.dumps(doc))
    naive["return_at"] = "2026-10-19T12:00:00"
    extra = json.loads(json.dumps(doc))
    extra["extra"] = 1
    two_roles = json.loads(json.dumps(doc))
    two_roles["decisions"]["developer"] = two_roles["decisions"]["qa"]
    schema = json.loads(json.dumps(doc))
    schema["schema"] = "hoh-routing-park-failover/2"
    bad_id = json.loads(json.dumps(doc))
    bad_id["park_id"] = "../not-a-park-id"
    before = json.loads(json.dumps(doc))
    before["return_at"] = "2020-01-01T00:00:00+00:00"
    consistent = json.loads(json.dumps(doc))             # internally consistent, and DECLARED
    consistent["decisions"]["qa"] = {
        "outcome": "DECLARED", "index": None,
        "route": {"provider": "claude", "model": "opus", "effort": "high"},
        "reason": "NO_EVIDENCE", "trust_assessed": False,
        "considered": [{"provider": "claude", "model": "opus", "state": "NO_EVIDENCE",
                        "not_before": None}]}
    return {
        "not_json": b"{",
        "nul": b'{"schema":\x00}',
        "lone_surrogate": b'{"schema": "\\ud800"}',
        "latin_1": b'{"schema": "\xe9"}',
        "a_list": b"[]",
        "oversize": b" " * (routing.MAX_FAILOVER_BYTES + 1),
        "digest_broken": json.dumps({**doc, "digest": "0" * 64}).encode(),
        "other_policy": _redo(other),
        "wrong_number": _redo(wrong_number),
        "names_the_declared_route": _redo(declared),
        "naive_time": _redo(naive),
        "wrong_schema": _redo(schema),
        "bad_park_id": _redo(bad_id),
        "returns_before_decided": _redo(before),
        "declared_but_consistent": _redo(consistent),
        "extra_key": _redo(extra),
        "two_roles": _redo(two_roles),
    }


@pytest.mark.parametrize("damage", sorted(_damages({"decisions": {"qa": {}}})) + ["symlink",
                                                                                 "directory",
                                                                                 "fifo",
                                                                                 "two_names"])
def test_a_damaged_park_failover_record_refuses_the_plan_and_blocks_a_new_decision(world, damage):
    """Kills P19 (a damaged record is read as 'no record': the run is started on the declared
    route silently, or a second record is written over the first's number), P20 (the route is
    taken from the record, not from the policy)."""
    store, doc = _good_record(world)
    path = store.dir / PARK1
    planted = _damages(doc)
    original = path.read_bytes()
    if damage in planted:
        path.write_bytes(planted[damage])
    else:
        moved = store.dir / "elsewhere.json"
        moved.write_bytes(original)
        path.rename(store.dir / "park1.moved")             # versioned out of the way, not removed
        if damage == "symlink":
            path.symlink_to(moved)
        elif damage == "directory":
            path.mkdir()
        elif damage == "fifo":
            os.mkfifo(path)
        elif damage == "two_names":
            os.link(moved, path)
    policy = routing.load(world.policy_file)
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    with pytest.raises(RoutingRefused):
        routing.plan_failover(policy, run_dir=store.dir, profiles=profiles, begun=True)
    rc, out, err = world.run()
    assert rc == 2 and "routing policy refused" in err
    world.park("r1", retry=datetime.now(timezone.utc) + 6 * HOUR, timer=False)
    rec = world.result()
    assert rec.get("action") != "resumed_failover" and "failover_not_decided" in rec
    assert not (store.dir / "routing-failover-park-2.json").exists()
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED


def test_a_planted_record_outside_the_numbered_names_is_not_read(world):
    store, doc = _good_record(world)
    (store.dir / "routing-failover-park-9.json").write_bytes(b"not json")
    (store.dir / "routing-failover-park-0.json").write_bytes(b"not json")
    (store.dir / "routing-failover-park-01.json").write_bytes(b"not json")
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "codex"


def test_a_record_run_directory_or_pin_that_cannot_be_trusted_is_refused(world):
    """Kills P22 (a run directory that is a link is followed), P23 (a pin that cannot be read is
    taken for 'no policy': the park is lifted, or reported as if nothing were declared)."""
    store = world.started_run("r1")
    world.park("r1")
    policy = routing.load(world.policy_file)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    link = world.tmp / "link-to-r1"
    link.symlink_to(store.dir)
    with pytest.raises(RoutingRefused, match="cannot be opened"):
        routing.record_park_failover(policy, run_dir=link, receipt=receipt)
    assert not (store.dir / PARK1).exists()
    pin = store.dir / cli.ROUTING_RECORD
    good = pin.read_bytes()
    pin.write_bytes(b"\xff not json")
    rec = world.result()
    assert rec.get("action") != "resumed_failover" and "cannot be read" in rec[
        "failover_not_decided"]
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED
    pin.write_bytes(good)
    assert world.result()["action"] == "resumed_failover"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_park_failover_record_is_a_refusal_not_an_absence(world):
    store, _doc = _good_record(world)
    (store.dir / PARK1).chmod(0)
    try:
        policy = routing.load(world.policy_file)
        profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
        with pytest.raises(RoutingRefused):
            routing.plan_failover(policy, run_dir=store.dir, profiles=profiles, begun=True)
    finally:
        (store.dir / PARK1).chmod(0o600)


def test_the_decision_is_made_at_the_moment_the_pass_was_given(world):
    """Kills P24 (the pass reads the clock again: the same pass at the same moment decides
    differently, and a replayed or tested moment is ignored)."""
    store = world.started_run("r1")
    world.park("r1", retry=AT + 3 * HOUR)
    (rec,) = [r for r in resume_runs(world.root, now="2026-10-19T12:00:00Z")
              if r["target"] == "r1"]
    assert rec["action"] == "resumed_failover"
    record = json.loads((store.dir / PARK1).read_bytes())
    assert record["decided_at"] == _iso(AT)
    assert record["return_at"] == _iso(AT + 3 * HOUR + timedelta(seconds=600))


def test_a_run_held_by_a_capability_violation_is_not_failed_over_even_when_asked_directly(world):
    """Kills P15 (the hold guard of `_failover_run` is dropped): every other route to this
    function turns a held run away first, so the guard is tested where it stands."""
    from hoh import quota, stages

    store = world.started_run("r1")
    world.park("r1", reason="capability violation: wrote outside scope")
    st = RunStore(world.root, "r1").read_state()
    assert stages.violation_unresolved(st)
    before = _tree(world.root)
    got = resumeloop._failover_run(RunStore(world.root, "r1"), st, timer_id=None,
                                   timers_store=TimerStore(world.root),
                                   when=datetime.now(timezone.utc), stages=stages, quota=quota)
    assert got == {}
    assert _tree(world.root) == before and not (store.dir / PARK1).exists()


def test_a_failure_inside_the_failover_does_not_stop_the_pass_for_the_other_runs(world, monkeypatch):
    """Kills P26 (an exception of the new path leaves the pass: a due run behind it is not
    resumed, the unit that is the only thing moving a quota wait stops)."""
    world.started_run("r1")
    world.park("r1", retry=AT + 3 * HOUR)
    world.started_run("r2")
    world.park("r2", provider="kimi", retry=AT - HOUR)    # due, with its timer: the ordinary path
    boom = RuntimeError("a defect")

    def broken(*a, **kw):
        raise boom

    monkeypatch.setattr(routing, "record_park_failover", broken)
    got = {r["target"]: r for r in resume_runs(world.root, now="2026-10-19T12:00:00Z")}
    assert got["r2"]["action"] == "resumed"
    assert got["r1"]["failover_not_decided"] == "internal error (RuntimeError)"   # no text of the defect
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED


def test_an_explicit_model_for_the_failed_over_role_is_refused_by_name(world):
    """The explicit `--role-model` of an operator is bound to the declared route: the resumed
    run is refused with the reason, not started on a model nobody declared for the fallback."""
    world.started_run("r1")
    world.park("r1")
    assert world.result()["action"] == "resumed_failover"
    rc, out, err = world.run("r1", "--role-model", "qa=opus")
    assert rc == 2 and "routing policy refused" in err and "explicit model" in err
    assert not world.built


# --------------------------------------------------------------------------- #
# P-D2 -- a park the real controller wrote, a governed master, a project node
# --------------------------------------------------------------------------- #

def _real_park(tmp_path, *, declare=None):
    """A real controller parks a QA dispatch on `claude`'s usage limit (the banner of the
    suite's own fixtures); the run is bound to a policy that declares `codex` for QA."""
    repo = _fresh_repo(tmp_path)
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    fake.models = {Role.QA: "opus"}
    if declare is not None:
        dispatchrecovery.declare_master(store.dir, session=declare[0], epoch=declare[1])
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    ctrl.run_iteration(state)
    policy_file = tmp_path / "policy.json"
    policy_file.write_bytes(json.dumps(_doc()).encode())
    policy = routing.load(str(policy_file))
    (store.dir / cli.ROUTING_RECORD).write_text(
        json.dumps(cli._routing_pin_record(policy, "test")), encoding="utf-8")
    return store, policy


def test_a_park_the_real_controller_wrote_is_failed_over_with_the_route_it_names(tmp_path):
    """Kills P25 (the decision reads a receipt shape the controller does not write: the role, the
    provider or the reset of the real receipt)."""
    store, policy = _real_park(tmp_path)
    state = store.read_state()
    assert state.condition is Condition.BLOCKED and state.blocked_kind == "usage_limit"
    (rec,) = resume_runs(tmp_path / "runs", now=datetime.now(timezone.utc))
    assert rec["action"] == "resumed_failover" and rec["route"]["provider"] == "codex", rec
    assert rec["binding"] == "BOUND"
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    plan = routing.plan_failover(policy, run_dir=store.dir, profiles=profiles, begun=True)
    try:
        assert plan.decided and plan.resolved.profiles[Role.QA] == "codex"
    finally:
        plan.close()


def test_a_governed_park_under_a_live_master_waits_and_the_node_follows_a_released_one(tmp_path):
    """The continuation unit's shape: a project node parked on a run that a master declared.
    While the master lives the consumer does not take the work (and decides nothing); once it
    has ended its drive the run is failed over and the node goes with it, in one pass."""
    from hoh.timers import TimerStore as Timers

    project = ProjectStore(tmp_path / "runs", "p")
    project.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", run_id="r1", lifecycle=Lifecycle.RUNNING)]))
    master = MasterAuthority(project.dir, "S1", serialize=project.lock)
    master.acquire()
    store, _policy = _real_park(tmp_path, declare=("S1", master.epoch))
    due = _timer_form(store.read_state().retry_after)
    st = project.read_state()
    node = st.node("a")
    node.lifecycle, node.blocked_kind, node.retry_after = Lifecycle.BLOCKED, "usage_limit", due
    master.stamp(st)
    project.write_state(st)
    Timers(project.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=due, program_epoch=1,
                                run_id=node_timer_key("a"), node_id="a")
    before = _tree(tmp_path / "runs")
    (rec,) = resume_runs(tmp_path / "runs", now=datetime.now(timezone.utc))
    assert rec["action"] == "refused" and "a live master holds project p" in rec[
        "failover_not_decided"], rec
    assert _tree(tmp_path / "runs") == before
    master.release(reason="orderly end of drive")
    runs = resumeloop.resume_runs(tmp_path / "runs", now=datetime.now(timezone.utc))
    nodes = resumeloop.resume_projects(tmp_path / "runs", now=datetime.now(timezone.utc))
    (rec,) = runs
    assert rec["action"] == "resumed_failover" and rec["binding"] == "BOUND"
    (hit,) = [r for r in nodes if r.get("node") == "a"]
    assert hit["action"] == "resumed_failover", nodes
    assert project.read_state().node("a").lifecycle is Lifecycle.READY


# --------------------------------------------------------------------------- #
# P-E -- the project pass: the continuation unit
# --------------------------------------------------------------------------- #

def _project(world, *, run_id: str = "r1", retry: datetime | None = None, pid: str = "p",
             node: str = "a") -> ProjectStore:
    retry_at = _timer_form(_iso(retry or datetime.now(timezone.utc) + 3 * HOUR))
    s = ProjectStore(world.root, pid)
    s.create(ProjectState(project_id=pid, repo_path=str(world.repo), nodes=[
        TaskNode(id=node, lifecycle=Lifecycle.BLOCKED, blocked_kind="usage_limit",
                 retry_after=retry_at, run_id=run_id)]))
    TimerStore(s.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=retry_at, program_epoch=1,
                              run_id=node_timer_key(node), node_id=node)
    return s


def _unpark(store: RunStore) -> None:
    """A person unblocks the run by hand: active again, no claim written."""
    old = RunStore._parse_state(store.state_path.read_text())
    store.write_state(old.model_copy(update={"condition": Condition.ACTIVE, "blocked_kind": None,
                                             "retry_after": None, "blocked_reason": None}))


def test_a_node_whose_run_was_resumed_on_a_fallback_is_resumed_with_it(world):
    """Kills P9 (the node waits for its own timer although its run is on the fallback: the
    continuation unit has nothing to drive), P21 (the node's timer stays armed)."""
    world.started_run("r1")
    world.park("r1")
    s = _project(world)
    out = world.resume()
    (run,) = [r for r in out["results"] if r["target"] == "r1"]
    (node,) = [r for r in out["projects"] if r.get("node") == "a"]
    assert run["action"] == "resumed_failover"
    assert (node["verdict"], node["action"]) == ("LIFTED_BY_FAILOVER", "resumed_failover")
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    assert "declared fallback" in s.read_state().node("a").note
    assert [t.status for t in TimerStore(s.dir).read()] == [TimerStatus.DISARMED]
    before = _tree(world.root)
    again = world.resume()
    assert all(r.get("action") not in ("resumed_failover", "resumed") for r in again["projects"])
    assert _tree(world.root) == before                    # quiet


def test_a_node_whose_run_was_unblocked_by_hand_keeps_waiting_for_its_timer(world):
    """Kills P9 at its other end: a run that is merely not parked any more, with no failover
    behind it, does not lift its node early."""
    store = world.started_run("r1")
    world.park("r1", timer=False)
    s = _project(world)
    old = RunStore._parse_state(store.state_path.read_text())
    store.write_state(old.model_copy(update={"condition": Condition.ACTIVE, "blocked_kind": None,
                                             "retry_after": None, "blocked_reason": None}))
    out = world.resume()
    assert [r for r in out["projects"] if r.get("node") == "a" and "action" in r
            and r["action"] in ("resumed", "resumed_failover")] == []
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED
    assert [t.status for t in TimerStore(s.dir).read()] == [TimerStatus.ARMED]


def test_a_node_is_not_lifted_by_the_record_of_an_earlier_park_or_of_a_park_nobody_claimed(world):
    """Kills P9b (the record is not compared with the run's newest park: the record of a park
    long lifted lifts the node of the next one), P9c (a record whose park was never claimed -- the
    loop did not lift it -- lifts the node)."""
    first = world.started_run("r1")
    second = world.started_run("r2")        # begun before r1's failover: that record is evidence now
    world.park("r1")
    assert world.result()["action"] == "resumed_failover"          # park one: recorded and lifted
    s = _project(world)                                            # the node waits on a LATER park
    world.park("r1", retry=datetime.now(timezone.utc) + 6 * HOUR, timer=False)
    _unpark(first)                                                 # ... which a person lifted
    out = world.resume()
    assert [r for r in out["projects"] if r.get("action") in ("resumed", "resumed_failover")] == []
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED
    # a record for the run's newest park that the loop never lifted (no claim)
    world.park("r2", timer=False)
    policy = routing.load(world.policy_file)
    routing.record_park_failover(policy, run_dir=second.dir,
                                 receipt=dispatchrecovery.read_records(second.dir)[0][-1])
    _unpark(second)
    t = _project(world, run_id="r2", pid="q", node="b")
    out = world.resume()
    assert [r for r in out["projects"] if r.get("node") == "b"
            and r.get("action") in ("resumed", "resumed_failover")] == []
    assert t.read_state().node("b").lifecycle is Lifecycle.BLOCKED


def test_a_node_stays_while_its_run_is_parked_again_on_the_same_receipt(world):
    """Kills L7 (the node is lifted although its run is BLOCKED: the run is not on its way)."""
    store = world.started_run("r1")
    world.park("r1")
    assert world.result()["action"] == "resumed_failover"
    old = RunStore._parse_state(store.state_path.read_text())
    store.write_state(old.model_copy(update={"condition": Condition.BLOCKED,
                                             "blocked_kind": "usage_limit",
                                             "blocked_reason": "parked",
                                             "retry_after": old.retry_after}))
    s = _project(world)
    out = world.resume()
    assert [r for r in out["projects"] if r.get("node") == "a"
            and r.get("action") in ("resumed", "resumed_failover")] == []
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_latest_park_failover_reads_only_a_record_that_is_its_own_and_has_a_park_id(world):
    """Kills J1 (the digest is not checked: a forged record lifts a node) and A6 (a park id that
    is not 32 hex digits reaches the claim path)."""
    store, doc = _good_record(world)
    path = store.dir / PARK1
    assert routing.latest_park_failover(store.dir)["park_id"] == doc["park_id"]
    path.write_bytes(json.dumps({**doc, "digest": "0" * 64}).encode())
    assert routing.latest_park_failover(store.dir) is None
    path.write_bytes(_redo({**doc, "park_id": "../../escape"}))
    assert routing.latest_park_failover(store.dir) is None
    assert routing.latest_park_failover(world.tmp / "no-such-run") is None


def test_a_spent_record_of_the_parked_receipt_is_refused_and_no_second_record_is_written(world):
    """Kills M1b (an attempt that crashed after the record, finished after `return_at`, writes a
    second record for the same park)."""
    store, doc = _good_record(world)
    policy = routing.load(world.policy_file)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    spent = datetime.fromisoformat(doc["return_at"]) + timedelta(seconds=1)
    with pytest.raises(RoutingRefused, match="spent"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt, now=spent)
    assert not (store.dir / "routing-failover-park-2.json").exists()


def test_a_park_on_a_provider_the_policy_does_not_declare_for_the_role_is_refused(world):
    """Kills H1 (a park on a route nobody declared is failed over: an explicit model or a policy
    that changed)."""
    store = world.started_run("r1")
    world.park("r1", provider="pi")
    policy = routing.load(world.policy_file)
    receipt = dispatchrecovery.read_records(store.dir)[0][-1]
    with pytest.raises(RoutingRefused, match="does not declare"):
        routing.record_park_failover(policy, run_dir=store.dir, receipt=receipt)
    assert not (store.dir / PARK1).exists()


def test_a_class_the_pass_does_not_own_is_not_failed_over(world):
    """Kills X1 (the hook runs for a block of a class the pass was not asked to resume)."""
    store = world.started_run("r1")
    world.park("r1")
    (rec,) = [r for r in resume_runs(world.root, now=datetime.now(timezone.utc),
                                     classes=frozenset({"provider_unavailable"}))
              if r["target"] == "r1"]
    assert "failover_not_decided" not in rec and rec.get("action") != "resumed_failover"
    assert not (store.dir / PARK1).exists()
    assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED


# --------------------------------------------------------------------------- #
# P-G -- which failures may fail a run over; every role; a run that began on a fallback
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("failure, ahead, goes, state", [
    (N.USAGE_LIMIT, 60, True, "LIMITED"),                    # a usage limit states its own reset
    (N.PROVIDER_UNAVAILABLE, 900, True, "DEGRADED"),         # a long outage: exactly the minimum
    (N.PROVIDER_UNAVAILABLE, 899, False, ""),                # a blip waits
    (N.NETWORK_TRANSIENT, 120, False, ""),
    (N.NETWORK_TRANSIENT, 7200, True, "DEGRADED"),
    (N.RATE_LIMIT, 300, False, ""),
    (N.RATE_LIMIT, 3600, True, "LIMITED"),
])
def test_a_long_availability_outage_fails_over_and_a_short_blip_waits_and_spends_no_record(
        world, failure, ahead, goes, state):
    """Kills E4 (an availability failure is no evidence), E1 (an availability failure is recorded
    as a limit), M2 (a blip fails over and spends a record of the run's budget), M2b (the minimum
    is exclusive or another bound)."""
    assert routing.MIN_FAILOVER_WAIT_SECONDS == 900
    store = world.started_run("r1")
    world.park("r1", failure=failure, retry=AT + timedelta(seconds=ahead))
    (rec,) = [r for r in resume_runs(world.root, now="2026-10-19T12:00:00Z") if r["target"] == "r1"]
    if goes:
        assert rec["action"] == "resumed_failover", rec
        record = json.loads((store.dir / PARK1).read_bytes())
        assert record["decisions"]["qa"]["considered"][0]["state"] == state
    else:
        assert "short wait" in rec["failover_not_decided"], rec
        assert not (store.dir / PARK1).exists()
        assert RunStore(world.root, "r1").read_state().condition is Condition.BLOCKED


def _two_roles(*qa_fallbacks) -> dict:
    return {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
            "roles": {"planner": {"provider": "claude", "model": "opus",
                                  "fallbacks": [{"provider": "codex", "model": "gpt-5"}]},
                      "qa": {"provider": "kimi", "model": "k2",
                             "fallbacks": list(qa_fallbacks) or [{"provider": "pi", "model": "p1"}]}}}


def _group(provider: str, hours: int) -> health.ProviderHealth:
    return health.ProviderHealth(
        provider=provider, account_handle="a", state=health.HealthState.LIMITED,
        not_before=_iso(AT + hours * HOUR), runs=("x",), failures=(("USAGE_LIMIT", 1),))


def _reader(*groups):
    report = health.HealthReport(at=_iso(AT), providers=tuple(groups), unattributed=(),
                                 problems=(), runs_seen=1)
    return lambda root, now, exclude_runs: report


def _receipt(role, provider, model, park_id, hours=3):
    from types import SimpleNamespace
    return SimpleNamespace(
        provenance=SimpleNamespace(role=role, provider=provider, account_handle="a", model=model),
        park_id=park_id, reason="USAGE_LIMIT", retry_at=_iso(AT + hours * HOUR))


def _plan(policy, run, at):
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}
    plan = routing.plan_failover(policy, run_dir=run, profiles=profiles, now=at, begun=True)
    try:
        return {r.value: plan.resolved.profiles[r] for r in (Role.PLANNER, Role.QA)}
    finally:
        plan.close()


def test_every_unspent_park_record_applies_and_the_newest_of_a_role_wins(tmp_path):
    """Kills OV0 (only the newest record applies: a weekly limit that hits two roles moves the
    last one and drops the first -- the motivating case), D1 (a record replaces the start
    record's decisions: a run that began on a fallback is moved back to the limited provider),
    OV1 (the oldest record of a role wins)."""
    from hoh.contracts import Role as R
    policy_file = tmp_path / "p.json"
    policy_file.write_bytes(json.dumps(_two_roles(
        {"provider": "pi", "model": "p1"}, {"provider": "codex", "model": "gpt-5"})).encode())
    policy = routing.load(str(policy_file))
    runs = tmp_path / "runs"
    # 1. two roles, two parks, both unspent: both fallbacks apply
    one = runs / "r1"
    one.mkdir(parents=True)
    routing.record_park_failover(policy, run_dir=one, receipt=_receipt("qa", "kimi", "k2", "a" * 32),
                                 now=AT, reader=_reader())
    routing.record_park_failover(policy, run_dir=one, receipt=_receipt(
        "planner", "claude", "opus", "b" * 32), now=AT + timedelta(minutes=1), reader=_reader())
    assert _plan(policy, one, AT + timedelta(minutes=2)) == {"planner": "codex", "qa": "pi"}
    # 2. a run that STARTED on the planner's fallback, then fails over on qa: planner stays
    two = tmp_path / "runs-two" / "r2"       # a root of its own: r1's records are evidence since
    two.mkdir(parents=True)                   # P3-41 slice 4 and would move r2's start by themselves
    start = routing.plan_failover(
        policy, run_dir=two, profiles={R.PLANNER: "claude", R.DEVELOPER: "claude", R.QA: "kimi"},
        now=AT, reader=_reader(_group("claude", 5)))
    start.commit()
    start.close()
    routing.record_park_failover(policy, run_dir=two, receipt=_receipt("qa", "kimi", "k2", "c" * 32),
                                 now=AT, reader=_reader(_group("claude", 5)))
    assert _plan(policy, two, AT + timedelta(minutes=1)) == {"planner": "codex", "qa": "pi"}
    # 3. the newest of a role wins: qa parks again on pi, kimi still out -> the second fallback
    routing.record_park_failover(policy, run_dir=two, receipt=_receipt("qa", "pi", "p1", "d" * 32),
                                 now=AT + timedelta(minutes=2), reader=_reader(_group("kimi", 5)))
    assert _plan(policy, two, AT + timedelta(minutes=3)) == {"planner": "codex", "qa": "codex"}


def test_a_weekly_limit_that_hits_two_roles_moves_both_through_resume_quota_and_hoh_run(world):
    """The motivating case through the entry points: a weekly limit parks the QA stage, the run is
    resumed on the fallback, then parks again on the PLANNING stage (the same provider is out for
    both): both roles run on the fallback, without a third record."""
    doc = _doc()
    doc["roles"]["planner"] = {"provider": "claude", "model": "opus",
                               "fallbacks": [{"provider": "codex", "model": "gpt-5"}]}
    world.policy(doc)
    store = world.started_run("r1")
    world.park("r1", role="qa", retry=datetime.now(timezone.utc) + 5 * HOUR)
    assert world.result()["failover_record"] == PARK1
    world.park("r1", role="planner", retry=datetime.now(timezone.utc) + 4 * HOUR)
    assert world.result()["failover_record"] == "routing-failover-park-2.json"
    rc, out, err = world.run()
    assert rc == 0, err
    d = world.built[0]
    assert d.profiles[Role.QA] == "codex" and d.profiles[Role.PLANNER] == "codex"
    assert not (store.dir / "routing-failover-park-3.json").exists()


# --------------------------------------------------------------------------- #
# P-F -- the register and the symbols the capability matrix measures
# --------------------------------------------------------------------------- #

def test_the_register_probe_of_p3_41_names_this_slice():
    rows = {r["id"]: r for r in json.loads(
        (Path(__file__).resolve().parent.parent / "program/v3_3/REQUIREMENTS.json"
         ).read_text(encoding="utf-8"))["requirements"]}
    # P3-41 slice 4 moved the probe to `route_receipt`; this slice's probe is the previous one
    probe = rows["P3-41"]["previous_probe"]
    assert probe["symbol"] == "record_park_failover" and probe["module"] == "routing.py"
    assert probe["test"].startswith("tests/test_park_failover.py::test_hoh_resume_quota_resumes")
    assert probe.get("equivalent")                        # the row is PARTIAL, not PROVEN
    assert rows["P3-41"]["probe"]["equivalent"]
    assert probe["previous_probe"]["symbol"] == "plan_failover"
    assert health.PROVIDER_EVIDENCE[N.USAGE_LIMIT] is health.Evidence.LIMIT
