"""Who may drive a project now -- and a replaced master writes nothing.

P2-07 session succession, P2-08 master epoch, P2-09 leader lease, and the
plan's fault tests they carry: DS1 (controller kill), DS2 (reboot), DS5
(master rollover), DS6 (stale master cannot write), AC7 (an old session epoch
cannot issue an authoritative command).

Environment facts -- the clock, the boot id, whether a pid is alive -- are
injected, because those are exactly the facts the scenarios change. The
cross-process versions of the same scenarios, with real SIGKILL and real git,
are `tools/durability_e2e.py`; these tests pin the rules.
"""

from __future__ import annotations

import pytest

from hoh.epoch import EpochStore, Reason
from hoh.orchestrator import (
    GateRunner,
    HaltClass,
    ProjectController,
    RunLauncher,
    RunOutcome,
    RunVerdict,
    MergeResult,
)
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import StaleWrite
from hoh.succession import (
    LeaseError,
    LeaseHeld,
    MasterAuthority,
    StaleMaster,
    status,
)


class Clock:
    def __init__(self, now="2026-09-25T10:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        from datetime import datetime, timedelta, UTC
        t = datetime.strptime(self.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.now = (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """One machine: a boot id, a set of live pids, a clock."""

    def __init__(self):
        self.clock = Clock()
        self.boot_id = "boot-1"
        self.live: set[int] = set()

    def master(self, directory, session, pid, **kw):
        self.live.add(pid)
        return MasterAuthority(directory, session, clock=self.clock,
                               boot=lambda: self.boot_id, host="h", pid=pid,
                               alive=lambda p: p in self.live, **kw)


@pytest.fixture
def w():
    return World()


def test_first_acquire_begins_epoch_one_initial(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    e = a.acquire()
    assert (e.epoch, e.reason) == (1, Reason.INITIAL)
    assert a.problems() == []
    lease = a.read_lease()
    assert lease.leader_session_id == "sA" and lease.master_epoch == 1


def test_a_live_holder_is_never_displaced(tmp_path, w):
    w.master(tmp_path, "sA", 100).acquire()
    with pytest.raises(LeaseHeld):
        w.master(tmp_path, "sB", 200).acquire()


def test_ds1_a_killed_holder_is_replaced_at_once_with_recovery(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    w.live.discard(100)                       # SIGKILL
    b = w.master(tmp_path, "sB", 200)
    e = b.acquire()
    assert (e.epoch, e.reason) == (2, Reason.RECOVERY)
    assert b.problems() == []
    with pytest.raises(StaleMaster):
        a.check()


def test_ds2_a_holder_on_another_boot_is_lost(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    w.boot_id = "boot-2"                      # the machine restarted; pid 100 may
    w.live = {100}                            # even be reused by something else
    e = w.master(tmp_path, "sA", 300).acquire()
    assert (e.epoch, e.reason) == (2, Reason.RECOVERY)


def test_an_expired_lease_is_lost_even_if_the_pid_lives(tmp_path, w):
    a = w.master(tmp_path, "sA", 100, ttl_seconds=60)
    a.acquire()
    w.clock.advance(61)
    assert a.problems() and "expired" in a.problems()[0]
    e = w.master(tmp_path, "sB", 200).acquire()
    assert e.reason is Reason.RECOVERY


def test_renew_keeps_a_live_master_live(tmp_path, w):
    a = w.master(tmp_path, "sA", 100, ttl_seconds=60)
    a.acquire()
    for _ in range(5):
        w.clock.advance(40)
        a.renew()
    assert a.problems() == []
    with pytest.raises(LeaseHeld):
        w.master(tmp_path, "sB", 200).acquire()


def test_ds5_rollover_hands_over_to_the_named_successor_only(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    a.handoff("sB", reason="context rotation")
    with pytest.raises(StaleMaster):
        a.check()                              # the old master stops at once
    with pytest.raises(LeaseHeld):
        w.master(tmp_path, "sC", 300).acquire()  # not the named successor
    e = w.master(tmp_path, "sB", 200).acquire()
    assert (e.epoch, e.reason) == (2, Reason.SUCCESSION)


def test_ac7_an_old_epoch_is_refused_even_under_the_same_session_id(tmp_path, w):
    """A restarted context presenting the same id and its old epoch number."""
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    w.live.discard(100)
    again = w.master(tmp_path, "sA", 101)
    assert again.acquire().epoch == 2
    ghost = w.master(tmp_path, "sA", 102)
    ghost.epoch = 1                            # what the old context believes
    assert any("superseded" in p for p in ghost.problems())
    with pytest.raises(StaleMaster):
        ghost.check()


def test_a_handle_that_never_acquired_has_no_authority(tmp_path, w):
    with pytest.raises(StaleMaster):
        w.master(tmp_path, "sA", 100).check()


def test_a_handoff_to_oneself_or_without_reason_is_refused(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    with pytest.raises(LeaseError):
        a.handoff("sA", reason="x")
    with pytest.raises(LeaseError):
        a.handoff("sB", reason="  ")


def test_every_change_of_authority_is_logged_and_epochs_are_chained(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    a.handoff("sB", reason="rotate")
    b = w.master(tmp_path, "sB", 200)
    b.acquire()
    b.release(reason="closed")
    log = (tmp_path / "lease.log.jsonl").read_text().splitlines()
    assert [__import__("json").loads(z)["event"] for z in log] == [
        "acquire", "handoff", "acquire", "release"]
    assert EpochStore(tmp_path).fsck() == []
    rep = status(tmp_path, clock=w.clock)
    assert [e["reason"] for e in rep["epochs"]] == ["INITIAL", "SUCCESSION"]
    assert rep["holder_state"] == "RELEASED"


# --------------------------------------------------------------------------- #
# The store fence: an older epoch cannot write, however fresh its state
# --------------------------------------------------------------------------- #

def test_the_store_refuses_a_state_from_an_older_epoch(tmp_path):
    s = ProjectStore(tmp_path, "p")
    st = s.create(ProjectState(project_id="p", repo_path=str(tmp_path)))
    st.master_epoch = 2
    s.write_state(st)
    stale = s.read_state()                    # fresh write_seq ...
    stale.master_epoch = 1                    # ... but the old master's epoch
    with pytest.raises(StaleMaster):
        s.write_state(stale)
    assert isinstance(StaleMaster("x"), StaleWrite)


# --------------------------------------------------------------------------- #
# The controller: every authoritative act is checked
# --------------------------------------------------------------------------- #

class Launcher(RunLauncher):
    def __init__(self, on_launch=None, on_merge=None, verdict=RunVerdict.ACCEPTED):
        self.on_launch, self.on_merge, self.verdict = on_launch, on_merge, verdict
        self.launched, self.merged = [], []

    def action_class(self, node):
        return node.action_class

    def launch(self, node):
        if self.on_launch:
            self.on_launch(node)
        self.launched.append(node.id)
        return RunOutcome(self.verdict, "ok", candidate=f"c-{node.id}",
                          retry_after=getattr(self, "retry_after", None))

    def evaluate(self, node):
        return RunOutcome(RunVerdict.NOT_STARTED, "fixture")

    def merge(self, node, outcome):
        if self.on_merge:
            self.on_merge(node)
        self.merged.append(node.id)
        return MergeResult(landed=True, candidate=outcome.candidate or "")


class Gates(GateRunner):
    def subject(self):
        return "abc"

    def run(self, subject):
        return [GateResult(name="g", subject=subject, outcome=GateOutcome.GREEN)]


def _project(tmp_path, nodes=("a", "b")):
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                          nodes=[TaskNode(id=n) for n in nodes]))
    return s


def _authority(w, s, session, pid, **kw):
    return w.master(s.dir, session, pid, serialize=s.lock, **kw)


def test_a_governed_drive_closes_and_stamps_every_write(tmp_path, w):
    s = _project(tmp_path)
    a = _authority(w, s, "sA", 100)
    a.acquire()
    r = ProjectController(s, Launcher(), Gates(), authority=a).run()
    assert r.halt is HaltClass.CLOSED, r.reason
    assert s.read_state().master_epoch == 1


def test_ds6_a_master_superseded_mid_drive_merges_nothing_more(tmp_path, w):
    s = _project(tmp_path)
    a = _authority(w, s, "sA", 100, ttl_seconds=60)
    a.acquire()
    b_holder = {}

    def takeover(node):
        # While A's dispatch of node b is in flight, A's lease lapses and B
        # takes over (A is "frozen"): A must not merge b's candidate.
        if node.id == "b":
            w.clock.advance(61)
            b = _authority(w, s, "sB", 200)
            b.acquire()
            b_holder["b"] = b

    launcher = Launcher(on_launch=takeover)
    r = ProjectController(s, launcher, Gates(), authority=a).run()
    assert r.halt is HaltClass.SUPERSEDED, r.reason
    assert launcher.merged == ["a"]           # b was never merged by A
    after = s.read_state()
    assert after.node("b").lifecycle is Lifecycle.RUNNING
    # and the successor finishes it, merging b exactly once
    launcher2 = Launcher()
    launcher2.evaluate = lambda n: RunOutcome(RunVerdict.ACCEPTED, "landed", "c-b")
    r2 = ProjectController(s, launcher2, Gates(), authority=b_holder["b"]).run()
    assert r2.halt is HaltClass.CLOSED, r2.reason
    assert launcher2.merged == ["b"] and launcher2.launched == []


def test_a_rolled_over_master_is_superseded_at_its_next_write(tmp_path, w):
    s = _project(tmp_path, nodes=("a",))
    a = _authority(w, s, "sA", 100)
    a.acquire()
    launcher = Launcher(on_launch=lambda n: a.handoff("sB", reason="rotate"))
    r = ProjectController(s, launcher, Gates(), authority=a).run()
    assert r.halt is HaltClass.SUPERSEDED
    assert launcher.merged == []


def test_a_drive_without_authority_is_the_old_behaviour(tmp_path):
    s = _project(tmp_path)
    assert ProjectController(s, Launcher(), Gates()).run().halt is HaltClass.CLOSED
    assert s.read_state().master_epoch == 0


def test_an_ungoverned_writer_cannot_overwrite_a_governed_project(tmp_path, w):
    s = _project(tmp_path, nodes=("a",))
    a = _authority(w, s, "sA", 100)
    a.acquire()
    ProjectController(s, Launcher(), Gates(), authority=a).run()
    s2 = ProjectStore(tmp_path, "p")
    st = s2.read_state()
    st.master_epoch = 0                       # a legacy writer's state
    with pytest.raises(StaleMaster):
        s2.write_state(st)


def test_ds3_a_provider_wait_arms_a_durable_timer_under_the_generation(tmp_path, w):
    from hoh.orchestrator import node_timer_key
    from hoh.timers import TimerKind, TimerStore

    s = _project(tmp_path, nodes=("a",))
    a = _authority(w, s, "sA", 100)
    a.acquire()
    launcher = Launcher(verdict=RunVerdict.PROVIDER_UNAVAILABLE)
    launcher.retry_after = "2026-09-25T11:00:00Z"
    r = ProjectController(s, launcher, Gates(), authority=a).run()
    assert r.halt is HaltClass.BLOCKED_PROVIDER
    node = s.read_state().node("a")
    assert (node.lifecycle, node.blocked_kind, node.retry_after) == (
        Lifecycle.BLOCKED, "usage_limit", "2026-09-25T11:00:00Z")
    [t] = TimerStore(s.dir).read()
    assert t.kind is TimerKind.PROVIDER_RETRY
    assert (t.run_id, t.node_id, t.program_epoch) == (node_timer_key("a"), "a", 1)


def test_ds4_a_closed_project_disarms_its_timers(tmp_path, w):
    from hoh.timers import TimerKind, TimerStatus, TimerStore

    s = _project(tmp_path, nodes=("a",))
    TimerStore(s.dir).arm_new(kind=TimerKind.HEARTBEAT, due_at="2026-09-26T00:00:00Z",
                              program_id="p")
    r = ProjectController(s, Launcher(), Gates()).run()
    assert r.halt is HaltClass.CLOSED
    assert [t.status for t in TimerStore(s.dir).read()] == [TimerStatus.DISARMED]


# --------------------------------------------------------------------------- #
# Review A regressions
# --------------------------------------------------------------------------- #

class _KeepaliveProbe:
    """The keepalive under a fake clock, observed from the launcher.

    The keepalive renews from its own thread, `hoh-lease-keepalive`. Under the
    real clock these tests slept through a 9 s launch against a 6 s TTL and
    hoped the thread was scheduled in time; a machine-wide scheduling stall of
    about 3 s against about 3 s of slack made them fail under parallel load
    (spec lease-tests, Rev.2 item 4). Here the lease's time is the fake clock:
    the launcher advances it step by step and, after each step, waits until the
    keepalive thread has renewed *at the new time*. Expiry then depends only on
    fake time, never on how soon a thread runs; the waits are bounded and fail
    the test instead of hanging.

    `fail_once`, if given, is raised by the first renewal attempted *from the
    keepalive thread*, and only there: the controller's own renewal after a
    state write (main thread) is never failed, so it cannot absorb the error in
    the keepalive's place (Rev.2 item 1).
    """

    THREAD = "hoh-lease-keepalive"
    WAIT = 10.0                 # wall seconds per step; generous, and only a bound

    def __init__(self, a, clock, monkeypatch, *, fail_once=None, interval=0.01):
        import functools
        import threading

        self.clock, self.fail_once = clock, fail_once
        self.failed = fail_once is None
        self.renewals = []      # renewed_at of every successful keepalive renewal
        self.after_failure = 0  # successful keepalive renewals after the failure
        self.cond = threading.Condition()
        real = a.renew

        def renew():
            here = threading.current_thread().name == self.THREAD
            if here and not self.failed:
                with self.cond:
                    self.failed = True
                    self.cond.notify_all()
                raise self.fail_once
            lease = real()
            if here:
                with self.cond:
                    self.renewals.append(lease.renewed_at)
                    self.after_failure += 1
                    self.cond.notify_all()
            return lease
        monkeypatch.setattr(a, "renew", renew)
        monkeypatch.setattr(a, "keepalive", functools.partial(
            MasterAuthority.keepalive, a, interval=interval))

    def renewed_now(self):
        """True once the thread has failed (if it is to) and then renewed at
        the clock's current time."""
        return self.failed and bool(self.renewals) and self.renewals[-1] == self.clock.now

    def launch(self, steps=3, seconds=3):
        """A launch of `steps * seconds` fake seconds, renewed through."""
        for i in range(steps):
            self.clock.advance(seconds)
            with self.cond:
                ok = self.cond.wait_for(self.renewed_now, timeout=self.WAIT)
            assert ok, (f"no keepalive renewal at {self.clock.now} (step {i + 1}): "
                        f"failed={self.failed}, renewals={self.renewals}")


def _keepalive_drive(tmp_path, monkeypatch, **probe):
    clock = Clock()
    s = _project(tmp_path, nodes=("a",))
    a = MasterAuthority(s.dir, "sA", ttl_seconds=6, serialize=s.lock, clock=clock)
    a.acquire()
    p = _KeepaliveProbe(a, clock, monkeypatch, **probe)
    launcher = Launcher(on_launch=lambda n: p.launch())
    r = ProjectController(s, launcher, Gates(), authority=a).run()
    return p, launcher, r


def test_p6_a_launch_longer_than_the_ttl_keeps_its_lease_alive(tmp_path, monkeypatch):
    """TTL 6 s, a dispatch of 9 fake seconds -- one and a half leases -- through
    the real controller. Renewal only at writes would lose the lease
    mid-dispatch; the keepalive thread holds it. The launch advances the fake
    clock 3 s at a time and waits (bounded) for the thread to renew at each
    new time, so no assertion depends on thread scheduling (see
    `_KeepaliveProbe`)."""
    p, launcher, r = _keepalive_drive(tmp_path, monkeypatch)
    assert r.halt is HaltClass.CLOSED, r.reason
    assert launcher.merged == ["a"]
    assert len(set(p.renewals)) >= 3, p.renewals


def test_keepalive_default_period_is_a_third_of_the_ttl(tmp_path, monkeypatch):
    """Without an injected interval the thread waits ttl/3 between renewals.
    The wait is recorded instead of slept through: the stop event's first
    `wait` records its timeout and returns False at once (the period has
    elapsed), so the thread renews; later waits block until the block ends."""
    import threading
    import types

    import hoh.succession as succession

    periods, renewed = [], threading.Event()

    class RecordingEvent(threading.Event):
        def wait(self, timeout=None):
            periods.append(timeout)
            if len(periods) == 1:
                return False                # the first period has elapsed
            return super().wait()           # until stop.set(); never the period

    monkeypatch.setattr(succession, "threading", types.SimpleNamespace(
        Event=RecordingEvent, Thread=threading.Thread))
    a = MasterAuthority(tmp_path, "sA", ttl_seconds=30, clock=Clock())
    a.acquire()
    real = a.renew

    def renew():
        lease = real()
        if threading.current_thread().name == "hoh-lease-keepalive":
            renewed.set()
        return lease
    monkeypatch.setattr(a, "renew", renew)
    with a.keepalive():
        assert renewed.wait(timeout=10.0), (
            f"the keepalive thread never renewed; waits={periods}")
    assert periods and all(p == 10.0 for p in periods), periods


def test_p6_a_frozen_master_still_lapses(tmp_path, w):
    """The keepalive lives in the master's process: no renewal without it."""
    a = w.master(tmp_path, "sA", 100, ttl_seconds=60)
    a.acquire()
    w.clock.advance(61)
    assert w.master(tmp_path, "sB", 200).acquire().reason is Reason.RECOVERY


def test_p7_a_holder_in_another_pid_namespace_is_not_judged_by_pid(tmp_path, w):
    a = w.master(tmp_path, "sA", 403, pidns=lambda: "pid:[4026532999]")
    a.acquire()
    w.live.discard(403)          # the host does not know pid 403 ...
    with pytest.raises(LeaseHeld):
        w.master(tmp_path, "sB", 200).acquire()   # ... and still must not steal it


def test_p7_a_reused_pid_is_a_dead_holder(tmp_path, w):
    starts = {100: "111"}
    a = w.master(tmp_path, "sA", 100, start=lambda p: starts.get(p, ""))
    a.acquire()
    starts[100] = "999"          # pid 100 now belongs to a later process
    b = w.master(tmp_path, "sB", 200, start=lambda p: starts.get(p, ""))
    assert b.acquire().reason is Reason.RECOVERY


def test_p4_knowing_session_and_epoch_is_not_being_the_master(tmp_path, w):
    a = w.master(tmp_path, "sA", 100)
    a.acquire()
    impostor = w.master(tmp_path, "sA", 555)
    impostor.present(epoch=1, token="guessed")
    assert any("token" in p for p in impostor.problems())
    rightful = w.master(tmp_path, "sA", 556)
    rightful.present(epoch=1, token=a.token)
    assert rightful.problems() == []
    assert a.token not in (tmp_path / "lease.json").read_text()


def test_p8_an_ungoverned_controller_refuses_a_governed_project(tmp_path, w):
    s = _project(tmp_path, nodes=("a",))
    _authority(w, s, "sA", 100).acquire()
    launcher = Launcher()
    r = ProjectController(s, launcher, Gates()).run()
    assert r.halt is HaltClass.SUPERSEDED and launcher.launched == []


def test_p1_a_later_block_of_another_kind_clears_the_wait_and_its_timer(tmp_path, w):
    from hoh.orchestrator import MergeFailure
    from hoh.projectstore import unblock
    from hoh.resumeloop import resume_projects
    from hoh.timers import TimerStatus, TimerStore

    s = _project(tmp_path, nodes=("a",))
    a = _authority(w, s, "sA", 100)
    a.acquire()
    park = Launcher(verdict=RunVerdict.PROVIDER_UNAVAILABLE)
    park.retry_after = "2026-09-25T11:00:00Z"
    ProjectController(s, park, Gates(), authority=a).run()
    unblock(s, "a", "operator: quota came back early", authority=a)
    n = s.read_state().node("a")
    assert (n.blocked_kind, n.retry_after) == (None, None)
    assert [t.status for t in TimerStore(s.dir).read()] == [TimerStatus.DISARMED]

    class Conflict(Launcher):
        def merge(self, node, outcome):
            return MergeResult(landed=False, failure=MergeFailure.CONFLICT, detail="c")
    ProjectController(s, Conflict(), Gates(), authority=a).run()
    a.release(reason="orderly")
    recs = resume_projects(tmp_path, now="2026-09-25T11:05:00Z")
    assert all(r.get("action") != "resumed" for r in recs)
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_s5_a_second_park_withdraws_the_first_timer(tmp_path, w):
    from hoh.timers import TimerStatus, TimerStore

    s = _project(tmp_path, nodes=("a",))
    a = _authority(w, s, "sA", 100)
    a.acquire()
    for due in ("2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z"):
        park = Launcher(verdict=RunVerdict.PROVIDER_UNAVAILABLE)
        park.retry_after = due
        from hoh.projectstore import unblock
        if s.read_state().node("a").lifecycle is Lifecycle.BLOCKED:
            unblock(s, "a", "retry", authority=a)
        ProjectController(s, park, Gates(), authority=a).run()
    armed = [t for t in TimerStore(s.dir).read() if t.status is TimerStatus.ARMED]
    assert [t.due_at for t in armed] == ["2026-09-25T12:00:00Z"]


# --------------------------------------------------------------------------- #
# Review A2 regressions
# --------------------------------------------------------------------------- #

def test_a2_a_one_io_error_does_not_end_the_keepalive(tmp_path, monkeypatch):
    """The keepalive thread's first renewal fails with a full disk -- in that
    thread only, so the controller's write-path renewal cannot absorb it. The
    thread must go on renewing: after the error it renews at each of the three
    fake-clock steps of a 9 s launch (TTL 6 s). Deterministic; see
    `_KeepaliveProbe`."""
    p, launcher, r = _keepalive_drive(
        tmp_path, monkeypatch, fail_once=OSError(28, "No space left on device"))
    assert p.failed
    assert r.halt is HaltClass.CLOSED, r.reason
    assert p.after_failure >= 3 and len(set(p.renewals)) >= 3, p.renewals


def test_a2_c_a_superseded_master_does_not_touch_the_successors_timer(tmp_path, w):
    """Review A2 (c), its r8 shape: B takes over between two of A's writes and
    parks node c on a timer; A, holding a stale state in which c is READY, then
    writes -- and must be refused *before* withdrawing B's timer."""
    from hoh.orchestrator import node_timer_key
    from hoh.timers import TimerKind, TimerStatus, TimerStore

    s = _project(tmp_path, nodes=("a", "b", "c"))
    a = _authority(w, s, "sA", 100, ttl_seconds=60)
    a.acquire()

    class TakeoverAtPrepare(Launcher):
        def prepare(self, node):
            if node.id == "b":
                w.clock.advance(61)
                b = _authority(w, s, "sB", 200)
                b.acquire()
                due = "2026-09-25T12:00:00Z"
                st = s.read_state()
                c = st.node("c")
                c.lifecycle, c.blocked_kind, c.retry_after = (
                    Lifecycle.BLOCKED, "usage_limit", due)
                TimerStore(s.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=due,
                                          program_epoch=1, run_id=node_timer_key("c"),
                                          node_id="c")
                b.stamp(st)
                s.write_state(st)
            return None
    r = ProjectController(s, TakeoverAtPrepare(), Gates(), authority=a).run()
    assert r.halt is HaltClass.SUPERSEDED, r.reason
    [t] = TimerStore(s.dir).read()
    assert t.node_id == "c" and t.status is TimerStatus.ARMED, t


def test_a2_n5_an_unreadable_timer_store_does_not_crash_a_legacy_controller(tmp_path):
    s = _project(tmp_path, nodes=("a",))
    (s.dir / "timers.json").write_text("{not json")
    r = ProjectController(s, Launcher(), Gates()).run()
    assert r.halt is HaltClass.CLOSED, r.reason


def test_a3_s4_a_non_io_error_does_not_end_the_keepalive(tmp_path, monkeypatch):
    """As A2 (a), with an error that is not an I/O error: one such error in
    the keepalive thread must not end it either. Deterministic; see
    `_KeepaliveProbe`."""
    p, launcher, r = _keepalive_drive(
        tmp_path, monkeypatch, fail_once=AssertionError("lease vanished for a moment"))
    assert p.failed
    assert r.halt is HaltClass.CLOSED, r.reason
    assert p.after_failure >= 3 and len(set(p.renewals)) >= 3, p.renewals


def test_a3_f4_a_timer_that_cannot_be_armed_leaves_a_human_owner(tmp_path, w):
    s = _project(tmp_path, nodes=("a",))
    (s.dir / "timers.json").write_text("{not json")
    park = Launcher(verdict=RunVerdict.PROVIDER_UNAVAILABLE)
    park.retry_after = "2026-09-25T11:00:00Z"
    r = ProjectController(s, park, Gates()).run()
    assert r.halt is HaltClass.BLOCKED_PROVIDER
    n = s.read_state().node("a")
    assert n.lifecycle is Lifecycle.BLOCKED
    assert (n.blocked_kind, n.retry_after) == (None, None)   # never a timed park w/o timer
    assert "could not be armed" in n.note
