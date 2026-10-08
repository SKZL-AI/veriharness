"""The resume loop consumes due timers -- and only its own class of wait.

O211: `hoh.resumption` decided what a due timer should wake and nothing acted
on it. `hoh.resumeloop` is the consumer behind `hoh resume-quota`. These tests
pin what it may and may not touch at the run level (the project level, with
real processes and a new governed master, is `tools/durability_e2e.py`):

* a due `usage_limit` wait resumes, its timer is consumed exactly once, and a
  second pass does nothing;
* a question collected for the digest, a contract park, and a capability
  violation are all timed or blocked waits that belong to somebody else --
  none is resumed, and their timers stay armed;
* a timer from another programme epoch is refused;
* the legacy path (a quota wait from before timers were armed) still resumes,
  and says it had no timer.
"""

from __future__ import annotations

import json

from hoh import cli
from hoh.contracts import Condition, RunState
from hoh.resumeloop import QUOTA_CLASSES, resume_runs
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore

PAST = "2026-09-25T09:00:00Z"
NOW = "2026-09-25T10:00:00Z"
FUTURE = "2026-09-25T11:00:00Z"


def _run(root, rid, **kw) -> RunStore:
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p",
                               spec_path="/x/s.md", spec_digest="a" * 16,
                               policy_digest="b" * 16, profile_digest="c" * 16,
                               condition=Condition.BLOCKED, **kw))
    return store


def _timer(root, rid, due=PAST, epoch=0, kind=TimerKind.PROVIDER_RETRY):
    return TimerStore(root).arm_new(kind=kind, due_at=due, reason="t",
                                    program_epoch=epoch, run_id=rid)


def _by_target(recs):
    return {r["target"]: r for r in recs}


def test_a_due_quota_wait_resumes_once_and_its_timer_fires(tmp_path):
    _run(tmp_path, "q", blocked_kind="usage_limit", blocked_reason="usage limit reached",
         retry_after="2026-09-25T09:00:00+00:00")
    t = _timer(tmp_path, "q")
    first = _by_target(resume_runs(tmp_path, now=NOW))
    assert first["q"]["action"] == "resumed"
    assert RunStore(tmp_path, "q").read_state().condition is not Condition.BLOCKED
    [after] = TimerStore(tmp_path).read()
    assert after.timer_id == t.timer_id and after.status is TimerStatus.FIRED
    again = resume_runs(tmp_path, now=NOW)
    assert all(r["action"] != "resumed" for r in again)


def test_not_due_is_not_resumed(tmp_path):
    _run(tmp_path, "q", blocked_kind="usage_limit", retry_after=FUTURE.replace("Z", "+00:00"))
    _timer(tmp_path, "q", due=FUTURE)
    [r] = resume_runs(tmp_path, now=NOW)
    assert (r["verdict"], r["action"]) == ("NOT_DUE", "refused")
    assert RunStore(tmp_path, "q").read_state().condition is Condition.BLOCKED


def test_a_digest_question_is_a_timed_wait_that_is_not_ours(tmp_path):
    _run(tmp_path, "d", blocked_kind="collected_for_digest", blocked_reason="a question")
    _timer(tmp_path, "d", kind=TimerKind.EXTERNAL_WAIT)
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["verdict"] == "RESUME"            # resumption says it is a due wait ...
    assert r["action"] == "class_not_owned"    # ... and this consumer does not own it
    assert RunStore(tmp_path, "d").read_state().condition is Condition.BLOCKED
    assert [t.status for t in TimerStore(tmp_path).read()] == [TimerStatus.ARMED]


def test_a_contract_park_is_not_ours_either(tmp_path):
    _run(tmp_path, "c", blocked_kind="parked_by_contract", blocked_reason="decision")
    _timer(tmp_path, "c", kind=TimerKind.EXTERNAL_WAIT)
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["action"] == "class_not_owned"


def test_a_capability_violation_never_resumes(tmp_path):
    """The P2-20 shape: parked_by_contract written over a capability block."""
    _run(tmp_path, "v", blocked_kind="parked_by_contract",
         blocked_reason="capability violation: wrote outside scope")
    _timer(tmp_path, "v", kind=TimerKind.EXTERNAL_WAIT)
    for classes in (QUOTA_CLASSES, frozenset({"parked_by_contract"})):
        [r] = resume_runs(tmp_path, now=NOW, classes=classes)
        assert (r["verdict"], r["action"]) == ("NEEDS_OVERRIDE", "refused")
    assert RunStore(tmp_path, "v").read_state().condition is Condition.BLOCKED


def test_a_usage_limit_labelled_capability_violation_is_still_refused(tmp_path):
    _run(tmp_path, "v2", blocked_kind="usage_limit",
         blocked_reason="capability violation: network access")
    _timer(tmp_path, "v2")
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["verdict"] == "NEEDS_OVERRIDE"


def test_a_timer_from_another_epoch_is_refused(tmp_path):
    _run(tmp_path, "q", blocked_kind="usage_limit", retry_after=PAST.replace("Z", "+00:00"))
    _timer(tmp_path, "q", epoch=3)          # the run has no contract: epoch 0
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["verdict"] == "SUPERSEDED_EPOCH"
    assert RunStore(tmp_path, "q").read_state().condition is Condition.BLOCKED


def test_the_kill_list_wins(tmp_path):
    _run(tmp_path, "q", blocked_kind="usage_limit", retry_after=PAST.replace("Z", "+00:00"))
    _timer(tmp_path, "q")
    [r] = resume_runs(tmp_path, now=NOW, killed=frozenset({"q"}))
    assert r["verdict"] == "KILLED"


def test_the_legacy_quota_wait_without_a_timer_still_resumes(tmp_path):
    _run(tmp_path, "old", blocked_kind="usage_limit",
         retry_after="2026-09-01T00:00:00+00:00")
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["action"] == "resumed_without_timer"
    assert r["timer_id"] is None


def test_the_legacy_path_does_not_touch_other_classes(tmp_path):
    _run(tmp_path, "d", blocked_kind="collected_for_digest",
         retry_after="2026-09-01T00:00:00+00:00")
    assert resume_runs(tmp_path, now=NOW) == []


def test_resume_attempts_are_capped(tmp_path):
    _run(tmp_path, "q", blocked_kind="usage_limit", resume_attempts=5,
         retry_after=PAST.replace("Z", "+00:00"))
    _timer(tmp_path, "q")
    [r] = resume_runs(tmp_path, now=NOW)
    assert r["action"] == "given_up"
    assert [t.status for t in TimerStore(tmp_path).read()] == [TimerStatus.ARMED]


def test_the_cli_verb_is_the_consumer(tmp_path, capsys):
    _run(tmp_path, "q", blocked_kind="usage_limit", retry_after="2026-09-01T00:00:00+00:00")
    _timer(tmp_path, "q", due="2026-09-01T00:00:00Z")
    _run(tmp_path, "d", blocked_kind="collected_for_digest")
    _timer(tmp_path, "d", due="2026-09-01T00:00:00Z", kind=TimerKind.EXTERNAL_WAIT)
    assert cli.main(["--root", str(tmp_path), "resume-quota"]) == 0
    out = json.loads(capsys.readouterr().out)
    acts = {r["target"]: r["action"] for r in out["results"]}
    assert acts == {"q": "resumed", "d": "class_not_owned"}
    assert out["projects"] == []


def test_a_controller_quota_block_arms_a_timer(tmp_path):
    """The run layer's quota wait now leaves a durable timer, not only a field."""
    from hoh.controller import Controller

    store = _run(tmp_path, "r", blocked_kind=None)
    state = store.read_state()
    state.condition = Condition.ACTIVE
    ctl = Controller.__new__(Controller)
    ctl.store = store
    ctl._governor = False
    ctl._block(state, "usage limit reached; resets in 2h")
    assert state.blocked_kind == "usage_limit"
    [t] = TimerStore(tmp_path).read()
    assert (t.kind, t.run_id, t.program_epoch) == (TimerKind.PROVIDER_RETRY, "r", 0)


# --------------------------------------------------------------------------- #
# Project level: the crash invariant, the fallback, and recovery of dead masters
# --------------------------------------------------------------------------- #

from hoh.orchestrator import node_timer_key  # noqa: E402
from hoh.project import Lifecycle, ProjectState, TaskNode  # noqa: E402
from hoh.projectstore import ProjectStore  # noqa: E402
from hoh.resumeloop import recover_projects, resume_projects  # noqa: E402
from hoh.succession import MasterAuthority  # noqa: E402


def _parked_project(tmp_path, *, retry_after=PAST, timer=True, kind="usage_limit"):
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, blocked_kind=kind,
                 retry_after=retry_after)]))
    if timer:
        TimerStore(s.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=retry_after,
                                  program_epoch=1, run_id=node_timer_key("a"), node_id="a")
    return s


def test_project_resume_persists_ready_before_consuming(tmp_path, monkeypatch):
    """Crash between the two writes: the node is READY, the timer an orphan."""
    import hoh.resumeloop as R

    s = _parked_project(tmp_path)

    def crash(*a, **k):
        raise KeyboardInterrupt("simulated death between the two writes")
    monkeypatch.setattr(R, "consume", crash)
    try:
        resume_projects(tmp_path, now=NOW)
    except KeyboardInterrupt:
        pass
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    [t] = TimerStore(s.dir).read()
    assert t.status is TimerStatus.ARMED          # an orphan, never a stuck park
    monkeypatch.undo()
    recs = resume_projects(tmp_path, now=NOW)
    assert all(r["action"] != "resumed" for r in recs)
    assert any(r.get("verdict") == "RUN_NOT_PARKED" for r in recs)


def test_a_parked_node_without_a_timer_is_resumed_by_its_retry_after(tmp_path):
    s = _parked_project(tmp_path, timer=False)
    [r] = resume_projects(tmp_path, now=NOW)
    assert (r["verdict"], r["action"]) == ("PARKED_WITHOUT_TIMER", "resumed_without_timer")
    assert s.read_state().node("a").lifecycle is Lifecycle.READY


def test_a_parked_node_without_a_timer_waits_until_its_retry_after(tmp_path):
    s = _parked_project(tmp_path, timer=False, retry_after=FUTURE)
    [r] = resume_projects(tmp_path, now=NOW)
    assert r["action"] == "waiting"
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_the_fallback_does_not_touch_other_classes(tmp_path):
    s = _parked_project(tmp_path, timer=False, kind="collected_for_digest")
    assert resume_projects(tmp_path, now=NOW) == []
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_a_project_resume_is_refused_while_a_live_master_holds_it(tmp_path):
    s = _parked_project(tmp_path)
    MasterAuthority(s.dir, "live", serialize=s.lock).acquire()
    [r] = [r for r in resume_projects(tmp_path, now=NOW) if r.get("node") == "a"]
    assert r["action"] == "held_by_live_master"
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_recover_ignores_released_and_live_leases_and_undriven_projects(tmp_path):
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                          nodes=[TaskNode(id="a")]))
    assert recover_projects(tmp_path) == []           # no driver registered
    (s.dir / "driver.json").write_text('{"factory": "nothing:here"}')
    a = MasterAuthority(s.dir, "live", serialize=s.lock)
    a.acquire()
    assert recover_projects(tmp_path) == []           # live holder: never displaced
    a.release(reason="orderly halt")
    assert recover_projects(tmp_path) == []           # released: not a death


def test_recover_replaces_a_dead_master_with_a_new_epoch(tmp_path, monkeypatch):
    import hoh.resumeloop as R

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                          nodes=[TaskNode(id="a")]))
    (s.dir / "driver.json").write_text('{"factory": "x:y"}')
    dead = MasterAuthority(s.dir, "dead", pid=2 ** 22 + 7, serialize=s.lock,
                           alive=lambda p: False)
    dead.acquire()
    driven = []

    def fake_drive(store, authority, factory, **kw):
        driven.append(authority.epoch)
        from hoh.orchestrator import HaltClass, Result
        return Result(HaltClass.CLOSED, "fixture")
    class Idle:
        def evaluate(self, node):
            from hoh.orchestrator import RunOutcome, RunVerdict
            return RunOutcome(RunVerdict.NOT_STARTED, "")
    monkeypatch.setattr(R, "load_driver", lambda d: (lambda store: (Idle(), None)))
    monkeypatch.setattr(R, "drive_governed", fake_drive)
    [r] = recover_projects(tmp_path)
    assert r["action"] == "recovered" and r["epoch"] == 2 and driven == [2]
    assert "no longer exists" in r["previous"]


def test_a_broken_driver_is_reported_and_costs_no_epoch(tmp_path):
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                          nodes=[TaskNode(id="a")]))
    (s.dir / "driver.json").write_text('{"factory": "no_such_module_here:build"}')
    MasterAuthority(s.dir, "dead", pid=2 ** 22 + 9, alive=lambda p: False,
                    serialize=s.lock).acquire()
    [r] = recover_projects(tmp_path)
    assert r["action"] == "refused" and "cannot be loaded" in r["why"]
    from hoh.epoch import EpochStore
    assert len(EpochStore(s.dir).chain()) == 1


def test_a_raising_driver_releases_its_lease_and_other_projects_still_run(tmp_path):
    """Review A P2: no epoch per tick, no starvation of the next project."""
    from hoh.epoch import EpochStore

    for pid in ("p1", "p2"):
        s = ProjectStore(tmp_path, pid)
        s.create(ProjectState(project_id=pid, repo_path=str(tmp_path),
                              nodes=[TaskNode(id="a")]))
        MasterAuthority(s.dir, f"dead-{pid}", pid=2 ** 22 + 11, alive=lambda p: False,
                        serialize=s.lock).acquire()
    bad = tmp_path / "drv"
    bad.mkdir()
    (bad / "raising_driver.py").write_text(
        "def build(store):\n    raise RuntimeError('repo path gone')\n")
    for pid in ("p1", "p2"):
        (tmp_path / "projects" / pid / "driver.json").write_text(json.dumps(
            {"factory": "raising_driver:build", "path": str(bad)}))
    first = recover_projects(tmp_path)
    # Both projects reported -- p1's failure does not stop the pass for p2 --
    # and the driver is used before anything is acquired, so no epoch is spent.
    assert {r["target"]: r["action"] for r in first} == {"p1": "skipped", "p2": "skipped"}
    recover_projects(tmp_path)
    assert len(EpochStore(tmp_path / "projects" / "p1").chain()) == 1


def test_a_corrupt_lease_skips_only_its_own_project(tmp_path):
    good = _parked_project(tmp_path)
    bad = ProjectStore(tmp_path, "bad")
    bad.create(ProjectState(project_id="bad", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, blocked_kind="usage_limit",
                 retry_after=PAST)]))
    TimerStore(bad.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST,
                                program_epoch=1, run_id=node_timer_key("a"), node_id="a")
    (bad.dir / "lease.json").write_text("{not json")
    recs = resume_projects(tmp_path, now=NOW)
    by = {r["target"]: r["action"] for r in recs if r.get("node") == "a"}
    assert by == {"bad": "refused", "p": "resumed"}
    assert good.read_state().node("a").lifecycle is Lifecycle.READY


def test_resume_without_drive_releases_the_lease(tmp_path):
    s = _parked_project(tmp_path)
    resume_projects(tmp_path, now=NOW, drive=False)
    lease = json.loads((s.dir / "lease.json").read_text())
    assert lease["released_at"]



def _in_flight_project(tmp_path, *, lease_state):
    """A RUNNING node whose run another live process still executes."""
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]))
    (s.dir / "driver.json").write_text('{"factory": "x:y"}')
    m = MasterAuthority(s.dir, "m1", pid=2 ** 22 + 13, alive=lambda p: False,
                        serialize=s.lock)
    m.acquire()
    if lease_state == "released-in-flight":
        m.alive = lambda p: True
        m.release(reason="orderly end of drive: RUN_IN_FLIGHT")
    return s


def _fake_driver(monkeypatch, verdict):
    import hoh.resumeloop as R
    from hoh.orchestrator import RunOutcome

    class L:
        def evaluate(self, node):
            return RunOutcome(verdict, "run held by pid 4242")
    driven = []
    monkeypatch.setattr(R, "load_driver", lambda d: (lambda store: (L(), None)))
    monkeypatch.setattr(R, "drive_governed",
                        lambda store, a, f, **k: driven.append(a.epoch) or __import__(
                            "hoh.orchestrator", fromlist=["Result"]).Result(
                            __import__("hoh.orchestrator", fromlist=["HaltClass"]
                                       ).HaltClass.CLOSED, "x"))
    return driven


def test_recover_waits_for_an_in_flight_run_without_spending_an_epoch(tmp_path, monkeypatch):
    from hoh.epoch import EpochStore
    from hoh.orchestrator import RunVerdict

    s = _in_flight_project(tmp_path, lease_state="lost")
    driven = _fake_driver(monkeypatch, RunVerdict.IN_FLIGHT)
    [r] = recover_projects(tmp_path)
    assert r["action"] == "waiting_on_run" and driven == []
    assert len(EpochStore(s.dir).chain()) == 1


def test_recover_resumes_once_the_in_flight_run_has_finished(tmp_path, monkeypatch):
    from hoh.orchestrator import RunVerdict

    _in_flight_project(tmp_path, lease_state="released-in-flight")
    driven = _fake_driver(monkeypatch, RunVerdict.ACCEPTED)
    [r] = recover_projects(tmp_path)
    assert r["action"] == "recovered" and driven == [2]


def test_a_controller_halts_run_in_flight_and_writes_nothing(tmp_path):
    from hoh.orchestrator import GateRunner, HaltClass, ProjectController, RunOutcome, RunVerdict

    s = ProjectStore(tmp_path, "q")
    s.create(ProjectState(project_id="q", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]))
    before = s.read_state().write_seq

    class L:
        def evaluate(self, node):
            return RunOutcome(RunVerdict.IN_FLIGHT, "held")

    class G(GateRunner):
        def subject(self):
            return "x"

        def run(self, subject):
            return []
    r = ProjectController(s, L(), G()).run()
    assert r.halt is HaltClass.RUN_IN_FLIGHT
    assert s.read_state().write_seq == before


def test_a2_b_a_busy_program_lock_skips_only_that_project(tmp_path, monkeypatch):
    import hoh.resumeloop as R
    from hoh.orchestrator import RunOutcome, RunVerdict
    from hoh.store import LockBusy

    for pid in ("p1", "p2"):
        s = ProjectStore(tmp_path, pid)
        s.create(ProjectState(project_id=pid, repo_path=str(tmp_path),
                              nodes=[TaskNode(id="a")]))
        (s.dir / "driver.json").write_text('{"factory": "x:y"}')
        MasterAuthority(s.dir, f"dead-{pid}", pid=2 ** 22 + 17, alive=lambda p: False,
                        serialize=s.lock).acquire()

    class Idle:
        def evaluate(self, node):
            return RunOutcome(RunVerdict.NOT_STARTED, "")
    monkeypatch.setattr(R, "load_driver", lambda d: (lambda store: (Idle(), None)))
    real_acquire = MasterAuthority.acquire

    def acquire(self, **kw):
        if self.dir.name == "p1":
            raise LockBusy("held by a frozen process")
        return real_acquire(self, **kw)
    monkeypatch.setattr(MasterAuthority, "acquire", acquire)
    monkeypatch.setattr(R, "drive_governed", lambda store, a, f, **k: __import__(
        "hoh.orchestrator", fromlist=["Result"]).Result(
        __import__("hoh.orchestrator", fromlist=["HaltClass"]).HaltClass.CLOSED, "x"))
    by = {r["target"]: r["action"] for r in recover_projects(tmp_path)}
    assert by == {"p1": "program_lock_busy", "p2": "recovered"}


def test_a2_n2_resume_with_drive_but_no_driver_releases(tmp_path):
    s = _parked_project(tmp_path)
    resume_projects(tmp_path, now=NOW, drive=True)
    assert json.loads((s.dir / "lease.json").read_text())["released_at"]


# --------------------------------------------------------------------------- #
# Review A3 regressions
# --------------------------------------------------------------------------- #

def test_a3_f1_a_ready_node_in_flight_spends_no_epoch(tmp_path, monkeypatch):
    from hoh.epoch import EpochStore
    from hoh.orchestrator import RunVerdict

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.READY, run_id="r-a")]))
    (s.dir / "driver.json").write_text('{"factory": "x:y"}')
    m = MasterAuthority(s.dir, "m1", serialize=s.lock)
    m.acquire()
    m.release(reason="orderly end of drive: RUN_IN_FLIGHT")
    driven = _fake_driver(monkeypatch, RunVerdict.IN_FLIGHT)
    for _ in range(3):
        [r] = recover_projects(tmp_path)
        assert r["action"] == "waiting_on_run"
    assert driven == [] and len(EpochStore(s.dir).chain()) == 1


def test_a3_f3_another_release_reason_cannot_strand_a_running_node(tmp_path, monkeypatch):
    """The release reason is not the signal: a RUNNING node on a released
    lease whose run has a verdict is recovered whoever released it."""
    from hoh.orchestrator import RunVerdict

    s = _in_flight_project(tmp_path, lease_state="released-in-flight")
    lease = json.loads((s.dir / "lease.json").read_text())
    lease["released_reason"] = "resume pass ended without driving"
    (s.dir / "lease.json").write_text(json.dumps(lease))
    driven = _fake_driver(monkeypatch, RunVerdict.ACCEPTED)
    [r] = recover_projects(tmp_path)
    assert r["action"] == "recovered" and driven


def test_a3_an_orderly_halt_with_only_unclear_work_is_not_re_driven(tmp_path, monkeypatch):
    from hoh.orchestrator import RunVerdict

    _in_flight_project(tmp_path, lease_state="released-in-flight")
    driven = _fake_driver(monkeypatch, RunVerdict.UNDETERMINED)
    assert recover_projects(tmp_path) == [] and driven == []


def test_a3_f4_a_malformed_timer_row_is_a_store_error(tmp_path):
    from hoh.timers import TimerStoreError

    (tmp_path / "timers.json").write_text(json.dumps(
        {"schema_version": "x", "timers": [{"timer_id": "t", "due_at": "nonsense"}]}))
    import pytest
    with pytest.raises(TimerStoreError):
        TimerStore(tmp_path).read()


# --------------------------------------------------------------------------- #
# Review A4 regressions
# --------------------------------------------------------------------------- #

def test_a4_g1_a_drive_that_changed_nothing_is_not_repeated(tmp_path, monkeypatch):
    """Progress guard: a released project whose last drive left it unchanged
    is not re-driven, whatever halted that drive."""
    from hoh.epoch import EpochStore
    from hoh.orchestrator import RunVerdict

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]))
    (s.dir / "driver.json").write_text('{"factory": "x:y"}')
    m = MasterAuthority(s.dir, "m1", serialize=s.lock)
    m.acquire()
    m.release(reason=f"orderly end of drive: AMBIGUOUS @ws={s.read_state().write_seq}")
    driven = _fake_driver(monkeypatch, RunVerdict.REJECTED)      # "decisive" -- yet
    [held] = recover_projects(tmp_path)
    assert held["action"] == "left_alone" and driven == []      # said, not silent
    assert len(EpochStore(s.dir).chain()) == 1
    st = s.read_state()                                            # the state changes ...
    st.node("a").note = "a person looked"
    s.write_state(st)
    [r] = recover_projects(tmp_path)                               # ... and it may again
    assert r["action"] == "recovered"


def test_a4_g1_waiting_for_a_run_that_has_finished_is_not_blocked_by_the_guard(
        tmp_path, monkeypatch):
    from hoh.orchestrator import RunVerdict

    s = _in_flight_project(tmp_path, lease_state="released-in-flight")
    lease = json.loads((s.dir / "lease.json").read_text())
    lease["released_reason"] = (f"orderly end of drive: RUN_IN_FLIGHT "
                                f"@ws={s.read_state().write_seq}")
    (s.dir / "lease.json").write_text(json.dumps(lease))
    driven = _fake_driver(monkeypatch, RunVerdict.ACCEPTED)
    [r] = recover_projects(tmp_path)
    assert r["action"] == "recovered" and driven


def test_a4_g4_an_unreadable_root_timer_store_does_not_stop_the_tick(tmp_path, capsys):
    s = _parked_project(tmp_path, retry_after="2026-09-01T00:00:00Z")   # the CLI's clock is real
    (tmp_path / "timers.json").write_text("{not json")
    assert cli.main(["--root", str(tmp_path), "resume-quota"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(r.get("action") == "skipped" for r in out["results"])
    assert {r["target"]: r["action"] for r in out["projects"]
            if r.get("node") == "a"} == {"p": "resumed"}
    assert s.read_state().node("a").lifecycle is Lifecycle.READY


def test_a6_the_legacy_path_reports_a_killed_run(tmp_path):
    _run(tmp_path, "k", blocked_kind="usage_limit", retry_after="2026-09-01T00:00:00+00:00")
    [r] = resume_runs(tmp_path, now=NOW, killed=frozenset({"k"}))
    assert (r["verdict"], r["action"]) == ("KILLED", "refused")
    assert RunStore(tmp_path, "k").read_state().condition is Condition.BLOCKED


def test_a7_a_launch_that_finds_its_run_in_flight_leaves_the_node_running(tmp_path):
    """Review A7: IN_FLIGHT from launch must not become an unclassified block."""
    from hoh.orchestrator import GateRunner, HaltClass, ProjectController, RunOutcome, RunVerdict

    s = ProjectStore(tmp_path, "q")
    s.create(ProjectState(project_id="q", repo_path=str(tmp_path),
                          nodes=[TaskNode(id="a")]))

    class L:
        def action_class(self, node):
            return node.action_class

        def depends_on(self, a, b):
            return False

        def prepare(self, node):
            return None

        def accepted_baseline(self, node):
            return None

        def evaluate(self, node):
            return RunOutcome(RunVerdict.NOT_STARTED, "")

        def launch(self, node):
            return RunOutcome(RunVerdict.IN_FLIGHT, "Run a is already being executed")

    class G(GateRunner):
        def subject(self):
            return "x"

        def run(self, subject):
            return []
    r = ProjectController(s, L(), G()).run()
    assert r.halt is HaltClass.RUN_IN_FLIGHT
    assert s.read_state().node("a").lifecycle is Lifecycle.RUNNING
