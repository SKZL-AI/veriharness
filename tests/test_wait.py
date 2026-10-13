"""`hoh wait RUN` (I12, slice 1 of the row P5-43): block until a run is at rest, from the state
the product already writes, read-only.

Every test builds real runs under `tmp_path` through the product's own `RunStore`, holds a real
process lock in the test process for a "live" run, and injects the clock. Nothing sleeps, and
nothing starts Herdr, an agent or a process: a `herdr` stub first on `PATH` logs its argv and
fails, `subprocess.run` and `subprocess.Popen` fail the test, and the stub's log is asserted
empty at the end (autouse fixture). Every fake `sleep` is guarded (a positive duration and a call
budget), so a wait that loops without sleeping fails a test instead of spinning it.

The exit codes are written out as numbers on purpose: they are the contract of the command, and a
table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import argparse
import ast
import errno
import json
import math
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hoh import cli, runwait
from hoh.contracts import Candidate, Condition, RunState, Stage, recorded_original
from hoh.failures import AUTOMATIC_KINDS
from hoh.quota import MAX_RESUME_ATTEMPTS
from hoh.resumeloop import QUOTA_CLASSES, STORAGE_CLASSES
from hoh.resumption import WAITING_KINDS
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore, in_seconds

KEYS = ["schema", "run_id", "until", "outcome", "matched", "exit_code", "phase", "stage",
        "condition", "iteration", "process", "process_since", "process_ended", "blocked_kind",
        "blocked_reason", "retry_after", "resume_attempts", "binding", "caveat", "stop_reason",
        "accepted_candidate", "waited_s", "polls"]

PAST = "2020-01-01T00:00:00Z"
CAPABILITY_REASON = "capability violation: write outside the arena"


# --------------------------------------------------------------------------- #
# Fixtures: no process, no Herdr, no sleep
# --------------------------------------------------------------------------- #


class Clock:
    """An injected clock. `sleep` is guarded: a positive duration, at most `budget` calls."""

    def __init__(self, budget: int = 8) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.budget = budget

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, f"a sleep of {seconds!r} seconds: a wait that does not wait"
        assert len(self.sleeps) < self.budget, f"more than {self.budget} sleeps: a runaway loop"
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _use_clock(monkeypatch, clock: Clock) -> Clock:
    monkeypatch.setattr(runwait, "time", types.SimpleNamespace(
        sleep=clock.sleep, monotonic=clock.monotonic))
    return clock


@pytest.fixture(autouse=True)
def _nothing_runs(tmp_path, monkeypatch):
    log = tmp_path / "herdr-stub" / "calls.log"
    stub = tmp_path / "herdr-stub" / "herdr"
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # By default nothing may sleep; a test that waits installs its own guarded clock.
    _use_clock(monkeypatch, Clock(budget=0))
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", "a test called herdr"


# --------------------------------------------------------------------------- #
# Builders: real runs
# --------------------------------------------------------------------------- #


def _candidate(root: Path | None = None) -> Candidate:
    return Candidate(candidate_id="cand-1", repo_path=str((root or Path("/arena")) / "repo"),
                     commit="c0ffee", tree_clean=True, tree_digest="t")


def _make(root: Path, run_id: str, *, stage=Stage.DEVELOPING, condition=Condition.ACTIVE,
          kind: str | None = None, reason: str | None = None, retry: str | None = None,
          attempts: int = 0, stop: str | None = None, candidate: bool = False,
          iteration: int = 1, timer: bool = True) -> RunStore:
    state = RunState(run_id=run_id, repo_path=str(root / "repo"), project_name="demo",
                     spec_path=str(root / "spec.md"), spec_digest="s", policy_digest="p",
                     profile_digest="h", stage=stage, condition=condition, iteration=iteration)
    if kind is not None:
        state.blocked_kind = kind
    if reason is not None:
        state.blocked_reason = reason
    if retry is not None:
        state.retry_after = retry
    state.resume_attempts = attempts
    if stop is not None:
        state.stop_reason = stop
    if candidate:
        state.last_accepted_candidate = _candidate(root)
    store = RunStore(root, run_id)
    store.write_state(state)
    if timer and condition is Condition.BLOCKED and kind in STORAGE_CLASSES:
        _arm(root, run_id)                       # a storage park the loop can wake, as the product parks
    return store


def _arm(root: Path, run_id: str, *, kind: TimerKind = TimerKind.STORAGE_RECHECK) -> str:
    """An armed timer of the runs root for the run, as `park_for_storage_pressure` arms it."""
    return TimerStore(root).arm_new(kind=kind, due_at=in_seconds(3600), reason="test",
                                    run_id=run_id).timer_id


def _ended(store: RunStore) -> None:
    """A `hoh run` that ended in order: the product's own marker, running then ended."""
    with store.process_marker():
        pass


def _marker(store: RunStore, **fields) -> None:
    """A marker written by hand (the lock file exists and is free)."""
    store.ensure()
    fd = os.open(str(store.process_lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    os.close(fd)
    store.process_path.write_text(json.dumps(fields) + "\n", encoding="utf-8")


def _died(store: RunStore) -> None:
    _marker(store, pid=4242, since="2026-10-01T00-00-00Z", state="running")


def _build_runs(root: Path) -> dict[str, tuple[str, int, str, bool, str, str]]:
    """The 16 runs of the table of section 2. name -> (run id, exit code, outcome, matched
    under settled, phase, process)."""
    table: dict[str, tuple[str, int, str, bool, str, str]] = {}

    def add(name, store, code, outcome, process, phase=None):
        table[name] = (store.run_id, code, outcome, True, phase or outcome, process)

    s = _make(root, "acc-cp", stage=Stage.CHECKPOINTED, candidate=True)
    add("accepted at CHECKPOINTED", s, 0, "accepted", "none")
    s = _make(root, "acc-rfd", stage=Stage.READY_FOR_DELIVERY, candidate=True)
    add("accepted at READY_FOR_DELIVERY", s, 0, "accepted", "none")
    s = _make(root, "acc-ended", stage=Stage.CHECKPOINTED, candidate=True)
    _ended(s)
    add("accepted, process ended", s, 0, "accepted", "ended")
    add("failed", _make(root, "failed", condition=Condition.FAILED), 1, "failed", "none")
    add("cancelled", _make(root, "cancelled", condition=Condition.CANCELLED), 1, "cancelled", "none")
    add("blocked", _make(root, "blocked", condition=Condition.BLOCKED,
                         reason="a review finding is open"), 3, "blocked", "none")
    add("capability violation", _make(root, "capviol", condition=Condition.BLOCKED,
                                      reason=CAPABILITY_REASON), 3, "blocked", "none")
    add("violation under a contract park", _make(
        root, "capcontract", condition=Condition.BLOCKED, kind="parked_by_contract",
        reason=CAPABILITY_REASON, retry=PAST), 3, "blocked", "none")
    add("contract park", _make(root, "contract", condition=Condition.BLOCKED,
                               kind="parked_by_contract", reason="parked by the contract",
                               retry=PAST), 8, "held", "none")
    add("collected for the digest", _make(
        root, "digest", condition=Condition.BLOCKED, kind="collected_for_digest",
        reason="a question for the digest", retry=PAST), 8, "held", "none")
    add("blocked at CHECKPOINTED", _make(
        root, "blocked-cp", stage=Stage.CHECKPOINTED, condition=Condition.BLOCKED,
        reason="a capability violation was found after the acceptance"), 3, "blocked", "none")
    add("usage limit", _make(root, "usage", condition=Condition.BLOCKED, kind="usage_limit",
                             reason="usage limit reached", retry=PAST), 4, "parked", "none")
    add("storage pressure", _make(root, "storage", condition=Condition.BLOCKED,
                                  kind="storage_pressure", reason="the disk is full",
                                  retry=PAST), 4, "parked", "none")
    add("paused", _make(root, "paused", condition=Condition.PAUSED), 5, "paused", "none")
    s = _make(root, "idle")
    _ended(s)
    add("idle", s, 6, "idle", "ended")
    s = _make(root, "died")
    _died(s)
    add("died", s, 7, "died", "died")
    assert len(table) == 16
    return table


# --------------------------------------------------------------------------- #
# Running the command
# --------------------------------------------------------------------------- #


class Answer:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.doc = json.loads(out) if out.strip() else None


def _ask(capsys, root: Path, run_id: str, *argv: str) -> Answer:
    try:
        code = cli.main(["--root", str(root), "wait", run_id, *argv])
    except SystemExit as exc:                       # argparse's own refusals
        code = exc.code
    captured = capsys.readouterr()
    if code == 2 and "invalid choice: 'wait'" in captured.err:
        pytest.fail("`hoh wait` does not exist: the parser has no such command")
    return Answer(code, captured.out, captured.err)


def _tree(root: Path) -> dict[str, tuple]:
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                                 info.st_ino)
    return seen


# --------------------------------------------------------------------------- #
# W1 - W5: the phase and the outcome
# --------------------------------------------------------------------------- #


def test_every_state_ends_the_wait_with_its_own_outcome_and_exit_code(tmp_path, capsys):
    """16 real runs, one per row of section 2, answered with `--timeout 0`: the outcome, the exit
    code (hard-coded), `matched`, the phase and the process. Catches a phase table that drifts
    (a violation read as a park, a contract park read as a wait for time, blocked at CHECKPOINTED
    read as accepted)."""
    root = tmp_path / "runs"
    runs = _build_runs(root)
    for name, (run_id, code, outcome, matched, phase, process) in runs.items():
        before = _tree(root)
        answer = _ask(capsys, root, run_id, "--timeout", "0")
        assert answer.err == "", (name, answer.err)
        assert answer.code == code, (name, answer.code, answer.out)
        doc = answer.doc
        assert (doc["outcome"], doc["matched"], doc["phase"], doc["process"]) == (
            outcome, matched, phase, process), name
        assert doc["exit_code"] == code, name
        assert doc["polls"] == 1, name
        assert _tree(root) == before, name
    done = _ask(capsys, root, "acc-ended", "--timeout", "0").doc
    assert done["process_since"] and done["process_ended"], "an ended marker says when"
    assert _ask(capsys, root, "idle", "--timeout", "0").doc["process_ended"]
    died = _ask(capsys, root, "died", "--timeout", "0").doc
    assert died["process_since"] == "2026-10-01T00-00-00Z" and died["process_ended"] is None
    none = _ask(capsys, root, "failed", "--timeout", "0").doc
    assert none["process_since"] is None and none["process_ended"] is None
    usage = _ask(capsys, root, "usage", "--timeout", "0").doc
    assert usage["blocked_kind"] == "usage_limit" and usage["retry_after"] == PAST
    contract = _ask(capsys, root, "contract", "--timeout", "0").doc
    assert contract["blocked_kind"] == "parked_by_contract" and contract["retry_after"] == PAST

    # A run not yet started, and one being executed: one look, then the time runs out.
    _make(root, "fresh", stage=Stage.NEW)
    fresh = _ask(capsys, root, "fresh", "--timeout", "0")
    assert fresh.code == 124 and fresh.doc["outcome"] == "timeout"
    assert (fresh.doc["phase"], fresh.doc["process"], fresh.doc["polls"]) == ("running", "none", 1)
    store = _make(root, "busy", stage=Stage.VERIFYING)
    with store.process_marker():
        busy = _ask(capsys, root, "busy", "--timeout", "0")
    assert busy.code == 124 and busy.doc["outcome"] == "timeout"
    assert (busy.doc["phase"], busy.doc["process"], busy.doc["polls"]) == ("running", "live", 1)
    assert busy.doc["matched"] is False


def test_terminal_is_only_accepted_failed_or_cancelled_and_settled_adds_the_halts(tmp_path, capsys):
    """The same runs under `terminal` and `settled`: a finished run ends both, a halt (blocked,
    parked, held, paused) ends only `settled`, an idle or dead run ends both but matches only
    `settled`. Catches a strict `terminal` that ends on a halt."""
    root = tmp_path / "runs"
    runs = _build_runs(root)
    finished = {"accepted", "failed", "cancelled"}
    halts = {"blocked", "parked", "held", "paused"}
    for name, (run_id, code, outcome, _matched, _phase, _process) in runs.items():
        settled = _ask(capsys, root, run_id, "--until", "settled", "--timeout", "0")
        assert (settled.code, settled.doc["outcome"], settled.doc["matched"]) == (code, outcome, True), name
        strict = _ask(capsys, root, run_id, "--until", "terminal", "--timeout", "0")
        if outcome in finished:
            assert (strict.code, strict.doc["outcome"], strict.doc["matched"]) == (code, outcome, True), name
        elif outcome in halts:
            assert strict.code == 124 and strict.doc["outcome"] == "timeout", name
            assert strict.doc["matched"] is False, name
        else:                                          # idle, died: never sat on
            assert (strict.code, strict.doc["outcome"], strict.doc["matched"]) == (code, outcome, False), name


def test_stage_and_condition_waits_match_the_state_and_failed_or_cancelled_end_them(tmp_path, capsys):
    """`stage=` and `condition=` hold whatever the process is doing; a FAILED or CANCELLED run ends
    every stage or condition wait that did not match, also while its process still runs. Catches a
    wait for a stage the run can no longer reach."""
    root = tmp_path / "runs"
    live = _make(root, "live", stage=Stage.VERIFYING)
    with live.process_marker():
        for until in ("stage=VERIFYING", "condition=ACTIVE"):
            got = _ask(capsys, root, "live", "--until", until, "--timeout", "0")
            assert (got.code, got.doc["outcome"], got.doc["matched"]) == (0, "reached", True), until
        for until in ("stage=DEVELOPING", "condition=PAUSED", "condition=FAILED"):
            got = _ask(capsys, root, "live", "--until", until, "--timeout", "0")
            assert (got.code, got.doc["outcome"], got.doc["matched"]) == (124, "timeout", False), until
    for name, condition, outcome in (("failed", Condition.FAILED, "failed"),
                                     ("cancelled", Condition.CANCELLED, "cancelled")):
        store = _make(root, name, stage=Stage.VERIFYING, condition=condition)
        with store.process_marker():                   # the process still runs
            for until in ("stage=CHECKPOINTED", "condition=ACTIVE"):
                got = _ask(capsys, root, name, "--until", until, "--timeout", "0")
                assert (got.code, got.doc["outcome"], got.doc["matched"]) == (1, outcome, False), (name, until)
            held = _ask(capsys, root, name, "--until", "stage=VERIFYING", "--timeout", "0")
            assert (held.code, held.doc["outcome"], held.doc["matched"]) == (0, "reached", True)
    again = _ask(capsys, root, "failed", "--until", "condition=FAILED", "--timeout", "0")
    assert (again.code, again.doc["outcome"], again.doc["matched"]) == (0, "reached", True)


def test_a_process_still_executing_a_finished_run_keeps_the_wait_open(tmp_path, capsys):
    """An accepted run whose process still holds the run's lock is `finishing`, not accepted: a
    `hoh run` with several iterations goes on after a checkpoint. Catches an "accepted" told
    while the run goes on."""
    root = tmp_path / "runs"
    done = _make(root, "done", stage=Stage.CHECKPOINTED, candidate=True)
    with done.process_marker():
        for until in ("terminal", "settled"):
            got = _ask(capsys, root, "done", "--until", until, "--timeout", "0")
            assert got.code == 124 and got.doc["outcome"] == "timeout", until
            assert got.doc["phase"] == "finishing" and got.doc["matched"] is False, until
            assert got.doc["process"] == "live", until
    for until in ("terminal", "settled"):
        got = _ask(capsys, root, "done", "--until", until, "--timeout", "0")
        assert (got.code, got.doc["outcome"], got.doc["phase"], got.doc["process"]) == (
            0, "accepted", "accepted", "ended"), until
    stuck = _make(root, "stuck", condition=Condition.BLOCKED, reason="a review finding is open")
    with stuck.process_marker():
        got = _ask(capsys, root, "stuck", "--timeout", "0")
        assert got.code == 124 and got.doc["phase"] == "finishing"
    assert _ask(capsys, root, "stuck", "--timeout", "0").code == 3


def test_a_run_nothing_executes_is_idle_or_died_and_a_starting_one_is_not_called_dead(
        tmp_path, capsys, monkeypatch):
    """Idle (6) and died (7) end every `--until` kind, matched only for `settled`; a requested
    stage that holds wins; the lock probe `RunStore.running_process` is called first and counted;
    a marker `running` with a free lock is probed twice (free then held reads live); an unreadable
    marker reads none; a run that finishes between the probe and the state read is accepted,
    never idle. Catches a dead verdict on a run that is just starting, and the wrong read order."""
    root = tmp_path / "runs"
    idle = _make(root, "idle")
    _ended(idle)
    dead = _make(root, "dead")
    _died(dead)
    for name, code in (("idle", 6), ("dead", 7)):
        outcome = "idle" if name == "idle" else "died"
        for until in ("settled", "terminal", "stage=VERIFYING", "condition=PAUSED"):
            got = _ask(capsys, root, name, "--until", until, "--timeout", "0")
            assert (got.code, got.doc["outcome"], got.doc["matched"]) == (
                code, outcome, until == "settled"), (name, until)
        won = _ask(capsys, root, name, "--until", "stage=DEVELOPING", "--timeout", "0")
        assert (won.code, won.doc["outcome"], won.doc["matched"]) == (0, "reached", True), name

    calls: list[int] = []
    original = RunStore.running_process

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(RunStore, "running_process", counting)
    ended = _make(root, "ended")
    _ended(ended)
    runwait.observe_run(ended)
    assert len(calls) == 1, "an ended marker: one probe"
    calls.clear()
    runwait.observe_run(_make(root, "nomarker"))
    assert len(calls) == 1, "no marker: one probe"
    calls.clear()
    held = _make(root, "held")
    with held.process_marker():
        assert runwait.observe_run(held).process == "live"
    assert len(calls) == 1, "a held lock: one probe"
    calls.clear()
    assert runwait.observe_run(dead).process == "died"
    assert len(calls) == 2, "a running marker with a free lock: two probes"
    calls.clear()

    answers = iter([None, {"pid": 4242}])
    monkeypatch.setattr(RunStore, "running_process", lambda self: next(answers))
    assert runwait.observe_run(dead).process == "live", "free, then held: a run that just started"
    answers = iter([None, None])
    monkeypatch.setattr(RunStore, "running_process", lambda self: next(answers))
    assert runwait.observe_run(dead).process == "died"
    monkeypatch.setattr(RunStore, "running_process", original)

    broken = _make(root, "broken")
    broken.ensure()
    broken.process_path.write_text("{not json", encoding="utf-8")
    assert runwait.observe_run(broken).process == "none"

    # The probe comes before the state is read: a run that finishes in between is accepted.
    racing = _make(root, "racing")
    _ended(racing)

    def finishing(self):
        if not finishing.done:
            finishing.done = True
            state = self.read_state()
            state.stage = Stage.CHECKPOINTED
            state.last_accepted_candidate = _candidate()
            self.write_state(state)
        return original(self)

    finishing.done = False
    monkeypatch.setattr(RunStore, "running_process", finishing)
    obs = runwait.observe_run(racing)
    assert obs.process == "ended" and obs.state_phase == "accepted"
    assert runwait.phase_of(obs) == "accepted", "never idle"


# --------------------------------------------------------------------------- #
# W6 - W8: the polling, bounded
# --------------------------------------------------------------------------- #


def test_the_wait_polls_gently_and_stops_at_its_timeout(tmp_path, monkeypatch):
    """The injected clock sleeps `[10, 10, 5]` for `timeout=25, poll=10` with one look per poll;
    a run reaching a checkpoint during the second sleep is seen at the next look and no later one;
    a bad poll or timeout raises before any sleep. Catches a loop that sleeps zero seconds, a last
    sleep that overshoots, and a floor on the poll that is not there."""
    root = tmp_path / "runs"
    store = _make(root, "r1")
    looks: list[int] = []

    def observe(st):
        looks.append(1)
        return runwait.observe_run(st)

    clock = Clock()
    result = runwait.wait(store, runwait.parse_until(None), timeout=25, poll=10,
                          sleep=clock.sleep, monotonic=clock.monotonic, observe=observe)
    assert clock.sleeps == [10, 10, 5]
    assert len(looks) == 4 and result.polls == 4
    assert (result.outcome, result.exit_code, result.waited_s) == ("timeout", 124, 25.0)
    assert result.matched is False and result.phase == "running"

    clock = Clock()

    def checkpoint_during_the_second_sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 2:
            state = store.read_state()
            state.stage = Stage.CHECKPOINTED
            state.last_accepted_candidate = _candidate()
            store.write_state(state)

    result = runwait.wait(store, runwait.parse_until(None), timeout=25, poll=10,
                          sleep=checkpoint_during_the_second_sleep, monotonic=clock.monotonic)
    assert clock.sleeps == [10, 10]
    assert (result.outcome, result.polls, result.waited_s) == ("accepted", 3, 20.0)

    clock = Clock()
    once = runwait.wait(store, runwait.parse_until("terminal"), timeout=0, poll=10,
                        sleep=clock.sleep, monotonic=clock.monotonic)
    assert once.polls == 1 and clock.sleeps == []

    never = Clock()
    for poll in (0, 0.5, math.nan, math.inf):
        with pytest.raises(ValueError):
            runwait.wait(store, runwait.parse_until(None), timeout=5, poll=poll,
                         sleep=never.sleep, monotonic=never.monotonic)
    for timeout in (-1, math.nan, math.inf):
        with pytest.raises(ValueError):
            runwait.wait(store, runwait.parse_until(None), timeout=timeout, poll=10,
                         sleep=never.sleep, monotonic=never.monotonic)
    assert never.sleeps == []


def test_the_flags_reach_the_wait(tmp_path, capsys, monkeypatch):
    """Through `cli.main` with `runwait.time` replaced: `--timeout` and `--poll` reach the wait,
    the defaults are 3600 and 10, `--until` reaches it. Catches a flag that is parsed and
    dropped."""
    root = tmp_path / "runs"
    _make(root, "r1")
    clock = _use_clock(monkeypatch, Clock(budget=400))
    got = _ask(capsys, root, "r1", "--timeout", "25", "--poll", "10")
    assert clock.sleeps == [10, 10, 5] and got.code == 124
    assert got.doc["until"] == "settled" and got.doc["polls"] == 4 and got.doc["waited_s"] == 25.0

    clock = _use_clock(monkeypatch, Clock(budget=400))
    got = _ask(capsys, root, "r1", "--timeout", "7", "--poll", "3", "--until", "terminal")
    assert clock.sleeps == [3, 3, 1] and got.code == 124 and got.doc["until"] == "terminal"

    clock = _use_clock(monkeypatch, Clock(budget=400))
    got = _ask(capsys, root, "r1")
    assert clock.sleeps == [10] * 360 and got.code == 124
    assert got.doc["until"] == "settled" and got.doc["polls"] == 361

    args = cli.build_parser().parse_args(["wait", "r1"])
    assert (args.until, args.timeout, args.poll) == (None, 3600.0, 10.0)


def test_a_state_already_at_rest_at_start_ends_the_wait_at_once(tmp_path, capsys):
    """A finished run with the default timeout: exit 0, one look, the sleep is never called (the
    default clock of this module fails on any sleep). The regression of the monitors that took
    the state at restart as their baseline and missed a run that had already finished."""
    root = tmp_path / "runs"
    _make(root, "r1", stage=Stage.CHECKPOINTED, candidate=True)
    got = _ask(capsys, root, "r1")
    assert got.code == 0 and got.doc["outcome"] == "accepted"
    assert got.doc["polls"] == 1 and got.doc["waited_s"] == 0.0


# --------------------------------------------------------------------------- #
# W9 - W10: read-only, and what is refused
# --------------------------------------------------------------------------- #


def test_the_wait_never_writes(tmp_path, capsys):
    """A snapshot of the whole tree (mode, size, mtime in ns, inode of every entry) is equal
    before and after 30 waits over accepted, live, died, parked and legacy-form runs, every
    `--until` kind. Catches a reader that migrates, parks or creates anything (`read_state`)."""
    root = tmp_path / "runs"
    accepted = _make(root, "acc", stage=Stage.CHECKPOINTED, candidate=True)
    _ended(accepted)
    live = _make(root, "live", stage=Stage.VERIFYING)
    dead = _make(root, "dead")
    _died(dead)
    _make(root, "park", condition=Condition.BLOCKED, kind="usage_limit", reason="limit",
          retry=PAST)
    legacy = _make(root, "legacy")
    raw = json.loads(legacy.state_path.read_text(encoding="utf-8"))
    raw["started_monotonic"] = 12345.0                # a field the schema dropped
    legacy.state_path.write_text(json.dumps(raw), encoding="utf-8")

    # The fixture is a real legacy form: `read_state()` rewrites it (and parks the old file).
    copy_root = tmp_path / "copy"
    shutil.copytree(root / "legacy", copy_root / "legacy")
    before_read = _tree(copy_root)
    RunStore(copy_root, "legacy").read_state()
    assert _tree(copy_root) != before_read, "the legacy fixture must be one read_state() changes"

    untils = ("settled", "terminal", "stage=CHECKPOINTED", "stage=NEW", "condition=ACTIVE",
              "condition=PAUSED")
    waits = 0
    with live.process_marker():
        snapshot = _tree(root)
        for run_id in ("acc", "live", "dead", "park", "legacy"):
            for until in untils:
                got = _ask(capsys, root, run_id, "--until", until, "--timeout", "0")
                assert got.code in (0, 3, 4, 6, 7, 124), (run_id, until, got.code, got.err)
                waits += 1
        assert _tree(root) == snapshot
    assert waits >= 24
    legacy_answer = _ask(capsys, root, "legacy", "--timeout", "0")
    assert legacy_answer.doc["stage"] == "DEVELOPING", "a legacy state is understood"

    missing = _ask(capsys, root, "nosuch", "--timeout", "0")
    assert missing.code == 2 and not (root / "nosuch").exists()


def test_what_cannot_be_waited_on_is_refused_with_exit_2_and_nothing_on_stdout(
        tmp_path, capsys, monkeypatch):
    """26 refusals: exit 2, nothing on stdout, a message on stderr, the tree unchanged; the same
    requests made valid are answered; a defect inside the reader is exit 2 with `wait failed:`
    and no JSON line; an answer that cannot be written is exit 2, no traceback, and stdout is
    redirected so the flush at exit cannot fail again."""
    root = tmp_path / "runs"
    _make(root, "r1", stage=Stage.CHECKPOINTED, candidate=True)
    garbage = _make(root, "garbage")
    garbage.state_path.write_text("this is not json", encoding="utf-8")
    future = _make(root, "future")
    raw = json.loads(future.state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 9999
    future.state_path.write_text(json.dumps(raw), encoding="utf-8")
    (root / "nostate").mkdir()

    refusals: list[tuple[str, tuple[str, ...]]] = [
        ("nosuch", ()), ("", ()), ("garbage", ()), ("future", ()), ("nostate", ()),
        ("../r1", ()), ("r1/x", ()), (".", ()),
    ]
    refusals += [("r1", ("--until", text)) for text in (
        "Terminal", "stage=checkpointed", "stage=NOPE", "stage=", "condition=blocked",
        "terminal=1", " settled", "")]
    refusals += [("r1", ("--poll", text)) for text in ("0.5", "0", "-3", "nan", "inf", "x")]
    refusals += [("r1", ("--timeout", text)) for text in ("-1", "nan", "inf", "soon")]
    assert len(refusals) == 26
    snapshot = _tree(root)
    for run_id, argv in refusals:
        got = _ask(capsys, root, run_id, *argv)
        assert got.code == 2, (run_id, argv, got.code, got.out)
        assert got.out == "", (run_id, argv)
        assert got.err.strip(), (run_id, argv)
        assert "Traceback" not in got.err, (run_id, argv)
    assert _tree(root) == snapshot

    one = _ask(capsys, root, "nosuch")
    assert one.err.count("\n") == 1, "the command's own refusals are one line"

    for argv in (("--until", "terminal"), ("--until", "stage=CHECKPOINTED"), ("--poll", "1"),
                 ("--timeout", "0"), ("--until", "condition=ACTIVE", "--timeout", "0")):
        got = _ask(capsys, root, "r1", *argv)
        assert got.code == 0 and got.doc["outcome"] in ("accepted", "reached"), argv

    def broken(store):
        raise RuntimeError("boom")

    with monkeypatch.context() as patched:
        patched.setattr(runwait, "observe_run", broken)
        got = _ask(capsys, root, "r1")
    assert got.code == 2 and got.out == "" and got.doc is None
    assert got.err.startswith("wait failed: RuntimeError: boom") and "Traceback" not in got.err

    # An answer that cannot be written: the stdout double has a real descriptor, a pipe whose read
    # end is closed. Writing to it fails before the command and succeeds after (the redirect).
    read_end, write_end = os.pipe()
    os.close(read_end)
    with pytest.raises(OSError):
        os.write(write_end, b"x")

    class Closed:
        def write(self, text):
            raise OSError(errno.EPIPE, "the pipe is closed")

        def flush(self):
            raise OSError(errno.EPIPE, "the pipe is closed")

        def fileno(self):
            return write_end

    with monkeypatch.context() as patched:
        patched.setattr(sys, "stdout", Closed())
        code = cli.main(["--root", str(root), "wait", "r1"])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("wait failed: the answer could not be written") and "Traceback" not in err
    assert os.write(write_end, b"x") == 1, "stdout now points at a pipe nobody reads from"
    os.close(write_end)


# --------------------------------------------------------------------------- #
# W11: the answer
# --------------------------------------------------------------------------- #


def test_the_answer_is_one_ascii_json_line_with_the_documented_fields(tmp_path, capsys, monkeypatch):
    """One line of ASCII, the 23 keys in order, every free text bounded, a block that is over not
    reported. Catches a line a hostile run directory can make long, a non-ASCII byte, and a stale
    block reported as a current one."""
    root = tmp_path / "runs"
    long = "x" * 900
    _make(root, "park", condition=Condition.BLOCKED, kind="usage_limit",
          reason=long, retry=PAST, stop=long)
    got = _ask(capsys, root, "park", "--timeout", "0")
    assert got.out.endswith("\n") and got.out.count("\n") == 1 and got.out.isascii()
    doc = got.doc
    assert list(doc) == KEYS and doc["schema"] == "hoh-wait/1" and doc["run_id"] == "park"
    assert runwait.CAVEATS == ("parked_without_timer", "timer_store_unreadable",
                               "checkpointed_without_candidate", "accepted_process_died")
    assert doc["until"] == "settled" and doc["exit_code"] == 4
    assert doc["blocked_reason"] == "x" * 400 and doc["stop_reason"] == "x" * 400
    assert doc["resume_attempts"] == 0 and doc["binding"] == "unchecked" and doc["caveat"] is None
    assert doc["blocked_kind"] == "usage_limit" and doc["retry_after"] == PAST

    for until in ("terminal", "stage=NEW", "condition=BLOCKED"):
        said = _ask(capsys, root, "park", "--until", until, "--timeout", "0").doc
        assert said["until"] == until

    # A finished run that still carries what an earlier block left: none of it is a block now.
    done = _make(root, "done", stage=Stage.CHECKPOINTED, candidate=True, iteration=3,
                 kind="usage_limit", reason="an old reason", retry=PAST, attempts=3)
    done_doc = _ask(capsys, root, "done", "--timeout", "0").doc
    for key in ("blocked_kind", "blocked_reason", "retry_after", "resume_attempts", "binding",
                "caveat"):
        assert done_doc[key] is None, key
    assert done_doc["accepted_candidate"] == "cand-1" and done_doc["iteration"] == 3
    assert done_doc["condition"] == "ACTIVE" and done_doc["stage"] == "CHECKPOINTED"
    # The state still holds them (the test proves it was a stale block, not an empty field).
    stored = done.read_state()
    assert stored.blocked_kind == "usage_limit" and stored.resume_attempts == 3

    odd = "bad \udcff byte and a letter \u00e9"
    _make(root, "odd", condition=Condition.BLOCKED, reason=odd, stop=odd)
    odd_got = _ask(capsys, root, "odd", "--timeout", "0")
    assert odd_got.out.isascii() and odd_got.out.count("\n") == 1
    assert odd_got.doc["blocked_reason"] == odd == odd_got.doc["stop_reason"]

    # Marker fields and the block's short fields are cut to 64 characters.
    huge = "k" * 100_000
    _make(root, "huge", condition=Condition.BLOCKED, kind=huge, reason="r", retry=huge)
    _marker(RunStore(root, "huge"), pid=1, since=huge, ended=huge, state="ended")
    huge_doc = _ask(capsys, root, "huge", "--timeout", "0").doc
    assert huge_doc["blocked_kind"] == "k" * 64 and huge_doc["retry_after"] == "k" * 64
    assert huge_doc["process_since"] == "k" * 64 and huge_doc["process_ended"] == "k" * 64
    assert len(_ask(capsys, root, "huge", "--timeout", "0").out) < 4000

    # `process_ended` is null unless the marker says ended, whatever else it holds.
    lying = _make(root, "lying", stage=Stage.VERIFYING)
    _marker(lying, pid=1, since="2026-10-01T00-00-00Z", ended="2026-10-02T00-00-00Z",
            state="running")
    assert _ask(capsys, root, "lying", "--timeout", "0").doc["process_ended"] is None
    held = lying.process_lock()
    with held:
        live = _ask(capsys, root, "lying", "--timeout", "0").doc
    assert live["process"] == "live" and live["process_ended"] is None

    # The time: the clock's 1.23456 seconds are reported to three places, with a single look.
    ticks = iter([0.0, 1.23456, 1.23456])
    monkeypatch.setattr(runwait, "time", types.SimpleNamespace(
        sleep=Clock(budget=0).sleep, monotonic=lambda: next(ticks)))
    done_again = _ask(capsys, root, "done", "--timeout", "0").doc
    assert done_again["waited_s"] == 1.235 and done_again["polls"] == 1
    assert "1.23456" not in json.dumps(done_again)
    assert recorded_original(odd) == odd


# --------------------------------------------------------------------------- #
# W12: structurally a reader
# --------------------------------------------------------------------------- #


FORBIDDEN_NAMES = {"read_state", "write_state", "ensure", "lock", "process_lock",
                   "process_marker", "open", "replace", "rename", "unlink", "mkdir", "touch",
                   "write_text", "write_bytes", "kill", "Popen"}
FORBIDDEN_IMPORTS = {"subprocess", "shutil", "signal", "fcntl", "tempfile", "herdr",
                     "agentcontrol", "dispatchers", "pane_ledger", "durable_io", "controller",
                     "stages"}
NOT_AT_THE_TOP = {"resumption", "resumeloop", "timers", "failures", "taxonomy", "dispatchrecovery"}


def test_the_command_is_a_pure_reader_registered_in_the_parser():
    """A syntax-tree scan of the module: no writer name, no process or write-capable import, and
    none of the resume machinery imported at the top (building the parser imports this module).
    Catches a reader that grows a write, and a parser build that becomes heavy."""
    path = Path(runwait.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.alias):
            names = [node.name.split(".")[-1]]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        else:
            continue
        for name in names:
            if name in FORBIDDEN_NAMES or name.startswith("durable_") or name in FORBIDDEN_IMPORTS:
                found.append(f"{name} (line {getattr(node, 'lineno', '?')})")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [(node.module or "").split(".")[0], *[a.name for a in node.names]]
            if node.level:
                modules = [(node.module or "").split(".")[0] or a.name for a in node.names] \
                    + [a.name for a in node.names]
        else:
            continue
        found += [f"import {m}" for m in modules if m in FORBIDDEN_IMPORTS]
    assert not found, found
    for node in tree.body:                              # top-level imports only
        if isinstance(node, ast.ImportFrom):
            modules = {(node.module or "").split(".")[0], *[a.name for a in node.names]}
        elif isinstance(node, ast.Import):
            modules = {a.name.split(".")[0] for a in node.names}
        else:
            continue
        assert not modules & NOT_AT_THE_TOP, (node.lineno, modules & NOT_AT_THE_TOP)

    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    wait = subparsers.choices["wait"]
    assert wait.get_default("func") is cli.cmd_wait
    options = {flag: a for a in wait._actions for flag in a.option_strings}
    assert options["--timeout"].type is runwait.timeout_arg
    assert options["--poll"].type is runwait.poll_arg
    assert options["--until"].default is None and "held" in options["--until"].help


# --------------------------------------------------------------------------- #
# W13: parked is a loop's, held is a person's
# --------------------------------------------------------------------------- #


def test_parked_means_a_loop_owns_the_kind_and_a_contract_park_is_held_for_a_person(tmp_path, capsys):
    """For every waiting kind of the product: `parked` (4) iff a resume loop consumes it, else
    `held` (8); the attempt limit makes a given-up quota park `held`; a violation is `blocked`
    under any of them; a lifted park reads `idle`. Catches a contract park read as a wait for
    time, and a park the loop gave up on read as one it will lift."""
    root = tmp_path / "runs"
    assert {"parked_by_contract", "collected_for_digest"} <= WAITING_KINDS
    assert AUTOMATIC_KINDS <= QUOTA_CLASSES
    assert not {"parked_by_contract", "collected_for_digest"} & (QUOTA_CLASSES | STORAGE_CLASSES)

    for index, kind in enumerate(sorted(WAITING_KINDS)):
        loop_owns = kind in QUOTA_CLASSES or kind in STORAGE_CLASSES
        _make(root, f"k{index}", condition=Condition.BLOCKED, kind=kind, reason="waiting",
              retry=PAST)
        got = _ask(capsys, root, f"k{index}", "--timeout", "0")
        doc = got.doc
        assert (got.code, doc["outcome"]) == ((4, "parked") if loop_owns else (8, "held")), kind
        assert doc["phase"] == doc["outcome"] and doc["matched"] is True, kind
        assert doc["blocked_kind"] == kind and doc["retry_after"] == PAST, kind
        assert doc["resume_attempts"] == 0, kind
        assert doc["binding"] == ("unchecked" if kind in QUOTA_CLASSES else None), kind
        if not loop_owns:
            strict = _ask(capsys, root, f"k{index}", "--until", "terminal", "--timeout", "0")
            assert strict.code == 124, f"held does not end terminal: {kind}"
        _make(root, f"v{index}", condition=Condition.BLOCKED, kind=kind,
              reason=CAPABILITY_REASON, retry=PAST)
        assert _ask(capsys, root, f"v{index}", "--timeout", "0").code == 3, kind

    _make(root, "unknown", condition=Condition.BLOCKED, kind="mystery", reason="who knows")
    _make(root, "nokind", condition=Condition.BLOCKED, reason="no kind at all")
    for name in ("unknown", "nokind"):
        assert _ask(capsys, root, name, "--timeout", "0").code == 3, name

    # The attempt limit: the quota loop gives up, the storage loop has no limit.
    for kind in sorted(QUOTA_CLASSES | STORAGE_CLASSES):
        for attempts in (MAX_RESUME_ATTEMPTS - 1, MAX_RESUME_ATTEMPTS, MAX_RESUME_ATTEMPTS + 3):
            name = f"a-{kind}-{attempts}"
            _make(root, name, condition=Condition.BLOCKED, kind=kind, reason="limit",
                  retry=PAST, attempts=attempts)
            got = _ask(capsys, root, name, "--timeout", "0")
            doc = got.doc
            given_up = kind in QUOTA_CLASSES and attempts >= MAX_RESUME_ATTEMPTS
            assert (got.code, doc["outcome"]) == ((8, "held") if given_up else (4, "parked")), (
                kind, attempts)
            assert doc["resume_attempts"] == attempts, (kind, attempts)
            assert doc["binding"] == ("unchecked" if kind in QUOTA_CLASSES and not given_up
                                      else None), (kind, attempts)
    _make(root, "given-up-violation", condition=Condition.BLOCKED, kind="usage_limit",
          reason=CAPABILITY_REASON, retry=PAST, attempts=MAX_RESUME_ATTEMPTS)
    assert _ask(capsys, root, "given-up-violation", "--timeout", "0").code == 3

    # A lifted park: ACTIVE, the old kind still on it, nothing executing it. Idle, no block shown.
    lifted = _make(root, "lifted", kind="usage_limit", reason="was blocked", retry=PAST,
                   attempts=2)
    _ended(lifted)
    got = _ask(capsys, root, "lifted", "--timeout", "0")
    assert (got.code, got.doc["outcome"]) == (6, "idle")
    for key in ("blocked_kind", "blocked_reason", "retry_after", "resume_attempts", "binding"):
        assert got.doc[key] is None, key


# --------------------------------------------------------------------------- #
# W14: the incident, as one node
# --------------------------------------------------------------------------- #


def test_an_accepted_run_ends_settled_and_terminal_waits_with_exit_0(tmp_path, capsys):
    """A run at CHECKPOINTED (and one at READY_FOR_DELIVERY) with the condition still ACTIVE, an
    accepted candidate and an ended process is accepted, for the default, `settled` and `terminal`
    waits, with the default timeout and with `--timeout 0`, without a sleep. Catches the monitor's
    old mistake of waiting for the condition to leave ACTIVE."""
    root = tmp_path / "runs"
    for stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
        store = _make(root, stage.value.lower(), stage=stage, candidate=True)
        _ended(store)
        for until in ([], ["--until", "settled"], ["--until", "terminal"]):
            for timeout in ([], ["--timeout", "0"]):
                got = _ask(capsys, root, store.run_id, *until, *timeout)
                doc = got.doc
                assert got.code == 0, (stage, until, timeout)
                assert (doc["outcome"], doc["phase"], doc["matched"]) == ("accepted", "accepted", True)
                assert (doc["condition"], doc["accepted_candidate"], doc["process"]) == (
                    "ACTIVE", "cand-1", "ended")
                assert doc["polls"] == 1
        for until in (f"stage={stage.value}", "condition=ACTIVE"):
            got = _ask(capsys, root, store.run_id, "--until", until)
            assert (got.code, got.doc["outcome"], got.doc["matched"]) == (0, "reached", True), until


# --------------------------------------------------------------------------- #
# W15 - W18: the minors of the merge of I12 (strand i12b)
# --------------------------------------------------------------------------- #


def test_a_final_stage_without_its_accepted_candidate_is_held_not_accepted(tmp_path, capsys):
    """CHECKPOINTED or READY_FOR_DELIVERY, ACTIVE, no accepted candidate: `held` (8) with the
    caveat `checkpointed_without_candidate`, whatever the process (ended, died, none), still
    `finishing` while a process holds the lock; with its candidate the same state is accepted
    with no caveat. Catches an "accepted" that no acceptance backs."""
    root = tmp_path / "runs"
    for stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
        for process in ("ended", "died", "none"):
            store = _make(root, f"{stage.value.lower()}-{process}", stage=stage, candidate=False)
            {"ended": _ended, "died": _died, "none": lambda s: None}[process](store)
            got = _ask(capsys, root, store.run_id, "--timeout", "0")
            doc = got.doc
            assert got.code == 8, (stage, process, got.out)
            assert (doc["outcome"], doc["phase"], doc["matched"]) == ("held", "held", True)
            assert (doc["caveat"], doc["accepted_candidate"], doc["process"]) == (
                "checkpointed_without_candidate", None, process)
            strict = _ask(capsys, root, store.run_id, "--until", "terminal", "--timeout", "0")
            assert strict.code == 124 and strict.doc["matched"] is False, "held is not terminal"
            reached = _ask(capsys, root, store.run_id, "--until", f"stage={stage.value}")
            assert (reached.code, reached.doc["outcome"]) == (0, "reached")
        busy = _make(root, f"{stage.value.lower()}-live", stage=stage, candidate=False)
        with busy.process_marker():
            got = _ask(capsys, root, busy.run_id, "--timeout", "0")
        assert (got.code, got.doc["phase"], got.doc["process"]) == (124, "finishing", "live")
        backed = _make(root, f"{stage.value.lower()}-backed", stage=stage, candidate=True)
        _ended(backed)
        doc = _ask(capsys, root, backed.run_id, "--timeout", "0").doc
        assert (doc["outcome"], doc["caveat"], doc["accepted_candidate"]) == ("accepted", None, "cand-1")


def test_a_storage_park_nothing_will_wake_is_held_and_says_why(tmp_path, capsys, monkeypatch):
    """A storage_pressure park is `parked` (4) only while an ARMED STORAGE_RECHECK timer of the
    run exists in the runs root. No timer file, a timer of another run, another kind, already
    fired or disarmed: `held` (8) with `parked_without_timer`. An unreadable timer store: `held`
    with `timer_store_unreadable`, no traceback. A quota park needs no timer (its loop has the
    retry_after fallback). The wait writes nothing. A park lifted between the state read and the
    timer read is not told as held."""
    root = tmp_path / "runs"
    kw = dict(condition=Condition.BLOCKED, kind="storage_pressure", reason="the disk is full",
              retry=PAST)
    _make(root, "armed", **kw)
    got = _ask(capsys, root, "armed", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc.get("caveat")) == (4, "parked", None)
    assert got.doc["binding"] is None

    # The runs below share one timers.json: the no-file case gets a root of its own.
    solo = tmp_path / "solo"
    _make(solo, "alone", timer=False, **kw)
    assert not (solo / "timers.json").exists()
    before = _tree(solo)
    got = _ask(capsys, solo, "alone", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc["phase"]) == (8, "held", "held")
    assert (got.doc.get("caveat"), got.doc["matched"], got.doc["blocked_kind"]) == (
        "parked_without_timer", True, "storage_pressure")
    assert got.doc["binding"] is None and _tree(solo) == before and not (solo / "timers.json").exists()
    strict = _ask(capsys, solo, "alone", "--until", "terminal", "--timeout", "0")
    assert strict.code == 124, "held does not end a terminal wait"

    # Timers that do not wake this run: another run's, another kind's, a spent one.
    _make(root, "other", timer=False, **kw)                      # only "armed" has a timer
    got = _ask(capsys, root, "other", "--timeout", "0")
    assert (got.code, got.doc.get("caveat")) == (8, "parked_without_timer")
    _make(root, "kind", timer=False, **kw)
    _arm(root, "kind", kind=TimerKind.EXTERNAL_WAIT)
    got = _ask(capsys, root, "kind", "--timeout", "0")
    assert (got.code, got.doc.get("caveat")) == (8, "parked_without_timer")
    for spent, how in (("fired", "fire"), ("disarmed", "disarm")):
        _make(root, spent, timer=False, **kw)
        timer_id = _arm(root, spent)
        getattr(TimerStore(root), how)(timer_id, *(["why"] if how == "disarm" else []))
        got = _ask(capsys, root, spent, "--timeout", "0")
        assert (got.code, got.doc.get("caveat")) == (8, "parked_without_timer"), spent
    assert any(t.status is not TimerStatus.ARMED for t in TimerStore(root).read())

    # A quota park has no timer and stays parked.
    _make(root, "quota", condition=Condition.BLOCKED, kind="usage_limit", reason="limit", retry=PAST)
    got = _ask(capsys, root, "quota", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc.get("caveat")) == (4, "parked", None)

    # An unreadable timer store: nothing wakes the park while it is unreadable.
    (root / "timers.json").write_text("{not json", encoding="utf-8")
    snapshot = _tree(root)
    got = _ask(capsys, root, "armed", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc.get("caveat")) == (8, "held", "timer_store_unreadable")
    assert got.err == "" and _tree(root) == snapshot
    assert _ask(capsys, root, "quota", "--timeout", "0").code == 4, "a quota park does not read timers"

    # The loop lifts the park between the state read and the timer read: the answer follows.
    race = tmp_path / "race"
    store = _make(race, "lifted", timer=False, **kw)
    _ended(store)

    def lifting_read(self):
        state = store.read_state()
        state.condition = Condition.ACTIVE
        state.blocked_kind = None
        state.retry_after = None
        store.write_state(state)
        return []

    with monkeypatch.context() as patched:
        patched.setattr(TimerStore, "read", lifting_read)
        got = _ask(capsys, race, "lifted", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc.get("caveat")) == (6, "idle", None)
    assert got.doc["blocked_kind"] is None and got.doc["condition"] == "ACTIVE"

    # A quota park replaces the storage park between the two reads: the second read decides, the
    # kind is checked again -- a quota park needs no timer, so it is parked, not held.
    swap = tmp_path / "swap"
    swapped = _make(swap, "swapped", timer=False, **kw)

    def swapping_read(self):
        state = swapped.read_state()
        state.blocked_kind = "usage_limit"
        swapped.write_state(state)
        return []

    with monkeypatch.context() as patched:
        patched.setattr(TimerStore, "read", swapping_read)
        got = _ask(capsys, swap, "swapped", "--timeout", "0")
    assert (got.code, got.doc["outcome"], got.doc.get("caveat")) == (4, "parked", None)
    assert (got.doc["blocked_kind"], got.doc["binding"]) == ("usage_limit", "unchecked")


def test_an_accepted_run_whose_process_died_is_accepted_and_says_so(tmp_path, capsys):
    """Accepted with a marker that says `running` and a free lock: still accepted, exit 0 (the
    state is the truth), and the caveat `accepted_process_died` says the close may not have run.
    No caveat for an ended marker, no marker or a live process; a run that is not accepted and
    whose process died is `died` with no caveat. Catches a silent 0."""
    root = tmp_path / "runs"
    died = _make(root, "died", stage=Stage.CHECKPOINTED, candidate=True)
    _died(died)
    for until in ([], ["--until", "terminal"], ["--until", "settled"]):
        got = _ask(capsys, root, "died", *until, "--timeout", "0")
        doc = got.doc
        assert got.code == 0, until
        assert (doc["outcome"], doc["phase"], doc["process"], doc.get("caveat")) == (
            "accepted", "accepted", "died", "accepted_process_died")
        assert doc["accepted_candidate"] == "cand-1" and doc["process_since"] == "2026-10-01T00-00-00Z"
    ended = _make(root, "ended", stage=Stage.READY_FOR_DELIVERY, candidate=True)
    _ended(ended)
    plain = _make(root, "plain", stage=Stage.CHECKPOINTED, candidate=True)
    for name in ("ended", "plain"):
        assert _ask(capsys, root, name, "--timeout", "0").doc.get("caveat") is None, name
    with plain.process_marker():
        doc = _ask(capsys, root, "plain", "--timeout", "0").doc
    assert (doc["phase"], doc["process"], doc.get("caveat")) == ("finishing", "live", None)
    killed = _make(root, "killed", stage=Stage.VERIFYING)
    _died(killed)
    doc = _ask(capsys, root, "killed", "--timeout", "0").doc
    assert (doc["outcome"], doc.get("caveat")) == ("died", None)


def test_a_closed_stdout_is_exit_2_before_any_look_and_never_exit_0_without_a_line(
        tmp_path, capsys, monkeypatch):
    """`hoh wait RUN >&-` starts with no sys.stdout: exit 2, a message on stderr, no traceback,
    no look at the run (no sleep, no state read), the tree unchanged; the same request with a
    stdout is answered. Catches a 0 that told nothing."""
    root = tmp_path / "runs"
    _make(root, "r1", stage=Stage.CHECKPOINTED, candidate=True)
    snapshot = _tree(root)
    looked: list[str] = []
    real = runwait.observe_run
    for run_id, argv in (("r1", ["--timeout", "0"]), ("r1", []), ("r1", ["--until", "terminal"]),
                         ("nosuch", [])):
        with monkeypatch.context() as patched:
            patched.setattr(runwait, "observe_run", lambda store: looked.append(store.run_id) or real(store))
            patched.setattr(sys, "stdout", None)
            code = cli.main(["--root", str(root), "wait", run_id, *argv])
        err = capsys.readouterr().err
        assert code == 2, (run_id, argv, code)
        assert "stdout is closed" in err and "Traceback" not in err, err
        assert looked == [], "the refusal comes before the first look"
    assert _tree(root) == snapshot
    got = _ask(capsys, root, "r1", "--timeout", "0")
    assert got.code == 0 and got.doc["outcome"] == "accepted"
