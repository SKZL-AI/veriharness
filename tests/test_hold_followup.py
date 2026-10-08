"""Follow-up to qa-arena-write (integration review qaw8-a): the hold and the seal ledger.

What this file pins (spec `dogfood/qaw-followup/spec.md`):

* **Every automatic resume path refuses a run held by a capability violation**, in every shape the
  hold can have: the sticky kind, and the legacy shape (a kind that is not the violation's, a reason
  whose first line names it). `hoh resume-quota`'s `retry_after` fallback (`resumeloop._legacy_run`),
  its timer path (`_resume_run`) and `hoh resume-storage` each ask `stages.violation_unresolved`
  themselves, so no path depends on the class filter or on rule 7 of `due_resumptions` alone.
* **A storage refusal never parks a held run**: `storage_park.park_for_storage_pressure` keeps the
  hold and arms no timer.
* **`tools/acceptance_record.py` treats an open seal in the captured run as unsatisfied**, by the same
  rule as `controller.open_seal_arenas` (a parity test pins the two together).
* **The seal ledger is written and read without following a symbolic link and with `st_nlink == 1`**
  (fail closed), so an append never reaches a file outside the run and a link to an empty file cannot
  hide an open seal.
* **A plan binds to its own arena**: a `planned` line that opens a seal is honoured only for the name
  this run's secret, and the iteration and attempt on the line, give (`controller.arena_name`).

The fakes model agents; nothing here starts Herdr or a provider. Every shape has a control that
changes only the condition under test. Not to be run as root (the seal's own tests need the cleared
write bits). Tests that leave a sealed arena or a 0o500 directory in `tmp_path` rely on pytest's own
cleanup; nothing is removed by hand.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_acceptance_record as _acc
import test_qa_arena_readonly as _qa
from test_acceptance_record import RUN, _problems, acc
from test_qa_arena_readonly import (
    Agents,
    _finish,
    _k10_scenario,
    _launcher_verdict,
    _real_verdict,
    _rewrite,
    build,
    deliver,
    events_for,
    forge,
    foreign_file,
    mode_of,
    state_on_disk,
    writable,
)

from hoh import capability as cap
from hoh import resumeloop, stages
from hoh import storage_park as spk
from hoh.contracts import Condition, RunState, digest
from hoh.controller import SEAL_LEDGER, open_seal_arenas
from hoh.resumption import Resumption, Verdict
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore

# The fixtures of the two files this one builds on, under their own names (an assignment, not an
# import, so pyflakes does not read every test parameter named `repo` as a redefinition, F811).
repo, spec, ev = _qa.repo, _qa.spec, _acc.ev

SRC = Path(__file__).resolve().parent.parent / "src"

PAST = "2026-09-25T09:00:00Z"
NOW = "2026-09-25T10:00:00Z"
OLD_RETRY = "2026-09-01T00:00:00+00:00"
VIOLATION = "capability violation: qa: a protected tree changed"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _run(root: Path, rid: str, **kw) -> RunStore:
    """A BLOCKED run state under `root`, as `tests/test_resumeloop.py` builds one."""
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p",
                               spec_path="/x/s.md", spec_digest="a" * 16,
                               policy_digest="b" * 16, profile_digest="c" * 16,
                               condition=kw.pop("condition", Condition.BLOCKED), **kw))
    return store


def _timer(root: Path, rid: str, kind: TimerKind = TimerKind.PROVIDER_RETRY):
    return TimerStore(root).arm_new(kind=kind, due_at=PAST, reason="t", program_epoch=0,
                                    run_id=rid)


def _forced_resume(timers, *, runs, now, program_epoch, killed=()):
    """A decision engine that is wrong: every armed timer is due and resumes."""
    return [Resumption(run_id=t.run_id, timer_id=t.timer_id, verdict=Verdict.RESUME,
                       why="forced by the test", due_at=t.due_at,
                       program_epoch=program_epoch, timer_epoch=t.program_epoch)
            for t in timers]


def _name(secret, iteration, attempt, suffix: str = "") -> str:
    """The arena directory name, spelt out here on purpose: `Controller._arena` has always used
    exactly this, and the tests of the binding must not depend on the function under test."""
    return digest(f"{secret}:{iteration}:{attempt}:{suffix}")[:12]


def _only(recs: list[dict], target: str) -> dict:
    [rec] = [r for r in recs if r.get("target") == target]
    return rec


# --------------------------------------------------------------------------- #
# 1. Every automatic resume path refuses a held run
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reason", [
    "capability violation: qa: a protected tree changed",
    "capability violation: qa: a protected tree changed (usage limit reached, resets at 5pm)",
    "usage limit reached, resets at 5pm. capability violation: qa: a protected tree changed",
], ids=["reason-only", "violation-then-quota-words", "quota-words-then-violation"])
def test_a_legacy_hold_is_not_unblocked_by_the_resume_loop(tmp_path, repo, spec, reason):
    """qaw8-a test_l1. A state an EARLIER build wrote: kind `usage_limit` (that build classified
    quota text first), a reason naming the violation, a past `retry_after`, no timer. Catches
    `_legacy_run` unblocking it: the launcher then reported the run ACCEPTED."""
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    held = _rewrite(store, condition=Condition.BLOCKED, blocked_kind="usage_limit",
                    blocked_reason=reason, stop_reason=reason,
                    retry_after="2020-01-01T00:00:00Z")
    assert stages.violation_unresolved(held), "the fixture is a hold"

    recs = resumeloop.resume_runs(tmp_path / "runs", now="2030-01-01T00:00:00Z")

    after = state_on_disk(store)
    rec = _only(recs, "r1")
    assert after.condition is Condition.BLOCKED, "the resume loop unblocked a (legacy) hold"
    assert after.resume_attempts == 0, "a refused resume is not an attempt"
    assert (rec["action"], rec["verdict"]) == ("refused", "NEEDS_OVERRIDE")
    assert "hoh unblock" in rec["why"]
    assert stages.violation_unresolved(after)
    assert _launcher_verdict(after).verdict is not RunVerdict.ACCEPTED


@pytest.mark.parametrize("timer", [False, True], ids=["no-timer", "armed-timer"])
def test_widening_the_classes_cannot_lift_a_hold(tmp_path, timer):
    """qaw8-a test_l2b. A caller that names `capability_violation` among the classes it owns:
    the sticky kind with no timer reaches `_legacy_run` and `quota.is_due(None)` is True. The
    timer shape is a control (rule 7 of `due_resumptions` refuses it, on the pin as well)."""
    store = _run(tmp_path, "v", blocked_kind="capability_violation", blocked_reason=VIOLATION,
                 stop_reason=VIOLATION)
    if timer:
        _timer(tmp_path, "v")
    classes = frozenset({"usage_limit", "capability_violation"})

    rec = _only(resumeloop.resume_runs(tmp_path, now=NOW, classes=classes), "v")

    after = store.read_state()
    assert after.condition is Condition.BLOCKED and after.blocked_kind == "capability_violation"
    assert (rec["action"], rec["verdict"]) == ("refused", "NEEDS_OVERRIDE")
    if timer:
        assert [t.status for t in TimerStore(tmp_path).read()] == [TimerStatus.ARMED]


@pytest.mark.parametrize("held", [True, False], ids=["held", "unheld-control"])
@pytest.mark.parametrize("shape", ["sticky-kind", "legacy-reason"])
def test_a_due_decision_cannot_lift_a_hold_whatever_decided_it(tmp_path, monkeypatch, shape, held):
    """`_resume_run` asks for the hold itself. The decision engine is replaced by one that says
    RESUME for every armed timer (as a bug in `due_resumptions` or in its caller would), and the
    classes are widened for the sticky shape so the class filter does not stand in front. The
    unheld control is resumed, so the refusal is the hold's and not the stand-in's."""
    monkeypatch.setattr(resumeloop, "due_resumptions", _forced_resume)
    if held and shape == "sticky-kind":
        kw = dict(blocked_kind="capability_violation", blocked_reason=VIOLATION)
    elif held:
        kw = dict(blocked_kind="usage_limit", blocked_reason=VIOLATION)
    else:
        kw = dict(blocked_kind="usage_limit", blocked_reason="usage limit reached")
    store = _run(tmp_path, "v", retry_after=OLD_RETRY, **kw)
    timer = _timer(tmp_path, "v")
    classes = frozenset({"usage_limit", "capability_violation"})

    rec = _only(resumeloop.resume_runs(tmp_path, now=NOW, classes=classes), "v")

    after = store.read_state()
    [stored] = TimerStore(tmp_path).read()
    if held:
        assert after.condition is Condition.BLOCKED, "a due decision lifted a hold"
        assert after.resume_attempts == 0
        assert rec["action"] == "refused" and "hoh unblock" in rec["why"]
        assert stored.status is TimerStatus.ARMED and stored.timer_id == timer.timer_id
    else:
        assert rec["action"] == "resumed"
        assert after.condition is Condition.ACTIVE
        assert stored.status is TimerStatus.FIRED


@pytest.mark.parametrize("held", [True, False], ids=["held", "unheld-control"])
def test_a_storage_decision_cannot_lift_a_hold(tmp_path, monkeypatch, held):
    """`resume_storage` asks for the hold itself. A run BLOCKED on `storage_pressure` whose reason
    names the violation, a decision forced to RESUME and a capacity that admits: the pin unblocks
    it. The unheld control (a plain storage reason) is resumed."""
    monkeypatch.setattr(resumeloop, "due_resumptions", _forced_resume)
    monkeypatch.setattr(resumeloop, "_capacity", lambda admission: (True, "OK", "999 B"))
    reason = VIOLATION if held else "storage pressure: free 1 B is under the reserve"
    store = _run(tmp_path, "s", blocked_kind="storage_pressure", blocked_reason=reason,
                 retry_after=OLD_RETRY)
    _timer(tmp_path, "s", kind=TimerKind.STORAGE_RECHECK)
    admission = SimpleNamespace(claim_boot=lambda: None)

    rec = _only(resumeloop.resume_storage(tmp_path, admission=admission, now=NOW,
                                          projects=False), "s")

    after = store.read_state()
    if held:
        assert after.condition is Condition.BLOCKED, "resume-storage lifted a hold"
        assert after.blocked_kind == "storage_pressure"
        assert rec["action"] == "refused" and "hoh unblock" in rec["why"]
        assert [t.status for t in TimerStore(tmp_path).read()] == [TimerStatus.ARMED]
    else:
        assert rec["action"] == "resumed" and after.condition is Condition.ACTIVE


# --------------------------------------------------------------------------- #
# 2. A storage refusal never parks a held run
# --------------------------------------------------------------------------- #


def _park(tmp_path: Path, **kw):
    store = _run(tmp_path, "p", **kw)
    state = store.read_state()
    refusal = SimpleNamespace(reason="free space is under the reserve", reserve=2, estimate=1,
                              reserved=0)
    parked = spk.park_for_storage_pressure(
        state, refusal, store=store, governor=None, policy=spk.ParkPolicy(),
        now=datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
    return parked, state, store


@pytest.mark.parametrize("shape,kw", [
    ("sticky-kind-active", dict(condition=Condition.ACTIVE, blocked_kind="capability_violation")),
    ("sticky-kind-blocked", dict(blocked_kind="capability_violation", blocked_reason=VIOLATION)),
    ("legacy-reason-blocked", dict(blocked_kind=None, blocked_reason=VIOLATION)),
    ("legacy-reason-paused", dict(condition=Condition.PAUSED,
                                  blocked_kind="collected_for_digest", blocked_reason=VIOLATION)),
])
def test_a_storage_refusal_never_parks_a_held_run(tmp_path, shape, kw):
    """The pin overwrites `blocked_kind` and `blocked_reason` with `storage_pressure`, and
    `hoh resume-storage` then lifts the hold. Now: not parked (False), the kind kept or adopted,
    the reason it already had kept, no `retry_after`, and no STORAGE_RECHECK timer armed."""
    parked, state, store = _park(tmp_path, **kw)

    for seen in (state, store.read_state()):             # the object and what is on disk
        assert stages.violation_unresolved(seen), "a storage park overwrote the hold"
        assert seen.blocked_kind == stages.CAPABILITY_VIOLATION
        assert seen.condition is Condition.BLOCKED
        assert seen.retry_after is None
        if kw.get("blocked_reason"):
            assert seen.blocked_reason == kw["blocked_reason"], "the reason it had is kept"
        else:
            assert "capability violation" in seen.blocked_reason
    assert TimerStore(tmp_path).read() == [], "no wake-up for a hold"
    assert any("not parked" in line for line in store.read_state().history[-3:])
    assert parked is False


def test_a_storage_refusal_still_parks_an_unheld_run(tmp_path):
    """The control: only the hold changes. An unheld ACTIVE run is parked exactly as before."""
    parked, state, store = _park(tmp_path, condition=Condition.ACTIVE)

    assert parked is True
    seen = store.read_state()
    assert (seen.condition, seen.blocked_kind) == (Condition.BLOCKED, "storage_pressure")
    assert seen.retry_after
    [timer] = TimerStore(tmp_path).read()
    assert timer.kind is TimerKind.STORAGE_RECHECK and timer.status is TimerStatus.ARMED


# --------------------------------------------------------------------------- #
# 3. tools/acceptance_record.py: an open seal is unsatisfied
# --------------------------------------------------------------------------- #


def _planned(arena: str) -> dict:
    return {"event": "planned", "arena": arena, "entries": {}}


def _released(arena: str) -> dict:
    return {"event": "released", "arena": arena}


def _write_ledger(ev, lines) -> Path:
    path = ev / "run-evidence" / RUN / SEAL_LEDGER
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines),
                    encoding="utf-8")
    return path


def test_an_open_seal_in_the_captured_run_is_not_an_acceptance(ev):
    """qaw8-a test_n1. The pin's tool never reads `seals.jsonl`, so a run whose QA arena was
    still sealed (its touch, if any, not yet compared) satisfied P1-16. Controls: the unmodified
    fixture, and a ledger whose only seal was released, raise no problem and add no key (the
    committed records keep re-deriving)."""
    assert _problems(ev) == []
    _write_ledger(ev, [_planned("/x/arenas/abc"), {"event": "sealed", "arena": "/x/arenas/abc"},
                       {"event": "releasing", "arena": "/x/arenas/abc"},
                       _released("/x/arenas/abc")])
    assert _problems(ev) == []
    assert "open_seals" not in acc.derive(ev)

    _write_ledger(ev, [_planned("/x/arenas/abc"), {"event": "sealed", "arena": "/x/arenas/abc"}])

    problems = _problems(ev)
    assert any("sealed for QA is still open" in p for p in problems), problems
    assert acc.derive(ev)["open_seals"] == ["/x/arenas/abc"]


LEDGERS = {
    "planned-only": [_planned("a")],
    "released": [_planned("a"), _released("a")],
    "second-planned-is-ignored": [_planned("a"), _planned("a"), _released("a")],
    "released-before-planned-closes-nothing": [_released("a"), _planned("a")],
    "one-of-two-open": [_planned("a"), _planned("b"), _released("a")],
    "reopened": [_planned("a"), _released("a"), _planned("a")],
    "garbage-lines-are-skipped": ["not json", "[1]", "{", _planned("a")],
    "arena-not-a-string": [{"event": "planned", "arena": 5}, {"event": "planned"}],
    "sealed-and-touched-only": [{"event": "sealed", "arena": "a"}, {"event": "touched", "arena": "a"}],
    "empty": [],
}


@pytest.mark.parametrize("name", sorted(LEDGERS))
def test_the_record_tool_and_the_product_agree_on_which_seals_are_open(ev, name):
    """The tool imports nothing from the product, so it carries its own copy of the rule. This
    pins the copy to `controller.open_seal_arenas` for the cases that decide it: first `planned`
    wins, `released` closes, a later `planned` reopens, garbage is skipped."""
    path = _write_ledger(ev, LEDGERS[name])

    assert acc.derive(ev).get("open_seals", []) == open_seal_arenas(path.parent)


@pytest.mark.parametrize("shape", ["invalid-utf8", "directory", "symlink"])
def test_a_ledger_the_record_tool_cannot_read_counts_as_an_open_seal(ev, shape):
    """What cannot be told is not "no open seal", as for `open_seal_arenas`: invalid bytes, a
    directory, or a symbolic link (a copy that kept one)."""
    path = ev / "run-evidence" / RUN / SEAL_LEDGER
    if shape == "invalid-utf8":
        path.write_bytes(b'{"event": "planned", "arena": "\xff\xfe"}\n')
    elif shape == "directory":
        path.mkdir()
    else:
        (ev / "elsewhere.jsonl").write_text("", encoding="utf-8")
        os.symlink(ev / "elsewhere.jsonl", path)

    record = acc.derive(ev)

    assert record["open_seals"] == [SEAL_LEDGER]
    assert any("sealed for QA is still open" in p for p in acc.satisfied(record))


def test_a_stale_record_of_a_run_with_an_open_seal_no_longer_re_derives(ev, tmp_path):
    """`--check` re-derives and compares: a record written before the evidence showed an open
    seal does not survive it (as for a held run)."""
    out = tmp_path / "record.json"
    assert acc.main(["--evidence", str(ev), "--out", str(out)]) == 0
    assert acc.main(["--evidence", str(ev), "--out", str(out), "--check"]) == 0
    _write_ledger(ev, [_planned("/x/arenas/abc")])
    assert acc.main(["--evidence", str(ev), "--out", str(out), "--check"]) == 1


# --------------------------------------------------------------------------- #
# 4. The seal ledger: no symbolic link, one name, never outside the run
# --------------------------------------------------------------------------- #


def _replace_ledger(store: RunStore, tmp_path: Path, shape: str, *, content: str = "") -> Path:
    """Move the real ledger aside and put `shape` in its place. Returns the file it points at
    (a symbolic link's target, or the second name's inode); for `directory`, the new directory."""
    led = store.dir / SEAL_LEDGER
    os.rename(led, led.with_name(SEAL_LEDGER + ".moved"))
    if shape == "directory":
        led.mkdir()
        return led
    other = tmp_path / "other.jsonl"
    other.write_text(content, encoding="utf-8")
    if shape == "symlink":
        os.symlink(other, led)
    else:
        os.link(other, led)
    return other


def test_a_ledger_replaced_by_a_symlink_is_never_appended_to(tmp_path, repo, spec):
    """qaw8-a test_m4. Between two dispatches a same-user pane replaces the ledger with a symlink
    to a file outside the run. The pin's next iteration appended its `planned` line to that
    file. Now the iteration does not seal and does not append: the run is held."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    victim = foreign_file(tmp_path, "victim.txt", 0o600)
    before = victim.read_bytes()
    led = store.dir / SEAL_LEDGER
    os.rename(led, led.with_name(SEAL_LEDGER + ".moved"))
    os.symlink(victim, led)

    out = ctrl.run_iteration(state)

    assert victim.read_bytes() == before, "an append reached a file outside the run"
    assert not out.accepted
    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.BLOCKED
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION


@pytest.mark.parametrize("shape", ["symlink", "hardlink"])
def test_the_seal_is_not_applied_when_the_ledger_cannot_be_written_safely(
        tmp_path, repo, spec, shape):
    """The write side alone (the read side would refuse first in a real iteration): the ledger is
    a symbolic link, or a second name of a file outside the run. `_seal_candidate` raises
    `SealFailed` before it changes a mode, and the file outside is untouched."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "f.txt").write_text("x\n", encoding="utf-8")
    victim = foreign_file(tmp_path, "victim.txt", 0o600)
    before = victim.read_bytes()
    led = store.dir / SEAL_LEDGER
    (os.symlink if shape == "symlink" else os.link)(victim, led)

    with pytest.raises(cap.SealFailed, match="ledger"):
        ctrl._seal_candidate(state, arena)

    assert victim.read_bytes() == before, "an append reached a file outside the run"
    assert writable(arena) != [], "nothing was sealed"
    assert mode_of(victim) == 0o600


@pytest.mark.parametrize("shape", ["symlink", "hardlink", "directory"])
def test_a_replaced_ledger_cannot_hide_an_open_seal(tmp_path, repo, spec, capsys, shape):
    """A seal is open (iteration 1's QA arena is kept sealed) and the pane replaces the ledger
    with a link to an EMPTY file. The pin reads no seal, reports the run ACCEPTED and delivers.
    Now the gate counts the ledger itself as one open seal. The directory shape is a control:
    the pin already counts it (an unreadable ledger)."""
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    led = store.dir / SEAL_LEDGER
    assert open_seal_arenas(store.dir) == [str(arena1)], "control: the seal is open"
    _replace_ledger(store, tmp_path, shape)

    assert open_seal_arenas(store.dir) == [str(led)]
    assert _real_verdict(tmp_path, repo).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "still open" in capsys.readouterr().err


@pytest.mark.parametrize("shape", ["symlink", "hardlink"])
def test_the_controller_blocks_the_run_on_an_untrusted_ledger_and_unseals_nothing(
        tmp_path, repo, spec, shape):
    """`_open_seals` refuses a ledger that is a symbolic link or has a second name, as it refuses
    a line naming a foreign path: the end of `hoh run` (`_finish_run`, which releases the seals)
    leaves the run BLOCKED with the violation kind and unseals nothing, and `compare_open_seals`
    and `release_open_seals` say why. The pin finds no seal at all and ends the run ACTIVE."""
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    _replace_ledger(store, tmp_path, shape)

    payload, _ = _finish(store, agents, ctrl, state)

    assert payload["condition"] == "BLOCKED", "the end of the run did not hold the run"
    assert writable(arena1) == [], "nothing was unsealed"
    held = state_on_disk(store)
    assert held.condition is Condition.BLOCKED and held.blocked_kind == stages.CAPABILITY_VIOLATION
    what = {"symlink": "is a symbolic link", "hardlink": "has 2 names, not one"}[shape]
    for call in (ctrl.compare_open_seals, ctrl.release_open_seals):
        refused = call().get("refused", "")
        assert "cannot be trusted" in refused and what in refused, (call.__name__, refused)
    assert writable(arena1) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_ledger_blocks_the_run_instead_of_crashing_it(tmp_path, repo, spec):
    """A ledger the controller cannot open (mode 000) was an uncaught `PermissionError` in
    `release_open_seals`; it is the same refusal as an untrusted one."""
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    led = store.dir / SEAL_LEDGER
    os.chmod(led, 0o000)
    try:
        result = ctrl.release_open_seals()
    finally:
        os.chmod(led, 0o644)
    assert "cannot be trusted" in result.get("refused", ""), result
    assert state_on_disk(store).blocked_kind == stages.CAPABILITY_VIOLATION


def test_a_fifo_ledger_is_refused_without_blocking(tmp_path):
    """A FIFO in the ledger's place hangs `read_text` forever (the open waits for a writer). The
    check runs in a child process with a timeout, so a regression is a failure and not a hang."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    os.mkfifo(run_dir / SEAL_LEDGER)
    code = ("import sys; from hoh.controller import open_seal_arenas; "
            "print(open_seal_arenas(sys.argv[1]))")

    done = subprocess.run([sys.executable, "-B", "-c", code, str(run_dir)],
                          env={**os.environ, "PYTHONPATH": str(SRC)},
                          capture_output=True, text=True, timeout=20)

    assert done.stdout.strip() == repr([str(run_dir / SEAL_LEDGER)]), done


def test_the_append_primitive_refuses_a_link_and_appends_to_a_plain_file(tmp_path):
    """`durable_append(single_link=True)`: a symbolic link (dangling or not), a second name and a
    non-regular file are `UnsafeRecordFile` (an `OSError`) with nothing written; a plain file, and
    a missing one, are appended to."""
    from hoh.durable_io import UnsafeRecordFile, durable_append

    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    link, dangling, second, fifo = (tmp_path / n for n in ("link", "dangling", "second", "fifo"))
    os.symlink(victim, link)
    os.symlink(tmp_path / "nowhere", dangling)
    os.link(victim, second)
    os.mkfifo(fifo)
    for path in (link, dangling, second, fifo):
        with pytest.raises(UnsafeRecordFile):
            durable_append(path, "line", single_link=True)
        assert isinstance(UnsafeRecordFile(path, "x"), OSError)
    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "nowhere").exists()

    plain = tmp_path / "plain.jsonl"
    durable_append(plain, "one", single_link=True)
    durable_append(plain, "two", single_link=True)
    assert plain.read_text(encoding="utf-8") == "one\ntwo\n"
    assert stat.S_ISREG(os.lstat(plain).st_mode) and os.lstat(plain).st_nlink == 1


# --------------------------------------------------------------------------- #
# 5. A plan binds to its own arena
# --------------------------------------------------------------------------- #


def _sibling(store: RunStore, name: str) -> Path:
    sib = store.arenas_dir / name
    sib.mkdir()
    os.chmod(sib, 0o500)
    return sib


def _forge_plan(store: RunStore, sib: Path, **fields) -> None:
    st = os.lstat(sib)
    forge(store, event="planned", arena=str(sib), entries={
        ".": [stat.S_IFDIR | 0o755, st.st_dev, st.st_ino]}, **fields)


def test_a_forged_plan_for_a_sibling_arena_is_refused(tmp_path, repo, spec):
    """qaw8-a test_m3. A forged `planned` line for another directory of the same run's arena
    root, with the right inode identity and a mode of 0o777: the pin's release chmods it. Now the
    plan is not bound to an arena this run sealed: refused, the run held, the mode unchanged."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    sib = _sibling(store, "sibling-of-the-same-run")
    try:
        st = os.lstat(sib)
        forge(store, event="planned", arena=str(sib),
              entries={".": [stat.S_IFDIR | 0o777, st.st_dev, st.st_ino]})

        result = ctrl.release_open_seals()

        assert mode_of(sib) == 0o500, "the release changed a mode it had no plan for"
        assert "not bound" in result.get("refused", ""), result
        held = state_on_disk(store)
        assert held.condition is Condition.BLOCKED
        assert held.blocked_kind == stages.CAPABILITY_VIOLATION
    finally:
        os.chmod(sib, 0o700)


@pytest.mark.parametrize("case", [
    "literal-name", "right-name-wrong-attempt", "right-name-wrong-iteration",
    "right-name-no-fields", "string-fields", "bool-fields", "no-run-secret",
])
def test_a_plan_is_refused_unless_its_name_is_the_one_its_iteration_and_attempt_give(
        tmp_path, repo, spec, case):
    """Each way a forged opening `planned` line can fail to be bound. The name must be exactly
    the one the run's secret gives to the ints on the line (`_name`), and the run must have a
    secret. `string-fields` and `bool-fields` carry names that WOULD match if the type were not
    checked."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    secret = None if case == "no-run-secret" else "s3cret"
    _rewrite(store, arena_secret=secret)
    name = {"literal-name": "a1",
            "bool-fields": _name(secret, True, True)}.get(case, _name(secret, 1, 1))
    fields = {"right-name-wrong-attempt": dict(iteration=1, attempt=2),
              "right-name-wrong-iteration": dict(iteration=2, attempt=1),
              "right-name-no-fields": {},
              "string-fields": dict(iteration="1", attempt="1"),
              "bool-fields": dict(iteration=True, attempt=True)}.get(case, dict(iteration=1, attempt=1))
    sib = _sibling(store, name)
    try:
        _forge_plan(store, sib, **fields)

        result = ctrl.release_open_seals()

        assert "not bound" in result.get("refused", ""), result
        assert mode_of(sib) == 0o500
    finally:
        os.chmod(sib, 0o700)


def test_a_line_with_the_name_its_own_iteration_and_attempt_give_is_the_stated_residual(
        tmp_path, repo, spec):
    """The control, and the limit stated plainly: the binding stops arbitrary names, not a pane
    that read the run's arena secret from `state.json`. A line that carries the name its own
    iteration and attempt give is honoured (and the sealed digest of such a directory is
    compared at the release as for any seal). If a keyed record ever replaces this, this test is
    the one to change."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    _rewrite(store, arena_secret="s3cret")
    sib = _sibling(store, _name("s3cret", 7, 1))
    _forge_plan(store, sib, iteration=7, attempt=1)

    seals = ctrl._open_seals()

    assert list(seals) == [str(sib)]
    assert ctrl.release_open_seals()["released"] == [str(sib)]
    assert mode_of(sib) == 0o755


def test_a_real_seal_carries_the_name_its_own_iteration_and_attempt_give(tmp_path, repo, spec):
    """The positive control on a real flow: the kept seal of iteration 1 is bound, `_open_seals`
    reads it without refusing, and `_arena` and the ledger check use the one `arena_name`."""
    from hoh.controller import arena_name

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    secret = state_on_disk(store).arena_secret
    [planned] = [e for e in events_for(store, arena1) if e["event"] == "planned"]
    assert Path(planned["arena"]).name == _name(secret, planned["iteration"], planned["attempt"])
    assert arena_name(secret, planned["iteration"], planned["attempt"]) == Path(planned["arena"]).name
    assert list(ctrl._open_seals()) == [str(arena1)]

    other = state_on_disk(store).model_copy(update={"iteration": 9, "attempt": 3})
    assert ctrl._arena(other).name == _name(secret, 9, 3) == arena_name(secret, 9, 3)
    assert ctrl._arena(other, suffix="basis").name == _name(secret, 9, 3, "basis")
    assert arena_name(secret, 9, 3, "basis") == _name(secret, 9, 3, "basis")
    assert arena_name(secret, 9, 3) != arena_name(secret, 9, 3, "basis")
