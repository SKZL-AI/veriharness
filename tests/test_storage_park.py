"""Durable STORAGE_PRESSURE park and resume (STORAGE-B3, P2-40).

What is simulated and what is real, said once for every test below: **low
space is simulated** by an injected observer (a fake ``statvfs`` that reports
the free bytes the test chooses -- never the machine's real free space), and
boots and pid namespaces by injected probes. **What is real** is the reaction:
the controller's dispatch path, the durable run state, the durable timers,
the ledger files, the resume loop, the CLI's refusal to start, the launcher's
verdicts, the orchestrator's settlement -- and, in the foreign-data test, the
fixture directory that stays untouched.

Every test name contains the selector it is run by, and every docstring names
the failure (the mutant) it catches. Each test carries a negative control that
changes only the condition under test.
"""
from __future__ import annotations

import ast
import builtins
import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import cli
from hoh import storage_host
from hoh import storage_park as spk
from hoh.capability import CapabilityViolation
from hoh.contracts import Budgets, Condition, Role, RunState
from hoh.controller import Controller
from hoh.resumeloop import resume_projects, resume_runs, resume_storage
from hoh.storage_pressure import (
    GiB,
    GuestObserver,
    NoHost,
    StatvfsHostObserver,
    StorageAdmission,
    StorageLedgerError,
    StoragePolicy,
)
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore, TimerStoreError

NS_HOST = "pid:[4026532229]"
NS_NEW_BOOT = "pid:[4026532999]"


class _Crash(BaseException):
    """A process death injected between two durable writes."""


# --------------------------------------------------------------------------- #
# Fakes: the injected observer (simulated free space) and the run fixture
# --------------------------------------------------------------------------- #


class FakeStatvfs:
    """``os.statvfs`` stand-in reporting the free bytes the test sets."""

    def __init__(self, free: int) -> None:
        self.free = free

    def __call__(self, path):
        return SimpleNamespace(f_bavail=self.free, f_frsize=1, f_favail=10_000_000,
                               f_fsid=0x6E57)


def admission(state_root: Path, free_gib: float, *, boot: str = "boot-A",
              ns: str | None = NS_HOST, claim: bool = True,
              policy: StoragePolicy | None = None) -> StorageAdmission:
    """A real ``StorageAdmission`` on ``state_root`` whose guest observer is
    simulated at ``free_gib`` free; boot and namespace injected. With
    ``claim`` the boot is claimed as a host entry point does."""
    adm = StorageAdmission.for_state_root(
        state_root, policy or StoragePolicy(),
        GuestObserver(state_root, NoHost(), statvfs=FakeStatvfs(int(free_gib * GiB))),
        pid_alive=lambda pid: True, proc_start_time=lambda pid: 111,
        boot_id=lambda: boot, pid_namespace=lambda: ns)
    if claim:
        adm.claim_boot()
    return adm


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    return p


MARKER = "done.txt"


class Dispatcher:
    """A fake role adapter: a bound plan, a developer that writes a marker,
    a QA that passes. Records which roles were started."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls: list[Role] = []

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role)
        if role is Role.PLANNER:
            base = state.last_accepted_candidate or state.base_candidate
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
                "spec_digest": state.spec_digest, "objective": "keep add() correct",
                "targets": ["app.py"], "preserve": [],
                "acceptance_checks": [{"check_id": "K1", "description": "marker",
                                       "command": f"test -f {MARKER}", "expect_exit": 0,
                                       "preserves": False}],
                "out_of_scope": [], "evidence_refs": [], "repair_only": False,
                "repair_reason": None})
        if role is Role.DEVELOPER:
            (self.repo / MARKER).write_text("ok\n", encoding="utf-8")
            return "done"
        rid = f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1"
        return json.dumps({"verdicts": [{"check_id": "K1", "outcome": "PASS",
                                         "receipt_id": rid, "reproduction": "test",
                                         "note": "green"}],
                           "open_gaps": [], "summary": "all good"})

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def build(tmp_path: Path, repo: Path, spec: Path, *, adm=None, park_policy=None):
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=5))
    store.write_state(state)
    d = Dispatcher(repo)
    ctl = Controller(store, d, spec_path=spec, storage_admission=adm,
                     park_policy=park_policy)
    return ctl, state, store, d


def run_timers(store: RunStore) -> list:
    return TimerStore(store.root).read()


def armed_storage(store_root: Path) -> list:
    return [t for t in TimerStore(store_root).read()
            if t.status is TimerStatus.ARMED and t.kind is TimerKind.STORAGE_RECHECK]


def park(tmp_path, repo, spec, *, free_gib: float = 10, **kw):
    """Park run r1 through the real controller with simulated low space."""
    state_root = tmp_path / "state"
    adm = admission(state_root, free_gib)
    ctl, state, store, d = build(tmp_path, repo, spec, adm=adm, **kw)
    out = ctl.run_iteration(state)
    return out, state, store, d, state_root


def after_due(store: RunStore, seconds: int = 1) -> datetime:
    [t] = armed_storage(store.root)
    due = datetime.strptime(t.due_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return due + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# K1 -- CRITICAL parks at a durable boundary, timer first (S8)
# --------------------------------------------------------------------------- #


def test_critical_parks_with_recheck_timer(tmp_path, repo, spec, monkeypatch):
    """Catches a refused dispatch that does not park, parks without a timer,
    starts a role anyway, charges the budget, or loses the refusal's numbers:
    with 10 GiB free (simulated, CRITICAL) the first role dispatch is refused,
    no role is started, the run is BLOCKED ``storage_pressure`` on disk with
    the effective free, reserve, estimate and state in ``blocked_reason``, a
    ``retry_after`` and exactly one armed STORAGE_RECHECK timer for the run.
    Negative control: the same run with 100 GiB free is admitted -- no park,
    no timer -- and each reservation is released with the measured actual
    (the difference of the run directory's and arena's apparent sizes)."""
    out, state, store, d, state_root = park(tmp_path, repo, spec, free_gib=10)
    assert not out.accepted
    assert d.calls == []
    disk = store.read_state()
    assert disk.condition is Condition.BLOCKED and disk.blocked_kind == "storage_pressure"
    for needle in ("state=CRITICAL", f"effective_free={10 * GiB} B",
                   f"reserve={30 * GiB} B", f"estimate={2 * GiB} B"):
        assert needle in disk.blocked_reason, needle
    assert disk.retry_after and disk.usage.dispatches == 0
    [timer] = armed_storage(store.root)
    assert (timer.run_id, timer.due_at) == ("r1", disk.retry_after)
    adm = admission(state_root, 10)
    assert list(adm.reservations_dir.glob("*.json")) == []

    # Negative control: plenty of space -> admitted, released with the actual.
    calls = []
    real = spk.measure_owned_bytes

    def measured(paths):
        paths = list(paths)
        value = real(paths)
        calls.append(([Path(p) for p in paths], value))
        return value
    monkeypatch.setattr(spk, "measure_owned_bytes", measured)
    root2 = tmp_path / "plenty"
    root2.mkdir()
    adm2 = admission(root2 / "state", 100)
    ctl2, state2, store2, d2 = build(root2, repo, spec, adm=adm2)
    out2 = ctl2.run_iteration(state2)
    assert d2.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA], out2.reason
    assert store2.read_state().blocked_kind != "storage_pressure"
    assert armed_storage(store2.root) == []
    assert all(p == [store2.dir, store2.arenas_dir] for p, _ in calls)
    expected = [max(0, calls[i + 1][1] - calls[i][1]) for i in range(0, len(calls), 2)]
    history = [json.loads(line) for line in
               adm2.history_path.read_text(encoding="utf-8").splitlines()]
    assert [h["actual"] for h in history] == expected and len(history) == 3
    assert list(adm2.reservations_dir.glob("*.json")) == []


def test_pressure_with_fitting_reservation_does_not_park(tmp_path, repo, spec):
    """Catches the reserve read as a global stop: at 45 GiB free (simulated,
    PRESSURE) a 2 GiB role run still leaves the 30 GiB reserve, so it is
    admitted and the run is not parked. Negative control: at 31 GiB free
    (still PRESSURE) the same role run would cross the reserve and parks."""
    adm = admission(tmp_path / "state", 45)
    assert adm.observe()[1].value == "PRESSURE"
    ctl, state, store, d = build(tmp_path, repo, spec, adm=adm)
    ctl.run_iteration(state)
    assert d.calls[:1] == [Role.PLANNER]
    assert store.read_state().blocked_kind != "storage_pressure"

    root2 = tmp_path / "tight"
    root2.mkdir()
    adm2 = admission(root2 / "state", 31)
    ctl2, state2, store2, d2 = build(root2, repo, spec, adm=adm2)
    ctl2.run_iteration(state2)
    assert d2.calls == []
    disk = store2.read_state()
    assert disk.blocked_kind == "storage_pressure" and "state=PRESSURE" in disk.blocked_reason


def test_timer_armed_before_parked_state(tmp_path, repo, spec, monkeypatch):
    """Catches the parked state persisted before its wake-up (mutant: arm
    after persist): a crash injected right after whichever of the two durable
    writes comes first leaves an armed STORAGE_RECHECK timer and an unparked
    run on disk -- never a parked run without a timer -- and the resume loop
    then refuses the orphan timer (the run is ACTIVE). Negative control: the
    same park without a crash leaves both, parked state and armed timer."""
    events: list[str] = []

    class CrashingTimers(TimerStore):
        def arm_new(self, **kw):
            super().arm_new(**kw)
            events.append("arm")
            raise _Crash("died after arming")

    state_root = tmp_path / "state"
    adm = admission(state_root, 10)
    ctl, state, store, d = build(tmp_path, repo, spec, adm=adm)
    original = store.write_state

    def write_state(st):
        result = original(st)
        if st.blocked_kind == "storage_pressure":
            events.append("park")
            raise _Crash("died after persisting the park")
        return result
    monkeypatch.setattr(store, "write_state", write_state)
    monkeypatch.setattr(spk, "TimerStore", CrashingTimers)
    with pytest.raises(_Crash):
        ctl.run_iteration(state)
    disk = RunStore(tmp_path / "runs", "r1").read_state()
    assert events == ["arm"]
    assert disk.blocked_kind != "storage_pressure" and disk.condition is Condition.ACTIVE
    [orphan] = armed_storage(store.root)
    monkeypatch.undo()
    recs = resume_storage(tmp_path / "runs", admission=admission(state_root, 100),
                          now=after_due(store), projects=False)
    assert recs == []          # an ACTIVE run is not a storage park: nothing resumed
    assert armed_storage(store.root) == [orphan]

    # Negative control: no crash -> parked and armed.
    root2 = tmp_path / "clean"
    root2.mkdir()
    out, _, store2, _, _ = park(root2, repo, spec)
    assert store2.read_state().blocked_kind == "storage_pressure"
    assert len(armed_storage(store2.root)) == 1


def test_arm_failure_stops_needs_you_without_park(tmp_path, repo, spec, monkeypatch):
    """Catches a park whose recheck timer could not be armed (mutant: park on
    arm failure): the run is not parked -- PAUSED with stop_reason exactly
    'storage pressure: the recheck timer could not be armed', a NEEDS_YOU
    note, no retry_after fallback, no armed timer, no role started -- and the
    launcher reads it STORAGE_UNAVAILABLE. Negative control: with a working
    timer store the same refusal parks."""
    from hoh.launcher import HohRunLauncher

    class BrokenTimers(TimerStore):
        def arm_new(self, **kw):
            raise TimerStoreError("timers.json is not writable")

    monkeypatch.setattr(spk, "TimerStore", BrokenTimers)
    adm = admission(tmp_path / "state", 10)
    ctl, state, store, d = build(tmp_path, repo, spec, adm=adm)
    out = ctl.run_iteration(state)
    disk = store.read_state()
    assert d.calls == [] and not out.accepted
    assert disk.condition is Condition.PAUSED
    assert disk.stop_reason == spk.ARM_FAILED
    assert disk.blocked_kind != "storage_pressure" and disk.retry_after is None
    assert any("NEEDS_YOU" in h for h in disk.history)
    assert armed_storage(store.root) == []
    verdict = HohRunLauncher(tmp_path, repo)._verdict(disk, None, None)
    assert verdict.verdict.value == "STORAGE_UNAVAILABLE"

    monkeypatch.undo()
    root2 = tmp_path / "working"
    root2.mkdir()
    _, _, store2, _, _ = park(root2, repo, spec)
    assert store2.read_state().blocked_kind == "storage_pressure"


# --------------------------------------------------------------------------- #
# K2 -- it resumes by itself once capacity returns, and re-arms while it does not
# --------------------------------------------------------------------------- #


def test_resumes_when_capacity_returns(tmp_path, repo, spec, monkeypatch):
    """Catches a resume without the admission check (mutant: resume without
    would_admit) and an unparked state persisted after the timer is consumed:
    at a due recheck with 40 GiB free (simulated, PRESSURE -- better than
    CRITICAL) but 10 GiB held by another open reservation, a role run would
    not be admitted, so the run stays parked; once 100 GiB are free it is
    resumed -- ACTIVE on disk, the timer FIRED -- and a crash injected in the
    consume step leaves an unparked run with its timer still armed (an orphan,
    never a stuck park). Negative controls: before the due time nothing is
    resumed; CRITICAL is never resumed."""
    import hoh.resumeloop as R

    out, state, store, d, state_root = park(tmp_path, repo, spec)
    runs = tmp_path / "runs"
    early = resume_storage(runs, admission=admission(state_root, 100),
                           now=after_due(store, -120), projects=False)
    assert [(r["verdict"], r["action"]) for r in early] == [("NOT_DUE", "refused")]
    assert store.read_state().blocked_kind == "storage_pressure"

    holder = admission(state_root, 100)
    held = holder.reserve("other-project", "clone", estimate_bytes=10 * GiB)
    assert held.admitted
    tight = admission(state_root, 40)
    assert tight.observe()[1].value == "PRESSURE"
    [rec] = resume_storage(runs, admission=tight, now=after_due(store), projects=False)
    assert rec["action"] == "rearmed", rec
    assert store.read_state().blocked_kind == "storage_pressure"
    holder.release(held, 0)

    [critical] = resume_storage(runs, admission=admission(state_root, 10),
                                now=after_due(store), projects=False)
    assert critical["action"] == "rearmed" and "still CRITICAL" in critical["why"]

    def crash(*a, **k):
        raise _Crash("died between unparking and consuming")
    monkeypatch.setattr(R, "consume", crash)
    [timer] = armed_storage(store.root)
    with pytest.raises(_Crash):
        resume_storage(runs, admission=admission(state_root, 100), now=after_due(store),
                       projects=False)
    disk = store.read_state()
    assert disk.condition is Condition.ACTIVE and disk.blocked_kind is None
    assert armed_storage(store.root) == [timer]
    monkeypatch.undo()

    # A clean resume on a fresh park.
    root2 = tmp_path / "clean"
    root2.mkdir()
    _, _, store2, _, state_root2 = park(root2, repo, spec)
    [timer2] = armed_storage(store2.root)
    [done] = resume_storage(root2 / "runs", admission=admission(state_root2, 100),
                            now=after_due(store2), projects=False)
    assert done["action"] == "resumed", done
    assert store2.read_state().condition is Condition.ACTIVE
    [fired] = [t for t in TimerStore(store2.root).read() if t.timer_id == timer2.timer_id]
    assert fired.status is TimerStatus.FIRED


def test_rearms_while_critical_and_disarms_old(tmp_path, repo, spec, monkeypatch):
    """Catches a re-arm that leaves the superseded timer armed (mutant: old
    timer left armed) or disarms it before the new one exists: a due recheck
    at 10 GiB (simulated, CRITICAL) arms a new STORAGE_RECHECK timer for the
    same run, then disarms the old one by id; the run stays parked with the
    new due time and the record says 'recheck: still CRITICAL, <free>'. A
    crash injected at the disarm leaves both armed -- a park never without
    its wake-up. Negative control: a resume pass before the due time changes
    no timer."""
    out, state, store, d, state_root = park(tmp_path, repo, spec)
    runs = tmp_path / "runs"
    [old] = armed_storage(store.root)
    resume_storage(runs, admission=admission(state_root, 10), now=after_due(store, -60),
                   projects=False)
    assert armed_storage(store.root) == [old]

    [rec] = resume_storage(runs, admission=admission(state_root, 10), now=after_due(store),
                           projects=False)
    assert rec["action"] == "rearmed" and rec["recheck"] == 1
    assert rec["why"] == f"recheck: still CRITICAL, {10 * GiB} B"
    [new] = armed_storage(store.root)
    assert new.timer_id != old.timer_id and new.run_id == "r1" and new.due_at > old.due_at
    [gone] = [t for t in TimerStore(store.root).read() if t.timer_id == old.timer_id]
    assert gone.status is TimerStatus.DISARMED and new.timer_id in gone.disarmed_reason
    disk = store.read_state()
    assert disk.blocked_kind == "storage_pressure" and disk.retry_after == new.due_at

    def crash(self, timer_id, reason):
        raise _Crash("died between arming and disarming")
    monkeypatch.setattr(TimerStore, "disarm", crash)
    with pytest.raises(_Crash):
        resume_storage(runs, admission=admission(state_root, 10), now=after_due(store),
                       projects=False)
    assert len(armed_storage(store.root)) == 2


def test_would_admit_writes_nothing(tmp_path):
    """Catches a dry run that writes (mutant: dry-run records history, or
    leaves a reservation): ``would_admit`` answers True at 100 GiB and False
    at 10 GiB (simulated) while the reservations directory keeps exactly its
    files -- a stale one is not even renamed -- and no history line is
    written. Negative control: ``reserve()`` on the same ledger writes a
    reservation file, and its release writes a history line."""
    root = tmp_path / "state"
    adm = admission(root, 100)
    owner = admission(root, 100)
    held = owner.reserve("p", "clone", estimate_bytes=5 * GiB)
    stale_judge = StorageAdmission.for_state_root(
        root, StoragePolicy(), GuestObserver(root, NoHost(), statvfs=FakeStatvfs(100 * GiB)),
        pid_alive=lambda pid: True, proc_start_time=lambda pid: 222,
        boot_id=lambda: "boot-A", pid_namespace=lambda: NS_HOST)
    before = sorted(p.name for p in adm.reservations_dir.iterdir())
    assert stale_judge.would_admit("role_run") is True
    assert adm.would_admit("role_run") is True
    assert admission(root, 10).would_admit("role_run") is False
    assert sorted(p.name for p in adm.reservations_dir.iterdir()) == before
    assert held.path.exists()
    assert not adm.history_path.exists()

    res = adm.reserve("p", "role_run")
    assert res.admitted and res.path.exists()
    adm.release(res, 1)
    assert len(adm.history_path.read_text(encoding="utf-8").splitlines()) == 1


# --------------------------------------------------------------------------- #
# K3 -- classes stay separate
# --------------------------------------------------------------------------- #


def _parked_run(root: Path, rid: str, kind: str, timer_kind: TimerKind,
                due: str = "2026-09-25T09:00:00Z") -> RunStore:
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p",
                               spec_path="/x/s.md", spec_digest="a" * 16,
                               policy_digest="b" * 16, profile_digest="c" * 16,
                               condition=Condition.BLOCKED, blocked_kind=kind,
                               blocked_reason=f"parked: {kind}", retry_after=due))
    reason = (spk.recheck_reason("p0", 0, "test park") if timer_kind is TimerKind.STORAGE_RECHECK
              else "quota")
    TimerStore(root).arm_new(kind=timer_kind, due_at=due, reason=reason, run_id=rid)
    return store


def test_class_not_owned_both_ways(tmp_path, capsys):
    """Catches a consumer that resumes the other's class: `hoh resume-quota`
    reports a due ``storage_pressure`` run CLASS_NOT_OWNED and leaves it
    parked with its timer armed, while it resumes the ``usage_limit`` run;
    `resume_storage` leaves the ``usage_limit`` run BLOCKED with its
    PROVIDER_RETRY timer armed, while it resumes the storage run (simulated
    100 GiB free). Negative control: each consumer does resume its own."""
    now = "2026-09-25T10:00:00Z"
    quota_root = tmp_path / "quota-pass"
    _parked_run(quota_root, "s", "storage_pressure", TimerKind.STORAGE_RECHECK)
    _parked_run(quota_root, "q", "usage_limit", TimerKind.PROVIDER_RETRY)
    recs = {r["target"]: r for r in resume_runs(quota_root, now=now)}
    assert recs["s"]["action"] == "class_not_owned"
    assert recs["q"]["action"] == "resumed"
    assert RunStore(quota_root, "s").read_state().blocked_kind == "storage_pressure"
    assert len(armed_storage(quota_root)) == 1
    assert cli.main(["--root", str(quota_root), "resume-quota"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["target"]: r["action"] for r in out["results"]}["s"] == "class_not_owned"

    storage_root = tmp_path / "storage-pass"
    _parked_run(storage_root, "s", "storage_pressure", TimerKind.STORAGE_RECHECK)
    _parked_run(storage_root, "q", "usage_limit", TimerKind.PROVIDER_RETRY)
    recs = {r["target"]: r for r in resume_storage(
        storage_root, admission=admission(tmp_path / "state", 100), now=now)}
    assert set(recs) == {"s"} and recs["s"]["action"] == "resumed"
    q = RunStore(storage_root, "q").read_state()
    assert q.condition is Condition.BLOCKED and q.blocked_kind == "usage_limit"
    [provider] = [t for t in TimerStore(storage_root).read()
                  if t.kind is TimerKind.PROVIDER_RETRY]
    assert provider.status is TimerStatus.ARMED


def _project_launcher(verdict_of):
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
            from hoh.orchestrator import RunOutcome, RunVerdict
            return RunOutcome(RunVerdict.NOT_STARTED, "")

        def launch(self, node):
            return verdict_of(node)
    return L()


class _NoGates:
    def subject(self):
        return "x"

    def run(self, subject):
        return []


def test_project_level_storage_wait_never_becomes_usage_limit(tmp_path):
    """Catches the storage class leaking into the quota path (mutant:
    ``storage_pressure`` in ``PROVIDER_BLOCKED``): a node whose run is parked
    on storage pressure -- verdict computed by the real launcher from the run
    state -- settles BLOCKED ``storage_pressure`` with a STORAGE_RECHECK timer
    (the node's superseded storage timer disarmed by id), never
    ``usage_limit`` and never PROVIDER_RETRY; the halt is a wait
    (STORAGE_WAIT, not in NEEDS_A_HUMAN), also when the project is driven
    again; `resume_projects` (quota) calls it CLASS_NOT_OWNED and
    `resume_storage` resumes it once a role run would be admitted. Negative
    control: a ``usage_limit`` run settles as the provider wait it is."""
    from hoh.launcher import PROVIDER_BLOCKED, Dispatch, HohRunLauncher
    from hoh.orchestrator import NEEDS_A_HUMAN, HaltClass, ProjectController, node_timer_key
    from hoh.project import Lifecycle, ProjectState, TaskNode
    from hoh.projectstore import ProjectStore

    assert "storage_pressure" not in PROVIDER_BLOCKED
    ln = HohRunLauncher(tmp_path, tmp_path)

    def run_state(kind, due):
        return RunState(run_id="a", repo_path="/x", project_name="p", spec_path="/x/s.md",
                        spec_digest="a" * 16, policy_digest="b" * 16,
                        profile_digest="c" * 16, condition=Condition.BLOCKED,
                        blocked_kind=kind, blocked_reason=f"{kind}: low", retry_after=due)

    s = ProjectStore(tmp_path / "runs", "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[TaskNode(id="a")]))
    stale = TimerStore(s.dir).arm_new(kind=TimerKind.STORAGE_RECHECK,
                                      due_at="2026-09-25T08:00:00Z",
                                      run_id=node_timer_key("a"), node_id="a")
    parked = run_state("storage_pressure", "2026-09-25T09:00:00Z")
    r = ProjectController(s, _project_launcher(
        lambda node: ln._verdict(parked, None, Dispatch(0, "", ""))), _NoGates()).run()
    assert r.halt is HaltClass.STORAGE_WAIT and r.halt not in NEEDS_A_HUMAN
    node = s.read_state().node("a")
    assert (node.lifecycle, node.blocked_kind, node.retry_after) == (
        Lifecycle.BLOCKED, "storage_pressure", "2026-09-25T09:00:00Z")
    timers = TimerStore(s.dir).read()
    assert [t.kind for t in timers if t.status is TimerStatus.ARMED] == [
        TimerKind.STORAGE_RECHECK]
    assert not any(t.kind is TimerKind.PROVIDER_RETRY for t in timers)
    [old] = [t for t in timers if t.timer_id == stale.timer_id]
    assert old.status is TimerStatus.DISARMED

    again = ProjectController(s, _project_launcher(lambda node: None), _NoGates()).run()
    assert again.halt is HaltClass.STORAGE_WAIT

    [quota] = [x for x in resume_projects(tmp_path / "runs", now="2026-09-25T10:00:00Z")
               if x.get("node") == "a"]
    assert quota["action"] == "class_not_owned"
    assert s.read_state().node("a").blocked_kind == "storage_pressure"
    recs = resume_storage(tmp_path / "runs", admission=admission(tmp_path / "state", 100),
                          now="2026-09-25T10:00:00Z")
    assert [x["action"] for x in recs if x.get("node") == "a"] == ["resumed"]
    assert s.read_state().node("a").lifecycle is Lifecycle.READY

    # Negative control: a usage_limit run is the provider wait it always was.
    s2 = ProjectStore(tmp_path / "runs2", "p")
    s2.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[TaskNode(id="a")]))
    quota_state = run_state("usage_limit", "2026-09-25T09:00:00+00:00")
    r2 = ProjectController(s2, _project_launcher(
        lambda node: ln._verdict(quota_state, None, Dispatch(0, "", ""))), _NoGates()).run()
    assert r2.halt is HaltClass.BLOCKED_PROVIDER
    assert s2.read_state().node("a").blocked_kind == "usage_limit"


# --------------------------------------------------------------------------- #
# K4 -- foreign low space only parks (S22)
# --------------------------------------------------------------------------- #


def _inventory(directory: Path) -> dict:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(directory.iterdir())}


def test_foreign_low_space_parks_and_touches_nothing(tmp_path, repo, spec, monkeypatch):
    """Catches a reaction to foreign low space that reads, lists, sizes or
    changes the foreign data (mutant: a listdir of the fixture): the free
    space is lowered (simulated: the injected observer reports 10 GiB, as if
    the foreign fixture directory had filled the disk); the run parks
    ``storage_pressure``; every fixture file keeps its name, size, mtime and
    content; and none of the recorded ``open``/``io.open``/``os.listdir``/
    ``os.scandir``/``os.walk`` calls during the dispatch path names the
    fixture. Negative control: an admitted dispatch sizes only the run
    directory and its arena (every recorded ``os.scandir`` is under them)."""
    foreign = tmp_path / "foreign-data"
    foreign.mkdir()
    for i in range(3):
        (foreign / f"blob-{i}.bin").write_bytes(bytes([i]) * (100 + i))
    before = _inventory(foreign)

    calls: list[tuple[str, str]] = []

    def recorder(name, fn):
        def wrapped(*a, **k):
            calls.append((name, os.fspath(a[0]) if a and isinstance(a[0], (str, os.PathLike))
                          else repr(a[:1])))
            return fn(*a, **k)
        return wrapped

    state_root = tmp_path / "state"
    adm = admission(state_root, 10)
    ctl, state, store, d = build(tmp_path, repo, spec, adm=adm)
    for target, name in ((builtins, "open"), (io, "open"), (os, "listdir"),
                         (os, "scandir"), (os, "walk")):
        monkeypatch.setattr(target, name, recorder(name, getattr(target, name)))
    ctl.run_iteration(state)
    monkeypatch.undo()
    assert store.read_state().blocked_kind == "storage_pressure"
    assert calls, "the recorder saw nothing: the patch did not take"
    assert not [c for c in calls if str(foreign) in c[1]]
    assert _inventory(foreign) == before

    # Negative control: an admitted dispatch sizes only the owned directories.
    calls.clear()
    root2 = tmp_path / "admitted"
    root2.mkdir()
    adm2 = admission(root2 / "state", 100)
    ctl2, state2, store2, d2 = build(root2, repo, spec, adm=adm2)
    monkeypatch.setattr(os, "scandir", recorder("scandir", os.scandir))
    sized: list[str] = []
    real = spk._walk_bytes

    def walk(directory):
        sized.append(os.fspath(directory))
        return real(directory)
    monkeypatch.setattr(spk, "_walk_bytes", walk)
    ctl2.run_iteration(state2)
    monkeypatch.undo()
    assert sized, "the admitted dispatch measured nothing"
    owned = (str(store2.dir), str(store2.arenas_dir))
    assert all(p.startswith(owned) for p in sized)
    assert not [c for c in calls if str(foreign) in c[1]]
    assert _inventory(foreign) == before


def test_sizing_follows_no_symlink(tmp_path, monkeypatch):
    """Catches sizing that follows links (mutant: ``follow_symlinks=True``):
    the run directory (100 B) and the arena (50 B) measure 150 B although the
    arena holds a link to an outside directory with a 10 000 B file and a
    link to an outside file; ``os.scandir`` is never called on the outside
    directory, and a given path that is itself a link counts 0. Negative
    control: the outside directory alone, given directly, measures 10 000 B."""
    run_dir = tmp_path / "runs" / "r1"
    arena = tmp_path / "runs" / "_arenas" / "r1"
    outside = tmp_path / "outside"
    for d in (run_dir, arena, outside):
        d.mkdir(parents=True)
    (run_dir / "state.json").write_bytes(b"x" * 100)
    (arena / "work.txt").write_bytes(b"y" * 50)
    (outside / "big.bin").write_bytes(b"z" * 10_000)
    os.symlink(outside, arena / "linked-dir")
    os.symlink(outside / "big.bin", arena / "linked-file")
    os.symlink(outside, tmp_path / "runs" / "link-root")

    scanned: list[str] = []
    real = os.scandir

    def scandir(path="."):
        scanned.append(os.fspath(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    assert spk.measure_owned_bytes([run_dir, arena]) == 150
    assert not [p for p in scanned if p.startswith(str(outside)) or "linked" in p]
    assert spk.measure_owned_bytes([tmp_path / "runs" / "link-root"]) == 0
    monkeypatch.undo()
    assert spk.measure_owned_bytes([outside]) == 10_000


# --------------------------------------------------------------------------- #
# K5 -- Needs-You, fail closed, real wiring
# --------------------------------------------------------------------------- #


def test_needs_you_after_repeated_rechecks(tmp_path, repo, spec):
    """Catches a run that is never escalated, escalated too early, or
    'helped' by cleaning: with CRITICAL simulated on every recheck, rechecks
    1-5 re-arm without NEEDS_YOU, the 6th (``needs_you_after`` default 6) is
    reported NEEDS_YOU with 'owned optimisation could not recover the
    reserve', and the run is still parked with an armed timer. The count
    comes from the durable timers: each pass uses a fresh policy object.
    Nothing is removed: every timer row ever written is still in the store.
    Negative control: the 5th recheck is not NEEDS_YOU."""
    out, state, store, d, state_root = park(tmp_path, repo, spec)
    runs = tmp_path / "runs"
    seen = []
    for n in range(1, 7):
        [rec] = resume_storage(runs, admission=admission(state_root, 10),
                               now=after_due(store), park_policy=spk.ParkPolicy(),
                               projects=False)
        assert rec["action"] == "rearmed" and rec["recheck"] == n
        seen.append(rec)
    assert all("needs_you" not in r for r in seen[:5])
    assert seen[4].get("verdict") != "NEEDS_YOU"
    assert seen[5]["verdict"] == "NEEDS_YOU"
    assert seen[5]["needs_you"] == "owned optimisation could not recover the reserve"
    disk = store.read_state()
    assert disk.condition is Condition.BLOCKED and disk.blocked_kind == "storage_pressure"
    assert len(armed_storage(store.root)) == 1
    assert len(TimerStore(store.root).read()) == 7


def test_admission_unavailable_fails_closed(tmp_path, repo, spec):
    """Catches an admission failure that is skipped (mutant: skip when
    unconfigured/unavailable): an admission whose ledger cannot be written
    (reserve raises) stops the run NEEDS_YOU ('storage admission
    unavailable'), no role is started and nothing is parked; a refusal that
    is not about capacity (the boot not claimed) stops the same way instead
    of parking for a recheck that cannot help. Negative control: a working,
    claimed admission with plenty of space starts the planner."""
    class Broken:
        def reserve(self, project_id, op_kind):
            raise OSError(30, "Read-only file system", "ledger")

    ctl, state, store, d = build(tmp_path, repo, spec, adm=Broken())
    out = ctl.run_iteration(state)
    disk = store.read_state()
    assert d.calls == [] and not out.accepted
    assert disk.condition is Condition.PAUSED
    assert disk.stop_reason.startswith("storage admission unavailable")
    assert disk.blocked_kind != "storage_pressure"
    assert armed_storage(store.root) == []

    root2 = tmp_path / "unclaimed"
    root2.mkdir()
    unclaimed = admission(root2 / "state", 100, claim=False)
    ctl2, state2, store2, d2 = build(root2, repo, spec, adm=unclaimed)
    ctl2.run_iteration(state2)
    disk2 = store2.read_state()
    assert d2.calls == [] and disk2.condition is Condition.PAUSED
    assert "boot not claimed" in disk2.stop_reason
    assert armed_storage(store2.root) == []

    root3 = tmp_path / "working"
    root3.mkdir()
    ctl3, state3, store3, d3 = build(root3, repo, spec, adm=admission(root3 / "state", 100))
    ctl3.run_iteration(state3)
    assert d3.calls[:1] == [Role.PLANNER]


class _Built(Exception):
    """Raised by the patched Controller once `hoh run` reached it."""

    def __init__(self, kwargs):
        super().__init__("controller built")
        self.kwargs = kwargs


def _cli_run(tmp_path, repo, spec, monkeypatch, *extra, wsl=False, profile=None):
    """Drive `cli.main(["run", ...])` up to the Controller (which is replaced
    by a recorder) with the host profile injected at the storage_host
    adapter's seam (`storage_host.detect_host_profile`): 'wsl2' when `wsl`,
    else 'native-linux', unless `profile` names one. Returns (exit code or
    None, the Controller kwargs or None)."""
    import hoh.controller as controller_mod
    import hoh.dispatchers as dispatchers

    runs = tmp_path / "runs"
    if not RunStore(runs, "r1").state_exists():
        RunStore(runs, "r1").write_state(Controller.new_state(
            run_id="r1", repo_path=repo, project_name="project", spec_path=spec))

    class Recorder:
        def __init__(self, *a, **kw):
            raise _Built(kw)

    chosen = profile or (storage_host.PROFILE_WSL2 if wsl else storage_host.PROFILE_NATIVE)
    monkeypatch.setattr(storage_host, "detect_host_profile", lambda *a, **k: chosen)
    monkeypatch.setattr(controller_mod, "Controller", Recorder)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: SimpleNamespace(
        endpoint_evidence=lambda r: "fake", close_own=lambda: []))
    try:
        rc = cli.main(["--root", str(runs), "run", "r1", "--no-herdr",
                       "--iterations", "0", *extra])
    except _Built as built:
        return None, built.kwargs
    return rc, None


def _init(path: Path) -> Path:
    assert cli.main(["storage", "init", "--root", str(path)]) == 0
    return path


def test_cli_run_uses_state_root_option(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches `--storage-root` being ignored (mutant): with `$HOH_STATE_ROOT`
    naming one initialised root and `--storage-root` another, the Controller
    receives an admission whose ledger lies under the `--storage-root` one,
    and the boot is claimed there. Negative control: without the option the
    environment's root is used."""
    env_root = _init(tmp_path / "env-root")
    opt_root = _init(tmp_path / "opt-root")
    monkeypatch.setenv("HOH_STATE_ROOT", str(env_root))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(opt_root))
    assert rc is None, capsys.readouterr().err
    adm = kw["storage_admission"]
    assert adm.ledger.parent == opt_root / "ledger"
    assert json.loads(adm.namespace_path.read_text(encoding="utf-8"))["claimed"] is True

    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch)
    assert kw["storage_admission"].ledger.parent == env_root / "ledger"


def test_cli_run_refuses_uninitialised_root(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches a root that is created instead of refused (mutant:
    auto-creating the root): `hoh run --storage-root` naming a path that does
    not exist, or an empty directory without OWNER.json, refuses to start
    (exit 2, 'storage state root is not initialised', naming `hoh storage
    init`), creates nothing there and never reaches the Controller. Negative
    control: after `hoh storage init --root` the same command starts."""
    missing = tmp_path / "nowhere" / "state"
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(missing))
    err = capsys.readouterr().err
    assert rc == 2 and kw is None
    assert "storage state root is not initialised" in err and "hoh storage init" in err
    assert not missing.exists() and not missing.parent.exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(empty))
    assert rc == 2 and kw is None and list(empty.iterdir()) == []

    _init(empty)
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(empty))
    assert rc is None and kw["storage_admission"] is not None


def test_cli_run_refuses_wsl_without_host_paths(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches the WSL check being dropped (mutant: WSL check removed): on a
    host the probe calls WSL, an initialised root whose policy configures no
    ``host_paths`` refuses to start ('WSL: configure host_paths'). The probe
    itself (the storage_host adapter's) reads /proc/version-shaped text
    ('Microsoft' -> WSL). Negative control: the same root on a native host
    starts."""
    root = _init(tmp_path / "state")
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc == 2 and kw is None
    assert "WSL: configure host_paths" in capsys.readouterr().err
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=False)
    assert rc is None and kw["storage_admission"] is not None

    wsl_text = "Linux version 6.6.87.2-microsoft-standard-WSL2 (root@x)\n"
    native = "Linux version 6.8.0-45-generic (buildd@lcy02)\n"
    monkeypatch.undo()
    assert storage_host.detect_host_profile(proc_version=wsl_text, env={}) == "wsl2"
    assert storage_host.detect_host_profile(proc_version=native, env={}) == "native-linux"


def test_cli_run_refuses_wsl_without_vhdx_volume(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches a non-empty ``host_paths`` accepted on WSL without the VHDX
    volume: without ``storage-host.json``, and with a ``wsl_host_volume``
    that is not among ``host_paths``, `hoh run` refuses ('WSL: the VHDX
    volume is not observed'). Negative control: naming the configured volume
    starts."""
    root = _init(tmp_path / "state")
    volume = tmp_path / "mnt-c"
    volume.mkdir()
    (root / "storage-policy.json").write_text(json.dumps({"host_paths": [str(volume)]}))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc == 2 and "WSL: the VHDX volume is not observed" in capsys.readouterr().err
    (root / "storage-host.json").write_text(json.dumps({"wsl_host_volume": "/mnt/d"}))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc == 2 and "WSL: the VHDX volume is not observed" in capsys.readouterr().err
    (root / "storage-host.json").write_text(json.dumps({"wsl_host_volume": str(volume)}))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc is None


def test_cli_run_starts_on_wsl_with_vhdx_volume(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches a correctly configured WSL host that still cannot start: with
    the probe saying WSL, ``host_paths`` naming the volume and
    ``storage-host.json`` naming the same volume, `hoh run` reaches the
    Controller with an admission that observes that volume as the host; with
    the mount probe injected (``ismount``/``statvfs``/``stat`` of a separate
    filesystem) the host reading is known and lowers effective free space.
    Negative control: the native host ignores a missing host file."""
    root = _init(tmp_path / "state")
    volume = tmp_path / "mnt-c"
    volume.mkdir()
    (root / "storage-policy.json").write_text(json.dumps({"host_paths": [str(volume)]}))
    (root / "storage-host.json").write_text(json.dumps({"wsl_host_volume": str(volume)}))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc is None, capsys.readouterr().err
    adm = kw["storage_admission"]
    host = adm.observer.host
    assert isinstance(host, StatvfsHostObserver) and host.paths == (volume,)

    # Inject the mount probe and a simulated guest: the host is observed.
    def statvfs(path):
        if Path(path) == volume:
            return SimpleNamespace(f_bavail=40 * GiB, f_frsize=1, f_favail=10_000_000,
                                   f_fsid=0x1000)
        return SimpleNamespace(f_bavail=100 * GiB, f_frsize=1, f_favail=10_000_000,
                               f_fsid=0x6E57)
    host._ismount = lambda p: True
    host._statvfs = statvfs
    host._stat = lambda p: SimpleNamespace(st_dev=60 if Path(p) == volume else 2096)
    adm.observer._statvfs = statvfs
    obs, pstate = adm.observe()
    assert not obs.host_unknown and obs.effective_free_bytes == 40 * GiB

    native = _init(tmp_path / "native")
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(native),
                      wsl=False)
    assert rc is None and isinstance(kw["storage_admission"].observer.host, NoHost)


def test_admission_host_profile_comes_from_storage_host(tmp_path, repo, spec, monkeypatch,
                                                         capsys):
    """Catches admission still deciding WSL or native through its own probe
    (mutant: `cli.is_wsl` still used for admission) instead of the merged
    storage_host adapter. The profile is injected at
    `storage_host.detect_host_profile` and any remaining `cli.is_wsl`
    answers the opposite, so on any host: profile 'native-linux' starts
    without ``host_paths``; profile 'wsl2' without ``storage-host.json``
    refuses with the WSL message. Nothing reads the machine's /proc/version."""
    root = _init(tmp_path / "state")
    monkeypatch.setattr(cli, "is_wsl", lambda *a, **k: True, raising=False)
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      profile=storage_host.PROFILE_NATIVE)
    assert rc is None and kw["storage_admission"] is not None, capsys.readouterr().err

    monkeypatch.setattr(cli, "is_wsl", lambda *a, **k: False, raising=False)
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      profile=storage_host.PROFILE_WSL2)
    assert rc == 2 and kw is None
    assert "WSL: configure host_paths" in capsys.readouterr().err

    volume = tmp_path / "vol"
    volume.mkdir()
    (root / "storage-policy.json").write_text(json.dumps({"host_paths": [str(volume)]}))
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      profile=storage_host.PROFILE_WSL2)
    assert rc == 2 and kw is None
    assert "WSL: the VHDX volume is not observed" in capsys.readouterr().err


def test_cli_run_refuses_unknown_host_profile(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches an 'unknown' host profile being treated as native (mutant:
    unknown treated as native): with the storage_host adapter reporting
    'unknown' -- or failing -- `hoh run` refuses to start (exit 2, 'storage
    admission unavailable: host profile unknown'), never builds a Controller
    and claims no boot. Negative control: the same root with 'native-linux'
    starts."""
    root = _init(tmp_path / "state")
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      profile=storage_host.PROFILE_UNKNOWN)
    err = capsys.readouterr().err
    assert rc == 2 and kw is None
    assert "storage admission unavailable: host profile unknown" in err
    assert not list(root.rglob("namespace.json"))     # no ledger made, no boot claimed

    def broken(*a, **k):
        raise storage_host.StorageHostError("probe failed")
    monkeypatch.setattr(storage_host, "detect_host_profile", broken)
    with pytest.raises(cli.StorageRefused, match="host profile unknown"):
        cli.storage_admission_for(str(root))
    assert not list(root.rglob("namespace.json"))     # no ledger made, no boot claimed

    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      profile=storage_host.PROFILE_NATIVE)
    assert rc is None and kw["storage_admission"] is not None


def test_wsl_host_volume_feeds_windows_mount(tmp_path, repo, spec, monkeypatch, capsys):
    """Catches the WSL adapter being built with `HostConfig()`'s default
    Windows mount '/mnt/c' although `storage-host.json` names the volume
    (mutant: HostConfig() default used despite the host file). On an
    injected 'wsl2' profile with `wsl_host_volume` naming a `tmp_path`
    directory (also the policy's only host path), the adapter `hoh run`
    builds through `storage_host.adapter_for_profile` has
    `config.windows_mount` equal to that volume, and the admission's host
    observer observes that volume only -- never '/mnt/c'. No mount, statvfs
    or /proc/version of the machine is read: the profile is injected and the
    run stops at the recorded Controller."""
    root = _init(tmp_path / "state")
    volume = tmp_path / "windows-volume"
    volume.mkdir()
    (root / "storage-policy.json").write_text(json.dumps({"host_paths": [str(volume)]}))
    (root / "storage-host.json").write_text(json.dumps({"wsl_host_volume": str(volume)}))
    built = []
    real = storage_host.adapter_for_profile

    def recording(profile, config=None, **probes):
        adapter = real(profile, config, **probes)
        built.append(adapter)
        return adapter
    monkeypatch.setattr(storage_host, "adapter_for_profile", recording)
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root),
                      wsl=True)
    assert rc is None, capsys.readouterr().err
    assert len(built) == 1 and isinstance(built[0], storage_host.WslHostAdapter)
    assert built[0].config.windows_mount == str(volume)
    host = kw["storage_admission"].observer.host
    assert isinstance(host, StatvfsHostObserver) and host.paths == (volume,)
    assert Path("/mnt/c") not in host.paths


def test_controller_without_admission_writes_nothing(tmp_path, repo, spec):
    """Catches a library Controller that silently performs admission, writes
    storage files, or stays silent about it: a Controller built without
    ``storage_admission`` runs a full accepted iteration (three dispatches),
    records exactly one note 'storage admission: not configured (library
    use)', and leaves no ledger, no reservation and no storage timer anywhere
    under the test's directory. Negative control: the same iteration with an
    admission writes the ledger."""
    ctl, state, store, d = build(tmp_path, repo, spec)
    out = ctl.run_iteration(state)
    assert out.accepted, out.reason
    notes = [h for h in store.read_state().history
             if "storage admission: not configured (library use)" in h]
    assert len(notes) == 1
    assert not [p for p in tmp_path.rglob("*") if p.name in ("ledger", "namespace.json")]
    assert armed_storage(store.root) == []

    root2 = tmp_path / "with-admission"
    root2.mkdir()
    adm = admission(root2 / "state", 100)
    ctl2, state2, store2, d2 = build(root2, repo, spec, adm=adm)
    ctl2.run_iteration(state2)
    assert adm.namespace_path.exists() and adm.history_path.exists()
    assert not [h for h in store2.read_state().history if "not configured" in h]


def _new_run(root: Path, rid: str = "r1") -> RunStore:
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p",
                               spec_path="/x/s.md", spec_digest="a" * 16,
                               policy_digest="b" * 16, profile_digest="c" * 16))
    return store


def test_launcher_passes_storage_root(tmp_path, monkeypatch):
    """Catches a launcher that spawns `hoh run` without the storage root
    (mutant: launcher without --storage-root) or does not claim the boot
    before spawning: with ``storage_root`` configured, the spawned argv ends
    in ``--storage-root <root>`` and the boot was claimed before the spawn;
    ``$HOH_STATE_ROOT`` is the fallback, resolved at construction (a later
    change of the variable does not move it). Negative control: with neither,
    no ``--storage-root`` is passed and the child resolves its own default."""
    from hoh.launcher import Dispatch, HohRunLauncher
    from hoh.project import TaskNode

    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    runs = tmp_path / "runs"
    _new_run(runs)
    root = _init(tmp_path / "state")
    events: list[str] = []

    def factory(path):
        adm = admission(path, 100, claim=False)
        original = adm.claim_boot

        def claim():
            events.append("claim")
            return original()
        adm.claim_boot = claim
        return adm

    argvs: list[list[str]] = []

    def run_cli(argv):
        events.append("spawn")
        argvs.append(argv)
        return Dispatch(0, "", "")
    ln = HohRunLauncher(runs, tmp_path, storage_root=root,
                        storage_admission_factory=factory)
    monkeypatch.setattr(ln, "_run_cli", run_cli)
    ln.launch(TaskNode(id="r1"))
    assert argvs[-1][-2:] == ["--storage-root", str(root)]
    assert events == ["claim", "spawn"]

    monkeypatch.setenv("HOH_STATE_ROOT", str(root))
    from_env = HohRunLauncher(runs, tmp_path, storage_admission_factory=factory)
    monkeypatch.setenv("HOH_STATE_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(from_env, "_run_cli", run_cli)
    from_env.launch(TaskNode(id="r1"))
    assert argvs[-1][-2:] == ["--storage-root", str(root)]

    monkeypatch.delenv("HOH_STATE_ROOT")
    bare = HohRunLauncher(runs, tmp_path)
    monkeypatch.setattr(bare, "_run_cli", run_cli)
    bare.launch(TaskNode(id="r1"))
    assert "--storage-root" not in argvs[-1]


def test_launcher_maps_refused_start_to_storage_unavailable(tmp_path, monkeypatch):
    """Catches a refused start read as UNDETERMINED or as a rejection: a
    `hoh run` that exits 2 with any of the storage refusals ('storage state
    root is not initialised', 'WSL: configure host_paths', 'WSL: the VHDX
    volume is not observed', 'storage admission unavailable') is
    STORAGE_UNAVAILABLE, and a project halts on it with a NEEDS_YOU class
    naming the reason; a run stopped with the arm-failure reason is the same,
    and a storage park is STORAGE_WAIT with its retry time. Negative control:
    an exit 2 with an unrelated message is not STORAGE_UNAVAILABLE."""
    from hoh.launcher import Dispatch, HohRunLauncher
    from hoh.orchestrator import NEEDS_A_HUMAN, HaltClass, ProjectController, RunVerdict
    from hoh.project import ProjectState, TaskNode
    from hoh.projectstore import ProjectStore

    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    runs = tmp_path / "runs"
    _new_run(runs)
    ln = HohRunLauncher(runs, tmp_path)
    for message in (cli.STORAGE_NOT_INITIALISED.format(root="/x"), cli.WSL_NO_HOST_PATHS,
                    cli.WSL_NO_VHDX_VOLUME + ": /x/storage-host.json",
                    "storage admission unavailable: OSError: read-only"):
        monkeypatch.setattr(ln, "_run_cli", lambda argv, m=message: Dispatch(
            2, "", f"NEEDS_YOU: {m}\n"))
        out = ln.launch(TaskNode(id="r1"))
        assert out.verdict is RunVerdict.STORAGE_UNAVAILABLE, (message, out)
        assert message.split(":")[0] in out.detail

    monkeypatch.setattr(ln, "_run_cli", lambda argv: Dispatch(2, "", "something else\n"))
    assert ln.launch(TaskNode(id="r1")).verdict is not RunVerdict.STORAGE_UNAVAILABLE

    stopped = RunStore(runs, "r1").read_state()
    stopped.condition = Condition.PAUSED
    stopped.stop_reason = spk.ARM_FAILED
    assert ln._verdict(stopped, None, Dispatch(0, "", "")).verdict \
        is RunVerdict.STORAGE_UNAVAILABLE
    parked = RunStore(runs, "r1").read_state()
    parked.condition = Condition.BLOCKED
    parked.blocked_kind = "storage_pressure"
    parked.retry_after = "2026-10-01T12:10:00Z"
    wait = ln._verdict(parked, None, Dispatch(0, "", ""))
    assert (wait.verdict, wait.retry_after) == (RunVerdict.STORAGE_WAIT, "2026-10-01T12:10:00Z")

    s = ProjectStore(tmp_path / "projects", "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[TaskNode(id="a")]))
    refused = SimpleNamespace(verdict=RunVerdict.STORAGE_UNAVAILABLE, detail=cli.WSL_NO_HOST_PATHS,
                              retry_after=None, candidate=None, candidate_tree=None)
    r = ProjectController(s, _project_launcher(lambda node: refused), _NoGates()).run()
    assert r.halt is HaltClass.STORAGE_UNAVAILABLE and r.halt in NEEDS_A_HUMAN
    assert cli.WSL_NO_HOST_PATHS in r.reason


# --------------------------------------------------------------------------- #
# K8 -- resume-storage claims the boot after a reboot
# --------------------------------------------------------------------------- #


def test_resume_storage_after_reboot_claims_then_resumes(tmp_path, repo, spec):
    """Catches a resume consumer that does not claim the boot (mutant:
    resume-storage does not claim): the run parked on boot A; after a reboot
    (boot B, a new pid namespace, simulated 100 GiB free) no controller has
    run, so the ledger's boot is unclaimed; `resume_storage` claims it and
    resumes the run -- it is not re-armed towards NEEDS_YOU. Negative
    control: without a claim, ``would_admit`` on boot B refuses."""
    out, state, store, d, state_root = park(tmp_path, repo, spec)
    rebooted = admission(state_root, 100, boot="boot-B", ns=NS_NEW_BOOT, claim=False)
    assert rebooted.would_admit("role_run") is False          # unclaimed boot
    [rec] = resume_storage(tmp_path / "runs", admission=rebooted, now=after_due(store),
                           projects=False)
    assert rec["action"] == "resumed", rec
    assert store.read_state().condition is Condition.ACTIVE
    record = json.loads(rebooted.namespace_path.read_text(encoding="utf-8"))
    assert (record["boot_id"], record["pid_ns"], record["claimed"]) == (
        "boot-B", NS_NEW_BOOT, True)


def test_resume_storage_refused_claim_resumes_nothing(tmp_path, repo, spec):
    """Catches a pass that resumes although it could not claim the boot: a
    caller whose namespace cannot be read is refused by ``claim_boot`` and the
    pass resumes nothing (NEEDS_YOU), leaving the run parked and its timer
    armed. Negative control: a readable host namespace resumes."""
    out, state, store, d, state_root = park(tmp_path, repo, spec)
    blind = admission(state_root, 100, ns=None, claim=False)
    with pytest.raises(StorageLedgerError):
        blind.claim_boot()
    [rec] = resume_storage(tmp_path / "runs", admission=blind, now=after_due(store))
    assert (rec["action"], rec["verdict"]) == ("refused", "NEEDS_YOU")
    assert store.read_state().blocked_kind == "storage_pressure"
    assert len(armed_storage(store.root)) == 1
    [ok] = resume_storage(tmp_path / "runs", admission=admission(state_root, 100),
                          now=after_due(store), projects=False)
    assert ok["action"] == "resumed"


# --------------------------------------------------------------------------- #
# Spec item 5 -- no removal call in the module
# --------------------------------------------------------------------------- #


def test_park_module_has_no_removal_call():
    """Catches a removal call in `hoh.storage_park` (house rule: nothing is
    removed): the AST holds no call of remove/unlink/rmdir/removedirs/rmtree/
    truncate and no shutil import. Negative control: the same scanner finds
    ``os.unlink`` in a probe."""
    source = Path(spk.__file__).read_text(encoding="utf-8")
    forbidden = {"remove", "unlink", "rmdir", "removedirs", "rmtree", "truncate", "ftruncate"}

    def hits(tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in forbidden:
                    out.append((node.lineno, name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any(n.split(".")[0] == "shutil" for n in names):
                    out.append((node.lineno, "shutil"))
        return out
    assert hits(ast.parse(source)) == []
    assert hits(ast.parse("import os\nos.unlink(p)\n")) == [(2, "unlink")]


# --------------------------------------------------------------------------- #
# K10 -- the resume-storage unit can write the state root (iteration 2, target 2)
# --------------------------------------------------------------------------- #

UNIT = Path(__file__).resolve().parent.parent / "ops" / "hoh-resume-storage.service"


def _unit_directives(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(key.strip(), []).append(value.strip())
    return out


def test_resume_storage_unit_write_paths_have_no_env_var():
    """Catches an unexpandable ReadWritePaths (mutant: the iteration-1 unit,
    ``ReadWritePaths=%h/hoh/runs ${HOH_STATE_ROOT}``): systemd expands
    environment variables in command lines only, so a ``$`` in
    ReadWritePaths= stays literal, the state root stays read-only under
    ProtectHome=read-only, and every `hoh resume-storage` tick fails closed in
    claim_boot()/would_admit() -- the parked run never resumes by itself. The
    unit has at least one ReadWritePaths= line, none contains ``$``, it names
    a state root through specifiers (``%S`` or ``%h``), and it keeps
    EnvironmentFile= and an explicit ``--storage-root`` in ExecStart.
    Negative control: the parser does see a ``$`` in the iteration-1 text."""
    text = UNIT.read_text(encoding="utf-8")
    d = _unit_directives(text)
    rw = d.get("ReadWritePaths", [])
    assert rw, "no ReadWritePaths= line: the state root would be read-only"
    assert not [v for v in rw if "$" in v], rw
    assert any("%S/veriharness" in v or "%h/hoh-state" in v for v in rw), rw
    assert d.get("EnvironmentFile"), "HOH_STATE_ROOT must come from an EnvironmentFile="
    assert any("resume-storage" in v and "--storage-root" in v for v in d.get("ExecStart", []))
    assert d.get("ProtectHome") == ["read-only"]

    old = "[Service]\nReadWritePaths=%h/hoh/runs ${HOH_STATE_ROOT}\n"
    assert [v for v in _unit_directives(old)["ReadWritePaths"] if "$" in v]


# --------------------------------------------------------------------------- #
# K11 -- sizing measures the arena the controller really uses (iteration 2, target 3)
# --------------------------------------------------------------------------- #


def test_sizing_measures_the_arena_the_controller_uses(tmp_path, repo, spec):
    """Catches a reservation released with an actual that never saw the arena
    (mutant: sizing ``store.arenas_dir`` only): `Controller._arena` puts
    arenas under ``scratch_dir`` when one is given, so a dispatch that writes
    N bytes into such an arena must release its reservation with an actual
    >= N. Free space is simulated (100 GiB, admitted); the bytes written and
    the release recorded in the ledger's history are real. Negative control:
    the same with ``scratch_dir=None`` still measures under
    ``store.arenas_dir``."""
    n = 123_457

    class ArenaWriter:
        def __init__(self) -> None:
            self.arena: Path | None = None

        def dispatch(self, role, prompt, *, state):
            (self.arena / "written-by-the-dispatch.bin").write_bytes(b"a" * n)
            return "done"

        def endpoint_evidence(self, role):
            return f"fake:{role.value}"

    for label, scratch in (("scratch", tmp_path / "scratch"), ("store", None)):
        base = tmp_path / label
        base.mkdir()
        adm = admission(base / "state", 100)
        store = RunStore(base / "runs", "r1")
        state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                     spec_path=spec, budgets=Budgets(max_iterations=5))
        state.arena_secret = "k11"
        store.write_state(state)
        writer = ArenaWriter()
        ctl = Controller(store, writer, spec_path=spec, scratch_dir=scratch,
                         storage_admission=adm)
        writer.arena = ctl._arena(state, suffix="k11")
        expected_base = scratch if scratch is not None else store.arenas_dir
        assert writer.arena.parent == expected_base
        ctl._qa_arena = writer.arena          # the QA role's own arena
        # qa-arena-write: QA's own arena is frozen for its dispatch, so this
        # write is now a capability violation. The reservation is released
        # on that way out too, with the bytes the dispatch wrote.
        with pytest.raises(CapabilityViolation, match="frozen tree was touched"):
            ctl._dispatch(Role.QA, "verify", state)
        [entry] = [json.loads(line) for line in
                   adm.history_path.read_text(encoding="utf-8").splitlines()]
        assert entry["op_kind"] == "role_run"
        assert entry["actual"] >= n, (label, entry)


# --------------------------------------------------------------------------- #
# K12 -- sizing never walks a shared scratch_dir (iteration 3, target 1)
# --------------------------------------------------------------------------- #


def test_sizing_never_walks_a_shared_scratch_dir(tmp_path, repo, spec, monkeypatch):
    """Catches sizing a shared scratch_dir (mutant: the iteration-2 sizing over
    ``Path(self.scratch_dir)``): a library caller may give several runs one
    ``scratch_dir``; this run's arena (created through the controller) sits
    next to a sibling directory standing for another run. During the dispatch
    ``os.scandir``/``os.listdir``/``os.walk``/``open`` are recorded: no call
    names the sibling or anything under it, the sibling's names, sizes, mtimes
    and contents are unchanged, and the released actual still includes the
    bytes the dispatch wrote into this run's arena. Free space is simulated
    (100 GiB, admitted); the files, the walk and the ledger's history are
    real. Negative control: without ``scratch_dir`` the arena under
    ``store.arenas_dir`` is sized as before."""
    n = 54_321

    class ArenaWriter:
        arena: Path

        def dispatch(self, role, prompt, *, state):
            (self.arena / "written-by-the-dispatch.bin").write_bytes(b"k" * n)
            return "done"

        def endpoint_evidence(self, role):
            return f"fake:{role.value}"

    scratch = tmp_path / "shared-scratch"
    sibling = scratch / "another-run-arena"
    sibling.mkdir(parents=True)
    for i in range(3):
        (sibling / f"other-{i}.bin").write_bytes(bytes([i + 1]) * (200 + i))
    before = _inventory(sibling)

    for label, scratch_dir in (("shared", scratch), ("store", None)):
        base = tmp_path / label
        base.mkdir()
        adm = admission(base / "state", 100)
        store = RunStore(base / "runs", "r1")
        state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                     spec_path=spec, budgets=Budgets(max_iterations=5))
        state.arena_secret = "k12"
        store.write_state(state)
        writer = ArenaWriter()
        ctl = Controller(store, writer, spec_path=spec, scratch_dir=scratch_dir,
                         storage_admission=adm)
        writer.arena = ctl._arena(state, suffix="k12")
        assert writer.arena.parent == (scratch_dir or store.arenas_dir)
        ctl._qa_arena = writer.arena

        calls: list[str] = []

        def recorder(name, fn):
            def wrapped(*a, **k):
                if a and isinstance(a[0], (str, os.PathLike)):
                    calls.append(os.fspath(a[0]))
                return fn(*a, **k)
            return wrapped
        for target, name in ((builtins, "open"), (io, "open"), (os, "listdir"),
                             (os, "scandir"), (os, "walk")):
            monkeypatch.setattr(target, name, recorder(name, getattr(target, name)))
        try:
            # qa-arena-write: the write into QA's frozen arena is a violation;
            # the sizing below still happened on that way out.
            with pytest.raises(CapabilityViolation, match="frozen tree was touched"):
                ctl._dispatch(Role.QA, "verify", state)
        finally:
            monkeypatch.undo()
        assert calls, "the recorder saw nothing: the patch did not take"
        assert not [c for c in calls if c == str(sibling) or c.startswith(str(sibling) + os.sep)]
        assert _inventory(sibling) == before
        [entry] = [json.loads(line) for line in
                   adm.history_path.read_text(encoding="utf-8").splitlines()]
        assert entry["actual"] >= n, (label, entry)
        if scratch_dir is None:
            assert any(c.startswith(str(store.arenas_dir)) for c in calls)


# --------------------------------------------------------------------------- #
# K13 -- a NEEDS_YOU persist failure is reported, never swallowed (target 2a)
# --------------------------------------------------------------------------- #


def _block_ledger(root: Path) -> None:
    """Make the ledger uncreatable: the registry's ``ledger`` directory is
    renamed (versioned, kept) and a plain file takes its name."""
    ledger = root / "ledger"
    os.rename(ledger, root / "ledger.v1.20261001T000000Z")
    ledger.write_text("not a directory\n", encoding="utf-8")


def test_admission_unavailable_persist_failure_is_reported(tmp_path, repo, spec,
                                                           monkeypatch, capsys):
    """Catches a swallowed persist failure (mutant: ``except Exception:
    pass``): when `hoh run` refuses with 'storage admission unavailable' (an
    initialised root whose ledger cannot be created) and persisting the
    NEEDS_YOU state also fails, stderr names both the refusal and the persist
    failure, the exit code is 2, and the launcher still maps that stderr to
    STORAGE_UNAVAILABLE. Negative control: with a working store the run is
    PAUSED with the NEEDS_YOU stop reason and stderr reports no persist
    failure."""
    from hoh.launcher import Dispatch, HohRunLauncher
    from hoh.project import TaskNode

    root = _init(tmp_path / "state")
    _block_ledger(root)

    def broken(state, store, reason):
        raise OSError(28, "No space left on device", "state.json")
    monkeypatch.setattr(spk, "stop_needs_you", broken)
    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root))
    err = capsys.readouterr().err
    assert rc == 2 and kw is None
    [line] = [l for l in err.splitlines() if l.startswith("NEEDS_YOU:")]
    assert "storage admission unavailable" in line
    assert "the NEEDS_YOU state could not be persisted" in line and "No space left" in line
    monkeypatch.undo()

    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    ln = HohRunLauncher(tmp_path / "runs", tmp_path)
    monkeypatch.setattr(ln, "_run_cli", lambda argv: Dispatch(2, "", err))
    out = ln.launch(TaskNode(id="r1"))
    assert out.verdict.value == "STORAGE_UNAVAILABLE", out
    monkeypatch.undo()

    rc, kw = _cli_run(tmp_path, repo, spec, monkeypatch, "--storage-root", str(root))
    err2 = capsys.readouterr().err
    assert rc == 2 and "could not be persisted" not in err2
    disk = RunStore(tmp_path / "runs", "r1").read_state()
    assert disk.condition is Condition.PAUSED
    assert disk.stop_reason.startswith("storage admission unavailable")


# --------------------------------------------------------------------------- #
# K14 -- a park is booked 'storage_parked', never 'failed' (target 2b)
# --------------------------------------------------------------------------- #


def _telemetry(store: RunStore) -> list[dict]:
    path = store.dir / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_park_is_recorded_as_storage_parked_in_telemetry(tmp_path, repo, spec):
    """Catches a park booked as a failed dispatch (mutant: ``outcome='failed'``
    for StorageParked): a CRITICAL observer (simulated 10 GiB) parks the run
    at the planner dispatch, and -- separately -- at the QA dispatch after
    the planner and developer were admitted (the developer's dispatch lowers
    the simulated free space). Each time the parked role's telemetry record
    says 'storage_parked' (for QA a record exists at all) and no record says
    'failed'. Negative control: a provider outage (DispatchError) at the
    planner is still booked 'failed'."""
    from hoh.controller import DispatchError

    out, state, store, d, _ = park(tmp_path, repo, spec)
    records = _telemetry(store)
    assert [(r["role"], r["outcome"]) for r in records] == [("planner", "storage_parked")]

    class LowerAtDeveloper(Dispatcher):
        statvfs: FakeStatvfs

        def dispatch(self, role, prompt, *, state):
            answer = super().dispatch(role, prompt, state=state)
            if role is Role.DEVELOPER:
                self.statvfs.free = 10 * GiB
            return answer

    root2 = tmp_path / "at-qa"
    root2.mkdir()
    adm = admission(root2 / "state", 100)
    store2 = RunStore(root2 / "runs", "r1")
    state2 = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                  spec_path=spec, budgets=Budgets(max_iterations=5))
    store2.write_state(state2)
    d2 = LowerAtDeveloper(repo)
    d2.statvfs = adm.observer._statvfs
    ctl2 = Controller(store2, d2, spec_path=spec, storage_admission=adm)
    ctl2.run_iteration(state2)
    assert d2.calls == [Role.PLANNER, Role.DEVELOPER]
    assert store2.read_state().blocked_kind == "storage_pressure"
    by_role = {r["role"]: r["outcome"] for r in _telemetry(store2)}
    assert by_role == {"planner": "ok", "developer": "ok", "qa": "storage_parked"}

    class Outage(Dispatcher):
        def dispatch(self, role, prompt, *, state):
            raise DispatchError("provider unavailable: 503")

    root3 = tmp_path / "outage"
    root3.mkdir()
    store3 = RunStore(root3 / "runs", "r1")
    state3 = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                  spec_path=spec, budgets=Budgets(max_iterations=5))
    store3.write_state(state3)
    ctl3 = Controller(store3, Outage(repo), spec_path=spec,
                      storage_admission=admission(root3 / "state", 100))
    with pytest.raises(DispatchError):
        ctl3.run_iteration(state3)
    assert [(r["role"], r["outcome"]) for r in _telemetry(store3)] == [("planner", "failed")]
