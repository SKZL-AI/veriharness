"""P3-06 slice 2: the controller takes the workspace lease.

Slice 1 built `hoh.wslease` (a persisted lease, one holder per worktree) and the guard
`commit_candidate(lease=)`, and nothing started it: no caller passed a lease. These tests drive
the real `Controller` -- the loop `hoh run` runs -- on a fake dispatcher (no Herdr, no agent, no
host storage root, nothing outside `tmp_path`) and read the lease the way a second writer would:
from disk, through `hoh.wslease`.

Each node names the clause it pins. Every bound is a literal number, not the module's constant.
Nothing here removes anything: scratch is `tmp_path`, and the two linked worktrees are never
pruned.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import types
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from hoh import cli, controller as controller_module, dispatchers, integrity, stages
from hoh.contracts import Budgets, Condition, Role, RunState, Stage
from hoh.controller import Controller, DispatchError
from hoh.sentinel import stuck_run
from hoh.store import RunStore
from hoh.timers import TimerStatus, TimerStore
from hoh.workspace import WorkspaceError, commit_candidate as real_commit_candidate
from hoh.wslease import WorkspaceLease

SPEC = "# Spec\nadd(a, b) must add correctly.\n"
WAIT = 30       # seconds a test waits for another thread before it calls the run hung


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def linked_worktree(repo: Path, tmp_path: Path, name: str) -> Path:
    """A linked worktree on a branch of its own: the shape `commit_candidate` accepts."""
    target = tmp_path / name
    git(repo, "worktree", "add", "-q", "-b", name, str(target))
    return target


def home(tmp_path: Path, name: str) -> Path:
    """A directory of its own for another run's store (one `runs` root per controller)."""
    target = tmp_path / name
    target.mkdir()
    return target


@pytest.fixture
def wt(tmp_path: Path, repo: Path) -> Path:
    return linked_worktree(repo, tmp_path, "hoh-work")


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text(SPEC, encoding="utf-8")
    return p


class Fake:
    """Planner, developer and QA the test decides. `before[role]` runs at the start of that
    role's dispatch (a test's way to look at the world from inside the iteration); `raises[role]`
    is raised by that role's dispatch."""

    def __init__(self, repo: Path, *, develops: bool = True, develop: Callable[[Path], None] | None = None,
                 check: str = "test -f done.txt") -> None:
        self.repo = repo
        self.develops = develops
        self.develop = develop
        self.check = check
        self.calls: list[Role] = []
        self.before: dict[Role, Callable[[], None]] = {}
        self.raises: dict[Role, BaseException] = {}

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role)
        hook = self.before.get(role)
        if hook is not None:
            hook()
        if role in self.raises:
            raise self.raises[role]
        if role is Role.PLANNER:
            base = state.last_accepted_candidate or state.base_candidate
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
                "spec_digest": state.spec_digest, "objective": "add done.txt",
                "targets": ["app.py"], "preserve": [],
                "acceptance_checks": [{
                    "check_id": "K1", "description": "done.txt exists",
                    "command": self.check, "expect_exit": 0, "preserves": False}],
                "out_of_scope": [], "evidence_refs": [],
                "repair_only": False, "repair_reason": None})
        if role is Role.DEVELOPER:
            if self.develop is not None:
                self.develop(self.repo)
            elif self.develops:
                (self.repo / "done.txt").write_text("ok\n", encoding="utf-8")
            return "done"
        return json.dumps({
            "verdicts": [{
                "check_id": "K1", "outcome": "PASS",
                "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1",
                "reproduction": "pytest", "note": "green"}],
            "open_gaps": [], "summary": "all good"})

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def build(tmp_path: Path, worktree: Path, spec: Path, *, run_id: str = "r1", fake: Fake | None = None,
          **controller_kwargs):
    """A controller on `worktree` for `run_id`, with a run store of its own."""
    store = RunStore(tmp_path / "runs", run_id)
    state = Controller.new_state(run_id=run_id, repo_path=worktree, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)
    fake = fake or Fake(worktree)
    return Controller(store, fake, spec_path=spec, **controller_kwargs), state, store, fake


def peek(worktree: Path):
    """What a second writer sees: the record on disk, read through the lease module."""
    return WorkspaceLease(worktree, "peek-reader").read()


def log_events(worktree: Path) -> list[dict]:
    lease = WorkspaceLease(worktree, "peek-reader")
    path = lease.directory / "lease.log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def seconds(lease) -> int:
    """The length of the lease a record carries: `expires_at` minus `renewed_at` (a renewal moves
    both by the same amount, so it is the lease length whenever the record is read)."""
    stamp = lambda text: datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
    return int((stamp(lease.expires_at) - stamp(lease.renewed_at)).total_seconds())


def armed_timers(store: RunStore) -> list:
    return [t for t in TimerStore(store.dir.parent).read() if t.status is TimerStatus.ARMED]


# --------------------------------------------------------------------------- #
# C1, C2, C4: the lease is the iteration's, from before the first role to after it
# --------------------------------------------------------------------------- #


def test_the_lease_is_held_from_before_the_first_role_to_after_the_iteration(
        tmp_path, wt, spec, monkeypatch):
    """Catches a controller that never takes the lease (the pin: nothing but `wslease` builds a
    `WorkspaceLease`), one that takes it after the planner has started, one that does not pass
    it to `commit_candidate` (the guard of slice 1 stays unasked), and one that leaves it held
    when the iteration ends. At the start of each role the record on disk is this run's, live,
    generation 1; the commit receives the handle of that lease; afterwards the record is
    released with the reason `iteration ended` and the log says acquire, release."""
    ctrl, state, store, fake = build(tmp_path, wt, spec)
    seen: list[tuple[str, str, int, str | None]] = []

    def look(role: Role) -> Callable[[], None]:
        def at() -> None:
            lease = peek(wt)
            assert lease is not None, f"no lease on disk when the {role.value} starts"
            seen.append((role.value, lease.holder, lease.generation, lease.released_at))
        return at

    for role in (Role.PLANNER, Role.DEVELOPER, Role.QA):
        fake.before[role] = look(role)
    lengths: list[int] = []
    fake.before[Role.PLANNER] = lambda: (look(Role.PLANNER)(), lengths.append(seconds(peek(wt))))
    handed: list[object] = []

    def spy(*args, **kwargs):
        handed.append(kwargs.get("lease"))
        return real_commit_candidate(*args, **kwargs)

    monkeypatch.setattr(controller_module, "commit_candidate", spy)
    head = git(wt, "rev-parse", "HEAD")

    out = ctrl.run_iteration(state)

    assert out.accepted, out.reason
    assert seen == [("planner", "r1", 1, None), ("developer", "r1", 1, None), ("qa", "r1", 1, None)]
    assert lengths == [1800], "the default lease is 1800 s"
    assert len(handed) == 1 and isinstance(handed[0], WorkspaceLease)
    assert handed[0].holder == "r1" and handed[0].worktree == wt.resolve()
    assert git(wt, "rev-parse", "HEAD") != head, "the accepted candidate was committed"
    after = peek(wt)
    assert after is not None and after.holder == "r1" and after.generation == 1
    assert after.released_at is not None and after.released_reason == "iteration ended"
    assert [(e["event"], e["holder"], e["generation"]) for e in log_events(wt)] == [
        ("acquire", "r1", 1), ("release", "r1", 1)]
    assert not any("workspace lease" in line for line in state.history), state.history


# --------------------------------------------------------------------------- #
# C1, C3: one active writer per worktree lease, through the real controller path
# --------------------------------------------------------------------------- #


def test_a_second_controller_on_a_held_worktree_waits_naming_the_holder_and_runs_no_role(
        tmp_path, wt, spec):
    """The invariant `one active writer per worktree lease`: controller A is inside its developer
    dispatch (it holds the lease), controller B -- another run, another store, the same worktree --
    asks to iterate. B is refused as a WAIT: not accepted, nothing dispatched, nothing charged
    (stage NEW, iteration 0, no usage), BLOCKED and owned by a person (`blocked_kind` is `human`:
    the sentinel's own way out, so `hoh stuck`'s checker does not call it a control-plane defect)
    with the holder's name in the reason, no `retry_after`, no timer, not a quota kind; A's
    record is untouched. When A has finished, B -- still BLOCKED, it is refused by
    `begin_iteration` and takes no lease; once unblocked by its operator it takes the worktree
    (generation 2) and carries no stale owner, so it is not stuck either."""
    a_ctrl, a_state, a_store, a_fake = build(tmp_path, wt, spec, run_id="ra")
    b_ctrl, b_state, b_store, b_fake = build(tmp_path, wt, spec, run_id="rb")
    inside, release = threading.Event(), threading.Event()

    def hold_here() -> None:
        inside.set()
        assert release.wait(WAIT), "the test never released controller A"

    a_fake.before[Role.DEVELOPER] = hold_here
    result: dict[str, object] = {}

    def run_a() -> None:
        try:
            result["out"] = a_ctrl.run_iteration(a_state)
        except BaseException as exc:                  # noqa: BLE001 - re-raised by the test
            result["exc"] = exc

    thread = threading.Thread(target=run_a, name="writer-a")
    thread.start()
    try:
        assert inside.wait(WAIT), "controller A never reached its developer"
        held = peek(wt)
        assert held is not None and held.holder == "ra" and held.released_at is None

        out = b_ctrl.run_iteration(b_state)

        assert not out.accepted
        assert "'ra'" in out.reason and "waiting for the workspace" in out.reason
        assert b_fake.calls == []
        persisted = b_store.read_state()
        assert persisted.condition is Condition.BLOCKED
        assert persisted.blocked_kind == "human"
        assert persisted.retry_after is None
        assert "'ra'" in (persisted.blocked_reason or "")
        assert stuck_run(persisted, timers=[]) == [], "a held worktree is a wait with an owner"
        assert persisted.stage is Stage.NEW and persisted.iteration == 0
        assert persisted.usage.iterations == 0
        assert armed_timers(b_store) == []
        assert not (b_store.dir / "recovery").exists()
        assert not any(w in " ".join(persisted.history).lower() for w in ("usage_limit", "quota"))
        same = peek(wt)
        assert same == held, "the holder's record was not touched by the refused writer"
    finally:
        release.set()
        thread.join(WAIT)
    assert not thread.is_alive()
    assert "exc" not in result, result.get("exc")
    assert result["out"].accepted, result["out"].reason

    # BLOCKED, B is refused by `begin_iteration` as before, and takes no lease for it
    with pytest.raises(stages.TransitionError):
        b_ctrl.run_iteration(b_state)
    assert b_fake.calls == [] and peek(wt).holder == "ra" and peek(wt).generation == 1

    stages.unblock(b_state, "the holder has finished")
    again = b_ctrl.run_iteration(b_state)
    assert b_fake.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA]
    assert again.reason
    assert b_state.blocked_kind is None, "the lifted wait leaves no stale owner behind"
    assert stuck_run(b_store.read_state(), timers=[]) == []
    final = peek(wt)
    assert final is not None and final.holder == "rb" and final.generation == 2
    assert final.released_at is not None


# --------------------------------------------------------------------------- #
# C4: every way out of the iteration gives the worktree back
# --------------------------------------------------------------------------- #

USAGE_LIMIT = ("developer ended its turn without writing the answer file\n"
               "--- Visible in pane ---\n"
               "● Usage limit reached · continuing automatically at 3:20am · esc to cancel\n"
               "⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)\n"
               "-----------------------")


@pytest.mark.parametrize("way", ["accepted", "rejected", "exception", "interrupt", "quota_park",
                                 "interrupt_in_acquire"])
def test_every_way_out_of_an_iteration_gives_the_worktree_back(tmp_path, wt, spec, way, monkeypatch):
    """Catches a release on the happy path only: after the accepted iteration, the rejected one,
    an exception out of a role, a `KeyboardInterrupt` and a quota park (the provider wait the
    controller turns into a BLOCKED state and a timer) and an interrupt that arrives **between
    the lease being written and `acquire` returning** (the wrapper below raises right after the
    real `acquire`: the handle holds a lease it was never told about) the record is released and
    another writer takes the worktree at once (generation 2), whatever the controller returned
    or raised."""
    ctrl, state, store, fake = build(tmp_path, wt, spec)
    expected: type[BaseException] | None = None
    if way == "rejected":
        fake.develops = False
    elif way == "exception":
        fake.raises[Role.DEVELOPER] = RuntimeError("the developer's harness fell over")
        expected = RuntimeError
    elif way == "interrupt":
        fake.raises[Role.DEVELOPER] = KeyboardInterrupt()
        expected = KeyboardInterrupt
    elif way == "quota_park":
        fake.raises[Role.DEVELOPER] = DispatchError(USAGE_LIMIT)
    elif way == "interrupt_in_acquire":
        real_acquire = WorkspaceLease.acquire

        def acquire_then_interrupt(self, **kwargs):
            real_acquire(self, **kwargs)
            raise KeyboardInterrupt()

        monkeypatch.setattr(WorkspaceLease, "acquire", acquire_then_interrupt)
        expected = KeyboardInterrupt

    if expected is not None:
        with pytest.raises(expected):
            ctrl.run_iteration(state)
    else:
        out = ctrl.run_iteration(state)
        assert out.accepted is (way == "accepted"), out.reason
        if way == "quota_park":
            assert state.condition is Condition.BLOCKED and state.blocked_kind == "usage_limit"
    assert fake.calls == [] if way == "interrupt_in_acquire" else fake.calls[0] is Role.PLANNER

    lease = peek(wt)
    assert lease is not None and lease.holder == "r1" and lease.generation == 1
    assert lease.released_at is not None and lease.released_reason == "iteration ended"
    if way == "interrupt_in_acquire":
        monkeypatch.setattr(WorkspaceLease, "acquire", real_acquire)
    taken = WorkspaceLease(wt, "next-writer").acquire()
    assert taken.generation == 2 and taken.holder == "next-writer"


# --------------------------------------------------------------------------- #
# C5: the keepalive
# --------------------------------------------------------------------------- #


def _recording_events(periods: list[float]):
    """A stand-in for the `threading` module of `hoh.controller` whose events write down how
    long they were asked to wait (and wait at most 0.05 s, so a test is not as slow as a lease):
    the period the keepalive really uses, read from its behaviour."""
    class Recording(threading.Event):
        def wait(self, timeout=None):
            if timeout is not None:
                periods.append(timeout)
                timeout = min(timeout, 0.05)
            return super().wait(timeout)

    return types.SimpleNamespace(Event=Recording, Thread=threading.Thread)


def test_the_lease_is_renewed_while_a_role_runs_at_a_third_of_the_lease_and_not_after(
        tmp_path, repo, wt, spec, monkeypatch):
    """Catches a lease nobody renews (a role longer than the lease loses the worktree while it is
    still the writer), a keepalive that ignores its period, one that is never stopped, a period
    that is not a third of the lease, a lease length that is not forwarded, and a keepalive that
    ends at its first transient error. The period the thread waits is 600 s for the 1800 s lease
    and 10 s for a 30 s one (read from the waits themselves), never under a second; the record
    carries the length that was asked for; with a 0.05 s period the renewals are counted while
    the developer waits for the third one, the first of them having failed with an `OSError`;
    after the iteration no thread is left and the count no longer moves."""
    assert controller_module.keepalive_period(1800) == 600.0
    assert controller_module.keepalive_period(90) == 30.0
    assert controller_module.keepalive_period(3) == 1.0
    assert controller_module.keepalive_period(1) == 1.0

    for ttl, period in ((1800, 600.0), (30, 10.0)):
        periods: list[float] = []
        lengths: list[int] = []
        own = linked_worktree(repo, tmp_path, f"hoh-ttl-{ttl}")
        ctrl, state, _, fake = build(home(tmp_path, f"ttl{ttl}"), own, spec, run_id=f"p{ttl}",
                                     workspace_lease_ttl=ttl)
        fake.before[Role.DEVELOPER] = lambda w=own: lengths.append(seconds(peek(w)))
        with monkeypatch.context() as m:
            m.setattr(controller_module, "threading", _recording_events(periods))
            ctrl.run_iteration(state)
        assert lengths == [ttl], f"the lease record carries {lengths} s, not {ttl}"
        assert periods and set(periods) == {period}, (ttl, periods)

    renewals = threading.Semaphore(0)
    count = [0]
    real_renew = WorkspaceLease.renew

    def counting(self):
        count[0] += 1
        renewals.release()
        if count[0] == 1:
            raise OSError("a transient failure of the file system")
        return real_renew(self)

    monkeypatch.setattr(WorkspaceLease, "renew", counting)
    ctrl, state, _, fake = build(tmp_path, wt, spec, run_id="rk", workspace_keepalive=0.05)

    def wait_for_three() -> None:
        for _ in range(3):
            assert renewals.acquire(timeout=WAIT), "the lease was not renewed while the role ran"

    fake.before[Role.DEVELOPER] = wait_for_three
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert count[0] >= 3
    assert not [t for t in threading.enumerate() if t.name == "hoh-workspace-keepalive"]
    settled = count[0]
    threading.Event().wait(0.4)
    assert count[0] == settled, "the keepalive kept renewing after the iteration"
    lease = peek(wt)
    assert lease is not None and lease.holder == "rk" and lease.released_at is not None
    noted = [line for line in state.history if "the keepalive's renewal failed" in line]
    assert len(noted) == 1 and "1 time(s)" in noted[0] and "OSError" in noted[0], state.history


# --------------------------------------------------------------------------- #
# C6: a lease that is lost is never read as held
# --------------------------------------------------------------------------- #


def _usurp(worktree: Path) -> None:
    """Replace the record the way another holder's `acquire` would: another token, same worktree.
    (Done by hand because a live holder is never displaced by `acquire` itself.)"""
    from hoh import durable_io
    lease = WorkspaceLease(worktree, "peek-reader")
    current = lease.read()
    assert current is not None
    usurper = replace(current, holder="usurper", generation=current.generation + 1,
                      token_sha256="f" * 64)
    durable_io.durable_write(lease.lease_path, usurper.to_bytes(), mode=0o600)


def test_a_lease_lost_before_the_commit_refuses_it_and_one_lost_after_is_written_down(
        tmp_path, repo, wt, spec, monkeypatch):
    """Catches a controller that commits without the lease (the guard unasked), one that never
    looks again after the commit, and a release that clobbers the lease of whoever replaced it.
    (a) Another writer takes the worktree while QA runs: the verdict stands, the commit is refused
    (`NOT committed`, HEAD unmoved) and the usurper's record is left exactly as it is at the end
    of the iteration. (b) The lease goes right after the commit: the commit stands and a note
    says the worktree was not exclusively this run's."""
    ctrl, state, store, fake = build(tmp_path, wt, spec)
    fake.before[Role.QA] = lambda: _usurp(wt)
    head = git(wt, "rev-parse", "HEAD")
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert git(wt, "rev-parse", "HEAD") == head, "nothing was committed under a lost lease"
    history = "\n".join(state.history)
    assert "NOT committed" in history and "no longer holds" in history
    record = peek(wt)
    assert record is not None and record.holder == "usurper" and record.released_at is None
    assert record.token_sha256 == "f" * 64, "the iteration's release must not touch it"

    wt2 = linked_worktree(repo, tmp_path, "hoh-work-2")
    ctrl, state, store, fake = build(home(tmp_path, "second"), wt2, spec, run_id="r2")

    def commit_then_lose(*args, **kwargs):
        sha = real_commit_candidate(*args, **kwargs)
        _usurp(wt2)
        return sha

    monkeypatch.setattr(controller_module, "commit_candidate", commit_then_lose)
    head2 = git(wt2, "rev-parse", "HEAD")
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert git(wt2, "rev-parse", "HEAD") != head2, "the commit was made before the lease went"
    note = "\n".join(state.history)
    assert "workspace lease LOST around the commit" in note and "NOT committed" not in note
    record = peek(wt2)
    assert record is not None and record.holder == "usurper" and record.released_at is None


# --------------------------------------------------------------------------- #
# C7: odd inputs
# --------------------------------------------------------------------------- #


def test_odd_run_ids_and_odd_lease_files_never_mean_no_lease(tmp_path, repo, wt, spec):
    """Catches a controller that runs without a lease because the run id does not fit the holder
    grammar (`-x`, 200 characters) -- it holds one under `run-<32 hex>` -- one that reads a lease
    it cannot read as `no lease` (a record that is an empty object) and a held worktree whose
    holder's name looks like a provider limit (`rate-limit`), also when the worktree is spelled
    through a symbolic link: both are waits or refusals for a person (`blocked_kind` `human`),
    never a usage limit, never a retry timer, and the unreadable file is left byte for byte; a
    legacy capability-violation hold keeps its own kind; a plain directory that is no worktree
    gets no lease and one note that says so, however many iterations ask."""
    for index, run_id in enumerate(("-x", "a" * 200)):
        base = tmp_path / f"odd{index}"
        base.mkdir()
        w = tmp_path / f"hoh-odd-{index}"
        git(repo, "worktree", "add", "-q", "-b", f"hoh-odd-{index}", str(w))
        ctrl, state, store, fake = build(base, w, spec, run_id=run_id)
        out = ctrl.run_iteration(state)
        assert out.accepted, out.reason
        lease = peek(w)
        assert lease is not None and lease.released_at is not None
        assert lease.holder.startswith("run-") and len(lease.holder) == 36
        assert lease.holder != run_id

    # a holder whose name reads as a limit to the quota detector
    held = WorkspaceLease(wt, "rate-limit")
    held.acquire()
    link = tmp_path / "wt-link"
    link.symlink_to(wt)
    ctrl, state, store, fake = build(home(tmp_path, "s"), wt, spec, run_id="rs")
    state.repo_path = str(link)                    # the same worktree, spelled through a link
    out = ctrl.run_iteration(state)
    assert not out.accepted and fake.calls == [] and "'rate-limit'" in out.reason
    ctrl, state, store, fake = build(home(tmp_path, "q"), wt, spec, run_id="rq")
    out = ctrl.run_iteration(state)
    assert not out.accepted and fake.calls == []
    persisted = store.read_state()
    assert persisted.blocked_kind == "human" and persisted.condition is Condition.BLOCKED
    assert persisted.retry_after is None and armed_timers(store) == []
    assert "'rate-limit'" in (persisted.blocked_reason or "")
    assert stuck_run(persisted, timers=[]) == []

    # a run still carrying a capability violation (a legacy state: ACTIVE, the kind set) keeps it
    ctrl, state, store, fake = build(home(tmp_path, "w"), wt, spec, run_id="rw")
    state.blocked_kind = stages.CAPABILITY_VIOLATION
    state.blocked_reason = "capability violation: the arena was written"
    out = ctrl.run_iteration(state)
    assert not out.accepted and fake.calls == []
    assert store.read_state().blocked_kind == "capability_violation"
    held.release(reason="done")

    # a lease record that is not one: refused for a person, bytes untouched, nothing dispatched
    lease_file = WorkspaceLease(wt, "peek-reader").lease_path
    lease_file.write_bytes(b"{}\n")
    ctrl, state, store, fake = build(home(tmp_path, "u"), wt, spec, run_id="ru")
    out = ctrl.run_iteration(state)
    assert not out.accepted and fake.calls == []
    persisted = store.read_state()
    assert persisted.blocked_kind == "human" and persisted.condition is Condition.BLOCKED
    assert "refused (schema)" in (persisted.blocked_reason or "")
    assert stuck_run(persisted, timers=[]) == []
    assert lease_file.read_bytes() == b"{}\n"
    assert persisted.stage is Stage.NEW and persisted.usage.iterations == 0

    # a directory that is not the root of a worktree: no lease, said once, the snapshot refuses it
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
    ctrl, state, store, fake = build(home(tmp_path, "v"), plain, spec, run_id="rv")
    for _ in range(2):
        with pytest.raises(WorkspaceError):
            ctrl.run_iteration(state)
    noted = [line for line in state.history if "workspace lease: not taken" in line]
    assert len(noted) == 1, noted
    assert not (plain / ".git").exists()


# --------------------------------------------------------------------------- #
# The production path: `hoh run`
# --------------------------------------------------------------------------- #


def _hoh_run(tmp_path: Path, monkeypatch, fake: Fake, run_id: str):
    """`hoh run` itself -- the real parser, `_cmd_run`, `_run_in` and its loop -- on a fake
    dispatcher: no Herdr, no agent, no host storage root. Returns the exit code."""
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=tmp_path / "ledger"), tmp_path / "storage"))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))

    class NoAdmission(controller_module.Controller):
        def __init__(self, *a, **kw):
            kw["storage_admission"] = None
            super().__init__(*a, **kw)

    monkeypatch.setattr(controller_module, "Controller", NoAdmission)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: fake)
    return cli.main(["--root", str(tmp_path / "runs"), "run", run_id, "--no-herdr", "--iterations", "1"])


def test_hoh_run_takes_the_worktree_before_its_first_role_and_waits_for_a_held_one(
        tmp_path, wt, spec, monkeypatch, capsys):
    """The production path (cli.main -> cmd_run -> _run_in -> Controller.run_iteration): a run on a
    free worktree holds the lease from before its planner until it ends, and releases it; a run
    on a worktree another writer holds starts no role, prints the wait with the holder's name,
    and leaves its run BLOCKED and owned (`human`) -- the exit is not a rejection of the work,
    and `hoh stuck` (the never-stuck checker of the product) finds nothing."""
    ctrl, state, store, fake = build(tmp_path, wt, spec, run_id="rc1")
    seen: list[tuple[str, int, str | None]] = []

    def look() -> None:
        lease = peek(wt)
        assert lease is not None
        seen.append((lease.holder, lease.generation, lease.released_at))

    fake.before[Role.PLANNER] = look
    rc = _hoh_run(tmp_path, monkeypatch, fake, "rc1")
    capsys.readouterr()
    assert rc == 0
    assert seen == [("rc1", 1, None)]
    done = peek(wt)
    assert done is not None and done.released_at is not None and done.holder == "rc1"

    holder = WorkspaceLease(wt, "other-writer")
    holder.acquire()
    second = home(tmp_path, "second")
    ctrl, state2, store2, fake2 = build(second, wt, spec, run_id="rc2")
    rc = _hoh_run(second, monkeypatch, fake2, "rc2")
    shown = capsys.readouterr().out
    assert fake2.calls == []
    assert "other-writer" in shown and "waiting for the workspace" in shown
    persisted = store2.read_state()
    assert persisted.condition is Condition.BLOCKED and persisted.blocked_kind == "human"
    assert persisted.usage.iterations == 0 and persisted.retry_after is None
    assert cli.main(["--root", str(second / "runs"), "stuck"]) == 0
    assert "nothing is stuck" in capsys.readouterr().out
    assert peek(wt).holder == "other-writer" and peek(wt).released_at is None
    holder.release(reason="done")
    # The exit code of `hoh run` is 0 for every wait (a quota park, a storage park): the state
    # on disk, read above, carries it.
    assert rc == 0


def test_a_keepalive_stuck_in_a_renewal_does_not_hold_the_iteration_up(tmp_path, wt, spec, monkeypatch):
    """Catches a release that waits for the keepalive thread without end: the renewal never
    returns inside this test (a file system that hangs), and the iteration still ends --
    accepted, released -- well inside 25 s (the bound is 5 s). Once the renewal is let go the
    thread finds the lease released and ends: nothing is left running."""
    entered, gate = threading.Event(), threading.Event()
    real_renew = WorkspaceLease.renew

    def stuck(self):
        entered.set()
        gate.wait(2 * WAIT)
        return real_renew(self)

    monkeypatch.setattr(WorkspaceLease, "renew", stuck)
    ctrl, state, store, fake = build(tmp_path, wt, spec, workspace_keepalive=0.05)

    def wait_until_stuck() -> None:
        assert entered.wait(WAIT), "the keepalive never tried to renew"

    fake.before[Role.DEVELOPER] = wait_until_stuck
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["out"] = ctrl.run_iteration(state)
        except BaseException as exc:                  # noqa: BLE001 - asserted below
            result["exc"] = exc

    thread = threading.Thread(target=run, name="writer-stuck")
    thread.start()
    thread.join(25)
    ended = not thread.is_alive()
    gate.set()
    thread.join(WAIT)
    assert ended, "the iteration waited for a keepalive that was stuck in a renewal"
    assert "exc" not in result, result.get("exc")
    assert result["out"].accepted, result["out"].reason
    lease = peek(wt)
    assert lease is not None and lease.released_at is not None and lease.released_reason == "iteration ended"
    for leftover in [t for t in threading.enumerate() if t.name == "hoh-workspace-keepalive"]:
        leftover.join(WAIT)
        assert not leftover.is_alive(), "the keepalive thread did not end once it was let go"


# --------------------------------------------------------------------------- #
# The leak: one run's leftovers committed as another's
# --------------------------------------------------------------------------- #


LEFTOVER_CASES = ["plain", "untracked_hidden_by_repo_config", "untracked_hidden_by_user_config",
                  "untracked_hidden_by_worktree_config", "staged_only_and_untracked_hidden",
                  "ignored_by_global_excludes"]


def _configure_git(case: str, wt: Path, tmp_path: Path, monkeypatch) -> None:
    """`git status` is configurable in ways `git add -A` is not: `status.showUntrackedFiles=no`
    -- in the repository, in the worktree's own config, in the user's -- lists no untracked file,
    and a global excludes file hides what the commit would not take either (that one is the
    commit's rule too, so it is a control: nothing to leak)."""
    user = tmp_path / "user.gitconfig"
    if case == "untracked_hidden_by_repo_config" or case == "staged_only_and_untracked_hidden":
        git(wt, "config", "status.showUntrackedFiles", "no")
    elif case == "untracked_hidden_by_worktree_config":
        git(wt, "config", "extensions.worktreeConfig", "true")
        git(wt, "config", "--worktree", "status.showUntrackedFiles", "no")
    elif case == "untracked_hidden_by_user_config":
        user.write_text("[status]\n\tshowUntrackedFiles = no\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    elif case == "ignored_by_global_excludes":
        excludes = tmp_path / "global-excludes"
        excludes.write_text("done.txt\n", encoding="utf-8")
        user.write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))


@pytest.mark.parametrize("case", LEFTOVER_CASES)
def test_a_rejected_runs_leftovers_are_never_committed_as_another_runs(
        tmp_path, wt, spec, monkeypatch, case):
    """The failure the lease exists for, through two whole runs: run `ra` is rejected and leaves
    its edit in the tree (there is no rollback); run `rb` then asks for the same worktree. On the
    pin `rb` is accepted and `commit_candidate` takes the whole tree: `done.txt` is committed
    under `rb`'s name. Now `rb` is refused -- BLOCKED, owned by a person, named reason (`ra`,
    generation, 'not this run's'), nothing dispatched, nothing charged, **nothing written** to the
    lease record or its log, HEAD unmoved, not stuck -- while `ra` itself carries on over its own
    leftovers. Once a person has moved the file away and unblocked `rb`, it is accepted and its
    commit holds `app.py` and not `done.txt`.

    The question is the commit's, not `git status`'s: the same two runs under
    `status.showUntrackedFiles=no` (repository, worktree and user configuration -- on 6129869c
    `rb` is accepted and commits `done.txt` in each), a leftover that is only staged, and -- as a
    control -- a leftover a global excludes file hides from `git add -A` as well: nothing is
    there to take, `rb` is accepted and `done.txt` is not in its commit."""
    _configure_git(case, wt, tmp_path, monkeypatch)

    def leave_done(root: Path) -> None:
        (root / "done.txt").write_text("unverified\n", encoding="utf-8")
        if case == "staged_only_and_untracked_hidden":
            git(root, "add", "done.txt")

    a_fake = Fake(wt, develop=leave_done, check="false")
    a_ctrl, a_state, a_store, _ = build(tmp_path, wt, spec, run_id="ra", fake=a_fake)
    assert not a_ctrl.run_iteration(a_state).accepted
    assert (wt / "done.txt").is_file(), "no rollback: the rejected run's edit stays in the tree"

    # `rb` asks for the worktree that carries `ra`'s leftover. This comes first on purpose: on the
    # pin it is the *leak* that fails here (`rb` is accepted and commits `done.txt`), not a
    # missing lease record.
    lease = WorkspaceLease(wt, "peek-reader")
    log_path = lease.directory / "lease.log.jsonl"
    record = lease.lease_path.read_bytes() if lease.lease_path.exists() else None
    log = log_path.read_bytes() if log_path.exists() else None
    head = git(wt, "rev-parse", "HEAD")

    def change_app(root: Path) -> None:
        (root / "app.py").write_text("x = 2\n", encoding="utf-8")

    b_fake = Fake(wt, develop=change_app, check="grep -q 'x = 2' app.py")
    b_ctrl, b_state, b_store, _ = build(home(tmp_path, "second"), wt, spec, run_id="rb", fake=b_fake)
    out = b_ctrl.run_iteration(b_state)

    if case == "ignored_by_global_excludes":
        # nothing for the commit to take: not a leftover, so not refused -- and not committed
        assert out.accepted, out.reason
        assert git(wt, "show", "HEAD:app.py") == "x = 2"
        assert "done.txt" not in git(wt, "ls-tree", "-r", "--name-only", "HEAD").split()
        return

    assert not out.accepted and b_fake.calls == [], "rb was started on ra's leftovers"
    assert "'ra'" in out.reason and "not this run's" in out.reason
    assert git(wt, "rev-parse", "HEAD") == head
    assert record is not None and lease.lease_path.read_bytes() == record, \
        "a refused run must not write the lease record"
    assert log_path.read_bytes() == log
    first = peek(wt)
    assert first is not None and first.holder == "ra" and first.released_at is not None
    persisted = b_store.read_state()
    assert persisted.condition is Condition.BLOCKED and persisted.blocked_kind == "human"
    assert persisted.usage.iterations == 0 and persisted.stage is Stage.NEW
    assert persisted.retry_after is None and armed_timers(b_store) == []
    assert stuck_run(persisted, timers=[]) == []

    # `ra` itself is not refused by its own leftovers
    assert not a_ctrl.run_iteration(a_state).accepted
    again = peek(wt)
    assert again is not None and again.holder == "ra" and again.generation == first.generation + 1
    assert a_fake.calls.count(Role.PLANNER) == 2

    # a person moves the leftover away; the run is unblocked and is the only writer of its own work
    if case == "staged_only_and_untracked_hidden":
        git(wt, "restore", "--staged", "done.txt")
    os.replace(wt / "done.txt", tmp_path / "moved-away.txt")
    stages.unblock(b_state, "the leftovers were moved away")
    done = b_ctrl.run_iteration(b_state)
    assert done.accepted, done.reason
    assert git(wt, "show", "HEAD:app.py") == "x = 2"
    assert "done.txt" not in git(wt, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert stuck_run(b_store.read_state(), timers=[]) == []


# --------------------------------------------------------------------------- #
# The dirty question itself
# --------------------------------------------------------------------------- #


def test_the_dirty_question_is_the_commits_own_and_fails_closed(tmp_path, repo, monkeypatch):
    """`workspace.has_uncommitted_work` is what `commit_candidate` would take: clean is False; a
    modified, a deleted, an untracked and a staged file are True **also when `git status` is
    configured to show no untracked files**; a path `.gitignore` or `.git/info/exclude` hides is
    not a leftover (the commit would not take it either); and a git that cannot answer -- a
    repository without a commit, where `HEAD` names nothing -- is a `WorkspaceError`, never
    `False`."""
    from hoh.workspace import has_uncommitted_work

    assert has_uncommitted_work(repo) is False
    git(repo, "config", "status.showUntrackedFiles", "no")           # status would say "clean"
    assert git(repo, "status", "--porcelain") == ""
    (repo / "new.txt").write_text("u\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain") == "", "status is blind to it; the commit is not"
    assert has_uncommitted_work(repo) is True
    new = repo / "new.txt"
    os.replace(new, tmp_path / "moved.txt")
    assert has_uncommitted_work(repo) is False

    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore logs")
    (repo / "noise.log").write_text("n\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text("local.txt\n", encoding="utf-8")
    (repo / "local.txt").write_text("l\n", encoding="utf-8")
    assert has_uncommitted_work(repo) is False, "what the commit would not take is not a leftover"

    original = (repo / "app.py").read_text(encoding="utf-8")
    (repo / "app.py").write_text("x = 5\n", encoding="utf-8")
    assert has_uncommitted_work(repo) is True
    (repo / "app.py").write_text(original, encoding="utf-8")
    assert has_uncommitted_work(repo) is False
    os.replace(repo / "app.py", tmp_path / "app-moved.py")           # a tracked file that is gone
    assert has_uncommitted_work(repo) is True, "a deleted tracked file is taken by `add -A`"
    os.replace(tmp_path / "app-moved.py", repo / "app.py")
    assert has_uncommitted_work(repo) is False
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    assert has_uncommitted_work(repo) is True, "the index holds it: `git commit` takes it"

    fresh = tmp_path / "no-commit-yet"
    fresh.mkdir()
    git(fresh, "init", "-q")
    with pytest.raises(WorkspaceError):
        has_uncommitted_work(fresh)


# --------------------------------------------------------------------------- #
# The repair slice's further pins
# --------------------------------------------------------------------------- #


def test_a_lease_that_lapses_under_failing_renewals_says_why(tmp_path, wt, spec, monkeypatch):
    """Catches a keepalive failure that is written down only when the lease survived: every
    renewal fails (`OSError`), the lease (2 s) runs out while the developer works, the commit is
    refused (`NOT committed ... ran out`) -- and the history also says that the keepalive's
    renewals failed, and with what. On the candidate the second line was missing exactly here."""
    def failing(self):
        raise OSError("disk full")

    monkeypatch.setattr(WorkspaceLease, "renew", failing)
    ctrl, state, store, fake = build(tmp_path, wt, spec, workspace_lease_ttl=2, workspace_keepalive=0.05)
    fake.before[Role.DEVELOPER] = lambda: threading.Event().wait(3.2)
    head = git(wt, "rev-parse", "HEAD")
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert git(wt, "rev-parse", "HEAD") == head
    history = "\n".join(state.history)
    assert "NOT committed" in history and "ran out" in history
    noted = [line for line in state.history if "the keepalive's renewal failed" in line]
    assert len(noted) == 1 and "disk full" in noted[0], state.history
    assert re.search(r"failed [1-9][0-9]* time\(s\)", noted[0])


def test_a_foreign_index_lock_is_a_named_wait_not_a_crash_or_a_pass(tmp_path, wt, spec):
    """`git add -A --dry-run` takes `index.lock`, so any concurrent git command on the worktree
    makes the dirty question fail (exit 128). Another run asking then is refused -- BLOCKED, owned
    by a person, the reason names that git could not answer and why, nothing dispatched, nothing
    charged, the lease record byte for byte as it was, not stuck -- on a **clean** tree too (an
    unanswered question is not "clean"). Once the other git command is done (the lock is moved
    away) and the run is unblocked, it proceeds."""
    a_fake = Fake(wt)
    a_ctrl, a_state, _, _ = build(tmp_path, wt, spec, run_id="ra", fake=a_fake)
    assert a_ctrl.run_iteration(a_state).accepted
    assert git(wt, "status", "--porcelain") == "", "ra committed: the tree is clean"
    lease = WorkspaceLease(wt, "peek-reader")
    record, log = lease.lease_path.read_bytes(), (lease.directory / "lease.log.jsonl").read_bytes()
    head = git(wt, "rev-parse", "HEAD")

    lock = Path(git(wt, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("", encoding="utf-8")                  # a foreign git command holds the index

    def change_app(root: Path) -> None:
        (root / "app.py").write_text("x = 2\n", encoding="utf-8")

    b_fake = Fake(wt, develop=change_app, check="grep -q 'x = 2' app.py")
    b_ctrl, b_state, b_store, _ = build(home(tmp_path, "second"), wt, spec, run_id="rb", fake=b_fake)
    out = b_ctrl.run_iteration(b_state)
    assert not out.accepted and b_fake.calls == [], out.reason
    assert "could not be examined" in out.reason and "index.lock" in out.reason, out.reason
    assert git(wt, "rev-parse", "HEAD") == head
    assert lease.lease_path.read_bytes() == record and (lease.directory / "lease.log.jsonl").read_bytes() == log
    persisted = b_store.read_state()
    assert persisted.condition is Condition.BLOCKED and persisted.blocked_kind == "human"
    assert persisted.usage.iterations == 0 and persisted.stage is Stage.NEW
    assert persisted.retry_after is None and armed_timers(b_store) == []
    assert stuck_run(persisted, timers=[]) == []

    os.replace(lock, tmp_path / "lock-moved-away")
    stages.unblock(b_state, "the other git command is done")
    done = b_ctrl.run_iteration(b_state)
    assert done.accepted, done.reason
    assert b_fake.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA]


def test_a_run_whose_only_change_is_untracked_is_committed_whatever_status_shows(tmp_path, wt, spec):
    """`commit_candidate` used to open with `git status` too: under `status.showUntrackedFiles=no`
    a candidate whose only change is a new file was "clean" there, accepted **without a commit**,
    and the history said `committed as <the old head>` while the file stayed in the tree for the
    next run. It asks the commit's own question now: the file is committed under this run, HEAD
    moves, the note names the new commit."""
    git(wt, "config", "status.showUntrackedFiles", "no")
    ctrl, state, store, fake = build(tmp_path, wt, spec, run_id="ra")
    before = git(wt, "rev-parse", "HEAD")
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    after = git(wt, "rev-parse", "HEAD")
    assert after != before, "nothing was committed: the history would name the old head"
    assert "done.txt" in git(wt, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert any(f"committed as {after[:12]}" in line for line in state.history), state.history
    assert "NOT committed" not in "\n".join(state.history)


# --------------------------------------------------------------------------- #
# The gates come first, and a halted run takes nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gate", ["budget", "stop_request", "paused", "blocked", "failed", "cancelled"])
def test_the_worktree_is_taken_after_the_gates_and_a_halted_run_takes_none(tmp_path, wt, spec, gate):
    """Catches a take that comes before the gates that end an iteration without a role (a run
    that is out of budget, or has a stop request filed, would say `waiting for the workspace`
    instead of its own reason) and a halted run (PAUSED, BLOCKED, FAILED, CANCELLED) that takes a
    lease it cannot use. The worktree is held by another writer throughout, so a take would be
    visible as a wait; the halted run is refused by `begin_iteration` as before, and the other
    writer's record is byte for byte what it was."""
    holder = WorkspaceLease(wt, "other-writer")
    holder.acquire()
    before = holder.lease_path.read_bytes()
    ctrl, state, store, fake = build(tmp_path, wt, spec, run_id="rg")
    if gate == "budget":
        state.usage.iterations = state.budgets.max_iterations
        store.write_state(state)
    elif gate == "stop_request":
        store.stop_path.write_text(json.dumps({"kind": "pause", "reason": "the captain's pause"}),
                                   encoding="utf-8")
    else:
        state.condition = Condition[gate.upper()]
        state.stop_reason = f"set by the test: {gate}"
        store.write_state(state)

    if gate in ("budget", "stop_request"):
        out = ctrl.run_iteration(state)
        assert not out.accepted and "waiting for the workspace" not in out.reason, out.reason
        assert ("budget" in out.reason) if gate == "budget" else out.reason.startswith("pause:")
    else:
        with pytest.raises(stages.TransitionError):
            ctrl.run_iteration(state)
    assert fake.calls == []
    assert holder.lease_path.read_bytes() == before, "nothing was taken, nothing was written"
    assert peek(wt).holder == "other-writer" and peek(wt).released_at is None

    # the control: once the other writer has let go and the gate is lifted, the same run takes it
    holder.release(reason="done")
    state.condition, state.stop_reason, state.blocked_reason = Condition.ACTIVE, None, None
    state.usage.iterations = 0
    ctrl.run_iteration(state)
    taken = peek(wt)
    assert taken is not None and taken.holder == "rg" and taken.generation == 2
    assert taken.released_at is not None and fake.calls[0] is Role.PLANNER
